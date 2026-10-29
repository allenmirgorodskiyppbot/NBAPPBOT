"""Strict, provenance-preserving manual NBA statistics and news uploads.

Uploads are user-supplied evidence, never an official or live-verified feed.
The complete document is validated before any game bundle is published.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

from feeds import canonical_game, canonical_player, canonical_team, TEAM_WORDS

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
REQUIRED = {'date', 'player', 'team', 'opponent', 'game_status', 'appearance_state'}
COUNTS = ('pts', 'reb', 'ast', 'tpm', 'fga', 'tpa', 'stl', 'blk', 'tov')
STATS = COUNTS + ('minutes',)
NULL_WORDS = {'', 'null', 'none', 'na', 'n/a', '--', '—'}
_IMPORT_LOCK = threading.RLock()


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _today_eastern():
    return dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()


def _key(value):
    if not isinstance(value, str):
        raise ValueError('Column names must be strings.')
    return re.sub(r'\s+', '_', value.strip().casefold())


def _row_keys(row):
    if not isinstance(row, dict):
        raise ValueError('Each statistics row must be an object.')
    result = {}
    for key, value in row.items():
        normalized = _key(key)
        if not normalized or normalized in result:
            raise ValueError('Column names must be nonempty and unique after normalization.')
        result[normalized] = value
    return result


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('JSON contains a duplicate field: ' + str(key)[:50])
        obj[key] = value
    return obj


def _finite_json(value):
    raise ValueError('JSON numbers must be finite; NaN and Infinity are not supported.')


def parse_stats_document(filename, payload):
    """Parse UTF-8 CSV/JSON statistics, separate from PrizePicks board documents."""
    if not isinstance(filename, str) or len(filename) > 255:
        raise ValueError('Supply a CSV or JSON filename of at most 255 characters.')
    suffix = Path(filename).suffix.casefold()
    if suffix not in {'.csv', '.json'}:
        raise ValueError('Statistics uploads support CSV or JSON only. Save an Excel sheet as CSV first.')
    if isinstance(payload, str):
        try:
            raw = payload.encode('utf-8')
        except UnicodeError:
            raise ValueError('Statistics documents must contain valid UTF-8 text.') from None
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError('Statistics document contents must be UTF-8 text or bytes.')
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Statistics documents must contain 1 byte to 2 MiB.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Statistics documents must be UTF-8 CSV or JSON.') from None
    if '\x00' in text:
        raise ValueError('Statistics documents cannot contain NUL bytes.')
    try:
        if suffix == '.json':
            rows = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_finite_json)
            if not isinstance(rows, list):
                raise ValueError('Statistics JSON must be a list of box-score row objects, not a board or entry.')
        else:
            reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
            if not reader.fieldnames:
                raise ValueError('Statistics CSV needs a header row.')
            normalized_headers = [_key(name) for name in reader.fieldnames]
            if len(set(normalized_headers)) != len(normalized_headers) or not all(normalized_headers):
                raise ValueError('Statistics CSV column names must be nonempty and unique.')
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('Every statistics CSV row must have the same number of columns as its header.')
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise ValueError('Statistics documents support at most 2000 rows.')
    except (json.JSONDecodeError, csv.Error, RecursionError) as error:
        raise ValueError('Statistics document could not be parsed as valid CSV/JSON.') from error
    if not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError('Statistics documents need 1–2000 box-score rows.')
    normalized = [_row_keys(row) for row in rows]
    for number, row in enumerate(normalized, 1):
        missing = REQUIRED - row.keys()
        if missing:
            raise ValueError(f'Row {number} is not a statistics row; missing ' + ', '.join(sorted(missing))
                             + '. PrizePicks lines use the separate board upload.')
    return normalized


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{field} must contain 1–{limit} text characters.')
    value = value.strip()
    if any(ord(character) < 32 for character in value):
        raise ValueError(f'{field} cannot contain control characters.')
    return value


def _team(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a recognized NBA team abbreviation.')
    result = canonical_team(value)
    if result not in TEAM_WORDS:
        raise ValueError(f'{field} must be a recognized NBA team abbreviation, such as NYK or BOS.')
    return result


def _number(value, field):
    if value is None or (isinstance(value, str) and value.strip().casefold() in NULL_WORDS):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f'{field} must be numeric or null; booleans are not statistics.')
    if field == 'minutes' and isinstance(value, str) and ':' in value:
        match = re.fullmatch(r'(\d{1,3}):([0-5]\d)', value.strip())
        if not match:
            raise ValueError('minutes must be a decimal number or MM:SS with seconds from 00 to 59.')
        return int(match[1]) + int(match[2]) / 60
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{field} must be a finite nonnegative number or null.') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'{field} must be a finite nonnegative number or null.')
    if field != 'minutes' and not result.is_integer():
        raise ValueError(f'{field} must be a whole-number count.')
    return result


def _normalize_rows(rows, source, stamp):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError('Supply a list of 1–2000 statistics rows.')
    try:
        document = json.dumps(rows, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError('Statistics rows must be finite JSON-compatible values.') from None
    if len(document.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Statistics uploads cannot exceed 2 MiB.')
    digest = hashlib.sha256(document.encode('utf-8')).hexdigest()
    groups, seen = {}, set()
    today = _today_eastern()
    for number, original in enumerate(rows, 1):
        try:
            row = _row_keys(original)
            if REQUIRED - row.keys():
                raise ValueError('Missing required statistics columns: '
                                 + ', '.join(sorted(REQUIRED - row.keys())))
            date = row['date']
            if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                raise ValueError('date must be exactly YYYY-MM-DD.')
            dt.date.fromisoformat(date)
            player = _text(row['player'], 'player', 120)
            team, opponent = _team(row['team'], 'team'), _team(row['opponent'], 'opponent')
            if team == opponent:
                raise ValueError('A team cannot play itself.')
            status = _text(row['game_status'], 'game_status', 20).lower()
            if status not in {'final', 'live'}:
                raise ValueError('Statistics uploads require game_status final or live; '
                                 'scheduled games do not yet have actual box scores.')
            if date > today:
                raise ValueError('A future Eastern calendar date cannot be marked live or final.')
            appearance = _text(row['appearance_state'], 'appearance_state', 20).casefold()
            if appearance not in {'played', 'dnp'}:
                raise ValueError('appearance_state must be Played or DNP.')
            appearance = 'Played' if appearance == 'played' else 'DNP'
            for field in ('period', 'market_period'):
                if row.get(field) in (None, ''):
                    continue
                period = str(row[field]).casefold().replace('-', '_').replace(' ', '_')
                if period not in {'full', 'game', 'full_game'}:
                    raise ValueError('Statistics uploads require full-game box scores; partial-game periods are unsupported.')
            game_id = canonical_game(date, team, opponent)
            player_id = canonical_player(player)
            if not re.search(r'[a-z]', player_id.removeprefix('name:')):
                raise ValueError('player needs a recognizable name, not only numbers or punctuation.')
            if row.get('game_id') not in (None, '') and row['game_id'] != game_id:
                raise ValueError('game_id conflicts with the canonical date/team/opponent game ID.')
            if row.get('player_id') not in (None, '') and row['player_id'] != player_id:
                raise ValueError('player_id conflicts with the canonical player name ID.')
            key = game_id, player_id
            if key in seen:
                raise ValueError('Duplicate player-game in this upload.')
            seen.add(key)
            stats = {field: _number(row[field], field) for field in STATS if field in row}
            if appearance == 'Played':
                if stats.get('minutes') is None or stats['minutes'] <= 0:
                    raise ValueError('Played observations require positive actual minutes.')
                if status == 'final' and stats.get('pts') is None:
                    raise ValueError('A final Played observation requires pts, including an explicit zero when appropriate.')
            else:
                if any(value is not None and value != 0 for value in stats.values()):
                    raise ValueError('DNP observations cannot contain nonzero playing statistics.')
                stats = {field: None for field in STATS}
            game = {'id': game_id, 'date': date, 'status': status, 'source': source,
                    'manual_unverified': True, 'coverage': 'Uploaded player rows only; full roster not verified.',
                    'observed_at': stamp}
            if row.get('home_team') or row.get('away_team'):
                home = _team(row.get('home_team'), 'home_team')
                away = _team(row.get('away_team'), 'away_team')
                if home == away or {home, away} != {team, opponent}:
                    raise ValueError('Explicit home_team/away_team must match this game’s two teams.')
                game.update(home_team=home, away_team=away)
            if game_id in groups:
                old_game = groups[game_id]['game']
                if old_game['status'] != status:
                    raise ValueError('Rows for the same game have conflicting game_status values.')
                if game.get('home_team') and old_game.get('home_team') and (
                        game['home_team'], game['away_team']) != (old_game['home_team'], old_game['away_team']):
                    raise ValueError('Rows for the same game have conflicting home/away teams.')
                if game.get('home_team'):
                    old_game.update(home_team=game['home_team'], away_team=game['away_team'])
            else:
                groups[game_id] = {'game': game, 'rows': []}
            record = {
                'game_id': game_id, 'player_id': player_id, 'player': player, 'team': team,
                'opponent': opponent, 'date': date, 'appearance_state': appearance, 'source': source,
                'manual_unverified': True, 'observed_at': stamp, 'upload_row': number,
                'original_upload': dict(original), **stats,
            }
            year, month = int(date[:4]), int(date[5:7])
            season_start = year if month >= 7 else year - 1
            record['season'] = f'{season_start}-{str(season_start + 1)[-2:]}'
            record['season_label_inferred_from_date'] = True
            # The calendar cannot establish whether a game is playoffs, preseason,
            # or regular season; a missing phase stays explicitly Unknown.
            record['season_type'] = 'Unknown'
            for field in ('season', 'season_type'):
                if row.get(field) not in (None, ''):
                    record[field] = _text(row[field], field, 60)
                    if field == 'season':
                        record['season_label_inferred_from_date'] = False
            groups[game_id]['rows'].append(record)
        except (ValueError, TypeError) as error:
            raise ValueError(f'Row {number}: {error}') from None
    return groups, digest


def _existing_conflicts(store, groups):
    with store.connect() as db:
        for game_id, bundle in groups.items():
            old_game = db.execute('SELECT status FROM games WHERE id=?', (game_id,)).fetchone()
            if old_game and str(old_game['status']).lower() == 'final' and bundle['game']['status'] != 'final':
                raise ValueError('An existing final game cannot be downgraded to live by a manual upload.')
            for row in list(bundle['rows']):
                old = db.execute('SELECT appearance_state,season,season_type FROM player_games WHERE game_id=? AND player_id=?',
                                 (game_id, row['player_id'])).fetchone()
                if old:
                    # An omitted optional scope field must not erase a phase/season
                    # already established by an earlier source for this observation.
                    original = _row_keys(row['original_upload'])
                    for field in ('season', 'season_type'):
                        if original.get(field) in (None, '') and old[field]:
                            row[field] = old[field]
                old_appearance = str(old['appearance_state'] or '').casefold() if old else ''
                if old_appearance in {'played', 'dnp', 'did not play', 'inactive', 'out', 'not played'}:
                    old_played = old_appearance == 'played'
                    if old_played != (row['appearance_state'] == 'Played'):
                        raise ValueError(f"Row {row['upload_row']}: recorded participation conflicts with "
                                         'the upload (Played versus DNP). Review the correction before importing.')
                # A final upload cannot promote omitted fields from a prior live
                # observation into supposedly final statistics.
                if old_game and str(old_game['status']).lower() != 'final' and bundle['game']['status'] == 'final':
                    for field in STATS:
                        if field not in row:
                            row[field] = None
            if old_game and str(old_game['status']).lower() != 'final' and bundle['game']['status'] == 'final':
                uploaded = {row['player_id'] for row in bundle['rows']}
                omitted = [old['player'] for old in db.execute(
                    'SELECT player_id,player FROM player_games WHERE game_id=?', (game_id,))
                    if old['player_id'] not in uploaded]
                if omitted:
                    raise ValueError('Final upload omits previously observed live players: '
                                     + ', '.join(str(name) for name in omitted[:8])
                                     + '. Include their final rows to avoid grading stale live statistics as final.')


def import_stats(store, rows, source='Manual user upload'):
    """Validate all rows, then publish atomic per-game bundles and regrade entries.

    Invalid input never publishes a partial document. An unexpected I/O failure
    during a multi-game publish is recorded as partial_failed with committed IDs;
    each individual game bundle remains atomic and can be safely retried.
    """
    label = _text(source, 'source', 400)
    label = 'Manual user upload' if label == 'Manual user upload' else 'Manual user upload | ' + label
    stamp = _stamp()
    with _IMPORT_LOCK:
        groups, digest = _normalize_rows(rows, label, stamp)
        _existing_conflicts(store, groups)
        import_id = uuid.uuid4().hex
        summary = {'id': import_id, 'import_id': import_id, 'source': label,
                   'created_at': stamp, 'status': 'running', 'rows': len(rows),
                   'player_games': len(rows), 'games': len(groups), 'sha256': digest,
                   'manual_unverified': True, 'committed_game_ids': [],
                   'player_rows_changed': 0, 'game_rows_changed': 0,
                   'warnings': ['User-supplied statistics; not official or live-verified.',
                                'Only uploaded player rows are covered; full game/roster completeness is not verified.']}
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS manual_imports (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, completed_at TEXT,
                source TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                summary_json TEXT NOT NULL)''')
            db.execute('INSERT INTO manual_imports VALUES (?,?,NULL,?,?,?,?)',
                       (import_id, stamp, label, digest, 'running', json.dumps(summary)))
        try:
            for game_id, bundle in groups.items():
                bundle['game']['manual_import_id'] = import_id
                for row in bundle['rows']:
                    row['manual_import_id'] = import_id
                outcome = store.upsert_game_bundle(bundle['game'], bundle['rows'])
                summary['committed_game_ids'].append(game_id)
                summary['player_rows_changed'] += int(outcome['player_rows_changed'])
                summary['game_rows_changed'] += int(bool(outcome['game_changed']))
                with store.connect() as db:
                    db.execute('UPDATE manual_imports SET summary_json=? WHERE id=?',
                               (json.dumps(summary), import_id))
            summary['status'] = 'complete'
            summary['completed_at'] = _stamp()
            try:
                grade_updates = store.grade_entries()
                summary['grade_updates'] = len(grade_updates)
                summary['graded_entry_ids'] = [entry['id'] for entry in grade_updates]
            except Exception:
                summary['grade_updates'] = 0
                summary['warnings'].append('Statistics were saved, but entry grading failed; inspect the ledger before relying on results.')
                store.feed_failure('manual_grading', 'Manual statistics saved; entry grading did not complete.')
            store.feed_success('manual_stats', f"User-supplied, not live verified: {len(rows)} player rows "
                               f"across {len(groups)} games; import {import_id}.")
            store.set_checkpoint('last_manual_stats_import', summary)
            with store.connect() as db:
                db.execute('UPDATE manual_imports SET completed_at=?,status=?,summary_json=? WHERE id=?',
                           (summary['completed_at'], summary['status'], json.dumps(summary), import_id))
            return summary
        except Exception as error:
            summary.update(status='partial_failed', completed_at=_stamp())
            with store.connect() as db:
                db.execute('UPDATE manual_imports SET completed_at=?,status=?,summary_json=? WHERE id=?',
                           (summary['completed_at'], summary['status'], json.dumps(summary), import_id))
            store.set_checkpoint('last_manual_stats_import', summary)
            store.feed_failure('manual_stats', f'Import {import_id} interrupted after '
                               f"{len(summary['committed_game_ids'])} committed game bundles; check import ledger.")
            raise ValueError(f'Manual import {import_id} interrupted after '
                             f"{len(summary['committed_game_ids'])} committed game bundles. "
                             'Each committed game is atomic; check the import ledger before retrying.') from error


def record_manual_news(store, title, detail='', url='', team='NYK'):
    title = _text(title, 'title', 300)
    if not isinstance(detail, str) or len(detail) > 6000 or '\x00' in detail:
        raise ValueError('detail must be text of at most 6000 characters without NUL bytes.')
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('url must contain at most 2048 characters.')
    url = url.strip()
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('url must be a public-reference HTTP/HTTPS URL without embedded credentials.')
        except ValueError:
            raise ValueError('url must be an HTTP/HTTPS URL without embedded credentials.') from None
    team = _team(team, 'team')
    identity = json.dumps([title, detail.strip(), url, team], ensure_ascii=False)
    item = {'id': 'manual:' + hashlib.sha256(identity.encode('utf-8')).hexdigest(),
            'title': title, 'body': detail.strip(), 'url': url or None, 'team': team,
            'source': 'Manual user upload', 'published_at': None, 'observed_at': _stamp(),
            'verification_status': 'manual_unverified', 'manual_unverified': True,
            'coverage': 'A user-supplied note; not an official injury/transaction report.'}
    changed = store.record_news(item)
    store.feed_success('manual_news', 'User-supplied unverified news note; not an official or live injury feed.')
    return {**item, 'changed': changed}
