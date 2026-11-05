"""Schedule-only checks block stale event timing without importing final statistics."""
import asyncio
import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dataset_refresh import initialize_scope
from feeds import canonical_game
from gameday_digest import GamedayDigest
from game_schedule_check import CHECKPOINT, refresh_game_schedule
from master import MasterStore
from nightly_data import NightlyNBARefresh

UTC=dt.timezone.utc
NOW=dt.datetime(2026,11,4,22,55,tzinfo=UTC)
DAY='2026-11-04'
TIP='2026-11-05T00:30:00+00:00'
ID=canonical_game(DAY,'NYK','CHI')


def event(home='NYK',away='CHI',status='STATUS_SCHEDULED',tip=TIP):
    state='in' if status in {'STATUS_IN_PROGRESS','STATUS_HALFTIME'} else 'post' if status=='STATUS_FINAL' else 'pre'
    return {'id':'fixture-'+home, 'date':tip,
        'status':{'type':{'name':status,'state':state,'completed':status=='STATUS_FINAL'}},
        'competitions':[{'competitors':[{'homeAway':'home','team':{'abbreviation':home},'score':'101'},
                                      {'homeAway':'away','team':{'abbreviation':away},'score':'99'}]}]}


def scoreboard(*events):
    return {'leagues':[{'abbreviation':'NBA'}],'events':list(events or [event()])}


async def inline(function,*args,**kwargs):
    return function(*args,**kwargs)


class PregameScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MasterStore(Path(self.temp.name)/'master.sqlite')
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule (id,season,date,home_team,away_team,start_time,status,source) '
                       'VALUES (?,?,?,?,?,?,?,?)',(ID,'2026-27',DAY,'NYK','CHI',TIP,'scheduled','https://example.com/saved'))
        self.document=scoreboard();self.now=NOW;self.calls=[];owner=self
        class Worker:
            async def request(inner,url):
                owner.calls.append(url)
                await asyncio.sleep(0)
                if isinstance(owner.document,BaseException):raise owner.document
                return copy.deepcopy(owner.document)
            async def collect_news_sources(inner):pass
            def now(inner):return owner.now
        self.worker=Worker();self.worker._cycle_lock=asyncio.Lock()

    def check(self):
        return asyncio.run(refresh_game_schedule(self.store,self.worker,now=self.now))

    def game(self):
        return next((g for g in self.store.games(DAY) if g['id']==ID),{})

    def test_only_known_events_update_timing_without_changing_saved_calendar_or_player_data(self):
        later='2026-11-05T01:00:00+00:00'
        self.document=scoreboard(event(tip=later),event('LAL','GSW'))
        report=self.check()
        self.assertEqual(report['status'],'success');self.assertEqual(len(report['games']),1)
        self.assertEqual(self.game()['start_time'],later)
        self.assertEqual(self.store.status()['counts']['games'],1)
        self.assertEqual(self.store.status()['counts']['player_games'],0)
        self.assertIsNone(self.game()['home_score'])
        with self.store.connect() as db:
            saved=dict(db.execute('SELECT * FROM season_schedule WHERE id=?',(ID,)).fetchone())
        self.assertEqual(saved['start_time'],TIP);self.assertEqual(saved['source'],'https://example.com/saved')
        self.assertTrue(all('scoreboard?' in url for url in self.calls))

    def test_reported_final_without_statistics_never_becomes_a_final_result(self):
        self.document=scoreboard(event(status='STATUS_FINAL'))
        report=self.check()
        self.assertEqual(self.game()['status'],'past_schedule_only')
        self.assertEqual(report['games'][0]['reported_status'],'final')
        self.assertIsNone(self.game()['home_score'])
        self.assertEqual(self.store.status()['counts']['player_games'],0)

    def test_existing_validated_final_and_scores_are_preserved(self):
        self.store.upsert_game({'id':ID,'date':DAY,'home_team':'NYK','away_team':'CHI',
                               'status':'final','home_score':123,'away_score':121,'source':'https://example.com/imported'})
        self.check()
        self.assertEqual(self.game()['status'],'final')
        self.assertEqual(self.game()['home_score'],123)
        self.assertEqual(self.game()['source'],'https://example.com/imported')

    def test_live_game_cannot_be_downgraded_to_a_stale_scheduled_state(self):
        self.store.upsert_game({'id':ID,'date':DAY,'home_team':'NYK','away_team':'CHI','status':'live'})
        report=self.check()
        self.assertEqual(report['status'],'partial')
        self.assertEqual(report['conflicting_game_ids'],[ID])
        self.assertEqual(self.game()['status'],'live')

    def test_missing_event_is_unknown_and_does_not_invent_a_cancellation(self):
        self.document={'leagues':[{'abbreviation':'NBA'}],'events':[]}
        report=self.check()
        self.assertEqual(report['status'],'partial');self.assertEqual(report['missing_game_ids'],[ID])
        self.assertEqual(self.store.status()['counts']['games'],0)

    def test_bad_date_timezone_league_status_and_duplicate_inventory_are_rejected(self):
        documents=[scoreboard(event(tip='2026-11-04T19:30:00')),
                   scoreboard(event(tip='2026-11-06T00:30:00Z')),
                   {'leagues':[{'abbreviation':'NFL'}],'events':[event()]},
                   scoreboard(event(status='UNKNOWN')),scoreboard(event(),event())]
        for index,document in enumerate(documents):
            with self.subTest(index=index):
                self.document=document;self.now=NOW+dt.timedelta(minutes=16*index)
                report=self.check()
                self.assertEqual(report['status'],'failed')
                self.assertEqual(self.store.status()['counts']['games'],0)

    def test_concurrent_and_cached_checks_share_one_request(self):
        async def run():
            return await asyncio.gather(*(refresh_game_schedule(self.store,self.worker,now=NOW) for _ in range(3)))
        reports=asyncio.run(run())
        self.assertTrue(all(report['status']=='success' for report in reports))
        self.assertEqual(len(self.calls),1)
        self.now+=dt.timedelta(minutes=13);self.check();self.assertEqual(len(self.calls),1)
        self.now+=dt.timedelta(minutes=1);self.check();self.assertEqual(len(self.calls),2)

    def test_failed_recheck_preserves_observations_but_invalidates_success(self):
        original=self.check();self.now+=dt.timedelta(minutes=16)
        self.document=httpx.ReadTimeout('fixture')
        failed=self.check()
        self.assertEqual(failed['status'],'failed');self.assertEqual(failed['error'],'ReadTimeout')
        self.assertEqual(failed['games'],original['games'])
        self.assertEqual(failed['last_success_at'],NOW.isoformat())
        self.assertEqual(failed['checked_at'],self.now.isoformat())

    def test_cancellation_does_not_leave_a_current_success_checkpoint(self):
        self.check();self.now+=dt.timedelta(minutes=16);self.document=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):self.check()
        self.assertEqual(self.store.get_checkpoint(CHECKPOINT)['status'],'failed')
        self.assertEqual(self.store.get_checkpoint(CHECKPOINT)['error'],'CancelledError')

    def test_daily_worker_checks_current_schedule_before_statistics_deadline(self):
        self.now=dt.datetime(2026,11,4,15,tzinfo=UTC)  # 10 AM Eastern.
        initialize_scope(self.store)
        self.store.set_checkpoint('nightly_nba_refresh',{'initialized':True,'handled_target_date':'2026-11-02',
            'pending_dates':[],'last_attempt':self.now.isoformat(),'last_tracking_attempt':self.now.isoformat()})
        asyncio.run(NightlyNBARefresh(self.store,self.worker,now=lambda:self.now).tick())
        self.assertEqual(len(self.calls),1)
        self.assertIn('scoreboard?dates=20261104',self.calls[0])
        self.assertEqual(self.store.status()['counts']['player_games'],0)

    def digest(self):
        notify=AsyncMock(return_value=True)
        digest=GamedayDigest(self.store,self.worker,notify,now=lambda:self.now)
        digest.refresh_schedule=AsyncMock(return_value={'games':[{'id':ID,'date':DAY,'home_team':'NYK',
            'away_team':'CHI','status':'scheduled','start_time':TIP,'source':'https://example.com/saved'}],
            'using_saved_calendar':True,'checked_at':(NOW-dt.timedelta(days=1)).isoformat()})
        return digest,notify

    def test_digest_uses_live_fallback_time_and_blocks_canceled_or_unknown_events(self):
        for status in ('STATUS_POSTPONED','STATUS_CANCELED','STATUS_FINAL','STATUS_IN_PROGRESS'):
            with self.subTest(status=status):
                self.document=scoreboard(event(status=status));self.now+=dt.timedelta(minutes=16)
                digest,notify=self.digest()
                with patch('gameday_digest.asyncio.to_thread',inline):
                    result=asyncio.run(digest.tick())
                self.assertEqual(result['state'],'no_upcoming_tipoff_today')
                notify.assert_not_awaited()

    def test_digest_holds_saved_calendar_when_current_check_fails(self):
        self.document=httpx.ReadTimeout('fixture');digest,notify=self.digest()
        result=asyncio.run(digest.tick())
        self.assertEqual(result['state'],'schedule_unavailable')
        notify.assert_not_awaited()

    def test_digest_uses_changed_tipoff_without_overwriting_season_calendar(self):
        self.document=scoreboard(event(tip='2026-11-05T02:00:00Z'))
        digest,notify=self.digest();result=asyncio.run(digest.tick())
        self.assertEqual(result['state'],'waiting')
        self.assertEqual(result['tipoff'],'2026-11-05T02:00:00+00:00')
        self.assertEqual(result['schedule_mode'],'live_checked')
        self.assertEqual(result['schedule_scope'],'current_day_events_only')
        notify.assert_not_awaited()


if __name__=='__main__':unittest.main()
