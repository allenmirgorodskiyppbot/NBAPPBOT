import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nbabot.core import Store, load_config
from nbabot.rotation_reference import collect_rotation_reference, load_rotation_reference, validate_rotation


def fixture(overtime=False):
    end=31800 if overtime else 28800
    mins='53:00' if overtime else '48:00'
    gid='0022500001'
    headers=['GAME_ID','TEAM_ID','PERSON_ID','PLAYER_FIRST','PLAYER_LAST','IN_TIME_REAL','OUT_TIME_REAL']
    rotation={'resultSets':[]}
    box={'boxScoreTraditional':{'gameId':gid}}
    for side,tid,team,prefix in [('homeTeam','1610612752','NYK','0'),('awayTeam','1610612738','BOS','1')]:
        players=[]; rows=[]
        for i in range(1,6):
            pid=prefix+str(i).zfill(2)
            players.append({'personId':pid,'firstName':'Fixture','familyName':pid,'position':'F','statistics':{'minutes':mins}})
            rows.append([gid,tid,pid,'Fixture',pid,0,end])
        box['boxScoreTraditional'][side]={'teamId':tid,'teamTricode':team,'players':players}
        rotation['resultSets'].append({'name':side,'headers':headers,'rowSet':rows})
    game={'game_id':gid,'opponent':'BOS','season':'2025-26','date_et':'2025-11-01','phase':'Regular Season'}
    return rotation,box,game


def substitute_fixture(tick=14400):
    rotation,box,game=fixture()
    rotation['resultSets'][0]['rowSet'][0][-1]=tick
    rotation['resultSets'][0]['rowSet'].append([game['game_id'],'1610612752','006','Fixture','006',tick,28800])
    box['boxScoreTraditional']['homeTeam']['players'][0]['statistics']['minutes']='24:00'
    box['boxScoreTraditional']['homeTeam']['players'].append({'personId':'006','firstName':'Fixture','familyName':'006','position':'','statistics':{'minutes':'24:00'}})
    return rotation,box,game


class RotationReference(unittest.TestCase):
    def test_validated_measured_stints_preserve_ids_and_scope(self):
        r,b,g=substitute_fixture()
        parsed=validate_rotation(r,b,g,{'001','006','101'})
        self.assertEqual(parsed['validation']['status'],'passed')
        self.assertEqual({x['player_id'] for x in parsed['stints']},{'001','006','101'})
        self.assertEqual(parsed['stints'][0]['in_time_source'],'0')
        self.assertEqual(next(x for x in parsed['stints'] if x['player_id']=='001')['duration_seconds'],'1440')
        self.assertEqual({x['player_id']:x['started'] for x in parsed['starts']},{'001':1,'006':0,'101':1})
        self.assertTrue(all(x['full_lineup_size']==5 for x in parsed['lineup_intervals']))
        self.assertTrue(all(set(x['scoped_player_ids'])<={'001','006','101'} for x in parsed['lineup_intervals']))

    def test_published_second_rounding_is_checked_without_adjusting_source(self):
        r,b,g=substitute_fixture(14405)
        parsed=validate_rotation(r,b,g,{'001','006'})
        self.assertEqual(parsed['stints'][0]['duration_seconds'],'1440.5')
        self.assertEqual(parsed['checks'][0]['box_minutes_source'],'24:00')
        r,b,g=substitute_fixture(14406)
        with self.assertRaisesRegex(ValueError,'source box precision'):
            validate_rotation(r,b,g,{'001'})

    def test_missing_duplicate_overlap_or_wrong_game_rejected(self):
        r,b,g=fixture()
        r['resultSets'][0]['rowSet'].pop()
        with self.assertRaisesRegex(ValueError,'Five-player'):
            validate_rotation(r,b,g,{'001'})
        r,b,g=fixture()
        r['resultSets'][0]['rowSet'].append(r['resultSets'][0]['rowSet'][0])
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            validate_rotation(r,b,g,{'001'})
        r,b,g=fixture()
        r['resultSets'][0]['rowSet'][0][0]='0022500999'
        with self.assertRaisesRegex(ValueError,'game ID'):
            validate_rotation(r,b,g,{'001'})

    def test_zero_clock_without_elapsed_rotation_is_not_a_bench_appearance(self):
        r,b,g=fixture()
        b['boxScoreTraditional']['homeTeam']['players'].append({'personId':'006','position':'','comment':'DNP','statistics':{'minutes':'0:00'}})
        parsed=validate_rotation(r,b,g,{'001','006'})
        self.assertEqual({x['player_id'] for x in parsed['starts']},{'001'})
        self.assertFalse(any(x['player_id']=='006' for x in parsed['stints']))

    def test_overtime_and_observed_starting_lineup(self):
        r,b,g=fixture(overtime=True)
        parsed=validate_rotation(r,b,g,{'001'})
        self.assertEqual(parsed['validation']['overtime_periods'],1)
        self.assertEqual(parsed['stints'][0]['duration_seconds'],'3180')
        b['boxScoreTraditional']['homeTeam']['players'][0]['position']=''
        with self.assertRaisesRegex(ValueError,'opening lineup'):
            validate_rotation(r,b,g,{'001'})

    def test_cache_rerun_and_source_failure_retain_verified_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config(Path(tmp)/'missing.json')
            c.update(data_dir=str(Path(tmp)/'data'),organized_players=['001'])
            store=Store(c)
            r,b,g=fixture()
            def source(url,config):
                return copy.deepcopy(r if 'gamerotation' in url else b)
            try:
                with patch('nbabot.maintenance.fetch_json',side_effect=source) as mocked:
                    first=collect_rotation_reference(store,games=[g],cohort_by_team={'BOS':['101']})
                    self.assertEqual(first['status'],'success')
                    self.assertEqual(mocked.call_count,2)
                    second=collect_rotation_reference(store,games=[g],cohort_by_team={'BOS':['101']})
                    self.assertEqual(second['coverage']['reused_games'],1)
                    self.assertEqual(mocked.call_count,2)
                before=load_rotation_reference(store)
                self.assertEqual(len(before['stints']),2)
                with patch('nbabot.maintenance.fetch_json',side_effect=TimeoutError('fixture')):
                    failed=collect_rotation_reference(store,games=[g],cohort_by_team={'BOS':['101']},force=True)
                self.assertEqual(failed['status'],'partial')
                after=load_rotation_reference(store)
                self.assertEqual(after['stints'],before['stints'])
                self.assertTrue(after['errors'][0]['retained_previous'])
                root=store.root/'reference/rotations'
                index=json.loads((root/'latest.json').read_text())
                index['games'][0]['path']='../nba.sqlite3'
                (root/'latest.json').write_text(json.dumps(index))
                invalid=load_rotation_reference(store)
                self.assertEqual(invalid['stints'],[])
                self.assertIn('Unsafe',invalid['errors'][-1]['error'])
            finally:
                store.close()

    def test_failed_game_cooldown_and_appended_batches_preserve_missingness(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config(Path(tmp)/'missing.json')
            c.update(data_dir=str(Path(tmp)/'data'),organized_players=['001'])
            store=Store(c)
            r,b,g=fixture()
            try:
                with patch('nbabot.maintenance.fetch_json',side_effect=TimeoutError('fixture')) as mocked:
                    first=collect_rotation_reference(store,games=[g],cohort_by_team={})
                    self.assertEqual(first['status'],'failed')
                    again=collect_rotation_reference(store,games=[g],cohort_by_team={})
                    self.assertEqual(again['coverage']['cooldown_skipped_games'],1)
                    self.assertEqual(mocked.call_count,1)
                appended=collect_rotation_reference(store,games=[],cohort_by_team={})
                self.assertEqual(appended['coverage']['unresolved_cached_failures'],1)
                self.assertEqual(load_rotation_reference(store)['errors'][0]['game_id'],g['game_id'])
                with patch('nbabot.maintenance.fetch_json',side_effect=lambda url, config: copy.deepcopy(r if 'gamerotation' in url else b)):
                    resolved=collect_rotation_reference(store,games=[g],cohort_by_team={},force=True)
                self.assertEqual(resolved['status'],'success')
                self.assertEqual(load_rotation_reference(store)['errors'],[])
            finally:
                store.close()

    def test_invalid_source_is_archived_but_not_published_as_stints(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config(Path(tmp)/'missing.json')
            c.update(data_dir=str(Path(tmp)/'data'),organized_players=['001'])
            store=Store(c)
            r,b,g=fixture()
            r['resultSets'][0]['rowSet'].pop()
            try:
                with patch('nbabot.maintenance.fetch_json',side_effect=lambda url,config: copy.deepcopy(r if 'gamerotation' in url else b)):
                    result=collect_rotation_reference(store,games=[g],cohort_by_team={})
                self.assertEqual(result['status'],'failed')
                self.assertEqual(load_rotation_reference(store)['stints'],[])
                self.assertEqual(result['errors'][0]['failure_kind'],'validation')
                captures=list((store.root/'reference/rotations/quarantine').glob('*.json'))
                self.assertEqual(len(captures),1)
                self.assertEqual(json.loads(captures[0].read_text())['validation_status'],'withheld')
            finally:
                store.close()


if __name__=='__main__':
    unittest.main()
