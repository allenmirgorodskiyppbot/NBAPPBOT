"""Bounded, verified import and reading of actual NBA guarding observations."""
from __future__ import annotations
import datetime as dt
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re

from master import normalized_name

BASE = 'https://raw.githubusercontent.com/allenmirgorodskiyppbot/NBAPPBOT/main/data/live/historical_matchups/'
MANIFEST = 'historical_matchups_manifest.json'
PART = re.compile(r'tracking_pair_history_[0-9a-f]{12}_\d{3}\.jsonl\.gz')
PHASES = {'Regular Season': '002', 'Playoffs': '004', 'Play-In': '005'}
COUNTS = ('player_points', 'team_points', 'fgm', 'fga', 'tpm', 'tpa', 'ftm', 'fta',
          'matchup_assists', 'matchup_turnovers', 'shooting_fouls', 'potential_assists',
          'blocks', 'help_blocks', 'help_fgm', 'help_fga', 'switches_on')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _time(value):
    stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Tracking timestamps require a timezone.')
    return stamp


def _number(value, integer=False):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0
            and (not integer or float(value).is_integer()))


def observation_key(row):
    game = str(row.get('game_id') or '')
    if not re.fullmatch(r'\d{10}', game):
        match = re.search(r'NBA_(\d{10})(?:_|$)', str(row.get('source_id') or ''))
        game = match.group(1) if match else ''
    ids = [str(row.get(side + '_nba_player_id') or '').removeprefix('nba:')
           for side in ('offense', 'defense')]
    if game and all(value.isdigit() for value in ids):
        return '_'.join([game, *ids])
    return None


def _validate_row(row, scope, pairs, now):
    if not isinstance(row, dict):
        raise ValueError('Measured observation must be an object.')
    names = {p['player']: p for p in scope['players']}
    for side in ('offense', 'defense'):
        name = row.get(side + '_player'); person = str(row.get(side + '_nba_player_id') or '')
        if name not in names or str(names[name].get('nba_player_id') or '') != person:
            raise ValueError('Measured player name and NBA identity disagree with scope.')
    if (row['offense_player'], row['defense_player']) not in pairs:
        raise ValueError('Measured named pair is outside the configured scope.')
    key = observation_key(row)
    if not key or row.get('pair_observation_id') != key:
        raise ValueError('Measured observation identity is inconsistent.')
    game, season, phase = row['game_id'], str(row.get('season')), row.get('season_type')
    if (not re.fullmatch(r'20\d\d-\d\d', season) or phase not in PHASES
            or game[:3] != PHASES[phase] or game[3:5] != season[2:4]
            or int(season[-2:]) != (int(season[:4]) + 1) % 100):
        raise ValueError('Measured game, statistical phase or season disagree.')
    day = dt.date.fromisoformat(str(row.get('date')))
    published, retrieved = _time(row.get('published_at')), _time(row.get('retrieved_at'))
    if day.year not in {int(season[:4]), int(season[:4]) + 1} or day > published.date() or published > now or retrieved > now or retrieved < published:
        raise ValueError('Measured source or covered-game date is inconsistent.')
    url = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
           f'nba_stats_game_matchups/game_matchups_{int(season[:4])+1}.parquet')
    if row.get('source') != url or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256'))):
        raise ValueError('Measured source identity is unsupported.')
    for key in COUNTS:
        value = row.get(key)
        if value is not None and not _number(value, integer=True):
            raise ValueError('Measured counting statistic is invalid.')
    for key in ('matchup_seconds', 'partial_possessions'):
        if not _number(row.get(key)):
            raise ValueError('Measured matchup exposure is invalid.')
    if not any(row.get(key, 0) for key in ('matchup_seconds', 'partial_possessions', *COUNTS)):
        raise ValueError('Zero exposure cannot establish a measured matchup.')
    for made, attempts in [('fgm', 'fga'), ('tpm', 'tpa'), ('ftm', 'fta'), ('help_fgm', 'help_fga')]:
        if row.get(made) is not None and row.get(attempts) is not None and row[made] > row[attempts]:
            raise ValueError('Measured makes exceed attempts.')
    mappings = row.get('date_mapping_sources')
    if not isinstance(mappings, list) or not mappings or len(mappings) > 4:
        raise ValueError('Measured observation lacks dated source proof.')
    for mapping in mappings:
        allowed_mapping = {'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
                           + f'{tag}/{prefix}_{int(season[:4])+1}.parquet'
                           for tag, prefix in [('nba_stats_schedules', 'nba_schedule'),
                                               ('nba_stats_player_game_logs', 'player_game_logs')]}
        stamp = _time(mapping.get('source_published_at'))
        if (mapping.get('source') not in allowed_mapping or stamp > now or day > stamp.date()
                or not re.fullmatch(r'[0-9a-f]{64}', str(mapping.get('source_content_sha256')))):
            raise ValueError('Measured dated mapping source is unsupported or inconsistent.')
    return row


def _download(client, url, limit):
    with client.stream('GET', url, follow_redirects=False) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError('Measured publication exceeds its byte bound.')
        return bytes(content)


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS measured_pair_history (observation_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,status TEXT NOT NULL,conflict_json TEXT)')


def _conflicting(left, right):
    keys = ('date', 'season', 'season_type',
            'matchup_seconds', 'partial_possessions', *COUNTS)
    return any(left.get(key) is not None and right.get(key) is not None and left[key] != right[key]
               for key in keys)


def refresh_measured_history(store, data_dir, client=None, *, scope=None, pair_scope=None, now=None):
    """Validate the whole relay before one atomic import; keep old evidence."""
    from dataset_refresh import load_scope
    scope = scope or load_scope()
    pair_scope = pair_scope or json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
    now = now or dt.datetime.now(dt.timezone.utc)
    allowed = {(p['offense_player'], p['defense_player']) for p in pair_scope['pairs']}
    owned = client is None
    if owned:
        import httpx
        client = httpx.Client(timeout=30, follow_redirects=False)
    root = Path(data_dir)
    if (root / 'historical_matchups' / MANIFEST).exists():
        root = root / 'historical_matchups'
    def get(name, limit):
        path = root / name
        if path.exists():
            if path.stat().st_size > limit:
                raise ValueError('Local measured publication exceeds its byte bound.')
            return path.read_bytes()
        return _download(client, BASE + name, limit)
    try:
        manifest = json.loads(get(MANIFEST, 4_000_000))
        expected = hashlib.sha256(_json(pair_scope).encode()).hexdigest()
        if (manifest.get('schema_version') != 1 or manifest.get('format') != 'nba-measured-pair-history-jsonl-gzip-v1'
                or manifest.get('scope_revision') != scope['revision'] or manifest.get('pair_scope_sha256') != expected):
            raise ValueError('Measured manifest format or configured scope differs.')
        parts = manifest.get('parts')
        if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
            raise ValueError('Measured manifest parts are invalid.')
        rows, total, aggregates, seen_parts, uncompressed = {}, 0, 0, set(), 0
        for part in parts:
            name = part.get('file', '')
            if not PART.fullmatch(name) or name in seen_parts:
                raise ValueError('Measured part path is unsupported or duplicated.')
            seen_parts.add(name)
            content = get(name, 8_000_000)
            if len(content) != part.get('bytes') or hashlib.sha256(content).hexdigest() != part.get('sha256'):
                raise ValueError('Measured part length or SHA256 differs.')
            count = 0
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                while True:
                    line = stream.readline(100_001)
                    if not line:
                        break
                    if len(line) > 100_000:
                        raise ValueError('Measured JSONL record exceeds its byte bound.')
                    count += 1; total += 1
                    uncompressed += len(line)
                    if uncompressed > 128_000_000:
                        raise ValueError('Measured publication exceeds its decompressed byte bound.')
                    if total > 100_000:
                        raise ValueError('Measured publication exceeds its record bound.')
                    item = json.loads(line)
                    if item.get('kind') == 'measured_pair':
                        row = _validate_row(item.get('data'), scope, allowed, now)
                        key = observation_key(row)
                        if key in rows:
                            raise ValueError('Measured publication repeats an observation identity.')
                        rows[key] = row
                    elif item.get('kind') == 'season_aggregate':
                        aggregates += 1
                    else:
                        raise ValueError('Measured publication record kind is unsupported.')
            if count != part.get('records'):
                raise ValueError('Measured part record count differs.')
        if not rows or len(rows) != manifest.get('measured_pair_games') or aggregates != manifest.get('season_aggregate_records'):
            raise ValueError('Measured manifest totals differ from its records.')
        added, conflicts = 0, 0
        with store.connect() as db:
            _schema(db)
            for key, row in rows.items():
                old = db.execute('SELECT payload_json,status FROM measured_pair_history WHERE observation_id=?', (key,)).fetchone()
                if not old:
                    db.execute('INSERT INTO measured_pair_history VALUES (?, ?, ?, NULL)', (key, _json(row), 'verified')); added += 1
                elif _conflicting(json.loads(old[0]), row):
                    db.execute("UPDATE measured_pair_history SET status='conflict',conflict_json=? WHERE observation_id=?", (_json(row), key)); conflicts += 1
        return {'phase': 'updated', 'observations': len(rows), 'added': added, 'conflicts': conflicts,
                'first_observed_game_date': manifest.get('first_observed_game_date'),
                'last_observed_game_date': manifest.get('last_observed_game_date'),
                'coverage_note': manifest.get('coverage_claim')}
    except Exception as error:
        return {'phase': 'unavailable', 'error_type': type(error).__name__,
                'detail': str(error)[:250] if isinstance(error, ValueError) else 'Measured history source could not be retrieved or validated.',
                'previous_history_retained': True}
    finally:
        if owned:
            client.close()


def measured_records(store, extra_rows=()):
    """Read retained legacy and imported actual observations without summing overlap."""
    rows = list(extra_rows)
    with store.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'workbook_evidence' in tables:
            rows.extend(json.loads(r[0]) for r in db.execute("SELECT payload_json FROM workbook_evidence WHERE kind='measured_pairs'"))
        if 'measured_pair_history' in tables:
            for payload, status in db.execute('SELECT payload_json,status FROM measured_pair_history'):
                row = json.loads(payload)
                if status == 'conflict':
                    row['_measured_conflict'] = True
                rows.append(row)
    resolved, legacy = {}, []
    by_names = {}
    for row in rows:
        key = observation_key(row)
        named = (str(row.get('date'))[:10], normalized_name(row.get('offense_player', '')), normalized_name(row.get('defense_player', '')))
        if key:
            by_names.setdefault(named, set()).add(key)
    for row in rows:
        named = (str(row.get('date'))[:10], normalized_name(row.get('offense_player', '')), normalized_name(row.get('defense_player', '')))
        keys = by_names.get(named, set())
        key = observation_key(row) or (next(iter(keys)) if len(keys) == 1 else None)
        if key is None:
            legacy.append(row); continue
        if key in resolved:
            old = resolved[key]
            if _conflicting(old, row) or row.get('_measured_conflict'):
                old['_measured_conflict'] = True
            else:
                # Retain richer current source proof, while preserving old dates.
                resolved[key] = {**old, **{k: v for k, v in row.items() if v is not None}}
        else:
            resolved[key] = dict(row)
    return legacy + list(resolved.values())
