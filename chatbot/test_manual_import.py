import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feeds import canonical_game, canonical_player
from manual_import import MAX_BYTES, import_stats, parse_stats_document, record_manual_news
from master import MasterStore


class ManualImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')

    def tearDown(self):
        self.directory.cleanup()

    def row(self, **changes):
        row = {'date': '2020-10-15', 'player': 'Jalen Brunson', 'team': 'NYK',
               'opponent': 'BOS', 'game_status': 'final', 'appearance_state': 'Played',
               'pts': 24, 'minutes': 35.5}
        row.update(changes)
        return row

    def records(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM player_games ORDER BY player')]

    def test_parse_csv_json_and_board_documents_are_distinct(self):
        csv = (b'date,player,team,opponent,game_status,appearance_state,pts,minutes,reb\n'
               b'2020-10-15,Jalen Brunson,NY,BOS,final,Played,24,35:30,\n')
        parsed = parse_stats_document('BOX.CSV', csv)
        import_stats(self.store, parsed)
        record = self.records()[0]
        self.assertEqual(record['minutes'], 35.5)
        self.assertEqual(record['pts'], 24)
        self.assertEqual(record['team'], 'NYK')
        self.assertIsNone(record['reb'])
        self.assertEqual(record['season_type'], 'Unknown')
        self.assertEqual(parse_stats_document('stats.json', json.dumps([self.row()])), [self.row()])
        for filename, payload in [
            ('board.csv', 'player,stat,line,side,date\nJalen Brunson,pts,24.5,more,2020-10-15\n'),
            ('board.json', json.dumps({'legs': []})),
            ('stats.xlsx', b'xlsx'),
            ('stats.csv', 'date,date\n2020-10-15,2020-10-15\n'),
            ('stats.json', '[{"date":"2020-10-15","date":"2020-10-16"}]'),
        ]:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                parse_stats_document(filename, payload)

    def test_document_size_rows_finite_values_and_csv_width_limits(self):
        for filename, payload in [
            ('stats.json', b' ' * (MAX_BYTES + 1)),
            ('stats.json', json.dumps([self.row()] * 2001)),
            ('stats.json', '[{"pts":NaN}]'),
            ('stats.csv', 'date,player\n2020-10-15,Jalen Brunson,extra\n'),
            ('stats.json', b'\xff\xfe'),
        ]:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                parse_stats_document(filename, payload)

    def test_validates_all_rows_before_any_game_is_written(self):
        valid = self.row()
        invalid = self.row(date='2020-10-16', player='Josh Hart', team='NOT_A_TEAM')
        with self.assertRaises(ValueError):
            import_stats(self.store, [valid, invalid])
        self.assertEqual(self.store.games(), [])
        self.assertEqual(self.records(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_snapshots').fetchone()[0], 0)

    def test_strict_dates_status_minutes_counts_and_identifiers(self):
        future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        bad = [
            {'date': future}, {'date': '2020-10-15T12:00:00'}, {'date': '2020-02-30'},
            {'game_status': 'scheduled'}, {'game_status': None},
            {'appearance_state': 'Out'}, {'minutes': 0}, {'minutes': -1},
            {'minutes': '15:99'}, {'pts': None}, {'pts': True}, {'pts': 24.5},
            {'pts': float('nan')}, {'pts': float('inf')}, {'reb': {}},
            {'team': 'BOS'}, {'opponent': 'SEA'}, {'game_id': 'provider:123'},
            {'player_id': 'nba:123'}, {'player': '!!!'},
            {'period': '1H'}, {'market_period': 'Q4'},
        ]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                import_stats(self.store, [self.row(**values)])
        self.assertEqual(self.records(), [])
        # A real zero-point appearance is permitted when actual minutes are positive.
        import_stats(self.store, [self.row(pts=0, minutes=1)])
        self.assertEqual(self.records()[0]['pts'], 0)

    def test_duplicate_player_game_and_conflicting_game_status_rejected(self):
        for rows in [
            [self.row(), self.row(player='Jalen-Brunson')],
            [self.row(), self.row(player='Josh Hart', game_status='live')],
            [self.row(home_team='NYK', away_team='BOS'),
             self.row(player='Josh Hart', home_team='BOS', away_team='NYK')],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                import_stats(self.store, rows)
        self.assertEqual(self.records(), [])

    def test_home_away_is_never_inferred_from_sorted_canonical_id(self):
        import_stats(self.store, [self.row()])
        game = self.store.games('2020-10-15')[0]
        self.assertIsNone(game['home_team'])
        self.assertIsNone(game['away_team'])
        import_stats(self.store, [self.row(home_team='NYK', away_team='BOS')])
        game = self.store.games('2020-10-15')[0]
        self.assertEqual((game['home_team'], game['away_team']), ('NYK', 'BOS'))

    def test_dnp_clears_zero_statistics_and_conflicting_participation_is_rejected(self):
        import_stats(self.store, [self.row(appearance_state='DNP', minutes=0, pts=0, reb=0)])
        record = self.records()[0]
        self.assertIsNone(record['minutes'])
        self.assertIsNone(record['pts'])
        self.assertIsNone(record['reb'])
        with self.assertRaises(ValueError):
            import_stats(self.store, [self.row()])
        with self.assertRaises(ValueError):
            import_stats(self.store, [self.row(player='Josh Hart', appearance_state='DNP', pts=1, minutes=0)])
        self.assertEqual(self.records()[0]['appearance_state'], 'DNP')

    def test_live_to_final_requires_other_observed_players_and_clears_preliminary_fields(self):
        import_stats(self.store, [
            self.row(game_status='live', pts=10, minutes=20, ast=2, reb=3),
            self.row(player='Josh Hart', game_status='live', pts=5, minutes=20, reb=2),
        ])
        with self.assertRaisesRegex(ValueError, 'previously observed live players'):
            import_stats(self.store, [self.row(pts=24, minutes=35)])
        self.assertEqual(self.store.games('2020-10-15')[0]['status'], 'live')
        self.assertEqual(next(r for r in self.records() if r['player'] == 'Jalen Brunson')['pts'], 10)
        import_stats(self.store, [self.row(pts=24, minutes=35),
                                 self.row(player='Josh Hart', pts=10, minutes=30)])
        self.assertEqual(self.store.games('2020-10-15')[0]['status'], 'final')
        for row in self.records():
            self.assertIsNone(row['reb'])
            self.assertIsNone(row['ast'])

    def test_already_final_partial_corrections_preserve_optional_final_fields(self):
        import_stats(self.store, [self.row(reb=5, season='2020-21', season_type='Regular Season')])
        import_stats(self.store, [self.row(pts=26)])
        record = self.records()[0]
        self.assertEqual(record['pts'], 26)
        self.assertEqual(record['reb'], 5)
        self.assertEqual(record['season_type'], 'Regular Season')
        with self.assertRaisesRegex(ValueError, 'downgraded'):
            import_stats(self.store, [self.row(game_status='live')])

    def test_persistent_manual_ledger_and_automatic_provisional_grading(self):
        date = '2020-10-15'
        legs = [{'player': 'Jalen Brunson', 'date': date, 'team': 'NYK', 'stat': 'pts',
                 'side': 'more', 'line': 22.5},
                {'player': 'Jayson Tatum', 'date': date, 'team': 'BOS', 'stat': 'pts',
                 'side': 'less', 'line': 30.5}]
        entry = self.store.create_entry(legs, stake=10, entry_type='power', payout_multiplier=3)
        result = import_stats(self.store, [self.row(), self.row(player='Jayson Tatum', team='BOS',
                                                               opponent='NYK', pts=28, minutes=38)],
                              source='Telegram statistics.csv')
        restarted = MasterStore(self.store.path)
        saved = restarted.get_entry(entry['id'])
        self.assertEqual(saved['provisional_status'], 'won')
        self.assertIsNone(saved['actual_payout'])
        self.assertEqual(result['grade_updates'], 1)
        self.assertEqual(restarted.ledger_summary()['settled_entries'], 0)
        self.assertEqual(restarted.get_checkpoint('last_manual_stats_import')['id'], result['id'])
        with restarted.connect() as db:
            ledger = dict(db.execute('SELECT * FROM manual_imports WHERE id=?', (result['id'],)).fetchone())
            self.assertEqual(ledger['status'], 'complete')
            raw = [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM raw_snapshots WHERE kind=?',
                                                          ('player_games',))]
        self.assertTrue(all(row['manual_unverified'] for row in raw))
        self.assertTrue(all(row['manual_import_id'] == result['id'] for row in raw))
        self.assertIn('not live verified', next(f['detail'] for f in restarted.status()['feeds']
                                              if f['name'] == 'manual_stats'))

    def test_missing_statistics_cannot_be_graded_as_zero(self):
        legs = [{'player': 'Jalen Brunson', 'date': '2020-10-15', 'stat': stat,
                 'side': side, 'line': line} for stat, side, line in
                [('pts', 'more', 20), ('reb', 'less', 0.5)]]
        entry = self.store.create_entry(legs, stake=10, entry_type='power')
        import_stats(self.store, [self.row()])
        saved = self.store.get_entry(entry['id'])
        self.assertEqual(saved['grades'][1]['status'], 'review')
        self.assertEqual(saved['provisional_status'], 'review')

    def test_manual_news_is_unverified_deduplicated_and_urls_are_not_executed(self):
        first = record_manual_news(self.store, 'Brunson availability note',
                                   detail='User reports he is questionable.',
                                   url='https://example.com/report', team='NY')
        second = record_manual_news(self.store, 'Brunson availability note',
                                    detail='User reports he is questionable.',
                                    url='https://example.com/report', team='NY')
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        news = self.store.recent_news()[0]
        metadata = json.loads(news['metadata_json'])
        self.assertEqual(metadata['verification_status'], 'manual_unverified')
        self.assertIsNone(news['published_at'])
        self.assertEqual(news['team'], 'NYK')
        for url in ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.com/report']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                record_manual_news(self.store, 'Note', url=url)

    def test_unexpected_later_bundle_failure_is_durable_and_explicit(self):
        publish = self.store.upsert_game_bundle
        calls = []

        def fail_second(game, rows):
            calls.append(game['id'])
            if len(calls) == 2:
                raise RuntimeError('Simulated disk failure')
            return publish(game, rows)

        with patch.object(self.store, 'upsert_game_bundle', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, 'after 1 committed game bundles'):
                import_stats(self.store, [self.row(), self.row(date='2020-10-16')])
        self.assertEqual(len(self.store.games()), 1)
        checkpoint = self.store.get_checkpoint('last_manual_stats_import')
        self.assertEqual(checkpoint['status'], 'partial_failed')
        self.assertEqual(len(checkpoint['committed_game_ids']), 1)
        with self.store.connect() as db:
            saved = db.execute('SELECT status,summary_json FROM manual_imports').fetchone()
            self.assertEqual(saved['status'], 'partial_failed')
            self.assertEqual(json.loads(saved['summary_json'])['committed_game_ids'], calls[:1])


if __name__ == '__main__':
    unittest.main()
