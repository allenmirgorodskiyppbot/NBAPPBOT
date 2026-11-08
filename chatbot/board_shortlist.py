"""Read-only, concise research decisions over uploaded lines and saved evidence.

The shortlist never creates a wager or invents a sportsbook line. Additional
players are a watchlist for obtaining actual offers, not synthetic selections.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from collections import defaultdict
from zoneinfo import ZoneInfo

from feeds import TEAM_WORDS, canonical_player, canonical_team
from contextual_projection import baseline_offer_decision, projection_read_scope
from news_digest import _aware, _clean, _metadata, is_promotion
from season_calendar import calendar_context, current_season
from lineup_selection import select_lineup
from pregame_freshness import assess_pregame_freshness

EASTERN = ZoneInfo('America/New_York')
MARKET_NAMES = {
    'pts': 'points', 'reb': 'rebounds', 'ast': 'assists', 'tpm': '3-pointers made',
    'fga': 'FG attempts', 'tpa': '3-point attempts', 'tov': 'turnovers',
    'pra': 'PTS+REB+AST', 'pr': 'PTS+REB', 'pa': 'PTS+AST', 'ra': 'REB+AST',
    'stl': 'steals', 'blk': 'blocks', 'stocks': 'steals+blocks',
    'oreb': 'offensive rebounds', 'dreb': 'defensive rebounds', 'minutes': 'minutes',
}
_OUT = re.compile(r'\b(?:ruled out|downgraded to out|will not play|won.t play|'
                  r'will miss|suspended|out for|out with|out indefinitely|not available|'
                  r'unavailable|not playing)\b|^out\b', re.I)
_UNCERTAIN = re.compile(r'\b(?:questionable|doubtful|game.time decision|minutes? restriction|'
                        r'minutes? limit|likely to play|probable)\b', re.I)
_AVAILABLE = re.compile(r'\b(?:cleared to play|will play|available|no longer listed|'
                        r'removed from.*injury report)\b', re.I)
_DAYS = {day.casefold(): number for number, day in enumerate(
    ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'))}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _now(value=None):
    if isinstance(value, str):
        value = _aware(value)
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _key(leg):
    return (canonical_player(leg.get('player', '')), str(leg.get('date') or leg.get('game_id') or '')[:80],
            str(leg.get('stat', '')))


def _variant(leg):
    value = str(leg.get('offer_variant') or 'unknown').strip().lower()
    return value if value in {'standard', 'goblin', 'demon', 'unknown'} else 'unknown'


def _offer_key(leg):
    return (*_key(leg), _number(leg.get('line')), str(leg.get('side', '')).lower())


def _offer_label(value):
    return {'standard': 'standard offer', 'goblin': 'Goblin offer',
            'demon': 'Demon offer', 'unknown': 'offer type unverified'}.get(value, 'offer type unverified')


def _subject_text(item, player):
    """A name mentioned in another player's report is not its injury subject."""
    title = _clean(item.get('title'), 400)
    name = re.escape(player)
    match = re.match(r'^' + name + r'\s*:\s*(.*)$', title, re.I)
    if match:
        return match.group(1), title + ' ' + _clean(item.get('body'), 1800)
    match = re.match(r'^' + name + r'\s+(?:is\s+|has\s+been\s+)?'
                     r'(ruled out\b|downgraded to out\b|will not play\b|won.t play\b|'
                     r'will miss\b|out for\b|questionable\b|doubtful\b|cleared to play\b|'
                     r'will play\b|available\b)(.*)$', title, re.I)
    if match:
        return match.group(1) + match.group(2), title + ' ' + _clean(item.get('body'), 1800)
    return None


def _news_player_availability(news, player, date, now):
    """Return publisher-reported cautions; never turn collection time into proof."""
    target = dt.date.fromisoformat(date)
    relevant = []
    for item in news:
        subject = _subject_text(item, player)
        if not subject or is_promotion(item):
            continue
        status_text, text = subject
        stamp = _aware(item.get('published_at'))
        if stamp and not -300 <= (now - stamp).total_seconds() <= 48 * 3600:
            continue
        # Even without a timezone, reject a plainly old publisher calendar date.
        wall_date = None
        try:
            wall_date = dt.date.fromisoformat(str(item.get('published_at'))[:10])
        except (ValueError, TypeError):
            pass
        if not stamp and (not wall_date or abs((target - wall_date).days) > 1):
            continue
        metadata = _metadata(item)
        explicit_date = str(item.get('game_date') or metadata.get('game_date') or '')[:10]
        if explicit_date and explicit_date != date:
            continue
        if not explicit_date and re.search(r'\bnext\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b',
                                           text, re.I):
            continue
        day_names = r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
        # Short player headlines commonly say "Out Friday". Prefer the
        # subject's own headline date; an unrelated weekday in the article body
        # must not override it. Body-only dates need an explicit game phrase.
        weekdays = re.findall(r'\b' + day_names + r'\b', status_text, re.I)
        if not weekdays:
            weekdays = re.findall(r'\b' + day_names + r"(?:['’]s)?\s+(?:game|matchup)\b", text, re.I)
        if weekdays and any(_DAYS[day.casefold()] != target.weekday() for day in weekdays):
            continue
        publication_day = stamp.astimezone(EASTERN).date() if stamp else wall_date
        relative_day = re.search(r'\b(?:for\s+)?(tomorrow|tonight|today)\b', status_text, re.I)
        if relative_day and publication_day:
            named_day = publication_day + dt.timedelta(days=relative_day.group(1).casefold() == 'tomorrow')
            if named_day != target:
                continue
        target_matched = bool(explicit_date == date or weekdays or relative_day or publication_day == target)
        if not target_matched:
            continue
        # Negated or conditional absence cannot become a confirmed-out decision.
        negated = re.search(r'\b(?:not|never|no longer)\s+(?:yet\s+)?(?:ruled out|out|doubtful)|'
                            r'\b(?:could|may|might)\s+(?:be\s+)?(?:ruled out|out|miss)', status_text, re.I)
        uncertain_clearance = re.search(r'\b(?:not|never)\s+(?:yet\s+)?cleared\b|'
                                       r'\b(?:awaiting|pending)\s+(?:medical\s+)?clearance\b', status_text, re.I)
        conditional_clearance = re.search(r'\b(?:could|may|might|likely|expected|hope\w*|if|potentially|possibly)\b',
                                         status_text, re.I)
        conditional_absence = re.search(r'\b(?:could|may|might|if|potentially|possibly)\b', status_text, re.I)
        non_game_absence = (re.search(r'\b(?:practice|shootaround|media session|warmup)\b', status_text, re.I)
                           and not re.search(r'\b(?:game|matchup)\b', status_text, re.I))
        state = ('unresolved_report' if uncertain_clearance else
                 'out_report' if _OUT.search(status_text) and not negated and not non_game_absence and not conditional_absence else
                 'uncertain' if _UNCERTAIN.search(status_text) else
                 'available_report' if _AVAILABLE.search(status_text) and not conditional_clearance else None)
        if not state:
            continue
        relevant.append({'state': state, 'published_at': item.get('published_at'),
                         'source': _clean(item.get('source'), 70) or 'publisher',
                         'title': _clean(item.get('title'), 180), 'url': item.get('url'),
                         'timing_confirmed': stamp is not None, '_stamp': stamp})
    # Only compare reliable instants. An untimed absence remains an unresolved
    # caution rather than being silently overridden by a different clock basis.
    timed = sorted((item for item in relevant if item['_stamp']), key=lambda item: item['_stamp'], reverse=True)
    # Equal publication times cannot establish which contradictory report won.
    # Retain every report at that instant so an out/clearance conflict holds the
    # player regardless of RSS ordering.
    latest = [item for item in timed if item['_stamp'] == timed[0]['_stamp']] if timed else []
    untimed = [item for item in relevant if not item['_stamp']]
    if any(item['state'] in {'out_report', 'unresolved_report'} for item in untimed):
        latest += [next(item for item in untimed if item['state'] in {'out_report', 'unresolved_report'})]
    elif not latest and untimed:
        latest = untimed[:1]
    reports = [{k: v for k, v in item.items() if k != '_stamp'} for item in latest]
    blocked = next((item for item in reports if item['state'] in {'out_report', 'unresolved_report'}), None)
    caution = next((item for item in reports if item['state'] == 'uncertain'), None)
    if blocked:
        if blocked['state'] == 'unresolved_report':
            reason = 'Hold: publisher reports unresolved playing clearance; verify availability first'
        else:
            reason = ('Hold: publisher reports this player out for the target game' if blocked['timing_confirmed'] else
                      'Hold: an out report has an unconfirmed timezone; verify availability first')
        return {'blocked': True, 'note': reason + f" ({blocked['source']}).", 'reports': reports}
    if caution:
        clock = '' if caution['timing_confirmed'] else '; timezone unconfirmed'
        return {'blocked': False, 'note': f"Availability caution: {caution['title']} ({caution['source']}{clock}).", 'reports': reports}
    return {'blocked': False, 'note': None, 'reports': reports}


def player_availability(news, player, date, now, *, store=None):
    """Merge exact official designations with dated news, preserving cautions.

    Omitted stores keep the original news-only contract. An official Available
    designation says nothing about a starting role or unrestricted minutes.
    """
    news_result = _news_player_availability(news, player, date, now)
    if store is None:
        return news_result
    from structured_availability import current_official_availability
    official = current_official_availability(store, player, date, now, max_age_seconds=6*3600)
    result = {**news_result, 'official_designation': official,
              'official_designation_known': bool(official.get('known')),
              'confirmed_starter': None, 'expected_minutes': None, 'minutes_restriction': None}
    if not official.get('known'):
        return result
    stamp = _aware(official['report_at'])
    designation = official['status']
    source = {'state': 'official_' + designation.lower(),
              'source': 'Official NBA injury report', 'source_kind': 'official_report',
              'title': player + ': ' + designation, 'url': official['source_url'],
              'published_at': official['report_at'], 'report_at': official['report_at'],
              'timing_confirmed': True, 'game_date': date}
    result['reports'] = [source, *news_result['reports']]
    if designation in {'Out', 'Questionable', 'Doubtful'}:
        detail = ('the official NBA report lists this player Out' if designation == 'Out' else
                  'the official NBA report lists this player ' + designation + '; wait for updated playing clearance')
        return {**result, 'blocked': True,
                'note': 'Hold: ' + detail + f" (as of {official['report_at']})."}
    if designation == 'Available':
        # Reliable later adverse reports, untimed cautions and actual reported
        # minutes limits cannot disappear behind an older Available snapshot.
        adverse = []
        for report in news_result['reports']:
            report_stamp = _aware(report.get('published_at'))
            restriction = re.search(r'\bminutes?\s+(?:restriction|limit)\b', report.get('title', ''), re.I)
            if (report.get('state') in {'out_report', 'unresolved_report', 'uncertain'}
                    and (not report_stamp or report_stamp >= stamp or restriction)):
                adverse.append(report)
        if adverse:
            caution = adverse[0]
            return {**result, 'blocked': True, 'official_superseded_or_limited': True,
                    'note': 'Hold: a later or unresolved publisher availability/minutes report requires verification: '
                            + caution['title'] + f" ({caution['source']})."}
        # An explicit later official clearance can replace an older timed out
        # headline. It still does not confirm a lineup or unlimited minutes.
        return {**result, 'blocked': False, 'note': None}
    # Probable is an official caution, not a clearance that erases an out report.
    if news_result['blocked'] or news_result['note']:
        return result
    return {**result, 'note': 'Availability caution: the official NBA report lists this player '
            + designation + f" (as of {official['report_at']}); verify playing status and minutes."}


def _event(store, leg, now):
    date = str(leg.get('date') or '')[:10]
    team = canonical_team(leg.get('team', ''))
    requested_id = leg.get('game_id')
    with store.connect() as db:
        if requested_id:
            observed = [dict(row) for row in db.execute('SELECT * FROM games WHERE id=?', (requested_id,))]
            scheduled = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE id=?', (requested_id,))]
        elif date and team:
            observed = [dict(row) for row in db.execute('SELECT * FROM games WHERE date=? '
                      'AND (home_team=? OR away_team=?)', (date, team, team))]
            scheduled = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE date=? '
                      'AND (home_team=? OR away_team=?)', (date, team, team))]
        else:
            observed, scheduled = [], []
    # Match the exact event across sources. A date/team fallback must never
    # silently turn an explicitly requested unknown event into a different game.
    by_id = {event['id']: event for event in scheduled}
    for event in observed:
        by_id[event['id']] = {**by_id.get(event['id'], {}),
                             **{key: value for key, value in event.items() if value is not None}}
    events = list(by_id.values())
    blocked_status = any(event.get('status') in {
        'postponed', 'canceled', 'cancelled', 'suspended', 'live', 'final', 'post', 'completed', 'past_schedule_only'
    } for event in [*scheduled, *observed])
    if requested_id and not events:
        return {}, 'The specified game is not in the saved schedule or game records.'
    if len(events) > 1:
        return {}, 'More than one stored game matches this line; the exact event is unresolved.'
    event = events[0] if events else {}
    if event:
        if ((date and event.get('date') != date) or
                (team and team not in {event.get('home_team'), event.get('away_team')})):
            return {}, 'The uploaded team/date conflicts with the specified game.'
        date = date or str(event.get('date') or '')[:10]
    try:
        day = dt.date.fromisoformat(date)
    except (ValueError, TypeError):
        return {}, 'Game date is unresolved.'
    if day < now.astimezone(EASTERN).date():
        return {}, 'The game date has passed.'
    context = calendar_context(store, date, team) if team else {}
    if event:
        # Calendar rest context can accompany this same event, but its older
        # clock/status cannot replace a later observed event update.
        if context.get('game_id') != event.get('id'):
            context = {}
        context = {**context, 'game_id': event.get('id'), 'tipoff': event.get('start_time'),
                   'status': event.get('status'), 'source': event.get('source'),
                   'opponent': (event.get('away_team') if event.get('home_team') == team else event.get('home_team'))
                   if team else None}
    context = {**context, 'date': date}
    opponent = canonical_team(leg.get('opponent'))
    if opponent and context.get('opponent') and opponent != canonical_team(context['opponent']):
        return context, 'The uploaded opponent conflicts with the specified game.'
    if blocked_status or context.get('status') in {'postponed', 'canceled', 'cancelled', 'suspended', 'live', 'final', 'post', 'completed', 'past_schedule_only'}:
        return context, 'The stored game is postponed, canceled or already started.'
    tip = _aware(context.get('tipoff'))
    if context.get('status') != 'scheduled' or not tip:
        return context, 'A scheduled game with a known tipoff is required; event timing is unresolved.'
    if tip.astimezone(EASTERN).date() != day:
        return context, 'The saved tipoff conflicts with this game date; confirm the current event.'
    if tip and tip <= now:
        return context, 'The saved tipoff has passed; this is no longer a pregame line.'
    return context, None


def _reason(item):
    p = item['projection']
    value = _number(p.get('reference_projection'))
    minutes = _number(p.get('projected_minutes'))
    last = _clean(p.get('last_date') or item.get('sample_last_date'), 10)
    base = f"20-game same-team mean {value:g}"
    if minutes is not None:
        base += f"; recent workload {minutes:g} minutes (context only)"
    base += f"; {int(p.get('n') or item.get('n') or 0)} saved games through {last}."
    opponent = next((c for c in p.get('cohorts', []) if c.get('type') == 'opponent_team'), None)
    if opponent:
        base += f" {opponent.get('team')}: {opponent.get('n', 0)} comparable games; no matchup adjustment."
    seasons = p.get('seasons') or item.get('sample_seasons') or []
    if len(seasons) > 1:
        base += ' Sample spans seasons.'
    return base


def _master_comparison(store, item, context, hold_reason, now, team_cache):
    """Describe measured prior games without changing an offer's eligibility."""
    leg = dict(item.get('leg') or {})
    team = canonical_team(leg.get('team'))
    date = str(leg.get('date') or context.get('date') or '')[:10]
    if (not context.get('game_id') or team not in TEAM_WORDS
            or canonical_team(context.get('opponent')) not in TEAM_WORDS):
        return None
    try:
        cutoff = min(dt.date.fromisoformat(date), now.astimezone(EASTERN).date()).isoformat()
    except ValueError:
        return None
    identity = (canonical_player(leg.get('player', '')), cutoff)
    if identity not in team_cache:
        from career_identity import resolve_career_identity
        aliases = resolve_career_identity(store, leg['player'])['aliases']
        with store.connect() as db:
            rows = list(db.execute('SELECT p.team,p.date FROM player_games p JOIN games g ON g.id=p.game_id '
                'WHERE p.player IN (' + ','.join('?' for _ in aliases) + ') AND p.date<? '
                "AND g.date=p.date AND g.status='final' AND p.team IN (g.home_team,g.away_team) "
                'ORDER BY p.date DESC,p.updated_at DESC LIMIT 20', (*aliases, cutoff))) if aliases else []
        latest = rows[0]['date'] if rows else None
        team_cache[identity] = {canonical_team(row['team']) for row in rows if row['date'] == latest}
    if team_cache[identity] != {team}:
        return None
    projection = item.get('projection') or {}
    observed = projection.get('source_games') or item.get('sample_games') or []
    # These rows come from the engine's exact-name/ID, final-game joins. Keep
    # their dates, teams and measured values attached; absent fields stay absent.
    rows = [row for row in observed[:20] if isinstance(row, dict)]
    if (not rows or any(canonical_team(row.get('team')) != team
            or not str(row.get('date') or '') < cutoff or _number(row.get('value')) is None
            or _number(row['value']) < 0 or not row.get('game_id') for row in rows)):
        return None
    if len({row['game_id'] for row in rows}) != len(rows):
        return None
    values = [float(row['value']) for row in rows]
    mean, median = statistics.fmean(values), statistics.median(values)
    first, last = min(row['date'] for row in rows), max(row['date'] for row in rows)
    line = _number(leg.get('line'))
    benchmark = None
    if line is not None:
        from contextual_projection import _quantile
        benchmark = baseline_offer_decision({'available': len(rows) == 20, 'n': len(rows),
            'reference_projection': mean, 'baseline_median': median,
            'empirical_scenario_range': {'p10': _quantile(values, .1), 'p90': _quantile(values, .9)}}, line)
    return {'leg': leg, 'player': leg['player'], 'team': team, 'date': date,
            'opponent': context['opponent'], 'game_id': context['game_id'], 'stat': leg.get('stat'),
            'line': line, 'n': len(values), 'mean': mean, 'median': median,
            'above': sum(value > line for value in values) if line is not None else None,
            'below': sum(value < line for value in values) if line is not None else None,
            'ties': sum(value == line for value in values) if line is not None else None,
            'source_first_date': first, 'source_data_through_date': last,
            'sample_seasons': sorted({str(row['season']) for row in rows if row.get('season')}),
            'source': 'saved master player games',
            'source_games': [{'game_id': row['game_id'], 'date': row['date'],
                'source': row.get('source'), 'value': row['value'], 'team': row['team'],
                'season': row.get('season')} for row in rows],
            'historical_benchmark': benchmark, 'hold_reason': hold_reason,
            'research_only': True, 'eligible_for_ranking': False, 'approved_for_entry': False}


def _research_choice(value):
    """Choose a readable historical lean without changing live eligibility."""
    offered = set(value.get('offered_sides') or ()) & {'more', 'less'}
    if (not offered or value.get('research_blocked') or value['n'] < 20
            or value.get('statistics_ready') is not True):
        return
    above, below, ties = value['above'], value['below'], value['ties']
    if above is None or below is None or above + below == 0:
        return
    # Engine source rows normally arrive newest first. Sort explicitly because
    # a caller's row order must not silently change the recent-form caution.
    ordered = sorted(value['source_games'], key=lambda row: row['date'], reverse=True)
    recent = ordered[:5]
    recent_values = [row['value'] for row in recent]
    recent_counts = {'more':sum(number > value['line'] for number in recent_values),
                     'less':sum(number < value['line'] for number in recent_values)}
    counts = {'more':above, 'less':below}
    benchmark_side = (value.get('historical_benchmark') or {}).get('side')
    if benchmark_side in offered:
        side, basis = benchmark_side, 'Existing mean/median research buffer'
    elif above != below:
        side = 'more' if above > below else 'less'
        basis = 'More observed games on this side in the saved sample'
    elif recent_counts['more'] != recent_counts['less']:
        side = 'more' if recent_counts['more'] > recent_counts['less'] else 'less'
        basis = 'Saved sample split evenly; recent observed games break the tie'
    elif value['mean'] != value['line']:
        side = 'more' if value['mean'] > value['line'] else 'less'
        basis = 'Observed counts tied; historical mean breaks the tie'
    elif value['median'] != value['line']:
        side = 'more' if value['median'] > value['line'] else 'less'
        basis = 'Observed counts and mean tied; historical median breaks the tie'
    else:
        return
    # A one-sided special offer does not license the unshown opposing choice,
    # or a forced side against all the supplied measured evidence.
    if side not in offered or counts[side] == 0:
        return
    recent_n = len(recent_values)
    current_season = next((row.get('season') for row in ordered if row.get('season')), None)
    season_values = [row['value'] for row in ordered
                     if current_season and row.get('season') == current_season]
    season_count = sum(number > value['line'] if side == 'more' else number < value['line']
                       for number in season_values)
    recent_count = recent_counts[side]
    conflict = bool(recent_n >= 3 and counts[side] > (above + below) / 2
                    and recent_count < recent_n / 2)
    cautious = conflict or benchmark_side != side
    value.update(lean_side=side, selection_basis=basis,
                 support_label='Mixed historical support' if conflict else
                               'Narrow research lean' if cautious else 'Research buffer supported',
                 selected_sample_count=counts[side], recent_n=recent_n, recent_side_count=recent_count,
                 recent_values=recent_values, recent_dates=[row['date'] for row in recent],
                 recent_conflict=conflict, current_sample_season=current_season,
                 current_sample_season_n=len(season_values), current_sample_season_side_count=season_count,
                 research_caution=('Recent observed games run against the longer-sample lean.' if conflict else
                                   'This lean does not clear the stricter mean/median buffer.' if cautious else None))
    value['leg'] = {**value['leg'], 'side': side}


def _research_order(value):
    side = value.get('lean_side')
    fraction = value.get('selected_sample_count', 0) / max(1, value['n'])
    recent_fraction = value.get('recent_side_count', 0) / max(1, value.get('recent_n', 0))
    return (not bool(side), bool(value.get('recent_conflict')),
            -bool(side and (value.get('historical_benchmark') or {}).get('side') == side),
            -(fraction + recent_fraction) / 2, -(value['n'] >= 20),
            -value['n'], value['player'], value['stat'], value['line'] if value['line'] is not None else -1)


def _research_from_master(store, held_items, now, count, required_teams=(), *, team_cache=None,
                          selected=(), constraints=None):
    """Select useful distinct-player comparisons; never call them a lineup."""
    team_cache = {} if team_cache is None else team_cache
    grouped = {}
    for item, leg, context, reason in held_items:
        key = (canonical_player(leg.get('player', '')), leg.get('date'), leg.get('stat'),
               _number(leg.get('line')), leg.get('game_id') or context.get('game_id'), _variant(leg))
        if key in grouped:
            grouped[key]['offered_sides'] = sorted(set(grouped[key]['offered_sides']) | {leg.get('side')})
            grouped[key]['research_blocked'] |= item.get('_research_blocked', False)
            continue
        value = _master_comparison(store, {**item, 'leg': leg}, context, reason, now, team_cache)
        if value:
            value['offered_sides'] = [leg['side']] if leg.get('side') in {'more', 'less'} else []
            freshness = assess_pregame_freshness(store, str(leg.get('date') or context.get('date'))[:10],
                leg.get('team'), now, player=leg.get('player'), stat=leg.get('stat'))
            value.update(statistics_ready=freshness.get('statistics_ready', freshness.get('ready') is True) is True,
                         timing_verified=freshness.get('timing_verified', freshness.get('ready') is True) is True,
                         timing_issues=list(freshness.get('timing_issues') or []),
                         statistics_issues=list(freshness.get('statistics_issues') or []),
                         research_blocked=item.get('_research_blocked', False))
            grouped[key] = value
    for value in grouped.values():
        _research_choice(value)
    comparisons = sorted(grouped.values(), key=_research_order)
    # Account for already-qualified selections when filling the remaining
    # slots. In particular, one remaining slot after three NYK selections must
    # prefer a readable CLE research choice over another NYK player.
    chosen, _ = select_lineup([*selected, *comparisons], len(selected) + count,
        required_teams=required_teams, constraints=constraints)
    comparison_ids = {id(item) for item in comparisons}
    return [item for item in chosen if id(item) in comparison_ids]


def _held_master_watchlist(store, held, now, count, team_cache):
    """Retrieve actual baselines for other saved players; no offer is invented."""
    from contextual_projection import projection_for_leg
    results, seen = [], set()
    for candidate in held[:12]:
        key = canonical_player(candidate.get('player', ''))
        if not key or key in seen:
            continue
        seen.add(key)
        leg = {key: candidate[key] for key in ('player', 'team', 'date', 'opponent', 'game_id') if candidate.get(key)}
        leg['stat'] = 'pts'
        context, issue = _event(store, leg, now)
        if issue:
            continue
        projection = projection_for_leg(store, leg, now)
        value = _master_comparison(store, {'leg': leg, 'projection': projection}, context,
                                   candidate['reason'], now, team_cache)
        if value:
            value['offer_required'] = True
            results.append(value)
        if len(results) >= min(2, count):
            break
    return results


def _watchlist(store, original_legs, news, now, held=None):
    """Rank observed workload/volume for other existing same-game players."""
    uploaded = {canonical_player(leg.get('player', '')) for leg in original_legs}
    contexts = {}
    for leg in original_legs:
        context, issue = _event(store, leg, now)
        team = canonical_team(leg.get('team', ''))
        date = str(leg.get('date') or context.get('date') or '')[:10]
        if issue or not context.get('game_id') or not context.get('opponent'):
            continue
        contexts[(date, team)] = context
        # Only a known saved matchup supplies the other team, never a guessed
        # opponent or whatever teams happen to coexist in an uploaded image.
        contexts[(date, context['opponent'])] = {**context, 'opponent': team}
    choices = []
    season = current_season(now)
    for (date, team), context in list(contexts.items())[:8]:
        cutoff = min(date, now.astimezone(EASTERN).date().isoformat())
        with store.connect() as db:
            players = [dict(row) for row in db.execute(
                'WITH ranked AS (SELECT player_id,player,team,date,ROW_NUMBER() OVER '
                '(PARTITION BY player_id ORDER BY date DESC,updated_at DESC) AS place '
                'FROM player_games WHERE date<? AND season=?) '
                'SELECT * FROM ranked WHERE place=1 AND team=? ORDER BY player_id', (cutoff, season, team))]
        for player in players[:40]:
            if canonical_player(player['player']) in uploaded:
                continue
            if (dt.date.fromisoformat(date) - dt.date.fromisoformat(player['date'])).days > 14:
                continue
            with store.connect() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT p.* FROM player_games p JOIN games g ON p.game_id=g.id "
                    "WHERE p.player_id=? AND p.team=? AND p.date<? AND p.season=? "
                    "AND p.season_type='Regular Season' AND LOWER(p.appearance_state)='played' "
                    "AND g.status='final' AND p.minutes>0 ORDER BY p.date DESC LIMIT 10",
                    (player['player_id'], team, cutoff, season))]
            if len(rows) < 3:
                continue
            recent = rows[:5]
            minutes = statistics.fmean(row['minutes'] for row in recent)
            if minutes < 20:
                continue
            availability = player_availability(news, player['player'], date, now, store=store)
            if availability['blocked']:
                continue
            freshness = assess_pregame_freshness(store, date, team, now, player=player['player'])
            if not freshness['ready']:
                if held is not None:
                    held.append({'player': player['player'], 'team': team,
                                 'date': date, 'opponent': context['opponent'], 'game_id': context['game_id'],
                                 'reason': 'Current game data is not ready: ' + ' '.join(freshness['reasons'])})
                continue
            markets = []
            for stat, normal in (('pts', 15), ('reb', 6), ('ast', 4)):
                if stat not in freshness.get('current_player_stat_coverage', []):
                    continue
                values = [_number(row.get(stat)) for row in recent]
                if any(value is None for value in values):
                    continue
                average = statistics.fmean(values)
                if average < normal * .5:
                    continue
                markets.append((average / normal, stat, average))
            if not markets:
                if held is not None:
                    held.append({'player': player['player'], 'team': team,
                                 'reason': 'No current, complete volume statistic supports a watchlist comparison.'})
                continue
            strength, stat, average = max(markets)
            points = minutes * math.log1p(len(recent)) + strength * 5
            prior = rows[5:]
            shift = None
            if len(prior) >= 3:
                before = statistics.fmean(row['minutes'] for row in prior)
                if before > 0:
                    shift = (minutes / before - 1) * 100
                    points += max(-5, min(10, shift / 2))
            reason = (f"{minutes:.1f} minutes and {average:.1f} {MARKET_NAMES[stat]} per game "
                      f"over {len(recent)} saved {season} appearances through {recent[0]['date']}.")
            if shift is not None and abs(shift) >= 10:
                reason += f" Minutes {shift:+.0f}% versus the previous {len(prior)} observations."
            if len(recent) < 5:
                reason += ' Small sample.'
            choices.append({'player': player['player'], 'team': team, 'date': date,
                            'opponent': context['opponent'], 'stat': stat,
                            'reason': reason, 'caution': availability['note'],
                            'availability': availability, 'source': 'saved master player games',
                            'source_data_through_date': recent[0]['date'], '_rank': points})
    choices.sort(key=lambda item: (-item['_rank'], item['player'], item['date']))
    unique, result = set(), []
    for item in choices:
        key = canonical_player(item['player'])
        if key in unique:
            continue
        unique.add(key)
        result.append({k: value for k, value in item.items() if k != '_rank'})
        if len(result) == 2:
            break
    return result


@projection_read_scope()
def enrich_shortlist(store, report, now=None, *, count=4, constraints=None):
    """Apply presentation/risk gates to engine output without changing saved data."""
    now = _now(now)
    # Validate caller preferences before any optional lookup. Eligibility gates
    # below remain identical regardless of the requested lineup size.
    select_lineup([], count, constraints=constraints)
    constraints = dict(constraints or {})
    preferred_stat = constraints.get('preferred_stat')
    if preferred_stat not in MARKET_NAMES:
        preferred_stat = None
    original = list(report.get('original_legs') or [item.get('leg', {}) for item in report.get('legs', [])])
    news = store.recent_news(200)
    thresholds = defaultdict(set)
    variants = defaultdict(set)
    for leg in original:
        number = _number(leg.get('line'))
        if number is not None:
            thresholds[_key(leg)].add(number)
            variants[_offer_key(leg)].add(_variant(leg))
    skipped, usable, freshness_cache, held_items = [], [], {}, []
    for item in report.get('legs', report.get('candidates', [])):
        leg = dict(item.get('leg', {}))
        leg['offer_variant'] = _variant(leg)
        projection = item.get('projection') or {}
        reason = None
        context, event_issue = _event(store, leg, now)
        if not leg.get('date') and context.get('date'):
            leg['date'] = context['date']
        if event_issue:
            reason = event_issue
        elif len(variants[_offer_key(item.get('leg', {}))]) > 1:
            reason = 'Conflicting offer variants for this exact selection; verify the current card.'
        elif len(thresholds[_key(item.get('leg', {}))]) > 1:
            reason = 'Multiple thresholds for this market; confirm the active offer and its terms.'
        elif not item.get('eligible_for_ranking'):
            reason = item.get('reason') or 'Insufficient eligible observations or unresolved event status.'
        elif not projection.get('available'):
            reason = projection.get('reason') or 'Comparable workload evidence is insufficient.'
        elif (_number(projection.get('reference_projection')) is None or
              _number(projection.get('selected_side_margin')) is None):
            reason = 'No usable projection was produced.'
        elif _number(projection['selected_side_margin']) <= 0:
            reason = 'The experimental reference does not support this side.'
        elif leg.get('side') not in {'more', 'less'} or _number(leg.get('line')) is None:
            reason = 'The line or offered side is unresolved.'
        else:
            try:
                lag = (dt.date.fromisoformat(leg['date'][:10]) - dt.date.fromisoformat(projection['last_date'][:10])).days
                if not 0 <= lag <= 7:
                    reason = 'Comparable workload statistics are more than seven days behind this game.'
            except (ValueError, TypeError, KeyError):
                reason = 'Comparable workload freshness is unresolved.'
        if not reason:
            decision = baseline_offer_decision(projection, leg.get('line'))
            if decision['side'] != leg.get('side'):
                reason = decision['reason'] if decision['side'] is None else 'The benchmark supports the opposite offered side.'
        freshness = None
        if not reason:
            key = (leg.get('date'), leg.get('team'), leg.get('player'), leg.get('stat'))
            if key not in freshness_cache:
                freshness_cache[key] = assess_pregame_freshness(store, leg['date'][:10],
                    leg.get('team'), now, player=leg.get('player'), stat=leg.get('stat'))
            freshness = freshness_cache[key]
            if not freshness['ready']:
                reason = 'Current game data is not ready: ' + ' '.join(freshness['reasons'])
        availability = None
        if not event_issue:
            availability = player_availability(news, leg.get('player', ''), leg['date'][:10], now, store=store)
            if availability['blocked']:
                reason = availability['note']
        if reason:
            skipped.append({'player': leg.get('player'), 'stat': leg.get('stat'),
                            'side': leg.get('side'), 'line': leg.get('line'),
                            'offer_variant': leg['offer_variant'], 'reason': _clean(reason, 250)})
            if not event_issue:
                blocked_research = bool((availability or {}).get('blocked') or
                    len(variants[_offer_key(item.get('leg', {}))]) > 1 or
                    len(thresholds[_key(item.get('leg', {}))]) > 1 or
                    leg.get('side') not in {'more','less'} or _number(leg.get('line')) is None)
                held_items.append(({**item, '_research_blocked':blocked_research}, leg, context, reason))
            continue
        standardized = _number(projection.get('standardized_margin'))
        margin = float(projection['selected_side_margin'])
        score = standardized if standardized is not None else margin / max(1, abs(projection['reference_projection']))
        usable.append({'leg': dict(leg), 'reason': _reason(item),
                       'caution': availability.get('note') if availability else None,
                       'availability': availability, 'projection': projection,
                       'context': context, 'freshness': freshness, '_score': score})
    usable.sort(key=lambda item: (-item['_score'], -int(item['projection'].get('n') or 0), item['leg']['player']))
    if preferred_stat:
        # Honor a market preference only after each actual offered side has
        # passed the normal gates. Compare players using their chosen market's
        # own score; a preference cannot manufacture a missing line or edge.
        preferred = {}
        for item in usable:
            identity = canonical_player(item['leg']['player'])
            previous = preferred.get(identity)
            if previous is None or (previous['leg']['stat'] != preferred_stat and
                                    item['leg']['stat'] == preferred_stat):
                preferred[identity] = item
        usable = sorted(preferred.values(), key=lambda item: (
            -item['_score'], -int(item['projection'].get('n') or 0), item['leg']['player']))
    # Only a verified saved event establishes which two teams a single-game
    # request must cover. Do not infer the opponent from player names or images.
    event_teams = {}
    for leg in original:
        context, issue = _event(store, leg, now)
        team = canonical_team(leg.get('team'))
        opponent = canonical_team(context.get('opponent'))
        event_id = context.get('game_id')
        if not issue and event_id and team in TEAM_WORDS and opponent in TEAM_WORDS:
            event_teams.setdefault(event_id, set()).update((team, opponent))
    required_teams = next(iter(event_teams.values())) if len(event_teams) == 1 else ()
    chosen, coverage = select_lineup(usable, count, required_teams=required_teams, constraints=constraints)
    if preferred_stat:
        coverage['lineup_constraints']['preferred_stat'] = preferred_stat
    selected = [{key: value for key, value in item.items() if key != '_score'} for item in chosen]
    # Collapse repeated More/Less exclusions for the same missing sample or offer.
    unique_skipped = []
    seen_skips = set()
    for item in skipped:
        key = (item['player'], item['stat'], item['reason'], item['offer_variant'])
        if key not in seen_skips:
            seen_skips.add(key)
            unique_skipped.append(item)
    adjusted_offers = any(_variant(leg) in {'goblin', 'demon'} for leg in original)
    watchlist_held = []
    watchlist = _watchlist(store, original, news, now, held=watchlist_held)
    team_cache = {}
    selected_names = {canonical_player(item['leg']['player']) for item in selected}
    remaining = max(0, count - len(selected))
    research = (_research_from_master(store, [item for item in held_items
        if canonical_player(item[1].get('player', '')) not in selected_names], now, remaining,
        required_teams, team_cache=team_cache, selected=selected, constraints=constraints) if remaining else [])
    research_picks = [item for item in research if item.get('lean_side')]
    _, combined_coverage = select_lineup([*selected, *research_picks], count,
        required_teams=required_teams, constraints=constraints)
    research_choice_coverage = {key: combined_coverage[key] for key in
        ('requested_count', 'required_teams', 'selected_teams', 'missing_teams', 'two_team_coverage')}
    research_choice_coverage.update(selected_count=len(selected) + len(research_picks),
        complete=combined_coverage['lineup_complete'], research_only=True, approved_for_entry=False)
    master_watchlist = _held_master_watchlist(store, watchlist_held, now, count, team_cache)
    return {'board_id': report.get('board_id'), 'report_id': report.get('id'),
            'generated_at': now.isoformat(), 'source_report_generated_at': report.get('generated_at'),
            'research_only': True, 'shortlist': selected, 'skipped': unique_skipped,
            **coverage,
            'watchlist': watchlist, 'watchlist_held': watchlist_held,
            'research_candidates': research, 'research_picks':research_picks,
            'research_choice_coverage': research_choice_coverage,
            'master_watchlist': master_watchlist,
            'has_adjusted_offers': adjusted_offers,
            'notes': ['Experimental comparisons, not validated win probabilities.',
                      'Current availability and offer/payout terms still need checking; no entry was placed.'] +
                     (['Goblin/Demon payouts depend on the actual offer; no universal multiplier is assumed.']
                      if adjusted_offers else [])}


def build_master_targets(store, report, now=None, *, count=4, constraints=None):
    """Calculate independent targets for this board's one exact saved game.

    Uploaded thresholds and offered sides never enter the independent builder.
    Resolving an invalid board must not silently select the next Knicks game.
    """
    now = _now(now)
    _, coverage = select_lineup([], count, constraints=constraints)
    original = list(report.get('original_legs') or
                    [item.get('leg', {}) for item in report.get('legs', [])])

    def held(note):
        return {'mode': 'independent_projections', 'generated_at': now.isoformat(),
                'requested_count': count, 'requires_prizepicks_comparison': True,
                'source_kind': 'saved statistics and schedule', 'picks': [], 'held': [],
                'evaluated_candidates': [], 'game': None, 'game_date': None, 'note': note,
                'research_only': True, 'uploaded_offers_used': False,
                'source_board_id': report.get('board_id'), **coverage,
                'limits': ['Calculated targets are research thresholds, not current PrizePicks offers.',
                           'Current game, statistics and availability checks still apply. No entry is recorded.']}

    if not original:
        return held('No readable uploaded offer identifies a saved game for independent targets.')
    events = {}
    for leg in original:
        context, issue = _event(store, leg, now)
        team = canonical_team(leg.get('team'))
        if issue or team not in TEAM_WORDS or not context.get('game_id') or not context.get('date'):
            return held('Independent targets require the exact saved pregame matchup from the uploads. '
                        + str(issue or 'The uploaded team or event is unresolved.'))
        events.setdefault(context['game_id'], (context['date'], team))
    if len(events) != 1:
        return held('Uploads reference more than one game; independent targets need one exact matchup.')
    event_id, (date, team) = next(iter(events.items()))
    from independent_picks import build_independent_picks
    result = build_independent_picks(store, date=date, count=count, team=team, now=now,
                                     constraints=constraints, allow_unverified_timing=True)
    game = result.get('game') or {}
    if game.get('id') != event_id or game.get('date') != date:
        return held('Independent targets could not be matched to the exact uploaded game. '
                    + str(result.get('note') or 'No matching current event was returned.'))
    return {**result, 'resolved_game_id': event_id, 'source_board_id': report.get('board_id'),
            'uploaded_offers_used': False,
            'target_scope': 'Independent master-data calculation for the exact uploaded matchup.'}


def format_shortlist(data):
    """Keep the decision, its reason, and next request in one Telegram message."""
    picks = data.get('shortlist', [])
    research = data.get('research_candidates', [])
    research_picks = [item for item in research if item.get('lean_side')]
    heading = ('Partial shortlist — lineup incomplete' if picks and data.get('lineup_complete') is False
               else 'My shortlist' if picks else
               f'{len(research_picks)} research picks from your readable lines' if research_picks else
               f'Saved stats loaded — {len(research)} held research comparisons' if research else
               'No pick from these lines yet')
    lines = [heading]
    if data.get('constraint_note') and not research_picks:
        lines.append(_clean(data['constraint_note'], 280))
    for number, item in enumerate(picks, 1):
        leg = item['leg']
        block = (f"{number}. {_clean(leg['player'], 70)} — {leg['side'].title()} {float(leg['line']):g} "
                 f"{MARKET_NAMES.get(leg['stat'], leg['stat'])} ({_offer_label(_variant(leg))})"
                 f"\nWhy: {_clean(item['reason'], 350)}")
        if item.get('caution'):
            block += '\nWatch: ' + _clean(item['caution'], 200)
        lines.append(block)
    if research_picks:
        if any(not item.get('timing_verified') for item in research_picks):
            lines.append('Research only: saved game statistics are current; live game timing is unverified. '
                         'These do not count as a qualified lineup.')
        else:
            lines.append('Research picks from saved statistics; these do not all clear the stricter shortlist rules.')
        for number, item in enumerate(research_picks, len(picks) + 1):
            market = MARKET_NAMES.get(item['stat'], item['stat'])
            side = item['lean_side']
            scope = ', '.join(item.get('sample_seasons') or [])
            block = (f"{number}. {_clean(item['player'], 70)} — {side.title()} {item['line']:g} {market} "
                     f"({item['team']}; {_offer_label(_variant(item['leg']))})"
                     f"\nRESEARCH PICK: {item['support_label']}. "
                     f"{item['selected_sample_count']}/{item['n']} on this side; "
                     f"mean {item['mean']:.2f}, median {item['median']:g}."
                     f"\nLast {item['recent_n']}: {item['recent_side_count']}/{item['recent_n']} on this side. "
                     f"Same-team {scope or 'regular-season'} sample through {item['source_data_through_date']}.")
            if item['above'] == item['below']:
                block += '\nBasis: ' + item['selection_basis'] + '.'
            if item.get('research_caution'):
                block += '\nCaution: ' + item['research_caution']
            lines.append(block)
        if len(research_picks) + len(picks) < data.get('requested_count', 4):
            lines.append('Fewer than the requested number of readable, supported choices are available; '
                         'unsupported selections were not filled in.')
        lines.append('Historical counts describe these saved games, not this game’s win probability. '
                     'Check current availability before entry.')
        # Non-directional comparisons remain available in the saved analysis;
        # they must not look like additional choices in this concise reply.
    elif research:
        lines.append('Research from your master — held, not an approved lineup')
        for number, item in enumerate(research, 1):
            market = MARKET_NAMES.get(item['stat'], item['stat'])
            block = (f"Research {number}. {_clean(item['player'], 70)} — {item['line']:g} {market} "
                     f"({_clean(item['team'], 8)}; {_offer_label(_variant(item['leg']))})"
                     f"\n{item['n']} saved same-team games: mean {item['mean']:.2f}; median {item['median']:g}; "
                     f"{item['above']}/{item['n']} above, {item['below']}/{item['n']} below, {item['ties']} tied. "
                     f"Through {item['source_data_through_date']}.")
            benchmark = item.get('historical_benchmark') or {}
            if benchmark.get('side'):
                block += '\nHistory only: ' + benchmark['side'].title() + ' meets the research buffer.'
                if benchmark['side'] not in item.get('offered_sides', []):
                    block += ' That side was not readable on this card.'
            else:
                block += '\nHistory only: no side clears the research rule.'
            block += '\nHold: ' + _clean(item['hold_reason'], 110)
            lines.append(block)
        lines.append('Saved history is not a win probability; held comparisons do not count as eligible picks.')
    skipped = [item for item in data.get('skipped', [])
               if item['reason'] != 'The experimental reference does not support this side.']
    for item in ([] if research else skipped[:2 if picks else 4]):
        lines.append(f"Hold: {_clean(item.get('player'), 70)} {MARKET_NAMES.get(item.get('stat'), item.get('stat', ''))} "
                     f"({_offer_label(_variant(item))}) — "
                     + _clean(item['reason'], 180))
    if not picks and not skipped:
        lines.append('No uploaded side has a supported positive projection margin.')
    watch = data.get('watchlist', [])
    if watch:
        lines.append('Beyond your screenshots — check these lines:')
        for item in watch:
            block = (f"• {_clean(item['player'], 70)} {MARKET_NAMES[item['stat']]} vs {_clean(item['opponent'], 8)}: "
                     + _clean(item['reason'], 230))
            if item.get('caution'):
                block += ' ' + _clean(item['caution'], 130)
            lines.append(block)
        lines.append('Send those actual offers before I choose More/Less for them.')
    master_watch = data.get('master_watchlist', [])
    if master_watch:
        lines.append('Other players from your master — actual offers needed')
        for item in master_watch:
            lines.append(f"• {_clean(item['player'], 70)} {MARKET_NAMES[item['stat']]} ({item['team']}): "
                f"{item['n']} saved games, mean {item['mean']:.2f}, median {item['median']:g}, "
                f"through {item['source_data_through_date']}. Held for current checks; no offered line or side is assumed.")
    lines.append('Experimental research. Verify availability and offer/payout terms. No entry placed.')
    if data.get('has_adjusted_offers'):
        lines.append('Goblin/Demon payouts depend on the actual offer; no universal multiplier is assumed.')
    if data.get('board_id'):
        lines.append('/analyze ' + _clean(data['board_id'], 64))
    text = '\n\n'.join(lines)
    # Keep every complete pick and the footer: trim extra watchlist/hold blocks
    # first instead of cutting a recommendation or command mid-sentence.
    if len(text) > 2400:
        while len('\n\n'.join(lines)) > 2400 and len(lines) > 3:
            candidates = [i for i, line in enumerate(lines) if line.startswith(('Hold:', '• '))]
            if not candidates:
                break
            lines.pop(candidates[-1])
        if not any(line.startswith('• ') for line in lines):
            lines = [line for line in lines if line not in {
                'Beyond your screenshots — check these lines:',
                'Send those actual offers before I choose More/Less for them.',
                'Other players from your master — actual offers needed'}]
        # A four/six-player request must not lose its last pick or the no-entry
        # footer to a character slice. Shorten prose before dropping any fact.
        for maximum in (180, 120, 80):
            if len('\n\n'.join(lines)) <= 2400:
                break
            lines = ['\n'.join(('Why: ' + _clean(part[5:], maximum) if part.startswith('Why: ')
                                 else 'Watch: ' + _clean(part[7:], max(80, maximum))
                                 if part.startswith('Watch: ') else part)
                                for part in line.split('\n')) for line in lines]
        if len('\n\n'.join(lines)) > 2400:
            lines = ['\n'.join(part for part in line.split('\n') if not part.startswith('Why: '))
                     for line in lines]
        text = '\n\n'.join(lines)
    return text
