"""Calendar replacement order and incomplete NBA Cup coverage regressions."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from gameday_digest import GamedayDigest, SCHEDULE_KEY, reminder_status
from master import MasterStore
from season_calendar import SEED_PATH, calendar_context, import_calendar, seed_calendar

UTC=dt.timezone.utc


def publication():
    return copy.deepcopy(json.loads(SEED_PATH.read_text()))


def nba_document(document):
    dates=[]
    for number, game in enumerate(document['games']):
        dates.append({'gameDate':game['date'],'games':[{
            'gameId':f'00226{number:05d}','gameTimeUTC':game.get('start_time'),
            'gameStatus':1,'gameStatusText':'TBD' if not game.get('start_time') else '7:30 pm ET',
            'homeTeam':{'teamTricode':game['home_team']},
            'awayTeam':{'teamTricode':game['away_team']}}]})
    return {'leagueSchedule':{'seasonYear':'2026-27','gameDates':dates}}


class CalendarRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'master.sqlite')
        settings=patch.dict('os.environ',{'NEWS_ALERT_MODE':'knicks_gameday'})
        settings.start();self.addCleanup(settings.stop)

    def tipoff(self):
        with self.store.connect() as db:
            return db.execute("SELECT start_time FROM season_schedule WHERE date='2026-10-30'").fetchone()[0]

    async def test_seed_does_not_overwrite_a_newer_live_instant_in_another_timezone(self):
        live=publication();live['retrieved_at']='2026-10-29T23:59:00-04:00'
        next(game for game in live['games'] if game['date']=='2026-10-30')['start_time']='2026-10-30T23:45:00+00:00'
        existing=import_calendar(self.store,live,source_kind='live_schedule')
        old_seed=publication();old_seed['retrieved_at']='2026-10-30T02:50:00+00:00'
        path=Path(self.directory.name)/'older-seed.json';path.write_text(json.dumps(old_seed))
        result=seed_calendar(self.store,path)
        self.assertEqual(result['revision'],existing['revision'])
        self.assertEqual(self.tipoff(),'2026-10-30T23:45:00+00:00')

    async def test_newer_seed_in_offset_timezone_can_replace_older_utc_publication(self):
        old=publication();old['retrieved_at']='2026-10-30T02:00:00+00:00'
        import_calendar(self.store,old)
        new=publication();new['retrieved_at']='2026-10-29T23:59:00-04:00'
        next(game for game in new['games'] if game['date']=='2026-10-30')['start_time']='2026-10-30T23:45:00+00:00'
        path=Path(self.directory.name)/'newer-seed.json';path.write_text(json.dumps(new))
        result=seed_calendar(self.store,path)
        self.assertEqual(result['source_checked_at'],new['retrieved_at'])
        self.assertEqual(self.tipoff(),'2026-10-30T23:45:00+00:00')

    async def test_direct_import_is_monotonic_and_invalid_windows_do_not_partially_replace(self):
        original=publication();original['retrieved_at']='2026-10-30T03:59:00+00:00'
        meta=import_calendar(self.store,original)
        older=publication();older['retrieved_at']='2026-10-29T22:00:00-04:00'
        next(game for game in older['games'] if game['date']=='2026-10-30')['start_time']='2026-10-31T00:00:00+00:00'
        self.assertEqual(import_calendar(self.store,older)['revision'],meta['revision'])
        self.assertEqual(self.tipoff(),'2026-10-30T23:30:00+00:00')
        bad=publication();bad['retrieved_at']='2026-10-30T04:00:00+00:00'
        bad['unassigned_windows']=[{'start':'2026-12-11','end':'2026-12-03'}]
        with self.assertRaises(ValueError):import_calendar(self.store,bad)
        self.assertEqual(self.store.get_checkpoint('season_calendar:2026-27')['revision'],meta['revision'])
        self.assertEqual(self.store.status()['counts']['games'],0)

    async def test_live_refresh_and_offline_fallback_preserve_cup_uncertainty(self):
        document=publication();import_calendar(self.store,document)
        raw=nba_document(document)
        class Worker:
            fail=False
            async def request(inner,url):
                if inner.fail:raise RuntimeError('Fixture source unavailable')
                return raw
        worker=Worker();now=[dt.datetime(2026,12,5,17,tzinfo=UTC)]
        notify=AsyncMock(return_value=True)
        digest=GamedayDigest(self.store,worker,notify,now=lambda:now[0])
        result=await digest.tick()
        self.assertEqual(result['state'],'cup_schedule_unassigned')
        meta=self.store.get_checkpoint('season_calendar:2026-27')
        self.assertEqual(meta['unassigned_windows'],document['unassigned_windows'])
        self.assertEqual(self.store.get_checkpoint(SCHEDULE_KEY)['metadata']['unassigned_windows'],document['unassigned_windows'])
        self.assertIsNone(calendar_context(self.store,'2026-12-12','NYK')['scheduled_rest_days'])
        worker.fail=True;now[0]+=dt.timedelta(hours=7)
        result=await digest.tick()
        self.assertEqual(result['state'],'cup_schedule_unassigned')
        self.assertEqual(result['schedule_mode'],'saved_calendar')
        notify.assert_not_awaited()

    async def test_unknown_missing_game_dates_are_neither_off_days_nor_known_rest(self):
        document=publication();document.pop('unassigned_windows')
        import_calendar(self.store,document)
        self.assertTrue(self.store.get_checkpoint('season_calendar:2026-27')['unassigned_coverage_unknown'])
        self.assertIsNone(calendar_context(self.store,'2026-12-12','NYK')['scheduled_rest_days'])
        class Worker:
            async def request(inner,url):raise RuntimeError('Fixture source unavailable')
        digest=GamedayDigest(self.store,Worker(),AsyncMock(return_value=True),
                             now=lambda:dt.datetime(2026,12,5,17,tzinfo=UTC))
        self.assertEqual((await digest.tick())['state'],'schedule_coverage_unknown')
        self.assertIn('not confirmed as an off day',reminder_status(self.store))

    async def test_partial_publication_does_not_establish_rest_even_when_team_is_covered(self):
        document=publication();document['games']=[game for game in document['games'] if game['date'] in {'2026-12-02','2026-12-12'}]
        document.update(complete_for_published_games=False,unassigned_games=0,unassigned_windows=[])
        import_calendar(self.store,document)
        self.assertIsNone(calendar_context(self.store,'2026-12-12','NYK')['scheduled_rest_days'])

    async def test_resolved_cup_slots_clear_old_windows_and_allow_known_rest(self):
        document=publication();import_calendar(self.store,document)
        completed=publication();completed['retrieved_at']='2026-12-01T15:00:00+00:00'
        for day in ('2026-12-06','2026-12-09'):
            completed['games'].append({'date':day,'home_team':'NYK','away_team':'PHI',
                'start_time':day+'T23:30:00+00:00','status':'scheduled','source':document['source']})
        completed.update(unassigned_games=0)
        meta=import_calendar(self.store,completed,'live_schedule')
        self.assertEqual(meta['unassigned_windows'],[])
        self.assertFalse(meta['unassigned_coverage_unknown'])
        self.assertEqual(calendar_context(self.store,'2026-12-12','NYK')['scheduled_rest_days'],2)
        raw=nba_document(completed)
        class Worker:
            async def request(inner,url):return raw
        digest=GamedayDigest(self.store,Worker(),AsyncMock(return_value=True),
                             now=lambda:dt.datetime(2026,12,5,17,tzinfo=UTC))
        self.assertEqual((await digest.tick())['state'],'off_day')


if __name__=='__main__':unittest.main()
