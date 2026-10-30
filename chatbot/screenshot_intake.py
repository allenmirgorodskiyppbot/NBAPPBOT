"""Resolve OCR cards against saved games and persist research-only screenshot sessions.

OCR never changes statistics, roster membership, or the placed-entry ledger.
Dates inferred from upload time are disclosed; unknown games stay unresolved.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo

from feeds import canonical_player
from master import STAT_COMPONENTS

EASTERN = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
SESSION_MINUTES = 30


def screenshot_date(message, now):
    dates = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', str(message.get('caption', ''))))
    if len(dates) > 1:
        raise ValueError('Use one game date in the screenshot caption.')
    if dates:
        return dt.date.fromisoformat(dates.pop()).isoformat(), 'caption'
    stamp = message.get('date')
    instant = dt.datetime.fromtimestamp(stamp, UTC) if type(stamp) in (int, float) else now
    return instant.astimezone(EASTERN).date().isoformat(), 'upload_day'


def roster_candidates(store, date):
    with store.connect() as db:
        rows = db.execute('WITH ranked AS (SELECT player,team,date,ROW_NUMBER() OVER '
            '(PARTITION BY player_id ORDER BY date DESC,updated_at DESC) AS rank '
            'FROM player_games WHERE date<=?) '
            'SELECT player,team,date FROM ranked WHERE rank=1 ORDER BY player', (date,)).fetchall()
    # Multiple identities with conflicting teams must not be guessed from a name.
    by_name = {}
    for row in rows:
        key = canonical_player(row['player'])
        by_name.setdefault(key, []).append(dict(row))
    return [{'name': values[0]['player'], 'team': values[0]['team'], 'roster_asof': values[0]['date']}
            for values in by_name.values() if values[0]['team'] and len({v['team'] for v in values}) == 1]


def resolve_cards(store, extraction, date, roster):
    """Require a single known game for the player's stored team on the given day."""
    players = {canonical_player(row['name']): row for row in roster}
    with store.connect() as db:
        games = [dict(row) for row in db.execute('SELECT id,home_team,away_team,start_time,status '
                                               'FROM season_schedule WHERE date=?', (date,))]
        observed = [dict(row) for row in db.execute('SELECT id,home_team,away_team,start_time,status '
                                                  'FROM games WHERE date=?', (date,))]
    by_id = {row['id']: row for row in games}
    for row in observed:
        prior = by_id.get(row['id'], {})
        by_id[row['id']] = {**prior, **{k: v for k, v in row.items() if v is not None}}
    unresolved = [str(value)[:160] for value in extraction.get('unresolved', [])[:20]]
    legs = []
    for card in extraction.get('cards', [])[:50]:
        player = players.get(canonical_player(card.get('player', '')))
        label = str(card.get('player') or 'Unclear player')[:70]
        if not player or card.get('team') != player['team']:
            unresolved.append(label + ': stored team is unresolved.')
            continue
        matches = [g for g in by_id.values() if player['team'] in (g['home_team'], g['away_team'])]
        if len(matches) != 1:
            unresolved.append(label + ': no single saved game for ' + date + '; date/team needs checking.')
            continue
        game = matches[0]
        if game['status'] not in {'scheduled', 'live', 'final'}:
            unresolved.append(label + ': saved game is postponed, canceled or its time is unknown.')
            continue
        if card.get('stat') not in STAT_COMPONENTS:
            unresolved.append(label + ': this statistic is not supported.')
            continue
        sides = set(card.get('sides', []))
        if not sides or not sides <= {'more', 'less'}:
            unresolved.append(label + ': available More/Less options are unclear.')
            continue
        opponent = game['away_team'] if player['team'] == game['home_team'] else game['home_team']
        for side in sorted(sides):
            variants = card.get('offer_variants') or {}
            variant = variants.get(side, 'unknown') if isinstance(variants, dict) else 'unknown'
            if variant not in {'standard', 'goblin', 'demon', 'unknown'}:
                variant = 'unknown'
            legs.append({'player': player['name'], 'team': player['team'], 'opponent': opponent,
                'game_id': game['id'], 'date': date, 'stat': card['stat'], 'line': card['line'],
                'side': side, 'period': 'full_game', 'screenshot_unconfirmed': True,
                'roster_asof': player['roster_asof'], 'line_source': 'screenshot OCR',
                'offer_terms': 'unverified', 'offer_variant': variant, 'available_alternative': True})
    # Apply the same finite-number/stat validation as manual boards, before writes.
    legs = store._legs(legs) if legs else []
    return legs, list(dict.fromkeys(unresolved))[:20]


def initialize_tables(store):
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_screenshot_items (
                update_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                game_date TEXT NOT NULL, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS screenshot_chat_date ON telegram_screenshot_items(chat_id,game_date);
            CREATE TABLE IF NOT EXISTS telegram_screenshot_boards (
                chat_id INTEGER NOT NULL, revision TEXT NOT NULL, board_id TEXT NOT NULL,
                PRIMARY KEY(chat_id,revision)
            );
        ''')


def save_extraction(store, chat_id, update_id, date, date_basis, extraction, legs, unresolved, now):
    initialize_tables(store)
    payload = {'date_basis': date_basis, 'cards': extraction.get('cards', [])[:50],
               'legs': legs, 'unresolved': unresolved, 'reader_version': extraction.get('reader_version')}
    draft_id = uuid.uuid5(uuid.NAMESPACE_URL, f'knicks:screenshot:{chat_id}:{update_id}').hex
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO telegram_drafts '
                   '(id,update_id,chat_id,kind,text,created_at) VALUES (?,?,?,?,?,?)',
                   (draft_id, update_id, chat_id, 'screenshot_cards', json.dumps({**payload,
                    'raw_text': str(extraction.get('raw_text', ''))[:16000]}), now.isoformat()))
        db.execute('INSERT OR IGNORE INTO telegram_screenshot_items VALUES (?,?,?,?,?)',
                   (update_id, chat_id, date, now.isoformat(), json.dumps(payload)))
    return draft_id


def combined_board(store, chat_id, date, now, reset_at=None, reset_update=-1):
    """Atomically link immutable merged snapshots; duplicates reuse their board."""
    initialize_tables(store)
    cutoff = (now - dt.timedelta(minutes=SESSION_MINUTES)).isoformat()
    # Telegram update order is authoritative for a reset. Wall-clock creation
    # order can differ when OCR was queued or an album was debounced.
    if reset_at and reset_update < 0:
        cutoff = max(cutoff, str(reset_at))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        records = db.execute('SELECT payload_json FROM telegram_screenshot_items '
            'WHERE chat_id=? AND game_date=? AND created_at>=? AND update_id>? '
            'ORDER BY update_id DESC LIMIT 20', (chat_id, date, cutoff, reset_update)).fetchall()
        unique, unresolved, assumed = {}, [], False
        payloads = [json.loads(record['payload_json']) for record in records]
        # An upgraded reader must not inherit cards inferred by an older reader.
        # Immutable older boards remain available, but current research is built
        # from the newest upload's reader version only.
        version = payloads[0].get('reader_version') if payloads else None
        payloads = [payload for payload in payloads if payload.get('reader_version') == version]
        for payload in payloads:
            assumed |= payload.get('date_basis') == 'upload_day'
            unresolved.extend(payload.get('unresolved', []))
            for leg in payload.get('legs', []):
                key = (leg['player'], leg['stat'], leg['line'], leg['side'], leg['date'], leg['game_id'],
                       leg.get('offer_variant', 'unknown'))
                unique.setdefault(key, leg)
        legs = [unique[key] for key in sorted(unique)]
        if len(legs) > 100:
            return {'board': None, 'unresolved': ['More than 100 alternatives. Use /newboard and send fewer cards.']}
        if not legs:
            return {'board': None, 'unresolved': list(dict.fromkeys(unresolved))[:20]}
        clean = store._legs(legs)
        revision = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
        prior = db.execute('SELECT board_id FROM telegram_screenshot_boards WHERE chat_id=? AND revision=?',
                           (chat_id, revision)).fetchone()
        if prior:
            board_id = prior['board_id']
        else:
            board_id = uuid.uuid4().hex
            db.execute('INSERT INTO boards VALUES (?,?,?,?)',
                       (board_id, now.isoformat(), f'screenshot:{chat_id}:{revision}', json.dumps(clean)))
            db.execute('INSERT INTO telegram_screenshot_boards VALUES (?,?,?)', (chat_id, revision, board_id))
    return {'board': store.get_board(board_id), 'unresolved': list(dict.fromkeys(unresolved))[:20],
            'date_assumed': assumed, 'date': date, 'reused': bool(prior), 'upload_count': len(payloads),
            'market_count': len({(l['player'], l['stat'], l['line'], l['date']) for l in legs})}
