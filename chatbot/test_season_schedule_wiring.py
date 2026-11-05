"""Knicks season calendar collection and views stay separate from NBA-wide stats and live readiness."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from master import MasterStore
from feeds import canonical_game, canonical_player
import supplemental_data as sd
from publish_public_data import allowed_path
from refresh_export import build_export_tables
from nba_daily_export import main_async

NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)


def calendar_document():
    from test_season_schedule_refresh import fixture_document
    return fixture_document(now=NOW)


class SeasonScheduleRuntimeWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = MasterStore(self.root / 'master.sqlite')
        self.document = calendar_document(); self.blob = json.dumps(self.document).encode()
        self.requests = []
        def handle(request):
            self.requests.append(str(request.url))
            self.assertEqual(str(request.url), sd.SEASON_SCHEDULE_URL)
            return httpx.Response(200, content=self.blob)
        self.client = httpx.Client(transport=httpx.MockTransport(handle)); self.addCleanup(self.client.close)
        self.store.set_checkpoint('nba_schedule_today', {'status':'failed','error':'HTTP 403'})
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete':False})

    def test_knicks_calendar_is_retrieved_and_future_games_are_exported(self):
        readiness = {key:self.store.get_checkpoint(key) for key in ('nba_schedule_today','daily_nba:2026-11-04')}
        report, changed = sd._season_schedule(self.store,self.root,self.client,{},NOW)
        self.assertTrue(changed); self.assertEqual(report['assigned_games'],80)
        self.assertEqual(report['relay_source'],sd.SEASON_SCHEDULE_URL)
        self.assertEqual(report['source'],self.document['source'])
        tables = build_export_tables(self.store,NOW)
        rows = tables['Knicks_Season_Schedule']
        self.assertEqual(len(rows),80)
        self.assertTrue(all('NYK' in (row['home_team'],row['away_team']) for row in rows))
        self.assertTrue(any(row['date'] > NOW.date().isoformat() for row in rows))
        self.assertTrue(all(row['start_time'] is None and 'unknown' in row['timing_scope'] for row in rows))
        coverage = tables['Schedule_Coverage']; self.assertEqual(len(coverage),1)
        self.assertEqual(coverage[0]['team'],'NYK')
        self.assertTrue(all(row['assigned_games']==80 and row['regular_season_target']==82 and row['unassigned_team_games']==2 for row in coverage))
        self.assertFalse(any(row['live_timing_verified'] for row in coverage))
        self.assertEqual(sum(row.get('aggregate_game_count',1) for row in tables['Schedule_TBD']),2)
        self.assertIn('Team_Schedule',tables)
        self.assertEqual(readiness,{key:self.store.get_checkpoint(key) for key in readiness})

    def test_identical_source_rechecks_without_claiming_changed_values(self):
        previous, changed = sd._season_schedule(self.store,self.root,self.client,{},NOW)
        self.assertTrue(changed)
        later, changed = sd._season_schedule(self.store,self.root,self.client,previous,NOW+dt.timedelta(hours=1))
        self.assertFalse(changed); self.assertEqual(later['phase'],'unchanged')
        self.assertEqual(self.requests,[sd.SEASON_SCHEDULE_URL,sd.SEASON_SCHEDULE_URL])
        self.assertEqual(later['source_published_at'],previous['source_published_at'])
        self.assertEqual(later['content_sha256'],previous['content_sha256'])

    def test_knicks_calendar_does_not_narrow_or_modify_nba_wide_player_statistics(self):
        identity = canonical_game('2026-11-02','LAL','CHI')
        self.store.upsert_game({'id':identity,'date':'2026-11-02','home_team':'LAL','away_team':'CHI',
            'status':'final','home_score':110,'away_score':101,'source':'synthetic NBA-wide fixture'})
        self.store.upsert_player_game({'game_id':identity,'date':'2026-11-02','player':'LeBron James',
            'player_id':canonical_player('LeBron James'),'team':'LAL','opponent':'CHI','pts':27,
            'minutes':34,'appearance_state':'Played','season':'2026-27','season_type':'Regular Season',
            'source':'synthetic NBA-wide fixture'})
        scope = {'population':'all_current_nba_players','teams':['CHI','LAL','NYK']}
        self.store.set_checkpoint('dataset_refresh_scope',scope)
        with self.store.connect() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM player_games')]
        sd._season_schedule(self.store,self.root,self.client,{},NOW)
        with self.store.connect() as db:
            self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM player_games')])
            self.assertEqual(db.execute('SELECT home_score,away_score FROM games WHERE id=?',(identity,)).fetchone()[:],(110,101))
        self.assertEqual(self.store.get_checkpoint('dataset_refresh_scope'),scope)
        tables = build_export_tables(self.store,NOW)
        self.assertTrue(any(row['player']=='LeBron James' for row in tables['player_games']))
        self.assertTrue(all('NYK' in (row['home_team'],row['away_team']) for row in tables['Knicks_Season_Schedule']))

    def test_failed_source_retains_calendar_and_reports_partial(self):
        previous, _ = sd._season_schedule(self.store,self.root,self.client,{},NOW)
        self.store.set_checkpoint(sd.CHECKPOINT, {'season_schedule':previous})
        with self.store.connect() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM season_schedule ORDER BY id')]
        client = httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(503)))
        self.addCleanup(client.close)
        def unchanged(*args): return {'phase':'unchanged'},False
        with patch.object(sd,'_repairs',unchanged),patch.object(sd,'_measured',unchanged),patch.object(sd,'_availability',unchanged):
            result = sd._refresh(self.store,self.root,client,NOW+dt.timedelta(hours=1),force=True)
        self.assertEqual(result['phase'],'partial'); self.assertFalse(result['changed'])
        self.assertEqual(result['season_schedule']['http_status'],503)
        self.assertTrue(result['season_schedule']['previous_evidence_retained'])
        with self.store.connect() as db:
            self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM season_schedule ORDER BY id')])
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'),{'status':'failed','error':'HTTP 403'})


class SeasonScheduleCollectorWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_collection_calls_season_calendar_before_day_boxes_and_preserves_failed_relay(self):
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); prior = b'{"prior":"calendar bytes"}\n'; (output/'season_schedule.json').write_bytes(prior)
            async def current(*args,**kwargs): calls.append('current_timing'); return {'status':'success'}
            async def calendar(*args,**kwargs): calls.append('season_calendar'); raise ValueError('Invalid new publication')
            day = {'source':'espn','games':[],'player_games':[],'complete':True}
            async def boxes(*args,**kwargs): calls.append('day_boxes'); return day
            collector = SimpleNamespace(now=NOW,publish_current_schedule=current,inventory=AsyncMock(return_value={}))
            mirror = SimpleNamespace(scope={'revision':'fixture'},configure_scope=Mock(),publish_scope=Mock(),day=boxes,
                                     tracking=AsyncMock(return_value=True))
            args = SimpleNamespace(output=str(output),days=1,schedule_only=False)
            with patch('nba_daily_export.PublicCollector',return_value=collector),patch('mirror_sources.PublishedNBAData',return_value=mirror), \
                 patch('season_schedule_refresh.collect_season_schedule',side_effect=calendar), \
                 patch('nba_daily_export.reconcile_day_inventory',return_value=day), \
                 patch('advanced_refresh.supplement_tracking',new=AsyncMock(return_value=False)), \
                 patch('nba_daily_export.collect_availability_inputs',new=AsyncMock(return_value={'phase':'updated'})), \
                 patch('career_history_refresh.refresh_career_history',new=AsyncMock(return_value={'phase':'updated'})), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = await main_async(args)
            self.assertEqual(result,0); self.assertEqual(calls,['current_timing','season_calendar','day_boxes'])
            self.assertEqual((output/'season_schedule.json').read_bytes(),prior)

    def test_only_daily_publication_scope_allows_the_calendar_relay(self):
        self.assertTrue(allowed_path('data/live/season_schedule.json','daily'))
        self.assertFalse(allowed_path('data/live/season_schedule.json','schedule'))
        for path in ('data/live/../season_schedule.json','data/live/cache/season_schedule.json','data/live/season_schedule.json.tmp'):
            self.assertFalse(allowed_path(path,'daily'))


if __name__ == '__main__':
    unittest.main()
