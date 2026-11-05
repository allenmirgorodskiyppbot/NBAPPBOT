"""Complete only NULL historical final scores from fully verified game sources.

Known scores and all other saved game fields stay byte-for-byte unchanged. An
independent source-ID crosswalk is required when NBA and ESPN IDs differ. This
historical repair never changes freshness, schedule, or live readiness checks.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

from career_metadata import _games, _instant
from feeds import canonical_game, canonical_team
from master import encoded
from history_backfill import HISTORICAL_FRANCHISES, history_exclusion
from stat_completion import _identity_index, _source_rows, _nba_source_known

SCORES = ('home_score', 'away_score')
CROSSWALK_SOURCE = (r'https://github\.com/sportsdataverse/sportsdataverse-data/releases/download/'
                    r'nba_stats_player_game_logs/player_game_logs_\d{4}\.parquet')
NBA_TEAM_IDS = {str(1610612737 + index): team for index, team in enumerate(
    'ATL BOS CLE NOP CHI DAL DEN GSW HOU LAC LAL MIA MIL MIN BKN NYK ORL IND PHI PHX POR SAC SAS OKC TOR UTA MEM WAS DET CHA'.split())}


def _provider_crosswalk(path, current):
    if path is None:
        return {}, None
    blob = Path(path).read_bytes()
    if not 0 < len(blob) <= 8_000_000:
        raise ValueError('Game identity crosswalk exceeds its compressed bound.')
    result, expanded, count = {}, 0, 0
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        while line := stream.readline(100_001):
            expanded += len(line); count += 1
            if len(line) > 100_000 or expanded > 64_000_000 or count > 200_000:
                raise ValueError('Game identity crosswalk exceeds its record bounds.')
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('Game identity crosswalk must contain objects.')
            proof = row.get('proof')
            published = _instant(row.get('source_published_at'))
            day, provider = str(row.get('date') or ''), str(row.get('provider_game_id') or '')
            home, away = canonical_team(row.get('home_team')), canonical_team(row.get('away_team'))
            if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                    or dt.date.fromisoformat(day).isoformat() != day
                    or day > current.astimezone(ZoneInfo('America/New_York')).date().isoformat()
                    or not re.fullmatch(r'00[245]\d{7}', provider)
                    or not home or not away or home == away
                    or row.get('game_id') != canonical_game(day, home, away)
                    or not re.fullmatch(CROSSWALK_SOURCE, str(row.get('source') or ''))
                    or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256') or ''))
                    or published is None or published > current
                    or not isinstance(proof, dict)
                    or proof.get('consistent_game_date_and_home_away_headers') is not True
                    or proof.get('both_source_teams_observed') is not True
                    or str(proof.get('provider_game_id') or '') != provider):
                raise ValueError('Game identity crosswalk lacks verified two-team source headers.')
            key = (row['game_id'], provider)
            if key in result and result[key] != row:
                raise ValueError('Conflicting source provider crosswalk.')
            # A provider event must never identify multiple canonical games.
            if any(key[1] == prior[1] and key[0] != prior[0] for prior in result):
                raise ValueError('Source provider event maps to multiple canonical games.')
            result[key] = row
    return result, hashlib.sha256(blob).hexdigest()



def _game_log_score_proof(row):
    """Two published team lines can prove scores without proving player boxes."""
    proof = row.get('proof', {})
    if (proof.get('source_rows') != 2 or type(proof.get('source_rows')) is not int
            or proof.get('team_plus_minus_consistent') is not True
            or proof.get('team_points_components_consistent') is not True):
        return None
    home, away = row['home_team'], row['away_team']
    raw_home = str(proof.get('home_raw_team_abbreviation') or '')
    raw_away = str(proof.get('away_raw_team_abbreviation') or '')
    def franchise(value):
        team = canonical_team(value)
        return HISTORICAL_FRANCHISES.get(team, team)
    if (franchise(raw_home) != home or franchise(raw_away) != away
            or proof.get('home_matchup_header') != raw_home + ' vs. ' + raw_away
            or proof.get('away_matchup_header') != raw_away + ' @ ' + raw_home
            or not re.fullmatch(r'16106127\d{2}', str(proof.get('home_source_team_id') or ''))
            or not re.fullmatch(r'16106127\d{2}', str(proof.get('away_source_team_id') or ''))
            or NBA_TEAM_IDS.get(str(proof.get('home_source_team_id'))) != home
            or NBA_TEAM_IDS.get(str(proof.get('away_source_team_id'))) != away):
        return None
    for field in SCORES:
        value = row.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or int(value) != value or not 0 <= value <= 400:
            return None
    if row['home_score'] == row['away_score']:
        return None
    home_won = row['home_score'] > row['away_score']
    if proof.get('home_wl') != ('W' if home_won else 'L') or proof.get('away_wl') != ('L' if home_won else 'W'):
        return None
    phase = {'regular-season': 'Regular Season', 'playoffs': 'Playoffs', 'play-in': 'Play-In'}.get(row.get('season_type'))
    game = {**row, 'id': row['game_id'], 'status': 'final', 'season_type': phase,
            'upstream_full_game_validated': False,
            'proof_kind': 'two_team_game_log_final_scores',
            'scope': 'Team final scores only; player box-score completeness is not established.'}
    return None if history_exclusion(game) else game


def _plan(db, old, incoming, crosswalk):
    if old.get('status') != 'final':
        return {}, 'not_final', None
    if any(old.get(field) != incoming.get(field) for field in ('id', 'date', 'home_team', 'away_team')):
        return {}, 'event_identity_conflict', None
    if any(old.get(field) is not None and old[field] != incoming[field] for field in SCORES):
        return {}, 'known_score_conflict', None
    old_provider = str(old.get('provider_game_id') or '')
    new_provider = str(incoming.get('provider_game_id') or '')
    binding = None
    if not old_provider:
        return {}, 'missing_saved_provider_identity', None
    if old_provider != new_provider:
        if _nba_source_known(incoming.get('source')):
            return {}, 'provider_identity_conflict', None
        binding = crosswalk.get((old['id'], old_provider))
        if binding is None or any(binding.get(field) != old.get(field) for field in
                                  ('date', 'home_team', 'away_team')):
            return {}, 'provider_identity_conflict', None
        if any(binding.get(field) is not None and binding[field] != incoming[field] for field in SCORES):
            return {}, 'crosswalk_score_conflict', None
    if old.get('start_time') and incoming.get('start_time'):
        if _instant(old['start_time']) is None or _instant(old['start_time']) != _instant(incoming['start_time']):
            return {}, 'known_tipoff_conflict', None
    bad_player = db.execute('SELECT 1 FROM player_games WHERE game_id=? AND '
        '(date IS NULL OR date!=? OR team IS NULL OR opponent IS NULL OR team NOT IN (?,?) '
        'OR opponent NOT IN (?,?) OR team=opponent) LIMIT 1',
        (old['id'], old['date'], old['home_team'], old['away_team'], old['home_team'], old['away_team'])).fetchone()
    if bad_player:
        return {}, 'saved_observation_identity_conflict', None
    return {field: incoming[field] for field in SCORES if old.get(field) is None}, None, binding


def _audit_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS game_score_completions (
        game_id TEXT NOT NULL, statistic TEXT NOT NULL, completed_at TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
        before_row_json TEXT NOT NULL, after_row_json TEXT NOT NULL, source_evidence_json TEXT NOT NULL,
        PRIMARY KEY(game_id,statistic))''')
    for action in ('UPDATE', 'DELETE'):
        db.execute('CREATE TRIGGER IF NOT EXISTS game_score_completions_no_' + action.lower() +
                   ' BEFORE ' + action + ' ON game_score_completions BEGIN '
                   "SELECT RAISE(ABORT, 'Game score completion audits are immutable'); END")


def complete_verified_game_scores(store, manifest_path, *, now=None, dry_run=False,
                                  provider_crosswalk_path=None):
    """Validate every part first; fill compatible NULL scores in one transaction."""
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Game score completion time requires an explicit timezone.')
    path = Path(manifest_path).resolve()
    digest, games, counts = _games(path, current)
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError('Manifest changed after validation.')
    manifest = json.loads(blob)
    if manifest['source_data_through_date'] > current.astimezone(ZoneInfo('America/New_York')).date().isoformat():
        raise ValueError('Source manifest claims future coverage.')
    identities, _ = _identity_index(manifest)
    # Validate all normalized game and player records, including NBA full-game
    # proofs not admitted by the ESPN-only legacy metadata reader.
    _source_rows(path, manifest, identities, {}, games, current)
    crosswalk, crosswalk_digest = _provider_crosswalk(provider_crosswalk_path, current)
    counts['verified_source_games'] = len(games)
    counts['source_games_without_required_proof'] = counts['source_games'] - len(games)
    for bridge in crosswalk.values():
        if bridge['game_id'] not in games and (score_game := _game_log_score_proof(bridge)) is not None:
            games[bridge['game_id']] = score_game
            counts['verified_game_log_score_proofs'] += 1
    plans, fillable, conflicts = [], Counter(), []
    with store.connect() as db:
        candidates = [dict(row) for row in db.execute(
            'SELECT * FROM games WHERE home_score IS NULL OR away_score IS NULL')]
        counts['candidate_games'] = len(candidates)
        for old in candidates:
            incoming = games.get(old['id'])
            if incoming is None:
                counts['games_without_verified_source'] += 1
                continue
            additions, reason, _ = _plan(db, old, incoming, crosswalk)
            if reason:
                counts['games_held_' + reason] += 1
                conflicts.append({'game_id': old['id'], 'reason': reason})
            elif additions:
                plans.append((old['id'], incoming)); fillable.update(additions.keys())
    result = {'manifest_sha256': digest, **dict(counts), 'dry_run': bool(dry_run),
              'rows_fillable': len(plans), 'fields_fillable': sum(fillable.values()),
              'fillable_by_statistic': dict(fillable), 'rows_completed': 0, 'fields_completed': 0,
              'completed_by_statistic': {}, 'backup': None, 'conflicts': conflicts,
              'provider_crosswalk_sha256': crosswalk_digest,
              'scope': 'Historical NULL final scores only; all other saved facts and readiness checks remain unchanged.'}
    if dry_run or not plans:
        return result
    destination = Path(store.path).parent / 'backups' / (
        'before-game-scores-' + digest[:16] + '-' + uuid.uuid4().hex[:12] + '.sqlite')
    result['backup'] = store.backup(str(destination))
    completed = Counter()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for game_id, incoming in plans:
            found = db.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()
            if found is None:
                continue
            old = dict(found)
            additions, _, binding = _plan(db, old, incoming, crosswalk)
            if not additions:
                continue
            _audit_schema(db)
            after = {**old, **additions}
            for field, value in additions.items():
                db.execute('''INSERT INTO game_score_completions VALUES (?,?,?,?,?,?,?,?,?)''',
                    (game_id, field, current.isoformat(), digest, 'null', encoded(value),
                     encoded(old), encoded(after), encoded({'game': incoming, 'provider_binding': binding,
                     'provider_crosswalk_sha256': crosswalk_digest})))
            fields = list(additions)
            db.execute('UPDATE games SET ' + ','.join(field + '=?' for field in fields) + ' WHERE id=?',
                       [additions[field] for field in fields] + [game_id])
            saved = dict(db.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone())
            if saved != after:
                raise ValueError('Game completion altered a protected field.')
            completed.update(additions.keys())
            result['rows_completed'] += 1
    result['fields_completed'] = sum(completed.values())
    result['completed_by_statistic'] = dict(completed)
    return result
