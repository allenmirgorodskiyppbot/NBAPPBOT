"""Historical metadata repairs cannot rewrite statistics or certify live games."""
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from career_metadata import complete_verified_game_metadata
from feeds import canonical_game
from master import MasterStore

NOW = dt.datetime(2026, 11, 5, 13, tzinfo=dt.timezone.utc)
SOURCE = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
          'espn_nba_player_boxscores/player_box_2025.parquet')


class CareerMetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.game = {'id': canonical_game('2025-01-25', 'CHI', 'PHI'), 'date': '2025-01-25',
            'status': 'final', 'home_team': 'CHI', 'away_team': 'PHI',
            'home_score': 97, 'away_score': 109, 'provider_game_id': '401705204',
            'start_time': '2025-01-26T01:00:00+00:00', 'season_type': 'Regular Season',
            'source': SOURCE, 'source_published_at': '2026-11-03T12:00:00+00:00',
            'source_content_sha256': 'a' * 64, 'upstream_full_game_validated': True}
        self.add_game()
        self.store.set_checkpoint('nba_schedule_today', {'status': 'unavailable'})
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete': False})
        self.store.set_checkpoint('chat:42', {'keep': 'conversation'})
        legs = [{'game_id': self.game['id'], 'player': 'Josh Giddey', 'team': 'CHI',
                 'date': '2025-01-25', 'stat': stat, 'line': line, 'side': 'more'}
                for stat, line in [('pts', 10.5), ('reb', 4.5)]]
        self.store.create_board(legs, source='fixture')
        self.store.create_entry(legs, 5, 'power', 2, source='fixture')

    def add_game(self, game=None, **changes):
        row = {**(game or self.game), 'status': 'unknown', 'source': 'workbook:original.xlsx',
               'home_score': None, 'away_score': None, 'start_time': None,
               'provider_game_id': '0022400642', **changes}
        self.store.upsert_game(row)
        self.store.upsert_player_game({'game_id': row['id'], 'player': 'Josh Giddey',
            'date': row['date'], 'team': row['home_team'], 'opponent': row['away_team'],
            'appearance_state': 'Played', 'minutes': 34, 'pts': 19, 'reb': 7, 'ast': 5,
            'season': '2024-25', 'season_type': 'Regular Season', 'source': 'workbook:original.xlsx'})

    def bundle(self, games=None, *, corrupt_hash=False, extra=None):
        events = [{'kind': 'game', 'data': game} for game in (games or [self.game])]
        events += extra or []
        data = gzip.compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode(), mtime=0)
        (self.root / 'history-001.jsonl.gz').write_bytes(data)
        manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'source_data_through_date': '2026-11-02', 'players': [],
            'parts': [{'file': 'history-001.jsonl.gz', 'bytes': len(data),
                       'records': len(events), 'sha256': '0' * 64 if corrupt_hash else hashlib.sha256(data).hexdigest()}]}
        path = self.root / 'history_manifest.json'
        path.write_text(json.dumps(manifest))
        return path

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def protected(self):
        with self.store.connect() as db:
            return {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)]
                    for name in ('player_games', 'boards', 'entries', 'grade_events', 'metadata', 'checkpoints')}

    def test_unknown_completion_preserves_stats_ledger_chat_and_readiness(self):
        before = self.protected()
        report = complete_verified_game_metadata(self.store, self.bundle(), now=NOW)
        self.assertEqual(report['games_completed'], 1)
        self.assertTrue(Path(report['backup']).exists())
        self.assertEqual(before, self.protected())
        with self.store.connect() as db:
            row = dict(db.execute('SELECT * FROM games WHERE id=?', (self.game['id'],)).fetchone())
            self.assertEqual((row['status'], row['home_score'], row['away_score']), ('final', 97, 109))
            self.assertEqual(row['provider_game_id'], '0022400642')
            self.assertEqual(row['source'], SOURCE)
            audit = dict(db.execute('SELECT * FROM career_game_metadata_repairs').fetchone())
            self.assertEqual(json.loads(audit['before_json'])['source'], 'workbook:original.xlsx')
            self.assertEqual(json.loads(audit['source_evidence_json'])['provider_game_id'], '401705204')
        state = self.dump()
        self.assertEqual(complete_verified_game_metadata(self.store, self.root / 'history_manifest.json', now=NOW)['games_completed'], 0)
        self.assertEqual(self.dump(), state)

    def test_final_live_and_conflicting_known_facts_are_retained(self):
        for change in ({'status': 'final'}, {'status': 'live'}, {'home_score': 104},
                       {'home_team': 'PHI', 'away_team': 'CHI'}, {'date': '2025-01-24'},
                       {'provider_game_id': '401999999', 'source': SOURCE}):
            with self.subTest(change=change):
                self.add_game(**change)
                old = self.dump()
                self.assertEqual(complete_verified_game_metadata(self.store, self.bundle(), now=NOW)['games_completed'], 0)
                self.assertEqual(self.dump(), old)

    def test_missing_verification_and_publication_never_promote_unknown(self):
        for change in ({'upstream_full_game_validated': False}, {'source_published_at': None},
                       {'source': 'https://untrusted.example/source'}, {'source_content_sha256': 'wrong'}):
            with self.subTest(change=change):
                before = self.dump()
                report = complete_verified_game_metadata(self.store, self.bundle([{**self.game, **change}]), now=NOW)
                self.assertEqual(report['games_completed'], 0)
                self.assertEqual(report['source_games_without_required_proof'], 1)
                self.assertEqual(self.dump(), before)

    def test_corrupt_late_record_and_bad_checksum_leave_all_rows_unchanged(self):
        for bad_hash in (True, False):
            path = self.bundle(corrupt_hash=bad_hash,
                extra=None if bad_hash else [{'kind': 'unsupported', 'data': {}}])
            before = self.dump()
            with self.assertRaises(ValueError):
                complete_verified_game_metadata(self.store, path, now=NOW)
            self.assertEqual(self.dump(), before)

    def test_path_count_and_gzip_bounds_are_verified_before_writes(self):
        path = self.bundle()
        original = json.loads(path.read_text())
        for change in ({'file': '../history-001.jsonl.gz'}, {'records': 2}, {'bytes': 1}):
            value = copy.deepcopy(original)
            value['parts'][0].update(change)
            path.write_text(json.dumps(value))
            before = self.dump()
            with self.assertRaises(ValueError):
                complete_verified_game_metadata(self.store, path, now=NOW)
            self.assertEqual(self.dump(), before)

    def test_future_source_future_game_and_invalid_scores_cannot_repair(self):
        for change in ({'source_published_at': '2026-11-06T00:00:00+00:00'},
                       {'date': '2026-11-06'}, {'home_score': float('inf')},
                       {'away_score': 97}, {'home_score': True}, {'provider_game_id': 'abc'}):
            with self.subTest(change=change):
                before = self.dump()
                with self.assertRaises(ValueError):
                    complete_verified_game_metadata(self.store, self.bundle([{**self.game, **change}]), now=NOW)
                self.assertEqual(self.dump(), before)

    def test_saved_player_identity_conflict_blocks_game_completion(self):
        with self.store.connect() as db:
            db.execute("UPDATE player_games SET team='BOS' WHERE game_id=?", (self.game['id'],))
        before = self.dump()
        report = complete_verified_game_metadata(self.store, self.bundle(), now=NOW)
        self.assertEqual(report['games_conflicted'], 1)
        self.assertEqual(self.dump(), before)

    def test_backup_failure_blocks_all_metadata_changes(self):
        path = self.bundle()
        before = self.dump()
        with patch.object(self.store, 'backup', side_effect=OSError('backup unavailable')):
            with self.assertRaises(OSError):
                complete_verified_game_metadata(self.store, path, now=NOW)
        self.assertEqual(self.dump(), before)

    def test_mid_transaction_failure_rolls_back_prior_repair(self):
        other = {**self.game, 'id': canonical_game('2025-01-26', 'CHI', 'NYK'), 'date': '2025-01-26',
                 'away_team': 'NYK', 'provider_game_id': '401705205'}
        self.add_game(other)
        path = self.bundle([self.game, other])
        before = self.dump()
        original = self.store.upsert_game
        count = 0
        def fail_second(row, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('simulated write failure')
            return original(row, **kwargs)
        with patch.object(self.store, 'upsert_game', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                complete_verified_game_metadata(self.store, path, now=NOW)
        self.assertEqual(self.dump(), before)


if __name__ == '__main__':
    unittest.main()
