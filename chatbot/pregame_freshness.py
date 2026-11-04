"""Read-only game-stat readiness, separate from injuries and defender freshness."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from feeds import canonical_team
from master import STAT_COMPONENTS, normalized_name
from news_digest import _aware


EASTERN = ZoneInfo('America/New_York')
_CANCELLED = {'postponed', 'canceled', 'cancelled'}


def _date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _successful_today(record, day, today, now):
    checked = _aware(record.get('last_checked_at'))
    published = _aware(record.get('publisher_retrieved_at'))
    return bool(record.get('date') == day and record.get('complete') is True and
                record.get('last_refresh_status') == 'success' and not record.get('last_refresh_error') and record.get('source') and
                checked and checked.astimezone(EASTERN).date() == today and
                -300 <= (now - checked).total_seconds() and published and
                published <= now + dt.timedelta(minutes=5) and
                isinstance(record.get('final_game_ids'), list) and
                isinstance(record.get('team_final_game_ids'), dict))


def _verified_inventory_today(record, today, now):
    verified = _aware(record.get('inventory_verified_at'))
    return bool(record.get('inventory_verified') is True and record.get('inventory_verification_source') and
                verified and verified.astimezone(EASTERN).date() == today and
                verified <= now + dt.timedelta(minutes=5))


def _number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _current_schedule_check(store, day, team, now):
    """Use the independent scoreboard check, never an old published clock."""
    checkpoint = store.get_checkpoint('nba_schedule_today', {})
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    detail = {key: checkpoint.get(key) for key in ('date', 'status', 'checked_at', 'source', 'error')}
    detail['game'] = None
    checked = _aware(checkpoint.get('checked_at'))
    if (checkpoint.get('date') != day or checkpoint.get('status') != 'success' or
            checkpoint.get('error') or not checkpoint.get('source') or not checked or
            not -300 <= (now - checked).total_seconds() <= 15 * 60):
        return detail, 'A successful independent check of today’s game schedule within 15 minutes is required.'
    games = checkpoint.get('games')
    if not isinstance(games, list):
        return detail, 'The current game schedule check has no usable game inventory.'
    matches = [game for game in games if isinstance(game, dict) and game.get('date') == day and
               team in {canonical_team(game.get('home_team')), canonical_team(game.get('away_team'))}]
    if len(matches) != 1:
        return detail, 'The independent schedule check does not establish one current game for this team.'
    game = matches[0]
    detail['game'] = {key: game.get(key) for key in
                      ('id', 'date', 'home_team', 'away_team', 'status', 'start_time', 'reported_status')}
    tipoff = _aware(game.get('start_time'))
    if (game.get('status') != 'scheduled' or
            game.get('reported_status') in {'live', 'final', 'postponed', 'cancelled', 'canceled'}):
        return detail, 'The independently checked game is not an upcoming scheduled game.'
    if not tipoff or tipoff.astimezone(EASTERN).date().isoformat() != day:
        return detail, 'The independently checked tipoff is unknown or conflicts with the requested game date.'
    if tipoff <= now:
        return detail, 'The independently checked tipoff has passed; this is no longer a pregame selection.'
    with store.connect() as db:
        saved = db.execute('SELECT * FROM season_schedule WHERE id=?', (game.get('id'),)).fetchone()
        observed = db.execute('SELECT * FROM games WHERE id=?', (game.get('id'),)).fetchone()
    event = dict(saved) if saved else {}
    if observed:
        event.update({key: value for key, value in dict(observed).items() if value is not None})
    event_teams = {canonical_team(event.get('home_team')), canonical_team(event.get('away_team'))}
    checked_teams = {canonical_team(game.get('home_team')), canonical_team(game.get('away_team'))}
    if (not game.get('id') or not event or event.get('date') != day or len(checked_teams) != 2 or
            '' in checked_teams or event_teams != checked_teams or event.get('status') != 'scheduled' or
            _aware(event.get('start_time')) != tipoff):
        return detail, 'The independently checked event conflicts with the saved game; current recommendations are held.'
    return detail, None


def assess_pregame_freshness(store, game_date, team, now, *, player=None, stat=None):
    """Verify today's completed-game inputs without relabeling history as current.

    A successful check of a published subset is not a full schedule inventory.
    Full-day independent inventory or a complete published team calendar must
    establish that no intervening team game is missing. Rest days are allowed;
    stale/unknown publication checks and unknown player participation are held.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    today = now.astimezone(EASTERN).date()
    required = today - dt.timedelta(days=1)
    target = _date(game_date)
    team = canonical_team(team)
    result = {'ready': False, 'reasons': [], 'game_date': str(game_date), 'team': team,
              'required_game_date': required.isoformat(), 'latest_team_final_date': None,
              'player_data_through_date': None, 'daily_refresh': {},
              'current_player_stat_coverage': [],
              'historical_only': target != today, 'checked_at': now.isoformat(),
              'coverage_scope': 'Game observations and current pregame schedule only; injuries, tracking and tentative defenders have separate source dates.'}
    reasons = result['reasons']
    if target != today:
        reasons.append('A current game-day refresh is required; future or past dates remain historical research.')
    if not team:
        reasons.append('The player’s current team is unresolved.')
        return result
    if target == today:
        result['schedule_check'], schedule_issue = _current_schedule_check(store, target.isoformat(), team, now)
        if schedule_issue:
            reasons.append(schedule_issue)
    daily = store.get_checkpoint('daily_nba:' + required.isoformat(), {}) or {}
    result['daily_refresh'] = {key: daily.get(key) for key in (
        'date', 'complete', 'last_checked_at', 'last_refresh_status', 'last_refresh_error',
        'imported_at', 'publisher_retrieved_at', 'inventory_scope', 'inventory_verified',
        'inventory_verified_at', 'inventory_verification_source')}
    worker = store.get_checkpoint('nightly_nba_refresh', {}) or {}
    year = today.year - (today.month < 7)
    calendar = store.get_checkpoint(f'season_calendar:{year}-{str(year + 1)[-2:]}', {}) or {}
    covered = (team in {canonical_team(value) for value in calendar.get('covered_teams', [])} and
               calendar.get('complete_for_published_games') is True and
               not calendar.get('unassigned_coverage_unknown') and
               not (calendar.get('unassigned_games') and not calendar.get('unassigned_windows')))
    with store.connect() as db:
        games = [dict(row) for row in db.execute(
            'SELECT * FROM games WHERE date<=? AND (home_team=? OR away_team=?) '
            'ORDER BY date DESC,id', (required.isoformat(), team, team))]
        scheduled = [dict(row) for row in db.execute(
            'SELECT * FROM season_schedule WHERE date<=? AND (home_team=? OR away_team=?) '
            'ORDER BY date DESC,id', (required.isoformat(), team, team))]
    finals = [game for game in games if game.get('status') == 'final' and _date(game.get('date'))]
    latest = finals[0] if finals else None
    if latest:
        result['latest_team_final_date'] = latest['date']
    else:
        reasons.append('The latest completed team game cannot be established from saved records.')
    start = _date(latest['date']) if latest else required
    for window in calendar.get('unassigned_windows', []):
        lower, upper = _date(window.get('start')), _date(window.get('end'))
        if not lower or not upper or (lower <= required and upper >= start):
            covered = False
    by_id = {game['id']: game for game in games}
    pending_events = [game for game in [*games, *scheduled] if
                      _date(game.get('date')) and start <= _date(game['date']) <= required and
                      game.get('status') not in _CANCELLED and
                      by_id.get(game['id'], game).get('status') != 'final']
    if pending_events:
        reasons.append('A past scheduled team game lacks final imported data: ' +
                       ', '.join(sorted({game['date'] for game in pending_events})) + '.')
    checked_target = _successful_today(daily, required.isoformat(), today, now)
    verified_inventory = bool(checked_target and _verified_inventory_today(daily, today, now))
    if not covered and not verified_inventory:
        reasons.append('Complete recent team-game inventory is unverified; a published statistics subset cannot establish missing games.')
    # A known empty day in a complete team calendar may lack a statistics file.
    # Its latest required final-game publication still needs a successful check
    # today. An empty games table by itself never establishes an off day.
    expected_yesterday = [game for game in scheduled if game.get('date') == required.isoformat()
                          and game.get('status') not in _CANCELLED]
    target_absent = not daily or (daily.get('complete') is not True and
                                 '404' in str(daily.get('last_refresh_error') or ''))
    calendar_offday = covered and not expected_yesterday and target_absent
    if not checked_target and not calendar_offday:
        reasons.append('The previous day’s game publication has no successful current-day refresh check.')
    if calendar_offday:
        result['off_day_basis'] = 'Complete saved published team calendar; live schedule changes are not independently verified.'
    if checked_target:
        team_ids = daily.get('team_final_game_ids', {}).get(team, [])
        if not isinstance(team_ids, list) or any(
                game_id not in by_id or by_id[game_id].get('status') != 'final' or
                by_id[game_id].get('date') != required.isoformat() for game_id in team_ids):
            reasons.append('The published team-game inventory contains a completed game missing from imported records.')
    if not covered and latest:
        # A verified empty yesterday cannot hide an unobserved game on an
        # intervening day. Establish every day since the last known team final.
        span = (required - start).days + 1
        missing = []
        if span > 60:
            reasons.append('The recent team-game inventory gap exceeds the verified daily window.')
        else:
            for offset in range(span):
                day = (start + dt.timedelta(days=offset)).isoformat()
                record = store.get_checkpoint('daily_nba:' + day, {}) or {}
                if not (_successful_today(record, day, today, now) and
                        _verified_inventory_today(record, today, now)):
                    missing.append(day)
                    continue
                ids = record.get('team_final_game_ids', {}).get(team, [])
                if not isinstance(ids, list) or any(game_id not in by_id or
                        by_id[game_id].get('status') != 'final' or by_id[game_id].get('date') != day
                        for game_id in ids):
                    reasons.append('An intervening published team game is missing from imported final records: ' + day + '.')
            if missing:
                result['missing_inventory_dates'] = missing
                reasons.append('Current independent team-game inventory is missing for: ' + ', '.join(missing) + '.')
    latest_record = {}
    if latest:
        latest_record = store.get_checkpoint('daily_nba:' + latest['date'], {}) or {}
        result['latest_team_publication'] = {key: latest_record.get(key) for key in
            ('date', 'last_checked_at', 'last_refresh_status', 'inventory_scope')}
        if (not _successful_today(latest_record, latest['date'], today, now) or
                latest['id'] not in latest_record.get('final_game_ids', []) or
                latest['id'] not in latest_record.get('team_final_game_ids', {}).get(team, [])):
            reasons.append('The latest completed team game lacks a successful current-day publication check and confirmed import coverage.')
    pending = []
    for field in ('pending_dates', 'older_pending_dates'):
        values = worker.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not _date(value) for value in values):
            reasons.append('Refresh pending-date status is malformed; game coverage cannot be verified.')
        else:
            pending.extend(values)
    for value in set(pending):
        day = _date(value)
        if not day or not start <= day <= required:
            continue
        calendar_empty = covered and not any(game.get('date') == value and game.get('status') not in _CANCELLED
                                             for game in scheduled)
        if not calendar_empty:
            reasons.append('A relevant game date is still pending import: ' + value + '.')
    if player and latest:
        latest_games = [game for game in finals if game['date'] == latest['date']]
        observed = []
        current_stats = set(STAT_COMPONENTS)
        with store.connect() as db:
            for game in latest_games:
                opponent = canonical_team(game['away_team'] if canonical_team(game['home_team']) == team
                                          else game['home_team'])
                rows = [dict(row) for row in db.execute(
                    'SELECT p.* FROM player_games p JOIN games g ON g.id=p.game_id '
                    'WHERE p.game_id=? AND p.team=? AND p.date=g.date', (game['id'], team))
                    if normalized_name(row['player']) == normalized_name(player) and row['source'] and
                    opponent != team and canonical_team(row['opponent']) in {'', opponent}]
                states = {str(row.get('appearance_state') or '').lower() for row in rows}
                if not rows or not states <= {'played', 'dnp'} or len(states) != 1:
                    reasons.append('The player’s participation in the latest completed team game is missing, unknown or conflicting.')
                    continue
                if 'played' in states:
                    minutes = {_number(row.get('minutes')) for row in rows} - {None}
                    if len(minutes) != 1 or next(iter(minutes), 0) <= 0:
                        reasons.append('The player’s latest played observation lacks consistent verified minutes.')
                        continue
                    supported = set()
                    for market, components in STAT_COMPONENTS.items():
                        values = [tuple(_number(row.get(field)) for field in components) for row in rows]
                        complete = [value for value in values if None not in value]
                        if (complete and all(len({value[index] for value in values if value[index] is not None}) == 1
                                             for index in range(len(components)))):
                            supported.add(market)
                    current_stats &= supported
                observed.append(game['date'])
        if len(observed) == len(latest_games):
            result['player_data_through_date'] = latest['date']
            result['current_player_stat_coverage'] = sorted(current_stats)
            if stat is not None and stat not in current_stats:
                reasons.append('The requested statistic is missing or conflicting in the player’s latest completed game.')
    result['reasons'] = list(dict.fromkeys(reasons))
    result['ready'] = not result['reasons']
    return result
