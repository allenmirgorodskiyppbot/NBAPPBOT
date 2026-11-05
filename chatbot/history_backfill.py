"""Validated career history and repeatable, insert-only history bundle imports.

The independently published source is not an independent league game inventory.
Source publication time never implies that every NBA game is covered. Retained
master observations win conflicts; historical imports cannot rewrite live data.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import datetime as dt
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import tempfile

from feeds import TEAM_WORDS, canonical_game, canonical_player, canonical_team, numeric
from game_phase import regular_phase_exclusion
from master import GAME_FIELDS, PLAYER_FIELDS
from nba_daily_export import validate_final

MAX_BUNDLE_RECORDS = 2_000_000
MAX_BUNDLE_COMPRESSED_BYTES = 128_000_000
MAX_BUNDLE_EXPANDED_BYTES = 2_000_000_000
MAX_RECORD_CHARS = 100_000

PHASES = {2: 'Regular Season', 3: 'Playoffs', 5: 'Play-In'}
# Preserve the historical franchise label in observations. These mappings are
# only used to establish that the participant was an NBA franchise, not All-Star.
HISTORICAL_FRANCHISES = {'NJ': 'BKN', 'NJN': 'BKN', 'SEA': 'OKC',
    'NOH': 'NOP', 'NOK': 'NOP', 'CHH': 'CHA', 'VAN': 'MEM'}
STAT_FIELDS = {'points': 'pts', 'rebounds': 'reb', 'assists': 'ast',
    'field_goals_made': 'fgm', 'field_goals_attempted': 'fga',
    'three_point_field_goals_made': 'tpm', 'three_point_field_goals_attempted': 'tpa',
    'free_throws_made': 'ftm', 'free_throws_attempted': 'fta',
    'offensive_rebounds': 'oreb', 'defensive_rebounds': 'dreb',
    'steals': 'stl', 'blocks': 'blk', 'turnovers': 'tov', 'fouls': 'fouls',
    'plus_minus': 'plus_minus'}


def nba_franchise(team):
    team = canonical_team(team)
    return HISTORICAL_FRANCHISES.get(team, team) in TEAM_WORDS


def history_exclusion(raw):
    """Separate regular, playoff and play-in rows; reject exhibitions and Cup final."""
    team = canonical_team(raw.get('team_abbreviation') or raw.get('team') or raw.get('home_team'))
    other = canonical_team(raw.get('opponent_team_abbreviation') or raw.get('opponent') or raw.get('away_team'))
    if not nba_franchise(team) or not nba_franchise(other) or team == other:
        return {'reason': 'Non-franchise exhibition/All-Star or unresolved team identity'}
    phase = raw.get('season_type')
    if str(phase) not in {str(x) for x in PHASES} | set(PHASES.values()):
        return {'reason': 'Unrecognized statistical phase'}
    if str(phase) in {'2', 'Regular Season'}:
        check = {**raw, 'team': HISTORICAL_FRANCHISES.get(team, team),
                 'opponent': HISTORICAL_FRANCHISES.get(other, other)}
        return regular_phase_exclusion(check)
    return None


def current_identities(rows, existing_scope=None):
    """Build a roster from NBA publication IDs, rejecting ambiguous current names.

    DNP roster observations count for identity, not for played-game statistics.
    Namesake players outside this current roster cannot enter the selected history.
    """
    existing = {canonical_player(x['player']): x for x in (existing_scope or {}).get('players', [])}
    by_id = {}; by_name = defaultdict(set)
    for row in rows:
        if history_exclusion(row) or str(row.get('season_type')) != '2':
            continue
        pid = str(row.get('athlete_id') or '')
        name = str(row.get('athlete_display_name') or '').strip()
        if not pid.isdigit() or not name:
            raise ValueError('Unresolved source player identity')
        key = canonical_player(name)
        by_name[key].add(pid)
        item = by_id.setdefault(pid, {'player': name, 'player_id': key,
                                      'espn_player_id': pid, 'aliases': set()})
        if item['player_id'] != key:
            raise ValueError('Current provider identity has conflicting canonical names')
        item['aliases'].add(name)
    collisions = {key: sorted(ids) for key, ids in by_name.items() if len(ids) > 1}
    if collisions:
        raise ValueError('Ambiguous current player names: ' + json.dumps(collisions, sort_keys=True))
    result = []
    for pid, item in by_id.items():
        known = existing.get(item['player_id'], {})
        name = known.get('player') or item['player']
        item['aliases'].add(name)
        result.append({**known, **item, 'player': name, 'aliases': sorted(item['aliases'])})
    return sorted(result, key=lambda x: x['player_id'])


def normalize_history(rows, identities, source, published_at, source_hash, through_date):
    """Validate complete published games before selecting the authorized roster.

    Returns complete-source validated games and selected observations plus every
    phase/validation exclusion. No outside-player statistics are imported.
    """
    selected = {str(x['espn_player_id']): x for x in identities}
    groups = {}; excluded = {}; failures = []; empty_nonparticipants = []
    through_date = dt.date.fromisoformat(through_date).isoformat()
    for raw in rows:
        event = str(raw.get('game_id') or '')
        day = str(raw.get('game_date') or '')[:10]
        exclusion = history_exclusion(raw)
        if exclusion:
            excluded[event] = {'provider_game_id': event, 'date': day, **exclusion}
            continue
        if dt.date.fromisoformat(day).isoformat() != day or day > through_date:
            excluded[event] = {'provider_game_id': event, 'date': day,
                               'reason': 'Beyond requested historical cutoff'}
            continue
        team = canonical_team(raw['team_abbreviation'])
        opponent = canonical_team(raw['opponent_team_abbreviation'])
        home_side = raw.get('home_away') == 'home'
        if raw.get('home_away') not in {'home', 'away'}:
            raise ValueError('Unresolved home/away source identity')
        home, away = (team, opponent) if home_side else (opponent, team)
        game_id = canonical_game(day, home, away)
        phase = PHASES[int(raw['season_type'])]
        start = raw.get('game_date_time')
        start = start.isoformat() if hasattr(start, 'isoformat') else start
        game = {'id': game_id, 'date': day, 'home_team': home, 'away_team': away,
            'home_score': numeric(raw.get('team_score') if home_side else raw.get('opponent_team_score')),
            'away_score': numeric(raw.get('opponent_team_score') if home_side else raw.get('team_score')),
            'provider_game_id': event, 'start_time': start, 'status': 'final', 'source': source,
            'season_type': phase, 'source_published_at': published_at, 'source_content_sha256': source_hash}
        group = groups.setdefault(game_id, {'game': game, 'rows': [], 'selected': []})
        if any(group['game'].get(k) != game.get(k) for k in
               ('provider_game_id', 'home_team', 'away_team', 'home_score', 'away_score', 'season_type')):
            group['conflict'] = 'Conflicting source game identity, phase or scoreboard'
        name = str(raw.get('athlete_display_name') or '').strip()
        provider_id = str(raw.get('athlete_id') or '')
        if not name or not provider_id.isdigit():
            # A publisher sometimes includes an unnamed inactive roster slot.
            # This is not a player observation. It contributes no statistics;
            # full named-player totals still must reconcile to the scoreboard.
            empty_stats = all(numeric(raw.get(field)) is None for field in [*STAT_FIELDS, 'minutes'])
            inactive = raw.get('active') is False or str(raw.get('did_not_play')).lower() == 'true'
            if not name and not provider_id and empty_stats and inactive:
                empty_nonparticipants.append({'provider_game_id': event, 'date': day,
                    'team': team, 'reason': 'Unnamed inactive roster placeholder with no statistics or minutes'})
                continue
            group['conflict'] = 'Unresolved player identity'
        dnp = raw.get('did_not_play')
        dnp = str(dnp).lower() == 'true' if not isinstance(dnp, bool) else dnp
        minutes = numeric(raw.get('minutes'))
        state = 'DNP' if dnp else 'Played' if minutes is not None and minutes > 0 else 'Unknown'
        year = int(raw['season'])
        row = {'game_id': game_id, 'player_id': 'espn:' + provider_id, 'player': name,
            'provider_player_id': provider_id, 'espn_player_id': provider_id,
            'team': team, 'opponent': opponent, 'date': day, 'season': f'{year-1}-{str(year)[-2:]}',
            'season_type': phase, 'appearance_state': state, 'minutes': minutes if state != 'DNP' else None,
            'source': source, 'source_published_at': published_at, 'source_content_sha256': source_hash,
            'upstream_full_game_validated': True,
            'minute_precision': 'Publisher numeric minutes; zero minutes without DNP flag remain unknown.',
            'starter': int(bool(raw['starter'])) if isinstance(raw.get('starter'), bool) else None}
        for column, field in STAT_FIELDS.items():
            row[field] = numeric(raw.get(column)) if state != 'DNP' else None
        group['rows'].append(row)
        identity = selected.get(provider_id)
        if identity:
            if canonical_player(name) not in {canonical_player(x) for x in
                    [identity['player'], *identity.get('aliases', [])]}:
                group['conflict'] = 'Selected provider ID has an unapproved name alias'
            group['selected'].append({**row, 'player': identity['player'],
                                       'player_id': identity['player_id']})
    games = []; observations = []
    for gid, group in sorted(groups.items()):
        if not group['selected']:
            continue
        try:
            if group.get('conflict'):
                raise ValueError(group['conflict'])
            validate_final(group['game'], group['rows'])
            if len({r['player_id'] for r in group['selected']}) != len(group['selected']):
                raise ValueError('Duplicate selected canonical identity')
            for row in group['rows']:
                for field in [*STAT_FIELDS.values(), 'minutes']:
                    value = row.get(field)
                    if value is not None and (not math.isfinite(float(value)) or (field != 'plus_minus' and value < 0)):
                        raise ValueError('Nonfinite or negative source statistic')
            games.append(group['game']); observations.extend(group['selected'])
        except ValueError as error:
            failures.append({'game_id': gid, 'provider_game_id': group['game']['provider_game_id'],
                'date': group['game']['date'], 'reason': str(error),
                'selected_players': sorted({r['player'] for r in group['selected']})})
    return games, observations, {'excluded_games': list(excluded.values()), 'rejected_games': failures,
        'omitted_empty_nonparticipants': empty_nonparticipants,
        'validated_games': len(games), 'selected_observations': len(observations)}


def insert_missing(store, games, observations, db, conflict_sink=None):
    """Insert missing keys only; even newer source disagreements retain originals."""
    counts = Counter()
    for table, kind, items, keys, fields, upsert in (
        ('games', 'game', games, ('id',), GAME_FIELDS, store.upsert_game),
        ('player_games', 'player_game', observations, ('game_id', 'player_id'), PLAYER_FIELDS, store.upsert_player_game)):
        where = ' AND '.join(k + '=?' for k in keys)
        for row in items:
            old = db.execute('SELECT * FROM ' + table + ' WHERE ' + where, tuple(row[k] for k in keys)).fetchone()
            if old:
                counts[kind + '_overlap'] += 1
                differences = {field: {'retained': old[field], 'incoming': row[field]}
                    for field in fields if field in row and field not in {'source'} and old[field] != row[field]}
                if differences:
                    counts[kind + '_conflicts'] += 1
                    for field in differences:
                        counts[kind + '_conflict_' + field] += 1
                    if conflict_sink:
                        conflict_sink({'kind': kind, 'key': {k: row[k] for k in keys}, 'differences': differences})
                continue
            # A real insert-only statement preserves a live key even if it
            # appeared after source planning. No known field is rewritten.
            names = fields + ['updated_at']
            values = [str(row[field]) if field in keys else row.get(field) for field in fields]
            cursor = db.execute('INSERT OR IGNORE INTO ' + table + ' (' + ','.join(names) + ') VALUES ('
                + ','.join('?' for _ in names) + ')', values + [str(row.get('updated_at') or dt.datetime.now(dt.timezone.utc).isoformat())])
            if cursor.rowcount:
                store._snapshot(db, table, ':'.join(str(row[field]) for field in keys), row)
                counts[kind + '_inserted'] += 1
    return dict(counts)


def _validate_bundle_record(kind, row, identities, through):
    if not isinstance(row, dict) or kind not in {'game', 'player_game'}:
        raise ValueError('Unsupported history record')
    day = str(row.get('date') or '')
    if dt.date.fromisoformat(day).isoformat() != day or day > through:
        raise ValueError('History record date outside manifest coverage')
    if history_exclusion(row):
        raise ValueError('History record has unsupported phase or participants')
    if kind == 'game':
        if row.get('id') != canonical_game(day, row.get('home_team'), row.get('away_team')) or row.get('status') != 'final':
            raise ValueError('History game identity/status invalid')
        for field in ('home_score', 'away_score'):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('History game score invalid')
    else:
        expected = identities.get(str(row.get('espn_player_id') or ''))
        if not expected or row.get('player_id') != expected['player_id'] or row.get('player') != expected['player']:
            raise ValueError('History player identity not authorized by manifest')
        if row.get('game_id') != canonical_game(day, row.get('team'), row.get('opponent')):
            raise ValueError('History player game identity invalid')
        if row.get('appearance_state') not in {'Played', 'DNP', 'Unknown'}:
            raise ValueError('History appearance state invalid')
        if row.get('season_type') not in PHASES.values():
            raise ValueError('History phase invalid')
        season = str(row.get('season') or '')
        try:
            start_year = int(season[:4])
            if season != f'{start_year}-{str(start_year + 1)[-2:]}':
                raise ValueError()
            # The delayed 2019-20 season finished in October 2020. Retain that
            # legitimate interval rather than inferring phase from calendar year.
            if not f'{start_year}-07-01' <= day <= f'{start_year + 1}-11-01':
                raise ValueError()
        except ValueError:
            raise ValueError('History season/date mismatch') from None
        for field in [*STAT_FIELDS.values(), 'minutes']:
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or (field != 'plus_minus' and value < 0)):
                raise ValueError('History statistic must be finite and nonnegative')
        for made, attempted in [('fgm', 'fga'), ('tpm', 'tpa'), ('ftm', 'fta'), ('tpm', 'fgm'), ('tpa', 'fga')]:
            if row.get(made) is not None and row.get(attempted) is not None and row[made] > row[attempted]:
                raise ValueError('History made/attempted statistics conflict')
        if row['appearance_state'] == 'Played' and (row.get('minutes') is None or row['minutes'] <= 0 or row.get('pts') is None):
            raise ValueError('Played history observation needs minutes and points')
        if row['appearance_state'] == 'DNP' and any(row.get(k) is not None for k in [*STAT_FIELDS.values(), 'minutes']):
            raise ValueError('DNP history observation cannot carry played statistics')


def _validate_existing_game(db, row, kind):
    gid = row['id'] if kind == 'game' else row['game_id']
    existing = db.execute('SELECT date,home_team,away_team FROM games WHERE id=?', (gid,)).fetchone()
    if not existing:
        return
    teams = {row.get('home_team'), row.get('away_team')} if kind == 'game' else {row.get('team'), row.get('opponent')}
    if existing['date'] and existing['date'] != row['date']:
        raise ValueError('Existing game date conflicts with history identity')
    if existing['home_team'] and existing['away_team'] and {existing['home_team'], existing['away_team']} != teams:
        raise ValueError('Existing game participants conflict with history identity')


def import_history_bundle(store, manifest_path):
    """Validate under a read snapshot, then atomically insert only missing keys.

    Source rows spool to a bounded private temporary SQLite file. A republished
    full career history does not hold the master writer lock while decompressing
    or checking its existing observations. No known values or freshness gates
    are changed, and a bad final record cannot commit any part of the bundle.
    """
    path = Path(manifest_path).resolve()
    if path.stat().st_size > 4_000_000:
        raise ValueError('History manifest exceeds its byte bound')
    manifest_blob = path.read_bytes()
    manifest = json.loads(manifest_blob)
    if manifest.get('schema_version') != 1 or manifest.get('format') != 'nba-history-jsonl-gzip-v1':
        raise ValueError('Unsupported history bundle manifest')
    through = dt.date.fromisoformat(manifest['source_data_through_date']).isoformat()
    identity_rows = manifest['players']
    identities = {str(x['espn_player_id']): x for x in identity_rows}
    if len(identities) != len(identity_rows) or len({x['player_id'] for x in identity_rows}) != len(identity_rows):
        raise ValueError('Duplicate manifest player identity')
    bundle_hash = hashlib.sha256(manifest_blob).hexdigest()
    key = 'career_history_bundle:' + bundle_hash
    prior = store.get_checkpoint(key, {})
    if prior.get('complete'):
        return {**prior, 'already_imported': True}
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise ValueError('History bundle part count invalid')
    verified = []; compressed = 0; expected_records = 0
    for part in parts:
        target = (path.parent / part['file']).resolve()
        size, records = part.get('bytes'), part.get('records')
        if (target.parent != path.parent or target == path or type(size) is not int
                or not 0 < size <= 32_000_000 or type(records) is not int or records <= 0
                or target.stat().st_size != size):
            raise ValueError('History part path or size invalid')
        compressed += size; expected_records += records
        if compressed > MAX_BUNDLE_COMPRESSED_BYTES or expected_records > MAX_BUNDLE_RECORDS:
            raise ValueError('History bundle exceeds its byte or record bound')
        verified.append((part, target))
    with tempfile.TemporaryDirectory(prefix='nba-history-plan-') as temporary:
        plan_path = Path(temporary) / 'plan.sqlite'
        with closing(sqlite3.connect(str(plan_path))) as plan:
            plan.execute('PRAGMA journal_mode=OFF')
            plan.execute('PRAGMA synchronous=OFF')
            plan.execute('CREATE TABLE records (kind TEXT,gid TEXT,pid TEXT,payload TEXT,initially_missing INTEGER,PRIMARY KEY(kind,gid,pid))')
            plan.execute('CREATE INDEX initial_missing_kind ON records(kind) WHERE initially_missing=1')
            plan.execute('CREATE TABLE game_identity (gid TEXT PRIMARY KEY,day TEXT,team_a TEXT,team_b TEXT)')
            totals = Counter(); expanded = 0
            with store.connect() as reader:
                reader.execute('BEGIN')
                for part, target in verified:
                    # Parse the exact verified bytes; never reopen a mutable
                    # source file after checking its content digest.
                    blob = target.read_bytes()
                    if len(blob) != part['bytes'] or hashlib.sha256(blob).hexdigest() != part['sha256']:
                        raise ValueError('History part checksum/size invalid')
                    count = 0
                    with gzip.open(io.BytesIO(blob), 'rt', encoding='utf-8') as stream:
                        while line := stream.readline(MAX_RECORD_CHARS + 1):
                            if len(line) > MAX_RECORD_CHARS:
                                raise ValueError('Oversized history record')
                            expanded += len(line.encode('utf-8')); count += 1
                            if expanded > MAX_BUNDLE_EXPANDED_BYTES:
                                raise ValueError('History bundle exceeds its expanded byte bound')
                            if count > part['records']:
                                raise ValueError('History part record count exceeds manifest')
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError('Unsupported history record')
                            kind = event.get('kind'); row = event.get('data')
                            _validate_bundle_record(kind, row, identities, through)
                            _validate_existing_game(reader, row, kind)
                            gid = row['id'] if kind == 'game' else row['game_id']
                            pid = '' if kind == 'game' else str(row['player_id'])
                            teams = sorted((row['home_team'], row['away_team']) if kind == 'game' else (row['team'], row['opponent']))
                            plan.execute('INSERT OR IGNORE INTO game_identity VALUES (?,?,?,?)', (gid,row['date'],*teams))
                            table, fields = ('games', GAME_FIELDS) if kind == 'game' else ('player_games', PLAYER_FIELDS)
                            old = reader.execute('SELECT * FROM '+table+' WHERE '+
                                ('id=?' if kind == 'game' else 'game_id=? AND player_id=?'),
                                (gid,) if kind == 'game' else (gid,pid)).fetchone()
                            if old:
                                totals[kind + '_overlap'] += 1
                                differences = [field for field in fields if field in row and field != 'source' and old[field] != row[field]]
                                if differences:
                                    totals[kind + '_conflicts'] += 1
                                    totals.update(kind + '_conflict_' + field for field in differences)
                            try:
                                plan.execute('INSERT INTO records VALUES (?,?,?,?,?)',
                                    (kind,gid,pid,json.dumps(row,sort_keys=True,ensure_ascii=False),int(old is None)))
                            except sqlite3.IntegrityError:
                                raise ValueError('Duplicate history bundle observation key') from None
                    if count != part['records']:
                        raise ValueError('History part record count mismatch')
                for (gid,) in plan.execute("SELECT gid FROM game_identity"):
                    if not reader.execute('SELECT 1 FROM games WHERE id=?', (gid,)).fetchone() and not plan.execute(
                            "SELECT 1 FROM records WHERE kind='game' AND gid=?", (gid,)).fetchone():
                        raise ValueError('History bundle has orphan player observations')
                for (gid,) in reader.execute('SELECT DISTINCT p.game_id FROM player_games p LEFT JOIN games g ON g.id=p.game_id WHERE g.id IS NULL'):
                    if not plan.execute("SELECT 1 FROM records WHERE kind='game' AND gid=?", (gid,)).fetchone():
                        raise ValueError('History bundle has orphan player observations')
            plan.commit()
            with store.connect() as db:
                db.execute('ATTACH DATABASE ? AS history_plan', (str(plan_path),))
                db.execute('BEGIN IMMEDIATE')
                completed = db.execute('SELECT value_json FROM checkpoints WHERE name=?', (key,)).fetchone()
                if completed and (saved := json.loads(completed[0])).get('complete'):
                    return {**saved, 'already_imported': True}
                # One check per game also covers overlapping rows: concurrent
                # identity corrections cannot be certified by an old read plan.
                for row in db.execute('''SELECT i.gid,i.day,i.team_a,i.team_b,g.date,g.home_team,g.away_team
                        FROM history_plan.game_identity i JOIN main.games g ON g.id=i.gid'''):
                    if row['date'] and row['date'] != row['day']:
                        raise ValueError('Existing game date conflicts with history identity')
                    if row['home_team'] and row['away_team'] and {row['home_team'],row['away_team']} != {row['team_a'],row['team_b']}:
                        raise ValueError('Existing game participants conflict with history identity')
                for kind in ('game','player_game'):
                    table = 'games' if kind == 'game' else 'player_games'
                    match = 'known.id=r.gid' if kind == 'game' else 'known.game_id=r.gid AND known.player_id=r.pid'
                    missing = 'known.id IS NULL' if kind == 'game' else 'known.game_id IS NULL'
                    # Count a newly arrived live observation as an overlap,
                    # preserving its values and its conflict diagnostics.
                    raced = 'SELECT r.payload FROM history_plan.records r JOIN main.'+table+' known ON '+match+' WHERE r.kind=? AND r.initially_missing=1'
                    for (payload,) in db.execute(raced,(kind,)):
                        row = json.loads(payload)
                        totals.update(insert_missing(store,[row] if kind=='game' else [],
                            [row] if kind=='player_game' else [],db))
                    # Indexed SQL joins identify missing keys at the actual
                    # writer lease, including deletions/new live rows that raced
                    # with planning. Existing payloads are never reparsed here.
                    query = 'SELECT r.payload FROM history_plan.records r LEFT JOIN main.'+table+' known ON '+match+' WHERE r.kind=? AND '+missing
                    for (payload,) in db.execute(query,(kind,)):
                        row = json.loads(payload)
                        _validate_existing_game(db,row,kind)
                        totals.update(insert_missing(store,[row] if kind=='game' else [],
                            [row] if kind=='player_game' else [],db))
                if db.execute('SELECT 1 FROM player_games p LEFT JOIN games g ON g.id=p.game_id WHERE g.id IS NULL LIMIT 1').fetchone():
                    raise ValueError('History bundle has orphan player observations')
                result = {**dict(totals), 'complete':True, 'source_data_through_date':through,
                    'manifest_sha256':bundle_hash,'completed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'policy':'Missing keys only; existing observations retained on every conflict.',
                    'overlap_check_basis':'Read snapshot; actual game identities and missing keys rechecked atomically before insertion.'}
                db.execute('INSERT OR REPLACE INTO checkpoints(name,value_json) VALUES (?,?)',(key,json.dumps(result,sort_keys=True)))
                db.execute('INSERT OR REPLACE INTO checkpoints(name,value_json) VALUES (?,?)',('career_history_status',json.dumps(result,sort_keys=True)))
            return result


def main():
    """Manual repeatable import, also useful for hosts without the chat worker."""
    import argparse
    from master import MasterStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Existing master SQLite database to extend without overwriting rows')
    parser.add_argument('--manifest', required=True, help='Local history_manifest.json beside verified gzip parts')
    args = parser.parse_args()
    print(json.dumps(import_history_bundle(MasterStore(args.db), args.manifest), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
