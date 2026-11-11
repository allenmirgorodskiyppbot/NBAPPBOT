"""A small, typed, read-only evidence API for conversational questions.

The model chooses questions to ask this API, never SQL, code, formulas, roster
membership, or whether an offer qualifies. Dates and missing values retain their
meaning when several reads are combined into an explanation.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import defaultdict
import datetime as dt
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
from zoneinfo import ZoneInfo

from basketball_research import TEAM_NAMES
from board_shortlist import MARKET_NAMES
from current_research import TABLE_FILTERS
from feeds import canonical_player, canonical_team
from master import STAT_COMPONENTS, normalized_name
from pick_discussion import normalize_stat

EASTERN = ZoneInfo('America/New_York')
_ARGUMENTS = {
    'player_games': {'player', 'stat', 'window', 'opponent', 'phase', 'season'},
    'player_profile': {'player', 'window'},
    'team_games': {'team', 'window'},
    'next_game': {'team', 'date', 'after_date'},
    'schedule': {'team', 'start_date', 'end_date', 'limit'},
    'availability': {'player', 'date'},
    'prop_assessment': {'player', 'stat', 'line', 'date', 'side'},
    'research_table': {'table', 'filters', 'limit'},
}
TOOL_NAMES = tuple(_ARGUMENTS)
CAPABILITIES = (
    'Read-only tools (JSON {tool,args}): player_games(player,stat,window=5,opponent?,phase="Regular Season",season?) '
    'returns final Played logs, mean/median/range and missing counts; window=1..100 or "career" means ALL STORED history, not verified lifetime coverage. '
    'player_profile(player,window=20) returns points/rebounds/assists/minutes/attempts and observed counts. '
    'team_games(team,window=5) returns final scores/form. next_game(team="NYK",date?,after_date?) reads the saved calendar; after_date is exclusive. '
    'schedule(team="NYK",start_date?,end_date?,limit=10) reads Knicks season fixtures; timing may be unverified. '
    'availability(player,date?) reads dated official/news reports; absence is unknown. '
    'prop_assessment(player,stat,line,date?,side?) computes the existing strict decision; never upgrade Pass. '
    'research_table(table,filters,limit=3) reads exact saved derivatives: Player_Metrics,Calculated_Splits,Correlations,Pair_Summaries,Tracking_Metrics,Measured_Pair_Metrics,Matchup_Assignments,Team_Schedule,Refresh_Coverage. '
    'Filters must be exact supported column names. Use separate reads to compare players or statistics. No SQL, code, writes or network.'
)
PLAN_CAPABILITIES = (
    'player_games(player,stat,window=5,opponent?,phase?,season?): played logs; career=all STORED history. '
    'player_profile(player,window=20): saved averages. '
    'team_games(team,window=5): PAST scores only. '
    'next_game(team=NYK,date?,after_date?): first fixture on/after date(today default) or STRICTLY AFTER after_date. '
    'schedule(team=NYK,start_date?,end_date?,limit=10): season fixtures; inclusive bounds. '
    'availability(player,date?): dated status, unknown if absent. '
    'prop_assessment(player,stat,line,date?,side?): authoritative More/Less/Pass. '
    'research_table(table,filters,limit=3): exact derivatives. '
    'ISO dates. No SQL/code/writes.'
)
_EXTRA_COMPONENTS = {name: (name,) for name in ('fgm', 'ftm', 'fta', 'fouls', 'plus_minus')}
_COMPONENTS = {**STAT_COMPONENTS, **_EXTRA_COMPONENTS}
_EXTRA_ALIASES = {'free throw attempts': 'fta', 'free throws attempted': 'fta',
                  'free throws made': 'ftm', 'field goals made': 'fgm',
                  'personal fouls': 'fouls', 'plus minus': 'plus_minus', 'plus-minus': 'plus_minus'}
_LABELS = {**MARKET_NAMES, 'fgm': 'field goals made', 'ftm': 'free throws made',
           'fta': 'free throw attempts', 'fouls': 'personal fouls', 'plus_minus': 'plus-minus'}
_PHASES = {'regular season': 'Regular Season', 'regularseason': 'Regular Season',
           'playoffs': 'Playoffs', 'preseason': 'Preseason', 'pre season': 'Preseason'}


class _Missing(ValueError):
    pass


def validate_task(task):
    """Validate structure only; canonical names and scopes require the master."""
    if (type(task) is not dict or set(task) - {'tool', 'args'}
            or type(task.get('tool')) is not str or task['tool'] not in _ARGUMENTS):
        return None
    args = task.get('args', {})
    if type(args) is not dict or set(args) - _ARGUMENTS[task['tool']]:
        return None
    for key, value in args.items():
        if key == 'filters':
            if (type(value) is not dict or len(value) > 10
                    or any(type(k) is not str or len(k) > 60 for k in value)
                    or any(v is not None and (type(v) is not str or not 1 <= len(v.strip()) <= 150)
                           for v in value.values())):
                return None
        elif value is not None and (type(value) not in (str, int, float)
                or type(value) is str and (not value.strip() or len(value) > 150)
                or type(value) in (int, float) and not math.isfinite(value)):
            return None
    return {'tool': task['tool'], 'args': dict(args)}


@contextmanager
def _open(master):
    path = getattr(master, 'database_path', None) or getattr(master, 'path', None)
    if not path:
        raise _Missing('The saved master database is not ready.')
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def _clock(value):
    value = value or dt.datetime.now(dt.timezone.utc)
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise _Missing('A timezone-aware clock is required for dated evidence.')
    return value


def _date(value, now):
    if value is None:
        return now.astimezone(EASTERN).date().isoformat()
    try:
        if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise _Missing('Use a real game date in YYYY-MM-DD format.') from None


def _window(value=5, *, career=False):
    if career and value == 'career':
        return value
    if type(value) is not int or not 1 <= value <= 100:
        raise _Missing('Use a game window from 1 to 100' + (' or "career" for all stored history.' if career else '.'))
    return value


def _stat(value, *, prop=False):
    raw = str(value or '').strip().casefold()
    stat = normalize_stat(raw) or _EXTRA_ALIASES.get(raw) or (raw if raw in _EXTRA_COMPONENTS else None)
    if stat not in (STAT_COMPONENTS if prop else _COMPONENTS):
        raise _Missing('Name a supported statistic, such as points, rebounds, assists, PRA, FG attempts or minutes.')
    return stat


def _team(value):
    if type(value) is not str:
        raise _Missing('Name one NBA team or its abbreviation.')
    key = normalized_name(value)
    direct = canonical_team(value)
    matches = {code for code, aliases in TEAM_NAMES.items()
               if key in {normalized_name(code), *(normalized_name(alias) for alias in aliases),
                          normalized_name(aliases[0] + ' ' + aliases[1])}}
    if direct in TEAM_NAMES:
        matches.add(direct)
    if len(matches) != 1:
        raise _Missing('Name one unambiguous NBA team, such as NYK or Cleveland Cavaliers.')
    return matches.pop()


def _player(master, value):
    if type(value) is not str or not normalized_name(value):
        raise _Missing('Which player do you mean? Use a player name from the saved master.')
    from career_identity import resolve_career_identity
    # Existing basketball nicknames are useful only if the full saved identity
    # below also resolves; an alias never supplies roster or career evidence.
    value = {'kat': 'Karl-Anthony Towns', 'deuce': 'Miles McBride'}.get(value.casefold().strip(), value)
    identity = resolve_career_identity(master, value)
    if identity.get('ambiguous'):
        raise _Missing('That player identity is ambiguous in the saved master; use the full player name.')
    with _open(master) as db:
        rows = [dict(row) for row in db.execute('SELECT DISTINCT player_id,player FROM player_games '
                "WHERE player IS NOT NULL AND TRIM(player)<>''")]
    approved = {normalized_name(alias) for alias in identity['aliases']}
    full = [row for row in rows if normalized_name(row['player']) in approved]
    if not full:
        # A surname is accepted only if it resolves to one stored full name.
        wanted = normalized_name(value)
        full = [row for row in rows if wanted == normalized_name(str(row['player']).split()[-1])]
    names = {normalized_name(row['player']) for row in full}
    if not full or len(names) > 1 and not identity.get('identity_source'):
        raise _Missing('No unique saved player matches that name; provide the full player name.')
    known_ids = {canonical_player(row['player']) for row in full}
    external_ids = {row['player_id'] for row in full} - known_ids
    if len(external_ids) > 1 and not identity.get('identity_source'):
        raise _Missing('That full name has conflicting saved player identities; a verified identity mapping is required.')
    name = next((row['player'] for row in full
                 if normalized_name(row['player']) == normalized_name(identity['player'])), full[0]['player'])
    return name, {row['player_id'] for row in full}, {normalized_name(row['player']) for row in full}


def _value(row, stat):
    values = [row.get(component) for component in _COMPONENTS[stat]]
    return sum(values) if all(type(value) in (int, float) and math.isfinite(value) for value in values) else None


def _numbers(values):
    observed = [value for value in values if value is not None]
    return {'observed': len(observed), 'missing': len(values) - len(observed),
            'mean': statistics.mean(observed) if observed else None,
            'median': statistics.median(observed) if observed else None,
            'minimum': min(observed) if observed else None,
            'maximum': max(observed) if observed else None,
            'total': sum(observed) if observed else None}


def _sources(rows):
    return sorted({str(row.get('source')).strip() for row in rows if row.get('source')})


def _card(entity, text, facts, *, sources=(), scope='', through=None, kind='saved_observations', card_id='R1'):
    return {'id': card_id, 'kind': kind, 'entity': entity, 'text': text,
            'source': sources[0] if sources else 'Saved master; original source not supplied',
            'source_urls': list(sources), 'scope': scope, 'data_through_date': through,
            'approved_facts': facts}


def _fact(entity, metric, kind, text, values=(), scope=''):
    return {'entity': entity, 'metric': metric, 'fact_type': kind,
            'text': text, 'values': list(values), 'scope': scope}


def _answer_parts(parts, qualifier='', limit=1000):
    """Fit complete application-authored facts while reserving source limits."""
    qualifier = str(qualifier).strip()
    if len(qualifier) >= limit:
        return ('The requested saved scope is too broad for a compact summary. '
                'Its dated records remain in the evidence; complete coverage and current availability are unverified.')
    selected = []
    size = len(qualifier) + 1
    for part in parts:
        part = str(part or '').strip()
        if part and size + len(part) + 1 <= limit:
            selected.append(part)
            size += len(part) + 1
    return '\n'.join([*selected, *([qualifier] if qualifier else [])])


def _player_rows(master, args, now, *, stats):
    player, ids, names = _player(master, args.get('player'))
    window = _window(args.get('window', 5), career=True)
    phase = _PHASES.get(str(args.get('phase', 'Regular Season')).strip().casefold())
    if not phase:
        raise _Missing('Choose Regular Season, Playoffs or Preseason; phases cannot be silently combined.')
    season = args.get('season')
    if season is not None and (type(season) is not str or not re.fullmatch(r'(?:19|20)\d{2}-\d{2}', season)
            or (int(season[:4]) + 1) % 100 != int(season[-2:])):
        raise _Missing('Use a season such as 2026-27.')
    opponent = _team(args['opponent']) if args.get('opponent') is not None else None
    today = now.astimezone(EASTERN).date().isoformat()
    where = ['p.player_id IN (' + ','.join('?' for _ in ids) + ')',
             "lower(g.status)='final'", "lower(p.appearance_state)='played'", '(p.minutes IS NULL OR p.minutes>0)',
             'g.date=p.date', 'substr(p.date,1,10)<=?', 'lower(p.season_type)=lower(?)']
    params = [*sorted(ids), today, phase]
    if season:
        where.append('p.season=?'); params.append(season)
    if opponent:
        where.append('p.opponent=?'); params.append(opponent)
    with _open(master) as db:
        rows = [dict(row) for row in db.execute('SELECT p.* FROM player_games p JOIN games g '
                'ON g.id=p.game_id WHERE ' + ' AND '.join(where) + ' ORDER BY p.date DESC,p.game_id DESC', params)
                if normalized_name(row['player']) in names]
    # Duplicate IDs do not get two votes. Conflicting versions are not guessed.
    groups = {}
    for row in rows:
        groups.setdefault(row['game_id'], []).append(row)
    selected, conflicts = [], 0
    for group in groups.values():
        fields = ('date', 'team', 'opponent', 'season', 'season_type', 'minutes',
                  *(component for stat in stats for component in _COMPONENTS[stat]))
        if len({tuple(row.get(field) for field in fields) for row in group}) > 1:
            conflicts += 1
            continue
        selected.append(group[0])
    coverage = {'total_matching_saved_games': len(selected),
                'matching_first_date': min((str(row['date'])[:10] for row in selected), default=None),
                'matching_last_date': max((str(row['date'])[:10] for row in selected), default=None),
                'sources': _sources(selected)}
    rows = selected if window == 'career' else selected[:window]
    canonical = {'player': player, 'window': window, 'phase': phase}
    if season:
        canonical['season'] = season
    if opponent:
        canonical['opponent'] = opponent
    return rows, canonical, conflicts, coverage


def _scope(rows, args):
    seasons = sorted({str(row.get('season') or 'unknown season') for row in rows})
    teams = sorted({str(row.get('team') or 'unknown team') for row in rows})
    through = max((str(row['date'])[:10] for row in rows), default=None)
    scope = (f"Final games; Played; {args['phase']}; " + (', '.join(seasons) if seasons else 'no observations')
             + '; stored teams ' + ', '.join(teams)
             + ('; against ' + args['opponent'] if args.get('opponent') else '; all stored opponents')
             + (f'; through {through}' if through else '')
             + '. Complete game inventory is unverified; zero-minute and DNP rows are excluded. Stored teams do not confirm a current roster.')
    return scope, through


def _player_games(master, args, now):
    stat = _stat(args.get('stat'))
    rows, canonical, conflicts, coverage = _player_rows(master, args, now, stats=[stat])
    canonical['stat'] = stat
    if not rows:
        text = (f"{canonical['player']}: 0 matching stored final Played {canonical['phase']} games were found in this scope; "
                'this is missing saved coverage, not proof the player never played and not a zero statistic.')
        scope, through = _scope([], canonical)
        fact = _fact(canonical['player'], 'games', 'sample_counts', text, [0], scope)
        return {'args': canonical, 'available': False, 'answer': _answer_parts([text], scope),
                'cards': [_card(canonical['player'], text, [fact], scope=scope)], 'sources': [],
                'scope': scope, 'missing': ['No matching saved final Played observations are available.'],
                'data': {'games': [], 'summary': _numbers([]), 'game_count': 0,
                         'total_matching_saved_games': 0, 'matching_first_date': None, 'matching_last_date': None,
                         'conflicting_games_excluded': conflicts, 'complete_coverage_verified': False}}
    scope, through = _scope(rows, canonical)
    player, label = canonical['player'], _LABELS[stat]
    values = [_value(row, stat) for row in rows]
    summary = _numbers(values)
    description = ('All stored history' if canonical['window'] == 'career' else f"Latest {len(rows)} saved games")
    if summary['observed']:
        result = (f"{player}: {description}, {label} mean {summary['mean']:g}, median {summary['median']:g}, "
                  f"range {summary['minimum']:g}–{summary['maximum']:g}.")
    else:
        result = f'{player}: {description}; {label} values are missing.'
    counts = (f"{summary['observed']} of {len(rows)} stored games have observed {label}; "
              f"{summary['missing']} missing values excluded, never zero-filled.")
    coverage_text = (f"{player}: the master holds {coverage['total_matching_saved_games']} matching stored games played"
                     + (f" against {canonical['opponent']}" if canonical.get('opponent') else '')
                     + f", from {coverage['matching_first_date']} through {coverage['matching_last_date']}. "
                     'This counts saved final Played games in the requested phase/season/opponent scope, not verified complete career history.')
    dates = f"Sample {min(str(row['date'])[:10] for row in rows)} through {through}."
    text = ' '.join((result, counts, dates, coverage_text, scope))
    facts = [_fact(player, label, 'average', result,
                   [value for key, value in summary.items() if key in {'mean', 'median', 'minimum', 'maximum'} and value is not None], scope),
             _fact(player, label, 'sample_counts', counts, [summary['observed'], len(rows), summary['missing']], scope),
             _fact(player, label, 'source_date', dates + ' Complete career coverage is not established.', scope=scope)]
    facts.append(_fact(player, 'games', 'sample_counts', coverage_text,
                       [coverage['total_matching_saved_games']],
                       f"All matching stored {canonical['phase']} games; "
                       + (f"season {canonical['season']}; " if canonical.get('season') else 'all stored seasons; ')
                       + (f"against {canonical['opponent']}; " if canonical.get('opponent') else 'all stored opponents; ')
                       + 'a stored count, not verified completeness or the number of observed metric values.'))
    if summary['observed']:
        facts.extend([_fact(player, label, 'median', f"{player}: median {summary['median']:g} {label}.", [summary['median']], scope),
                      _fact(player, label, 'total', f"{player}: {summary['total']:g} {label} total in these {summary['observed']} observed saved games.",
                            [summary['total'], summary['observed']], scope),
                      _fact(player, label, 'dispersion', f"{player}: observed {label} range {summary['minimum']:g} to {summary['maximum']:g}.",
                            [summary['minimum'], summary['maximum']], scope)])
    games = [{'date': str(row['date'])[:10], 'team': row['team'], 'opponent': row.get('opponent'),
              'season': row.get('season'), 'value': value, 'source': row.get('source')}
             for row, value in zip(rows, values)]
    log_text = f'{player}: dated {label}, newest first: ' + '; '.join(
        f"{row['date']} {row['team']} vs {row['opponent']}: {row['value']:g}" if row['value'] is not None else
        f"{row['date']} {row['team']} vs {row['opponent']}: missing" for row in games[:6]) + '.'
    log_facts = [_fact(player, label, 'recent_values', log_text,
                       [row['value'] for row in games[:6] if row['value'] is not None], scope)]
    answer = _answer_parts([result, coverage_text, counts, dates,
                           log_text if canonical['window'] != 'career' and len(rows) <= 5 else ''], scope)
    missing = ([f'{summary["missing"]} selected game values are missing.'] if summary['missing'] else [])
    if conflicts:
        missing.append(f'{conflicts} conflicting duplicate game observations were excluded.')
    if any(not row.get('source') for row in rows):
        missing.append('Some selected observations have no recorded original source.')
    return {'args': canonical, 'available': bool(summary['observed']), 'answer': answer,
            'cards': [_card(player, text, facts, sources=coverage['sources'], scope=scope, through=through),
                      _card(player, log_text, log_facts, sources=_sources(rows), scope=scope, through=through, card_id='R2')],
            'sources': coverage['sources'], 'scope': scope, 'missing': missing,
            'data': {'games': games, 'summary': summary, 'game_count': len(rows),
                     'total_matching_saved_games': coverage['total_matching_saved_games'],
                     'matching_first_date': coverage['matching_first_date'], 'matching_last_date': coverage['matching_last_date'],
                     'conflicting_games_excluded': conflicts, 'complete_coverage_verified': False}}


def _profile(master, args, now):
    stats = ('pts', 'reb', 'ast', 'minutes', 'fga', 'tpa', 'tpm')
    rows, canonical, conflicts, coverage = _player_rows(master, {**args, 'window': _window(args.get('window', 20))}, now, stats=stats)
    if not rows:
        raise _Missing('No completed Regular Season games played by this player are saved.')
    scope, through = _scope(rows, canonical)
    player = canonical['player']
    summaries = {stat: _numbers([_value(row, stat) for row in rows]) for stat in stats}
    parts, facts = [], []
    for stat, summary in summaries.items():
        if not summary['observed']:
            parts.append(_LABELS[stat] + ' unavailable')
            continue
        text = (f"{player}: {summary['mean']:g} {_LABELS[stat]} per game "
                f"({summary['observed']}/{len(rows)} observed; missing values excluded).")
        parts.append(text)
        facts.append(_fact(player, _LABELS[stat], 'average',
                           f"{player}: mean {summary['mean']:g} {_LABELS[stat]} per game in this saved sample.",
                           [summary['mean']], scope))
        facts.append(_fact(player, _LABELS[stat], 'sample_counts',
                           f"{player}: {summary['observed']} of {len(rows)} stored games have observed {_LABELS[stat]}; "
                           f"{summary['missing']} values are missing.", [summary['observed'], len(rows), summary['missing']], scope))
    text = ' '.join(parts) + ' ' + scope
    missing = [f'{_LABELS[stat]}: {summary["missing"]} missing values.' for stat, summary in summaries.items() if summary['missing']]
    if conflicts:
        missing.append(f'{conflicts} conflicting duplicate games excluded.')
    return {'args': {'player': player, 'window': canonical['window']}, 'available': True,
            'answer': _answer_parts(parts, scope), 'cards': [_card(player, text, facts, sources=_sources(rows), scope=scope, through=through)],
            'sources': _sources(rows), 'scope': scope, 'missing': missing,
            'data': {'summary': summaries, 'game_count': len(rows), 'total_matching_saved_games': coverage['total_matching_saved_games'],
                     'matching_first_date': coverage['matching_first_date'], 'matching_last_date': coverage['matching_last_date'],
                     'complete_coverage_verified': False}}


def _team_games(master, args, now):
    team, window = _team(args.get('team')), _window(args.get('window', 5))
    with _open(master) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM games WHERE lower(status)='final' "
            'AND date<=? AND (home_team=? OR away_team=?) ORDER BY date DESC,id DESC LIMIT ?',
            (now.astimezone(EASTERN).date().isoformat(), team, team, window))]
    if not rows:
        raise _Missing(f'No final saved team games are available for {team}.')
    games = []
    for row in rows:
        home = row['home_team'] == team
        pf, pa = (row.get('home_score'), row.get('away_score')) if home else (row.get('away_score'), row.get('home_score'))
        valid = all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in (pf, pa))
        games.append({'date': row['date'], 'opponent': row['away_team'] if home else row['home_team'],
                      'venue': 'home' if home else 'away', 'points_for': pf if valid else None,
                      'points_against': pa if valid else None, 'source': row.get('source')})
    observed = [row for row in games if row['points_for'] is not None]
    through = max(row['date'] for row in rows)
    scope = f'Latest {len(rows)} saved final {team} games through {through}; all stored seasons and opponents. Complete inventory is unverified.'
    wins = sum(row['points_for'] > row['points_against'] for row in observed)
    losses = sum(row['points_for'] < row['points_against'] for row in observed)
    summary = {'wins': wins, 'losses': losses, 'observed': len(observed), 'missing': len(games) - len(observed),
               'points_for_average': statistics.mean(row['points_for'] for row in observed) if observed else None,
               'points_against_average': statistics.mean(row['points_against'] for row in observed) if observed else None}
    text = f"{team}: {wins} wins, {losses} losses in {len(observed)} stored games with observed final scores."
    facts = [_fact(team, 'games', 'counts', text, [wins, losses, len(observed)], scope)]
    if observed:
        scoring = (f"{team}: {summary['points_for_average']:g} points scored and "
                   f"{summary['points_against_average']:g} allowed per game in this saved sample.")
        text += ' ' + scoring
        facts.append(_fact(team, 'points', 'average',
                           f"{team}: averaged {summary['points_for_average']:g} points scored per game in this saved sample.",
                           [summary['points_for_average']], scope))
        facts.append(_fact(team, 'points', 'average',
                           f"{team}: allowed an average of {summary['points_against_average']:g} points per game in this saved sample.",
                           [summary['points_against_average']], scope))
    text += ' ' + scope
    return {'args': {'team': team, 'window': window}, 'available': bool(observed),
            'answer': _answer_parts([fact['text'] for fact in facts], scope),
            'cards': [_card(team, text, facts, sources=_sources(rows), scope=scope, through=through)],
            'sources': _sources(rows), 'scope': scope,
            'missing': [f'{summary["missing"]} final scores are missing.'] if summary['missing'] else [],
            'data': {'games': games, 'summary': summary, 'complete_coverage_verified': False}}


def _next_game(master, args, now):
    from upcoming_game import build_preview, preview_evidence
    team = _team(args.get('team', 'NYK'))
    if args.get('date') is not None and args.get('after_date') is not None:
        raise _Missing('Use either date for an inclusive lookup or after_date for a strictly later game, not both.')
    after = _date(args['after_date'], now) if args.get('after_date') is not None else None
    day = _date(args['date'], now) if args.get('date') is not None else None
    if after:
        try:
            day = (dt.date.fromisoformat(after) + dt.timedelta(days=1)).isoformat()
        except OverflowError:
            raise _Missing('The after_date must leave a later calendar day within the supported date range.') from None
    anchor = now
    if day and day != now.astimezone(EASTERN).date().isoformat():
        anchor = dt.datetime.combine(dt.date.fromisoformat(day), dt.time.min, EASTERN)
    result = build_preview(master, team=team, now=anchor, resolved_intent=True)
    game = (result or {}).get('next_game_focus')
    answer = (result or {}).get('answer_header') or (result or {}).get('answer') or 'No saved upcoming game matches this scope.'
    if after:
        answer = f'Saved calendar lookup strictly after {after} (starting {day}). ' + answer
    elif day and anchor != now:
        answer = f'Saved calendar lookup on/after {day}. ' + answer
    scope = ('Saved schedule; calendar presence is not independently verified live timing. '
             + ('Strictly after ' + after + '; first eligible date ' + day + '.' if after else
                'Lookup anchor: ' + day + '.' if day else 'Upcoming relative to the current Eastern clock.'))
    # Separate entity cards are required by grounded_reply's entity validator;
    # combining the teams would allow one team's numbers to support the other.
    cards = [card for card in preview_evidence(result or {}) if card.get('kind') == 'upcoming_team_form'][:2]
    if game:
        # The app renders this source-bound header; synthesis must retain its qualifier.
        facts = [_fact(team, 'schedule', 'context', answer, scope=scope)]
        cards.insert(0, _card(team, answer, facts, sources=[row['source'] for row in result.get('sources', [])
                           if row.get('source')], scope=scope, kind='saved_schedule', card_id='R1'))
    return {'args': {'team': team, **({'after_date': after} if after else {'date': day} if day else {})}, 'available': bool(game),
            'answer': _answer_parts(answer.split('\n'), scope), 'cards': cards[:3], 'sources': (result or {}).get('sources', []),
            'scope': scope, 'missing': [] if game else ['No saved upcoming game matches the selected team and date.'],
            'data': result or {}}


def _availability(master, args, now):
    from board_shortlist import player_availability
    player, _, _ = _player(master, args.get('player'))
    day = _date(args.get('date'), now)
    with _open(master) as db:
        news = [dict(row) for row in db.execute('SELECT * FROM news ORDER BY COALESCE(published_at,updated_at) DESC LIMIT 100')]
    result = player_availability(news, player, day, now, store=master)
    official = result.get('official_designation') or {}
    scope = f'Game date {day}; dated saved official reports and collected news as of {now.isoformat()}; no schedule check performed.'
    if official.get('known'):
        text = (f"{player}: official designation {official['status']} for {day}, reported {official['report_at']}. "
                'This designation does not establish a starting role or unrestricted minutes.')
        missing = []
    else:
        text = f'{player}: no fresh, exact-time official designation is saved for {day}. Status is unknown; absence from reports is not clearance.'
        missing = [official.get('reason') or 'Fresh game-specific official designation is missing.']
    sources = []
    facts = [_fact(player, 'availability', 'context', text, scope=scope)]
    if official.get('source_url'):
        sources.append({'source': official['source_url'], 'report_at': official['report_at'], 'authority': 'official'})
    reports = result.get('reports') or []
    for report in [row for row in reports if not str(row.get('state', '')).startswith('official_')][:2]:
        report_text = (f"Saved publisher report: {report.get('title') or 'untitled'} "
                       f"({report.get('source') or 'unknown publisher'}; report time {report.get('published_at') or 'unknown'}).")
        text += ' ' + report_text
        facts.append(_fact(player, 'availability', 'context', report_text, scope=scope))
        sources.append({'source': report.get('url') or report.get('source'),
                        'report_at': report.get('published_at'), 'authority': 'publisher'})
    if result.get('official_superseded_or_limited'):
        caution = 'A later or unresolved publisher report conflicts with or limits this official designation.'
        text += ' ' + caution
        facts.insert(1, _fact(player, 'availability', 'context', caution, scope=scope))
    return {'args': {'player': player, 'date': day}, 'available': True,
            'answer': _answer_parts([fact['text'] for fact in facts], scope),
            'cards': [_card(player, text, facts, sources=[row['source'] for row in sources if row.get('source')],
                            scope=scope, kind='availability')],
            'sources': sources, 'scope': scope, 'missing': missing + ['Starting role and expected minutes are unverified.'],
            'data': result}


def _schedule(master, args, now):
    from feeds import canonical_game
    from season_calendar import current_season
    team = _team(args.get('team', 'NYK'))
    if team != 'NYK':
        raise _Missing('The saved season fixture calendar covers the Knicks. Other teams have saved game statistics, not a complete season calendar.')
    start = _date(args['start_date'], now) if args.get('start_date') is not None else None
    end = _date(args['end_date'], now) if args.get('end_date') is not None else None
    if start and end and start > end:
        raise _Missing('The schedule start date must be on or before the end date.')
    limit = args.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise _Missing('Use a schedule result limit from 1 to 20.')
    season = current_season(now)
    where, params = ['season=?', '(home_team=? OR away_team=?)'], [season, team, team]
    if start:
        where.append('date>=?'); params.append(start)
    if end:
        where.append('date<=?'); params.append(end)
    with _open(master) as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE '
                + ' AND '.join(where) + ' ORDER BY date,id', params)]
    rows = [row for row in rows if row['home_team'] in TEAM_NAMES and row['away_team'] in TEAM_NAMES
            and row['id'] == canonical_game(row['date'], row['home_team'], row['away_team'])]
    canonical = {'team': team, 'limit': limit, **({'start_date': start} if start else {}), **({'end_date': end} if end else {})}
    if not rows:
        raise _Missing(f'No saved Knicks {season} season fixtures match that date range; no broader dates were substituted.')
    selected = rows[:limit]
    scope = (f'Saved Knicks {season} season fixtures, ordered by Eastern game date. '
             'Source calendar status and tipoff times are dated publication values; current timing and results are not established. '
             'Returned fixtures do not independently establish a complete season inventory.')
    lines = []
    for row in selected:
        clock = 'tipoff unknown'
        if row.get('start_time'):
            try:
                stamp = dt.datetime.fromisoformat(str(row['start_time']).replace('Z', '+00:00'))
                if stamp.tzinfo is not None and stamp.astimezone(EASTERN).date().isoformat() == row['date']:
                    clock = stamp.astimezone(EASTERN).strftime('%I:%M %p').lstrip('0') + ' Eastern'
            except ValueError:
                pass
        lines.append(f"{row['date']}: {row['away_team']} at {row['home_team']}, {clock} ({row.get('status') or 'status unknown'}).")
    # Return precisely the rows that fit the compact factual response. The
    # continuation starts at the next undisplayed row, never after hidden rows.
    visible = []
    used = 240  # bounded heading, source qualification and continuation
    for line in lines:
        if used + len(line) + 1 > 1000:
            break
        visible.append(line)
        used += len(line) + 1
    selected = selected[:len(visible)]
    lines = visible
    prefix = f'Knicks {season} saved schedule — {len(selected)} of {len(rows)} matching fixtures. '
    suffix = 'Saved calendar; current timing unverified. '
    if len(rows) > len(selected):
        suffix += f"More fixtures begin {rows[len(selected)]['date']}; request that start_date to continue."
    answer = prefix + '\n' + '\n'.join(lines) + '\n' + suffix
    facts = [_fact(team, 'schedule', 'context', line + ' Saved calendar timing; not a verified live start time.', scope=scope)
             for line in lines]
    source_dates = sorted({str(row.get('source_asof') or 'unknown') for row in selected})
    proof = 'Calendar source dates: ' + ', '.join(source_dates) + '. ' + scope
    facts.append(_fact(team, 'coverage', 'context', proof, scope=scope))
    sources = [{'source': row.get('source') or 'saved season calendar', 'source_asof': row.get('source_asof')}
               for row in selected]
    return {'args': canonical, 'available': True, 'answer': answer, 'sources': sources, 'scope': scope,
            'cards': [_card(team, prefix + ' '.join(lines) + ' ' + proof, facts,
                            sources=_sources(selected), scope=scope, kind='saved_schedule')],
            'missing': ['Independent current timing and complete season inventory are not established by this lookup.'],
            'data': {'season': season, 'fixtures': selected, 'matched_rows': len(rows), 'returned_rows': len(selected),
                     'truncated': len(rows) > len(selected), 'next_start_date': rows[len(selected)]['date'] if len(rows) > len(selected) else None,
                     'timing_verified': False}}


def _prop(master, args, now):
    from pick_discussion import build_pick_discussion
    player, _, _ = _player(master, args.get('player'))
    stat = _stat(args.get('stat'), prop=True)
    line = args.get('line')
    if type(line) not in (int, float) or not math.isfinite(line) or line < 0 or line > 10000:
        raise _Missing('What is the numeric offered line for this statistic?')
    day = _date(args.get('date'), now)
    side = args.get('side')
    if side is not None:
        side = {'over': 'more', 'under': 'less', 'up': 'more', 'down': 'less'}.get(str(side).casefold(), str(side).casefold())
        if side not in {'more', 'less'}:
            raise _Missing('The requested side must be More or Less.')
    canonical = {'player': player, 'stat': stat, 'line': line, 'date': day}
    if side:
        canonical['side'] = side
    report = build_pick_discussion(master, canonical, now=now)
    decision = report.get('decision', 'pass')
    reason = report.get('reason') or 'The required assessment evidence is unavailable.'
    header = f'{player} {line:g} {_LABELS[stat]} ({day}) — {decision.title()}.'
    baseline_text = ''
    summary = header + ' ' + reason
    baseline = report.get('baseline') or {}
    if baseline.get('available'):
        baseline_text = (f"Saved {baseline['n']}-game same-team baseline: mean {baseline['reference_projection']:g}, "
                         f"median {baseline['baseline_median']:g}, through {baseline['last_date']}. This is not a win probability.")
        summary += ' ' + baseline_text
    scope = f'Existing strict prop assessment for {day}; user-supplied line, not a verified live offer. Decision is authoritative.'
    cards = [card for card in report.get('evidence_cards', []) if card.get('kind') in {'prop_baseline', 'prop_sample'}][:1]
    cards.append(_card(player, summary, [], scope=scope, kind='prop_assessment', card_id='R2'))
    cards[-1]['decision'] = decision
    return {'args': canonical, 'available': bool(report.get('available')),
            'answer': _answer_parts([header, reason, baseline_text], scope),
            'cards': cards[:2], 'sources': report.get('sources', []), 'scope': scope,
            'missing': list(report.get('missing') or []), 'decision': decision, 'data': report}


def _research(master, args, now):
    from current_research import current_research_card, ENTITY_FILTERS, NOTE
    table, filters = args.get('table'), dict(args.get('filters') or {})
    if table not in TABLE_FILTERS:
        raise _Missing('Choose one supported saved research table: ' + ', '.join(TABLE_FILTERS) + '.')
    if set(filters) - TABLE_FILTERS[table]:
        raise _Missing('Supported filters for ' + table + ': ' + ', '.join(sorted(TABLE_FILTERS[table])) + '.')
    player_filters = {'player', 'other_player', 'named_player', 'offense_player', 'defense_player', 'selected_player', 'named_matchup_player'}
    for key, value in list(filters.items()):
        if value is None:
            continue
        if key in player_filters:
            filters[key] = _player(master, value)[0]
        elif key == 'entity':
            try:
                filters[key] = _team(value)
            except _Missing:
                filters[key] = _player(master, value)[0]
        elif key in {'team', 'opponent', 'opponent_team'}:
            filters[key] = _team(value)
        elif key == 'date':
            filters[key] = _date(value, now)
    limit = args.get('limit', 3)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise _Missing('Use a research result limit from 1 to 10.')
    if table != 'Refresh_Coverage' and not ENTITY_FILTERS.intersection(filters):
        raise _Missing('Choose a specific player/entity, team or existing assignment before reading ' + table + '.')
    records = _research_records(master, table, filters, now)
    matches = [row for row in records if _matches(row, filters)]
    selected = []
    for row in matches[:limit]:
        if table == 'Matchup_Assignments':
            from assignment_refresh import refresh_caution
            original = row
            row = {k: v for k, v in row.items() if k not in {'candidate_checks', 'historical_measured_candidates'}}
            row['recheck_caution'] = refresh_caution(original)
        selected.append(row)
    result = {'table': table, 'filters': filters, 'rows': selected, 'matched_rows': len(matches),
              'returned_rows': len(selected), 'truncated': len(matches) > limit, 'read_at': now.isoformat(),
              'note': NOTE, 'source': 'Regenerated saved-master research; same definitions as NBA_Master.xlsx',
              'available_filters': sorted(TABLE_FILTERS[table]),
              'no_matches_note': ('No saved existing definition/evidence matched these exact filters; '
                                  'no broader season, player or metric was substituted.') if not matches else None}
    card = current_research_card(result, 1)
    text = _research_answer(result)
    # Raw derivative records are context, not new model-approved measurements;
    # the existing current_research card retains every date and missing caveat.
    return {'args': {'table': table, 'filters': filters, 'limit': limit},
            'available': bool(result['rows']), 'answer': text, 'cards': [card] if card else [],
            'sources': [result['source']], 'scope': result['note'],
            'missing': [] if result['rows'] else [result['no_matches_note']], 'data': result}


def _matches(row, filters):
    return all(key in row and (row[key] is None if value is None else
               str(row[key]).strip().casefold() == str(value).strip().casefold()) for key, value in filters.items())


def _evidence(db, kind):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_evidence'").fetchone()
    return [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM workbook_evidence WHERE kind=?', (kind,))] if exists else []


def _research_players(master, names, now):
    """Read only participants needed by the selected existing definitions."""
    if not names:
        return {}
    today = now.astimezone(EASTERN).date().isoformat()
    with _open(master) as db:
        rows = [dict(row) for row in db.execute('SELECT p.*,g.home_team,g.away_team FROM player_games p '
                'JOIN games g ON g.id=p.game_id WHERE p.player IN (' + ','.join('?' for _ in names) + ') '
                "AND g.status='final' AND lower(p.appearance_state)='played' AND p.date<=?",
                (*sorted(names), today))]
    players = defaultdict(list)
    for row in rows:
        row['venue'] = ('Home' if row.get('team') == row.get('home_team') else
                        'Away' if row.get('team') == row.get('away_team') else None)
        players[row['player']].append(row)
    return players


def _research_records(master, table, filters, now):
    """Same export helpers, only the requested table and its participants.

    Regenerating the complete multi-season workbook for every question scanned
    the full career dataset and calculated every original definition. Filtering
    definitions before computation preserves their populations while avoiding
    that unrelated work. No cache can turn an old source date into freshness.
    """
    import refresh_export as export
    today = now.astimezone(EASTERN).date().isoformat()
    if table == 'Player_Metrics':
        return export.player_metrics(_research_players(master, {filters['player']} if filters.get('player') else set(), now))
    if table == 'Tracking_Metrics':
        with _open(master) as db:
            records = [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM refreshed_tracking WHERE entity=?',
                                                              (filters.get('entity'),))]
        return export.tracking_metrics(records)
    if table == 'Measured_Pair_Metrics':
        from measured_history import measured_records
        # Existing overlap/conflict resolution remains authoritative. It scans
        # the measured archive only, not all game/derivative/export tables.
        rows = [row for row in measured_records(master) if str(row.get('date') or '')[:10] <= today
                and row.get('date') and all(row.get(key) == filters[key]
                    for key in ('offense_player', 'defense_player', 'season', 'season_type') if filters.get(key))]
        return export.measured_pair_metrics(export._unique_measured(rows))
    if table == 'Matchup_Assignments':
        from assignment_refresh import assignment_rows_for
        return assignment_rows_for(master, now=now)
    if table in {'Team_Schedule', 'Calculated_Splits', 'Correlations', 'Pair_Summaries'}:
        kinds = {'Calculated_Splits': 'splits', 'Correlations': 'correlations', 'Pair_Summaries': 'pair_summaries'}
        with _open(master) as db:
            definitions = [row for row in _evidence(db, kinds[table]) if _matches(row, filters)] if table in kinds else []
            rest = _evidence(db, 'rest_schedule') if table in {'Team_Schedule', 'Calculated_Splits'} else []
            assignments = _evidence(db, 'assignments') if table == 'Pair_Summaries' else []
        names = {row[key] for row in definitions for key in ('player', 'other_player', 'named_player') if row.get(key)}
        players = _research_players(master, names, now)
        rest_lookup = {(row.get('team'), str(row.get('date') or '')[:10]): row for row in rest}
        # Rest calculation is needed only for actual rest queries; baseline,
        # venue and opponent splits do not need the whole saved rest calendar.
        if table == 'Team_Schedule' or any(row.get('definition') in {'Rest', 'A: Opponent team by Rest'} for row in definitions):
            from season_calendar import refreshed_rest_rows
            for row in refreshed_rest_rows(master, now=now):
                rest_lookup[(row.get('team'), str(row.get('date') or '')[:10])] = row
        if table == 'Team_Schedule':
            return list(rest_lookup.values())
        if table == 'Calculated_Splits':
            return export.calculated_splits(definitions, players, rest_lookup)
        if table == 'Correlations':
            return export.correlations(definitions, players)
        return export.pair_summaries(definitions, players, assignments)
    if table == 'Refresh_Coverage':
        return _refresh_coverage(master, now)
    raise _Missing('This saved research table is unavailable.')


def _refresh_coverage(master, now):
    """Read the export coverage inventory without deriving every statistic."""
    from assignment_refresh import assignment_rows_for
    from season_schedule_refresh import schedule_coverage_rows
    with _open(master) as db:
        def checkpoint(name, default):
            row = db.execute('SELECT value_json FROM checkpoints WHERE name=?', (name,)).fetchone()
            return json.loads(row[0]) if row else default
        inventory = checkpoint('workbook_sheet_inventory', [])
        tracking = checkpoint('tracking_refresh_status', {})
        refresh = checkpoint('nightly_nba_refresh', {})
        players = {row[0] for row in db.execute("SELECT DISTINCT p.player FROM player_games p JOIN games g ON g.id=p.game_id "
                   "WHERE g.status='final' AND lower(p.appearance_state)='played' AND p.date<=?",
                   (now.astimezone(EASTERN).date().isoformat(),))}
        latest = db.execute("SELECT MAX(p.date) FROM player_games p JOIN games g ON g.id=p.game_id "
                   "WHERE g.status='final' AND lower(p.appearance_state)='played' AND p.date<=?",
                   (now.astimezone(EASTERN).date().isoformat(),)).fetchone()[0]
    refreshed = {'Player_Games': 'player_games; Player_Metrics', 'Season_Totals': 'Player_Metrics; season_averages; Tracking_Metrics',
                 'Calculated_Splits': 'Calculated_Splits', 'Correlations': 'Correlations', 'Pair_Summaries': 'Pair_Summaries',
                 'Pair_Tracking': 'Measured_Pair_Metrics; Tracking_Metrics', 'Matchup_Assignments': 'Matchup_Assignments',
                 'Team_Schedule': 'Team_Schedule; season_schedule; Original_Evidence'}
    coverage = []
    for sheet in inventory:
        name = sheet['sheet']; target = refreshed.get(name)
        profile = name in players or name.startswith('vs ') or name == 'Overview'
        generated = name in players or name.startswith('vs ') or name == 'Overview'
        coverage.append({'original_sheet': name, 'original_rows': sheet.get('rows'),
            'current_export_tables': name if profile else target,
            'status': 'current_derivatives_and_dated_sources' if target or profile else 'original_reference_snapshot',
            'original_sheet_rewritten': False, 'readable_current_view_generated': bool(generated and profile),
            'note': 'Original workbook remains preserved. Current readable views use saved derivatives and their per-row coverage, not copied original formatting. Missing sources retain original dates.',
            'export_generated_at': now.isoformat()})
    assignments = assignment_rows_for(master, now=now)
    coverage.extend([
        {'group': 'Game-derived statistics', 'status': 'recomputed_from_saved_observations', 'latest_observed_game': latest,
         'target_game_date': refresh.get('target_game_date'), 'source_data_through_date': refresh.get('source_data_through_date'),
         'note': 'Every statistic uses its own observed population/date. Latest game is not proof of complete history.'},
        {'group': 'Tentative assignments', 'status': 'revalidated_dated_evidence' if any(row.get('last_checked') for row in assignments) else 'original_snapshot',
         'note': 'Last checked is separate from assignment_asof. Revalidation does not confirm a defender or invent a replacement.'},
        {'group': 'Rest-filtered statistics', 'status': 'verified_rest_rows_only', 'note': 'Missing team schedule/rest evidence is excluded; player appearance gaps do not establish team rest.'},
        {'group': 'Original 53-sheet presentation', 'status': 'preserved_reference', 'note': 'Current exports regenerate supported tables; original formatted player/opponent sheets remain the original dated file.'}])
    coverage.extend({'group': 'Tracking: ' + str(item.get('category')), **item,
                     'note': 'Source publication time, covered-game date and export time are separate.'}
                    for item in tracking.get('coverage', []))
    coverage.extend({'group': 'Season schedule: ' + str(item.get('team')), **item,
                     'note': 'Published season fixtures do not verify live timing. Unassigned games and missing source tipoff times remain explicit.'}
                    for item in schedule_coverage_rows(master))
    return coverage


def _research_answer(result):
    if not result['rows']:
        return str(result['no_matches_note'])
    # Whole selected field values, including source-clock and missing qualifiers;
    # never cut a row JSON midway through a number to meet a reply budget.
    useful = {'player', 'entity', 'team', 'opponent', 'season', 'season_type', 'statistic', 'metric', 'category',
              'window', 'definition', 'filter', 'named_player', 'offense_player', 'defense_player',
              'n', 'missing', 'mean', 'median', 'total', 'value', 'matched_games', 'pearson_r', 'confirmed_coappearances',
              'first_game', 'last_game', 'data_through_date', 'source_published_at', 'published_at', 'status', 'reason'}
    text = result['table'] + ' — saved research. '
    for row in result['rows']:
        body = json.dumps({key: value for key, value in row.items() if key in useful}, ensure_ascii=False, default=str, separators=(',', ':'))
        if len(text) + len(body) + 145 > 1000:
            break
        text += body + '\n'
    return text + 'Saved observations only; missing values and unknown coverage stay unknown. Read time is not freshness; coappearance is not guarding.'


def _focused_args(tool, args, focus):
    """A changed market/player cannot silently inherit the previous offer."""
    args = dict(args)
    focus = focus if type(focus) is dict else {}
    if tool not in {'player_games', 'player_profile', 'availability', 'prop_assessment'}:
        return args
    same_player = (not args.get('player') or not focus.get('player')
                   or normalized_name(args['player']) == normalized_name(focus['player']))
    same_stat = (not args.get('stat') or not focus.get('stat')
                 or normalize_stat(args['stat']) == normalize_stat(focus['stat']))
    if 'player' not in args and focus.get('player'):
        args['player'] = focus['player']
    if tool == 'prop_assessment':
        if 'stat' not in args and same_player and focus.get('stat'):
            args['stat'] = focus['stat']
        if 'line' not in args and same_player and same_stat and focus.get('line') is not None:
            args['line'] = focus['line']
    if tool in {'availability', 'prop_assessment'} and same_player and 'date' not in args:
        day = focus.get('date') or focus.get('game_date')
        if day:
            args['date'] = day
    return args


def execute_read(master, task, *, focus=None, now=None):
    """Execute one validated, bounded read. Failure stays a targeted unknown."""
    parsed = validate_task(task)
    base = {'tool': parsed['tool'] if parsed else None, 'args': parsed['args'] if parsed else {},
            'available': False, 'answer': '', 'cards': [], 'sources': [], 'scope': '', 'missing': []}
    if parsed is None:
        base.update(answer='That evidence request has an unsupported tool, argument or value.',
                    missing=['Use one documented read-only tool with its named arguments.'])
        return base
    dispatch = {'player_games': _player_games, 'player_profile': _profile, 'team_games': _team_games,
                'next_game': _next_game, 'schedule': _schedule, 'availability': _availability, 'prop_assessment': _prop,
                'research_table': _research}
    try:
        clock = _clock(now)
        args = _focused_args(parsed['tool'], parsed['args'], focus)
        return {**base, **dispatch[parsed['tool']](master, args, clock)}
    except _Missing as error:
        return {**base, 'answer': str(error), 'missing': [str(error)]}
    except (sqlite3.Error, OSError) as error:
        detail = f'Saved evidence could not be read ({type(error).__name__}); no data was changed.'
        return {**base, 'answer': detail, 'missing': [detail]}
