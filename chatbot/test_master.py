"""Meaningful persistence, correction and platform-settlement boundary tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from master import MasterStore


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / 'master.sqlite')
        self.store = MasterStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def game(self, **changes):
        row = {'id': 'nba:g1', 'date': '2026-10-27', 'status': 'final',
               'home_team': 'NYK', 'away_team': 'DET', 'home_score': 111,
               'away_score': 97, 'source': 'fixture'}
        row.update(changes)
        return row

    def stat(self, **changes):
        row = {'game_id': 'nba:g1', 'player_id': 'nba:p1', 'player': 'Jalen Brunson',
               'team': 'NYK', 'opponent': 'DET', 'date': '2026-10-27',
               'season': '2026-27', 'season_type': 'Regular Season',
               'appearance_state': 'Played', 'pts': 25, 'reb': 3, 'ast': 5,
               'minutes': 35.5, 'source': 'fixture'}
        row.update(changes)
        return row

    def legs(self, **changes):
        leg = {'player': 'Jalen Brunson', 'player_id': 'nba:p1', 'game_id': 'nba:g1',
               'stat': 'pts', 'line': 24.5, 'side': 'more'}
        leg.update(changes)
        return [leg, {'player': 'Jalen Brunson', 'player_id': 'nba:p1', 'game_id': 'nba:g1',
                      'stat': 'ast', 'line': 4.5, 'side': 'more'}]

    def entry(self, **changes):
        values = {'legs': self.legs(), 'stake': 10, 'entry_type': 'power', 'payout_multiplier': 3}
        values.update(changes)
        return self.store.create_entry(**values)

    def test_restarts_idempotency_and_source_corrections(self):
        self.assertTrue(self.store.upsert_game(self.game()))
        self.assertTrue(self.store.upsert_player_game(self.stat()))
        self.assertFalse(self.store.upsert_game(self.game()))
        self.assertFalse(self.store.upsert_player_game(self.stat()))
        reopened = MasterStore(self.path)
        self.assertEqual(reopened.status()['counts']['games'], 1)
        self.assertEqual(reopened.query('SELECT pts FROM player_games')['rows'], [(25.0,)])
        self.assertTrue(reopened.upsert_player_game({'game_id': 'nba:g1', 'player_id': 'nba:p1', 'pts': 24}))
        self.assertEqual(reopened.query('SELECT pts,reb FROM player_games')['rows'], [(24.0, 3.0)])
        with reopened.connect() as db:
            snapshots = db.execute("SELECT COUNT(*) FROM raw_snapshots WHERE kind='player_games'").fetchone()[0]
        self.assertEqual(snapshots, 2)

    def test_game_bundle_rolls_back_invalid_rows_and_raw_snapshots(self):
        with self.assertRaises(ValueError):
            self.store.upsert_game_bundle(self.game(), [self.stat(),
                self.stat(player_id='nba:p2', pts=float('nan'))])
        self.assertEqual(self.store.status()['counts']['games'], 0)
        self.assertEqual(self.store.status()['counts']['player_games'], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_snapshots').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            self.store.upsert_game_bundle(self.game(), [self.stat(),
                self.stat(game_id='wrong-game', player_id='nba:p2')])
        self.assertEqual(self.store.status()['counts']['player_games'], 0)

    def test_final_game_bundle_publishes_player_rows_before_final_status(self):
        original_upsert = self.store._upsert
        operations = []

        def inspect(table, fields, keys, payload, db=None):
            changed = original_upsert(table, fields, keys, payload, db=db)
            operations.append(table)
            if table == 'games':
                # The writer itself already has every new player before the
                # final marker is written, although no reader can yet see it.
                self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT status FROM games').fetchone()[0], 'final')
                with sqlite3.connect(self.path) as reader:
                    self.assertEqual(reader.execute('SELECT COUNT(*) FROM games').fetchone()[0], 0)
                    self.assertEqual(reader.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)
            return changed

        with patch.object(self.store, '_upsert', side_effect=inspect):
            result = self.store.upsert_game_bundle(self.game(), [self.stat(), self.stat(player_id='nba:p2')])
        self.assertEqual(operations, ['player_games', 'player_games', 'games'])
        self.assertEqual(result, {'game_changed': True, 'player_rows': 2, 'player_rows_changed': 2})
        self.assertEqual(self.store.status()['counts']['player_games'], 2)
        self.assertFalse(self.store.upsert_game_bundle(self.game(),
                         [self.stat(), self.stat(player_id='nba:p2')])['game_changed'])

    def test_corrected_bundle_is_never_visible_as_mixed_player_versions(self):
        self.store.upsert_game_bundle(self.game(), [self.stat(pts=10),
            self.stat(player_id='nba:p2', pts=20)])
        original_upsert = self.store._upsert
        observations = []

        def inspect(table, fields, keys, payload, db=None):
            changed = original_upsert(table, fields, keys, payload, db=db)
            with sqlite3.connect(self.path) as reader:
                observations.append(reader.execute(
                    'SELECT player_id,pts FROM player_games ORDER BY player_id').fetchall())
                self.assertEqual(reader.execute('SELECT home_score FROM games').fetchone()[0], 111)
            return changed

        with patch.object(self.store, '_upsert', side_effect=inspect):
            self.store.upsert_game_bundle(self.game(home_score=112), [self.stat(pts=25),
                self.stat(player_id='nba:p2', pts=30)])
        self.assertEqual(observations, [[('nba:p1', 10), ('nba:p2', 20)]] * 3)
        self.assertEqual(self.store.query('SELECT pts FROM player_games ORDER BY player_id')['rows'], [(25,), (30,)])
        self.assertEqual(self.store.games('2026-10-27')[0]['home_score'], 112)
        with self.assertRaises(ValueError):
            self.store.upsert_game_bundle(self.game(home_score=113), [self.stat(pts=100),
                self.stat(player_id='nba:p2', reb=-1)])
        self.assertEqual(self.store.query('SELECT pts FROM player_games ORDER BY player_id')['rows'], [(25,), (30,)])
        self.assertEqual(self.store.games('2026-10-27')[0]['home_score'], 112)

    def test_grading_holds_one_snapshot_against_concurrent_publish(self):
        self.store.upsert_game_bundle(self.game(), [self.stat()])
        entry = self.entry()
        original_grade = self.store._grade_leg
        attempted_writes = []

        def inspect(db, leg):
            # A second writer cannot publish between the grader's separate
            # SELECTs; WAL readers remain available throughout this transaction.
            with sqlite3.connect(self.path, timeout=0.01) as concurrent:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    concurrent.execute('UPDATE player_games SET pts=0')
            attempted_writes.append(True)
            return original_grade(db, leg)

        with patch.object(self.store, '_grade_leg', side_effect=inspect):
            self.store.grade_entries()
        self.assertEqual(len(attempted_writes), 2)
        self.assertEqual(self.store.get_entry(entry['id'])['provisional_status'], 'won')
        self.store.upsert_game_bundle(self.game(), [self.stat(pts=0)])
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(entry['id'])['provisional_status'], 'lost')

    def test_schema_one_migrates_and_preserves_tipoff_across_partial_updates(self):
        old_path = str(Path(self.directory.name) / 'legacy.sqlite')
        with sqlite3.connect(old_path) as db:
            db.execute('CREATE TABLE games (id TEXT PRIMARY KEY,date TEXT,status TEXT,'
                       'home_team TEXT,away_team TEXT,home_score REAL,away_score REAL,'
                       'source TEXT,updated_at TEXT NOT NULL)')
            db.execute('INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)',
                       ('nba:g1', '2026-10-27', 'scheduled', 'NYK', 'DET', None,
                        None, 'fixture', '2026-10-27T12:00:00Z'))
        migrated = MasterStore(old_path)
        self.assertEqual(migrated.status()['schema_version'], 2)
        self.assertEqual(len(migrated.games('2026-10-27')), 1)
        migrated.upsert_game({'id': 'nba:g1', 'start_time': '2026-10-28T00:00:00Z',
                              'provider_game_id': 'provider:1'})
        migrated.upsert_game({'id': 'nba:g1', 'status': 'final', 'home_score': 111})
        restarted = MasterStore(old_path)
        game = restarted.games('2026-10-27')[0]
        self.assertEqual(game['start_time'], '2026-10-28T00:00:00Z')
        self.assertEqual(game['provider_game_id'], 'provider:1')
        self.assertEqual(game['home_team'], 'NYK')
        restarted.upsert_game({'id': 'nba:g1', 'start_time': None})
        self.assertIsNone(restarted.games('2026-10-27')[0]['start_time'])

    def test_sql_is_read_only_and_excludes_raw_credentials_and_checkpoints(self):
        self.store.set_checkpoint('offset', 5)
        for sql in ['DELETE FROM games', 'DROP TABLE games', 'PRAGMA journal_mode',
                    'SELECT * FROM checkpoints', 'SELECT * FROM raw_snapshots',
                    "ATTACH DATABASE '/tmp/other.sqlite' AS other", "SELECT load_extension('x')"]:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self.store.query(sql)
        self.assertEqual(self.store.get_checkpoint('offset'), 5)

    def test_news_and_feed_checkpoints_are_durable_and_deduplicated(self):
        news = {'id': 'n1', 'title': 'Brunson available', 'url': 'https://example.com/1',
                'source': 'fixture', 'published_at': '2026-10-27T20:00:00Z'}
        self.assertTrue(self.store.record_news(news))
        self.assertFalse(self.store.record_news({**news, 'observed_at': 'later'}))
        self.assertTrue(self.store.record_news({**news, 'title': 'Brunson ruled out'}))
        self.store.feed_success('games', 'Imported 12 games')
        self.store.feed_failure('games', 'Upstream timed out')
        self.store.set_checkpoint('offset', {'id': 22})
        reopened = MasterStore(self.path)
        feed = reopened.status()['feeds'][0]
        self.assertIsNotNone(feed['last_success'])
        self.assertIsNotNone(feed['last_error'])
        self.assertEqual(reopened.get_checkpoint('offset'), {'id': 22})
        self.assertEqual(len(reopened.recent_news()), 1)
        reopened.feed_success('games', 'Recovered')
        self.assertIsNone(reopened.status()['feeds'][0]['error'])

    def test_original_picks_are_immutable_and_corrections_add_grade_events(self):
        self.store.upsert_game(self.game())
        self.store.upsert_player_game(self.stat())
        original = self.entry()
        first = self.store.grade_entries()[0]
        self.assertEqual(first['provisional_status'], 'won')
        self.assertEqual(first['expected_payout'], 30)
        self.assertIsNone(first['actual_payout'])
        self.assertEqual(self.store.grade_entries(), [])
        self.store.upsert_player_game(self.stat(pts=24))
        corrected = self.store.grade_entries()[0]
        self.assertEqual(corrected['provisional_status'], 'lost')
        self.assertEqual(corrected['legs'], original['legs'])
        with self.store.connect() as db:
            events = db.execute('SELECT kind FROM grade_events WHERE entry_id=? ORDER BY id',
                                (original['id'],)).fetchall()
        self.assertEqual([r[0] for r in events], ['created', 'provisional_grade', 'provisional_grade'])

    def test_pending_tie_dnp_missing_and_reboot_are_not_miscounted(self):
        entry = self.entry()
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(entry['id'])['provisional_status'], 'pending')
        self.store.upsert_game(self.game(status='in_progress'))
        self.store.upsert_player_game(self.stat())
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(entry['id'])['provisional_status'], 'pending')
        self.store.upsert_game(self.game())
        for changes, expected in [({'pts': 24.5}, 'tie'), ({'pts': None}, 'review'),
                                  ({'appearance_state': 'DNP'}, 'dnp')]:
            self.store.upsert_player_game(self.stat(**changes))
            self.store.grade_entries()
            result = self.store.get_entry(entry['id'])
            self.assertEqual(result['grades'][0]['status'], expected)
            self.assertEqual(result['provisional_status'], 'review')
        self.store.upsert_player_game(self.stat())
        reboot = self.entry(legs=self.legs(reboot=True))
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(reboot['id'])['grades'][0]['status'], 'review')
        less = self.entry(legs=self.legs(reboot=True, side='less', line=30))
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(less['id'])['grades'][0]['status'], 'won')

    def test_flex_partial_and_unmatched_ambiguous_name_require_review(self):
        self.store.upsert_game(self.game())
        self.store.upsert_player_game(self.stat())
        partial = self.entry(legs=self.legs(line=30), entry_type='flex', payout_multiplier=3)
        self.store.grade_entries()
        result = self.store.get_entry(partial['id'])
        self.assertEqual(result['provisional_status'], 'partial')
        self.assertIsNone(result['expected_payout'])
        self.store.upsert_player_game(self.stat(player_id='other:p1'))
        legs = self.legs()
        for leg in legs:
            del leg['player_id']
        ambiguous = self.entry(legs=legs)
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(ambiguous['id'])['grades'][0]['status'], 'review')

    def test_cancelled_or_postponed_game_without_player_rows_requires_review(self):
        entry = self.entry()
        self.store.upsert_game(self.game(status='postponed'))
        self.store.grade_entries()
        result = self.store.get_entry(entry['id'])
        self.assertEqual(result['provisional_status'], 'review')
        self.assertEqual(result['grades'][0]['status'], 'review')
        self.assertIn('Postponed', result['grades'][0]['reason'])
        legs = self.legs()
        for leg in legs:
            del leg['game_id']
            leg.update(date='2026-10-27', team='NYK')
        date_matched = self.entry(legs=legs)
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(date_matched['id'])['grades'][0]['status'], 'review')

    def test_market_period_alias_never_uses_full_game_statistics(self):
        self.store.upsert_game(self.game())
        self.store.upsert_player_game(self.stat())
        for key, value in [('period', '1H'), ('market_period', '1H'), ('market_period', '4Q')]:
            with self.subTest(key=key, value=value):
                entry = self.entry(legs=self.legs(**{key: value}))
                self.store.grade_entries()
                grade = self.store.get_entry(entry['id'])['grades'][0]
                self.assertEqual(grade['status'], 'review')
                self.assertNotIn('actual', grade)
        full_game = self.entry(legs=self.legs(market_period='full-game'))
        self.store.grade_entries()
        self.assertEqual(self.store.get_entry(full_game['id'])['grades'][0]['status'], 'won')

    def test_board_does_not_count_as_placed_entry_and_cash_returns_are_not_profit(self):
        board = self.store.create_board(self.legs())
        self.assertEqual(self.store.get_board(board['id'])['legs'][0]['stat'], 'pts')
        self.assertEqual(self.store.ledger_summary()['entries'], 0)
        entries = [self.entry() for _ in range(4)]
        self.store.mark_settlement(entries[0]['id'], 30, 'Uploaded settled PrizePicks slip')
        self.store.mark_settlement(entries[1]['id'], 0, 'Lost')
        self.store.mark_settlement(entries[2]['id'], 10, 'Refund')
        result = self.store.ledger_summary()
        self.assertEqual(result['settled_entries'], 3)
        self.assertEqual(result['unsettled_entries'], 1)
        self.assertEqual(result['settled_wins'], 1)
        self.assertEqual(result['settled_losses'], 1)
        self.assertEqual(result['settled_break_even'], 1)
        self.assertEqual(result['settled_stake'], 30)
        self.assertEqual(result['total_returned'], 40)
        self.assertEqual(result['net_profit'], 10)
        self.assertAlmostEqual(result['roi_percent'], 33.33)
        restarted = MasterStore(self.path)
        self.assertEqual(restarted.ledger_summary(), result)

    def test_validation_rejects_ambiguous_lines_and_invalid_cash(self):
        for bad in [{'line': float('nan')}, {'line': -1}, {'side': 'either'},
                    {'stat': 'unsupported'}, {'date': 'not-a-date'}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.create_board(self.legs(**bad))
        with self.assertRaises(ValueError):
            self.entry(stake=-10)
        with self.assertRaises(ValueError):
            self.entry(payout_multiplier=float('inf'))
        with self.assertRaises(ValueError):
            self.entry(legs=[self.legs()[0]])

    def test_consistent_backups_include_wal_and_confirmed_ledger(self):
        self.store.upsert_game(self.game())
        self.store.upsert_player_game(self.stat())
        entry = self.entry()
        self.store.mark_settlement(entry['id'], 30, 'Confirmed slip')
        self.store.set_checkpoint('telegram', 100)
        destination = str(Path(self.directory.name) / 'backups' / 'daily.sqlite')
        self.store.backup(destination)
        copy = MasterStore(destination)
        self.assertEqual(copy.ledger_summary(), self.store.ledger_summary())
        self.assertEqual(copy.get_checkpoint('telegram'), 100)
        self.assertEqual(copy.status()['counts']['player_games'], 1)
        with self.assertRaises(ValueError):
            self.store.backup(self.path)


if __name__ == '__main__':
    unittest.main()
