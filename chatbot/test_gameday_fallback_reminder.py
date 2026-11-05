"""A refused live source must not silently erase a trusted pregame reminder."""
import asyncio
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gameday_digest import GamedayDigest, parse_schedule, reminder_status
from master import MasterStore
from season_calendar import import_calendar
from telegram_bot import TelegramBot
import test_gameday_digest as fixtures

UTC=dt.timezone.utc
NOW=dt.datetime(2026,11,4,23,30,tzinfo=UTC) # 6:30pm Eastern
TIP=dt.datetime(2026,11,5,0,30,tzinfo=UTC) # 7:30pm Eastern


class FallbackReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env=patch.dict(os.environ,{'NEWS_ALERT_MODE':'knicks_gameday','KNICKS_DIGEST_MINUTES_BEFORE':'60',
            'KNICKS_SCHEDULE_URL':fixtures.URL});env.start();self.addCleanup(env.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'master.sqlite';self.store=MasterStore(self.path)
        self.current=NOW;self.failure='403';self.calls=[]
        self.document=fixtures.calendar(TIP.isoformat())
        for day in self.document['leagueSchedule']['gameDates']:
            day['games'][0]['awayTeam']['teamTricode']='CHI'
        self.games=parse_schedule(self.document,NOW.date(),fixtures.URL)
        import_calendar(self.store,{'season':'2026-27','covered_teams':['NYK'],'games':self.games,
            'source':'https://www.nba.com/knicks/schedule','retrieved_at':'2026-10-29T12:00:00+00:00'})
        self.bot=TelegramBot(self.store,AsyncMock(),token='fake-unit-test-token',allowed_user_ids=[123])
        owner=self
        class Worker:
            def __init__(inner):
                inner._cycle_lock=asyncio.Lock();inner.collect_news_sources=AsyncMock()
            async def request(inner,url):
                owner.calls.append(url)
                if owner.failure=='timeout':raise httpx.ReadTimeout('synthetic source timeout')
                if owner.failure=='403':
                    response=httpx.Response(403,request=httpx.Request('GET',url));response.raise_for_status()
                return owner.document
        self.worker=Worker();self.digest=GamedayDigest(self.store,self.worker,self.bot.notify,now=lambda:self.current)

    def outbox(self):
        with self.store.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox')]

    async def test_403_at_saved_hour_queues_warning_without_picks_and_keeps_failed_freshness(self):
        with patch('independent_picks.build_independent_picks') as picks,patch('gameday_digest.render_digest') as research:
            result=await self.digest.tick()
        self.assertEqual(result['state'],'queued')
        self.assertEqual(result['notice_kind'],'timing_warning')
        self.assertFalse(result['timing_verified'])
        picks.assert_not_called();research.assert_not_called()
        self.worker.collect_news_sources.assert_not_awaited()
        rows=self.outbox();self.assertEqual(len(rows),1)
        self.assertIn('Timing unverified',rows[0]['text'])
        self.assertIn('HTTP 403',rows[0]['text'])
        self.assertIn('7:30 PM Eastern',rows[0]['text'])
        self.assertIn('Picks held',rows[0]['text'])
        self.assertEqual(rows[0]['expires_at'],TIP.timestamp())
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today')['status'],'failed')
        self.assertEqual(self.store.status()['counts']['games'],0)

    async def test_timeout_has_concrete_warning_and_deduplicates_after_restart(self):
        self.failure='timeout'
        await self.digest.tick()
        self.assertIn('ReadTimeout',self.outbox()[0]['text'])
        reopened=GamedayDigest(MasterStore(self.path),self.worker,self.bot.notify,now=lambda:self.current)
        await asyncio.gather(reopened.tick(),reopened.tick())
        self.assertEqual(len(self.outbox()),1)
        self.assertIn('picks were held',reminder_status(self.store))

    async def test_outbox_key_survives_crash_between_queue_and_checkpoint(self):
        await self.digest.tick()
        with self.store.connect() as db:db.execute("DELETE FROM checkpoints WHERE name='knicks-gameday:2026-11-04'")
        reopened=GamedayDigest(self.store,self.worker,self.bot.notify,now=lambda:self.current)
        await reopened.tick()
        self.assertEqual(len(self.outbox()),1)

    async def test_before_due_waits_then_short_restart_catches_up_once(self):
        self.current=NOW-dt.timedelta(minutes=1)
        self.assertEqual((await self.digest.tick())['state'],'waiting')
        self.assertEqual(self.outbox(),[])
        self.current=NOW+dt.timedelta(minutes=8)
        self.assertEqual((await self.digest.tick())['state'],'queued')
        self.assertIn('Catch-up notice',self.outbox()[0]['text'])

    async def test_late_verified_restart_sends_notice_without_old_research(self):
        self.failure=None;self.current=NOW+dt.timedelta(minutes=40)
        with patch('gameday_digest.render_digest') as render:
            result=await self.digest.tick()
        render.assert_not_called()
        self.assertEqual(result['notice_kind'],'late_catchup')
        self.assertIn('Picks held',self.outbox()[0]['text'])

    async def test_at_tipoff_and_after_tipoff_never_queue(self):
        self.current=TIP
        self.assertEqual((await self.digest.tick())['state'],'game_started')
        self.current=TIP+dt.timedelta(minutes=1)
        await self.digest.tick();self.assertEqual(self.outbox(),[])

    async def test_saved_off_day_remains_quiet(self):
        self.current=NOW+dt.timedelta(days=1)
        self.assertEqual((await self.digest.tick())['state'],'off_day')
        self.assertEqual(self.outbox(),[])

    async def test_saved_calendar_cannot_undo_known_cancellation(self):
        game={**self.games[0],'status':'canceled'};self.store.upsert_game(game)
        self.assertEqual((await self.digest.tick())['state'],'no_upcoming_tipoff_today')
        self.assertEqual(self.outbox(),[])

    async def test_live_verified_route_retains_research_digest(self):
        self.failure=None
        with patch('gameday_digest.render_digest',return_value='Verified current research fixture') as render:
            result=await self.digest.tick()
        self.assertEqual(result['state'],'queued');render.assert_called_once()
        self.worker.collect_news_sources.assert_awaited_once()
        self.assertEqual(self.outbox()[0]['text'],'Verified current research fixture')
        self.assertEqual(result['schedule_mode'],'live_checked')

    async def test_daylight_saving_saved_reminder_keeps_six_thirty_local_hour(self):
        self.assertEqual(NOW.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).hour,18)
        await self.digest.tick()
        self.assertIn('7:30 PM Eastern',self.outbox()[0]['text'])


if __name__=='__main__':unittest.main()
