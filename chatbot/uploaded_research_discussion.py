"""Recompute why an uploaded historical research side was selected."""
from __future__ import annotations

import datetime as dt

from board_shortlist import (MARKET_NAMES, _event, _master_comparison, _number,
                             _research_choice, player_availability)
from career_identity import resolve_career_identity
from contextual_projection import projection_for_leg, projection_read_scope
from master import normalized_name
from pick_discussion import EASTERN, _now, build_pick_discussion, format_pick_discussion, normalize_stat
from pregame_freshness import assess_pregame_freshness


def is_uploaded_research_choice(pick):
    return (pick.get('research_choice') is True and pick.get('proposal_group') == 'uploaded'
            and pick.get('line_kind') == 'assistant_proposal')


def matches_uploaded_research_context(turn):
    """A new player, threshold or market must use the ordinary assessment path."""
    picks = turn.get('recent_picks') or []
    slots = turn.get('slots') or {}
    if slots.get('season_type') not in (None, '', 'Regular Season'):
        return False
    marked = [pick for pick in picks if is_uploaded_research_choice(pick)]
    if not marked:
        return False
    if not slots.get('player'):
        return True
    for pick in marked:
        if normalized_name(pick.get('player')) != normalized_name(slots['player']):
            continue
        if slots.get('stat') and normalize_stat(slots['stat']) != normalize_stat(pick.get('stat')):
            continue
        if slots.get('line') is not None and _number(slots['line']) != _number(pick.get('line')):
            continue
        if slots.get('side') and slots['side'] != pick.get('side'):
            continue
        date = slots.get('game_date') or slots.get('date')
        if date and date != (pick.get('game_date') or pick.get('date')):
            continue
        return True
    return False


def _current_player(store, name, cutoff):
    aliases = resolve_career_identity(store, name)['aliases']
    if not aliases:
        return None
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            'SELECT p.player,p.team,p.date FROM player_games p JOIN games g ON g.id=p.game_id '
            'WHERE p.player IN (' + ','.join('?' for _ in aliases) + ') AND p.date<? '
            "AND p.date=g.date AND g.status='final' AND p.team IN (g.home_team,g.away_team) "
            'ORDER BY p.date DESC,p.updated_at DESC LIMIT 20', (*aliases, cutoff))]
    if not rows or len({row['team'] for row in rows if row['date'] == rows[0]['date']}) != 1:
        return None
    return rows[0]


def _held(pick, reason):
    return {'player': pick.get('player', 'This selection'), 'stat': pick.get('stat'),
            'side': pick.get('side'), 'line': pick.get('line'), 'status': 'held',
            'reason': reason, 'research_only': True, 'eligible_for_ranking': False,
            'approved_for_entry': False, 'offer_verified': False}


def _explain_one(store, pick, slots, now):
    name, stat, line = str(pick.get('player') or ''), normalize_stat(pick.get('stat')), _number(pick.get('line'))
    side = pick.get('side')
    if not name or not stat or line is None or side not in {'more', 'less'}:
        return _held(pick, 'The saved player, statistic, line or readable side is incomplete.')
    saved_date = pick.get('game_date') or pick.get('date')
    requested_date = slots.get('game_date') or slots.get('date')
    if saved_date and requested_date and saved_date != requested_date:
        return _held(pick, 'That uploaded choice belongs to a different game date.')
    try:
        day = dt.date.fromisoformat(str(saved_date or requested_date))
    except (TypeError, ValueError):
        return _held(pick, 'The original uploaded game date is missing or invalid.')
    today = now.astimezone(EASTERN).date()
    if day < today:
        return _held(pick, 'That game date has passed; the old research side is not a new pregame recommendation.')
    if pick.get('season_type') not in (None, '', 'Regular Season'):
        return _held(pick, 'This research rule supports regular-season observations only.')
    current = _current_player(store, name, min(day, today).isoformat())
    if not current:
        return _held(pick, 'The latest stored team identity is unavailable or conflicting.')
    leg = {**current, 'player': name, 'date': day.isoformat(), 'stat': stat, 'line': line, 'side': side}
    for key in ('game_id', 'opponent', 'offer_variant'):
        if pick.get(key):
            leg[key] = pick[key]
    if (slots.get('opponent') and slots.get('player')
            and normalized_name(slots['player']) == normalized_name(name)):
        leg['opponent'] = slots['opponent']
    context, issue = _event(store, leg, now)
    if issue:
        return _held(pick, issue)
    projection = projection_for_leg(store, leg, now)
    comparison = _master_comparison(store, {'leg': leg, 'projection': projection}, context, '', now, {})
    if not comparison:
        return _held(pick, projection.get('reason') or 'The saved 20-game source observations cannot be validated.')
    readiness = assess_pregame_freshness(store, day.isoformat(), current['team'], now,
                                        player=name, stat=stat)
    availability = player_availability(store.recent_news(150), name, day.isoformat(), now, store=store)
    comparison.update(offered_sides=[side],
        statistics_ready=readiness.get('statistics_ready', readiness.get('ready') is True) is True,
        timing_verified=readiness.get('timing_verified', readiness.get('ready') is True) is True,
        research_blocked=availability.get('blocked', False))
    _research_choice(comparison)
    if not comparison.get('lean_side'):
        if availability.get('blocked'):
            reason = availability.get('note') or 'Current availability requires checking.'
        elif not comparison['statistics_ready']:
            reason = '; '.join(readiness.get('statistics_issues') or readiness.get('reasons') or
                               ['Current statistical coverage is unverified.'])
        elif comparison['n'] < 20:
            reason = f"Only {comparison['n']} valid same-team observations remain; 20 are required."
        else:
            reason = 'The recomputed historical sample no longer supports that readable side.'
        return _held(pick, reason)
    comparison['opponent_context'] = next((dict(row) for row in projection.get('cohorts', [])
        if row.get('type') == 'opponent_team' and row.get('team') == context.get('opponent')), None)
    comparison['recent_minutes_reference'] = _number(projection.get('projected_minutes'))
    return {**comparison, 'status': 'research_choice', 'offer_verified': False,
            'availability_caution': availability.get('note'), 'data_readiness': readiness}


@projection_read_scope()
def build_uploaded_research_discussion(store, picks, slots=None, now=None, question=''):
    """Use current master rows and the same selection rule, never old prose stats."""
    now, slots = _now(now), dict(slots or {})
    result = {'ai_used': False, 'sources': [], 'evidence_cards': [], 'research_only': True,
              'eligible_for_ranking': False, 'approved_for_entry': False, 'offer_verified': False}
    if store is None:
        return {**result, 'answer': 'The saved master data is not ready to explain that research choice.'}
    picks = [dict(pick) for pick in picks if isinstance(pick, dict)][:12]
    explanations, assessments, research = [], [], []
    for pick in picks:
        if not is_uploaded_research_choice(pick):
            if pick.get('line_kind') == 'research_target':
                from research_discussion import build_research_discussion
                other = build_research_discussion(store, [pick], slots, now)
                explanations.append(other['fallback_answer'])
                result['sources'].extend(other.get('sources', []))
            else:
                other = build_pick_discussion(store, pick, now)
                explanations.append(format_pick_discussion(other, question))
                result['sources'].extend(other.get('sources', []))
            continue
        item = _explain_one(store, pick, slots, now)
        assessments.append(item)
        if item['status'] == 'held':
            explanations.append(f"{item['player']}: the earlier research choice is held now. {item['reason']}")
            continue
        research.append(item)
        side, metric = item['lean_side'], MARKET_NAMES[item['stat']]
        relation = 'above' if side == 'more' else 'below'
        scope = ', '.join(item.get('sample_seasons') or [])
        block = (f"{len(research)}. {item['player']} — {side.title()} {item['line']:g} {metric}"
                 f"\nRESEARCH PICK: {item['support_label']}. {item['selection_basis']}."
                 f"\nThe same {item['n']}-game rule has {item['selected_sample_count']}/{item['n']} "
                 f"{relation} this line; mean {item['mean']:.2f}, median {item['median']:g}. "
                 f"Same-team {scope or 'regular-season'} sample through {item['source_data_through_date']}."
                 f"\nLast {item['recent_n']}: {item['recent_side_count']}/{item['recent_n']} on this side.")
        block += f"\nGame context: {item['team']} vs {item['opponent']} on {item['date']}."
        cohort = item.get('opponent_context') or {}
        games = int(cohort.get('n') or 0)
        if games:
            block += (f" {games} prior comparable {'game' if games == 1 else 'games'} against "
                      f"{item['opponent']} in the recent same-team window")
            if _number(cohort.get('mean')) is not None:
                block += f"; mean {cohort['mean']:.2f}"
            block += '. This is a limited opponent sample, not lifetime matchup coverage.'
        else:
            block += ' No prior opponent game is available in the recent comparable window.'
        block += ' No numerical matchup bonus is applied.'
        if item.get('recent_minutes_reference') is not None:
            block += f" Stored recent workload reference: {item['recent_minutes_reference']:.1f} minutes."
        if item.get('research_caution'):
            block += '\nCaution: ' + item['research_caution']
        if item.get('availability_caution'):
            block += '\nAvailability: ' + item['availability_caution']
        explanations.append(block)
        result['sources'].extend(row['source'] for row in item['source_games'] if row.get('source'))
    dates = {item['date'] for item in research}
    header = ([f'Game date: {next(iter(dates))} Eastern.'] if len(dates) == 1 else [])
    if research:
        typed = [pick for pick in picks if is_uploaded_research_choice(pick)]
        source_kinds = {pick.get('source_kind') for pick in typed}
        if typed and source_kinds == {'prizepicks_the_odds_api'}:
            header.append('From PrizePicks via The Odds API')
        elif typed and source_kinds == {'prizepicks_public_feed'}:
            header.append("From PrizePicks' public feed")
        else:
            header.append('From your uploaded lines')
    if research and any(not item['timing_verified'] for item in research):
        explanations.append('Current saved statistical coverage is verified; live game timing is unverified. '
                            'That timing hold does not erase the historical reason for the research side.')
    if research:
        query = str(question).casefold()
        if any(word in query for word in ('blowout', 'bench', 'minutes', 'foul')):
            explanations.append('Reduced minutes from a blowout, foul trouble or a role change can lower counting stats. '
                                'The historical counts do not establish that any of those will happen.')
        explanations.append('This remains research, not a qualified lineup or verified current offer. '
                            'Historical counts are not this game’s win probability. No entry placed.')
    result['answer'] = '\n\n'.join([*header, *explanations]) or 'Which uploaded research choice should I explain?'
    result['sources'] = list(dict.fromkeys(result['sources']))
    result['discussion'] = {'assessments': [{key: item[key] for key in
        ('player', 'stat', 'status', 'research_only', 'eligible_for_ranking', 'approved_for_entry')}
        for item in assessments], 'research_choice': True, 'eligible_for_ranking': False,
        'approved_for_entry': False}
    return result
