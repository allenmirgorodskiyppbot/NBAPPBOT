"""Recheck existing tentative pairings without inventing game-day defenders.

The original assignment date stays attached to the original names. Current
roster observations, game-specific publisher reports and directional measured
history are separate evidence. None of them confirms a future coaching choice.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from news_digest import _aware

EASTERN = ZoneInfo('America/New_York')
MAX_EVIDENCE_DAYS = 7


def _now(value=None):
    value = _aware(value) if isinstance(value, str) else value
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _season(now):
    local = now.astimezone(EASTERN)
    year = local.year - (local.month < 7)
    return f'{year}-{str(year + 1)[-2:]}'


def _key(row):
    identity = [row.get(k) for k in ('assignment_id', '_row', 'selected_player',
                                     'opponent_team', 'primary', 'alternate', 'assignment_asof')]
    return hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()


def _sources(master, fallback_rows=None, now=None):
    today = _now(now).astimezone(EASTERN).date()
    rows = list(fallback_rows or [])
    if master is not None and callable(getattr(master, 'connect', None)):
        with master.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_evidence'").fetchone():
                indexed = [json.loads(r[0]) for r in db.execute(
                    "SELECT payload_json FROM workbook_evidence WHERE kind='assignments'")]
                # A supplied workbook is authoritative for its own source rows.
                # Do not resurrect deleted/changed rows from a previous seed.
                if fallback_rows is None:
                    rows = indexed
    return [dict(row) for row in rows if row.get('selected_player') and row.get('opponent_team')
            and _day(row.get('assignment_asof')) and _day(row['assignment_asof']) <= today]


def _next_games(db, now):
    today = now.astimezone(EASTERN).date().isoformat()
    games = {}
    for table in ('season_schedule', 'games'):
        for record in db.execute(f'SELECT * FROM {table} WHERE date>=? '
                                 "AND (home_team='NYK' OR away_team='NYK')", (today,)):
            row = dict(record)
            games[row['id']] = {**games.get(row['id'], {}),
                                **{k: v for k, v in row.items() if v is not None}}
    selected = {}
    for row in sorted(games.values(), key=lambda r: (r.get('start_time') or '', r['id'])):
        start = _aware(row.get('start_time'))
        if row.get('status') != 'scheduled' or not start or start <= now:
            continue
        if _season(start) != _season(now):
            continue
        opponent = row['away_team'] if row['home_team'] == 'NYK' else row['home_team']
        selected.setdefault(opponent, row)
    return selected


def _rosters(db, now):
    today = now.astimezone(EASTERN).date()
    rows = db.execute("SELECT p.player,p.team,p.date,p.source FROM player_games p "
                      "JOIN games g ON g.id=p.game_id WHERE g.status='final' "
                      "AND p.season=? AND p.season_type='Regular Season' "
                      "AND LOWER(p.appearance_state)='played' AND p.date<=? "
                      "ORDER BY p.date DESC,p.player", (_season(now), today.isoformat()))
    result = {}
    for item in rows:
        record = dict(item)
        day = _day(record['date'])
        if not day or not 0 <= (today - day).days <= MAX_EVIDENCE_DAYS:
            continue
        old = result.get(record['player'])
        if old is None:
            result[record['player']] = {**record, 'conflicting_teams': False}
        elif old['date'] == record['date'] and old['team'] != record['team']:
            old['conflicting_teams'] = True
    return result


def _candidate_check(name, role, expected_team, rosters, news, game_date, now):
    from board_shortlist import player_availability
    row = rosters.get(name)
    state = ('unknown' if row is None else 'conflicting_observations' if row['conflicting_teams']
             else 'team_conflict' if row['team'] != expected_team else 'observed_with_expected_team')
    game_day = _day(game_date)
    near_game = game_day and 0 <= (game_day - now.astimezone(EASTERN).date()).days <= 2
    availability = (player_availability(news, name, game_date, now) if near_game else
                    {'blocked': False, 'note': None, 'reports': []})
    return {'player': name, 'role': role, 'expected_team': expected_team,
            'roster_state': state, 'observed_team': row.get('team') if row else None,
            'observed_date': row.get('date') if row else None,
            'roster_source': row.get('source') if row else None,
            'game_date': game_date, 'availability': availability,
            'note': 'Recent appearances are roster evidence, not confirmation of the next lineup or assignment.'}


def _measured_history(records, selected, opponent, rosters, now):
    """Return directional historical evidence; never a projected future assignment."""
    result = []
    today = now.astimezone(EASTERN).date()
    for row in records:
        names = str(row.get('entity') or '').split(' | ')
        if len(names) != 2 or selected not in names or names[0] == names[1]:
            continue
        if row.get('season') != _season(now) or row.get('season_type') != 'Regular Season':
            continue
        stamp = _aware(row.get('published_at'))
        covered = _day(row.get('data_through_date'))
        if not stamp or not 0 <= (now - stamp).total_seconds() <= MAX_EVIDENCE_DAYS * 86400:
            continue
        if covered and (covered > today or (today - covered).days > MAX_EVIDENCE_DAYS):
            continue
        other = names[1] if names[0] == selected else names[0]
        if any(not rosters.get(name) or rosters[name]['conflicting_teams'] or
               rosters[name]['team'] != team for name, team in ((selected, 'NYK'), (other, opponent))):
            continue
        fields = row.get('fields') or {}
        volume = fields.get('partial_possessions')
        if type(volume) not in (int, float) or not math.isfinite(volume) or volume <= 0:
            volume = fields.get('matchup_minutes')
        if type(volume) not in (int, float) or not math.isfinite(volume) or volume <= 0:
            continue
        result.append({'offensive_player': names[0], 'defender': names[1],
                       'direction': 'opponent_defended_selected' if names[0] == selected else 'selected_defended_opponent',
                       'fields': {k: v for k, v in fields.items()
                                  if type(v) in (int, float) and math.isfinite(v) and v >= 0},
                       'season': row['season'], 'season_type': row['season_type'],
                       'source': row.get('source'), 'published_at': row['published_at'],
                       'data_through_date': row.get('data_through_date'),
                       'population': 'Actual historical measured pair; not a future assignment',
                       'verified_game_day_assignment': False, '_volume': volume})
    result.sort(key=lambda row: (-row['_volume'], row['offensive_player'], row['defender']))
    selected_rows = []
    for direction in ('opponent_defended_selected', 'selected_defended_opponent'):
        selected_rows.extend({k: v for k, v in row.items() if k != '_volume'}
                             for row in [r for r in result if r['direction'] == direction][:2])
    return selected_rows


def refresh_assignments(master, now=None):
    """Persist current checks of the existing assignment scope; return status."""
    now = _now(now)
    source_rows = _sources(master, now=now)
    news = master.recent_news(1000)
    with master.connect() as db:
        rosters = _rosters(db, now)
        next_games = _next_games(db, now)
        measured = [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM refreshed_tracking WHERE category='Named pair'")]
    result = []
    for original in source_rows:
        source_day = _day(original['assignment_asof'])
        source_year = source_day.year - (source_day.month < 7)
        source_season = f'{source_year}-{str(source_year + 1)[-2:]}'
        if source_season != _season(now):
            result.append({**original, 'source_asof': original['assignment_asof'],
                           'last_checked': now.isoformat(), 'next_game_id': None,
                           'next_game_date': None, 'assignment_direction': 'unspecified',
                           'verified_game_day_assignment': False, 'refresh_state': 'historical_snapshot',
                           'refresh_note': 'Historical assignment source preserved; current rosters do not revise historical estimates.',
                           'candidate_checks': [], 'historical_measured_candidates': []})
            continue
        game = next_games.get(original['opponent_team'])
        game_date = game['date'] if game else None
        checks = [_candidate_check(original['selected_player'], 'selected_player', 'NYK',
                                   rosters, news, game_date, now)]
        for role in ('primary', 'alternate'):
            if original.get(role):
                checks.append(_candidate_check(original[role], role, original['opponent_team'],
                                               rosters, news, game_date, now))
        needs_review = any(check['roster_state'] in {'team_conflict', 'conflicting_observations'}
                           or check['availability']['blocked'] for check in checks)
        history = _measured_history(measured, original['selected_player'], original['opponent_team'], rosters, now)
        note = ('Prior tentative names need review because current collected evidence conflicts with their use. '
                if needs_review else 'Original tentative names retained; no newer verified assignment source is available. ')
        note += 'A recheck is not a new assignment date or proof of who will guard whom.'
        result.append({**original, 'source_asof': original['assignment_asof'],
                       'last_checked': now.isoformat(), 'next_game_id': game.get('id') if game else None,
                       'next_game_date': game_date, 'assignment_direction': 'unspecified',
                       'verified_game_day_assignment': False,
                       'refresh_state': 'needs_review' if needs_review else 'legacy_estimate_rechecked',
                       'refresh_note': note, 'candidate_checks': checks,
                       'historical_measured_candidates': history})
    result = list({_key(row): row for row in result}.values())
    revision = hashlib.sha256(json.dumps(
        [{k: v for k, v in row.items() if k != 'last_checked'} for row in result],
        sort_keys=True, default=str).encode()).hexdigest()
    with master.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS refreshed_assignments '
                   '(source_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL)')
        db.execute('DELETE FROM refreshed_assignments')
        db.executemany('INSERT INTO refreshed_assignments VALUES (?,?)',
                       [(_key(row), json.dumps(row, default=str)) for row in result])
    status = {'checked_at': now.isoformat(), 'revision': revision, 'records': len(result),
              'needs_review': sum(row['refresh_state'] == 'needs_review' for row in result),
              'unresolved_roster_records': sum(any(check.get('roster_state') == 'unknown'
                                                   for check in row['candidate_checks']) for row in result),
              'with_directional_historical_evidence': sum(bool(row['historical_measured_candidates']) for row in result),
              'new_verified_assignments': 0,
              'state': 'rechecked' if result else 'no_existing_assignment_scope',
              'note': 'Checks use recent roster observations and collected game-specific reports. '
                      'Original assignment dates are preserved; directional history is not a projected defender.'}
    master.set_checkpoint('assignment_refresh_status', status)
    return status


def assignment_rows_for(master, fallback_rows=None, now=None):
    """Read shared assignment evidence without writes or hidden refresh calls."""
    now = _now(now)
    rows = _sources(master, fallback_rows, now)
    if master is None or not callable(getattr(master, 'connect', None)):
        return rows
    with master.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='refreshed_assignments'").fetchone():
            return rows
        saved = {row[0]: json.loads(row[1]) for row in db.execute(
            'SELECT source_key,payload_json FROM refreshed_assignments')}
    result = []
    for source in rows:
        record = saved.get(_key(source))
        if record is None:
            result.append(source)
            continue
        checked = _aware(record.get('last_checked'))
        if not checked or checked > now:
            result.append(source)
            continue
        if record.get('refresh_state') != 'historical_snapshot' and (now - checked).total_seconds() > 24 * 3600:
            record = {**record, 'refresh_state': 'recheck_overdue',
                      'refresh_note': 'The saved recheck is over 24 hours old; current roster and availability are unresolved.'}
        result.append(record)
    return result


def assignment_refresh_status(master):
    return master.get_checkpoint('assignment_refresh_status', {})


def refresh_caution(row):
    """Bounded canonical wording shared by direct answers and AI evidence."""
    if not row.get('last_checked'):
        return ''
    if row.get('refresh_state') == 'historical_snapshot':
        return 'Historical assignment estimate preserved with its original source date.'
    checked = _aware(row['last_checked'])
    checked_date = checked.astimezone(EASTERN).date().isoformat() if checked else str(row['last_checked'])[:10]
    parts = [f"Rechecked {checked_date} Eastern; original assignment date unchanged."]
    if row.get('refresh_state') == 'recheck_overdue':
        parts.append('That recheck is overdue; current availability is unresolved.')
        return ' '.join(parts)
    for check in row.get('candidate_checks', []):
        if check.get('roster_state') == 'team_conflict':
            parts.append(f"{check['player']} was last observed with {check['observed_team']} on "
                         f"{check['observed_date']}; this conflicts with the saved team pairing.")
        elif check.get('roster_state') == 'conflicting_observations':
            parts.append(f"{check['player']} has conflicting stored team observations.")
        note = (check.get('availability') or {}).get('note')
        if note:
            parts.append(check['player'] + ': ' + note)
    if len(parts) == 1:
        parts.append('No newer verified game-day assignment is available; missing reports do not establish availability.')
    return ' '.join(parts)[:1000]
