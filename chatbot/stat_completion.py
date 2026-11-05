"""Fill missing player statistics only from compatible verified source rows.

This supplements the insert-only career importer. Existing non-null statistics
always win, and a disagreement holds the row instead of silently correcting it.
The operation records per-field provenance and cannot certify game-day readiness.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from career_metadata import _games, _instant, _source_known as _espn_source_known
from feeds import canonical_game, canonical_player, canonical_team
from history_backfill import _validate_bundle_record, history_exclusion
from master import encoded, normalized_name

STAT_FIELDS = ('pts', 'reb', 'ast', 'tpm', 'fga', 'tpa', 'minutes', 'stl', 'blk',
               'tov', 'fgm', 'ftm', 'fta', 'oreb', 'dreb', 'fouls', 'plus_minus', 'starter')
MAX_RECORD_BYTES = 100_000
MAX_RECORDS = 2_000_000
MAX_EXPANDED_PART = 64_000_000
MAX_EXPANDED_TOTAL = 2_000_000_000


def _nba_source_known(value):
    return bool(re.fullmatch(
        r'https://github\.com/sportsdataverse/sportsdataverse-data/releases/download/'
        r'nba_stats_player_boxscores/player_boxscores_\d{4}\.parquet', str(value or '')))


def _source_known(value):
    return _espn_source_known(value) or _nba_source_known(value)


def _verified_nba_game(row, through, current):
    if (row.get('upstream_full_game_validated') is not True or
            not _nba_source_known(row.get('source')) or
            not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256') or ''))):
        return False
    published = _instant(row.get('source_published_at'))
    if published is None:
        return False
    if published > current:
        raise ValueError('NBA box-score publication is in the future.')
    if row['date'] > through or row['date'] > current.astimezone(ZoneInfo('America/New_York')).date().isoformat():
        raise ValueError('NBA game lies outside verified coverage.')
    if not re.fullmatch(r'\d{1,15}', str(row.get('provider_game_id') or '')):
        raise ValueError('NBA source game needs an explicit provider identity.')
    for field in ('home_score', 'away_score'):
        value = row.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value or not 0 <= value <= 400:
            raise ValueError('NBA source final score is invalid.')
    if row['home_score'] == row['away_score']:
        raise ValueError('NBA source final score is tied.')
    return row.get('status') == 'final' and not history_exclusion(row)


def _clock_minutes(value):
    text = str(value or '')
    match = re.fullmatch(r'(\d{1,3}):([0-5]\d(?:\.\d+)?)', text)
    if match:
        return int(match[1]) + float(match[2]) / 60
    match = re.fullmatch(r'PT(?:(\d{1,3})M)?(?:(\d+(?:\.\d+)?)S)?', text)
    if match and any(match.groups()) and float(match[2] or 0) < 60:
        return int(match[1] or 0) + float(match[2] or 0) / 60
    return None


def _old_precision_snapshots(store, candidates):
    keys = {row['game_id'] + ':' + row['player_id'] for values in candidates.values() for row in values
            if row.get('source') and _espn_source_known(row['source'])
            and type(row.get('minutes')) in (int, float) and int(row['minutes']) == row['minutes']}
    if not keys:
        return {}
    result = defaultdict(list)
    with store.connect() as db:
        # A single pass over the existing immutable source snapshots avoids one
        # full raw-table scan for every old player observation.
        for start in range(0, len(keys), 800):
            batch = sorted(keys)[start:start + 800]
            for row in db.execute("SELECT entity_key,payload_json FROM raw_snapshots WHERE kind='player_games' "
                                  'AND entity_key IN (' + ','.join('?' for _ in batch) + ')', batch):
                value = json.loads(row['payload_json'])
                if isinstance(value, dict):
                    result[row['entity_key']].append(value)
    return result


def _minute_precision_evidence(old, incoming, snapshots):
    """Compare reported whole ESPN minutes with an independently published NBA clock.

    This never changes stored minutes and never excuses a counting-stat conflict.
    An immutable original source row is required; a guessed display precision or
    an arbitrary workbook decimal is insufficient.
    """
    old_minutes, new_minutes = old.get('minutes'), incoming.get('minutes')
    clock_raw = incoming.get('source_minutes_raw')
    clock = _clock_minutes(clock_raw)
    if (not _nba_source_known(incoming.get('source')) or clock is None
            or type(new_minutes) not in (int, float) or abs(clock - new_minutes) > 1e-9
            or type(old_minutes) not in (int, float) or int(old_minutes) != old_minutes
            or abs(clock - old_minutes) > .5 + 1e-9
            or not str(old.get('source') or '').startswith(
                'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/')):
        return None
    for original in snapshots.get(old['game_id'] + ':' + old['player_id'], []):
        if (original.get('upstream_full_game_validated') is not True
                or original.get('source') != old['source'] or original.get('minutes') != old_minutes
                or not re.fullmatch(r'[0-9a-f]{64}', str(original.get('source_content_sha256') or ''))
                or _instant(original.get('source_published_at')) is None):
            continue
        if any(original.get(field) != old.get(field) for field in
               ('game_id', 'player_id', 'date', 'season', 'season_type', 'team', 'opponent')):
            continue
        if any(original.get(field) is not None and old.get(field) is not None
               and original[field] != old[field] for field in STAT_FIELDS if field != 'minutes'):
            continue
        return {'stored_source_url': original['source'],
                'stored_source_content_sha256': original['source_content_sha256'],
                'stored_source_minutes': old_minutes, 'source_clock_raw': clock_raw,
                'source_clock_minutes': clock, 'relation': 'compatible_at_reported_whole_minute_precision',
                'stored_minutes_changed': False,
                'policy': 'Only an original verified ESPN whole-minute observation and matched NBA source clock may use this rule.'}
    return None


def _roster_records(path, identities, current):
    if path is None:
        return {}, None
    target = Path(path)
    blob = target.read_bytes()
    if not 0 < len(blob) <= 32_000_000:
        raise ValueError('Historical roster proof file exceeds its bound.')
    records, count, expanded = {}, 0, 0
    required = ('exactly_five_starters_on_both_team_rosters',
                'both_team_rosters_have_unique_named_player_ids',
                'explicit_starter_boolean', 'no_starter_DNP_contradiction')
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        while line := stream.readline(MAX_RECORD_BYTES + 1):
            count += 1; expanded += len(line)
            if len(line) > MAX_RECORD_BYTES or count > 200_000 or expanded > MAX_EXPANDED_PART:
                raise ValueError('Historical roster proof inventory exceeds its bounds.')
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('Historical starter proof must be an object.')
            provider = str(row.get('espn_player_id') or '')
            identity = identities.get(provider)
            published = _instant(row.get('source_published_at'))
            source = str(row.get('source') or '')
            if (not identity or not isinstance(row.get('proof'), dict)
                    or not all(row['proof'].get(key) is True for key in required)
                    or not re.fullmatch(r'https://github\.com/sportsdataverse/sportsdataverse-data/releases/download/'
                                        r'espn_nba_game_rosters/game_rosters_\d{4}\.parquet', source)
                    or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256') or ''))
                    or published is None or published > current
                    or type(row.get('did_not_play')) is not bool
                    or row.get('starter') not in (0, 1) or type(row.get('starter')) not in (int, bool)
                    or type(row.get('season_end_year')) is not int
                    or not re.fullmatch(r'\d{1,15}', str(row.get('provider_game_id') or ''))):
                raise ValueError('Historical starter proof is incomplete or invalid.')
            if normalized_name(row.get('player')) not in {normalized_name(name) for name in
                    [identity['player'], *identity.get('aliases', [])]}:
                raise ValueError('Historical starter proof has an unapproved player alias.')
            key = (str(row['provider_game_id']), provider, canonical_team(row.get('team')))
            if not key[2] or (key in records and records[key] != row):
                raise ValueError('Historical starter proofs disagree for one player/game.')
            records[key] = row
            if row.get('game_id') is not None:
                proof = row.get('date_mapping_proof')
                mapped_publication = _instant(row.get('date_mapping_source_published_at'))
                if (not isinstance(proof, dict)
                        or proof.get('consistent_game_date_and_home_away_headers') is not True
                        or proof.get('both_source_teams_observed') is not True
                        or str(proof.get('provider_game_id')) != str(row['provider_game_id'])
                        or proof.get('season_end_year') != row['season_end_year']
                        or not re.fullmatch(r'https://github\.com/sportsdataverse/sportsdataverse-data/releases/download/'
                            r'espn_nba_player_boxscores/player_box_\d{4}\.parquet', str(row.get('date_mapping_source') or ''))
                        or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('date_mapping_source_content_sha256') or ''))
                        or mapped_publication is None or mapped_publication > current):
                    raise ValueError('Historical starter date mapping lacks verified source headers.')
                day = str(row.get('date') or '')
                if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                        or dt.date.fromisoformat(day).isoformat() != day
                        or day > current.astimezone(ZoneInfo('America/New_York')).date().isoformat()
                        or row['game_id'] != canonical_game(day, row.get('home_team'), row.get('away_team'))
                        or canonical_team(row['team']) not in
                            {canonical_team(row.get('home_team')), canonical_team(row.get('away_team'))}
                        or canonical_team(row.get('home_team')) == canonical_team(row.get('away_team'))):
                    raise ValueError('Historical starter date mapping has a conflicting event identity.')
                canonical_key = ('canonical:' + row['game_id'], provider, key[2])
                if canonical_key in records and records[canonical_key] != row:
                    raise ValueError('Multiple historical starter headers identify one canonical player/game.')
                records[canonical_key] = row
    return records, hashlib.sha256(blob).hexdigest()


def _explicit_nba_starter(row):
    proof = row.get('starter_source_proof')
    return (_nba_source_known(row.get('source')) and isinstance(proof, dict)
            and proof.get('exactly_five_starters_on_both_team_rosters') is True
            and proof.get('explicit_start_position') is True
            and proof.get('source_field') in {'START_POSITION', 'startPosition'}
            and row.get('starter') in (0, 1)
            and isinstance(proof.get('starter_counts'), dict)
            and {canonical_team(team) for team in proof['starter_counts']} ==
                {canonical_team(row['team']), canonical_team(row['opponent'])}
            and all(value == 5 for value in proof['starter_counts'].values())
            and type(proof.get('full_roster_players')) is int and proof['full_roster_players'] >= 10
            and str(proof.get('position_value') or '') == str(row.get('source_start_position_raw') or '')
            and bool(str(proof.get('position_value') or '').strip()) == bool(row['starter']))


def _starter_evidence(incoming, game, rosters):
    key = (str(game.get('provider_game_id') or ''), str(incoming.get('espn_player_id') or ''),
           canonical_team(incoming.get('team')))
    roster = rosters.get(key)
    if roster is None:
        roster = rosters.get(('canonical:' + incoming['game_id'], key[1], key[2]))
    if roster is not None:
        if (roster['did_not_play'] or roster['season_end_year'] != int(incoming['season'][:4]) + 1
                or (roster.get('date') is not None and
                    (roster['date'] != incoming['date']
                     or canonical_team(roster.get('home_team')) != canonical_team(game['home_team'])
                     or canonical_team(roster.get('away_team')) != canonical_team(game['away_team'])))
                or (incoming.get('starter') is not None and incoming['starter'] != roster['starter'])):
            return None, True
        return roster, False
    if _explicit_nba_starter(incoming):
        return {**incoming, 'proof': incoming['starter_source_proof'],
                'source_population': 'Explicit historical NBA starting-position flag; not a pregame confirmation.'}, False
    return None, False


def _identity_index(manifest):
    players = manifest.get('players')
    if not isinstance(players, list) or not players:
        raise ValueError('Player-stat completion requires an explicit identity manifest.')
    by_provider, aliases, nba_bindings = {}, defaultdict(set), {}
    for row in players:
        if not isinstance(row, dict):
            raise ValueError('Malformed manifest player identity.')
        provider = str(row.get('espn_player_id') or '')
        player = row.get('player')
        if (not provider.isdigit() or not isinstance(player, str) or not player.strip()
                or row.get('player_id') != canonical_player(player) or provider in by_provider):
            raise ValueError('Invalid or duplicate manifest provider identity.')
        if not isinstance(row.get('aliases', []), list):
            raise ValueError('Manifest player aliases must be a list.')
        names = [player, *row.get('aliases', [])]
        if not all(isinstance(name, str) and name.strip() for name in names):
            raise ValueError('Invalid manifest player aliases.')
        by_provider[provider] = row
        nba = str(row.get('nba_player_id') or '')
        if nba:
            if not nba.isdigit() or (nba in nba_bindings and nba_bindings[nba] != provider):
                raise ValueError('Conflicting manifest NBA person identity.')
            nba_bindings[nba] = provider
        for name in names:
            aliases[normalized_name(name)].add(provider)
    return by_provider, aliases


def _candidate_rows(store, aliases, identities, through, current):
    cutoff = min(through, current.astimezone(ZoneInfo('America/New_York')).date().isoformat())
    where = ' OR '.join('p.' + field + ' IS NULL' for field in STAT_FIELDS)
    result, counts = defaultdict(list), Counter()
    with store.connect() as db:
        rows = db.execute('SELECT p.* FROM player_games p JOIN games g ON g.id=p.game_id '
            "WHERE g.status='final' AND lower(p.appearance_state)='played' AND g.date=p.date "
            'AND p.date<=? AND (' + where + ')', (cutoff,)).fetchall()
    for original in rows:
        row = dict(original)
        if history_exclusion(row):
            counts['candidate_rows_excluded_phase'] += 1
            continue
        provider_ids = aliases.get(normalized_name(row['player']), set())
        if len(provider_ids) != 1:
            counts['candidate_rows_unresolved_identity'] += 1
            continue
        provider = next(iter(provider_ids))
        identity = identities[provider]
        permitted = {canonical_player(name) for name in [identity['player'], *identity.get('aliases', [])]}
        permitted.update((identity['player_id'], 'espn:' + provider))
        if identity.get('nba_player_id'):
            permitted.update((str(identity['nba_player_id']), 'nba:' + str(identity['nba_player_id'])))
        if row['player_id'] not in permitted:
            counts['candidate_rows_unresolved_identity'] += 1
            continue
        result[(row['game_id'], provider)].append(row)
    counts['candidate_rows'] = sum(len(values) for values in result.values())
    return result, counts


def _source_rows(path, manifest, identities, candidates, games, current):
    """Check every record before returning the small set of relevant source rows."""
    through = manifest['source_data_through_date']
    sources, counts, seen = {}, Counter(), set()
    expanded_total = 0
    for part in manifest['parts']:
        target = (path.parent / part['file']).resolve()
        if target.parent != path.parent:
            raise ValueError('Source part escapes the manifest directory.')
        blob = target.read_bytes()
        if len(blob) != part['bytes'] or hashlib.sha256(blob).hexdigest() != part['sha256']:
            raise ValueError('Source part changed after verification.')
        rows, expanded = 0, 0
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
            while line := stream.readline(MAX_RECORD_BYTES + 1):
                expanded += len(line); expanded_total += len(line)
                if (len(line) > MAX_RECORD_BYTES or expanded > MAX_EXPANDED_PART
                        or expanded_total > MAX_EXPANDED_TOTAL):
                    raise ValueError('Source part exceeds its decompressed bounds.')
                rows += 1; counts['records_read'] += 1
                if rows > part['records'] or counts['records_read'] > MAX_RECORDS:
                    raise ValueError('Source record inventory exceeded.')
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Malformed source event.')
                kind, row = event.get('kind'), event.get('data')
                _validate_bundle_record(kind, row, identities, through)
                key = (kind, row['id']) if kind == 'game' else (kind, row['game_id'], row['player_id'])
                if key in seen:
                    raise ValueError('Duplicate source record.')
                seen.add(key)
                if kind == 'game':
                    if row['id'] not in games and _verified_nba_game(row, through, current):
                        games[row['id']] = row
                    continue
                provider = str(row.get('espn_player_id') or '')
                lookup = (row['game_id'], provider)
                if lookup not in candidates:
                    continue
                counts['matching_source_rows'] += 1
                publication = _instant(row.get('source_published_at'))
                if (row.get('upstream_full_game_validated') is not True or
                        not _source_known(row.get('source')) or publication is None or
                        not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256') or ''))):
                    counts['source_rows_without_required_proof'] += 1
                    continue
                if publication > current:
                    raise ValueError('Source observation publication is in the future.')
                if row['appearance_state'] != 'Played':
                    counts['source_rows_not_played'] += 1
                    continue
                if _nba_source_known(row.get('source')):
                    expected_nba = str(identities[provider].get('nba_player_id') or '')
                    if not expected_nba or str(row.get('nba_player_id') or '') != expected_nba:
                        raise ValueError('NBA source person identity does not match the authorized player.')
                    if row.get('starter') is not None and not _explicit_nba_starter(row):
                        # BoxScoreTraditional's generic position can describe a
                        # bench player's roster role. Even five nonempty roles
                        # cannot establish who actually started the game.
                        row = {**row, 'source_discarded_unproved_starter': row['starter'], 'starter': None}
                        counts['source_starter_flags_without_explicit_proof'] += 1
                starter = row.get('starter')
                if starter is not None and (type(starter) not in (int, float, bool)
                        or not math.isfinite(starter) or starter not in (0, 1)):
                    raise ValueError('Source starter flag must be explicitly zero or one.')
                if not _coherent(row):
                    raise ValueError('Source observation has an incoherent box score.')
                sources[lookup] = row
        if rows != part['records']:
            raise ValueError('Source part record count mismatch.')
    # A normalized bundle may list its observations before the game proof.
    # All events were validated first, so ordering cannot change eligibility.
    for lookup in list(sources):
        if lookup[0] not in games:
            counts['source_rows_without_game_proof'] += 1
            del sources[lookup]
    return sources, counts


def _coherent(row):
    for field in STAT_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        if (type(value) not in (int, float) and not (field == 'starter' and type(value) is bool)):
            return False
        if not math.isfinite(value) or (field != 'plus_minus' and value < 0):
            return False
        if field not in {'minutes', 'starter'} and int(value) != value:
            return False
        if field == 'starter' and value not in (0, 1):
            return False
    for made, attempted in [('fgm', 'fga'), ('tpm', 'tpa'), ('ftm', 'fta'), ('tpm', 'fgm'), ('tpa', 'fga')]:
        if row.get(made) is not None and row.get(attempted) is not None and row[made] > row[attempted]:
            return False
    if all(row.get(field) is not None for field in ('reb', 'oreb', 'dreb')):
        if row['reb'] != row['oreb'] + row['dreb']:
            return False
    if all(row.get(field) is not None for field in ('pts', 'fgm', 'tpm', 'ftm')):
        if row['pts'] != 2 * row['fgm'] + row['tpm'] + row['ftm']:
            return False
    return True


def _plan(old, incoming, game, *, minute_precision=None, starter_evidence=None, starter_conflict=False):
    if (str(old.get('appearance_state') or '').lower() != 'played'
            or game.get('status') != 'final' or game.get('date') != old.get('date')):
        return {}, 'no_longer_eligible', []
    for field in ('date', 'season', 'season_type'):
        if old.get(field) != incoming.get(field):
            return {}, 'identity_or_population_conflict', [field]
    for field in ('team', 'opponent'):
        if canonical_team(old.get(field)) != canonical_team(incoming.get(field)):
            return {}, 'identity_or_population_conflict', [field]
    if {old.get('team'), old.get('opponent')} != {game.get('home_team'), game.get('away_team')}:
        return {}, 'identity_or_population_conflict', ['game_participants']
    additions = {field: incoming[field] for field in STAT_FIELDS
                 if old.get(field) is None and incoming.get(field) is not None}
    if old.get('starter') is None and starter_evidence is not None:
        additions['starter'] = starter_evidence['starter']
    if not additions:
        return {}, 'no_source_values_to_add', []
    if starter_conflict:
        return {}, 'source_starter_disagreement', ['starter']
    differences = [field for field in STAT_FIELDS if old.get(field) is not None
                   and incoming.get(field) is not None and old[field] != incoming[field]]
    if differences:
        non_minutes = [field for field in differences if field != 'minutes']
        if non_minutes:
            return {}, 'known_statistics_conflict', differences
        if minute_precision is not None:
            pass
        elif starter_evidence is not None and old.get('starter') is None:
            # Actual starter membership is independently published categorical
            # evidence. It does not alter or infer a numeric minutes value.
            additions = {'starter': starter_evidence['starter']}
        else:
            return {}, 'known_statistics_conflict', differences
    if not _coherent({**old, **additions}):
        return {}, 'combined_statistics_conflict', []
    return additions, None, []


def _audit_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS player_stat_completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
        player_id TEXT NOT NULL, statistic TEXT NOT NULL, completed_at TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, old_value_json TEXT NOT NULL,
        new_value_json TEXT NOT NULL, source TEXT NOT NULL,
        source_content_sha256 TEXT NOT NULL, source_published_at TEXT NOT NULL,
        before_row_json TEXT NOT NULL, source_evidence_json TEXT NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS player_stat_completions_no_update
        BEFORE UPDATE ON player_stat_completions BEGIN
        SELECT RAISE(ABORT, 'Player statistic completion audits are immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS player_stat_completions_no_delete
        BEFORE DELETE ON player_stat_completions BEGIN
        SELECT RAISE(ABORT, 'Player statistic completion audits are immutable'); END''')


def complete_verified_player_statistics(store, manifest_path, *, now=None, dry_run=False,
                                       starter_roster_path=None):
    """Fill source-observed NULL cells when every known overlapping statistic agrees.

    The entire bundle is validated before a backup or database write. ``dry_run``
    performs that same validation and reports fillable cells without modifying
    the database. Minute conflicts remain held except a verified original ESPN
    whole-minute/NBA-clock precision relationship, or a separately proven
    categorical starter flag. Neither exception changes stored minutes.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Statistic completion time requires an explicit timezone.')
    path = Path(manifest_path).resolve()
    digest, games, game_counts = _games(path, current)
    manifest_blob = path.read_bytes()
    if hashlib.sha256(manifest_blob).hexdigest() != digest:
        raise ValueError('Manifest changed after source verification.')
    manifest = json.loads(manifest_blob)
    through = dt.date.fromisoformat(manifest['source_data_through_date']).isoformat()
    if through > current.astimezone(ZoneInfo('America/New_York')).date().isoformat():
        raise ValueError('Source manifest claims future covered games.')
    identities, aliases = _identity_index(manifest)
    candidates, counts = _candidate_rows(store, aliases, identities, through, current)
    sources, source_counts = _source_rows(path, manifest, identities, candidates, games, current)
    counts.update(source_counts)
    snapshots = _old_precision_snapshots(store, candidates)
    rosters, roster_digest = _roster_records(starter_roster_path, identities, current)
    plans, field_counts, conflicts = [], Counter(), Counter()
    for lookup, existing in candidates.items():
        incoming = sources.get(lookup)
        if incoming is None:
            counts['candidate_rows_without_verified_source'] += len(existing)
            continue
        for old in existing:
            game = games[old['game_id']]
            minute_precision = _minute_precision_evidence(old, incoming, snapshots)
            starter, starter_conflict = _starter_evidence(incoming, game, rosters)
            additions, reason, differences = _plan(old, incoming, game,
                minute_precision=minute_precision, starter_evidence=starter, starter_conflict=starter_conflict)
            if reason:
                counts['rows_held_' + reason] += 1
                conflicts.update(differences)
            else:
                plans.append((old['game_id'], old['player_id'], incoming, starter, minute_precision))
                field_counts.update(additions.keys())
    result = {'manifest_sha256': digest, 'source_data_through_date': through,
              **dict(counts), 'rows_fillable': len(plans), 'fields_fillable': sum(field_counts.values()),
              'fillable_by_statistic': dict(field_counts), 'conflicting_source_fields': dict(conflicts),
              'rows_completed': 0, 'fields_completed': 0, 'completed_by_statistic': {},
              'backup': None, 'dry_run': bool(dry_run),
              'starter_roster_sha256': roster_digest,
              'scope': 'Source-observed missing player statistics only; known values and current readiness remain unchanged.'}
    if dry_run or not plans:
        return result
    destination = Path(store.path).parent / 'backups' / ('before-stat-completion-' + digest[:16] + '.sqlite')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result['backup'] = store.backup(str(destination))
    completed = Counter()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for game_id, player_id, incoming, starter, prior_precision in plans:
            original = db.execute('SELECT * FROM player_games WHERE game_id=? AND player_id=?',
                                  (game_id, player_id)).fetchone()
            event = db.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()
            if original is None or event is None:
                continue
            old = dict(original)
            minute_precision = _minute_precision_evidence(old, incoming, snapshots)
            _, starter_conflict = _starter_evidence(incoming, games[game_id], rosters)
            additions, _, _ = _plan(old, incoming, dict(event),
                minute_precision=minute_precision, starter_evidence=starter, starter_conflict=starter_conflict)
            if not additions:
                continue
            _audit_schema(db)
            stamp = current.isoformat()
            for field, value in additions.items():
                field_source = starter if field == 'starter' and starter is not None else incoming
                evidence = {'observation': incoming, 'game': games[game_id],
                            'minute_precision_verified': minute_precision,
                            'starter_source': starter if field == 'starter' else None}
                db.execute('''INSERT INTO player_stat_completions
                    (game_id,player_id,statistic,completed_at,manifest_sha256,old_value_json,
                     new_value_json,source,source_content_sha256,source_published_at,
                     before_row_json,source_evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (game_id, player_id, field, stamp, digest, 'null', encoded(value), field_source['source'],
                     field_source['source_content_sha256'], field_source['source_published_at'], encoded(old), encoded(evidence)))
                store._snapshot(db, 'player_stat_completion', game_id + ':' + player_id + ':' + field,
                    {'statistic': field, 'value': value, 'source': field_source['source'],
                     'manifest_sha256': digest, 'source_content_sha256': field_source['source_content_sha256'],
                     'source_published_at': field_source['source_published_at'], 'before': old,
                     'verified_source': evidence})
                completed[field] += 1
            fields = list(additions)
            db.execute('UPDATE player_games SET ' + ','.join(field + '=?' for field in fields)
                       + ',updated_at=? WHERE game_id=? AND player_id=?',
                       [additions[field] for field in fields] + [stamp, game_id, player_id])
            result['rows_completed'] += 1
    result['fields_completed'] = sum(completed.values())
    result['completed_by_statistic'] = dict(completed)
    return result
