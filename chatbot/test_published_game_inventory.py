"""Historical independent inventory stays separate from live readiness."""
import copy
import datetime as dt
import hashlib
import json
import unittest
from unittest.mock import patch

from feeds import canonical_game
from nba_daily_export import reconcile_day_inventory
import published_game_inventory as pub

NOW=dt.datetime(2026,11,5,18,tzinfo=dt.timezone.utc)
DAY='2026-11-04'


def source(tag,filename,published='2026-11-05T15:00:00Z'):
    return {'source':pub.ROOT+tag+'/'+filename,'source_published_at':published,'source_content_sha256':'a'*64}


def fixture():
    calendar=[{'game_id':'0022600177','game_date':DAY,'season':2027,'season_type':'regular-season',
        'home_team_abbreviation':'NYK','away_team_abbreviation':'CHI','home_team_id':1610612752,'away_team_id':1610612741,
        'home_pts':None,'away_pts':None,'home_wl':None,'away_wl':None}]
    common={'game_id':'0022600177','game_date':DAY,'season':2027,'season_type':'regular-season',
            'fgm':20,'fga':40,'fg3a':10,'fta':20}
    results=[{**common,'team_id':1610612752,'team_abbreviation':'NYK','matchup':'NYK vs. CHI','fg3m':5,'ftm':15,'pts':60,'wl':'W','plus_minus':2},
             {**common,'team_id':1610612741,'team_abbreviation':'CHI','matchup':'CHI @ NYK','fg3m':4,'ftm':14,'pts':58,'wl':'L','plus_minus':-2}]
    key=canonical_game(DAY,'NYK','CHI')
    snapshot={'schema_version':1,'date':DAY,'source':'espn','source_url':pub.ROOT+'espn_nba_player_boxscores/player_box_2027.parquet',
        'published_at':'2026-11-05T12:34:18Z','source_content_sha256':'b'*64,'upstream_full_game_validated':True,
        'expected_games':1,'complete':True,'pending_game_ids':[],'errors':[],
        'games':[{'id':key,'date':DAY,'home_team':'NYK','away_team':'CHI','status':'final','home_score':60,'away_score':58,
                  'start_time':'2026-11-05T00:00:00Z','provider_game_id':'401909925'}],'player_games':[]}
    return calendar,results,snapshot


class HistoricalInventoryTests(unittest.TestCase):
    def setUp(self):
        self.calendar,self.results,self.snapshot=fixture()
        self.cs=source('nba_stats_schedules','nba_schedule_2027.parquet','2026-11-03T15:15:40Z')
        self.rs=source('nba_stats_player_game_logs','player_game_logs_2027.parquet')

    def check(self,day=DAY,**kwargs):
        return pub.historical_inventory(self.calendar,self.results,self.snapshot,day,checked_at=NOW,
            calendar_source=self.cs,results_source=self.rs,**kwargs)

    def test_three_way_final_proof_keeps_publication_and_observation_clocks_separate(self):
        result=self.check();self.assertTrue(result['inventory_verified']);self.assertFalse(result['inventory_off_day_verified'])
        self.assertEqual(result['inventory_date'],DAY);self.assertEqual(result['inventory_verified_at'],NOW.isoformat())
        self.assertEqual(result['inventory_sources'][0]['source_published_at'],'2026-11-03T15:15:40Z')
        self.assertEqual(result['games'][0]['status'],'final');self.assertIn('no live schedule',result['inventory_scope'])

    def test_success_can_reconcile_only_the_proven_historical_snapshot(self):
        result=reconcile_day_inventory(self.snapshot,self.check(),DAY,{'revision':'scope'})
        self.assertTrue(result['inventory_verified']);self.assertEqual(result['date'],DAY)
        self.assertEqual(len(result['inventory_sources']),3)

    def test_today_and_future_cannot_supply_live_readiness(self):
        for day in ['2026-11-05','2026-11-06']:
            with self.subTest(day=day),self.assertRaises(ValueError):self.check(day)

    def test_empty_calendar_never_certifies_an_off_day(self):
        self.calendar=[];self.results=[]
        with self.assertRaisesRegex(ValueError,'nonempty'):self.check()

    def test_calendar_alone_never_establishes_final_games(self):
        self.results=[]
        with self.assertRaisesRegex(ValueError,'exactly match'):self.check()

    def test_missing_or_extra_results_fail_the_whole_inventory(self):
        for results in [self.results[:1],[*self.results,{**self.results[0],'game_id':'0022600178'}]]:
            self.results=results
            with self.assertRaises(ValueError):self.check()

    def test_duplicate_team_views_and_wrong_home_headers_are_rejected(self):
        original=copy.deepcopy(self.results)
        for changed in [[*original,original[0]],[original[0],{**original[1],'matchup':'CHI vs. NYK'}]]:
            self.results=changed
            with self.assertRaises(ValueError):self.check()

    def test_wrong_calendar_or_team_ids_are_rejected(self):
        self.results[0]['team_id']=999
        with self.assertRaises(ValueError):self.check()

    def test_points_components_and_win_loss_cannot_fake_final_status(self):
        for changes in [{'pts':61},{'wl':'L'},{'pts':58,'ftm':13}]:
            self.results=fixture()[1];self.results[0].update(changes)
            with self.assertRaises(ValueError):self.check()

    def test_cup_tbd_and_nonregular_populations_are_held(self):
        for changes in [{'season_type':'nba-cup'},{'game_id':'0062600001'},{'home_team_abbreviation':'TBD'},
                        {'season_type':'playoffs','game_id':'0042600001'}]:
            self.calendar=fixture()[0];self.calendar[0].update(changes)
            with self.assertRaises(ValueError):self.check()

    def test_espn_must_be_full_validated_and_match_every_independent_final(self):
        for changes in [{'complete':False},{'upstream_full_game_validated':False},{'pending_game_ids':['x']}]:
            self.snapshot=fixture()[2];self.snapshot.update(changes)
            with self.assertRaises(ValueError):self.check()
        self.snapshot=fixture()[2];self.snapshot['games'][0]['home_score']=61
        with self.assertRaisesRegex(ValueError,'scores disagree'):self.check()

    def test_eastern_clock_and_future_publication_are_not_silently_accepted(self):
        self.snapshot['games'][0]['start_time']='2026-11-05T15:00:00Z'
        with self.assertRaisesRegex(ValueError,'Eastern'):self.check()
        self.snapshot=fixture()[2];self.rs['source_published_at']='2026-11-06T15:00:00Z'
        with self.assertRaises(ValueError):self.check()


class FakeMirror:
    end_year=2027
    def __init__(self):
        calendar,results,_=fixture();self.values={'nba_schedule_2027.parquet':json.dumps(calendar).encode(),
                                                'player_game_logs_2027.parquet':json.dumps(results).encode()};self.calls=[]
    async def release(self,tag):
        name='nba_schedule_2027.parquet' if tag=='nba_stats_schedules' else 'player_game_logs_2027.parquet'
        data=self.values[name]
        return {name:{'browser_download_url':pub.ROOT+tag+'/'+name,'size':len(data),
                      'updated_at':'2026-11-05T15:00:00Z','digest':'sha256:'+hashlib.sha256(data).hexdigest()}}
    async def asset(self,tag,name):
        self.calls.append(name)
        return self.values[name],{'browser_download_url':pub.ROOT+tag+'/'+name}


class HistoricalInventoryFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_sources_are_downloaded_once_and_reused_with_original_source_dates(self):
        mirror=FakeMirror()
        with patch.object(pub,'_parquet_rows',side_effect=json.loads):
            one=await pub.fetch_historical_inventory(mirror,DAY,fixture()[2],checked_at=NOW)
            two=await pub.fetch_historical_inventory(mirror,DAY,fixture()[2],checked_at=NOW)
        self.assertEqual(one,two);self.assertEqual(len(mirror.calls),2)

    async def test_source_hash_failure_stops_before_any_inventory_certificate(self):
        mirror=FakeMirror();original=mirror.asset
        async def corrupt(tag,name):
            body,meta=await original(tag,name);return body+b'bad',meta
        mirror.asset=corrupt
        with patch.object(pub,'_parquet_rows',side_effect=json.loads),self.assertRaisesRegex(ValueError,'checksum'):
            await pub.fetch_historical_inventory(mirror,DAY,fixture()[2],checked_at=NOW)
        self.assertEqual(len(mirror.calls),1)


if __name__=='__main__':unittest.main()
