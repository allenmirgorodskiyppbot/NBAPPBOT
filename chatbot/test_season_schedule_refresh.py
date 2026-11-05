"""Isolated whole-season calendar tests; no live source or private master writes."""
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock

from master import MasterStore
from season_calendar import current_season
from season_schedule_refresh import (TEAM_IDS,ROOT,TAG,build_published_schedule,collect_season_schedule,
    import_published_schedule,validate_published_schedule,schedule_coverage_rows,unassigned_schedule_rows,team_calendar_metadata)

NOW=dt.datetime(2026,11,5,19,tzinfo=dt.timezone.utc)


def fixture_rows(now=None):
    """Bundled NYK80fixture identities with synthetic provider IDs and source metadata."""
    from season_calendar import SEED_PATH
    now=now or NOW;year=int(current_season(now)[:4]);seed=json.loads(SEED_PATH.read_text());rows=[]
    for index,game in enumerate(seed['games']):
        home,away=game['home_team'],game['away_team']
        rows.append({'game_id':f'002{str(year)[-2:]}{index+1:05d}',
            'season':year+1,'season_type':'regular-season','game_date':game['date'],
            'home_team_abbreviation':home,'away_team_abbreviation':away,
            'home_team_id':int(TEAM_IDS[home]),'away_team_id':int(TEAM_IDS[away]),'matchup':f'{home} vs. {away}'})
    return rows


def fixture_document(now=None):
    now=now or NOW;end=int(current_season(now)[:4])+1
    meta={'source':ROOT+TAG+f'/nba_schedule_{end}.parquet',
          'source_published_at':(now-dt.timedelta(days=2)).isoformat(),'source_content_sha256':'a'*64}
    return build_published_schedule(fixture_rows(now),meta,now)


class PublishedSeasonTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'isolated.sqlite')
        self.document=fixture_document()

    def count(self,table):
        with self.store.connect() as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_knicks_only_and_honest_unassigned_inventory(self):
        doc=self.document
        self.assertEqual((doc['assigned_games'],doc['published_regular_slots']), (80,80))
        self.assertEqual((doc['unassigned_games'],doc['unpublished_regular_games']), (2,2))
        self.assertEqual(len(doc['team_coverage']),1)
        self.assertTrue(all(row['assigned_games']==80 and row['unassigned_team_games']==2 for row in doc['team_coverage']))
        self.assertFalse(doc['complete_season_schedule']);self.assertTrue(doc['complete_for_published_games'])
        self.assertEqual(len(doc['unassigned_slots']),0)
        self.assertTrue(all(row['start_time'] is None for row in doc['games']))

    def test_current_source_never_infers_finals_from_points(self):
        rows=fixture_rows();rows[0].update(home_pts=120,away_pts=110,home_wl='W',away_wl='L')
        doc=build_published_schedule(rows,self.document,NOW)
        self.assertTrue(all(row['status']=='time_tbd' for row in doc['games']))
        self.assertTrue(all('home_score' not in row and 'away_score' not in row for row in doc['games']))

    def test_source_filter_ignores_other_teams_calendar_and_global_unknown_cup_slots(self):
        rows=fixture_rows()
        rows.append({'game_id':'0022601000','season':2027,'season_type':'regular-season','game_date':'2026-11-05',
            'home_team_abbreviation':'BOS','away_team_abbreviation':'CHI','home_team_id':int(TEAM_IDS['BOS']),
            'away_team_id':int(TEAM_IDS['CHI']),'matchup':'BOS vs. CHI'})
        rows.append({'game_id':'0022601201','season':2027,'season_type':'regular-season','game_date':'2026-12-04',
            'home_team_abbreviation':None,'away_team_abbreviation':None,'home_team_id':0,'away_team_id':0,
            'home_pts':None,'away_pts':None,'home_wl':None,'away_wl':None,'matchup':None})
        doc=build_published_schedule(rows,self.document,NOW)
        self.assertEqual(len(doc['games']),80);self.assertEqual(doc['unassigned_slots'],[])
        self.assertEqual(doc['covered_teams'],['NYK'])
        self.assertTrue(all('NYK' in (row['home_team'],row['away_team']) for row in doc['games']))

    def test_existing_other_team_fixture_is_outside_this_import_scope(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ('nba:2026-11-05:BOS:CHI','2026-27','2026-11-05','BOS','CHI',None,'time_tbd',
                 'https://example.org/synthetic','2026-11-01','published_league_calendar','2026-11-01T00:00:00+00:00','{}'))
        import_published_schedule(self.store,self.document,NOW)
        with self.store.connect() as db:
            other=dict(db.execute("SELECT * FROM season_schedule WHERE id='nba:2026-11-05:BOS:CHI'").fetchone())
        self.assertEqual(other['source'],'https://example.org/synthetic');self.assertEqual(self.count('season_schedule'),81)

    def test_import_calendar_only_preserves_results_and_live_freshness(self):
        row=self.document['games'][0]
        self.store.upsert_game({**row,'status':'final','home_score':120,'away_score':110,'source':'synthetic known result'})
        self.store.set_checkpoint('nba_schedule_today',{'status':'failed','date':'2026-11-05'})
        result=import_published_schedule(self.store,self.document,NOW)
        self.assertTrue(result['changed']);self.assertEqual(self.count('season_schedule'),80)
        self.assertEqual(self.count('games'),1);self.assertEqual(self.count('player_games'),0)
        with self.store.connect() as db:
            actual=dict(db.execute('SELECT * FROM games').fetchone())
        self.assertEqual((actual['status'],actual['home_score'],actual['away_score']),('final',120,110))
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today')['status'],'failed')
        self.assertFalse(self.store.get_checkpoint('daily_nba:2026-11-04'))

    def test_existing_exact_tipoff_and_provenance_are_preserved(self):
        row=self.document['games'][0]
        tip=row['date']+'T23:30:00+00:00';source='https://www.nba.com/team/synthetic/schedule'
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'],row['season'],row['date'],row['home_team'],row['away_team'],tip,'scheduled',
                 source,'2026-10-29','saved_publication','2026-10-30T00:00:00+00:00','{"original":true}'))
        result=import_published_schedule(self.store,self.document,NOW)
        with self.store.connect() as db:actual=dict(db.execute('SELECT * FROM season_schedule WHERE id=?',(row['id'],)).fetchone())
        self.assertEqual((actual['start_time'],actual['source'],actual['source_asof']), (tip,source,'2026-10-29'))
        self.assertEqual(result['preserved_tipoffs'],1)
        proof=json.loads(actual['payload_json'])['preserved_tipoff_provenance']
        self.assertEqual(proof['start_time'],tip)

    def test_repeated_import_is_unchanged_without_source_freshness_promotion(self):
        import_published_schedule(self.store,self.document,NOW)
        result=import_published_schedule(self.store,self.document,NOW+dt.timedelta(minutes=1))
        self.assertFalse(result['changed']);self.assertEqual(result['phase'],'unchanged')
        self.assertEqual(result['source_published_at'],self.document['source_published_at'])
        self.assertEqual(self.count('season_schedule'),80)

    def test_older_publication_is_retained_without_mutation(self):
        import_published_schedule(self.store,self.document,NOW)
        old=fixture_document(NOW-dt.timedelta(days=1))
        result=import_published_schedule(self.store,old,NOW)
        self.assertTrue(result['retained_newer_publication']);self.assertFalse(result['changed'])
        self.assertEqual(self.store.get_checkpoint('season_schedule_refresh')['revision'],self.document['revision'])

    def test_unchanged_source_repairs_missing_local_row_and_reports_real_change(self):
        import_published_schedule(self.store,self.document,NOW)
        with self.store.connect() as db:
            db.execute('DELETE FROM season_schedule WHERE id=?',(self.document['games'][0]['id'],))
        repaired=import_published_schedule(self.store,self.document,NOW)
        self.assertTrue(repaired['changed']);self.assertEqual(self.count('season_schedule'),80)
        self.assertFalse(import_published_schedule(self.store,self.document,NOW)['changed'])

    def test_publication_only_reissue_updates_provenance_without_data_change(self):
        import_published_schedule(self.store,self.document,NOW)
        meta={key:self.document[key] for key in ('source','source_published_at','source_content_sha256')}
        meta.update(source_published_at=(NOW-dt.timedelta(days=1)).isoformat(),source_content_sha256='b'*64)
        reissued=build_published_schedule(fixture_rows(),meta,NOW)
        self.assertEqual(reissued['revision'],self.document['revision'])
        result=import_published_schedule(self.store,reissued,NOW)
        self.assertFalse(result['changed']);self.assertEqual(result['source_content_sha256'],'b'*64)

    def test_rescheduled_future_fixture_retires_older_saved_date_without_transferring_tipoff(self):
        row=next(row for row in self.document['games'] if row['date']>'2026-11-07')
        tip=row['date']+'T23:30:00+00:00'
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'],row['season'],row['date'],row['home_team'],row['away_team'],tip,'scheduled',
                 'https://www.nba.com/team/synthetic/schedule','2026-10-29','saved_publication','2026-10-30T00:00:00+00:00','{}'))
        rows=fixture_rows()
        next(raw for raw in rows if raw['game_id']==row['provider_game_id'])['game_date']='2027-04-01'
        corrected=build_published_schedule(rows,self.document,NOW)
        result=import_published_schedule(self.store,corrected,NOW)
        self.assertTrue(result['changed'])
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM season_schedule WHERE id=?',(row['id'],)).fetchone())
            moved=db.execute('SELECT start_time FROM season_schedule WHERE date=? AND home_team=? AND away_team=?',
                ('2027-04-01',row['home_team'],row['away_team'])).fetchone()
        self.assertIsNotNone(moved);self.assertIsNone(moved[0])

    def test_duplicate_provider_invalid_team_missing_population_and_bad_tipoff_reject(self):
        for mutate in (
                lambda rows:rows.append(copy.deepcopy(rows[0])),
                lambda rows:rows[0].update(home_team_id=123),
                lambda rows:rows.pop(0),
                lambda rows:rows[0].update(home_team_abbreviation='FAKE')):
            rows=fixture_rows();mutate(rows)
            if len(rows)==79: # Removing one remains a legitimate partial calendar; removing NYK does not.
                rows=[r for r in rows if 'NYK' not in (r.get('home_team_abbreviation'),r.get('away_team_abbreviation'))]
            with self.assertRaises(ValueError):build_published_schedule(rows,self.document,NOW)
        bad=copy.deepcopy(self.document);bad['games'][0]['start_time']='2026-10-20T23:00:00+00:00'
        with self.assertRaises(ValueError):import_published_schedule(self.store,bad,NOW)
        self.assertEqual(self.count('season_schedule'),0)

    def test_tampered_coverage_canonical_id_and_unknown_slot_reject_before_writes(self):
        for section,key,value in [('games','id','nba:FAKE'),('team_coverage','assigned_games',82)]:
            bad=copy.deepcopy(self.document);bad[section][0][key]=value
            with self.assertRaises(ValueError):import_published_schedule(self.store,bad,NOW)
        bad=copy.deepcopy(self.document);bad['unassigned_games']=0
        with self.assertRaises(ValueError):validate_published_schedule(bad,NOW)
        self.assertEqual(self.count('season_schedule'),0)

    def test_atomic_failure_rolls_back_every_insert_and_checkpoint(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fixture_fail BEFORE INSERT ON season_schedule WHEN NEW.home_team='NYK' "
                       "BEGIN SELECT RAISE(ABORT,'isolated fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):import_published_schedule(self.store,self.document,NOW)
        self.assertEqual(self.count('season_schedule'),0)
        self.assertFalse(self.store.get_checkpoint('season_schedule_refresh'))

    def test_export_helpers_expose_per_team_gaps_and_unpublished_aggregate(self):
        import_published_schedule(self.store,self.document,NOW)
        coverage=schedule_coverage_rows(self.store)
        gaps=unassigned_schedule_rows(self.store)
        self.assertEqual(len(coverage),1);self.assertTrue(all(r['live_timing_verified'] is False for r in coverage))
        self.assertEqual(len(gaps),1);self.assertEqual(gaps[-1]['aggregate_game_count'],2)
        self.assertIsNone(gaps[-1]['date']);self.assertEqual(gaps[-1]['status'],'not_yet_published')

    def test_existing_team_only_gap_window_survives_only_for_same_assigned_inventory(self):
        window={'start':'2026-12-03','end':'2026-12-11','reason':'Synthetic saved Cup window'}
        nyk=[row for row in self.document['games'] if 'NYK' in (row['home_team'],row['away_team'])]
        with self.store.connect() as db:
            for row in nyk:
                db.execute('INSERT INTO season_schedule VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['id'],row['season'],row['date'],row['home_team'],row['away_team'],None,'time_tbd',
                     'https://www.nba.com/team/synthetic/schedule','2026-10-29','saved_publication','2026-10-30T00:00:00+00:00','{}'))
        self.store.set_checkpoint('season_calendar:2026-27',{'season':'2026-27','covered_teams':['NYK'],
            'unassigned_games':2,'unassigned_windows':[window],'source':'https://www.nba.com/team/synthetic/schedule',
            'source_kind':'saved_publication','source_checked_at':'2026-10-30T00:00:00+00:00','revision':'c'*64})
        import_published_schedule(self.store,self.document,NOW)
        nyk_meta=team_calendar_metadata(self.store,'NYK','2026-27')
        chi_meta=team_calendar_metadata(self.store,'CHI','2026-27')
        self.assertEqual((nyk_meta['published_games'],nyk_meta['regular_season_total'],nyk_meta['unassigned_games']),(80,82,2))
        self.assertEqual(nyk_meta['unassigned_windows'],[window]);self.assertFalse(nyk_meta['unassigned_coverage_unknown'])
        self.assertTrue(nyk_meta['upstream_published_schedule_validated'])
        self.assertNotIn('CHI',chi_meta.get('covered_teams',[]))
        import_published_schedule(self.store,self.document,NOW)
        repeated=team_calendar_metadata(self.store,'NYK','2026-27')
        self.assertEqual(repeated['unassigned_window_provenance']['source'],'https://www.nba.com/team/synthetic/schedule')


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_shared_source_cache_writes_bounded_relay(self):
        from types import SimpleNamespace
        rows=fixture_rows();doc=fixture_document()
        mirror=SimpleNamespace(now=NOW,end_year=2027,_published_inventory_assets={
            'nba_schedule_2027.parquet':(rows,{k:doc[k] for k in ('source','source_published_at','source_content_sha256')})},
            release=AsyncMock(side_effect=AssertionError('No network expected')))
        with tempfile.TemporaryDirectory() as directory:
            result=await collect_season_schedule(mirror,directory,NOW)
            data=(Path(directory)/'season_schedule.json').read_bytes()
        self.assertEqual(result['assigned_games'],80);self.assertLess(len(data),2_000_000)
        validate_published_schedule(json.loads(data),NOW)
        mirror.release.assert_not_awaited()


if __name__=='__main__':unittest.main()
