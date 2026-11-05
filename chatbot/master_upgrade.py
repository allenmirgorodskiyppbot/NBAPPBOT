"""Add researched workbook data without replacing a running bot's database.

This is an additive import, not a restore. Existing observations, live checks,
ledger records, chat state and settings always win. Workbook publication dates
never become successful live source checks. The complete input is validated
before a backup and one SQLite transaction make the additions visible.
"""
from __future__ import annotations

from collections import defaultdict
import base64
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid
import zipfile
from zoneinfo import ZoneInfo

import openpyxl

from master import GAME_FIELDS, PLAYER_FIELDS, clear_unverified_tracking_coverage, encoded

MAX_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ROWS = 2_000_000
MAX_COLUMNS = 512
_RAW = {'games', 'player_games', 'news', 'feed_status', 'boards', 'entries',
        'grade_events', 'season_schedule', 'season_averages', 'refreshed_tracking'}
_GENERATED = {'player_metrics', 'calculated_splits', 'correlations', 'pair_summaries',
              'measured_pair_metrics', 'tracking_metrics', 'matchup_assignments',
              'team_schedule', 'original_evidence', 'original_missing_data',
              'original_sources', 'refresh_coverage', 'export_readme',
              'historical_coverage', 'current_missing_data', 'dataset_coverage',
              'upgrade_imports', 'workbook_text_chunks', 'history_import',
              'matchup_career_coverage', 'matchup_career_refresh', 'missing_stats_summary',
              'missing_stats_by_game', 'tracking_field_coverage', 'unknown_participation',
              'missing_stats_readme', 'structured_availability', 'player_stat_completions',
              'measured_pair_history', 'measured_pair_evidence', 'career_metadata_repairs',
              'game_stat_coverage', 'game_stat_gaps', 'game_score_completions'}
# Populated only from known application tables, never from a workbook-provided SQL name.
STRUCTURED_EXPORTS = {'research_runs': 'Research_Runs', 'research_candidates': 'Research_Candidates'}
TEXT_PREFIX = '__NBA_XLSX_TEXT__:'


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _day(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError(label + ' must be YYYY-MM-DD.')
    dt.date.fromisoformat(value)
    return value


def _json(value, label):
    def reject_constant(value):
        raise ValueError(label + ' contains a nonfinite number.')
    try:
        result = json.loads(value, parse_constant=reject_constant) if isinstance(value, str) else value
        if not isinstance(result, dict):
            raise ValueError(label + ' must be a JSON object.')
        encoded(result)
        return result
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(label + ' must contain valid JSON.') from error


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(label + ' must contain nonempty text.')
    return value


def _input(source):
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        path = Path(source)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('Workbook exceeds the 128 MiB import limit.')
        data = path.read_bytes()
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Workbook must contain 1 byte to 128 MiB.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_EXPANDED_BYTES:
                raise ValueError('Expanded workbook exceeds the 1 GiB limit.')
            if '[Content_Types].xml' not in archive.namelist():
                raise ValueError('File is not an Excel workbook.')
    except zipfile.BadZipFile as error:
        raise ValueError('File is not a readable XLSX workbook.') from error
    return data, hashlib.sha256(data).hexdigest()


def _cell(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Workbook contains a nonfinite cell.')
    return value


def _sheet_records(sheet):
    iterator = sheet.iter_rows(values_only=True)
    header = next(iterator, ())
    while header and header[-1] is None:
        header = header[:-1]
    if not header:
        return []
    if (len(header) > MAX_COLUMNS or not all(isinstance(c, str) and c for c in header)
            or len(set(header)) != len(header)):
        raise ValueError(sheet.title + ' needs unique text column names in row 1.')
    rows = []
    for values in iterator:
        if not any(value is not None for value in values):
            continue
        if len(values) > len(header) and any(v is not None for v in values[len(header):]):
            raise ValueError(sheet.title + ' has cells outside its declared columns.')
        row = {key: _cell(value) for key, value in zip(header, values)}
        if any(isinstance(value, str) and value.startswith('=') for value in row.values()):
            raise ValueError('Import data cannot contain Excel formulas: ' + sheet.title)
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError('Workbook table exceeds the row limit.')
    return rows


def _parse(data):
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    try:
        names = book.sheetnames
        if 'games' not in names or 'player_games' not in names:
            raise ValueError('Use an exported NBA master with games and player_games sheets.')
        structured_names = {'games', 'player_games', 'Original_Evidence', 'refreshed_tracking',
                            'news', 'season_schedule', 'Workbook_Text_Chunks', *STRUCTURED_EXPORTS.values()}
        parsed, consumed = {}, set()
        for name in names:
            base = name if name in structured_names else re.sub(r'_\d+$', '', name)
            if base in structured_names:
                parsed.setdefault(base, []).extend(_sheet_records(book[name]))
                consumed.add(name)
                if len(parsed[base]) > MAX_ROWS:
                    raise ValueError('Combined workbook table exceeds the row limit.')
        _restore_long_cells(parsed)
        archives = {}
        for sheet in book:
            folded = sheet.title.casefold()
            base = re.sub(r'_\d+$', '', folded)
            if folded in _RAW | _GENERATED or base in _RAW | _GENERATED or sheet.title in consumed:
                continue
            archives[sheet.title] = [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
            if len(archives[sheet.title]) > MAX_ROWS:
                raise ValueError('Archived workbook sheet exceeds the row limit.')
        return parsed, archives, names
    finally:
        book.close()


def _validate(parsed):
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    games = {}
    for row in parsed['games']:
        identity = _nonempty(row.get('id'), 'Game id')
        _day(row.get('date'), 'Game date')
        if identity in games:
            raise ValueError('Workbook contains duplicate game ids.')
        _nonempty(row.get('source'), 'Game source')
        if row.get('status') in {'final', 'live'} and row['date'] > today:
            raise ValueError('Future games cannot contain completed observations.')
        for key in ('home_score', 'away_score'):
            value = row.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(key + ' must be nonnegative or blank.')
        games[identity] = row
    observations, identities = [], set()
    stats = set(PLAYER_FIELDS) - {'game_id', 'player_id', 'player', 'team', 'opponent', 'date',
                                  'season', 'season_type', 'appearance_state', 'source'}
    for name, rows in parsed.items():
        if not re.fullmatch(r'player_games(?:_\d+)?', name):
            continue
        for row in rows:
            for key in ('game_id', 'player_id', 'player', 'team', 'opponent', 'source'):
                _nonempty(row.get(key), key)
            _day(row.get('date'), 'Player date')
            key = (row['game_id'], row['player_id'])
            if key in identities:
                raise ValueError('Workbook contains duplicate player-game ids.')
            identities.add(key)
            game = games.get(row['game_id'])
            if game is None or game['date'] != row['date']:
                raise ValueError('Every player observation must match an included game and its date.')
            if row['date'] > today or row['team'] == row['opponent']:
                raise ValueError('Invalid player observation date or opponent.')
            if game.get('home_team') and game.get('away_team'):
                if {row['team'], row['opponent']} != {game['home_team'], game['away_team']}:
                    raise ValueError('Player teams do not match the game.')
            for field in stats:
                value = row.get(field)
                if value is None:
                    continue
                if field == 'starter' and isinstance(value, bool):
                    row[field] = int(value)
                    continue
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or (field != 'plus_minus' and value < 0)):
                    raise ValueError(field + ' must be a finite number or blank.')
                if field != 'minutes' and not float(value).is_integer():
                    raise ValueError(field + ' must be a whole-number observation.')
                if field == 'starter' and value not in (0, 1):
                    raise ValueError('starter must be 0, 1 or blank.')
            observations.append(row)
    for row in parsed.get('Original_Evidence', []):
        _nonempty(row.get('kind'), 'Evidence kind')
        row['payload_json'] = encoded(_json(row.get('payload_json'), 'Evidence payload'))
    for row in parsed.get('refreshed_tracking', []):
        for key in ('season', 'entity', 'category'):
            _nonempty(row.get(key), 'Tracking ' + key)
        payload = clear_unverified_tracking_coverage(_json(row.get('payload_json'), 'Tracking payload'))
        for key in ('season', 'entity', 'category'):
            if payload.get(key) is not None and payload[key] != row[key]:
                raise ValueError('Tracking payload identity does not match its table key.')
        fields = payload.get('fields', {})
        if not isinstance(fields, dict):
            raise ValueError('Tracking fields must be a numeric object.')
        for value in fields.values():
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('Tracking fields must be finite and nonnegative or blank.')
        gp = payload.get('gp')
        if gp is not None and (type(gp) not in (int, float) or not math.isfinite(gp)
                               or gp < 0 or not float(gp).is_integer()):
            raise ValueError('Tracking games played must be a nonnegative integer.')
        for made, attempted in [('fgm', 'fga'), ('tpm', 'tpa'), ('ftm', 'fta'),
                                ('defended_fgm', 'defended_fga'), ('rim_defended_fgm', 'rim_defended_fga')]:
            if fields.get(made) is not None and fields.get(attempted) is not None and fields[made] > fields[attempted]:
                raise ValueError('Tracking makes cannot exceed attempts.')
        for field in ('fg_pct', 'fg_pct_allowed'):
            if fields.get(field) is not None and fields[field] > 1:
                raise ValueError('Tracking ratios must be between zero and one.')
        for field in ('data_through_date',):
            if payload.get(field):
                _day(payload[field], 'Tracking covered date')
                if payload[field] > today:
                    raise ValueError('Tracking covered date cannot be in the future.')
        row['payload_json'] = encoded(payload)
    for row in parsed.get('news', []):
        _nonempty(row.get('id'), 'News id')
        _nonempty(row.get('title'), 'News title')
        if row.get('metadata_json'):
            row['metadata_json'] = encoded(_json(row['metadata_json'], 'News metadata'))
    runs = {}
    for row in parsed.get('Research_Runs', []):
        from pregame_archive import RUN_COLUMNS
        if set(RUN_COLUMNS) - row.keys():
            raise ValueError('Research run has missing archive columns.')
        for key in ('id', 'owner_key', 'captured_at', 'kind', 'offer_kind', 'timing_status'):
            _nonempty(row.get(key), 'Research ' + key)
        captured = dt.datetime.fromisoformat(row['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            raise ValueError('Research capture time must include its original timezone.')
        try:
            versions = json.loads(row.get('model_versions_json'))
        except (TypeError, ValueError) as error:
            raise ValueError('Research model versions must contain a JSON list.') from error
        if not isinstance(versions, list) or not all(isinstance(value, str) for value in versions):
            raise ValueError('Research model versions must contain a list of strings.')
        _json(row['payload_json'], 'Research payload')
        if hashlib.sha256(row['payload_json'].encode()).hexdigest() != row.get('payload_sha256'):
            raise ValueError('Research payload hash mismatch; import refused.')
        if row['id'] in runs:
            raise ValueError('Duplicate research run id.')
        runs[row['id']] = row
    seen_candidates = set()
    for row in parsed.get('Research_Candidates', []):
        from pregame_archive import CANDIDATE_COLUMNS
        if set(CANDIDATE_COLUMNS) - row.keys():
            raise ValueError('Research candidate has missing archive columns.')
        if row.get('run_id') not in runs or type(row.get('ordinal')) is not int or row['ordinal'] < 0:
            raise ValueError('Research candidate must reference an included run and valid ordinal.')
        key = row['run_id'], row['ordinal']
        if key in seen_candidates:
            raise ValueError('Duplicate research candidate id.')
        seen_candidates.add(key)
        _json(row['payload_json'], 'Research candidate payload')
        for key in ('disposition', 'timing_status'):
            _nonempty(row.get(key), 'Research candidate ' + key)
        for key in ('line', 'reference_projection'):
            value = row.get(key)
            if value is not None and (type(value) not in (float, int) or not math.isfinite(value)):
                raise ValueError('Research numeric values must be finite or blank.')
    return list(games.values()), observations


def _encode_long_cells(tables):
    """Excel's per-cell text limit must never silently truncate JSON evidence."""
    chunks, known = [], set()
    for name, rows in list(tables.items()):
        if name == 'Workbook_Text_Chunks':
            continue
        for row in rows:
            for column, value in list(row.items()):
                text = encoded(value) if isinstance(value, (dict, list, tuple)) else value
                if not isinstance(text, str) or len(text) <= 30000:
                    continue
                digest = hashlib.sha256(text.encode()).hexdigest()
                row[column] = TEXT_PREFIX + digest
                if digest in known:
                    continue
                known.add(digest)
                raw = text.encode()
                pieces = [raw[index:index + 18000] for index in range(0, len(raw), 18000)]
                chunks.extend({'sha256': digest, 'part': index, 'parts': len(pieces),
                               'text_b64': 'B64:' + base64.b64encode(piece).decode('ascii')}
                              for index, piece in enumerate(pieces))
    if chunks:
        tables['Workbook_Text_Chunks'] = chunks


def _restore_long_cells(tables):
    groups, restored = defaultdict(dict), {}
    for row in tables.get('Workbook_Text_Chunks', []):
        digest, part, total, text = row.get('sha256'), row.get('part'), row.get('parts'), row.get('text_b64')
        if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or type(part) is not int or type(total) is not int or not 0 <= part < total
                or not isinstance(text, str) or not text.startswith('B64:') or part in groups[digest]):
            raise ValueError('Invalid or duplicate workbook text chunk.')
        try:
            groups[digest][part] = (total, base64.b64decode(text[4:], validate=True))
        except ValueError as error:
            raise ValueError('Invalid workbook text chunk encoding.') from error
    for digest, pieces in groups.items():
        totals = {value[0] for value in pieces.values()}
        if len(totals) != 1 or set(pieces) != set(range(next(iter(totals)))):
            raise ValueError('Workbook text chunks are incomplete.')
        try:
            text = b''.join(pieces[index][1] for index in sorted(pieces)).decode('utf-8')
        except UnicodeError as error:
            raise ValueError('Workbook text chunks contain invalid UTF-8.') from error
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            raise ValueError('Workbook text chunk hash mismatch.')
        restored[TEXT_PREFIX + digest] = text
    for name, rows in tables.items():
        if name == 'Workbook_Text_Chunks':
            continue
        for row in rows:
            for key, value in list(row.items()):
                if isinstance(value, str) and value.startswith(TEXT_PREFIX):
                    if value not in restored:
                        raise ValueError('Workbook text reference has no complete chunks.')
                    row[key] = restored[value]


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS master_upgrade_imports (
        sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, imported_at TEXT NOT NULL,
        report_json TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS master_upgrade_sheet_archives (
        sheet_name TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
        archived_at TEXT NOT NULL, row_count INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS master_upgrade_sheet_rows (
        sheet_name TEXT NOT NULL, row_number INTEGER NOT NULL, values_json TEXT NOT NULL,
        PRIMARY KEY(sheet_name,row_number))''')
    db.execute('''CREATE TABLE IF NOT EXISTS master_upgrade_archive_sources (
        sha256 TEXT PRIMARY KEY, archived_at TEXT NOT NULL, sheets_added INTEGER NOT NULL)''')


def _insert(db, table, fields, row):
    cursor = db.execute('INSERT OR IGNORE INTO ' + table + ' (' + ','.join(fields)
                        + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                        [row.get(key) for key in fields])
    return cursor.rowcount


def _archive(db, archives, digest, stamp):
    added = 0
    for name, rows in archives.items():
        if db.execute('SELECT 1 FROM master_upgrade_sheet_archives WHERE sheet_name=?', (name,)).fetchone():
            continue
        db.execute('INSERT INTO master_upgrade_sheet_archives VALUES (?,?,?,?)',
                   (name, digest, stamp, len(rows)))
        db.executemany('INSERT INTO master_upgrade_sheet_rows VALUES (?,?,?)',
                       [(name, number, encoded(values)) for number, values in enumerate(rows, 1)])
        added += 1
    return added


def merge_enriched_workbook(store, source, *, filename='NBA_Master.xlsx', backup_directory=None):
    """Validate, back up, and insert missing research rows in one transaction.

    Existing data always wins, even if the upload claims a newer update time.
    No imported worksheet can execute SQL, modify the ledger, or certify live
    schedule/publication readiness. Repeating the same file is idempotent.
    """
    data, digest = _input(source)
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='master_upgrade_imports'").fetchone()
        old = db.execute('SELECT report_json FROM master_upgrade_imports WHERE sha256=?', (digest,)).fetchone() if exists else None
    if old:
        return {**json.loads(old[0]), 'imported': False, 'skipped': True}
    parsed, archives, names = _parse(data)
    games, observations = _validate(parsed)
    stamp = _stamp()
    destination = Path(backup_directory or Path(store.path).parent / 'backups')
    backup = store.backup(destination / ('before-master-import-' + uuid.uuid4().hex + '.sqlite'))
    report = {'imported': True, 'skipped': False, 'sha256': digest,
              'filename': Path(filename).name, 'imported_at': stamp, 'backup': backup,
              'games_added': 0, 'player_games_added': 0, 'player_games_preserved': 0,
              'tracking_added': 0, 'evidence_added': 0, 'news_added': 0,
              'archives_added': 0, 'research_runs_added': 0, 'research_candidates_added': 0,
              'source_sheet_count': len(names),
              'coverage': 'Additive workbook snapshot. Existing rows and live checks preserved; import does not verify current-game freshness.'}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        if db.execute('SELECT 1 FROM master_upgrade_imports WHERE sha256=?', (digest,)).fetchone():
            return {**report, 'imported': False, 'skipped': True}
        for row in games:
            value = {**row, 'updated_at': row.get('updated_at') or stamp}
            added = _insert(db, 'games', GAME_FIELDS + ['updated_at'], value)
            report['games_added'] += added
            if added:
                store._snapshot(db, 'games', row['id'], {**row, 'workbook_import_sha256': digest})
        for row in observations:
            live_game = db.execute('SELECT date,home_team,away_team FROM games WHERE id=?', (row['game_id'],)).fetchone()
            if (live_game['date'] != row['date'] or (live_game['home_team'] and live_game['away_team']
                    and {row['team'], row['opponent']} != {live_game['home_team'], live_game['away_team']})):
                raise ValueError('Uploaded observations conflict with an existing live game identity; no rows imported.')
            value = {**row, 'updated_at': row.get('updated_at') or stamp}
            added = _insert(db, 'player_games', PLAYER_FIELDS + ['updated_at'], value)
            report['player_games_added'] += added
            report['player_games_preserved'] += not added
            if added:
                store._snapshot(db, 'player_games', row['game_id'] + ':' + row['player_id'],
                                {**row, 'workbook_import_sha256': digest,
                                 'import_verification': 'saved_source_snapshot_not_a_live_source_check'})
        for row in parsed.get('refreshed_tracking', []):
            report['tracking_added'] += _insert(db, 'refreshed_tracking',
                ['season', 'entity', 'category', 'payload_json'], row)
        for row in parsed.get('news', []):
            report['news_added'] += _insert(db, 'news',
                ['id','title','url','published_at','source','body','team','metadata_json','updated_at'],
                {**row, 'updated_at': row.get('updated_at') or stamp})
        if parsed.get('Research_Runs'):
            from pregame_archive import SCHEMA, RUN_COLUMNS, CANDIDATE_COLUMNS
            # executescript would commit the surrounding transaction. Execute each
            # complete application-owned SQL statement without an implicit commit.
            statement = ''
            for line in SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
            inserted_runs = set()
            for row in parsed['Research_Runs']:
                if _insert(db, 'research_runs', RUN_COLUMNS, row):
                    inserted_runs.add(row['id'])
            report['research_runs_added'] = len(inserted_runs)
            for row in parsed.get('Research_Candidates', []):
                if row['run_id'] in inserted_runs:
                    report['research_candidates_added'] += _insert(db, 'research_candidates', CANDIDATE_COLUMNS, row)
        if parsed.get('Original_Evidence'):
            db.execute('CREATE TABLE IF NOT EXISTS workbook_evidence (kind TEXT NOT NULL,entity TEXT NOT NULL,payload_json TEXT NOT NULL)')
            existing = {(r['kind'], r['entity'], encoded(_json(r['payload_json'], 'Existing evidence')))
                        for r in db.execute('SELECT * FROM workbook_evidence')}
            # IDs identify one dated definition. A different upload must not replace
            # an existing assignment/split definition under the same ID.
            keyed = set()
            for kind, entity, payload in existing:
                value = json.loads(payload)
                identity = value.get('assignment_id') if kind == 'assignments' else value.get('split_id') if kind == 'splits' else None
                if identity:
                    keyed.add((kind, str(identity)))
            for row in parsed['Original_Evidence']:
                key = (row['kind'], str(row.get('entity') or ''), row['payload_json'])
                payload = json.loads(row['payload_json'])
                identity = payload.get('assignment_id') if row['kind'] == 'assignments' else payload.get('split_id') if row['kind'] == 'splits' else None
                if key in existing or (identity and (row['kind'], str(identity)) in keyed):
                    continue
                db.execute('INSERT INTO workbook_evidence VALUES (?,?,?)', key)
                existing.add(key)
                if identity:
                    keyed.add((row['kind'], str(identity)))
                report['evidence_added'] += 1
        report['archives_added'] = _archive(db, archives, digest, stamp)
        # Inventory is additive metadata only; never copy operational checkpoints.
        saved = db.execute("SELECT value_json FROM checkpoints WHERE name='workbook_sheet_inventory'").fetchone()
        inventory = json.loads(saved[0]) if saved else []
        known = {row.get('sheet') for row in inventory}
        inventory.extend({'sheet': name, 'source': 'enriched_workbook_snapshot'} for name in names if name not in known)
        db.execute("INSERT INTO checkpoints VALUES ('workbook_sheet_inventory',?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (encoded(inventory),))
        db.execute('INSERT INTO master_upgrade_imports VALUES (?,?,?,?)',
                   (digest, Path(filename).name, stamp, encoded(report)))
    return report


def archive_workbook_sheets(store, source):
    """Retain unsupported original presentation sheets as dated raw cell rows."""
    data, digest = _input(source)
    with store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='master_upgrade_archive_sources'").fetchone()
        if exists and db.execute('SELECT 1 FROM master_upgrade_archive_sources WHERE sha256=?', (digest,)).fetchone():
            return 0
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        archives = {sheet.title: [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
                    for sheet in book if sheet.title.casefold() not in _RAW | _GENERATED}
    finally:
        book.close()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        stamp = _stamp()
        count = _archive(db, archives, digest, stamp)
        db.execute('INSERT OR IGNORE INTO master_upgrade_archive_sources VALUES (?,?,?)', (digest, stamp, count))
        return count


def archived_export_sheets(store, existing_names=()):
    """Return original cell rows only for tabs without a regenerated current view."""
    existing = {name.casefold() for name in existing_names}
    result = {}
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='master_upgrade_sheet_archives'").fetchone():
            return result
        for record in db.execute('SELECT sheet_name FROM master_upgrade_sheet_archives ORDER BY rowid'):
            name = record[0]
            if name.casefold() in existing:
                continue
            result[name] = [json.loads(row[0]) for row in db.execute(
                'SELECT values_json FROM master_upgrade_sheet_rows WHERE sheet_name=? ORDER BY row_number', (name,))]
    return result


def augment_export_tables(master, tables, now=None):
    """Add honest sample coverage and missing-data reports; never mark data fresh."""
    historical = defaultdict(list)
    games = {row['id']: row for row in tables.get('games', [])}
    for row in tables.get('player_games', []):
        if games.get(row['game_id'], {}).get('status') == 'final' and str(row.get('appearance_state') or '').casefold() == 'played':
            historical[(row.get('player'), row.get('season'), row.get('season_type'))].append(row)
    coverage = []
    for (player, season, phase), rows in sorted(historical.items(), key=lambda item: str(item[0])):
        dates = [r['date'] for r in rows if r.get('date')]
        coverage.append({'player': player, 'season': season, 'season_type': phase,
            'observed_games': len(rows), 'first_game': min(dates) if dates else None,
            'last_game': max(dates) if dates else None,
            'actual_teams': ', '.join(sorted({str(r['team']) for r in rows if r.get('team')})),
            **{'missing_' + field: sum(r.get(field) is None for r in rows)
               for field in ('pts', 'reb', 'ast', 'minutes', 'fga', 'tpa', 'tpm')},
            'history_completeness': 'observed_rows_only_not_independently_verified_complete',
            'note': 'Coverage is separate for each player, season and phase; lifetime completeness requires an independent expected-game inventory.'})
    tables['Historical_Coverage'] = coverage
    from career_identity import matchup_career_coverage
    tables['Matchup_Career_Coverage'] = matchup_career_coverage(
        master, tables.get('player_games', []), tables.get('games', []), now=now)
    observations = tables.get('player_games', [])
    dates = [row['date'] for row in observations if row.get('date')]
    dataset = [{'dataset': 'Saved player-game observations', 'records': len(observations),
                'players': len({row.get('player_id') for row in observations}),
                'games': len(games), 'oldest_covered_date': min(dates) if dates else None,
                'latest_covered_date': max(dates) if dates else None,
                'status': 'observed_rows_only',
                'note': 'Actual saved observations; these counts do not prove complete career or current-game coverage.'}]
    tracking = defaultdict(list)
    for row in tables.get('refreshed_tracking', []):
        record = _json(row['payload_json'], 'Saved tracking')
        tracking[row['category']].append(record)
    for category, rows in sorted(tracking.items()):
        dates = [r.get('data_through_date') for r in rows if r.get('data_through_date')]
        dataset.append({'dataset': category, 'records': len(rows),
            'unknown_covered_date': len(rows) - len(dates), 'oldest_covered_date': min(dates) if dates else None,
            'latest_covered_date': max(dates) if dates else None,
            'status': 'coverage_unknown' if len(dates) < len(rows) else 'dated_source_records',
            'note': 'Publication, retrieval, export time and actual covered-game dates are separate.'})
    missing = [
        {'area': 'Historical game completeness', 'status': 'not_independently_verified',
         'next_action': 'Compare each player-season with an independent expected-game inventory; use Historical_Coverage for actual counts.'},
        {'area': 'Tracking freshness', 'status': 'unknown_coverage' if any(r.get('unknown_covered_date') for r in dataset) else 'inspect_dated_coverage' if tracking else 'unavailable',
         'next_action': 'Acquire source-reported covered-game dates and missing categories; a new export does not refresh tracking.'},
        {'area': 'Tentative defenders', 'status': 'unverified_candidates',
         'verified_rows': sum(bool(r.get('verified_game_day_assignment')) for r in tables.get('Matchup_Assignments', [])),
         'next_action': 'Refresh source-backed candidate assignments for the game; coappearance is not measured guarding.'},
        {'area': 'Historical PrizePicks backtesting', 'status': 'coverage_not_established_by_export',
         'next_action': 'Archive dated actual offers, available sides, special-offer type and payout terms; simulated lines do not establish a betting edge.'},
        {'area': 'Official injury and lineup coverage', 'status': 'not_established_by_news_or_research_archive',
         'next_action': 'Collect official per-game availability and confirmed lineups with source/time. An archived report or missing headline does not establish that a player is available.'},
        {'area': 'Pregame minutes and role', 'status': 'only_as_supplied_in_saved_research',
         'next_action': 'Archive source-backed expected minutes, role and restrictions before tipoff; distinguish estimates from confirmed information.'},
    ]
    with master.connect() as db:
        available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        imported = db.execute("SELECT value_json FROM checkpoints WHERE name='career_history_status'").fetchone()
        if imported:
            status = _json(imported[0], 'Saved history status')
            tables['History_Import'] = [{'field': key, 'value': value,
                'scope': 'Historical import report; independent live readiness remains separate.'}
                for key, value in status.items()]
        rolling = db.execute("SELECT value_json FROM checkpoints WHERE name='matchup_career_history_status'").fetchone()
        if rolling:
            status = _json(rolling[0], 'Saved matchup career status')
            tables['Matchup_Career_Refresh'] = [{'field': key, 'value': value,
                'scope': 'Historical source coverage; not current-game readiness.'}
                for key, value in status.items()]
        for table, sheet in STRUCTURED_EXPORTS.items():
            if table in available:
                rows = [dict(r) for r in db.execute('SELECT * FROM ' + table)]
                tables[sheet] = rows
                dataset.append({'dataset': sheet, 'records': len(rows),
                                'status': 'archived_snapshots' if rows else 'empty_archive',
                                'note': 'Snapshots retain their original as-of/source timestamps; rows are not necessarily fresh or complete.'})
        if 'career_game_metadata_repairs' in available:
            # Evidence stays a dated reference on workbook import. Uploaded
            # audit rows cannot themselves certify that a game was verified.
            tables['Career_Metadata_Repairs'] = [dict(row) for row in db.execute(
                'SELECT * FROM career_game_metadata_repairs ORDER BY game_id')]
        if 'master_upgrade_imports' in available:
            tables['Upgrade_Imports'] = [dict(r) for r in db.execute('SELECT * FROM master_upgrade_imports')]
        archive_names = [r[0] for r in db.execute('SELECT sheet_name FROM master_upgrade_sheet_archives')] if 'master_upgrade_sheet_archives' in available else []
    for name in archive_names:
        dataset.append({'dataset': name, 'status': 'dated_original_sheet_preserved',
                        'note': 'Original cell values are preserved; this does not refresh their underlying sources.'})
    tables['Dataset_Coverage'] = dataset
    tables['Current_Missing_Data'] = missing
    tables.setdefault('Export_ReadMe', []).append({'topic': 'Long evidence text',
        'value': 'Cells beginning ' + TEXT_PREFIX + ' reference lossless UTF-8/base64 pieces in Workbook_Text_Chunks. The master importer validates and reconstructs them; do not delete these pieces.'})
    _encode_long_cells(tables)
    return tables


if __name__ == '__main__':
    import argparse
    from master import MasterStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--workbook', required=True)
    parser.add_argument('--backup-directory')
    args = parser.parse_args()
    print(json.dumps(merge_enriched_workbook(MasterStore(args.database), args.workbook,
        filename=Path(args.workbook).name, backup_directory=args.backup_directory), indent=2))
