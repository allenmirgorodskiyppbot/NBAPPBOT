"""One sourced Knicks injury digest per Eastern game day, independent of AI/stats.

A fresh, explicit schedule is required. News cannot establish a tipoff, and an
unreachable schedule is never interpreted as an off day. Schedule-only records
stay outside the game/result ledger so a calendar cannot settle an entry.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from nba_cdn import SCHEDULE_URL, parse_game
from news_digest import _aware, _categories, _clean, _metadata, _publication_label, _url, is_promotion

EASTERN = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
SCHEDULE_KEY = 'knicks_digest_schedule_v1'
STATUS_KEY = 'knicks_digest_status'


def alert_mode():
    value = os.getenv('NEWS_ALERT_MODE', 'knicks_gameday').strip().lower()
    if value not in {'knicks_gameday', 'instant', 'off'}:
        raise ValueError('NEWS_ALERT_MODE must be knicks_gameday, instant or off.')
    return value


def parse_schedule(document, today, source):
    """Validate the official complete season calendar; reject old seasons/TBD tips."""
    league = document.get('leagueSchedule') if isinstance(document, dict) else None
    if not isinstance(league, dict) or not isinstance(league.get('gameDates'), list):
        raise ValueError('Schedule must use the NBA leagueSchedule/gameDates JSON format.')
    season = str(league.get('seasonYear', ''))
    year = today.year - (today.month < 7)
    if season not in {f'{year}-{str(year + 1)[-2:]}', str(year)}:
        raise ValueError('Schedule season is not current; automatic reminder timing is unavailable.')
    games, seen, total = [], set(), 0
    for day in league['gameDates']:
        if not isinstance(day, dict) or not isinstance(day.get('games'), list):
            raise ValueError('Schedule date is missing its games list.')
        for payload in day['games']:
            total += 1
            if total > 2500:
                raise ValueError('Schedule contains too many games.')
            if not isinstance(payload, dict):
                raise ValueError('Invalid schedule game.')
            teams = {(payload.get(side + 'Team') or {}).get('teamTricode') for side in ('home', 'away')}
            if 'NYK' not in teams:
                continue
            game = parse_game(payload, source=source, fallback_date=day.get('gameDate'), season=season)
            if game['provider_game_id'] in seen:
                raise ValueError('Schedule has duplicate Knicks game identities.')
            seen.add(game['provider_game_id'])
            if not game.get('start_time') or str(payload.get('postponedStatus', '')).upper() == 'P':
                game['status'] = 'postponed' if str(payload.get('postponedStatus', '')).upper() == 'P' else 'time_tbd'
            # Never guess a start from midnight placeholders or an explicit TBD.
            if re.search(r'\b(?:TBD|TBA)\b', str(payload.get('gameStatusText', '')), re.I):
                game['status'] = 'time_tbd'
            games.append(game)
    if not games:
        raise ValueError('Current-season schedule contains no Knicks games; timing is unavailable.')
    if len(games) < 70:
        raise ValueError('Schedule is incomplete; a partial calendar cannot establish Knicks off days.')
    return sorted(games, key=lambda game: (game['date'], game['id']))


def relevant_reports(store, opponent, now):
    """Match both teams/names to collected reports, preserving uncertain timestamps."""
    from feeds import TEAM_WORDS
    teams = {'NYK', opponent}
    with store.connect() as db:
        # The latest dated team per player is evidence for name matching, not a
        # claim that the historical workbook is a current official roster.
        names = db.execute('WITH ranked AS (SELECT player,team,ROW_NUMBER() OVER '
                           '(PARTITION BY player_id ORDER BY date DESC,updated_at DESC) AS place '
                           'FROM player_games WHERE date<=?) '
                           'SELECT player,team FROM ranked WHERE place=1 AND team IN (?,?)',
                           (now.astimezone(EASTERN).date().isoformat(), 'NYK', opponent)).fetchall()
        rows = [dict(row) for row in db.execute('SELECT * FROM news ORDER BY updated_at DESC LIMIT 1000')]
    aliases = [TEAM_WORDS[t] for t in teams if t in TEAM_WORDS]
    aliases.extend(str(row['player']) for row in names if row['player'])
    patterns = [re.compile(r'\b' + re.escape(value) + r'\b', re.I) for value in set(aliases)]
    reports = []
    for item in rows:
        if is_promotion(item):
            continue
        title, body = _clean(item.get('title'), 220), _clean(item.get('body'), 4000)
        if not _categories(title, body):
            continue
        metadata = _metadata(item)
        text = title + ' ' + body
        if item.get('team') not in teams and metadata.get('team') not in teams and not any(p.search(text) for p in patterns):
            continue
        published, collected = _aware(item.get('published_at')), _aware(item.get('updated_at'))
        if published and not -300 <= (now - published).total_seconds() <= 48 * 3600:
            continue
        if not published:
            # Unknown publisher time is never replaced by the collection time.
            # An old date cannot become breaking news just because it was fetched.
            try:
                clock = dt.datetime.fromisoformat(str(item.get('published_at')))
                if clock.date() < now.astimezone(EASTERN).date() - dt.timedelta(days=2):
                    continue
            except (ValueError, TypeError):
                pass
            if not collected or not -300 <= (now - collected).total_seconds() <= 48 * 3600:
                continue
        rank = (0 if published else 1, -(published or collected).timestamp())
        reports.append((rank, item))
    reports.sort(key=lambda value: value[0])
    seen, result = set(), []
    for _, item in reports:
        # RotoWire commonly links multiple updates to the same player page.
        key = (item.get('source'), item.get('url') or item['id'])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def render_digest(store, game, now, schedule_checked, refresh_ok):
    opponent = game['away_team'] if game['home_team'] == 'NYK' else game['home_team']
    tip = _aware(game['start_time']).astimezone(EASTERN)
    lines = [f"Knicks game-day injury / roster update — {tip.strftime('%b %d')}",
             f"NYK vs {opponent} · tipoff {tip.strftime('%I:%M %p').lstrip('0')} Eastern",
             f"Reports checked: {now.astimezone(EASTERN).strftime('%I:%M %p').lstrip('0')} Eastern"]
    feeds = store.status()['feeds']
    news_feeds = [f for f in feeds if f['name'].startswith('rss_') or f['name'] in {'espn_news', 'injuries_configured'}]
    fresh = [f for f in news_feeds if not f.get('error') and _aware(f.get('last_success'))
             and -300 <= (now - _aware(f['last_success'])).total_seconds() <= 1800]
    failures = [f for f in news_feeds if f.get('error') and not f['error'].startswith('Disabled:')]
    if not refresh_ok or not fresh:
        lines.append('Fresh news check incomplete; reports below may be stale. See /status.')
    elif failures:
        lines.append('Some configured news feeds failed their latest check; coverage is partial. See /status.')
    candidates = relevant_reports(store, opponent, now)
    count = 0
    for item in candidates:
        block = (f"• {_clean(item['title'], 220)}\n"
                 f"Published: {_publication_label(item.get('published_at'))}\n"
                 f"Source: {_clean(item.get('source'), 80)}")
        url = _url(item.get('url'))
        if url:
            block += '\n' + url
        # Leave room for provenance and coverage while keeping one Telegram chunk.
        if len('\n\n'.join(lines)) + len(block) > 2600 or count == 7:
            break
        lines.append(block)
        count += 1
    if not count:
        lines.append('No recent matching Knicks/opponent reports were found in the collected feeds. This does not mean everyone is available.')
    elif len(candidates) > count:
        lines.append(f'{count} of {len(candidates)} matching reports shown; /news has the archive.')
    lines.append('Publisher reports/manual notes; not independently verified or a complete official injury report. '
                 'Roster name matching also uses stored game history. Later changes may occur; /summary remains available on request.')
    source = urlsplit(game.get('source') or SCHEDULE_URL)
    public_source = source._replace(query='', fragment='').geturl()
    lines.append('Schedule checked: ' + str(schedule_checked) + '\nSource: ' + public_source[:250])
    return '\n\n'.join(lines)[:3450]


class GamedayDigest:
    def __init__(self, store, worker, notify, now=None):
        self.store, self.worker, self.notify = store, worker, notify
        self.now = now or (lambda: dt.datetime.now(UTC))
        self.url = os.getenv('KNICKS_SCHEDULE_URL', SCHEDULE_URL).strip() or SCHEDULE_URL
        parsed = urlsplit(self.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('KNICKS_SCHEDULE_URL must be HTTPS without URL credentials.')
        self.lead = max(15, min(180, int(os.getenv('KNICKS_DIGEST_MINUTES_BEFORE', '60'))))
        self._lock = asyncio.Lock()

    def _status(self, state, **values):
        report = {'mode': alert_mode(), 'state': state, 'minutes_before_tipoff': self.lead,
                  'timezone': 'America/New_York', 'checked_at': self.now().isoformat(), **values}
        self.store.set_checkpoint(STATUS_KEY, report)
        return report

    async def refresh_schedule(self):
        current = self.now()
        snapshot = self.store.get_checkpoint(SCHEDULE_KEY, {})
        checked = _aware(snapshot.get('checked_at'))
        # A regular 15-minute refresh also checks changed times/cancellations.
        if (snapshot.get('url') == self.url and checked and
                0 <= (current - checked).total_seconds() < 900):
            return snapshot
        attempt = _aware(self.store.get_checkpoint('knicks_schedule_attempt'))
        if attempt and 0 <= (current - attempt).total_seconds() < 900:
            return snapshot if snapshot.get('url') == self.url else {}
        self.store.set_checkpoint('knicks_schedule_attempt', current.isoformat())
        try:
            document = await self.worker.request(self.url)
            games = parse_schedule(document, current.astimezone(EASTERN).date(), self.url)
            snapshot = {'games': games, 'checked_at': current.isoformat(), 'url': self.url}
            self.store.set_checkpoint(SCHEDULE_KEY, snapshot)
            self.store.feed_success('knicks_schedule', 'Independent Knicks game-day timing; NBA season schedule; no game statistics imported.')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Never include a configured URL/query or arbitrary exception message.
            code = getattr(getattr(error, 'response', None), 'status_code', None)
            self.store.feed_failure('knicks_schedule', type(error).__name__ + (f': HTTP {code}' if code else '')
                                    + '; automatic digest timing unavailable without a current reachable schedule.')
        return snapshot if snapshot.get('url') == self.url else {}

    async def tick(self):
        async with self._lock:
            mode = alert_mode()
            if mode != 'knicks_gameday':
                return self._status('disabled')
            snapshot = await self.refresh_schedule()
            current = self.now()
            checked = _aware(snapshot.get('checked_at'))
            if not checked or not 0 <= (current - checked).total_seconds() < 1200:
                return self._status('schedule_unavailable', detail='No fresh verified schedule; no game time or off day assumed. See /status or /reminder.')
            today = current.astimezone(EASTERN).date().isoformat()
            games = [g for g in snapshot.get('games', []) if g['date'] == today]
            future = [g for g in snapshot.get('games', []) if g['date'] >= today
                      and g['status'] == 'scheduled' and _aware(g.get('start_time'))
                      and _aware(g['start_time']) > current]
            next_game = min(future, key=lambda g: g['start_time']) if future else None
            next_info = ({'next_game': next_game['id'],
                          'opponent': next_game['away_team'] if next_game['home_team'] == 'NYK' else next_game['home_team'],
                          'tipoff': next_game['start_time'],
                          'send_at': (_aware(next_game['start_time']) - dt.timedelta(minutes=self.lead)).isoformat()}
                         if next_game else {})
            if not games:
                return self._status('off_day', **next_info)
            game = next((g for g in games if g['status'] == 'scheduled' and _aware(g.get('start_time'))), None)
            if not game:
                return self._status('no_upcoming_tipoff_today', **next_info)
            tip = _aware(game['start_time'])
            due = tip - dt.timedelta(minutes=self.lead)
            if current >= tip:
                return self._status('game_started', **next_info)
            key = 'knicks-gameday:' + today
            if self.store.get_checkpoint(key):
                return self._status('already_queued_today', game=game['id'], tipoff=tip.isoformat())
            if current < due:
                return self._status('waiting', **next_info)
            # Collect again just before composing; the regular polling cycle remains
            # active on off days but is silent. This path never waits for the model.
            refreshed = True
            try:
                async with asyncio.timeout(90):
                    async with self.worker._cycle_lock:
                        await self.worker.collect_news_sources()
            except asyncio.CancelledError:
                raise
            except Exception:
                refreshed = False
            current = self.now()
            if current >= tip:
                return self._status('game_started', **next_info)
            message = await asyncio.to_thread(render_digest, self.store, game, current,
                                              snapshot['checked_at'], refreshed)
            accepted = await self.notify(message, event_key=key, expires_at=tip.timestamp())
            if accepted:
                self.store.set_checkpoint(key, {'queued_at': current.isoformat(), 'game': game['id'], 'tipoff': tip.isoformat()})
                return self._status('queued', game=game['id'], tipoff=tip.isoformat(), queued_at=current.isoformat())
            return self._status('telegram_disabled', **next_info)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.store.feed_failure('knicks_digest', type(error).__name__ + '; see /reminder for timing status.')
            await asyncio.sleep(60)


def reminder_status(store):
    report = store.get_checkpoint(STATUS_KEY, {})
    if not report:
        report = {'mode': alert_mode(), 'state': 'starting', 'detail': 'Schedule has not been checked yet.'}
    state = report.get('state')
    lines = ['Knicks injury reminders: ' + report.get('mode', alert_mode())]
    if state == 'schedule_unavailable':
        lines.append('Automatic timing is blocked: the current schedule could not be verified. '
                     'No off day or tipoff is assumed. /status shows the knicks_schedule error; '
                     '/diagnose tests the public schedule connection.')
    elif state == 'off_day':
        lines.append('No Knicks game today. No automatic injury message today.')
    elif state in {'queued', 'already_queued_today'}:
        lines.append('Today’s one game-day digest has been queued. Telegram retries delivery until tipoff.')
    elif state in {'game_started', 'no_upcoming_tipoff_today'}:
        lines.append('No remaining confirmed tipoff today; no late injury digest will be sent.')
    elif state == 'telegram_disabled':
        lines.append('Telegram delivery is not configured; check the private bot settings.')
    elif state in {'starting', 'disabled'}:
        lines.append('Waiting for the first schedule check.' if state == 'starting' else 'Game-day digest is disabled.')
    tip, due = _aware(report.get('tipoff')), _aware(report.get('send_at'))
    if tip and due:
        lines.extend([f"Next game: NYK vs {report.get('opponent', '?')} — " + tip.astimezone(EASTERN).strftime('%b %d, %I:%M %p Eastern'),
                      'Digest time: ' + due.astimezone(EASTERN).strftime('%b %d, %I:%M %p Eastern')])
    if report.get('mode') == 'knicks_gameday':
        lines.append(f"One digest per Knicks game day, {report.get('minutes_before_tipoff', 60)} minutes before tipoff. "
                     'News collection continues quietly; /summary is available whenever you ask.')
    return '\n\n'.join(lines)
