"""An independent game feed never establishes a tracking extract's coverage."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from advanced_refresh import import_tracking
from dataset_refresh import initialize_scope
from feeds import canonical_game,canonical_player
from master import MasterStore
from mirror_sources import PublishedNBAData,tracking_coverage
from player_stats import published_average


NOW=dt.datetime(2026,10,30,15,30,tzinfo=dt.timezone.utc)
PUBLISHED='2026-10-29T14:18:20+00:00'


class MirrorCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output=Path(self.directory.name)
        self.mirror=PublishedNBAData(None,NOW)
        self.scope={'revision':'fixture','teams':['PHI'],
                    'players':[{'player':'Tyrese Maxey','nba_player_id':'1630178'}]}
        self.mirror.scope=self.scope
        self.mirror.days={'2026-10-28':{}}
        self.changed=False

        async def asset(tag,name):
            row={'season':2027,'season_type':'Regular Season','per_mode':'Totals',
                 'player_name':'Tyrese Maxey','player_id':1630178,'team_id':1610612755,
                 'team_abbreviation':'PHI','gp':5,'pts':125,'reb':20,'ast':35,'min':175,
                 'opp_pts':500,'opp_fgm':200,'opp_fga':400,'potential_ast':60,
                 'passes_made':300,'reb_chances':50,'touches':400,'time_of_poss':40,
                 'def_rim_fgm':5,'def_rim_fga':10}
            updated=PUBLISHED
            if self.changed and name.startswith('player_stats_base'):
                row.update(gp=6,pts=155,reb=24,ast=40,min=210)
                updated='2026-10-30T14:18:20+00:00'
            return json.dumps([row]).encode(),{'updated_at':updated,
                'browser_download_url':'https://example.com/'+name}

        self.mirror.asset=asset
        module=types.ModuleType('pyarrow');parquet=types.ModuleType('pyarrow.parquet')
        class Table:
            def __init__(self,content):self.rows=json.loads(content.getvalue())
            def to_pylist(self):return self.rows
        parquet.read_table=Table;module.parquet=parquet
        self.modules=patch.dict(sys.modules,{'pyarrow':module,'pyarrow.parquet':parquet})
        self.modules.start();self.addCleanup(self.modules.stop)

    def read(self):
        return json.loads((self.output/'existing_tracking.json').read_text())

    async def seed_known_snapshot(self):
        await self.mirror.tracking(self.output)
        data=self.read()
        for record in data['records']:
            record.update(data_through_date='2026-10-28',coverage_basis='source_reported')
        (self.output/'existing_tracking.json').write_text(json.dumps(data))

    async def test_new_game_publication_does_not_redate_unchanged_tracking(self):
        await self.seed_known_snapshot()
        self.mirror.days['2026-10-29']={}
        await self.mirror.tracking(self.output)
        data=self.read()
        self.assertEqual(len(data['records']),7)
        self.assertEqual({r['data_through_date'] for r in data['records']},{'2026-10-28'})
        self.assertEqual(data['source_data_through_date'],'2026-10-28')
        self.assertEqual({r['coverage_basis'] for r in data['records']},{'prior_unchanged_source_statistics'})
        self.assertEqual({r['coverage_origin'] for r in data['records']},{'source_reported'})
        self.assertTrue(all(c['source_content_sha256'] for c in data['coverage'] if c['status']=='collected'))
        await self.mirror.tracking(self.output)
        self.assertEqual({r['data_through_date'] for r in self.read()['records']},{'2026-10-28'})

    async def test_legacy_csv_derived_coverage_is_not_preserved(self):
        await self.seed_known_snapshot()
        data=self.read()
        for record in data['records']:
            record['data_through_date']='2026-10-29'
            record.pop('coverage_basis',None)
            record.pop('coverage_origin',None)
        (self.output/'existing_tracking.json').write_text(json.dumps(data))
        self.mirror.days['2026-10-29']={}
        await self.mirror.tracking(self.output)
        self.assertTrue(all(r['data_through_date'] is None for r in self.read()['records']))

    def test_unchanged_label_without_origin_does_not_launder_legacy_date(self):
        old={'entity':'fixture','source':'fixture','data_through_date':'2026-10-29',
             'coverage_basis':'prior_unchanged_source_statistics'}
        record=tracking_coverage({'entity':'fixture','source':'fixture'},old)
        self.assertIsNone(record['data_through_date'])

    async def test_changed_tracking_has_unknown_coverage_instead_of_game_or_publication_date(self):
        await self.seed_known_snapshot()
        self.mirror.days['2026-10-29']={};self.changed=True
        await self.mirror.tracking(self.output)
        data=self.read()
        player=next(r for r in data['records'] if r['category']=='Player totals')
        self.assertEqual(player['gp'],6)
        self.assertEqual(player['fields']['pts'],155)
        self.assertEqual(player['published_at'],'2026-10-30T14:18:20+00:00')
        self.assertIsNone(player['data_through_date'])
        self.assertEqual(player['coverage_basis'],'not_reported_by_tracking_source')
        self.assertIsNone(data['source_data_through_date'])
        defense=next(r for r in data['records'] if r['category']=='Team defense')
        self.assertEqual(defense['data_through_date'],'2026-10-28')

    async def test_first_tracking_publication_has_unknown_covered_date(self):
        self.mirror.days['2026-10-29']={}
        await self.mirror.tracking(self.output)
        data=self.read()
        self.assertTrue(data['records'])
        self.assertTrue(all(r['data_through_date'] is None for r in data['records']))
        self.assertIsNone(data['source_data_through_date'])


class AggregateCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'master.sqlite')
        self.scope=initialize_scope(self.store)

    def record(self,covered=None,published='2026-10-30T14:18:20+00:00'):
        return {'entity':'Tyrese Maxey','player_id':canonical_player('Tyrese Maxey'),
                'category':'Player totals','season':'2026-27','season_type':'Regular Season',
                'team':'PHI','gp':6,'fields':{'pts':150,'reb':24,'ast':40,'minutes':210},
                'source':'https://example.com/player_stats_base_2027.parquet',
                'published_at':published,'data_through_date':covered}

    def import_record(self,record):
        return import_tracking(self.store,{'schema_version':1,'scope_revision':self.scope['revision'],
            'season':'2026-27','records':[record],'coverage':[]},NOW)

    def observation(self):
        game=canonical_game('2026-10-29','PHI','CLE')
        self.store.upsert_game({'id':game,'date':'2026-10-29','home_team':'PHI','away_team':'CLE','status':'final'})
        self.store.upsert_player_game({'game_id':game,'player':'Tyrese Maxey','team':'PHI','opponent':'CLE',
            'date':'2026-10-29','season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
            'pts':35,'reb':4,'ast':5,'minutes':35})

    def average(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT ppg,games_played,through_date FROM season_averages WHERE player='Tyrese Maxey'")]

    def test_unknown_coverage_does_not_use_publication_date_or_hide_dated_observations(self):
        self.observation();self.import_record(self.record())
        self.assertEqual(self.average(),[{'ppg':35.0,'games_played':1,'through_date':'2026-10-29'}])
        self.assertIsNone(published_average(self.store,'Tyrese Maxey',['pts'],'2026-27'))

    def test_published_values_remain_available_without_inventing_coverage(self):
        self.import_record(self.record())
        self.assertEqual(self.average(),[{'ppg':25.0,'games_played':6,'through_date':None}])
        answer=published_average(self.store,'Tyrese Maxey',['pts'],'2026-27')
        self.assertIn('Covered game date: unknown',answer['answer'])
        self.assertIsNone(answer['data']['through_date'])
        self.assertEqual(answer['data']['source_published_at'],'2026-10-30T14:18:20+00:00')

    def test_known_covered_record_wins_without_duplicates_and_stale_dates_cannot_replace_it(self):
        self.observation();self.import_record(self.record('2026-10-29'))
        self.assertEqual(self.average(),[{'ppg':25.0,'games_played':6,'through_date':'2026-10-29'}])
        older=self.record('2026-10-28',published='2026-10-30T15:00:00+00:00')
        older['fields']['pts']=999
        self.import_record(older)
        self.assertEqual(self.average()[0]['ppg'],25.0)
        stale_unknown=self.record(published='2026-10-29T14:00:00+00:00')
        stale_unknown['fields']['pts']=888
        self.import_record(stale_unknown)
        self.assertEqual(self.average()[0]['ppg'],25.0)

    def test_bad_coverage_date_is_rejected(self):
        for value in ('unknown','2026-13-01','2026-10-31',123):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.import_record(self.record(value))

    def test_legacy_mirror_import_and_reopen_clear_dates_without_losing_values(self):
        legacy=self.record('2026-10-29')
        legacy['source']='https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_leaguedash/player_stats_base_2027.parquet'
        # Simulate an old collector writing just before this code deploys.
        with self.store.connect() as db:
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                (legacy['season'],legacy['entity'],legacy['category'],json.dumps(legacy)))
        self.store=MasterStore(self.store.path)
        self.assertEqual(self.average(),[{'ppg':25.0,'games_played':6,'through_date':None}])
        # The next legacy snapshot cannot reintroduce the false covered date.
        self.import_record(legacy)
        self.assertIsNone(self.average()[0]['through_date'])
        # Identical publication times accept the provenance correction.
        verified=dict(legacy,coverage_basis='source_reported')
        self.import_record(verified)
        self.assertEqual(self.average()[0]['through_date'],'2026-10-29')
        cleaned=dict(legacy,data_through_date=None,coverage_basis='not_reported_by_tracking_source')
        self.import_record(cleaned)
        self.assertIsNone(self.average()[0]['through_date'])
        answer=published_average(self.store,'Tyrese Maxey',['pts'],'2026-27')
        self.assertIn('pts: 25.00 per game',answer['answer'])
        self.assertIn('Covered game date: unknown',answer['answer'])


if __name__=='__main__':unittest.main()
