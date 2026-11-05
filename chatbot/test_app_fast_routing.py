"""Telegram routine answers precede model guards; deep requests keep them."""
import asyncio
import datetime as dt
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app
from feeds import canonical_game, canonical_player
from master import MasterStore
from season_calendar import import_calendar

NOW=dt.datetime(2026,11,5,17,tzinfo=dt.timezone.utc)


class AppFastRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.master=MasterStore(Path(self.temp.name)/'master.sqlite')
        game={'id':canonical_game('2026-11-06','NYK','BOS'),'date':'2026-11-06',
              'home_team':'NYK','away_team':'BOS','status':'scheduled',
              'start_time':'2026-11-07T00:30:00+00:00','source':'https://example.test/saved-calendar'}
        import_calendar(self.master,{'season':'2026-27','covered_teams':['NYK'],'games':[game],
            'complete_for_published_games':False,'source':game['source'],'retrieved_at':'2026-11-04T12:00:00+00:00'})
        prior=canonical_game('2026-11-04','NYK','BOS')
        self.master.upsert_game({'id':prior,'date':'2026-11-04','home_team':'NYK','away_team':'BOS',
            'status':'final','source':'synthetic prior game fixture'})
        self.master.upsert_player_game({'game_id':prior,'date':'2026-11-04','player':'Jalen Brunson',
            'player_id':canonical_player('Jalen Brunson'),'team':'NYK','opponent':'BOS',
            'season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
            'minutes':33,'pts':30,'reb':4,'ast':7,'fga':19,'source':'synthetic prior observation fixture'})
        self.state=SimpleNamespace(store=None,master=self.master,phase='workbook',detail='Indexing the saved workbook',
            chat_lock=asyncio.Lock(),ai_started_at=None)
        state=patch.object(app.app,'state',self.state);state.start();self.addCleanup(state.stop)
        clock=patch('pick_discussion._now',return_value=NOW);clock.start();self.addCleanup(clock.stop)
        self.generate=AsyncMock(side_effect=AssertionError('Fast answers must not invoke inference'))
        generator=patch('app.answer_question',self.generate);generator.start();self.addCleanup(generator.stop)

    async def test_next_game_uses_saved_calendar_while_workbook_and_model_are_not_ready(self):
        before=self.master.get_checkpoint('nba_schedule_today')
        with patch('basketball_research.resolve_question',side_effect=AssertionError('Old prop routing must not run')):
            text=await app.telegram_answer('Whats up next for the Knicks?')
        self.assertIn('Next saved matchup for NYK',text)
        self.assertIn('BOS at NYK',text)
        self.assertIn('current timing is unverified',text)
        self.assertIn('this may not be their next NBA game',text)
        self.assertEqual(self.master.get_checkpoint('nba_schedule_today'),before)
        self.generate.assert_not_awaited()

    async def test_next_game_overrides_old_prop_focus_even_while_model_busy(self):
        self.state.phase='ready';await self.state.chat_lock.acquire()
        self.addCleanup(self.state.chat_lock.release)
        history=[{'role':'user','content':'Jalen Brunson more 25.5 points tonight?'},
                 {'role':'assistant','content':'Jalen Brunson — More 25.5 points. This is the old pick.'}]
        text=await app.telegram_answer('What is the next Knicks game?',history)
        self.assertIn('Next saved matchup for NYK',text)
        self.assertNotIn('25.5',text)
        self.assertNotIn('answering another question',text)
        self.generate.assert_not_awaited()

    async def test_routine_acknowledgment_does_not_wait_for_busy_or_warming_model(self):
        for phase in ('model','ready'):
            self.state.phase=phase
            await self.state.chat_lock.acquire()
            try:
                text=await app.telegram_answer('Thanks!')
            finally:self.state.chat_lock.release()
            self.assertEqual(text,'You’re welcome.')
        self.generate.assert_not_awaited()

    async def test_complete_prop_returns_computed_pass_without_bypassing_freshness(self):
        for number in range(1,21):
            day=(NOW.date()-dt.timedelta(days=number)).isoformat()
            identity=canonical_game(day,'NYK','BOS')
            self.master.upsert_game({'id':identity,'date':day,'home_team':'NYK','away_team':'BOS',
                'status':'final','source':'synthetic prior game fixture'})
            self.master.upsert_player_game({'game_id':identity,'date':day,'player':'Jalen Brunson',
                'player_id':canonical_player('Jalen Brunson'),'team':'NYK','opponent':'BOS',
                'season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
                'minutes':33,'pts':30,'reb':4,'ast':7,'fga':19,'source':'synthetic prior observation fixture'})
        text=await app.telegram_answer('Jalen Brunson more than 25.5 points?')
        self.assertIn('pass',text.casefold())
        self.assertNotIn('model is not ready',text.casefold())
        self.assertFalse(self.master.get_checkpoint('nba_schedule_today'))
        self.generate.assert_not_awaited()

    async def test_explicit_deep_request_stays_behind_readiness_guard(self):
        text=await app.telegram_answer('Explain in detail whether Jalen Brunson more than 25.5 points is a good pick.')
        self.assertIn('AI model is not ready',text)
        self.generate.assert_not_awaited()

    async def test_explicit_deep_request_stays_behind_busy_guard(self):
        self.state.phase='ready';await self.state.chat_lock.acquire()
        try:
            text=await app.telegram_answer('Give me a detailed comparison of Jalen Brunson more than 25.5 points.')
        finally:self.state.chat_lock.release()
        self.assertIn('answering another question',text)
        self.generate.assert_not_awaited()


class ApplicationShutdownOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_shutdown_explicitly_stops_the_independent_refresh_job(self):
        entered=asyncio.Event();cancelled=asyncio.Event();created=[]
        async def manual_job():
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        class Bot:
            def __init__(self,*args,**kwargs):
                self.job=None;self.shutdown_calls=0;created.append(self)
            async def shutdown(self):
                self.shutdown_calls+=1
                if self.job:
                    self.job.cancel();await asyncio.gather(self.job,return_exceptions=True)
        async def startup(application):
            application.state.telegram.job=asyncio.create_task(manual_job())
            await asyncio.Event().wait()
        application=SimpleNamespace(state=SimpleNamespace())
        with tempfile.TemporaryDirectory() as folder,patch.object(app,'DATA_DIR',Path(folder)), \
                patch.object(app,'TelegramBot',Bot),patch.object(app,'initialize_with_retry',startup):
            async with app.lifespan(application):
                await asyncio.wait_for(entered.wait(),1)
                self.assertFalse(cancelled.is_set())
            self.assertTrue(cancelled.is_set())
            self.assertEqual(created[0].shutdown_calls,1)


if __name__=='__main__':unittest.main()
