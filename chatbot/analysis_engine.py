"""Versioned, descriptive research against immutable uploaded PrizePicks lines.

Historical fractions are observations, not calibrated future win probabilities.
No actual entry is placed, created, or settled by this analysis module.
"""
from __future__ import annotations

import datetime as dt
from itertools import combinations
import json
import statistics
import uuid
from zoneinfo import ZoneInfo

from feeds import canonical_player, canonical_team
from master import STAT_COMPONENTS

MAX_OBSERVED = 20
MIN_OBSERVED = 10
MAX_SCAN = 200


def _report_tables(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS analysis_reports (
            id TEXT PRIMARY KEY, board_id TEXT NOT NULL, generated_at TEXT NOT NULL,
            report_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS analysis_reports_board ON analysis_reports(board_id,generated_at);''')


def _event_date(store, leg):
    value = leg.get('date')
    if not value and leg.get('game_id'):
        with store.connect() as db:
            row = db.execute('SELECT date FROM games WHERE id=?', (leg['game_id'],)).fetchone()
            value = row[0] if row else None
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def _feed_health(status, now):
    feeds = status.get('feeds', [])
    warnings = []
    healthy = []
    for feed in feeds:
        name = str(feed.get('name', ''))
        if str(feed.get('error') or '').startswith('Disabled:'):
            continue
        if not (name.startswith(('espn_', 'nba_', 'rss_')) or
                name in {'injuries_configured', 'availability_coverage', 'collector'}):
            continue
        stamp = feed.get('last_success')
        try:
            instant = dt.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=dt.timezone.utc)
            age = (now - instant).total_seconds()
        except (TypeError, ValueError):
            age = None
        fresh = age is not None and -60 <= age < 1800 and not feed.get('error')
        if fresh:
            healthy.append(name)
        else:
            warnings.append(f"Feed {feed.get('name', 'unknown')} has no successful update within "
                            '30 minutes, or its latest attempt failed.')
    if not healthy:
        warnings.append('No live feed is currently verified fresh; check availability and news manually.')
    availability = [name for name in healthy if any(t in name.casefold() for t in ('injur', 'availability'))]
    if not availability:
        warnings.append('No fresh availability feed is active. Player participation must be checked manually.')
    warnings.append('Public box-score/headline coverage is not complete official NBA injury-report coverage.')
    return {'feeds': feeds, 'fresh_feeds': healthy, 'fresh_availability_feeds': availability,
            'warnings': warnings}


def _analyze_leg(store, leg, now):
    result = {'leg': dict(leg), 'eligible_for_ranking': False, 'research_only': True}
    stat = leg.get('stat')
    components = STAT_COMPONENTS.get(stat)
    if not components:
        result['reason'] = 'Unsupported statistic; this line requires manual analysis.'
        return result
    date = _event_date(store, leg)
    result['event_date'] = date
    if not date:
        result['reason'] = 'The target event date is not known. Supply a date or a stored game ID.'
        return result
    as_of_date = now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    cutoff = min(date, as_of_date)
    result['sample_cutoff_exclusive'] = cutoff
    gate_reasons = []
    period = str(leg.get('period') or leg.get('market_period') or 'full_game').lower()
    if period.replace('-', '_').replace(' ', '_') not in {'full', 'game', 'full_game'}:
        gate_reasons.append('Only full-game lines are supported; this period needs manual analysis.')
    if date < as_of_date:
        gate_reasons.append('The target date is in the past; this is retrospective research, not an actionable line.')
    player_id = leg.get('player_id') or canonical_player(leg['player'])
    with store.connect() as db:
        if leg.get('game_id'):
            events = list(db.execute('SELECT status FROM games WHERE id=?', (str(leg['game_id']),)))
        elif leg.get('team'):
            events = list(db.execute('SELECT status FROM games WHERE date=? AND '
                                     '(home_team=? OR away_team=?)',
                                     (date, canonical_team(leg['team']), canonical_team(leg['team']))))
        else:
            events = list(db.execute('SELECT DISTINCT g.status FROM games g '
                                     'JOIN player_games p ON p.game_id=g.id '
                                     'WHERE p.date=? AND p.player_id=?', (date, player_id)))
    if any(str(row[0]).lower() in {'live', 'final', 'post', 'completed', 'closed'} for row in events):
        gate_reasons.append('The matched game is already started or final; this is retrospective research.')
    if any(str(row[0]).lower() in {'postponed', 'canceled', 'cancelled', 'suspended'} for row in events):
        gate_reasons.append('The matched game is postponed/canceled/suspended; verify any replacement line manually.')
    with store.connect() as db:
        rows = [dict(row) for row in db.execute('''
            SELECT p.*, g.status AS game_status
            FROM player_games p JOIN games g ON g.id=p.game_id
            WHERE p.player_id=? AND p.date<? AND g.status='final'
              AND LOWER(p.appearance_state)='played'
              AND COALESCE(p.season_type,'') != 'Preseason'
            ORDER BY p.date DESC,p.game_id DESC LIMIT ?''', (player_id, cutoff, MAX_SCAN))]
    values, used_rows, missing, considered = [], [], 0, 0
    for row in rows:
        considered += 1
        if any(row.get(field) is None for field in components):
            missing += 1
            continue
        value = sum(float(row[field]) for field in components)
        values.append(value)
        used_rows.append(row)
        if len(values) >= MAX_OBSERVED:
            break
    n = len(values)
    line = float(leg['line'])
    more = sum(v > line for v in values)
    less = sum(v < line for v in values)
    ties = sum(v == line for v in values)
    side_count = more if leg['side'] == 'more' else less
    result.update({
        'n': n, 'missing_games': missing, 'games_examined': considered,
        'scan_limit': MAX_SCAN, 'observed_window_limit': MAX_OBSERVED,
        'mean': statistics.fmean(values) if values else None,
        'median': statistics.median(values) if values else None,
        'more_count': more, 'less_count': less, 'tie_count': ties,
        'historical_selected_side_fraction': side_count / n if n else None,
        'sample_first_date': min((r['date'] for r in used_rows), default=None),
        'sample_last_date': max((r['date'] for r in used_rows), default=None),
        'sample_seasons': sorted(set(str(r['season']) for r in used_rows if r.get('season'))),
        'sample_phases': sorted(set(str(r['season_type']) for r in used_rows if r.get('season_type'))),
        'sample_teams': sorted(set(str(r['team']) for r in used_rows if r.get('team'))),
        'sample_games': [{'game_id': r['game_id'], 'date': r['date'], 'team': r['team'],
                          'opponent': r['opponent'], 'value': value, 'source': r['source'],
                          'observed_at': r['updated_at']} for r, value in zip(used_rows, values)],
        'eligible_for_ranking': n >= MIN_OBSERVED and not gate_reasons,
        'eligibility_blocks': gate_reasons,
        'warnings': ['Historical line-crossing frequency is descriptive, not a forecast win probability.',
                     'The sample may include different teams, seasons and playoff phases; inspect its scope.',
                     'Opponent, injury and expected-minutes adjustments are not a validated predictive model.'],
    })
    if n < MIN_OBSERVED:
        result['reason'] = f'Only {n} observed prior games are available; at least {MIN_OBSERVED} are needed to rank.'
    elif gate_reasons:
        result['reason'] = ' '.join(gate_reasons)
    else:
        result['reason'] = 'Enough historical observations for a descriptive comparison only.'
    if not events:
        result['warnings'].append('No target game was matched; confirm its date, team and start status manually.')
    return result


def _suggest_entries(candidates):
    """Construct research alternatives only; never mutate the actual entry ledger."""
    pairs = []
    for first, second in combinations(candidates, 2):
        legs = [first['leg'], second['leg']]
        players = {leg.get('player_id') or canonical_player(leg['player']) for leg in legs}
        teams = {canonical_team(leg.get('team')) for leg in legs if leg.get('team')}
        if len(players) != 2 or len(teams) < 2:
            continue
        pairs.append((first, second))
    # A weaker-leg historical support ranking is descriptive. It is deliberately
    # not an estimate of combined win probability, payout or expected return.
    pairs.sort(key=lambda pair: (min(item['historical_selected_side_fraction'] for item in pair),
                                 min(item['n'] for item in pair)), reverse=True)
    suggestions = []
    for number, pair in enumerate(pairs[:3], 1):
        suggestions.append({
            'alternative': number, 'selection_count': 2, 'research_only': True, 'actionable': False,
            'legs': [dict(item['leg']) for item in pair],
            'historical_support': [{key: item[key] for key in (
                'n', 'historical_selected_side_fraction', 'sample_first_date', 'sample_last_date')}
                for item in pair],
            'ranking_basis': 'Weaker-leg historical support, then smaller sample size; not a forecast.',
            'combined_win_probability': None,
            'correlation_warning': 'These selections may be correlated, particularly in the same game. '
                                   'No independence assumption or multiplied leg probability is used.',
            'requires_verification': ['Current uploaded line availability', 'Player availability and game start status',
                                      'PrizePicks entry type, eligibility, payout and promotion terms'],
        })
    if suggestions:
        note = ('Research alternatives only. Verify every line and availability item in PrizePicks before '
                'placing anything; no tracked entry has been created.')
    elif len(candidates) < 2:
        note = 'Fewer than two eligible candidates are available; no two-leg research lineup can be assembled.'
    elif any(not item['leg'].get('team') for item in candidates):
        note = ('Supply explicit team abbreviations for the selections. A research lineup needs two '
                'distinct players and at least two explicitly specified teams.')
    else:
        teams = sorted({canonical_team(item['leg']['team']) for item in candidates})
        note = (f"Eligible candidates cover only {', '.join(teams)}. Upload an eligible line for another "
                'team; ordinary PrizePicks lineups need selections from at least two teams. '
                'No actual entry has been created.')
    return suggestions, note


def analyze_board(store, board_id, as_of=None):
    """Analyze a saved board without mutating it or creating a placed entry."""
    board = store.get_board(board_id)
    if not board:
        raise ValueError('Unknown board ID.')
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now.replace('Z', '+00:00'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    results = [_analyze_leg(store, leg, now) for leg in board['legs']]
    candidates = [r for r in results if r['eligible_for_ranking']]
    candidates.sort(key=lambda r: (r['historical_selected_side_fraction'], r['n']), reverse=True)
    for rank, item in enumerate(candidates, 1):
        item['descriptive_rank'] = rank
    suggested_entries, lineup_note = _suggest_entries(candidates)
    freshness = _feed_health(store.status(), now)
    report = {
        'id': uuid.uuid4().hex, 'board_id': board_id, 'generated_at': now.isoformat(),
        'engine_version': 'descriptive-v1', 'research_only': True,
        'board_created_at': board.get('created_at'), 'board_source': board.get('source'),
        'original_legs': board['legs'], 'legs': results, 'candidates': candidates,
        'suggested_entries': suggested_entries, 'lineup_note': lineup_note,
        'freshness': freshness,
        'recent_news': store.recent_news(10),
        'limitations': [
            'This starter ranks historical support; it does not estimate calibrated future probabilities or expected return.',
            'No parlay probability is calculated by multiplying leg hit rates; same-game and same-player legs may be dependent.',
            'These are manually uploaded line snapshots. Current PrizePicks availability and payout rules must be checked.',
            'The historical seed is only the embedded workbook subset; it is not the full companion ZIP.',
            'News text is source evidence and may be incomplete, edited or stale. Check publication and retrieval times.',
            'Historical as_of filtering uses calendar dates and current corrected statistics, not archived point-in-time backtests.',
        ],
    }
    _report_tables(store)
    with store.connect() as db:
        db.execute('INSERT INTO analysis_reports VALUES (?,?,?,?)',
                   (report['id'], board_id, report['generated_at'], json.dumps(report, default=str)))
    return report


def analysis_history(store, board_id, limit=10):
    _report_tables(store)
    with store.connect() as db:
        rows = db.execute('SELECT report_json FROM analysis_reports WHERE board_id=? '
                          'ORDER BY generated_at DESC,id DESC LIMIT ?',
                          (board_id, max(1, min(30, int(limit)))))
        return [json.loads(row[0]) for row in rows]
