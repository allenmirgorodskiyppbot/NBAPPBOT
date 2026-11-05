"""Validated career history and repeatable, insert-only history bundle imports.

The independently published source is not an independent league game inventory.
Source publication time never implies that every NBA game is covered. Retained
master observations win conflicts; historical imports cannot rewrite live data.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path

from feeds import TEAM_WORDS, canonical_game, canonical_player, canonical_team, numeric
from game_phase import regular_phase_exclusion
from master import GAME_FIELDS, PLAYER_FIELDS
from nba_daily_export import validate_final

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
            upsert(row, db=db)
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
    """Apply local, verified published bundle parts once; safe to retry on startup.

    The caller may download the manifest and parts from its pinned SOURCE_REF.
    Relative paths are confined to the manifest directory. There are no network
    calls, credentials, changes to existing statistics, or entry-ledger writes.
    """
    path = Path(manifest_path).resolve()
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
    verified = []
    for part in manifest['parts']:
        target = (path.parent / part['file']).resolve()
        if target.parent != path.parent or target == path or target.stat().st_size != part['bytes']:
            raise ValueError('History part path or size invalid')
        blob = target.read_bytes()
        if len(blob) > 32_000_000 or hashlib.sha256(blob).hexdigest() != part['sha256']:
            raise ValueError('History part checksum/size invalid')
        verified.append((part, target))
    totals = Counter(); seen = set()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for part, target in verified:
            games = []; rows = []; count = 0
            with gzip.open(target, 'rt', encoding='utf-8') as stream:
                for line in stream:
                    if len(line) > 100_000:
                        raise ValueError('Oversized history record')
                    event = json.loads(line); kind = event.get('kind'); row = event.get('data')
                    _validate_bundle_record(kind, row, identities, through)
                    _validate_existing_game(db, row, kind)
                    identity = (kind, row['id']) if kind == 'game' else (kind, row['game_id'], row['player_id'])
                    if identity in seen:
                        raise ValueError('Duplicate history bundle observation key')
                    seen.add(identity)
                    (games if kind == 'game' else rows).append(row)
                    count += 1
                    if count > part['records']:
                        raise ValueError('History part record count exceeds manifest')
            if count != part['records']:
                raise ValueError('History part record count mismatch')
            totals.update(insert_missing(store, games, rows, db))
        # Row and game records may span parts. Reject dangling newly imported keys.
        if db.execute('SELECT 1 FROM player_games p LEFT JOIN games g ON g.id=p.game_id WHERE g.id IS NULL LIMIT 1').fetchone():
            raise ValueError('History bundle has orphan player observations')
        result = {**dict(totals), 'complete': True, 'source_data_through_date': through,
            'manifest_sha256': bundle_hash, 'completed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'policy': 'Missing keys only; existing observations retained on every conflict.'}
        db.execute('INSERT OR REPLACE INTO checkpoints(name,value_json) VALUES (?,?)', (key, json.dumps(result, sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO checkpoints(name,value_json) VALUES (?,?)',
                   ('career_history_status', json.dumps(result, sort_keys=True)))
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
