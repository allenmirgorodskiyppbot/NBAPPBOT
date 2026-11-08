"""Experimental evidence-based projection scenarios over existing workbook metrics.

No calibrated probability, causal defender effect, expected return or validated
betting edge is claimed. The model can decline to offer an aggressive candidate.
"""
from __future__ import annotations

import datetime as dt
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import re
import statistics
import threading
from zoneinfo import ZoneInfo

from feeds import canonical_game, canonical_player, canonical_team, TEAM_WORDS
from master import STAT_COMPONENTS, normalized_name
from news_digest import _aware, material_categories
from season_calendar import calendar_context
from game_phase import regular_phase_exclusion

MIN_COMPARABLE_OBSERVATIONS = 20
BENCHMARK_METHOD = 'same-team-mean20-v1'
_PROJECTION_READS = ContextVar('projection_request_reads', default=None)


def _read_owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


def _read_cache():
    cache = _PROJECTION_READS.get()
    return cache if cache is not None and cache['owner'] == _read_owner() else None


@contextmanager
def projection_read_scope():
    """Reuse raw reads during one synchronous analysis, never across requests.

    This can decorate a synchronous builder with ``@projection_read_scope()``.
    Nested calls share the caller's reads. Child tasks/threads get their own
    scope rather than inheriting a mutable cache from another request.
    """
    existing = _read_cache()
    if existing is not None:
        yield
        return
    cache = {'owner': _read_owner(), 'stores': {}, 'names': {}, 'aliases': {}, 'rows': {}}
    token = _PROJECTION_READS.set(cache)
    try:
        yield
    finally:
        _PROJECTION_READS.reset(token)


def _raw_comparable_rows(store, player, player_id, team, cutoff):
    """Fetch identities and raw games; market-specific validation stays below."""
    from career_identity import resolve_career_identity
    cache = _read_cache()
    store_key = id(store)
    if cache is not None:
        # Keep references alive so an object ID cannot be reused in this scope.
        cache['stores'][store_key] = store
    row_key = (store_key, str(player), player_id, team, cutoff)
    if cache is not None and row_key in cache['rows']:
        return cache['rows'][row_key]
    alias_key = (store_key, str(player))
    if cache is not None and alias_key in cache['aliases']:
        documented = cache['aliases'][alias_key]
    else:
        documented = resolve_career_identity(store, player)
        if cache is not None:
            cache['aliases'][alias_key] = documented
    approved_names = {normalized_name(name) for name in documented['aliases']}
    with store.connect() as db:
        names_key = (store_key, cutoff)
        if cache is not None and names_key in cache['names']:
            indexed_names = cache['names'][names_key]
        else:
            indexed_names = {}
            for row in db.execute('SELECT DISTINCT player,player_id FROM player_games WHERE date<?', (cutoff,)):
                indexed_names.setdefault(normalized_name(row['player']), []).append(dict(row))
            if cache is not None:
                cache['names'][names_key] = indexed_names
        names = [row for name in approved_names for row in indexed_names.get(name, [])]
        if not names:
            rows = []
        else:
            identities = {row['player_id'] for row in names if row['player_id']}
            identities.update(value for value in (player_id, canonical_player(player)) if value)
            exact_names = {row['player'] for row in names}
            filters = ['p.player_id IN (' + ','.join('?' for _ in identities) + ')',
                       'p.player IN (' + ','.join('?' for _ in exact_names) + ')',
                       'p.date<?', 'g.date=p.date', "g.status='final'",
                       "LOWER(p.appearance_state)='played'", "p.season_type='Regular Season'"]
            args = [*sorted(identities), *sorted(exact_names), cutoff]
            if team:
                filters.append('p.team=?')
                args.append(team)
            rows = [dict(row) for row in db.execute(
                'WITH eligible AS (SELECT p.*,g.home_team,g.away_team FROM player_games p '
                'JOIN games g ON g.id=p.game_id WHERE ' + ' AND '.join(filters) + '), '
                'recent_games AS (SELECT game_id,MAX(date) AS game_date FROM eligible GROUP BY game_id '
                'ORDER BY game_date DESC,game_id DESC LIMIT 100) '
                'SELECT e.* FROM eligible e JOIN recent_games r ON r.game_id=e.game_id '
                'ORDER BY e.date DESC,e.game_id DESC,e.updated_at DESC,e.player_id', args)]
    if cache is not None:
        cache['rows'][row_key] = rows
    return rows


def _evidence(store,kind,entity):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_evidence'").fetchone():
            return []
        return [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM workbook_evidence WHERE kind=? AND entity=?',(kind,entity))]


def _quantile(values,p):
    ordered=sorted(values);position=(len(ordered)-1)*p;lo=int(position);hi=math.ceil(position)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)


def _weighted(values):
    weights=[.94**i for i in range(len(values))]
    return sum(v*w for v,w in zip(values,weights))/sum(weights)


def _nonnegative(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _comparable_observations(store, player, player_id, team, cutoff, components):
    """Resolve exact-name ID aliases and count one agreeing observation per game.

    Provider IDs are lookup keys, not proof of identity. Keep complete aliases
    together before imposing the game window so a duplicate cannot inflate the
    sample or hide a conflicting value just beyond a row limit.
    """
    quality = {'duplicate_rows': 0, 'conflicting_games': 0, 'incomplete_games': 0,
               'invalid_event_rows': 0, 'invalid_season_rows': 0}
    rows = _raw_comparable_rows(store, player, player_id, team, cutoff)
    groups = {}
    for original in rows:
        row = dict(original)
        try:
            game_day = dt.date.fromisoformat(row['date'])
        except (TypeError, ValueError):
            quality['invalid_event_rows'] += 1
            continue
        home, away, actual_team = (canonical_team(row.get(field))
                                   for field in ('home_team', 'away_team', 'team'))
        actual_opponent = away if actual_team == home else home if actual_team == away else None
        stated_opponent = canonical_team(row.get('opponent'))
        if (home not in TEAM_WORDS or away not in TEAM_WORDS or home == away or not actual_opponent or
                (stated_opponent and stated_opponent != actual_opponent)):
            quality['invalid_event_rows'] += 1
            continue
        season = re.fullmatch(r'(\d{4})-(\d{2})', str(row.get('season') or ''))
        if (not season or int(season[2]) != (int(season[1]) + 1) % 100 or
                game_day.year not in {int(season[1]), int(season[1]) + 1}):
            quality['invalid_season_rows'] += 1
            continue
        # The joined final game supplies its actual opponent when an imported
        # player alias omits that field. This is an event identity, not a guess.
        row['team'], row['opponent'] = actual_team, actual_opponent
        try:
            excluded_phase = regular_phase_exclusion(row)
        except ValueError:
            excluded_phase = {'reason': 'Source event identity conflicts with the statistical phase.'}
        if excluded_phase:
            quality['invalid_event_rows'] += 1
            continue
        key = (canonical_game(row['date'], home, away), normalized_name(player))
        groups.setdefault(key, []).append(row)
    valid = []
    for aliases in groups.values():
        quality['duplicate_rows'] += max(0, len(aliases) - 1)
        observed = [{field: _nonnegative(row.get(field)) for field in (*components, 'minutes')}
                    for row in aliases]
        # Known partial values also participate in conflict detection. Missing
        # data may be filled by a complete alias; a contradictory value may not.
        if any(len({value[field] for value in observed if value[field] is not None}) > 1
               for field in (*components, 'minutes')) or len({row['season'] for row in aliases}) > 1:
            quality['conflicting_games'] += 1
            continue
        complete = [(row, values) for row, values in zip(aliases, observed)
                    if values['minutes'] is not None and values['minutes'] > 0 and
                    all(values[field] is not None for field in components)]
        if not complete:
            quality['incomplete_games'] += 1
            continue
        original, values = next(((row, values) for row, values in complete if row.get('source')), complete[0])
        row = {**original, **values}
        # Shot volume is an optional explanatory statistic. Conflicting aliases
        # must not let an arbitrary FGA value leak into that explanation.
        volume = {_nonnegative(alias.get('fga')) for alias in aliases} - {None}
        row['fga'] = next(iter(volume)) if len(volume) == 1 else None
        valid.append(row)
        if len(valid) == 40:
            break
    return valid, quality


def benchmark_margin(mean, low, high):
    """Disclosed research buffer; it is not a calibrated betting edge."""
    return max(.5, .15 * abs(float(mean)), .25 * max(0.0, float(high) - float(low)))


def baseline_offer_decision(projection, line):
    """One statistical offer rule shared by chat and board analysis.

    Callers must still apply current schedule, availability and source checks.
    The buffer is a research rule, not an estimated edge or win probability.
    """
    value = _nonnegative(line)
    empty = {'side': None, 'minimum_margin': None, 'margin': None, 'status': 'pass'}
    if not projection.get('available') or int(projection.get('n') or 0) < MIN_COMPARABLE_OBSERVATIONS:
        return {**empty, 'reason': projection.get('reason') or 'Twenty valid same-team games are required.'}
    if value is None:
        return {**empty, 'reason': 'A finite nonnegative offered line is required.'}
    mean = _nonnegative(projection.get('reference_projection'))
    median = _nonnegative(projection.get('baseline_median'))
    spread = projection.get('empirical_scenario_range') or {}
    low, high = _nonnegative(spread.get('p10')), _nonnegative(spread.get('p90'))
    if None in (mean, median, low, high) or high < low:
        return {**empty, 'reason': 'The benchmark, median or observed range is incomplete.'}
    required = benchmark_margin(mean, low, high)
    details = {**empty, 'minimum_margin': round(required, 4), 'mean': mean, 'median': median,
               'margin': round(abs(mean-value), 4)}
    context_holds = projection.get('current_context_holds') or []
    if context_holds:
        return {**details, 'reason': ' '.join(context_holds)}
    side = 'more' if mean > value and median > value else 'less' if mean < value and median < value else None
    if side is None:
        return {**details, 'reason': 'The benchmark mean and median do not both support the same side of this line.'}
    if abs(mean-value) + 1e-9 < required:
        return {**details, 'reason': f'The margin {abs(mean-value):g} is smaller than the research buffer {required:g}.'}
    return {**details, 'side': side, 'status': 'supported',
            'reason': f'The 20-game mean {mean:g} and median {median:g} support {side.title()} with a research buffer of {required:g}; no betting edge is established.'}


def projection_for_leg(store, leg, now):
    components = STAT_COMPONENTS.get(leg.get('stat'))
    try:
        date = dt.date.fromisoformat(str(leg.get('date') or '')[:10]).isoformat()
    except (ValueError, TypeError):
        date = None
    team = canonical_team(leg.get('team'))
    if not components or not date:
        return {'available': False, 'reason': 'A supported statistic and event date are required.'}
    if team not in TEAM_WORDS:
        return {'available': False, 'reason': 'A current NBA team is required for a same-team benchmark.'}
    today = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    cutoff = min(date, today)
    player = leg['player']
    player_id = leg.get('player_id') or canonical_player(player)
    valid, quality = _comparable_observations(store, player, player_id, team, cutoff, components)
    recent = valid[:MIN_COMPARABLE_OBSERVATIONS]
    values = [sum(float(row[key]) for key in components) for row in recent]
    partial = {'n': len(values), 'mean': statistics.fmean(values) if values else None,
               'median': statistics.median(values) if values else None,
               'first_date': min((row['date'] for row in recent), default=None),
               'last_date': recent[0]['date'] if recent else None,
               'team': team, 'phase': 'Regular Season',
               'seasons': sorted({row['season'] for row in recent})}
    if len(recent) < MIN_COMPARABLE_OBSERVATIONS:
        return {'available': False, 'method': BENCHMARK_METHOD,
                'reason': f'Only {len(recent)} valid same-team regular-season games are stored; '
                          f'{MIN_COMPARABLE_OBSERVATIONS} are required for the benchmark. '
                          'This is partial stored coverage, not a career-game count.',
                'observations': len(recent), 'required_observations': MIN_COMPARABLE_OBSERVATIONS,
                'partial_sample': partial, 'sample_quality': quality,
                'source_games': [{'game_id': row['game_id'], 'date': row['date'], 'source': row['source'],
                                 'value': value, 'minutes': row['minutes'], 'team': row['team'],
                                 'opponent': row['opponent'], 'season': row['season'],
                                 'observed_at': row.get('updated_at'),
                                 'components': {key: row[key] for key in components}}
                                for row, value in zip(recent, values)],
                'missing_policy': 'Missing minutes/statistics are not imputed; conflicting duplicate games are excluded.'}
    expected = statistics.fmean(values)
    median = statistics.median(values)
    last5 = recent[:5]
    minutes = _weighted([row['minutes'] for row in last5])
    observed_minutes = statistics.fmean(row['minutes'] for row in recent)
    rate = sum(values) / sum(row['minutes'] for row in recent)
    low_minutes = _quantile([row['minutes'] for row in recent], .25)
    high_minutes = _quantile([row['minutes'] for row in recent], .75)
    low, high = _quantile(values, .1), _quantile(values, .9)
    sd = statistics.stdev(values)
    context = calendar_context(store, date, team)
    opponent = leg.get('opponent') or context.get('opponent')
    reasons = [f'Benchmark: the arithmetic mean of the last {len(recent)} valid {team} regular-season games is {expected:.2f}; median {median:g}.',
               f'Recent workload reference: {minutes:.1f} minutes from the last five appearances. Minutes do not rescale the benchmark.',
               'Previous seasons on the same team may be included; sample dates and seasons are shown separately.']
    limitations = ['Descriptive benchmark selected for testing; no calibrated win probability or demonstrated betting advantage.',
                   'The simulation did not establish an advantage over its Always Less control.',
                   'Opponent history, rest and tentative defenders are context only; they do not numerically change the benchmark.',
                   'Stored history can be incomplete; this sample is not a complete career record.']
    if quality['conflicting_games']:
        limitations.append('Conflicting duplicate game statistics or minutes were excluded.')
    cohorts = []
    if opponent:
        matched = [row for row in valid if row.get('opponent') == opponent]
        matched_values = [sum(row[key] for key in components) for row in matched]
        cohorts.append({'type': 'opponent_team', 'team': opponent, 'n': len(matched),
                        'mean': statistics.fmean(matched_values) if matched_values else None,
                        'median': statistics.median(matched_values) if matched_values else None,
                        'first_date': min((row['date'] for row in matched), default=None),
                        'last_date': max((row['date'] for row in matched), default=None),
                        'adjustment': 0.0,
                        'scope': 'Opponent subset of up to 40 recent same-team regular-season games; not lifetime or named-defender data.'})
        reasons.append(f'Against {opponent}: {len(matched)} stored games in the recent comparable window; no matchup multiplier applied.')
    rest = context.get('scheduled_rest_days')
    if rest is not None:
        cohorts.append({'type': 'team_rest', 'days': rest, 'n': 0, 'adjustment': 0.0})
        reasons.append(f'Team calendar shows {rest} full rest day(s); no automatic fatigue multiplier is applied.')
    else:
        limitations.append('Complete scheduled team rest context is unavailable.')
    source_news = []
    for item in store.recent_news(100):
        if player.casefold() not in (str(item.get('title', '')) + ' ' + str(item.get('body', ''))).casefold():
            continue
        stamp = _aware(item.get('published_at'))
        if not stamp or not 0 <= (now - stamp).total_seconds() <= 48 * 3600:
            continue
        if not material_categories(item):
            continue
        source_news.append({key: item.get(key) for key in ('title', 'source', 'url', 'published_at')})
        if len(source_news) == 3:
            break
    if source_news:
        limitations.append('Recent reports may affect participation or workload; the historical mean does not incorporate that change.')
    from assignment_refresh import assignment_rows_for, refresh_caution
    assignments = [row for row in assignment_rows_for(store, now=now)
                   if row.get('selected_player') == player and row.get('opponent_team') == opponent
                   and str(row.get('assignment_asof', ''))[:10] <= today]
    assignments = sorted(assignments, key=lambda row: str(row.get('assignment_asof', '')), reverse=True)[:1]
    pairing = None
    if assignments:
        row = assignments[0]
        pairing = {'primary': row.get('primary'), 'alternate': row.get('alternate'),
                   'asof': row.get('assignment_asof'), 'excel_row': row.get('_row'),
                   'status': 'Tentative; direction and actual game-day defensive assignment are not confirmed.',
                   'last_checked': row.get('last_checked'), 'refresh_state': row.get('refresh_state'),
                   'refresh_caution': refresh_caution(row)}
        reasons.append(f"Dated tentative pairing: {pairing['primary']} / {pairing['alternate']} ({str(pairing['asof'])[:10]}). No defender effect is assumed.")
        if pairing['refresh_caution']:
            limitations.append(pairing['refresh_caution'])
    line = _nonnegative(leg.get('line'))
    priced = line is not None and leg.get('side') in {'more', 'less'}
    direction = 1 if leg.get('side') == 'more' else -1
    margin = (expected - line) * direction if priced else None
    median_margin = (median - line) * direction if priced else None
    standardized = margin / sd if priced and sd else None
    required_margin = benchmark_margin(expected, low, high)
    sample_lag = (dt.date.fromisoformat(cutoff) - dt.date.fromisoformat(recent[0]['date'])).days
    side_reasons = []
    if priced:
        if date < today:
            side_reasons.append('The event date is in the past; this is historical research.')
        if margin <= 0:
            side_reasons.append('The benchmark does not support this side.')
        elif margin + 1e-9 < required_margin:
            side_reasons.append(f'The margin {margin:g} is smaller than the disclosed research buffer {required_margin:g}.')
        if median_margin <= 0:
            side_reasons.append('The sample median does not agree with this side.')
        if not 0 <= sample_lag <= 7:
            side_reasons.append('The latest comparable game is more than seven days old.')
    volume = [row.get('fga') for row in last5 if row.get('fga') is not None]
    if len(volume) >= 3 and leg['stat'] in {'pts', 'tpm'}:
        reasons.append(f'Recent shot volume: {statistics.fmean(volume):.1f} FGA per observed game ({len(volume)} observations).')
    result = {'available': True, 'method': BENCHMARK_METHOD, 'sample_quality': quality,
            'reference_projection': round(expected, 2), 'baseline_median': round(median, 2),
            'baseline_standard_deviation': round(sd, 4), 'projected_minutes': round(minutes, 2),
            'per_minute_baseline': round(rate, 4),
            'workload_context': {'recent_five_reference_minutes': round(minutes, 2),
                                 'sample_mean_minutes': round(observed_minutes, 2),
                                 'changes_benchmark': False},
            'minutes_scenarios': {'lower_workload': round(rate * low_minutes, 2),
                                 'reference': round(expected, 2), 'higher_workload': round(rate * high_minutes, 2),
                                 'meaning': 'Proportional workload illustrations only; the reference remains the unadjusted 20-game mean.'},
            'empirical_scenario_range': {'p10': round(low, 2), 'p90': round(high, 2),
                                        'meaning': 'Spread of actual observed game statistics; not a calibrated forecast interval.'},
            'selected_side_margin': round(margin, 2) if margin is not None else None,
            'standardized_margin': round(standardized, 3) if standardized is not None else None,
            'research_margin_requirement': round(required_margin, 4),
            'research_margin_method': 'At least 0.5, 15% of the benchmark, or 25% of the empirical p10-p90 width; heuristic, not calibrated edge.',
            'selected_side_supported': bool(priced and not side_reasons),
            'selected_side_pass_reasons': side_reasons, 'priced_offer': priced,
            'confidence': 'low', 'n': len(recent), 'required_observations': MIN_COMPARABLE_OBSERVATIONS,
            'event_date': date, 'evaluated_as_of_date': today, 'sample_lag_days': sample_lag,
            'current_context_holds': ([ 'The event date is in the past; this is historical research.' ] if date < today else []) +
                                     ([ 'The latest comparable game is more than seven days old.' ] if not 0 <= sample_lag <= 7 else []),
            'first_date': min(row['date'] for row in recent), 'last_date': recent[0]['date'],
            'actual_teams': sorted({row['team'] for row in recent}),
            'seasons': sorted({row['season'] for row in recent}), 'opponent': opponent,
            'calendar': context, 'cohorts': cohorts, 'tentative_pairing': pairing,
            'supporting_reasons': reasons, 'counterarguments': limitations, 'player_reports': source_news,
            'aggressive_research_candidate': False, 'aggressive_signals': [],
            'risk_note': 'A historical mean can miss changes in role, minutes and availability. Passing the buffer is not proof of a betting advantage.',
            'source_games': [{'game_id': row['game_id'], 'date': row['date'], 'source': row['source'],
                             'value': value, 'minutes': row['minutes'], 'team': row['team'],
                             'opponent': row['opponent'], 'season': row['season'],
                             'observed_at': row.get('updated_at'),
                             'components': {key: row[key] for key in components}}
                            for row, value in zip(recent, values)]}
    if priced:
        decision = baseline_offer_decision(result, line)
        result['selected_side_supported'] = decision['side'] == leg['side']
        result['selected_side_pass_reasons'] = ([] if result['selected_side_supported'] else
                                               [decision['reason'] if decision['side'] is None else 'The benchmark supports the opposite side.'])
        result['offer_decision'] = decision
    return result
