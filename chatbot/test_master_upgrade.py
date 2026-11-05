"""A workbook enrichment must be additive, recoverable and lossless."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from master import MasterStore
from master_upgrade import archive_workbook_sheets, merge_enriched_workbook
from pregame_archive import CANDIDATE_COLUMNS, RUN_COLUMNS, get_research, initialize_tables, record_research
from refresh_export import export_master

NOW = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)


class MasterUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.source = MasterStore(self.root / 'source.sqlite')
        self.live = MasterStore(self.root / 'live.sqlite')

    def tearDown(self):
        self.folder.cleanup()

    def observation(self, store, day='2026-10-27', pts=25):
        identity = 'nba:' + day + ':NYK:DET'
        store.upsert_game({'id': identity, 'date': day, 'status': 'final',
            'home_team': 'NYK', 'away_team': 'DET', 'home_score': 100,
            'away_score': 90, 'source': 'https://example.test/box'})
        store.upsert_player_game({'game_id': identity, 'player_id': 'name:jalenbrunson',
            'player': 'Jalen Brunson', 'team': 'NYK', 'opponent': 'DET', 'date': day,
            'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
            'pts': pts, 'reb': 3, 'ast': 5, 'minutes': 30, 'tpm': 2, 'fga': 19,
            'source': 'https://example.test/box'})
        return identity

    def book(self):
        return export_master(self.source, self.root / 'exports', NOW)

    def dump(self, store):
        with sqlite3.connect(store.path) as db:
            return '\n'.join(db.iterdump())

    def mutate(self, path, sheet, field, value, row_number=2):
        book = openpyxl.load_workbook(path)
        page = book[sheet]
        headers = [c.value for c in page[1]]
        page.cell(row_number, headers.index(field) + 1, value)
        book.save(path)
        book.close()

    def test_actual_export_merge_roundtrip_preserves_ledger_chat_settings_and_current_rows(self):
        identity = self.observation(self.source, pts=25)
        self.observation(self.source, '2026-10-29', pts=27)
        self.observation(self.live, pts=39)
        legs = [{'game_id': identity, 'player': 'Jalen Brunson', 'stat': 'pts', 'line': 24.5, 'side': 'more'},
                {'game_id': identity, 'player': 'Jalen Brunson', 'stat': 'ast', 'line': 4.5, 'side': 'more'}]
        board_id = self.live.create_board(legs)['id']
        entry_id = self.live.create_entry(legs, 5, 'power', 2)['id']
        self.live.grade_entries()
        self.live.set_checkpoint('nightly_nba_refresh', {'target_game_date': '2026-11-04', 'phase': 'updated'})
        self.live.set_checkpoint('chat:42', {'message': 'keep conversation'})
        with self.live.connect() as db:
            db.execute("INSERT INTO metadata VALUES ('my_setting','keep')")
            protected = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                         for table in ('boards', 'entries', 'grade_events', 'metadata', 'checkpoints')}
        path = self.book()
        report = merge_enriched_workbook(self.live, path)
        self.assertEqual(report['player_games_added'], 1)
        self.assertEqual(report['player_games_preserved'], 1)
        self.assertTrue(Path(report['backup']).exists())
        with sqlite3.connect(report['backup']) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 1)
        with self.live.connect() as db:
            self.assertEqual(db.execute('SELECT pts FROM player_games WHERE game_id=?', (identity,)).fetchone()[0], 39)
            for table, rows in protected.items():
                actual = [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                if table == 'checkpoints':
                    actual = [row for row in actual if row[0] != 'workbook_sheet_inventory']
                self.assertEqual(actual, rows, table)
        self.assertEqual(self.live.get_board(board_id)['id'], board_id)
        self.assertEqual(self.live.get_entry(entry_id)['id'], entry_id)
        before = self.dump(self.live)
        repeat = merge_enriched_workbook(self.live, path)
        self.assertTrue(repeat['skipped'])
        self.assertEqual(self.dump(self.live), before)

    def test_corrupt_input_and_invalid_last_row_leave_database_unchanged(self):
        self.observation(self.source)
        self.observation(self.source, '2026-10-29')
        self.observation(self.live, pts=39)
        before = self.dump(self.live)
        with self.assertRaises(ValueError):
            merge_enriched_workbook(self.live, b'not a zip')
        self.assertEqual(self.dump(self.live), before)
        path = self.book()
        self.mutate(path, 'player_games', 'pts', -3, row_number=3)
        with self.assertRaisesRegex(ValueError, 'pts'):
            merge_enriched_workbook(self.live, path)
        self.assertEqual(self.dump(self.live), before)

    def test_mid_import_failure_rolls_back_all_added_rows_and_metadata(self):
        self.observation(self.source)
        before = self.dump(self.live)
        with patch.object(self.live, '_snapshot', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                merge_enriched_workbook(self.live, self.book())
        self.assertEqual(self.dump(self.live), before)
        self.assertTrue(list((self.root / 'backups').glob('before-master-import-*.sqlite')))

    def test_backup_failure_does_not_mutate_live_database(self):
        self.observation(self.source)
        before = self.dump(self.live)
        with patch.object(self.live, 'backup', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                merge_enriched_workbook(self.live, self.book())
        self.assertEqual(self.dump(self.live), before)

    def test_unknown_tracking_coverage_and_newer_live_tracking_are_preserved(self):
        self.observation(self.source)
        old = {'entity': 'Jalen Brunson', 'category': 'Passing', 'season': '2026-27',
               'fields': {'potential_assists': 40}, 'published_at': '2026-10-31',
               'data_through_date': None}
        current = {**old, 'fields': {'potential_assists': 60}, 'published_at': '2026-11-05'}
        for store, row in ((self.source, old), (self.live, current)):
            with store.connect() as db:
                db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                    ('2026-27', 'Jalen Brunson', 'Passing', json.dumps(row)))
        report = merge_enriched_workbook(self.live, self.book())
        self.assertEqual(report['tracking_added'], 0)
        with self.live.connect() as db:
            saved = json.loads(db.execute('SELECT payload_json FROM refreshed_tracking').fetchone()[0])
        self.assertEqual(saved, current)
        self.assertIsNone(saved['data_through_date'])
        self.assertIsNone(self.live.get_checkpoint('tracking_refresh_status'))

    def test_collector_writing_during_parse_backup_window_keeps_its_newer_row(self):
        self.observation(self.source, pts=25)
        backup = self.live.backup
        def concurrent_backup(path):
            result = backup(path)
            self.observation(self.live, pts=44)
            return result
        with patch.object(self.live, 'backup', side_effect=concurrent_backup):
            report = merge_enriched_workbook(self.live, self.book())
        self.assertEqual(report['player_games_added'], 0)
        with self.live.connect() as db:
            self.assertEqual(db.execute('SELECT pts FROM player_games').fetchone()[0], 44)

    def test_invalid_tracking_identity_and_impossible_makes_do_not_import(self):
        self.observation(self.source)
        valid = {'entity': 'Jalen Brunson', 'category': 'Player totals', 'season': '2026-27',
                 'fields': {'fgm': 10, 'fga': 20}, 'gp': 1, 'data_through_date': None}
        before = self.dump(self.live)
        for payload in ({**valid, 'entity': 'Another player'},
                        {**valid, 'fields': {'fgm': 30, 'fga': 20}},
                        {**valid, 'fields': {'fgm': -1}}, {**valid, 'gp': 1.5}):
            with self.subTest(payload=payload), self.source.connect() as db:
                db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                           ('2026-27', 'Jalen Brunson', 'Player totals', json.dumps(payload)))
            with self.assertRaises(ValueError):
                merge_enriched_workbook(self.live, self.book())
            self.assertEqual(self.dump(self.live), before)

    def test_split_player_game_sheets_are_imported_and_duplicate_identity_is_rejected(self):
        self.observation(self.source)
        self.observation(self.source, '2026-10-29')
        path = self.book()
        book = openpyxl.load_workbook(path)
        main = book['player_games']
        split = book.create_sheet('player_games_10')
        split.append([cell.value for cell in main[1]])
        split.append([cell.value for cell in main[3]])
        main.delete_rows(3)
        book.save(path)
        book.close()
        report = merge_enriched_workbook(self.live, path)
        self.assertEqual(report['player_games_added'], 2)
        book = openpyxl.load_workbook(path)
        book['player_games'].append([cell.value for cell in book['player_games_10'][2]])
        book.save(path)
        book.close()
        before = self.dump(self.live)
        with self.assertRaisesRegex(ValueError, 'duplicate player-game'):
            merge_enriched_workbook(self.live, path)
        self.assertEqual(self.dump(self.live), before)

    def test_conflicting_existing_game_identity_rolls_back_whole_import(self):
        identity = self.observation(self.source)
        self.observation(self.source, '2026-10-29')
        self.live.upsert_game({'id': identity, 'date': '2026-10-27', 'status': 'final',
            'home_team': 'NYK', 'away_team': 'CHI', 'source': 'live'})
        before = self.dump(self.live)
        with self.assertRaisesRegex(ValueError, 'existing live game identity'):
            merge_enriched_workbook(self.live, self.book())
        self.assertEqual(self.dump(self.live), before)

    def test_advanced_evidence_original_tabs_and_lossless_long_research_survive_roundtrip(self):
        identity = self.observation(self.source)
        self.source.set_checkpoint('workbook_sheet_inventory', [{'sheet': 'Jalen Brunson'}, {'sheet': 'vs DET'}])
        with self.source.connect() as db:
            db.execute('CREATE TABLE workbook_evidence (kind TEXT,entity TEXT,payload_json TEXT)')
            for kind, value in [('assignments', {'assignment_id': 'a', 'selected_player': 'Jalen Brunson',
                'opponent_team': 'DET', 'primary': 'Old tentative name', 'assignment_asof': '2026-10-22'}),
                ('measured_pairs', {'game_id': identity, 'date': '2026-10-27', 'season': '2026-27',
                 'season_type': 'Regular Season', 'offense_player': 'Jalen Brunson',
                 'defense_player': 'Actual measured defender', 'fgm': 1, 'fga': 2})]:
                db.execute('INSERT INTO workbook_evidence VALUES (?,?,?)', (kind, 'Jalen Brunson', json.dumps(value)))
        reference = self.root / 'original.xlsx'
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Legacy_Reference'
        sheet.append(['Original dated report'])
        sheet.append(['=unsafe formula text'])
        sheet.append(['2026-10-22', 42])
        book.save(reference)
        book.close()
        archive_workbook_sheets(self.source, reference)
        report = {'game': {'id': identity}, 'evaluated_candidates': [{'player': 'Jalen Brunson',
            'game_id': identity, 'stat': 'pts', 'line': 24.5, 'side': 'more', 'status': 'pass',
            'reason': 'unverified', 'projection': {'method': 'test', 'evidence': '長い🟢=+-@' * 20000}}]}
        run_id = record_research(self.source, report, owner_key='telegram:42', kind='independent', now=NOW)
        path = self.book()
        book = openpyxl.load_workbook(path, read_only=True)
        self.assertIn('Workbook_Text_Chunks', book.sheetnames)
        self.assertIn('Legacy_Reference', book.sheetnames)
        book.close()
        result = merge_enriched_workbook(self.live, path)
        self.assertEqual(result['evidence_added'], 2)
        self.assertEqual(result['research_runs_added'], 1)
        self.assertEqual(get_research(self.source, run_id, owner_key='telegram:42'),
                         get_research(self.live, run_id, owner_key='telegram:42'))
        second = export_master(self.live, self.root / 'second', NOW)
        book = openpyxl.load_workbook(second, read_only=True)
        self.assertIn('Legacy_Reference', book.sheetnames)
        self.assertIn('Measured_Pair_Metrics', book.sheetnames)
        self.assertIn('Matchup_Assignments', book.sheetnames)
        self.assertIn('Historical_Coverage', book.sheetnames)
        self.assertIn('Current_Missing_Data', book.sheetnames)
        rows = list(book['Matchup_Assignments'].values)
        headers = rows[0]
        self.assertEqual(rows[1][headers.index('assignment_asof')], '2026-10-22')
        book.close()

    def test_incomplete_long_text_chunks_rejected_before_import(self):
        self.observation(self.source)
        record_research(self.source, {'evaluated_candidates': [], 'large': 'x' * 70000},
                        owner_key='telegram:42', kind='independent', now=NOW)
        path = self.book()
        book = openpyxl.load_workbook(path)
        book['Workbook_Text_Chunks'].delete_rows(2)
        book.save(path)
        book.close()
        before = self.dump(self.live)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            merge_enriched_workbook(self.live, path)
        self.assertEqual(self.dump(self.live), before)

    def test_empty_prospective_archives_export_real_columns_without_fake_history(self):
        initialize_tables(self.source)
        book = openpyxl.load_workbook(self.book(), read_only=True)
        for sheet, columns in [('Research_Runs', RUN_COLUMNS), ('Research_Candidates', CANDIDATE_COLUMNS)]:
            rows = list(book[sheet].values)
            self.assertEqual(rows, [columns])
        book.close()


if __name__ == '__main__':
    unittest.main()
