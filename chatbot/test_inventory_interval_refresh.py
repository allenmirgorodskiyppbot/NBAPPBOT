"""Refresh queues satisfy the same bounded interval the readiness check needs."""
import asyncio
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dataset_refresh import initialize_scope
from feeds import canonical_game
from master import MasterStore
from nightly_data import NightlyNBARefresh, _pregame_recheck_days
from pregame_freshness import assess_pregame_freshness

UTC=dt.timezone.utc
NOW=dt.datetime(2026,11,10,17,tzinfo=UTC)
TARGET='2026-11-09'


class InventoryIntervalRefreshTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store=MasterStore(Path(directory.name)/'interval.sqlite')
        self.now=[NOW]
        self.final_games={}
        self.scope=initialize_scope(self.store)
        self.calls=[]
        self.fail_dates=set()
        self.game={'id':canonical_game('2026-11-10','NYK','MIL'),'season':'2026-27',
            'date':'2026-11-10','home_team':'NYK','away_team':'MIL',
            'start_time':'2026-11-11T00:30:00+00:00','status':'scheduled',
            'source':'https://example.invalid/synthetic-calendar'}
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.game['id'],self.game['season'],self.game['date'],'NYK','MIL',
                 self.game['start_time'],'scheduled',self.game['source'],'2026-11-10',
                 'saved_publication',NOW.isoformat(),'{}'))
        self.store.set_checkpoint('nba_schedule_today',{'date':'2026-11-10','status':'success',
            'source':'https://example.invalid/synthetic-current-schedule','checked_at':NOW.isoformat(),
            'games':[self.game]})

    def final(self,day,team,opponent):
        game={'id':canonical_game(day,team,opponent),'date':day,'home_team':team,
            'away_team':opponent,'status':'final','source':'https://example.invalid/synthetic-final',
            'home_score':100,'away_score':90}
        self.store.upsert_game(game)
        self.final_games.setdefault(day,[]).append(game)
        return game['id']

    def records(self):
        result=[]
        for day,games in sorted(self.final_games.items()):
            result.append({'date':day,'complete':True,'games':len(games),
                'final_game_ids':[g['id'] for g in games],
                'team_final_game_ids':{team:[g['id'] for g in games
                    if team in (g['home_team'],g['away_team'])]
                    for team in {t for g in games for t in (g['home_team'],g['away_team'])}}})
        return result

    def rechecks(self,target=TARGET):
        return _pregame_recheck_days(self.store,self.records(),target,self.now[0])

    def calendar(self,teams=('NYK',),**changes):
        self.store.set_checkpoint('season_calendar:2026-27',{
            'covered_teams':list(teams),'complete_for_published_games':True,
            'unassigned_games':0,'unassigned_windows':[],
            'unassigned_coverage_unknown':False,**changes})

    def test_uncovered_team_queues_every_intervening_date_not_only_last_final(self):
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-07','MIL','CHI')
        self.assertEqual(self.rechecks(),['2026-11-07','2026-11-08'])

    def test_master_finals_without_any_daily_checkpoint_still_request_publication_checks(self):
        self.final('2026-11-07','MIL','CHI')
        self.assertEqual(_pregame_recheck_days(self.store,[],TARGET,NOW),
                         ['2026-11-07','2026-11-08'])

    def test_complete_team_calendars_only_need_last_final_rechecks(self):
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-07','MIL','CHI')
        self.calendar(('NYK','MIL'))
        self.assertEqual(self.rechecks(),['2026-11-07'])

    def test_unrelated_latest_league_game_does_not_fill_the_matchup_batch(self):
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-06','MIL','CHI')
        self.final('2026-11-08','PHI','DET')
        self.calendar(('NYK','MIL'))
        self.assertEqual(self.rechecks(),['2026-11-06','2026-11-07'])

    def test_knicks_calendar_does_not_supply_missing_opponent_coverage(self):
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-07','MIL','CHI')
        self.calendar()
        self.assertEqual(self.rechecks(),['2026-11-07','2026-11-08'])

    def test_unknown_or_overlapping_cup_windows_require_interval_checks(self):
        self.final('2026-11-07','NYK','BOS')
        self.final(TARGET,'MIL','CHI')
        for changes in ({'unassigned_coverage_unknown':True},
                        {'unassigned_games':2,'unassigned_windows':[]},
                        {'unassigned_games':2,'unassigned_windows':[
                            {'start':'2026-11-08','end':'2026-11-08'}]}):
            with self.subTest(changes=changes):
                self.calendar(**changes)
                self.assertIn('2026-11-08',self.rechecks())
        self.calendar(unassigned_games=2,unassigned_windows=[
            {'start':'2026-12-03','end':'2026-12-11'}])
        self.assertEqual(self.rechecks(),['2026-11-07'])

    def test_range_is_bounded_to_sixty_days_and_never_after_the_target(self):
        target=dt.date.fromisoformat(TARGET)
        old=(target-dt.timedelta(days=60)).isoformat()
        self.final(old,'NYK','BOS')
        self.assertEqual(self.rechecks(),[old])
        boundary=(target-dt.timedelta(days=59)).isoformat()
        self.final(boundary,'NYK','CHI')
        dates=self.rechecks()
        self.assertEqual(len(dates),59)
        self.assertEqual(dates[0],boundary)
        self.assertTrue(all(day<TARGET for day in dates))
        self.final('2026-11-11','NYK','BOS')
        self.assertEqual(self.rechecks(),dates)

    def test_helper_is_read_only_and_pre_deadline_target_does_not_fetch_later_dates(self):
        self.final('2026-11-07','NYK','BOS')
        with self.store.connect() as db:
            before='\n'.join(db.iterdump())
        self.assertEqual(self.rechecks(target='2026-11-07'),[])
        with self.store.connect() as db:
            self.assertEqual('\n'.join(db.iterdump()),before)

    def test_current_team_certificates_avoid_requeueing_completed_gaps_without_borrowing(self):
        from published_game_inventory import ROOT
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-07','MIL','CHI')
        records=[]
        for day in ['2026-11-07','2026-11-08',TARGET]:
            games=self.final_games.get(day,[])
            teams={team:[g['id'] for g in games if team in (g['home_team'],g['away_team'])]
                   for team in ('NYK','MIL')}
            sources=[{'source':ROOT+tag+'/'+filename,'source_published_at':NOW.isoformat(),
                      'source_content_sha256':'a'*64}
                     for tag,filename in [('nba_stats_schedules','nba_schedule_2027.parquet'),
                                          ('nba_stats_player_game_logs','player_game_logs_2027.parquet'),
                                          ('espn_nba_player_boxscores','player_box_2027.parquet')]]
            certificates={team:{'team':team,'date':day,'inventory_verified_at':NOW.isoformat(),
                'inventory_game_ids':ids,'inventory_off_day_verified':not ids,
                'inventory_verification_source':sources[1]['source'],'inventory_sources':sources}
                for team,ids in teams.items()}
            records.append({'date':day,'complete':True,'source':'espn',
                'publisher_retrieved_at':NOW.isoformat(),'last_checked_at':NOW.isoformat(),
                'last_refresh_status':'success','final_game_ids':[g['id'] for g in games],
                'team_final_game_ids':teams,'inventory_verified':False,
                'team_inventory_verified':certificates})
        self.assertEqual(_pregame_recheck_days(self.store,records,TARGET,NOW),['2026-11-07'])
        middle=next(record for record in records if record['date']=='2026-11-08')
        del middle['team_inventory_verified']['MIL']
        self.assertEqual(_pregame_recheck_days(self.store,records,TARGET,NOW),['2026-11-08'])
        self.assertTrue(all(record['inventory_verified'] is False for record in records))

    def document(self,day):
        games=self.final_games.get(day,[])
        return {'schema_version':1,'date':day,'source':'espn',
            'published_at':self.now[0].isoformat(),'expected_games':len(games),
            'games':games,'player_games':[],'complete':True,'pending_game_ids':[],'errors':[],
            'scope_revision':self.scope['revision'],'upstream_full_game_validated':True,
            'inventory_verified':True,'inventory_verified_at':self.now[0].isoformat(),
            'inventory_verification_source':'https://example.invalid/synthetic-independent-inventory',
            'inventory_game_ids':[game['id'] for game in games],
            'inventory_off_day_verified':not games}

    def worker_fixture(self):
        self.final('2026-11-07','NYK','BOS')
        self.final('2026-11-06','MIL','CHI')
        old=NOW-dt.timedelta(days=1)
        for day in ['2026-11-06','2026-11-07','2026-11-08',TARGET]:
            doc=self.document(day)
            self.store.set_checkpoint('daily_nba:'+day,{
                'date':day,'complete':True,'games':len(doc['games']),
                'source':'espn','publisher_retrieved_at':old.isoformat(),
                'last_checked_at':old.isoformat(),'last_refresh_status':'success',
                'team_inventory_checked_at':old.isoformat(),
                'inventory_verified':True,'inventory_verified_at':old.isoformat(),
                'inventory_verification_source':doc['inventory_verification_source'],
                'final_game_ids':doc['inventory_game_ids'],
                'team_final_game_ids':{team:[g['id'] for g in doc['games']
                    if team in (g['home_team'],g['away_team'])]
                    for team in {t for g in doc['games'] for t in (g['home_team'],g['away_team'])}}})
        self.store.set_checkpoint('nightly_nba_refresh',{'initialized':True,
            'pending_dates':[],'handled_target_date':TARGET,'last_attempt':old.isoformat()})
        owner=self
        class Worker:
            async def request(self,url):
                name=url.rsplit('/',1)[-1]
                if name=='existing_tracking.json':
                    return {'schema_version':1,'scope_revision':owner.scope['revision'],
                        'season':'2026-27','last_attempt':owner.now[0].isoformat(),
                        'source_data_through_date':None,'coverage':[],'records':[]}
                day=name.removesuffix('.json')
                owner.calls.append(day)
                if day in owner.fail_dates:
                    response=httpx.Response(404,request=httpx.Request('GET',url))
                    response.raise_for_status()
                return owner.document(day)
        return Worker()

    def tick(self,worker):
        async def inline(function,*args,**kwargs):
            return function(*args,**kwargs)
        with patch.dict('os.environ',{'DAILY_NBA_REFRESH':'1',
                'NBA_DAILY_DATA_URL':'https://example.invalid/synthetic'}), \
                patch('nightly_data.asyncio.to_thread',inline), \
                patch('game_schedule_check.refresh_game_schedule',new=AsyncMock(return_value={})):
            service=NightlyNBARefresh(self.store,worker,now=lambda:self.now[0])
            asyncio.run(service.tick(export=False))

    def test_bounded_worker_batches_rotate_gap_dates_and_keep_hourly_cooldown(self):
        worker=self.worker_fixture()
        self.tick(worker)
        self.assertEqual(self.calls,[TARGET,'2026-11-06','2026-11-07'])
        state=self.store.get_checkpoint('nightly_nba_refresh')
        self.assertIn('2026-11-08',state['pending_dates'])
        self.assertFalse(assess_pregame_freshness(self.store,'2026-11-10','NYK',NOW)['statistics_ready'])
        self.now[0]+=dt.timedelta(seconds=3599)
        self.tick(worker)
        self.assertEqual(len(self.calls),3)
        self.now[0]+=dt.timedelta(seconds=1)
        before=len(self.calls)
        self.tick(worker)
        self.assertEqual(self.calls[before:before+2],[TARGET,'2026-11-08'])
        self.assertLessEqual(len(self.calls)-before,3)
        for team in ('NYK','MIL'):
            result=assess_pregame_freshness(self.store,'2026-11-10',team,self.now[0])
            self.assertTrue(result['statistics_ready'],result['statistics_issues'])

    def test_failed_gap_inventory_stays_pending_and_never_becomes_ready(self):
        worker=self.worker_fixture()
        self.tick(worker)
        self.fail_dates.add('2026-11-08')
        self.now[0]+=dt.timedelta(hours=1)
        self.tick(worker)
        state=self.store.get_checkpoint('nightly_nba_refresh')
        self.assertIn('2026-11-08',state['pending_dates'])
        failed=self.store.get_checkpoint('daily_nba:2026-11-08')
        self.assertEqual(failed['last_refresh_status'],'failed')
        self.assertLess(failed['inventory_verified_at'],NOW.isoformat())
        self.assertFalse(assess_pregame_freshness(self.store,'2026-11-10','MIL',self.now[0])['statistics_ready'])
        self.fail_dates.clear()
        self.now[0]+=dt.timedelta(hours=1)
        self.tick(worker)
        self.assertTrue(assess_pregame_freshness(self.store,'2026-11-10','MIL',self.now[0])['statistics_ready'])


if __name__=='__main__':
    unittest.main()
