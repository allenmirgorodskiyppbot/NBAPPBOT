"""Own projections and conditional target lines, before a sportsbook upload.

Research stays within existing players/statistics. Targets are a transparent
heuristic buffer around experimental projections, never claimed market offers,
calibrated win probabilities, or actual entries.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from board_shortlist import MARKET_NAMES, player_availability
from contextual_projection import projection_for_leg
from feeds import canonical_player, canonical_team
from news_digest import _aware, _clean
from lineup_selection import select_lineup
from pregame_freshness import assess_pregame_freshness

EASTERN = ZoneInfo('America/New_York')
MARKETS = ('pts', 'reb', 'ast', 'pra', 'fga')
MIN_OBSERVATIONS = 8
MAX_SOURCE_LAG_DAYS = 7


def _now(value):
    if isinstance(value, str):
        value = _aware(value)
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _select_game(store, date, team, now):
    today = now.astimezone(EASTERN).date().isoformat()
    if date is not None:
        date = dt.date.fromisoformat(str(date)).isoformat()
    with store.connect() as db:
        schedule = [dict(row) for row in db.execute(
            'SELECT * FROM season_schedule WHERE date>=? AND (home_team=? OR away_team=?) '
            'ORDER BY date,start_time,id', (date or today, team, team))]
        observed = [dict(row) for row in db.execute(
            'SELECT * FROM games WHERE date>=? AND (home_team=? OR away_team=?) '
            'ORDER BY date,start_time,id', (date or today, team, team))]
    by_id = {row['id']: row for row in schedule}
    blocked_ids = {row['id'] for row in [*schedule, *observed] if row.get('status') in {
        'postponed', 'canceled', 'cancelled', 'suspended', 'live', 'final', 'post', 'completed', 'past_schedule_only'}}
    for row in observed:
        old = by_id.get(row['id'], {})
        by_id[row['id']] = {**old, **{key: value for key, value in row.items() if value is not None}}
    games = sorted(by_id.values(), key=lambda row: (row['date'], row.get('start_time') or '', row['id']))
    if date:
        games = [game for game in games if game['date'] == date]
    if not games:
        return None, 'No saved game was found for that date.' if date else 'No upcoming saved game is available.'
    eligible = [g for g in games if g['id'] not in blocked_ids and g.get('status') == 'scheduled' and
                _aware(g.get('start_time')) and _aware(g['start_time']) > now and
                _aware(g['start_time']).astimezone(EASTERN).date().isoformat() == g['date']]
    if not eligible:
        return None, 'The saved game has started, ended, been postponed, or has no confirmed tipoff time.'
    first_date = eligible[0]['date']
    matches = [g for g in eligible if g['date'] == first_date]
    if len(matches) != 1:
        return None, 'More than one saved game matches the date; event timing needs checking.'
    return matches[0], None


def _targets(projection):
    value = float(projection['reference_projection'])
    spread = projection['empirical_scenario_range']
    low, high = float(spread['p10']), float(spread['p90'])
    if not all(math.isfinite(number) for number in (value, low, high)) or value <= 0 or high < low:
        return None
    # A heuristic research threshold, not a calibrated probability or bookmaker
    # prediction. Half-point rounding always moves away from the projection.
    buffer = max(.5, .15 * value, .25 * (high - low))
    more = math.floor(value - buffer - .5) + .5
    less = math.ceil(value + buffer - .5) + .5
    return {'more_at_or_below': more if more >= .5 else None, 'less_at_or_above': less,
            'buffer': round(buffer, 3),
            'method': 'At least 0.5, 15% of the projection, or 25% of the observed p10–p90 spread; rounded outward to half points.'}


def _reason(projection):
    opponent = next((row for row in projection.get('cohorts', []) if row.get('type') == 'opponent_team'), None)
    text = f"{projection['projected_minutes']:.1f} projected minutes; {projection['n']} saved games through {projection['last_date']}."
    if opponent:
        text += (f" {opponent['team']}: {opponent['n']} comparable games; " +
                 ('small context adjustment.' if opponent.get('adjustment') else 'no matchup adjustment.'))
    if len(projection.get('seasons', [])) > 1:
        text += ' Mixed-season sample.'
    return text


def build_independent_picks(store, date=None, count=4, team='NYK', now=None, constraints=None):
    """Build a read-only conditional research draft without reading any board."""
    now = _now(now)
    if type(count) is not int or not 1 <= count <= 6:
        raise ValueError('Request between one and six distinct players.')
    constraints = dict(constraints or {})
    # A preference changes the ordering of independently qualified markets. It
    # must not expand the supported market set or weaken the evidence gates.
    preferred_stat = constraints.get('preferred_stat')
    if preferred_stat not in MARKETS:
        preferred_stat = None
    team = canonical_team(team)
    game, error = _select_game(store, date, team, now)
    result = {'mode': 'independent_projections', 'generated_at': now.isoformat(),
              'requested_count': count, 'requires_prizepicks_comparison': True,
              'source_kind': 'saved statistics and schedule', 'picks': [], 'held': [],
              'game': game, 'game_date': game['date'] if game else date,
              'two_team_coverage': False, 'note': error, 'research_only': True,
              'limits': ['These are own projections and conditional target lines, not current PrizePicks offers.',
                         'Experimental scenarios; no calibrated win probabilities or combined entry odds.',
                         'Current availability, game time and offer terms require checking. No entry is recorded.']}
    _, coverage = select_lineup([], count, constraints=constraints)
    result.update(coverage)
    if preferred_stat:
        result['lineup_constraints']['preferred_stat'] = preferred_stat
    if error:
        return result
    teams = {game['home_team'], game['away_team']}
    team_freshness = {value: assess_pregame_freshness(store, game['date'], value, now)
                      for value in sorted(teams)}
    result['freshness_by_team'] = team_freshness
    cutoff = min(game['date'], now.astimezone(EASTERN).date().isoformat())
    with store.connect() as db:
        players = [dict(row) for row in db.execute(
            'WITH ranked AS (SELECT player_id,player,team,date,ROW_NUMBER() OVER '
            '(PARTITION BY player_id ORDER BY date DESC,updated_at DESC) AS place '
            'FROM player_games WHERE date<=?) '
            'SELECT * FROM ranked WHERE place=1 AND team IN (?,?) ORDER BY player_id',
            (cutoff, *sorted(teams)))]
    news = store.recent_news(200)
    candidates = []
    seen = set()
    for player in players[:80]:
        identity = canonical_player(player['player'])
        if identity in seen:
            continue
        seen.add(identity)
        availability = player_availability(news, player['player'], game['date'], now)
        if availability['blocked']:
            result['held'].append({'player': player['player'], 'reason': availability['note']})
            continue
        freshness = team_freshness[player['team']]
        if freshness['ready']:
            freshness = assess_pregame_freshness(store, game['date'], player['team'], now,
                                                 player=player['player'])
        if not freshness['ready']:
            result['held'].append({'player': player['player'],
                'reason': 'Current game data is not ready: ' + ' '.join(freshness['reasons']),
                'freshness': freshness})
            continue
        choices, reasons = [], []
        for stat in MARKETS:
            if stat not in freshness.get('current_player_stat_coverage', []):
                reasons.append('The requested statistic is missing or conflicting in the player’s latest completed game.')
                continue
            try:
                projection = projection_for_leg(store, {'player': player['player'], 'player_id': player['player_id'],
                    'team': player['team'], 'opponent': game['away_team'] if player['team'] == game['home_team'] else game['home_team'],
                    'date': game['date'], 'stat': stat}, now)
            except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                reasons.append('Projection inputs could not be validated.')
                continue
            if not projection.get('available'):
                reasons.append(projection.get('reason') or 'Comparable observations are missing.')
                continue
            try:
                lag = (dt.date.fromisoformat(cutoff) - dt.date.fromisoformat(projection['last_date'])).days
                target_lag = (dt.date.fromisoformat(game['date']) - dt.date.fromisoformat(projection['last_date'])).days
                minutes = float(projection['projected_minutes'])
                n = int(projection['n'])
                targets = _targets(projection)
            except (ValueError, TypeError, KeyError):
                reasons.append('Projection inputs are incomplete.')
                continue
            if n < MIN_OBSERVATIONS or not 0 <= lag <= MAX_SOURCE_LAG_DAYS or target_lag > MAX_SOURCE_LAG_DAYS:
                reasons.append('Comparable workload data is too sparse or more than seven days behind the game.')
                continue
            if not targets or not math.isfinite(minutes) or minutes < 20:
                reasons.append('A stable, meaningful workload projection is unavailable.')
                continue
            if not projection.get('source_games') or any(not row.get('source') for row in projection['source_games']):
                reasons.append('Source provenance is incomplete.')
                continue
            expected = projection['reference_projection']
            spread = projection['empirical_scenario_range']
            relative_width = (spread['p90'] - spread['p10']) / max(expected, 1)
            # Rank research readiness, not a nonexistent edge against an offer.
            support = sum(min(int(c.get('n') or 0), 8) for c in projection.get('cohorts', []))
            rank = min(n, 20) / (1 + relative_width) + min(minutes, 40) / 8 + support / 8 - lag / 2
            if availability.get('note'):
                rank -= 4
            choices.append({'player': player['player'], 'player_id': player['player_id'],
                            'team': player['team'], 'opponent': projection['opponent'],
                            'stat': stat, 'projection': projection, 'targets': targets,
                            'reason': _reason(projection), 'availability': availability,
                            'caution': availability.get('note'), '_rank': rank})
        if choices:
            choices.sort(key=lambda value: (bool(preferred_stat and value['stat'] != preferred_stat),
                                            -value['_rank'], MARKETS.index(value['stat'])))
            candidates.append(choices[0])
        else:
            result['held'].append({'player': player['player'], 'reason': next(iter(dict.fromkeys(reasons)), 'Insufficient data.')})
    candidates.sort(key=lambda value: (-value['_rank'], value['player']))
    chosen, coverage = select_lineup(candidates, count, required_teams=teams, constraints=constraints)
    result.update(coverage)
    if preferred_stat:
        result['lineup_constraints']['preferred_stat'] = preferred_stat
    result['picks'] = [{k: v for k, v in row.items() if k != '_rank'} for row in chosen]
    result['note'] = (f"{len(chosen)} of {count} requested player projections are supported." if len(chosen) < count else
                      'Compare these independent targets with the actual offers before selecting sides.')
    if not chosen and any(not value['ready'] for value in team_freshness.values()):
        result['note'] += ' Current game-stat coverage is missing or stale; /freshness shows the required updates.'
    if not result['lineup_complete']:
        result['note'] += ' ' + result['constraint_note']
    result['source_data_through_date'] = min((row['projection']['last_date'] for row in chosen), default=None)
    result['revision'] = hashlib.sha256(json.dumps({'game': game, 'picks': result['picks'],
                                                  'held': result['held']}, sort_keys=True).encode()).hexdigest()
    return result


def format_independent_picks(report, compact=False):
    limit = 950 if compact else 1800
    game = report.get('game')
    if not game:
        return 'Independent picks: ' + str(report.get('note') or 'No supported upcoming game is available.')
    tip = _aware(game.get('start_time'))
    label = tip.astimezone(EASTERN).strftime('%b %d, %-I:%M %p Eastern') if tip else str(game['date'])
    heading = 'Partial research' if report.get('lineup_complete') is False else 'My projections'
    lines = [f"{heading} — {game['home_team']} vs {game['away_team']} · {label}",
             'My target lines, not quoted PrizePicks offers.']
    if report.get('lineup_complete') is False:
        lines[1] += '\n' + _clean(report.get('constraint_note'), 240)
    for i, item in enumerate(report.get('picks', []), 1):
        value = item['projection']['reference_projection']
        targets = item['targets']
        more, less = targets['more_at_or_below'], targets['less_at_or_above']
        target = (f'More if line ≤{more:g}; ' if more is not None else '') + f'Less if line ≥{less:g}.'
        block = f"{i}. {_clean(item['player'], 60)} · {MARKET_NAMES[item['stat']]}: {value:g}\n{target}"
        if not compact:
            block += '\nWhy: ' + _clean(item['reason'], 240)
        if item.get('caution'):
            block += '\n' + _clean(item['caution'], 140 if not compact else 70)
        lines.append(block)
    if not report.get('picks'):
        lines.append('No supported projections yet.')
    lines.append(_clean(report.get('note'), 240))
    if not compact and report.get('held'):
        first = report['held'][0]
        lines.append('Held: '+_clean(first['player'], 60)+' — '+_clean(first['reason'], 140))
    if report.get('source_data_through_date'):
        lines.append('Saved evidence through '+report['source_data_through_date']+'.')
    lines.append('Check actual lines, availability and payout terms. Experimental research; no entry recorded.')
    total = len(report.get('picks', []))
    shown = total
    def rendered():
        if shown < total:
            return '\n\n'.join(lines[:-1] + [f'Showing {shown} of {total} projections; /picks shows the fuller report.', lines[-1]])
        return '\n\n'.join(lines)
    while len(rendered()) > limit:
        removable = next((i for i, line in enumerate(lines) if line.startswith('Held:')), None)
        if removable is not None:
            lines.pop(removable)
            continue
        changed = False
        for i in range(2, len(lines)):
            if '\nWhy: ' in lines[i]:
                before, after = lines[i].split('\nWhy: ', 1)
                caution = '\n'+after.split('\n', 1)[1] if '\n' in after else ''
                lines[i] = before+caution
                changed = True
                break
        if not changed:
            blocks = [i for i, line in enumerate(lines) if line.split('.', 1)[0].isdigit()]
            if not blocks:
                break
            lines.pop(blocks[-1])
            shown -= 1
    return rendered()
