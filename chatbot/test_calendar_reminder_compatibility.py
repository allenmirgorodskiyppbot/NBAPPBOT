"""A Knicks-only published season refresh preserves trusted reminder times and Cup gaps."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gameday_digest import GamedayDigest
from master import MasterStore
from news_digest import _aware
from season_calendar import SEED_PATH, seed_calendar, saved_schedule, calendar_context
from season_schedule_refresh import import_published_schedule, team_calendar_metadata
from telegram_bot import TelegramBot
from upcoming_game import build_preview
from test_season_schedule_refresh import fixture_document, NOW


class CalendarReminderCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MasterStore(Path(self.temp.name)/'isolated.sqlite')
        seed_calendar(self.store)
        self.seed = json.loads(SEED_PATH.read_text())
        self.timed = [row for row in self.seed['games'] if _aware(row.get('start_time'))]
        future = [row for row in self.timed if _aware(row['start_time']) > NOW]
        self.game = min(future,key=lambda row:row['start_time'])
        self.tipoff = _aware(self.game['start_time']); self.current = self.tipoff-dt.timedelta(hours=1)
        self.report = import_published_schedule(self.store,fixture_document(now=NOW),now=NOW)
        env = patch.dict(os.environ,{'NEWS_ALERT_MODE':'knicks_gameday','KNICKS_DIGEST_MINUTES_BEFORE':'60'})
        env.start(); self.addCleanup(env.stop)
        self.bot = TelegramBot(self.store,AsyncMock(),token='fixture-token',allowed_user_ids=[123])
        class Worker:
            def __init__(inner):
                inner._cycle_lock = asyncio.Lock(); inner.collect_news_sources = AsyncMock()
            async def request(inner,url):
                response=httpx.Response(403,request=httpx.Request('GET',url));response.raise_for_status()
        self.worker = Worker()
        self.digest = GamedayDigest(self.store,self.worker,self.bot.notify,now=lambda:self.current)

    def outbox(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox')]

    async def test_seed_times_survive_refresh_and_hour_before_reminder_is_warning_only_once(self):
        self.assertEqual(self.report['preserved_tipoffs'],len(self.timed))
        with self.store.connect() as db:
            saved = {row['id']:dict(row) for row in db.execute('SELECT * FROM season_schedule')}
        self.assertEqual(len(saved),80)
        self.assertEqual({row['id']:row['start_time'] for row in self.timed},
                         {key:row['start_time'] for key,row in saved.items() if row['start_time']})
        with patch('independent_picks.build_independent_picks') as picks,patch('gameday_digest.render_digest') as research:
            first = await self.digest.tick(); second = await self.digest.tick()
        self.assertEqual(first['state'],'queued'); self.assertEqual(first['notice_kind'],'timing_warning')
        self.assertFalse(first['timing_verified'])
        self.assertEqual(len(self.outbox()),1)
        self.assertIn('Timing unverified',self.outbox()[0]['text'])
        self.assertIn('Picks held',self.outbox()[0]['text'])
        self.assertEqual(self.outbox()[0]['expires_at'],self.tipoff.timestamp())
        picks.assert_not_called(); research.assert_not_called()
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today')['status'],'failed')
        self.assertEqual(self.store.status()['counts']['games'],0)

    async def test_knicks_gap_window_survives_repeated_refresh_and_does_not_cover_other_teams(self):
        import_published_schedule(self.store,fixture_document(now=NOW),now=NOW)
        meta = team_calendar_metadata(self.store,'NYK',season='2026-27')
        self.assertEqual(meta['regular_season_total'],82)
        self.assertEqual(meta['unassigned_games'],2)
        self.assertEqual(meta['unassigned_windows'],self.seed['unassigned_windows'])
        self.assertFalse(meta['unassigned_coverage_unknown'])
        saved = saved_schedule(self.store,self.current)
        self.assertEqual(saved['metadata']['covered_teams'],['NYK'])
        preview = build_preview(self.store,'what is next for the Knicks?',now=NOW)
        self.assertTrue(preview['schedule_coverage']['selected_team_coverage_established'])
        self.assertIn('NYK',preview['answer'])
        chicago = build_preview(self.store,'what is next for Chicago?',now=NOW)
        self.assertFalse(chicago['schedule_coverage']['selected_team_coverage_established'])
        rest = calendar_context(self.store,self.game['date'],'NYK')
        self.assertIsNotNone(rest['scheduled_rest_days'])
        self.current = dt.datetime(2026,12,5,18,tzinfo=dt.timezone.utc)
        result = await self.digest.tick()
        # A date in the known Cup gap cannot become a confirmed off day.
        self.assertNotEqual(result['state'],'off_day')

    async def test_new_source_kind_requires_validated_marker_fixed_source_and_checksum(self):
        key='season_calendar:2026-27'; original=self.store.get_checkpoint(key)
        self.assertTrue(self.digest._saved_publication(self.current))
        for change in ({'upstream_published_schedule_validated':False},
                       {'source':'https://example.com/untrusted-calendar'},
                       {'source_content_sha256':'not-a-checksum'}):
            self.store.set_checkpoint(key,{**original,**change})
            self.assertFalse(self.digest._saved_publication(self.current))
        self.store.set_checkpoint(key,original)
        self.assertTrue(self.digest._saved_publication(self.current))

    async def test_saved_calendar_refresh_cannot_undo_observed_cancellation(self):
        self.store.upsert_game({**self.game,'status':'canceled'})
        result = await self.digest.tick()
        self.assertEqual(result['state'],'no_upcoming_tipoff_today')
        self.assertEqual(self.outbox(),[])


if __name__ == '__main__':
    unittest.main()
