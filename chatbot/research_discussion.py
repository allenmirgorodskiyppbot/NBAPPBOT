"""Explain an existing unpriced shortlist without fabricating a sportsbook offer."""
from __future__ import annotations

import datetime as dt
import math

from board_shortlist import MARKET_NAMES, player_availability
from contextual_projection import projection_for_leg
from feeds import canonical_team
from independent_picks import _select_game, _targets
from master import normalized_name
from pick_discussion import EASTERN, _now, normalize_stat


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _latest_player(store, name, cutoff):
    with store.connect() as db:
        matches = [dict(row) for row in db.execute(
            'SELECT player_id,player,team,date FROM player_games WHERE date<? '
            'ORDER BY date DESC,updated_at DESC', (cutoff,))
            if normalized_name(row['player']) == normalized_name(name)]
    return matches[0] if matches else None


def _card(item, index):
    player, stat = item['player'], item['stat']
    metric = MARKET_NAMES[stat]
    projection, targets = item['projection'], item['targets']
    scope = (f"Same-team regular-season observations, seasons {', '.join(projection['seasons'])}, "
             f"through {projection['last_date']}; hypothetical research targets, not current offers")
    common = {'entity': player, 'metric': metric, 'scope': scope}
    facts = [
        {**common, 'fact_type': 'projection',
         'text': f"{player}: experimental reference {projection['reference_projection']:g} {metric} from {projection['n']} stored games.",
         'values': [projection['reference_projection'], projection['n']]},
        {**common, 'metric': 'minutes', 'fact_type': 'workload',
         'text': f"{player}: workload reference {projection['projected_minutes']:g} minutes.",
         'values': [projection['projected_minutes']]},
    ]
    more, less = targets['more_at_or_below'], targets['less_at_or_above']
    text = f'{player}: hypothetical '
    numbers = []
    if more is not None:
        text += f'More target at or below {more:g} {metric}; '
        numbers.append(more)
    text += f'Less target at or above {less:g} {metric}.'
    numbers.append(less)
    facts.append({**common, 'fact_type': 'line', 'text': text, 'values': numbers})
    qualification = f'{player}: these are hypothetical targets; no current offer or betting edge is verified.'
    if item['status'] == 'held':
        qualification = f"{player}: Hold. " + ' '.join(item['cautions'][:1]) + ' No current offer is verified.'
    facts.append({**common, 'fact_type': 'context', 'text': qualification, 'values': []})
    body = ' '.join(fact['text'] for fact in facts)
    # Fact text is bounded structurally. Never cut a number or its qualifier.
    return {'id': f'R{index}', 'kind': 'research_projection', 'entity': player,
            'text': body, 'approved_facts': facts,
            'source': 'Experimental workload model from stored player games',
            'data_through_date': projection['last_date'], 'scope': scope,
            'line_kind': 'research_target', 'offer_verified': False, 'research_only': True,
            **({'decision': 'pass'} if item['status'] == 'held' else {})}


def build_research_discussion(store, picks, slots=None, now=None):
    """Recompute explanation evidence for supplied identities, at most four.

    Never uses an old model's numerical prose as evidence, selects new players,
    saves a board, or upgrades a conditional target to a verified offered line.
    """
    now, slots = _now(now), dict(slots or {})
    today = now.astimezone(EASTERN).date()
    result = {'cards': [], 'evidence_cards': [], 'assessments': [], 'held': [],
              'sources': [], 'research_only': True, 'offer_verified': False,
              'generated_at': now.isoformat()}
    if store is None:
        result['fallback_answer'] = 'The saved data is not ready to explain those research targets yet.'
        return result
    picks = [dict(pick) for pick in (picks or []) if isinstance(pick, dict)]
    if slots.get('player'):
        picks = [pick for pick in picks if normalized_name(pick.get('player')) == normalized_name(slots['player'])]
    explicit_date = slots.get('game_date') or slots.get('date')
    saved_dates = {str(pick.get('date') or pick.get('game_date')) for pick in picks
                   if pick.get('date') or pick.get('game_date')}
    if not explicit_date and len(saved_dates) > 1:
        result['fallback_answer'] = 'Those research targets refer to different dates. Which game date should I explain?'
        return result
    explicit_date = explicit_date or next(iter(saved_dates), None)
    try:
        day = dt.date.fromisoformat(str(explicit_date or today))
    except (TypeError, ValueError):
        result['fallback_answer'] = 'I need a valid game date to recompute those research targets.'
        return result
    result.update(date=day.isoformat(), date_assumed=not bool(explicit_date))
    if day < today:
        result['fallback_answer'] = ('Those targets were for a game date that has passed. I can discuss the original '
            'method, but I won’t present a newly calculated target as the pregame recommendation.')
        return result
    cutoff = min(day, today).isoformat()
    seen = set()
    for pick in picks:
        name, stat = str(pick.get('player') or '').strip(), normalize_stat(pick.get('stat'))
        key = (normalized_name(name), stat)
        if not name or key in seen:
            continue
        seen.add(key)
        if len(seen) > 4:
            break
        if not stat:
            result['held'].append({'player': name, 'reason': 'The full statistic is missing or unsupported.'})
            continue
        player = _latest_player(store, name, cutoff)
        if not player:
            result['held'].append({'player': name, 'reason': 'No earlier stored team observation is available.'})
            continue
        team = player['team']
        if pick.get('team') and canonical_team(pick['team']) != team:
            result['held'].append({'player': name, 'reason': 'The selected team conflicts with the latest stored team.'})
            continue
        game, error = _select_game(store, day.isoformat(), team, now)
        if error or not game:
            result['held'].append({'player': name, 'reason': error or 'The saved game is unavailable.'})
            continue
        opponent = game['away_team'] if team == game['home_team'] else game['home_team']
        requested_opponent = pick.get('opponent') or slots.get('opponent')
        allowed_opponents = ({opponent} if pick.get('opponent') or slots.get('player')
                             else {game['home_team'], game['away_team']})
        if requested_opponent and canonical_team(requested_opponent) not in allowed_opponents:
            result['held'].append({'player': name, 'reason': 'The opponent conflicts with the saved game for this date.'})
            continue
        # Passing no side or line is intentional: neither the original model
        # text nor a target threshold establishes an actual priced offer.
        leg = {key: player[key] for key in ('player', 'player_id', 'team')}
        leg.update(stat=stat, date=day.isoformat(), opponent=opponent)
        try:
            projection = projection_for_leg(store, leg, now)
        except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
            projection = {'available': False, 'reason': 'The projection inputs could not be validated.'}
        if not projection.get('available'):
            result['held'].append({'player': name, 'reason': projection.get('reason') or 'Comparable observations are missing.'})
            continue
        try:
            age = (day - dt.date.fromisoformat(projection['last_date'])).days
            targets = _targets(projection)
            valid = (targets and all(_finite(projection.get(key)) for key in
                    ('reference_projection', 'projected_minutes', 'n')) and projection['n'] >= 8
                     and projection['last_date'] < cutoff and isinstance(projection.get('seasons'), list))
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            result['held'].append({'player': name, 'reason': 'The projection or source dates could not be validated.'})
            continue
        availability = player_availability(store.recent_news(150), player['player'], day.isoformat(), now)
        cautions = []
        if age > 7:
            cautions.append('The observations are more than seven days behind the requested game.')
        source_games = projection.get('source_games') or []
        if not source_games or any(not item.get('source') for item in source_games):
            cautions.append('Source provenance is incomplete.')
        if availability.get('blocked') or availability.get('note'):
            cautions.append(availability.get('note') or 'Availability requires checking.')
        item = {'player': player['player'], 'stat': stat, 'team': team, 'opponent': opponent,
                'date': day.isoformat(), 'status': 'held' if cautions else 'research_only',
                'projection': projection, 'targets': targets, 'line_kind': 'research_target',
                'offer_verified': False, 'cautions': cautions,
                'reason': ('The shortlist used recorded workload and production per minute, with only supported '
                           'opponent/rest adjustments. Its buffered targets are conditional research thresholds.'),
                'supporting_reasons': projection.get('supporting_reasons', [])[:3]}
        result['assessments'].append(item)
        result['cards'].append(_card(item, len(result['cards']) + 1))
        result['sources'].extend(item['source'] for item in source_games if item.get('source'))
    result['sources'] = list(dict.fromkeys(result['sources']))
    result['evidence_cards'] = result['cards']
    if result['assessments']:
        explanations = []
        for item in result['assessments']:
            p = item['projection']
            explanations.append(f"{item['player']}: experimental reference {p['reference_projection']:g} "
                f"{MARKET_NAMES[item['stat']]} at {p['projected_minutes']:g} minutes, using {p['n']} stored games "
                f"through {p['last_date']}." + (' ' + item['cautions'][0] if item['cautions'] else ''))
        result['fallback_answer'] = ('These were research targets, not confirmed PrizePicks offers.\n'
            + '\n'.join(explanations)
            + '\nThe reference combines observed production and workload; actual lines, current availability and payout still need checking.')
    else:
        reasons = '; '.join(f"{item['player']}: {item['reason']}" for item in result['held'][:2])
        result['fallback_answer'] = ('I can’t recompute that shortlist for this game yet. ' + reasons
            if reasons else 'Which player and statistic from the research shortlist should I explain?')
    return result
