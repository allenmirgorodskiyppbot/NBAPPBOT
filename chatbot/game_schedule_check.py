"""Bounded current-day event checks; never import player statistics or settle games."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from feeds import ESPN, TEAM_WORDS, parse_game
from news_digest import _aware

EASTERN = ZoneInfo('America/New_York')
CHECKPOINT = 'nba_schedule_today'
CHECK_SECONDS = 900
SUCCESS_CACHE_SECONDS = 840
RELAY_URL = 'https://raw.githubusercontent.com/allenmirgorodskiyppbot/NBAPPBOT/main/data/live/schedule_today.json'


async def _database_write(operation, *args, **kwargs):
    """Keep locks owned until each real DB attempt settles; retry BUSY/LOCKED only."""
    from dataset_update import _complete_callback, _sqlite_contention
    delay = .05
    while True:
        try:
            return await _complete_callback(lambda: asyncio.to_thread(operation, *args, **kwargs))
        except sqlite3.Error as error:
            if not _sqlite_contention(error):
                raise
            await asyncio.sleep(delay)
            delay = min(1, delay * 2)


def _metadata(store):
    from dataset_update import _ControllerStore
    return _ControllerStore(store)


def _merge_current_events(store, known, events):
    """One short transaction; timing only, preserving final scores and statistics."""
    conflicts, matched = [], []
    with store.connect() as db:
        db.execute('PRAGMA busy_timeout=100')
        db.execute('BEGIN IMMEDIATE')
        for identity in sorted(set(known) & set(events)):
            event = events[identity]
            old = db.execute('SELECT * FROM games WHERE id=?', (identity,)).fetchone()
            old = dict(old) if old else {}
            reported = event['status']
            status = 'past_schedule_only' if reported == 'final' else reported
            if old.get('status') == 'final':
                status = 'final'
            elif old.get('status') in {'live', 'suspended', 'past_schedule_only'} and status == 'scheduled':
                status = old['status']; conflicts.append(identity)
            observation = {key: event[key] for key in ('id', 'date', 'home_team', 'away_team',
                           'start_time', 'provider_game_id', 'source')}
            observation['status'] = status
            if old.get('status') != 'final':
                store.upsert_game({**old, **observation}, db=db)
            matched.append({**observation, 'reported_status': reported})
    return conflicts, matched


def _record_cancelled(store, report):
    metadata = _metadata(store)
    metadata.set_checkpoint(CHECKPOINT, {**report, 'status': 'failed', 'error': 'CancelledError'})
    metadata.feed_failure('nba_schedule_today', 'CancelledError; current event timing is unverified.')


def _known_games(store, day):
    with store.connect() as db:
        saved = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE date=?', (day,))]
        observed = [dict(row) for row in db.execute('SELECT * FROM games WHERE date=?', (day,))]
    games = {row['id']: row for row in saved}
    for row in observed:
        games[row['id']] = {**games.get(row['id'], {}), **{k:v for k,v in row.items() if v is not None}}
    return games


def _parse_today(document, day):
    if not isinstance(document, dict) or not isinstance(document.get('events'), list):
        raise ValueError('Current NBA scoreboard is missing its event inventory.')
    leagues = document.get('leagues')
    if not isinstance(leagues, list) or not any(isinstance(x, dict) and x.get('abbreviation') == 'NBA' for x in leagues):
        raise ValueError('Current event inventory does not identify the NBA.')
    if len(document['events']) > 30:
        raise ValueError('Current NBA event inventory exceeds its expected size.')
    result = {}
    names = {'STATUS_SCHEDULED', 'STATUS_DELAYED', 'STATUS_IN_PROGRESS', 'STATUS_HALFTIME',
             'STATUS_END_PERIOD', 'STATUS_FINAL', 'STATUS_FINAL_OT', 'STATUS_POSTPONED',
             'STATUS_CANCELED', 'STATUS_SUSPENDED'}
    for event in document['events']:
        competition = (event.get('competitions') or [{}])[0]
        tip = _aware(event.get('date') or competition.get('date'))
        state = (event.get('status') or competition.get('status') or {}).get('type', {})
        if not tip or tip.astimezone(EASTERN).date().isoformat() != day or state.get('name') not in names:
            raise ValueError('Current event date, timezone or status is unresolved.')
        game = parse_game(event)
        from game_phase import published_event_exclusion
        if published_event_exclusion(event,game):
            raise ValueError('Current event is outside the regular-season strategy population.')
        if (game['date'] != day or game['home_team'] not in TEAM_WORDS or game['away_team'] not in TEAM_WORDS
                or game['home_team'] == game['away_team'] or game['id'] in result):
            raise ValueError('Current event identity is invalid or duplicated.')
        if state.get('name') == 'STATUS_SUSPENDED':
            game['status'] = 'suspended'
        game['start_time'] = tip.isoformat()
        result[game['id']] = game
    return result


def _parse_nba_today(document, day):
    """The live NBA scoreboard has an explicit date; a saved calendar is not used."""
    from nba_cdn import SCOREBOARD_URL, parse_game as nba_game
    board = document.get('scoreboard') if isinstance(document, dict) else None
    if (not isinstance(board, dict) or board.get('gameDate') != day
            or not isinstance(board.get('games'), list) or len(board['games']) > 30):
        raise ValueError('Official current NBA scoreboard date or inventory is unresolved.')
    result = {}
    for raw in board['games']:
        game = nba_game(raw, SCOREBOARD_URL)
        tip = _aware(game.get('start_time'))
        if (not tip or game['date'] != day or game['home_team'] not in TEAM_WORDS
                or game['away_team'] not in TEAM_WORDS or game['id'] in result
                or game.get('nba_season_type') != 'Regular Season'):
            raise ValueError('Official current NBA event identity or timing is unresolved.')
        result[game['id']] = game
    return result


def _parse_relay(document, day, now):
    from nba_cdn import SCOREBOARD_URL
    if not isinstance(document, dict) or document.get('schema_version') != 1 or document.get('date') != day:
        raise ValueError('Published schedule relay has the wrong schema or date.')
    checked = _aware(document.get('source_checked_at'))
    if not checked or not 0 <= (now - checked).total_seconds() < SUCCESS_CACHE_SECONDS:
        raise ValueError('Published schedule relay is stale or has a future check time.')
    payload = document.get('document')
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if document.get('source_content_sha256') != digest:
        raise ValueError('Published schedule relay content does not match its recorded digest.')
    source = document.get('source_url')
    if source == ESPN + 'scoreboard?dates=' + day.replace('-', ''):
        games = _parse_today(payload, day)
    elif source == SCOREBOARD_URL:
        games = _parse_nba_today(payload, day)
    else:
        raise ValueError('Published schedule relay has an unsupported upstream source.')
    return games, checked, source


async def _current_events(worker, day, now):
    """Try independent publishers once, then a timestamp-preserving trusted relay."""
    from nba_cdn import SCOREBOARD_URL
    attempts = []
    for source, parser in ((ESPN + 'scoreboard?dates=' + day.replace('-', ''), _parse_today),
                           (SCOREBOARD_URL, _parse_nba_today), (RELAY_URL, None)):
        try:
            async with asyncio.timeout(10):
                document = await worker.request(source)
            if parser is None:
                games, checked, upstream = _parse_relay(document, day, now)
                return games, checked, upstream, source, attempts
            return parser(document, day), now, source, None, attempts
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = getattr(getattr(error, 'response', None), 'status_code', None)
            detail = type(error).__name__ + (f': HTTP {code}' if code else '')
            attempts.append({'source': source, 'error': detail})
    error = RuntimeError('All current schedule sources are unavailable.')
    error.schedule_attempts = attempts
    raise error


async def refresh_game_schedule(store, worker, now=None):
    """Check existing IDs; successful timing checks refresh before the 15-minute gate.

    A failed fetch retains previous observations but makes the check failed.
    No absence is interpreted as a cancellation or a verified league off day.
    """
    now = now or (worker.now() if callable(getattr(worker, 'now', None)) else dt.datetime.now(dt.timezone.utc))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Current schedule checks require an aware clock.')
    day = now.astimezone(EASTERN).date().isoformat()
    known = _known_games(store, day)
    if not known:
        return {'date':day, 'status':'not_applicable', 'games':[], 'checked_at':now.isoformat(),
                'error':None, 'source':None}
    loop = asyncio.get_running_loop()
    if getattr(worker, '_schedule_check_loop', None) is not loop:
        worker._schedule_check_loop = loop
        worker._schedule_check_lock = asyncio.Lock()
    async with worker._schedule_check_lock:
        prior = store.get_checkpoint(CHECKPOINT, {}) or {}
        checked = _aware(prior.get('checked_at'))
        cache_seconds = SUCCESS_CACHE_SECONDS if prior.get('status') == 'success' else CHECK_SECONDS
        if (prior.get('date') == day and checked and 0 <= (now-checked).total_seconds() < cache_seconds
                and set(prior.get('expected_game_ids', [])) == set(known)):
            return prior
        source = ESPN + 'scoreboard?dates=' + day.replace('-', '')
        report = {'date':day, 'status':'failed', 'checked_at':now.isoformat(), 'source':source,
                  'expected_game_ids':sorted(known), 'games':prior.get('games', []) if prior.get('date') == day else [],
                  'last_success_at':prior.get('last_success_at'), 'error':None}
        await _database_write(_metadata(store).set_checkpoint, CHECKPOINT, {**report, 'status':'checking'})
        try:
            events, verified_at, source, relay, attempts = await _current_events(worker, day, now)
            report.update(checked_at=verified_at.isoformat(), attempted_at=now.isoformat(),
                          source=source, relay_url=relay, attempts=attempts)
            missing = sorted(set(known)-set(events))
            conflicts, matched = await _database_write(_merge_current_events, store, known, events)
            report.update(games=matched, missing_game_ids=missing, conflicting_game_ids=conflicts)
            if missing or conflicts:
                report.update(status='partial', error='Current event inventory is missing or conflicts with saved events.')
            else:
                report.update(status='success', last_success_at=verified_at.isoformat())
        except asyncio.CancelledError:
            from dataset_update import _complete_callback, _sqlite_contention
            try:
                await _complete_callback(lambda: asyncio.to_thread(_record_cancelled, store, report))
            except sqlite3.Error as error:
                if not _sqlite_contention(error):
                    raise
            raise
        except sqlite3.Error:
            raise
        except Exception as error:
            code = getattr(getattr(error, 'response', None), 'status_code', None)
            attempts = getattr(error, 'schedule_attempts', [])
            report['attempts'] = attempts
            report['error'] = attempts[0]['error'] if attempts else type(error).__name__ + (f': HTTP {code}' if code else '')
        await _database_write(_metadata(store).set_checkpoint, CHECKPOINT, report)
        if report['status'] == 'success':
            await _database_write(_metadata(store).feed_success, 'nba_schedule_today', 'Current event timing checked; existing games only; no player statistics imported.')
        else:
            await _database_write(_metadata(store).feed_failure, 'nba_schedule_today', report['error'] + '; current event timing is unverified.')
        return report
