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
from news_digest import _aware, _clean, _metadata, is_promotion
from season_calendar import calendar_context, current_season

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


def player_availability(news, player, date, now):
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
    latest = timed[:1]
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


def _event(store, leg, now):
    date = str(leg.get('date') or '')[:10]
    team = canonical_team(leg.get('team', ''))
    with store.connect() as db:
        if leg.get('game_id'):
            events = [dict(row) for row in db.execute('SELECT * FROM games WHERE id=?', (leg['game_id'],))]
        elif date and team:
            events = [dict(row) for row in db.execute('SELECT * FROM games WHERE date=? '
                      'AND (home_team=? OR away_team=?)', (date, team, team))]
        else:
            events = []
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
    if event.get('status') in {'postponed', 'canceled', 'cancelled', 'suspended', 'live', 'final', 'post', 'completed'}:
        return context, 'The stored game is postponed, canceled or already started.'
    if context.get('status') in {'postponed', 'canceled', 'live', 'final', 'past_schedule_only'}:
        return context, 'The stored game is postponed, canceled or already started.'
    if not context and event:
        context = {'game_id': event.get('id'), 'tipoff': event.get('start_time'),
                   'status': event.get('status'), 'source': event.get('source'),
                   'opponent': (event.get('away_team') if event.get('home_team') == team else event.get('home_team'))
                   if team else None}
    context = {**context, 'date': date}
    tip = _aware(context.get('tipoff'))
    if context.get('status') != 'scheduled' or not tip:
        return context, 'A scheduled game with a known tipoff is required; event timing is unresolved.'
    if tip and tip <= now:
        return context, 'The saved tipoff has passed; this is no longer a pregame line.'
    return context, None


def _reason(item):
    p = item['projection']
    value = _number(p.get('reference_projection'))
    minutes = _number(p.get('projected_minutes'))
    last = _clean(p.get('last_date') or item.get('sample_last_date'), 10)
    base = f"Experimental reference {value:g}"
    if minutes is not None:
        base += f" at {minutes:g} minutes"
    base += f"; {int(p.get('n') or item.get('n') or 0)} saved games through {last}."
    opponent = next((c for c in p.get('cohorts', []) if c.get('type') == 'opponent_team'), None)
    if opponent:
        base += (f" {opponent.get('team')}: {opponent.get('n', 0)} comparable games" +
                 ('; no matchup adjustment.' if not opponent.get('adjustment') else '; small matchup adjustment.'))
    seasons = p.get('seasons') or item.get('sample_seasons') or []
    if len(seasons) > 1:
        base += ' Sample spans seasons.'
    return base


def _watchlist(store, original_legs, news, now):
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
            availability = player_availability(news, player['player'], date, now)
            if availability['blocked']:
                continue
            markets = []
            for stat, normal in (('pts', 15), ('reb', 6), ('ast', 4)):
                values = [_number(row.get(stat)) for row in recent]
                if any(value is None for value in values):
                    continue
                average = statistics.fmean(values)
                if average < normal * .5:
                    continue
                markets.append((average / normal, stat, average))
            if not markets:
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


def enrich_shortlist(store, report, now=None):
    """Apply presentation/risk gates to engine output without changing saved data."""
    now = _now(now)
    original = list(report.get('original_legs') or [item.get('leg', {}) for item in report.get('legs', [])])
    news = store.recent_news(200)
    thresholds = defaultdict(set)
    variants = defaultdict(set)
    for leg in original:
        number = _number(leg.get('line'))
        if number is not None:
            thresholds[_key(leg)].add(number)
            variants[_offer_key(leg)].add(_variant(leg))
    skipped, usable = [], []
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
        availability = None
        if not event_issue:
            availability = player_availability(news, leg.get('player', ''), leg['date'][:10], now)
            if availability['blocked']:
                reason = availability['note']
        if reason:
            skipped.append({'player': leg.get('player'), 'stat': leg.get('stat'),
                            'side': leg.get('side'), 'line': leg.get('line'),
                            'offer_variant': leg['offer_variant'], 'reason': _clean(reason, 250)})
            continue
        standardized = _number(projection.get('standardized_margin'))
        margin = float(projection['selected_side_margin'])
        score = standardized if standardized is not None else margin / max(1, abs(projection['reference_projection']))
        usable.append({'leg': dict(leg), 'reason': _reason(item),
                       'caution': availability.get('note') if availability else None,
                       'availability': availability, 'projection': projection,
                       'context': context, '_score': score})
    usable.sort(key=lambda item: (-item['_score'], -int(item['projection'].get('n') or 0), item['leg']['player']))
    selected, seen = [], set()
    for item in usable:
        player = canonical_player(item['leg']['player'])
        if player in seen:
            continue
        seen.add(player)
        selected.append({key: value for key, value in item.items() if key != '_score'})
        if len(selected) == 3:
            break
    # Collapse repeated More/Less exclusions for the same missing sample or offer.
    unique_skipped = []
    seen_skips = set()
    for item in skipped:
        key = (item['player'], item['stat'], item['reason'], item['offer_variant'])
        if key not in seen_skips:
            seen_skips.add(key)
            unique_skipped.append(item)
    adjusted_offers = any(_variant(leg) in {'goblin', 'demon'} for leg in original)
    return {'board_id': report.get('board_id'), 'report_id': report.get('id'),
            'generated_at': now.isoformat(), 'source_report_generated_at': report.get('generated_at'),
            'research_only': True, 'shortlist': selected, 'skipped': unique_skipped,
            'watchlist': _watchlist(store, original, news, now),
            'has_adjusted_offers': adjusted_offers,
            'notes': ['Experimental comparisons, not validated win probabilities.',
                      'Current availability and offer/payout terms still need checking; no entry was placed.'] +
                     (['Goblin/Demon payouts depend on the actual offer; no universal multiplier is assumed.']
                      if adjusted_offers else [])}


def format_shortlist(data):
    """Keep the decision, its reason, and next request in one Telegram message."""
    picks = data.get('shortlist', [])
    lines = ['My shortlist' if picks else 'No pick from these lines yet']
    for number, item in enumerate(picks, 1):
        leg = item['leg']
        block = (f"{number}. {_clean(leg['player'], 70)} — {leg['side'].title()} {float(leg['line']):g} "
                 f"{MARKET_NAMES.get(leg['stat'], leg['stat'])} ({_offer_label(_variant(leg))})"
                 f"\nWhy: {_clean(item['reason'], 350)}")
        if item.get('caution'):
            block += '\nWatch: ' + _clean(item['caution'], 200)
        lines.append(block)
    skipped = [item for item in data.get('skipped', [])
               if item['reason'] != 'The experimental reference does not support this side.']
    for item in skipped[:2 if picks else 3]:
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
        text = '\n\n'.join(lines)
    return text[:2400]
