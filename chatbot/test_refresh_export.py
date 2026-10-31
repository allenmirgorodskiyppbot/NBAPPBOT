"""Validate refreshed export populations against independent numeric examples."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from feeds import canonical_game
from master import MasterStore
from refresh_export import (build_export_tables, calculated_splits, correlations,
                            export_master, summarize, tracking_metrics)

NOW=dt.datetime(2026,10,31,16,tzinfo=dt.timezone.utc)


class RefreshExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.master=MasterStore(self.root/'master.sqlite')
        with self.master.connect() as db:
            db.execute('CREATE TABLE workbook_evidence(kind TEXT,entity TEXT,payload_json TEXT)')

    def observation(self,day,player='Jalen Brunson',team='NYK',opponent='PHI',pts=20,
                    phase='Regular Season',appearance='Played',**values):
        identity=canonical_game(day,team,opponent)
        season=str(int(day[:4])-(int(day[5:7])<7))
        self.master.upsert_game({'id':identity,'date':day,'home_team':team,'away_team':opponent,
                                'status':'final','source':'fixture'})
        self.master.upsert_player_game({'game_id':identity,'player':player,'team':team,'opponent':opponent,
            'date':day,'season':season+'-'+str(int(season)+1)[-2:],'season_type':phase,
            'appearance_state':appearance,'pts':pts,'reb':5,'ast':4,'minutes':30,
            'source':'fixture',**values})
        return identity

    def evidence(self,kind,rows):
        with self.master.connect() as db:
            db.executemany('INSERT INTO workbook_evidence VALUES(?,?,?)',
                [(kind,str(r.get('player') or r.get('selected_player') or ''),json.dumps(r)) for r in rows])

    def dump(self):
        with sqlite3.connect(self.master.path) as db:
            return '\n'.join(db.iterdump())

    def test_current_game_changes_derived_metrics_historical_phase_and_dnp_stay_separate(self):
        self.observation('2026-10-28',pts=10)
        self.observation('2026-10-30',pts=30)
        self.observation('2026-10-29',pts=None,appearance='DNP')
        self.observation('2026-10-27',pts=100,phase='Preseason')
        self.observation('2025-11-01',pts=50)
        def points():
            return next(r for r in build_export_tables(self.master,NOW)['Player_Metrics']
                        if r['statistic']=='pts' and r['season']=='2026-27' and r['season_type']=='Regular Season')
        row=points()
        self.assertEqual((row['n'],row['mean'],row['last_game']),(2,20,'2026-10-30'))
        self.observation('2026-10-30',pts=40)
        self.assertEqual((points()['n'],points()['mean']),(2,25))

    def test_ratios_use_matching_games_and_missing_denominator_is_not_zero(self):
        self.observation('2026-10-28',fgm=1,fga=2,tpm=1,tpa=1,ftm=0,fta=0)
        self.observation('2026-10-30',fgm=9,fga=10,tpm=0,tpa=0,ftm=0,fta=0)
        self.observation('2026-10-29',fgm=None,fga=50)
        rows=build_export_tables(self.master,NOW)['Player_Metrics']
        fg=next(r for r in rows if r['statistic']=='fg_pct')
        ft=next(r for r in rows if r['statistic']=='ft_pct')
        self.assertAlmostEqual(fg['value'],10/12)
        self.assertEqual((fg['n'],fg['missing']),(2,1))
        self.assertIsNone(ft['value'])

    def test_existing_splits_recalculate_only_exact_opponent_and_known_rest(self):
        self.observation('2026-10-27',pts=10)
        self.observation('2026-10-28',pts=30,opponent='BOS')
        self.observation('2026-10-30',pts=50)
        self.evidence('splits',[{'split_id':'s','player':'Jalen Brunson','window':'2026-27',
            'season_type':'Regular Season','definition':'A: Opponent team by Rest','filter':'1',
            'opponent_team':'PHI','statistic':'pts','n':9}])
        self.evidence('rest_schedule',[{'team':'NYK','date':'2026-10-27','rest_days':1}])
        row=build_export_tables(self.master,NOW)['Calculated_Splits'][0]
        self.assertEqual((row['n'],row['mean'],row['baseline_mean']),(1,10,30))
        self.assertEqual(row['unknown_rest_games_excluded'],1)
        self.assertIn('fewer observations',row['warning'])

    def test_correlations_use_same_game_not_same_date_or_dnp_and_preserve_original_window(self):
        for day,a,b in [('2026-10-27',10,40),('2026-10-28',20,30),('2026-10-30',30,20)]:
            self.observation(day,pts=a)
            self.observation(day,player='Tyrese Maxey',team='PHI',opponent='NYK',pts=b)
        self.observation('2026-10-29',pts=40)
        self.observation('2026-10-29',player='Tyrese Maxey',team='PHI',opponent='BOS',pts=99)
        self.evidence('correlations',[{'player':'Jalen Brunson','other_player':'Tyrese Maxey','x':'pts','y':'pts',
            'window':'2026-27','season_type':'Regular Season','relation':'Same-game opponents'}])
        row=build_export_tables(self.master,NOW)['Correlations'][0]
        self.assertEqual(row['matched_games'],3)
        self.assertAlmostEqual(row['pearson_r'],-1)

    def test_coappearance_does_not_include_other_games_or_become_measured_defense(self):
        self.observation('2026-10-27',pts=10)
        self.observation('2026-10-27',player='Tyrese Maxey',team='PHI',opponent='NYK',pts=25)
        self.observation('2026-10-30',pts=30)
        self.observation('2026-10-30',player='Tyrese Maxey',team='PHI',opponent='NYK',pts=None,appearance='DNP')
        self.evidence('assignments',[{'assignment_id':'a','selected_player':'Jalen Brunson','opponent_team':'PHI',
                                    'primary':'Tyrese Maxey','assignment_asof':'2026-10-22'}])
        self.evidence('pair_summaries',[{'assignment_id':'a','direction':'Knicks offense','player':'Jalen Brunson',
            'named_player':'Tyrese Maxey','window':'2026-27','season_type':'Regular Season'}])
        tables=build_export_tables(self.master,NOW)
        row=tables['Pair_Summaries'][0]
        self.assertEqual((row['confirmed_coappearances'],row['mean_pts'],row['unknown_or_not_observed_other']),(1,10,1))
        self.assertEqual(tables['Measured_Pair_Metrics'],[])
        self.assertIn('not measured guarding',row['warning'])

    def test_measured_pair_metrics_preserve_actual_dates_and_zero_attempt_rate_is_blank(self):
        self.observation('2026-10-30',pts=99)
        self.evidence('measured_pairs',[{'game_id':'old','date':'2026-10-20','season':'2026-27',
            'season_type':'Regular Season','offense_player':'Jalen Brunson','defense_player':'Tyrese Maxey',
            'fgm':0,'fga':0,'source_id':'measured'}])
        row=next(r for r in build_export_tables(self.master,NOW)['Measured_Pair_Metrics'] if r['metric']=='fg_pct')
        self.assertIsNone(row['value'])
        self.assertEqual(row['last_game'],'2026-10-20')

    def test_publication_retrieval_unknown_coverage_and_retained_values_are_distinct(self):
        rows=tracking_metrics([{'entity':'Jalen Brunson','category':'Passing','season':'2026-27','gp':5,
            'fields':{'potential_assists':50},'published_at':'2026-10-31T10:00:00+00:00',
            'publication_time_kind':'retrieval_observed','retrieved_at':'2026-10-31T10:00:00+00:00',
            'field_provenance':{'potential_assists':{'published_at':'2026-10-31T10:00:00+00:00',
                                'publication_time_kind':'retrieval_observed'}},
            'retained_fields':{'passes':{'value':20,'gp':1,'published_at':'2026-10-20'}}}])
        current,old=rows
        self.assertIsNone(current['published_at'])
        self.assertIsNone(current['source_published_at'])
        self.assertEqual(current['status'],'source_value_covered_date_unknown')
        self.assertEqual((old['value'],old['gp'],old['published_at']),(20,1,'2026-10-20'))
        self.assertEqual(old['status'],'retained_prior_value_not_refreshed')

    def test_opponent_view_excludes_other_defenders_old_rosters_and_future_measured_rows(self):
        self.observation('2026-10-30')
        self.observation('2026-10-30',player='Tyrese Maxey',team='PHI',opponent='NYK')
        self.observation('2025-11-01',player='Historical Player',team='PHI',opponent='BOS')
        self.master.set_checkpoint('workbook_sheet_inventory',[{'sheet':'Jalen Brunson'},{'sheet':'vs PHI'},{'sheet':'Overview'}])
        for day,defender,team in [('2026-10-20','Tyrese Maxey','PHI'),('2026-10-23','Derrick White','BOS'),
                                  ('2026-11-03','Future Defender','PHI')]:
            self.evidence('measured_pairs',[{'game_id':day,'date':day,'season':'2026-27','season_type':'Regular Season',
                'offense_player':'Jalen Brunson','defense_player':defender,'offense_team':'NYK','defense_team':team,'fgm':1,'fga':2}])
        tables=build_export_tables(self.master,NOW)
        defenders={r['defender'] for r in tables['vs PHI'] if r['section']=='Dated measured matchup'}
        self.assertEqual(defenders,{'Tyrese Maxey'})
        self.assertFalse(any(r.get('player')=='Historical Player' for r in tables['vs PHI']))
        self.assertTrue(tables['Overview'])
        self.assertFalse(any(r['defense_player']=='Future Defender' for r in tables['Measured_Pair_Metrics']))

    def test_readable_existing_profile_tabs_and_coverage_preserve_assignment_date_and_database(self):
        self.observation('2026-10-30')
        self.master.set_checkpoint('workbook_sheet_inventory',[{'sheet':'Jalen Brunson'},{'sheet':'vs PHI'},
            {'sheet':'Matchup_Assignments'},{'sheet':'Legacy_Review'}])
        self.evidence('assignments',[{'assignment_id':'a','selected_player':'Jalen Brunson','opponent_team':'PHI',
            'primary':'Tyrese Maxey','assignment_asof':'2026-10-22'}])
        before=self.dump()
        tables=build_export_tables(self.master,NOW)
        self.assertIn('Jalen Brunson',tables)
        self.assertIn('vs PHI',tables)
        self.assertEqual(tables['Matchup_Assignments'][0]['assignment_asof'],'2026-10-22')
        self.assertEqual(len([r for r in tables['Refresh_Coverage'] if r.get('original_sheet')]),4)
        self.assertEqual(self.dump(),before)

    def test_formula_escaping_applies_to_generated_tables_and_export_is_atomic(self):
        self.observation('2026-10-30')
        self.evidence('assignments',[{'assignment_id':'a','selected_player':'=BAD()','primary':'+SUM(1,2)',
            'assignment_asof':'2026-10-22','opponent_team':'PHI'}])
        path=export_master(self.master,self.root/'exports',NOW)
        with open(path,'rb') as source:
            original=source.read()
        book=openpyxl.load_workbook(path,read_only=True,data_only=False)
        row=list(book['Matchup_Assignments'].values)[1]
        self.assertIn("'=BAD()",row)
        self.assertIn("'+SUM(1,2)",row)
        book.close()
        with patch('openpyxl.workbook.workbook.Workbook.save',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                export_master(self.master,self.root/'exports',NOW)
        self.assertEqual(path.read_bytes(),original)
        self.assertFalse(list(path.parent.glob('*.partial.xlsx')))

    def test_descriptive_t_interval_and_missing_combinations(self):
        rows=[{'game_id':'1','date':'2026-10-29','pts':10,'reb':1,'ast':None},
              {'game_id':'2','date':'2026-10-30','pts':20,'reb':2,'ast':3}]
        points=summarize(rows,'pts')
        self.assertAlmostEqual(points['ci_high'],15+12.706204736*5,places=6)
        combined=summarize(rows,'pra')
        self.assertEqual((combined['n'],combined['missing'],combined['mean']),(1,1,25))


if __name__=='__main__':
    unittest.main()
