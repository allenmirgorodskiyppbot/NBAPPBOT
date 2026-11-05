"""Durable pregame capture, immutable evidence, scope isolation and honest gaps."""
import copy
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from master import MasterStore
from pregame_archive import get_research, initialize_tables, record_research

UTC = dt.timezone.utc
CAPTURE = dt.datetime(2026, 11, 5, 22, 0, tzinfo=UTC)


class PregameArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.event = {'id': 'nba:2026-11-05:CHI:NYK', 'date': '2026-11-05',
                      'home_team': 'NYK', 'away_team': 'CHI', 'status': 'scheduled',
                      'start_time': '2026-11-06T00:30:00+00:00', 'source': 'saved-calendar'}
        self.store.upsert_game(self.event)
        self.leg = {'player': 'Jalen Brunson', 'player_id': 'nba:1628973',
                    'game_id': self.event['id'], 'date': '2026-11-05', 'team': 'NYK',
                    'stat': 'pts', 'line': 25.5, 'side': 'more',
                    'screenshot_unconfirmed': True, 'offer_variant': 'standard'}
        self.projection = {'available': True, 'method': 'same-team-mean20-v1',
                           'reference_projection': 28.1, 'projected_minutes': 34.0,
                           'tentative_pairing': {'defender': 'Example', 'verified': False},
                           'source_games': [{'game_id': 'old-game', 'date': '2026-11-03',
                                             'value': 30, 'source': 'fixture'}]}
        self.board = self.store.create_board([self.leg], source='screenshot:100:example')
        self.source = {'id': 'engine-report', 'board_id': self.board['id'],
                       'generated_at': '2026-11-05T21:59:00+00:00',
                       'engine_version': 'same-team-mean20-v1',
                       'original_legs': [self.leg],
                       'legs': [{'leg': self.leg, 'eligible_for_ranking': True,
                                 'projection': self.projection, 'reason': 'Qualified'}]}
        self.selection = {'board_id': self.board['id'],
                          'generated_at': '2026-11-05T22:00:00+00:00',
                          'shortlist': [{'leg': self.leg, 'projection': self.projection,
                                        'availability': {'blocked': False, 'note': None},
                                        'reason': 'Research comparison'}], 'skipped': []}

    def tearDown(self):
        self.directory.cleanup()

    def save(self, **extra):
        args = dict(owner_key='telegram:100', kind='board_shortlist',
                    source_report=self.source, now=CAPTURE, source_ref='tested-source-sha')
        args.update(extra)
        return record_research(self.store, self.selection, **args)

    def read(self, run_id, owner='telegram:100'):
        return get_research(self.store, run_id, owner_key=owner)

    def test_round_trip_preserves_exact_offer_sources_and_unknown_fields(self):
        run_id = self.save()
        row = self.read(run_id)
        self.assertEqual(row['payload']['source_report'], self.source)
        self.assertEqual(row['payload']['board_snapshot']['legs'], [self.leg])
        self.assertEqual(row['offer_kind'], 'user_supplied_offers_unverified')
        self.assertIsNone(row['payload']['payout_terms'])
        self.assertEqual(row['model_versions'], ['same-team-mean20-v1'])
        candidate = row['candidates'][0]
        self.assertEqual((candidate['disposition'], candidate['line'], candidate['reference_projection']),
                         ('selected', 25.5, 28.1))
        saved = candidate['payload']['candidate']
        self.assertTrue(saved['leg']['screenshot_unconfirmed'])
        self.assertFalse(saved['projection']['tentative_pairing']['verified'])
        self.assertEqual(saved['availability'], {'blocked': False, 'note': None})
        self.assertEqual(self.store.list_entries(), [])

    def test_repeat_is_idempotent_and_retains_first_capture(self):
        first = self.save()
        again = self.save(now=CAPTURE + dt.timedelta(hours=5))
        self.assertEqual(first, again)
        self.assertEqual(self.read(first)['captured_at'], CAPTURE.isoformat())
        self.assertEqual(self.read(first)['timing_status'], 'before_saved_tipoff')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_runs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_candidates').fetchone()[0], 1)

    def test_late_capture_does_not_backdate_using_report_generation(self):
        late = self.save(now=CAPTURE + dt.timedelta(hours=4))
        row = self.read(late)
        self.assertEqual(row['generated_at'], self.selection['generated_at'])
        self.assertEqual(row['timing_status'], 'at_or_after_saved_tipoff')
        self.assertEqual(row['candidates'][0]['timing_status'], 'at_or_after_saved_tipoff')

    def test_changed_prediction_appends_and_input_mutation_cannot_change_history(self):
        original = self.save()
        self.projection['reference_projection'] = 99
        self.source['legs'][0]['projection']['source_games'][0]['value'] = 99
        changed = self.save()
        self.assertNotEqual(original, changed)
        self.assertEqual(self.read(original)['candidates'][0]['reference_projection'], 28.1)
        self.assertEqual(self.read(changed)['candidates'][0]['reference_projection'], 99)
        saved_value = self.read(original)['payload']['source_report']['legs'][0]['projection']['source_games'][0]['value']
        self.assertEqual(saved_value, 30)

    def test_schema_blocks_rewriting_or_deleting_captured_evidence(self):
        run_id = self.save()
        for table in ('research_runs', 'research_candidates'):
            for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET payload_json=\'{{}}\''):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    with self.store.connect() as db:
                        db.execute(sql)
        initialize_tables(self.store)
        self.assertEqual(len(self.read(run_id)['candidates']), 1)

    def test_scope_isolation_and_safe_missing_id(self):
        first = self.save()
        second = self.save(owner_key='telegram:200')
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.read(first, 'telegram:200')
        with self.assertRaisesRegex(ValueError, 'owner key'):
            self.read(first, '')
        self.assertEqual(self.read(second, 'telegram:200')['owner_key'], 'telegram:200')

    def test_all_board_legs_including_passes_and_unselected_survive(self):
        denied = copy.deepcopy(self.source['legs'][0])
        denied['leg'] = {**self.leg, 'stat': 'reb', 'line': 4.5}
        denied['projection'] = {'available': False, 'reason': 'Only seven observations'}
        denied['eligible_for_ranking'] = False
        unselected = copy.deepcopy(self.source['legs'][0])
        unselected['leg'] = {**self.leg, 'stat': 'ast', 'line': 6.5}
        self.source['legs'] += [denied, unselected]
        self.selection['skipped'] = [{**denied['leg'], 'reason': 'Only seven observations'}]
        row = self.read(self.save())
        self.assertEqual([item['disposition'] for item in row['candidates']],
                         ['selected', 'pass', 'candidate_not_selected'])
        self.assertEqual(row['candidates'][1]['reason'], 'Only seven observations')
        self.assertIsNone(row['candidates'][1]['reference_projection'])

    def test_independent_targets_never_become_actual_offers_or_payouts(self):
        report = {'generated_at': CAPTURE.isoformat(), 'game': self.event,
                  'game_date': self.event['date'], 'picks': [],
                  'evaluated_candidates': [
                      {'player': 'Jalen Brunson', 'team': 'NYK', 'stat': 'pts',
                       'projection': self.projection, 'status': 'candidate',
                       'targets': {'more_at_or_below': 24.5}},
                      {'player': 'Josh Giddey', 'team': 'CHI', 'stat': 'ast',
                       'status': 'pass', 'reason': 'Role not verified',
                       'availability': {'note': 'Questionable', 'blocked': True}}]}
        run_id = record_research(self.store, report, owner_key='telegram:100', kind='independent', now=CAPTURE)
        row = self.read(run_id)
        self.assertEqual(row['offer_kind'], 'conditional_targets_not_offers')
        self.assertEqual(row['payload']['candidate_coverage'], 'all_evaluated_markets')
        self.assertEqual([item['line'] for item in row['candidates']], [None, None])
        self.assertEqual([item['side'] for item in row['candidates']], [None, None])
        self.assertEqual(row['candidates'][1]['disposition'], 'pass')
        self.assertIsNone(row['payload']['payout_terms'])

    def test_actual_supplied_payout_terms_are_captured_without_inferring_rules(self):
        terms = {'entry_type': 'power', 'multiplier': 7.25, 'stake': 40,
                 'source': 'user entry receipt', 'verified': False}
        row = self.read(self.save(payout_terms=terms))
        self.assertEqual(row['payload']['payout_terms'], terms)
        self.assertEqual(self.store.list_entries(), [])

    def test_missing_timing_does_not_become_pregame_verified(self):
        self.store.upsert_game({'id': self.event['id'], 'start_time': None})
        row = self.read(self.save())
        self.assertEqual(row['timing_status'], 'timing_unknown')
        self.assertIsNone(row['candidates'][0]['tipoff'])

    def test_invalid_or_oversized_payload_does_not_create_partial_archive(self):
        initialize_tables(self.store)
        with self.assertRaises(ValueError):
            self.save(now=dt.datetime(2026, 11, 5, 22))
        self.selection['invalid'] = float('nan')
        with self.assertRaises(ValueError):
            self.save()
        del self.selection['invalid']
        with patch('pregame_archive.MAX_PAYLOAD_BYTES', 10), self.assertRaises(ValueError):
            self.save()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_runs').fetchone()[0], 0)

    def test_candidate_insert_failure_rolls_back_entire_run(self):
        initialize_tables(self.store)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_candidate BEFORE INSERT ON research_candidates "
                       "BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM research_runs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
