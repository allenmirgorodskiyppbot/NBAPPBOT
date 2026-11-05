"""Export actual guarding history and source audits without making freshness claims."""
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest

import openpyxl

from master import MasterStore
from master_upgrade import _parse
from measured_history import _schema
from refresh_export import build_export_tables, export_master
from test_measured_history import observation

NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)


class MeasuredHistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MasterStore(self.root/'master.sqlite')
        with self.store.connect() as db:
            _schema(db)
            db.execute('CREATE TABLE workbook_evidence(kind TEXT,entity TEXT,payload_json TEXT)')

    def tearDown(self):
        self.tmp.cleanup()

    def imported(self, row, status='verified', alternative=None):
        with self.store.connect() as db:
            db.execute('INSERT INTO measured_pair_history VALUES(?,?,?,?)',
                (row['pair_observation_id'], json.dumps(row), status,
                 json.dumps(alternative) if alternative else None))

    def evidence(self, row):
        with self.store.connect() as db:
            db.execute('INSERT INTO workbook_evidence VALUES(?,?,?)',
                       ('measured_pairs', row['offense_player'], json.dumps(row)))

    def test_legacy_and_new_history_overlap_does_not_double_game_or_shooting_counts(self):
        first = observation()
        legacy = dict(first)
        legacy.pop('offense_nba_player_id'); legacy.pop('defense_nba_player_id')
        legacy['game_id'] = 'nba:2026-10-20:PHI:NYK'
        self.evidence(legacy)
        self.imported(first)
        self.imported(observation('0022600002','2026-10-21',made=1,attempts=2))
        tables = build_export_tables(self.store, NOW)
        row = next(r for r in tables['Measured_Pair_Metrics'] if r['metric']=='fg_pct')
        self.assertEqual((row['covered_games'],row['numerator'],row['denominator']), (2,3,6))
        self.assertEqual(row['value'],.5)
        self.assertEqual(len(tables['Measured_Pair_Evidence']),2)
        self.assertEqual(row['last_game'],'2026-10-21')
        self.assertIn(first['source'],row['source_urls'])
        self.assertIn(first['source_content_sha256'],row['source_content_sha256s'])

    def test_legacy_only_rows_are_not_duplicated_by_extra_evidence_read(self):
        row = observation(); row.pop('offense_nba_player_id'); row.pop('defense_nba_player_id')
        row['game_id']='old_game';self.evidence(row)
        tables=build_export_tables(self.store,NOW)
        metric=next(r for r in tables['Measured_Pair_Metrics'] if r['metric']=='fga')
        self.assertEqual(metric['value'],4)
        self.assertEqual(metric['observed_records'],1)
        self.assertEqual(len(tables['Measured_Pair_Evidence']),1)

    def test_conflicted_source_retains_raw_alternative_but_withholds_derived_metrics(self):
        row=observation(); alternative=observation(made=3)
        self.imported(row,'conflict',alternative)
        tables=build_export_tables(self.store,NOW)
        metrics=tables['Measured_Pair_Metrics']
        self.assertTrue(all(r['value'] is None for r in metrics))
        self.assertTrue(all(r['status']=='held_conflicting_measurements' for r in metrics))
        self.assertEqual(metrics[0]['conflicting_records'],1)
        audit=tables['Measured_Pair_History'][0]
        self.assertEqual(json.loads(audit['conflict_json'])['fgm'],3)
        self.assertEqual(json.loads(audit['payload_json'])['fgm'],2)

    def test_conflicting_legacy_duplicates_are_held_and_extended_fields_are_exported(self):
        row=observation();row.pop('offense_nba_player_id');row.pop('defense_nba_player_id');row['game_id']='legacy'
        self.evidence(row);self.evidence({**row,'fgm':3})
        tables=build_export_tables(self.store,NOW)
        self.assertTrue(all(r['value'] is None for r in tables['Measured_Pair_Metrics']))
        self.assertIn('potential_assists',{r['metric'] for r in tables['Measured_Pair_Metrics']})
        self.assertIn('help_fga',{r['metric'] for r in tables['Measured_Pair_Metrics']})

    def test_publication_date_never_becomes_last_game_and_phases_remain_separate(self):
        self.imported(observation())
        self.imported(observation('0042500001','2026-05-02','Playoffs',made=1,attempts=1))
        tables=build_export_tables(self.store,NOW)
        rates=[r for r in tables['Measured_Pair_Metrics'] if r['metric']=='fg_pct']
        self.assertEqual({(r['season_type'],r['value']) for r in rates}, {('Regular Season',.5),('Playoffs',1)})
        self.assertEqual({r['last_game'] for r in rates},{'2026-10-20','2026-05-02'})
        self.assertTrue(all('2026-11-04' in r['source_published_at'] for r in rates))

    def test_future_actual_rows_are_excluded_from_both_evidence_and_metrics(self):
        self.imported(observation('0022600009','2026-12-01'))
        tables=build_export_tables(self.store,NOW)
        self.assertEqual(tables['Measured_Pair_Evidence'],[])
        self.assertEqual(tables['Measured_Pair_Metrics'],[])

    def test_availability_and_completion_audits_keep_sources_and_do_not_certify_readiness(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE structured_availability(record_id TEXT,report_at TEXT,payload_json TEXT)')
            db.execute('INSERT INTO structured_availability VALUES(?,?,?)',
                ('injury1','2026-11-05T15:00:00Z',json.dumps({'status':'Out','source':'official-report','game_date':'2026-11-05'})))
            db.execute('CREATE TABLE player_stat_completions(id INTEGER,source TEXT,old_value_json TEXT,new_value_json TEXT,source_evidence_json TEXT)')
            db.execute('INSERT INTO player_stat_completions VALUES(?,?,?,?,?)',
                (1,'NBA boxscore','null','5',json.dumps({'source_content_sha256':'a'*64})))
        tables=build_export_tables(self.store,NOW)
        self.assertEqual(tables['Structured_Availability'][0]['report_at'],'2026-11-05T15:00:00Z')
        audit=tables['Player_Stat_Completions'][0]
        self.assertEqual((audit['old_value_json'],audit['new_value_json']),('null','5'))
        self.assertIn('does not independently verify',audit['export_policy'])
        self.assertIn('a'*64,audit['source_evidence_json'])

    def test_export_does_not_write_database_and_generated_audits_are_not_operational_imports(self):
        self.imported(observation())
        with self.store.connect() as db:
            db.execute('CREATE TABLE structured_availability(record_id TEXT,payload_json TEXT)')
            db.execute('CREATE TABLE player_stat_completions(id INTEGER,source TEXT)')
            before='\n'.join(db.iterdump())
        target=export_master(self.store,self.root/'export',NOW)
        with self.store.connect() as db:self.assertEqual('\n'.join(db.iterdump()),before)
        book=openpyxl.load_workbook(target,read_only=True)
        self.assertIn('Structured_Availability',book.sheetnames)
        self.assertIn('Player_Stat_Completions',book.sheetnames)
        self.assertEqual(next(book['Structured_Availability'].iter_rows(values_only=True)),('record_id','payload_json'))
        book.close()
        parsed,archives,_=_parse(target.read_bytes())
        for name in ['Measured_Pair_History','Measured_Pair_Evidence','Structured_Availability','Player_Stat_Completions']:
            self.assertNotIn(name,parsed)
            self.assertNotIn(name,archives)


if __name__=='__main__':unittest.main()
