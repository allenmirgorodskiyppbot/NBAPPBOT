"""Normalize public NBA hustle and actual player-matchup publications.

Actual NBA person/game IDs and dated NBA results establish measured observations.
Neither team coappearance nor a release timestamp establishes guarding or a
covered-game date. This module performs no network or database writes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import datetime as dt
import math
import re

from advanced_refresh import MAPS, normalize_records
from feeds import TEAM_WORDS, canonical_team, canonical_game
from game_phase import regular_phase_exclusion
from master import normalized_name

PAIR_FIELDS = {
    'matchup_minutes': 'matchup_minutes', 'partial_possessions': 'partial_possessions',
    'player_points': 'player_points', 'team_points': 'team_points',
    'matchup_field_goals_made': 'fgm', 'matchup_field_goals_attempted': 'fga',
    'matchup_three_pointers_made': 'tpm', 'matchup_three_pointers_attempted': 'tpa',
    'matchup_free_throws_made': 'ftm', 'matchup_free_throws_attempted': 'fta',
    'matchup_assists': 'ast', 'matchup_turnovers': 'tov', 'shooting_fouls': 'shooting_fouls',
    'matchup_potential_assists': 'potential_assists', 'matchup_blocks': 'blocks',
    'help_blocks': 'help_blocks', 'help_field_goals_made': 'help_fgm',
    'help_field_goals_attempted': 'help_fga', 'switches_on': 'switches_on',
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _phase(value, season_type='Regular Season'):
    # These are NBA Stats identifiers, not ESPN season_type codes. NBA 3 is
    # All-Star, 4 is Playoffs, 5 is Play-In and 6 is the Cup championship.
    aliases = {'Regular Season': {'2', 'regular-season', 'regular season'},
               'Playoffs': {'4', 'playoffs', 'postseason'},
               'Play-In': {'5', 'play-in', 'playin', 'play in'}}
    if season_type not in aliases:
        raise ValueError('Unsupported measured statistical phase.')
    return str(value).lower() in aliases[season_type]


def _game_excluded(game, season_type):
    if season_type == 'Regular Season':
        return regular_phase_exclusion({**game, 'season_type': 'Regular Season'})
    return (game['home_team'] not in TEAM_WORDS or game['away_team'] not in TEAM_WORDS
            or game['home_team'] == game['away_team'])


def _event_in_season(game, year, season_type):
    return bool(game.get('date') and int(game['date'][:4]) in {year - 1, year}
                and re.fullmatch(r'\d{10}', game.get('id') or '')
                and game['id'][:3] == {'Regular Season': '002', 'Playoffs': '004', 'Play-In': '005'}[season_type]
                and game['id'][3:5] == str(year - 1)[-2:]
                and all(value is not None and value == int(value) for value in
                        (game.get('home_score'), game.get('away_score'))))


def _day(value):
    try:
        text = str(value)[:10]
        return dt.date.fromisoformat(text).isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text) else None
    except ValueError:
        return None


def _season_year(season):
    if not re.fullmatch(r'\d{4}-\d{2}', str(season)):
        raise ValueError('An explicit NBA season is required.')
    start = int(season[:4])
    if int(season[-2:]) != (start + 1) % 100:
        raise ValueError('Inconsistent NBA season.')
    return start + 1


def normalize_hustle(rows, scope, season):
    """Accept only cumulative hustle values; G is their own source denominator."""
    year = _season_year(season)
    converted = []
    for row in rows:
        if (str(row.get('season')) != str(year) or not _phase(row.get('season_type'))
                or str(row.get('per_mode')).lower() != 'totals'):
            continue
        item = {key.upper(): value for key, value in row.items()}
        item['PER_MODE'] = 'Totals'
        converted.append(item)
    return normalize_records(converted, 'Hustle', scope, season)


def game_date_inventory(schedule_rows, team_log_rows, season, *, mapping_sources=(), season_type='Regular Season'):
    """Validate NBA-ID dates against actual dated results, not remembered schedules."""
    year = _season_year(season)
    candidates = defaultdict(list)
    rejected = Counter()
    def proof(kind):
        # A newer team-results file can establish a game absent from the older
        # schedule. Its proof must not also credit a pre-game publication.
        tags = {'schedule': '/nba_stats_schedules/', 'logs': '/nba_stats_player_game_logs/'}
        recognized = any(any(tag in item.get('source', '') for tag in tags.values())
                         for item in mapping_sources)
        return [item for item in mapping_sources if not recognized or tags[kind] in item.get('source', '')]
    for row in schedule_rows:
        if str(row.get('season')) != str(year) or not _phase(row.get('season_type'), season_type):
            continue
        day = _day(row.get('game_date'))
        home, away = canonical_team(row.get('home_team_abbreviation')), canonical_team(row.get('away_team_abbreviation'))
        hs, aws = _number(row.get('home_pts')), _number(row.get('away_pts'))
        if hs is None or aws is None or hs == aws:
            continue
        outcome = (row.get('home_wl'), row.get('away_wl'))
        if outcome != (('W', 'L') if hs > aws else ('L', 'W')):
            rejected['schedule_outcome_conflict'] += 1; continue
        game = {'id': str(row.get('game_id') or ''), 'date': day, 'home_team': home,
                'away_team': away, 'home_score': hs, 'away_score': aws, 'mapping_kind': 'published_final_schedule',
                'season_type':season_type, 'mapping_sources':proof('schedule')}
        if not _event_in_season(game, year, season_type) or _game_excluded(game, season_type):
            rejected['schedule_identity_or_phase'] += 1; continue
        candidates[game['id']].append(game)
    groups = defaultdict(list)
    for row in team_log_rows:
        if str(row.get('season')) == str(year) and _phase(row.get('season_type'), season_type):
            groups[str(row.get('game_id') or '')].append(row)
    for provider, rows in groups.items():
        # This unfortunately named dataset contains TEAM rows. It is used only
        # for game dates/results, never individual player plus-minus or totals.
        unique = {}
        conflict = False
        for row in rows:
            identity = (str(row.get('team_id')), row.get('team_abbreviation'))
            previous = unique.get(identity)
            if previous and any(previous.get(field) != row.get(field) for field in
                                ('game_date', 'matchup', 'pts', 'wl')):
                conflict = True
            unique[identity] = row
        if conflict:
            rejected['team_log_duplicate_conflict'] += 1; continue
        if len(unique) != 2:
            rejected['team_log_participants'] += 1; continue
        rows = list(unique.values())
        home = [row for row in rows if re.search(r'\bvs\.?\s', str(row.get('matchup') or ''), re.I)]
        away = [row for row in rows if '@' in str(row.get('matchup') or '')]
        if len(home) != 1 or len(away) != 1:
            rejected['team_log_home_away'] += 1; continue
        home, away = home[0], away[0]
        day = _day(home.get('game_date'))
        hs, aws = _number(home.get('pts')), _number(away.get('pts'))
        if (not day or day != _day(away.get('game_date')) or hs is None or aws is None or hs == aws
                or (home.get('wl'), away.get('wl')) != (('W', 'L') if hs > aws else ('L', 'W'))):
            rejected['team_log_result'] += 1; continue
        game = {'id': provider, 'date': day, 'home_team': canonical_team(home['team_abbreviation']),
                'away_team': canonical_team(away['team_abbreviation']), 'home_score': hs, 'away_score': aws,
                'mapping_kind': 'published_team_game_results','season_type':season_type,
                'mapping_sources':proof('logs')}
        if not _event_in_season(game, year, season_type) or _game_excluded(game, season_type):
            rejected['team_log_identity_or_phase'] += 1; continue
        candidates[provider].append(game)
    result = {}
    fields = ('date', 'home_team', 'away_team', 'home_score', 'away_score')
    for provider, versions in candidates.items():
        if any(len({row[key] for row in versions}) != 1 for key in fields):
            rejected['mapping_source_conflict'] += 1; continue
        sources = []
        for version in versions:
            sources.extend(source for source in version['mapping_sources'] if source not in sources)
        result[provider] = {**versions[0], 'mapping_sources': sources}
    return result, dict(rejected)


def _identities(scope, alias_rows=()):
    ids, names = defaultdict(list), defaultdict(list)
    for player in scope['players']:
        aliases = {player['player'], *player.get('aliases', [])}
        for extra in alias_rows:
            if ((extra.get('nba_player_id') and str(extra['nba_player_id']) == str(player.get('nba_player_id')))
                    or normalized_name(extra.get('requested_player') or extra.get('player')) == normalized_name(player['player'])):
                aliases.update(value for value in [extra.get('requested_player'), extra.get('player'), *extra.get('aliases', [])] if value)
        item = {**player, '_aliases': {normalized_name(name) for name in aliases}}
        if player.get('nba_player_id'):
            ids[str(player['nba_player_id'])].append(item)
        for name in item['_aliases']:
            names[name].append(item)
    return ids, names


def _player(person, first, family, ids, names):
    if isinstance(person, bool) or not str(person or '').isdigit():
        return None
    raw_name = normalized_name((str(first or '') + ' ' + str(family or '')).strip())
    candidates = ids.get(str(person), [])
    if not candidates:
        candidates = [item for item in names.get(raw_name, []) if not item.get('nba_player_id')]
    unique = {item['player']: item for item in candidates if raw_name in item['_aliases']}
    return next(iter(unique.values()))['player'] if len(unique) == 1 else None


def _minutes(value):
    if isinstance(value, str) and re.fullmatch(r'\d+:[0-5]\d(?:\.\d+)?', value):
        minutes, seconds = value.split(':')
        return int(minutes) + float(seconds) / 60
    return None


def measured_matchups(rows, inventory, scope, pair_scope, season, *, source,
                      published_at, source_hash, now, alias_rows=(), season_type='Regular Season'):
    """Return actual per-game guarding rows and their separate season aggregates."""
    year = _season_year(season)
    if now.tzinfo is None:
        raise ValueError('Tracking normalization needs an aware observation time.')
    publication = dt.datetime.fromisoformat(published_at.replace('Z', '+00:00'))
    if publication.tzinfo is None or publication > now:
        raise ValueError('Source publication time is unavailable or in the future.')
    ids, names = _identities(scope, alias_rows)
    allowed = {(row['offense_player'], row['defense_player']) for row in pair_scope['pairs']}
    team_ids = pair_scope.get('team_ids', {})
    staged, rejected = defaultdict(list), Counter()
    for raw in rows:
        if str(raw.get('season')) != str(year) or not _phase(raw.get('season_type_id', raw.get('season_type')), season_type):
            continue
        offense = _player(raw.get('off_person_id'), raw.get('off_first_name'), raw.get('off_family_name'), ids, names)
        defense = _player(raw.get('def_person_id'), raw.get('def_first_name'), raw.get('def_family_name'), ids, names)
        if not offense or not defense or (offense, defense) not in allowed:
            continue
        provider = str(raw.get('game_id') or '')
        game = inventory.get(provider)
        if not game:
            rejected['missing_verified_game_date'] += 1; continue
        if game.get('season_type',season_type)!=season_type:
            rejected['game_phase_identity_conflict'] += 1; continue
        if game['date'] > now.date().isoformat() or game['date'] > publication.date().isoformat():
            rejected['future_or_post_publication_game'] += 1; continue
        offense_team = canonical_team(raw.get('off_team_tricode'))
        defense_team = team_ids.get(str(raw.get('def_team_id')))
        if (not offense_team or not defense_team or offense_team == defense_team
                or {offense_team, defense_team} != {game['home_team'], game['away_team']}
                or team_ids.get(str(raw.get('off_team_id'))) != offense_team):
            rejected['game_team_identity_conflict'] += 1; continue
        fields = {target: _minutes(raw.get(key)) if key == 'matchup_minutes' else _number(raw.get(key))
                  for key, target in PAIR_FIELDS.items()}
        if fields['matchup_minutes'] is None or fields['partial_possessions'] is None:
            rejected['missing_matchup_exposure'] += 1; continue
        if not any(value and value > 0 for value in fields.values()):
            rejected['zero_measured_exposure'] += 1; continue
        if any(fields[made] is not None and fields[attempts] is not None and fields[made] > fields[attempts]
               for made, attempts in [('fgm', 'fga'), ('tpm', 'tpa'), ('ftm', 'fta'), ('help_fgm', 'help_fga')]):
            rejected['makes_exceed_attempts'] += 1; continue
        key = (provider, str(raw['off_person_id']), str(raw['def_person_id']))
        row = {'pair_observation_id': '_'.join(key), 'game_id': provider, 'date': game['date'],
            'season': season, 'season_type': season_type, 'offense_player': offense,
            'defense_player': defense, 'offense_nba_player_id': key[1], 'defense_nba_player_id': key[2],
            'offense_player_id': 'nba:' + key[1], 'defense_player_id': 'nba:' + key[2],
            'offense_team': offense_team, 'defense_team': defense_team,
            'matchup_seconds': fields['matchup_minutes'] * 60,
            **{('matchup_assists' if key == 'ast' else 'matchup_turnovers' if key == 'tov' else key): value
               for key, value in fields.items() if key != 'matchup_minutes'},
            'source': source, 'source_id': 'NBA_' + provider + '_matchups',
            'published_at': published_at, 'retrieved_at': now.isoformat(), 'source_content_sha256': source_hash,
            'date_mapping_sources': game.get('mapping_sources', []),
            'population': 'Actual measured named player pair; not team coappearance',
            'statistic_units': 'Counts; matchup_seconds is seconds; partial_possessions are fractional possessions.'}
        staged[key].append((row, fields))
    measured, totals = [], defaultdict(list)
    for key, versions in sorted(staged.items()):
        if len({str(sorted(fields.items())) for _, fields in versions}) != 1:
            rejected['conflicting_duplicate_pair_game'] += 1; continue
        row, fields = versions[0]
        rejected['identical_duplicate_pair_game'] += len(versions) - 1
        measured.append(row)
        totals[(row['offense_player'], row['defense_player'])].append((row, fields))
    records = []
    for (offense, defense), observations in sorted(totals.items()):
        fields = {key: sum(values[key] for _, values in observations)
                  for key in PAIR_FIELDS.values() if all(values[key] is not None for _, values in observations)}
        fields['fg_pct'] = (fields['fgm'] / fields['fga'] if fields.get('fga', 0) > 0 and 'fgm' in fields else None)
        dates = [row['date'] for row, _ in observations]
        record = {'entity': offense + ' | ' + defense, 'category': 'Named pair', 'season': season,
            'season_type': season_type, 'gp': len(observations), 'fields': fields,
            'per_mode': 'Totals', 'field_units': {key: 'ratio' if key == 'fg_pct' else 'cumulative_total' for key in fields},
            'source': source, 'published_at': published_at, 'source_published_at': published_at,
            'publication_time_kind': 'publisher_modified', 'retrieved_at': now.isoformat(),
            'publication_source': 'SportsDataverse published NBA game matchup extract',
            'source_content_sha256': source_hash, 'data_through_date': max(dates),
            'first_observed_game_date': min(dates), 'covered_game_ids': sorted({row['game_id'] for row, _ in observations}),
            'coverage_basis': 'verified_game_inventory',
            'population': 'Actual measured named pair games present in the publication',
            'coverage_note': 'Date is the last observed measured pair game, not proof of complete NBA or lifetime pair coverage.',
            'date_mapping_sources': observations[0][0].get('date_mapping_sources', [])}
        records.append(record)
    report = {'source_rows': len(rows), 'measured_pair_games': len(measured), 'named_pairs': len(records),
              'first_observed_game_date': min((r['date'] for r in measured), default=None),
              'last_observed_game_date': max((r['date'] for r in measured), default=None),
              'held_rows': dict(rejected), 'coverage_claim': 'Published actual pair-game observations only.'}
    return records, measured, report
