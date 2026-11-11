"""Read-only prop discussion: calculate evidence before asking the model to explain it.

The returned lean is a deliberately modest research opinion, not a calibrated
win probability. A user's line is never promoted to a verified live offer.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import statistics
from zoneinfo import ZoneInfo

from board_shortlist import MARKET_NAMES, player_availability
from contextual_projection import (_quantile, _weighted, projection_for_leg,
                                   baseline_offer_decision, BENCHMARK_METHOD)
from feeds import canonical_player, canonical_team
from master import STAT_ALIASES, STAT_COMPONENTS, normalized_name
from news_digest import _aware
from pregame_freshness import assess_pregame_freshness
from season_calendar import calendar_context, current_season

EASTERN = ZoneInfo('America/New_York')
_ALIASES = {
    'threes': 'tpm', 'three pointers': 'tpm', 'three pointers made': 'tpm',
    '3 pointers': 'tpm', '3 pointers made': 'tpm', '3ptm': 'tpm', '3pm': 'tpm',
    'three point attempts': 'tpa', '3 point attempts': 'tpa', '3pta': 'tpa',
    'attempts': 'fga', 'fg attempts': 'fga', 'field goal attempts': 'fga',
    'field goals attempted': 'fga', 'points rebounds assists': 'pra',
    'pts rebs asts': 'pra', 'points rebounds': 'pr', 'points assists': 'pa',
    'rebounds assists': 'ra', 'rebs asts': 'ra', 'reb': 'reb', 'asts': 'ast',
    'rebs': 'reb', 'steals blocks': 'stocks', 'minutes played': 'minutes',
}


def normalize_stat(value):
    raw = str(value or '').strip().casefold()
    if raw in STAT_COMPONENTS:
        return raw
    if raw in STAT_ALIASES:
        return STAT_ALIASES[raw]
    from conversation import _combo_syntax
    combined, error = _combo_syntax(raw)
    if error:
        return None
    if combined in STAT_COMPONENTS:
        return combined
    key = re.sub(r'[^a-z0-9]+', ' ', raw).strip()
    return _ALIASES.get(key) or STAT_ALIASES.get(key)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def _now(value):
    if isinstance(value, str):
        value = _aware(value)
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _round(value):
    return round(value, 2) if value is not None else None


def _event(store, day, team, now):
    context = calendar_context(store, day, team) if team else {}
    with store.connect() as db:
        games = [dict(row) for row in db.execute(
            'SELECT * FROM games WHERE date=? AND (home_team=? OR away_team=?)',
            (day, team, team))]
    if len(games) == 1:
        game = games[0]
        context.update(game_id=game['id'], status=game.get('status'),
                       opponent=game['away_team'] if game['home_team'] == team else game['home_team'])
        if game.get('start_time'):
            context['tipoff'] = game['start_time']
        if game.get('source'):
            context['source'] = game['source']
    elif len(games) > 1:
        context['ambiguous'] = True
    tip = _aware(context.get('tipoff'))
    context['pregame'] = bool(context.get('status') == 'scheduled' and tip and tip > now)
    context['started_or_unavailable'] = (context.get('status') in
        {'final', 'live', 'post', 'completed', 'canceled', 'cancelled', 'postponed', 'suspended'}
        or bool(tip and tip <= now))
    return context


def _opponent_defense(store, opponent, season, phase, stat, day, now):
    """Use only relevant existing team fields, with separate source clocks."""
    empty = {'available': False, 'team': opponent, 'metrics': [], 'quantitative_adjustment': False}
    if not opponent:
        return empty
    with store.connect() as db:
        saved = db.execute("SELECT payload_json FROM refreshed_tracking WHERE entity=? AND season=? "
                           "AND category='Team defense'", (opponent, season)).fetchone()
    if not saved:
        return empty
    try:
        row = json.loads(saved[0])
    except (TypeError, ValueError):
        return empty
    if not isinstance(row, dict) or row.get('season_type') != phase:
        return empty
    published = _aware(row.get('published_at'))
    if published and published > now:
        return empty
    if not published and row.get('published_at'):
        try:
            if dt.date.fromisoformat(str(row['published_at'])[:10]) > now.astimezone(EASTERN).date():
                return empty
        except ValueError:
            pass
    coverage = row.get('data_through_date')
    if coverage:
        try:
            coverage = dt.date.fromisoformat(str(coverage)[:10]).isoformat()
        except ValueError:
            coverage = None
    # Retrospective discussion must not bring a later published snapshot into
    # a pregame explanation. Unknown coverage cannot support backtesting.
    if coverage and (coverage >= day.isoformat()
                     or coverage > now.astimezone(EASTERN).date().isoformat()):
        return empty
    if day < now.astimezone(EASTERN).date() and (
            not coverage or not published or published.astimezone(EASTERN).date() >= day):
        return empty
    gp = _number(row.get('gp'))
    fields = row.get('fields')
    if not gp or not isinstance(fields, dict):
        return empty
    wanted = []
    if stat in {'pts', 'pra', 'pr', 'pa'}:
        wanted.append(('points_allowed', 'points allowed per game'))
    if stat in {'reb', 'pra', 'pr', 'ra', 'oreb', 'dreb'}:
        wanted.append(('opponent_rebounds', 'total rebounds allowed per game'))
    if stat in {'ast', 'pra', 'pa', 'ra'}:
        wanted.append(('opponent_assists', 'assists allowed per game'))
    if stat == 'fga':
        wanted.append(('defended_fga', 'opponent field-goal attempts per game'))
    if stat in {'tpm', 'tpa'}:
        if stat == 'tpm':
            wanted.append(('opponent_tpm', 'three-pointers allowed per game'))
        wanted.append(('opponent_tpa', 'opponent three-point attempts per game'))
    metrics = []
    for key, label in wanted:
        value = _number(fields.get(key))
        if value is not None:
            metrics.append({'key': key, 'label': label, 'value': _round(value / gp),
                            'source_total': value, 'source_games': gp})
    if stat in {'pts', 'tpm'}:
        made, attempts = (('opponent_tpm', 'opponent_tpa') if stat == 'tpm'
                          else ('defended_fgm', 'defended_fga'))
        a, b = _number(fields.get(made)), _number(fields.get(attempts))
        if a is not None and b and a <= b:
            label = 'opponent three-point percentage' if stat == 'tpm' else 'opponent field-goal percentage'
            metrics.append({'key': 'opponent_shooting_percentage', 'label': label,
                            'value': _round(100 * a / b), 'made': a, 'attempts': b})
    if not metrics:
        return empty
    return {**empty, 'available': True, 'season': season, 'season_type': phase,
            'metrics': metrics[:3], 'gp': gp, 'published_at': row.get('published_at'),
            'data_through_date': coverage, 'coverage_unknown': coverage is None,
            'source': row.get('source') or 'Stored published team-defense extract',
            'scope': 'Whole opposing-team production against all opponents; not this player or a named defender.',
            'note': ('Covered-game date unknown; use as dated context only, not a current matchup adjustment.'
                     if coverage is None else 'Historical team context only; no automatic player-projection adjustment.')}


def build_pick_discussion(store, pick, now=None):
    """Return a compact grounded assessment from structured conversational slots.

    ``pick`` accepts player, stat, line, optional side/date/opponent/team. Only
    the latest stored team's current-season final played observations are used.
    Missing components, DNPs, unknown appearances, and target/future games are
    never converted to zeros or silently mixed into the sample.
    """
    now = _now(now)
    today = now.astimezone(EASTERN).date()
    pick = dict(pick or {})
    player = str(pick.get('player') or '').strip()
    stat = normalize_stat(pick.get('stat'))
    line = _number(pick.get('line'))
    side = str(pick.get('side') or '').casefold()
    side = {'over': 'more', 'up': 'more', 'under': 'less', 'down': 'less'}.get(side, side)
    side = side if side in {'more', 'less'} else None
    missing = [name for name, value in [('player', player), ('stat', stat), ('line', line)]
               if value is None or value == '']
    result = {'available': False, 'player': player, 'stat': stat, 'line': line,
              'requested_side': side, 'decision': 'pass', 'research_lean': None,
              'confidence': 'insufficient', 'missing': missing, 'reasons': [],
              'cautions': [], 'sources': [], 'evidence_cards': [], 'research_only': True,
              'offer_verified': False, 'method': BENCHMARK_METHOD,
              'generated_at': now.isoformat()}
    if missing:
        result['reason'] = 'I need the ' + ', '.join(missing) + ' to assess this line.'
        result['fallback_answer'] = result['reason']
        return result
    if store is None:
        result['reason'] = 'Saved player observations are not ready yet.'
        result['fallback_answer'] = result['reason']
        return result
    if pick.get('date') and pick.get('game_date') and str(pick['date']) != str(pick['game_date']):
        result['reason'] = 'The supplied game dates conflict; which date should I assess?'
        result['fallback_answer'] = result['reason']
        return result
    try:
        day = dt.date.fromisoformat(str(pick.get('date') or pick.get('game_date') or today))
    except ValueError:
        result['reason'] = 'The game date is invalid; use a real calendar date.'
        result['fallback_answer'] = result['reason']
        return result
    cutoff = min(day, today).isoformat()
    season = current_season(dt.datetime.combine(day, dt.time(12), EASTERN))
    phase = str(pick.get('season_type') or 'Regular Season')
    if phase not in {'Regular Season', 'Playoffs'}:
        result['reason'] = 'The requested season phase is unsupported.'
        result['fallback_answer'] = result['reason']
        return result
    result.update(date=day.isoformat(), date_assumed=not bool(pick.get('date') or pick.get('game_date')),
                  season=season, season_type=phase, mode='retrospective' if day < today else 'pregame_research')
    with store.connect() as db:
        # Name IDs and provider IDs may coexist. Resolve an exact normalized
        # name first; typo resolution belongs to the conversational layer.
        names = [dict(row) for row in db.execute(
            'SELECT DISTINCT player,player_id FROM player_games WHERE date<?', (cutoff,))]
        identities = {row['player_id'] for row in names
                      if normalized_name(row['player']) == normalized_name(player)}
        identities.add(canonical_player(player))
        placeholders = ','.join('?' for _ in identities)
        # Provider IDs are useful lookup keys, not permission to borrow another
        # player's row if an upstream identity was reused or imported wrongly.
        latest = next((row for row in db.execute(
            'SELECT player,team,date FROM player_games WHERE player_id IN (' +
            placeholders + ') AND date<? ORDER BY date DESC,updated_at DESC',
            (*sorted(identities), cutoff))
            if normalized_name(row['player']) == normalized_name(player)), None)
        if not latest:
            result['reason'] = f'I have no stored observations for {player} before this game.'
            result['fallback_answer'] = result['reason']
            return result
        player = latest['player']
        team = latest['team']
        rows = [dict(row) for row in db.execute(
            'SELECT p.* FROM player_games p JOIN games g ON g.id=p.game_id '
            'WHERE p.player_id IN (' + placeholders + ') AND p.team=? AND p.season=? '
            'AND p.season_type=? AND p.date<? AND g.date=p.date AND g.status=? '
            'AND lower(p.appearance_state)=? ORDER BY p.date DESC,p.game_id DESC,p.updated_at DESC LIMIT 100',
            (*sorted(identities), team, season, phase, cutoff, 'final', 'played'))
            if normalized_name(row['player']) == normalized_name(player)]
    result.update(player=player, team=team)
    if pick.get('team') and canonical_team(pick['team']) != team:
        result['reason'] = f'The requested team conflicts with {player}’s latest stored team ({team}).'
        result['fallback_answer'] = result['reason']
        return result
    components = STAT_COMPONENTS[stat]
    groups = {}
    for row in rows:
        identity = (row['game_id'], normalized_name(row['player']))
        groups.setdefault(identity, []).append(row)
    valid, conflicts, uncertain_workload = [], 0, 0
    for candidates in list(groups.values())[:30]:
        complete = []
        for row in candidates:
            values = [_number(row.get(component)) for component in components]
            if any(value is None for value in values) or row.get('minutes') == 0:
                continue
            complete.append((row, tuple(values)))
        if not complete:
            continue
        if len({values for _, values in complete}) > 1:
            conflicts += 1
            continue
        # A complete alias can supply an observation missing from the other
        # import. Conflicting complete statistics cannot be cherry-picked.
        row, values = complete[0]
        row = dict(row)
        known_minutes = {_number(item.get('minutes')) for item, _ in complete}
        known_minutes.discard(None)
        if len(known_minutes) > 1:
            row['minutes'] = None
            uncertain_workload += 1
        elif known_minutes:
            row['minutes'] = next(iter(known_minutes))
        if len({item.get('opponent') for item, _ in complete}) > 1:
            row['opponent'] = None
        row['value'] = sum(values)
        valid.append(row)
    if not valid:
        result['reason'] = f'No complete {MARKET_NAMES[stat]} observations are available for {player} in this season and team.'
        result['fallback_answer'] = result['reason']
        return result
    values = [row['value'] for row in valid]
    n = len(values)
    mean, median = statistics.fmean(values), statistics.median(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    above, below, ties = (sum(value > line for value in values),
                          sum(value < line for value in values), sum(value == line for value in values))
    sample = {'n': n, 'values': values, 'more': above, 'less': below, 'ties': ties,
              'mean': _round(mean), 'median': _round(median), 'stdev': _round(sd),
              'minimum': min(values), 'maximum': max(values),
              'first_date': min(row['date'] for row in valid), 'last_date': valid[0]['date'],
              'excluded_incomplete': min(len(groups), 30) - n - conflicts,
              'conflicting_games': conflicts, 'uncertain_workload_games': uncertain_workload,
              'scope': 'Current-season same-team final played games only.'}
    result.update(available=True, sample=sample, confidence='low')
    if conflicts:
        result['cautions'].append('Conflicting duplicate game statistics were excluded from the sample.')
    if uncertain_workload:
        result['cautions'].append('Conflicting duplicate minutes were treated as unknown workload.')
    event = _event(store, day.isoformat(), team, now)
    readiness = assess_pregame_freshness(store, day.isoformat(), team, now, player=player, stat=stat)
    result['data_readiness'] = readiness
    if not readiness.get('ready'):
        result['cautions'].extend(readiness.get('reasons') or ['Current game-data readiness is unverified.'])
    opponent = canonical_team(pick.get('opponent')) if pick.get('opponent') else event.get('opponent')
    result.update(event=event, opponent=opponent)
    if opponent and event.get('opponent') and opponent != event['opponent']:
        result['cautions'].append('The requested opponent conflicts with the saved game schedule.')
        result['event_conflict'] = True
    opponents = [row['value'] for row in valid if opponent and row.get('opponent') == opponent]
    result['opponent_sample'] = {'team': opponent, 'n': len(opponents),
        'mean': _round(statistics.fmean(opponents)) if opponents else None,
        'median': _round(statistics.median(opponents)) if opponents else None,
        'quantitative_adjustment': False}
    result['opponent_defense'] = _opponent_defense(store, opponent, season, phase, stat, day, now)
    minutes_rows = [row for row in valid if _number(row.get('minutes')) is not None and row['minutes'] > 0]
    workload = {'observed_games': len(minutes_rows), 'reference_minutes': None,
                'recent_minutes': [row['minutes'] for row in minutes_rows[:5]], 'unstable': False}
    reference = None
    if len(minutes_rows) >= 3:
        recent = minutes_rows[:10]
        minute_values = [row['minutes'] for row in recent]
        reference_minutes = statistics.median(minute_values[:5])
        rate = _weighted([row['value'] / row['minutes'] for row in recent])
        reference = reference_minutes * rate
        workload.update(reference_minutes=_round(reference_minutes), per_minute_rate=round(rate, 4),
            reference_value=_round(reference), lower_minutes=_round(_quantile(minute_values, .25)),
            higher_minutes=_round(_quantile(minute_values, .75)),
            lower_workload_value=_round(_quantile(minute_values, .25) * rate),
            higher_workload_value=_round(_quantile(minute_values, .75) * rate),
            note='Historical workload sensitivity, not a confirmed minutes forecast or probability interval.')
        if len(minute_values) >= 6:
            recent_minutes, prior_minutes = statistics.fmean(minute_values[:3]), statistics.fmean(minute_values[3:6])
            workload['unstable'] = abs(recent_minutes / prior_minutes - 1) > .2
            if workload['unstable']:
                result['cautions'].append('Recent playing time differs materially from the preceding games; the role may be changing.')
    result['workload'] = workload
    volume = {}
    for key in ('fga', 'tpa'):
        observed = [_number(row.get(key)) for row in valid[:5]]
        observed = [value for value in observed if value is not None]
        if observed:
            volume[key] = {'n': len(observed), 'mean': _round(statistics.fmean(observed)), 'values': observed}
    result['shot_volume'] = volume
    sources = list(dict.fromkeys(str(row.get('source') or '') for row in valid))
    result['sources'] = [source for source in sources if source]
    age = (day - dt.date.fromisoformat(sample['last_date'])).days
    result['sample_age_days'] = age
    availability = {'blocked': False, 'note': None, 'reports': []}
    if day >= today:
        availability = player_availability(store.recent_news(150), player, day.isoformat(), now, store=store)
    result['availability'] = availability
    if availability.get('note'):
        result['cautions'].append(availability['note'])
    result['availability_confirmed'] = False
    result['cautions'].append('Stored news is not a complete current injury or minutes report.')
    if result['opponent_defense'].get('coverage_unknown'):
        result['cautions'].append('The opponent-defense snapshot has an unknown covered-game date; no projection adjustment was applied.')
    if age > 7:
        result['cautions'].append('The latest relevant observation is more than seven days before the requested game.')
    if not all(sources):
        result['cautions'].append('Some sample observations have no recorded source.')
    if n < 10:
        result['cautions'].append('This is a small early-season sample; hit counts are not this game’s win probability.')
    if not opponent:
        result['cautions'].append('The opponent is unresolved; no matchup adjustment was made.')
    elif len(opponents) < 3:
        result['cautions'].append(f'The sample against {opponent} is too small for a quantitative matchup adjustment.')
    # Current-season hit counts remain descriptive. Decisions use exactly the
    # same benchmark and offer rule as /picks and screenshot comparisons.
    baseline = projection_for_leg(store, {'player': player, 'team': team, 'stat': stat,
        'date': day.isoformat(), 'opponent': opponent}, now)
    if phase != 'Regular Season':
        baseline = {'available': False, 'reason': 'The 20-game strategy is restricted to regular-season games.'}
    offer_decision = baseline_offer_decision(baseline, line)
    result.update(baseline=baseline, offer_decision=offer_decision)
    lean = offer_decision['side']
    result['research_lean'] = lean
    blockers = []
    if not baseline.get('available'):
        blockers.append(baseline.get('reason', 'The 20-game benchmark is unavailable.'))
    if len(minutes_rows) < min(n, 5):
        blockers.append('too few observations with known playing time')
    if age > 7:
        blockers.append('stale game observations')
    if not all(sources):
        blockers.append('missing source provenance')
    if workload['unstable']:
        blockers.append('unstable recent playing time')
    if availability.get('blocked') or availability.get('note'):
        blockers.append('unresolved reported availability or minutes')
    if result.get('event_conflict') or event.get('ambiguous'):
        blockers.append('unresolved game identity')
    if event.get('started_or_unavailable') or day < today:
        blockers.append('the game has started, passed, or is unavailable for pregame analysis')
    elif not event.get('pregame'):
        blockers.append('a saved upcoming game and tipoff have not been verified')
    if not readiness.get('ready'):
        blockers.extend((readiness.get('reasons') or ['current game-data readiness is unverified'])[:2])
    if blockers:
        result['reason'] = 'Pass for now: ' + '; '.join(blockers) + '.'
    elif not lean:
        result['reason'] = 'Pass: ' + offer_decision['reason']
    else:
        result['decision'] = lean
        result['reason'] = ('Tentative ' + lean.title() + ': ' + offer_decision['reason'] +
                            ' Current playing-time and availability checks still apply.')
    result['reasons'].append(result['reason'])
    result['conditional_risks'] = [
        'Reduced minutes, including a possible blowout or foul trouble, can lower both shot attempts and counting statistics; no blowout is forecast here.',
        'A higher payout does not establish better value; the actual payout and joint probabilities are needed.',
        'Multiple legs from one game or player can move together; historical hit counts must not be multiplied into entry odds.',
    ]
    if stat == 'tpm':
        result['conditional_risks'].append('Made threes depend on both three-point attempts and shooting accuracy; motivation alone does not predict either.')
    elif stat in {'pts', 'pra', 'pr', 'pa'}:
        result['conditional_risks'].append('Scoring depends on shot opportunities, shooting accuracy and free throws; a weaker opponent can help efficiency while a blowout can reduce minutes.')
    elif stat in {'fga', 'tpa'}:
        result['conditional_risks'].append('Attempts depend on minutes and shot share rather than whether shots go in; a lineup or role change can alter volume.')
    else:
        result['conditional_risks'].append('Minutes, opportunities and role changes can affect this market; no unmeasured role change is assumed.')
    result['evidence_cards'] = discussion_evidence(result)
    result['fallback_answer'] = format_pick_discussion(result)
    return result


def discussion_evidence(report):
    """Small approved factual units for natural, numerically checked prose."""
    player, stat, line = report['player'], report['stat'], report['line']
    metric = MARKET_NAMES[stat]
    sample = report['sample']
    scope = (f"{report['season']} {report['season_type']}, {report['team']}, "
             f"stored observations through {sample['last_date']}")
    common = {'entity': player, 'metric': metric, 'scope': scope}
    facts = [
        {**common, 'fact_type': 'line', 'text': f'{player}: the user-provided line is {line:g} {metric}.', 'values': [line]},
        {**common, 'fact_type': 'sample', 'text': f"{player}: above {line:g} {metric} in {sample['more']} of {sample['n']} stored games, below in {sample['less']}, tied in {sample['ties']}.",
         'values': [line, sample['more'], sample['n'], sample['less'], sample['ties']]},
        {**common, 'fact_type': 'average', 'text': f"{player}: the sample average is {sample['mean']:g} {metric}.", 'values': [sample['mean']]},
        {**common, 'fact_type': 'median', 'text': f"{player}: the sample median is {sample['median']:g} {metric}.", 'values': [sample['median']]},
        {**common, 'fact_type': 'recent_values', 'text': f"{player}: recent {metric}, newest first: " + ', '.join(f'{v:g}' for v in sample['values'][:6]) + '.',
         'values': sample['values'][:6]},
    ]
    if stat != 'minutes':
        threshold = math.floor(line) + 1
        facts.append({**common, 'fact_type': 'line', 'text': f'{player}: More than {line:g} {metric} needs at least {threshold} {metric}.',
                      'values': [line, threshold]})
    source = 'Stored player game observations; ' + scope + '.'
    cards = [{'id': 'P1', 'kind': 'prop_sample', 'text': ' '.join(f['text'] for f in facts),
              'source': source, 'entity': player, 'data_through_date': sample['last_date'], 'approved_facts': facts}]
    baseline = report.get('baseline', {})
    if baseline.get('available'):
        baseline_scope = (f"Latest {baseline['n']} same-team regular-season observations, "
                          f"{', '.join(baseline['seasons'])}; through {baseline['last_date']}")
        baseline_facts = [{'entity': player, 'metric': metric, 'scope': baseline_scope,
            'fact_type': 'projection',
            'text': (f"{player}: the {baseline['n']}-game baseline average is {baseline['reference_projection']:g} "
                     f"{metric}; median {baseline['baseline_median']:g}. This is the selection benchmark, "
                     'not a calibrated prediction or win probability.'),
            'values': [baseline['n'], baseline['reference_projection'], baseline['baseline_median']]}]
        cards.append({'id': 'P5', 'kind': 'prop_baseline', 'entity': player,
            'source': 'Stored game observations; ' + baseline_scope,
            'data_through_date': baseline['last_date'], 'text': baseline_facts[0]['text'],
            'approved_facts': baseline_facts})
    workload = report['workload']
    if workload.get('reference_minutes') is not None:
        wf = [
            {**common, 'fact_type': 'workload', 'metric': 'minutes',
             'text': f"{player}: the recent median workload is {workload['reference_minutes']:g} minutes.",
             'values': [workload['reference_minutes']]},
            {**common, 'fact_type': 'projection',
             'text': f"{player}: the historical workload reference is {workload['reference_value']:g} {metric}, with {workload['lower_workload_value']:g} at the lower observed workload and {workload['higher_workload_value']:g} at the higher observed workload. These are scenarios, not a calibrated forecast.",
             'values': [workload['reference_value'], workload['lower_workload_value'], workload['higher_workload_value']]},
        ]
        cards.append({'id': 'P2', 'kind': 'prop_workload', 'text': ' '.join(f['text'] for f in wf),
                      'source': source, 'entity': player, 'data_through_date': sample['last_date'], 'approved_facts': wf})
    relevant_risks = [report['conditional_risks'][-1], report['conditional_risks'][0]]
    compact_assessment = report['reason']
    for sentence in relevant_risks:
        if len(compact_assessment) + len(sentence) + 1 <= 600:
            compact_assessment += ' ' + sentence
    cards.append({'id': 'P3', 'kind': 'prop_assessment', 'entity': player, 'source': source,
                  'decision': report['decision'],
                  'data_through_date': sample['last_date'], 'text': compact_assessment,
                  'approved_facts': []})
    defense = report.get('opponent_defense', {})
    if defense.get('available'):
        defense_scope = (f"{defense['season']} {defense['season_type']}, all opponents; covered through "
                         f"{defense.get('data_through_date') or 'unknown'}, published "
                         f"{defense.get('published_at') or 'unknown'}")
        df = [{'entity': defense['team'], 'metric': item['label'], 'scope': defense_scope,
               'fact_type': 'context', 'text': f"{defense['team']}: {item['value']:g} {item['label']} in the published snapshot.",
               'values': [item['value']]} for item in defense['metrics']]
        df.append({'entity': defense['team'], 'metric': 'coverage', 'scope': defense_scope,
                   'fact_type': 'context', 'text': defense['team'] + ': ' + defense['note'], 'values': []})
        cards.append({'id': 'P4', 'kind': 'opponent_team_context', 'entity': defense['team'],
                      'source': defense['source'], 'data_through_date': defense.get('data_through_date'),
                      'published_at': defense.get('published_at'),
                      'text': ' '.join(f['text'] for f in df) + ' ' + defense_scope + '. ' + defense['scope'] + ' ' + defense['note'],
                      'approved_facts': df})
    return cards


def format_pick_discussion(report, question=''):
    """Useful non-AI fallback; no raw evidence dump or unverifiable prose."""
    if not report.get('available'):
        return report.get('reason') or 'I need a player, full statistic and line to assess the pick.'
    sample = report['sample']
    metric = MARKET_NAMES[report['stat']]
    label = f"{report['player']} {report['line']:g} {metric}"
    if report['decision'] == 'pass':
        first = f"{label} — {report['reason']}"
    else:
        first = f"I lean {report['decision'].title()} on {label}, with low confidence."
    evidence = (f"In {sample['n']} stored games: {sample['more']} above, {sample['less']} below"
                + (f", {sample['ties']} tied" if sample['ties'] else '')
                + f"; median {sample['median']:g}.")
    baseline = report.get('baseline', {})
    if baseline.get('available'):
        evidence = (f"20-game baseline: average {baseline['reference_projection']:g}, "
                    f"median {baseline['baseline_median']:g}; same team, "
                    f"{', '.join(baseline['seasons'])}.\nCurrent-season context: " + evidence)
    question = str(question).casefold()
    if question and baseline.get('available'):
        evidence = (f"{baseline['n']}-game same-team baseline: average {baseline['reference_projection']:g}, "
                    f"median {baseline['baseline_median']:g} {metric}, through {baseline['last_date']}.")
    if question and report['decision'] == 'pass' and not report.get('data_readiness', {}).get('ready', True):
        reasons = report.get('data_readiness', {}).get('reasons') or []
        if reasons:
            first = f"{label} — Pass for now: {reasons[0]}"
    if re.search(r'\b(?:what (?:is|does)|explain|define)\b.*\bpra\b|\bpra\b.*\bmean\b', question) and report['stat'] == 'pra':
        return ('PRA means points plus rebounds plus assists. Each contributes to the combined total; '
                'it is a different market from points alone. Your supplied line remains '
                f"{report['line']:g} PRA.")
    if report['decision'] != 'pass':
        risk = ('Made threes depend on both attempts and shooting accuracy.' if report['stat'] == 'tpm'
                else 'Reduced minutes or a role change could overturn this lean.')
    else:
        risk = 'Those results describe the sample, not this game’s win probability.'
    if re.search(r'blowout|bench|fewer minutes|less minutes|playing time', question):
        risk = ('If a blowout or rotation change reduces his minutes, both attempts and counting stats can fall. '
                'I’m not predicting that he will be benched.')
    elif re.search(r'revenge|old team|motivat', question):
        risk = ('Facing his old team might matter to him, but motivation alone does not establish more shot '
                'attempts or better shooting accuracy.')
    elif re.search(r'change your mind|would change|what changes', question):
        risk = ('I’d reassess if the actual line changes, his role or minutes change, or current game and '
                'availability checks fail. None of those changes is assumed here.')
    elif re.search(r'confiden|chance|probabil|how sure', question):
        risk = ('Confidence is low. Historical averages and hit counts do not give a calibrated chance '
                'of this pick winning; minutes and role can change.')
    elif re.search(r'why|reason|what makes|concern|go wrong', question):
        if baseline.get('available') and report['decision'] != 'pass':
            first += ' The historical benchmark supports that side under the same rule used for the shortlist.'
        risk = ('Made threes depend on both attempts and shooting accuracy; a minutes reduction can hurt either.'
                if report['stat'] == 'tpm' else
                'The main risk is a change in minutes or role; a historical average does not lock in this game’s result.')
    return (first + '\n' + evidence + '\n' + risk +
            f" Source: stored player games through {sample['last_date']}; current availability is not fully verified.")
