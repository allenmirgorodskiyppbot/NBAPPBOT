"""Official public NBA CDN game adapter, selected independently of other providers.

CDN access is not guaranteed for cloud hosts. Authorization failures propagate to
the worker's circuit breaker; this module does not alter identity or retry on a
different host. Schemas are documented by nba_api/live endpoints and ScheduleLeagueV2.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import time
from zoneinfo import ZoneInfo

import httpx

SCHEDULE_URL = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json'
SCOREBOARD_URL = 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'
BOX_URL = 'https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{}.json'
EASTERN = ZoneInfo('America/New_York')


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        return int(value) if value.is_integer() else value
    except (TypeError, ValueError):
        return None


def parse_duration(value):
    """NBA live minutes use ISO8601 (PT25M01.00S), not the game clock."""
    if not isinstance(value, str):
        return number(value)
    match = re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?', value)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(x or 0) for x in match.groups())
        return hours * 60 + minutes + seconds / 60
    if re.fullmatch(r'\d+:\d{2}(?:\.\d+)?', value):
        minutes, seconds = value.split(':')
        return float(minutes) + float(seconds) / 60 if float(seconds) < 60 else None
    return number(value)


def status_of(game):
    text = str(game.get('gameStatusText') or '').strip().casefold()
    if 'postpon' in text or text in {'ppd', 'postponed'}:
        return 'postponed'
    if 'cancel' in text:
        return 'canceled'
    if 'suspend' in text or 'abandon' in text:
        raise ValueError('NBA game has an unresolved suspended/abandoned status.')
    value = number(game.get('gameStatus'))
    if value not in {1, 2, 3}:
        raise ValueError('Unknown NBA gameStatus; no final result assumed.')
    return {1: 'scheduled', 2: 'live', 3: 'final'}[value]


def game_start(game):
    for field in ['gameTimeUTC', 'gameDateTimeUTC', 'gameTimeLocal', 'gameTimeHome']:
        value = game.get(field)
        if not value:
            continue
        try:
            instant = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if instant.year < 2000:
                continue
            if instant.tzinfo is None:
                if 'UTC' not in field:
                    continue  # Arena-local wall time is not necessarily Eastern.
                instant = instant.replace(tzinfo=dt.timezone.utc)
            return instant
        except (TypeError, ValueError):
            continue
    # Schedule EST fields use wall-clock Eastern time despite a misleading Z suffix.
    value = game.get('gameDateTimeEst')
    if value:
        try:
            return dt.datetime.fromisoformat(str(value).rstrip('Z')).replace(tzinfo=EASTERN)
        except (TypeError, ValueError):
            pass
    return None


def day_value(value):
    value = str(value or '')
    for pattern in ['%Y-%m-%d', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y']:
        try:
            return dt.datetime.strptime(value[:10] if pattern == '%Y-%m-%d' else value, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError('NBA schedule has no usable game date.')


def season_info(game_id, date, season=None):
    if season and re.fullmatch(r'\d{4}-\d{2}', str(season)):
        year = int(str(season)[:4])
    elif re.fullmatch(r'\d{10}', str(game_id)):
        year = 2000 + int(str(game_id)[3:5])
    else:
        year = int(date[:4]) - (int(date[5:7]) < 7)
    phase = {'001': 'Preseason', '002': 'Regular Season', '003': 'All-Star',
             '004': 'Playoffs', '005': 'Play-In', '006': 'NBA Cup Final'}.get(str(game_id)[:3], 'Unknown')
    return f'{year}-{str(year + 1)[-2:]}', phase


def parse_game(payload, source=SCHEDULE_URL, fallback_date=None, season=None):
    # Lazy shared imports let feeds.py select this adapter without import cycles.
    from feeds import canonical_game, canonical_team
    native_id = str(payload.get('gameId') or '')
    if not re.fullmatch(r'\d{10}', native_id):
        raise ValueError('NBA game requires a native ten-digit gameId.')
    home = canonical_team((payload.get('homeTeam') or {}).get('teamTricode'))
    away = canonical_team((payload.get('awayTeam') or {}).get('teamTricode'))
    if not home or not away or home == away or home in {'TBD', 'TBA'} or away in {'TBD', 'TBA'}:
        raise ValueError('NBA game has unresolved team identities.')
    start = game_start(payload)
    date = start.astimezone(EASTERN).date().isoformat() if start else day_value(
        payload.get('gameDateEst') or fallback_date)
    status = status_of(payload)
    season, phase = season_info(native_id, date, season)
    result = {'id': canonical_game(date, home, away), 'date': date, 'status': status,
              'home_team': home, 'away_team': away, 'provider_game_id': native_id,
              'source': source, 'nba_season': season, 'nba_season_type': phase}
    if start:
        result['start_time'] = start.astimezone(dt.timezone.utc).isoformat()
    if status in {'live', 'final'}:
        for side in ['home', 'away']:
            score = number((payload.get(side + 'Team') or {}).get('score'))
            if score is not None:
                result[side + '_score'] = score
    return result


def parse_players(document, game):
    from feeds import canonical_player, canonical_team
    payload = document.get('game')
    if not isinstance(payload, dict) or str(payload.get('gameId')) != game['provider_game_id']:
        raise ValueError('NBA box score game identity does not match the requested game.')
    rows, seen = [], set()
    final = game['status'] == 'final'
    for side in ['home', 'away']:
        team = payload.get(side + 'Team') or {}
        abbreviation = canonical_team(team.get('teamTricode'))
        if abbreviation != game[side + '_team']:
            raise ValueError('NBA box score team identity does not match the schedule.')
        players = team.get('players')
        if not isinstance(players, list) or (final and not players):
            raise ValueError('NBA box score has an incomplete player roster.')
        for player in players:
            name = str(player.get('name') or '').strip()
            if not name:
                name = ' '.join(str(player.get(k) or '').strip() for k in ['firstName', 'familyName']).strip()
            # Never key a canonical player from NBA's abbreviated nameI field.
            if not name or re.match(r'^[A-Za-z]\.\s', name):
                raise ValueError('NBA player full name is unavailable or abbreviated.')
            native_id = str(player.get('personId') or '')
            if not native_id.isdigit() or int(native_id) <= 0:
                raise ValueError('NBA player native identity is missing.')
            identity = canonical_player(name)
            if identity in seen:
                raise ValueError('NBA player names collide; identity requires manual review.')
            seen.add(identity)
            statistics = player.get('statistics') or {}
            if not isinstance(statistics, dict):
                raise ValueError('NBA player statistics have an unsupported shape.')
            played = player.get('played')
            minute_value = parse_duration(statistics.get('minutes'))
            if minute_value is None:
                minute_value = parse_duration(statistics.get('minutesCalculated'))
            pts = number(statistics.get('points'))
            if played in {'1', 1, True}:
                appearance = 'Played'
            elif played in {'0', 0, False}:
                if not final:
                    continue  # A bench player who has not entered a live game is not DNP.
                if (minute_value or 0) > 0 or (pts or 0) > 0:
                    raise ValueError('NBA played flag conflicts with observed player statistics.')
                appearance = 'DNP'
            elif minute_value is not None and minute_value > 0:
                appearance = 'Played'
            else:
                raise ValueError('NBA player appearance is unknown; no DNP inferred.')
            if final and appearance == 'Played' and (pts is None or minute_value is None):
                raise ValueError('Final NBA player row is missing points or minutes; no rows written.')
            row = {'game_id': game['id'], 'player_id': identity, 'player': name,
                   'provider_player_id': native_id, 'nba_player_id': native_id,
                   'team': abbreviation,
                   'opponent': game['away_team'] if side == 'home' else game['home_team'],
                   'date': game['date'], 'season': game['nba_season'],
                   'season_type': game['nba_season_type'], 'appearance_state': appearance,
                   'source': BOX_URL.format(game['provider_game_id'])}
            if appearance == 'Played':
                for source, target in [('points', 'pts'), ('reboundsTotal', 'reb'), ('assists', 'ast'),
                                       ('threePointersMade', 'tpm'), ('threePointersAttempted', 'tpa'),
                                       ('fieldGoalsAttempted', 'fga'), ('steals', 'stl'),
                                       ('blocks', 'blk'), ('turnovers', 'tov')]:
                    value = number(statistics.get(source))
                    if value is not None:
                        row[target] = value
                if minute_value is not None:
                    row['minutes'] = minute_value
            rows.append(row)
        if final and not any(r['team'] == abbreviation and r['appearance_state'] == 'Played' for r in rows):
            raise ValueError('Final NBA team has no observed played rows.')
    return rows


class NBADataSource:
    def __init__(self, store, request, now=None):
        self.store, self.request = store, request
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._schedule = None
        self._fetched_at = 0
        self._season = None

    async def schedule(self):
        if self._schedule is not None and time.monotonic() - self._fetched_at < 3600:
            return self._schedule
        document = await self.request(SCHEDULE_URL)
        league = document.get('leagueSchedule')
        if not isinstance(league, dict) or not isinstance(league.get('gameDates'), list):
            raise ValueError('NBA league schedule shape is invalid.')
        result = {}
        for item in league['gameDates']:
            day = day_value(item.get('gameDate'))
            if not isinstance(item.get('games'), list):
                raise ValueError('NBA schedule date is missing its game list.')
            result.setdefault(day, []).extend(item['games'])
        if not result:
            raise ValueError('NBA league schedule is empty; coverage cannot be established.')
        self._schedule, self._season = result, league.get('seasonYear')
        self._fetched_at = time.monotonic()
        return result

    async def update_games(self, day, with_boxscores=True):
        day = dt.date.fromisoformat(str(day)).isoformat()
        schedule = await self.schedule()
        if day < min(schedule) or day > max(schedule):
            raise ValueError('Requested day is outside this NBA schedule season; historical coverage unavailable.')
        candidates = {str(record.get('gameId')): (record, SCHEDULE_URL)
                      for record in schedule.get(day, [])}
        if day >= (self.now().astimezone(EASTERN).date() - dt.timedelta(days=1)).isoformat() \
                and day <= self.now().astimezone(EASTERN).date().isoformat():
            document = await self.request(SCOREBOARD_URL)
            board = document.get('scoreboard')
            if not isinstance(board, dict) or not isinstance(board.get('games'), list):
                raise ValueError('NBA daily scoreboard shape is invalid.')
            if day_value(board.get('gameDate')) == day:
                for record in board['games']:
                    candidates[str(record.get('gameId'))] = (record, SCOREBOARD_URL)
        previous = {row['id']: row for row in self.store.games(day)}
        count, observations, issues = 0, 0, []
        for record, source in candidates.values():
            try:
                observations += await self.update_one(record, source, day, previous, with_boxscores)
                count += 1
            except ValueError as error:
                issues.append(str(record.get('gameId') or 'unknown') + ': ' + str(error)[:250])
            except TypeError:
                issues.append(str(record.get('gameId') or 'unknown') + ': malformed game data')
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {401, 403, 429}:
                    raise  # Authorization/rate-limit decisions stop this source immediately.
                issues.append(str(record.get('gameId') or 'unknown') + ': HTTP ' + str(error.response.status_code))
        if issues:
            raise ValueError(f'{count} games refreshed; incomplete NBA game data: ' + '; '.join(issues[:5]))
        return count, observations

    async def update_one(self, record, source, day, previous, with_boxscores):
        game = parse_game(record, source, day, self._season)
        if game['date'] != day:
            raise ValueError('NBA game UTC tip time conflicts with the requested Eastern date.')
        old = previous.get(game['id'])
        if old and old.get('status') == 'final' and game['status'] != 'final':
            # A cached schedule cannot undo an observed final or its original score.
            raise ValueError('Stale non-final status conflicts with stored final.')
        if old and old.get('status') == 'live' and game['status'] == 'scheduled':
            raise ValueError('Stale scheduled status conflicts with observed live game.')
        rows = []
        # Final markers are never published without a validated box score, even
        # on a schedule-only request. Normal future scheduled games stay cheap.
        if game['status'] == 'final' or (with_boxscores and game['status'] == 'live'):
            document = await self.request(BOX_URL.format(game['provider_game_id']))
            native_game = document.get('game')
            if not isinstance(native_game, dict):
                raise ValueError('NBA box score is missing its game object.')
            fresh_game = parse_game(native_game, BOX_URL.format(game['provider_game_id']), day, self._season)
            if fresh_game['id'] != game['id'] or fresh_game['provider_game_id'] != game['provider_game_id']:
                raise ValueError('NBA schedule and box score identify different games.')
            if game['status'] == 'final' and fresh_game['status'] != 'final':
                raise ValueError('NBA final schedule has an incomplete/non-final box score.')
            game = {**game, **fresh_game}
            rows = parse_players(document, game)
            # Validate the full final response before changing the game or any player.
            if old:
                existing = self.store.query('SELECT player_id,appearance_state,minutes,pts FROM player_games '
                                            "WHERE game_id='" + game['id'].replace("'", "''") + "'")
                fields = existing.get('columns') or []
                old_players = {r['player_id']: r for r in
                               [dict(zip(fields, row)) for row in existing.get('rows', [])]}
                for row in rows:
                    prior = old_players.get(row['player_id'])
                    if prior and row['appearance_state'] == 'DNP' and (
                            (prior.get('minutes') or 0) > 0 or (prior.get('pts') or 0) > 0):
                        raise ValueError('NBA DNP conflicts with an earlier played observation; review required.')
        atomic_write = getattr(self.store, 'upsert_game_bundle', None)
        if atomic_write is not None:
            await asyncio.to_thread(atomic_write, game, rows)
        else:
            # Fixture adapters without transactions publish the final marker last.
            for row in rows:
                await asyncio.to_thread(self.store.upsert_player_game, row)
            await asyncio.to_thread(self.store.upsert_game, game)
        return len(rows)
