"""Game-day notification policy, scheduling and durable private delivery."""
import asyncio
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from feeds import FeedWorker
from gameday_digest import GamedayDigest, SCHEDULE_KEY, parse_schedule, reminder_status, render_digest
from master import MasterStore
from telegram_bot import TelegramBot, chunks

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 30, 22, tzinfo=UTC)  # 6pm Eastern; tipoff 7pm.
URL = 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json'


def calendar(tip='2026-10-30T23:00:00Z'):
    first = dt.datetime.fromisoformat(tip.replace('Z', '+00:00'))
    dates = []
    for number in range(70):
        start = first + dt.timedelta(days=number * 2)
        dates.append({'gameDate': start.date().isoformat(), 'games': [{
            'gameId': f'00226{number:05d}', 'gameTimeUTC': start.isoformat(),
            'gameStatus': 1, 'gameStatusText': '7:00 pm ET',
            'homeTeam': {'teamTricode': 'NYK'}, 'awayTeam': {'teamTricode': 'CHA'}}]})
    return {'leagueSchedule': {'seasonYear': '2026-27', 'gameDates': dates}}


class ScheduleParsingTests(unittest.TestCase):
    def test_official_schedule_eastern_dates_and_unknown_tipoff(self):
        data = calendar('2026-10-31T00:30:00Z')
        games = parse_schedule(data, NOW.date(), URL)
        self.assertEqual(games[0]['date'], '2026-10-30')
        self.assertEqual(games[0]['start_time'], '2026-10-31T00:30:00+00:00')
        data['leagueSchedule']['gameDates'][0]['games'][0]['gameStatusText'] = 'TBD'
        self.assertEqual(parse_schedule(data, NOW.date(), URL)[0]['status'], 'time_tbd')

    def test_old_or_incomplete_calendar_is_not_an_off_day(self):
        data = calendar()
        data['leagueSchedule']['seasonYear'] = '2025-26'
        with self.assertRaises(ValueError):
            parse_schedule(data, NOW.date(), URL)
        data = calendar()
        data['leagueSchedule']['gameDates'] = data['leagueSchedule']['gameDates'][:1]
        with self.assertRaises(ValueError):
            parse_schedule(data, NOW.date(), URL)
        with self.assertRaises(ValueError):
            parse_schedule({'error': 'Access denied'}, NOW.date(), URL)


class DigestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {'NBA_DATA_SOURCE': 'manual', 'NEWS_SOURCE': 'rss',
            'NEWS_FEED_URLS': 'https://publisher.example/news.xml', 'NEWS_ALERT_MODE': 'knicks_gameday',
            'NEWS_ALERT_SCOPE': 'league', 'KNICKS_DIGEST_MINUTES_BEFORE': '60', 'KNICKS_SCHEDULE_URL': URL})
        env.start()
        self.addCleanup(env.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'master.sqlite'
        self.store = MasterStore(self.path)
        self.current = NOW
        self.document = calendar()
        self.fail_schedule = False
        self.schedule_calls = 0
        self.messages = []
        self.bot = TelegramBot(self.store, AsyncMock(), token='fake-test-token', allowed_user_ids=[123])
        def route(request):
            if request.url.host == 'cdn.nba.com':
                self.schedule_calls += 1
                return httpx.Response(403) if self.fail_schedule else httpx.Response(200, json=self.document)
            if request.url.host == 'publisher.example':
                return httpx.Response(200, text='<rss><channel><item><guid>latest</guid>'
                    '<title>OG Anunoby questionable for Knicks game with ankle injury</title>'
                    '<pubDate>Fri, 30 Oct 2026 21:55:00 +0000</pubDate>'
                    '<link>https://publisher.example/anunoby</link></item></channel></rss>')
            raise AssertionError('Unexpected request: ' + request.url.host)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(route))
        self.addAsyncCleanup(self.client.aclose)
        self.worker = FeedWorker(self.store, notify=self.bot.notify, client=self.client, now=lambda: self.current)
        self.digest = GamedayDigest(self.store, self.worker, self.bot.notify, now=lambda: self.current)

    def rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox')]

    async def test_due_digest_refreshes_reports_and_survives_restart_without_duplicate(self):
        result = await self.digest.tick()
        self.assertEqual(result['state'], 'queued')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn('OG Anunoby', rows[0]['text'])
        self.assertIn('queued', reminder_status(self.store))
        self.assertEqual(rows[0]['expires_at'], (NOW + dt.timedelta(hours=1)).timestamp())
        self.assertEqual(len(list(chunks(rows[0]['text']))), 1)
        self.assertEqual(self.store.status()['counts']['games'], 0)  # No result ledger pollution.
        restarted = GamedayDigest(MasterStore(self.path), self.worker, self.bot.notify, now=lambda: self.current)
        await asyncio.gather(restarted.tick(), restarted.tick())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.schedule_calls, 1)

    async def test_off_day_is_quiet_but_news_is_still_collected(self):
        self.current = NOW - dt.timedelta(days=1)
        self.assertEqual((await self.digest.tick())['state'], 'off_day')
        await self.worker.collect_once()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.status()['counts']['news'], 1)
        text = reminder_status(self.store)
        self.assertIn('No Knicks game today', text)
        self.assertIn('06:00 PM Eastern', text)

    async def test_before_due_waits_then_updated_tipoff_changes_send_time(self):
        self.current = NOW - dt.timedelta(hours=1)
        self.assertEqual((await self.digest.tick())['state'], 'waiting')
        self.assertEqual(self.rows(), [])
        self.current = NOW
        self.document = calendar('2026-10-31T00:00:00Z')
        self.assertEqual((await self.digest.tick())['state'], 'waiting')
        self.assertIn('07:00 PM Eastern', reminder_status(self.store))
        self.current += dt.timedelta(hours=1)
        self.assertEqual((await self.digest.tick())['state'], 'queued')
        self.assertEqual(len(self.rows()), 1)

    async def test_blocked_schedule_fails_closed_without_spamming_or_guessing(self):
        self.fail_schedule = True
        self.assertEqual((await self.digest.tick())['state'], 'schedule_unavailable')
        await self.digest.tick()
        self.assertEqual(self.schedule_calls, 1)
        self.assertEqual(self.rows(), [])
        self.assertIn('blocked', reminder_status(self.store))
        self.assertIn('HTTP 403', next(f for f in self.store.status()['feeds'] if f['name'] == 'knicks_schedule')['error'])
        reopened = GamedayDigest(MasterStore(self.path), self.worker, self.bot.notify, now=lambda: self.current)
        await reopened.tick()
        self.assertEqual(self.schedule_calls, 1)

    async def test_stale_cached_schedule_cannot_trigger_reminder(self):
        checked = NOW - dt.timedelta(hours=1)
        self.store.set_checkpoint(SCHEDULE_KEY, {'games': parse_schedule(self.document, NOW.date(), URL),
            'checked_at': checked.isoformat(), 'url': URL})
        self.fail_schedule = True
        self.assertEqual((await self.digest.tick())['state'], 'schedule_unavailable')
        self.assertEqual(self.rows(), [])

    async def test_postponed_canceled_tbd_and_started_games_do_not_send(self):
        for state in ['postponed', 'canceled', 'time_tbd', 'final', 'live']:
            games = parse_schedule(self.document, NOW.date(), URL)
            games[0]['status'] = state
            self.store.set_checkpoint(SCHEDULE_KEY, {'games': games, 'checked_at': NOW.isoformat(), 'url': URL})
            await self.digest.tick()
            self.assertEqual(self.rows(), [], state)
        self.store.set_checkpoint(SCHEDULE_KEY, {'games': parse_schedule(self.document, NOW.date(), URL),
            'checked_at': (NOW + dt.timedelta(hours=2)).isoformat(), 'url': URL})
        self.current = NOW + dt.timedelta(hours=2)
        self.assertEqual((await self.digest.tick())['state'], 'game_started')
        self.assertEqual(self.rows(), [])

    async def test_news_scope_promo_age_and_source_times(self):
        items = [
            ('ny', 'Knicks injury: OG Anunoby questionable', 'NYK', '2026-10-30T16:00:00-04:00'),
            ('cha', 'Hornets injury: Player out for Friday', 'CHA', '2026-10-30T17:00:00'),
            ('other', 'Lakers injury: Player out for Friday', 'LAL', NOW.isoformat()),
            ('old', 'Knicks injury: Old player out', 'NYK', '2026-10-20T17:00:00Z'),
            ('promo', 'Knicks Promo Code: Get $50 injury trade offer', 'NYK', NOW.isoformat())]
        for key, title, team, published in items:
            self.store.record_news({'id': key, 'title': title, 'team': team, 'published_at': published,
                'source': 'fixture', 'url': 'https://publisher.example/' + key})
        game = parse_schedule(self.document, NOW.date(), URL)[0]
        message = render_digest(self.store, game, NOW, NOW.isoformat(), True)
        self.assertIn('OG Anunoby', message)
        self.assertIn('Hornets injury', message)
        self.assertIn('timezone unconfirmed', message)
        for forbidden in ['Lakers injury', 'Old player', 'Promo Code']:
            self.assertNotIn(forbidden, message)
        self.assertEqual(len(list(chunks(message))), 1)
        self.assertIn('Fresh news check incomplete', message)

    async def test_expired_digest_does_not_deliver_after_telegram_downtime(self):
        await self.digest.tick()
        self.bot._api = AsyncMock(return_value={})
        with patch('telegram_bot.time.time', return_value=(NOW + dt.timedelta(hours=2)).timestamp()):
            await self.bot.flush_outbox()
        self.bot._api.assert_not_awaited()
        self.assertEqual(self.rows()[0]['status'], 'expired')

    async def test_roster_names_without_team_tags_and_long_digest_stay_one_message(self):
        self.store.upsert_player_game({'game_id': 'historical', 'player': 'Josh Hart',
            'team': 'NYK', 'date': '2026-10-27'})
        self.store.record_news({'id': 'name-only', 'title': 'Josh Hart: Questionable with injury',
            'source': 'fixture', 'published_at': NOW.isoformat(), 'url': 'https://publisher.example/hart'})
        for number in range(12):
            self.store.record_news({'id': 'long' + str(number),
                'title': 'Hornets injury: ' + ('player update ' * 40), 'team': 'CHA',
                'source': 'fixture', 'published_at': (NOW - dt.timedelta(minutes=number + 1)).isoformat(),
                'url': 'https://publisher.example/' + str(number) + '/' + ('x' * 700)})
        game = parse_schedule(self.document, NOW.date(), URL)[0]
        message = render_digest(self.store, game, NOW, NOW.isoformat(), True)
        self.assertIn('Josh Hart', message)
        self.assertIn('not independently verified', message)
        self.assertIn('Schedule checked:', message)
        self.assertEqual(len(list(chunks(message))), 1)

    async def test_upgrade_cancels_pending_headlines_but_keeps_entry_replies(self):
        self.bot._queue(123, 'Old injury alert', 'news:old')
        self.bot._queue(123, 'Old health warning', 'feed-health:rss:bad')
        self.bot._queue(123, 'Your entry result', 'grade:one')
        TelegramBot(self.store, AsyncMock(), token='fake-test-token', allowed_user_ids=[123])
        rows = self.rows()
        self.assertEqual([row['status'] for row in rows], ['cancelled', 'cancelled', 'pending'])

    async def test_eastern_dst_changes_reminder_hour_correctly(self):
        self.current = dt.datetime(2026, 11, 2, 23, tzinfo=UTC)  # 6pm EST.
        self.document = calendar('2026-11-03T00:00:00Z')
        self.assertEqual((await self.digest.tick())['state'], 'queued')
        self.assertIn('7:00 PM Eastern', self.rows()[0]['text'])

    async def test_daily_health_alerts_stay_in_status_in_scheduled_mode(self):
        import app
        notify = AsyncMock()
        self.store.feed_failure('rss_1', 'HTTP 500')
        await app.notify_feed_health(self.store, notify, current=NOW)
        notify.assert_not_awaited()
        self.assertTrue(self.store.status()['feeds'])


if __name__ == '__main__':
    unittest.main()
