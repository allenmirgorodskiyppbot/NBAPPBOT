"""Historical score completion fills only verified NULL cells and preserves facts."""
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from feeds import canonical_game, canonical_player
from master import MasterStore
from game_stat_completion import complete_verified_game_scores

NOW = dt.datetime(2026, 11, 5, 17, tzinfo=dt.timezone.utc)
ESPN = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
        'espn_nba_player_boxscores/player_box_2025.parquet')
NBA = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
       'nba_stats_player_boxscores/player_boxscores_2025.parquet')
LOG = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
       'nba_stats_player_game_logs/player_game_logs_2025.parquet')


class GameScoreCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = MasterStore(self.root / 'master.sqlite')
        self.source = {'id': canonical_game('2025-01-25', 'CHI', 'PHI'), 'date': '2025-01-25',
            'status': 'final', 'home_team': 'CHI', 'away_team': 'PHI', 'home_score': 97,
            'away_score': 109, 'provider_game_id': '0022400642', 'season_type': 'Regular Season',
            'source': NBA, 'source_published_at': '2026-11-03T12:00:00+00:00',
            'source_content_sha256': 'a' * 64, 'upstream_full_game_validated': True}
        self.identity = {'player': 'Josh Giddey', 'player_id': canonical_player('Josh Giddey'),
                         'espn_player_id': '4871145', 'nba_player_id': '1630581'}
        self.save()
        self.store.set_checkpoint('nba_schedule_today', {'status': 'unavailable'})
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete': False})

    def save(self, **changes):
        self.store.upsert_game({**self.source, 'home_score': None, 'away_score': None,
                               'source': 'workbook:original.xlsx', **changes})

    def game(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM games').fetchone())

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def bundle(self, game=None, *, extra=None, bad_hash=False):
        events = [{'kind': 'game', 'data': game or self.source}] + (extra or [])
        blob = gzip.compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode(), mtime=0)
        (self.root / 'games-001.jsonl.gz').write_bytes(blob)
        manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'source_data_through_date': '2026-11-04', 'players': [self.identity],
            'parts': [{'file': 'games-001.jsonl.gz', 'bytes': len(blob), 'records': len(events),
                       'sha256': '0' * 64 if bad_hash else hashlib.sha256(blob).hexdigest()}]}
        path = self.root / 'history_manifest.json'; path.write_text(json.dumps(manifest)); return path

    def crosswalk(self, **changes):
        row = {'game_id': self.source['id'], 'date': self.source['date'], 'home_team': 'CHI',
               'away_team': 'PHI', 'provider_game_id': '0022400642', 'source': LOG,
               'source_content_sha256': 'b' * 64, 'source_published_at': '2026-11-03T12:00:00+00:00',
               'home_score': 97, 'away_score': 109,
               'proof': {'consistent_game_date_and_home_away_headers': True,
                         'both_source_teams_observed': True, 'provider_game_id': '0022400642'}, **changes}
        path = self.root / 'game_scores_provider_crosswalk.jsonl.gz'
        path.write_bytes(gzip.compress((json.dumps(row) + '\n').encode())); return path

    def test_only_null_scores_fill_with_backup_immutable_audit_and_idempotence(self):
        self.save(home_score=97)
        before = self.game()
        with self.store.connect() as db:
            checks = [tuple(row) for row in db.execute('SELECT * FROM checkpoints')]
        result = complete_verified_game_scores(self.store, self.bundle(), now=NOW)
        self.assertEqual(result['fields_completed'], 1)
        self.assertEqual(result['completed_by_statistic'], {'away_score': 1})
        self.assertTrue(Path(result['backup']).exists())
        after = self.game(); self.assertEqual(after['away_score'], 109)
        self.assertEqual({k:v for k,v in before.items() if k != 'away_score'},
                         {k:v for k,v in after.items() if k != 'away_score'})
        with self.store.connect() as db:
            self.assertEqual(checks, [tuple(row) for row in db.execute('SELECT * FROM checkpoints')])
            audit = dict(db.execute('SELECT * FROM game_score_completions').fetchone())
            self.assertEqual(audit['old_value_json'], 'null')
            self.assertEqual(json.loads(audit['source_evidence_json'])['game'], self.source)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE game_score_completions SET statistic='altered'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM game_score_completions')
        before = self.dump()
        self.assertEqual(complete_verified_game_scores(self.store, self.bundle(), now=NOW)['fields_completed'], 0)
        self.assertEqual(before, self.dump())

    def test_dry_run_and_no_source_proof_do_not_mutate_database(self):
        before = self.dump()
        report = complete_verified_game_scores(self.store, self.bundle(), now=NOW, dry_run=True)
        self.assertEqual(report['fields_fillable'], 2)
        self.assertEqual(report['fillable_by_statistic'], {'home_score': 1, 'away_score': 1})
        self.assertIsNone(report['backup']); self.assertEqual(before, self.dump())
        for change in ({'upstream_full_game_validated': False}, {'source': 'https://unknown.example/file'},
                       {'source_content_sha256': None}):
            report = complete_verified_game_scores(self.store, self.bundle({**self.source, **change}), now=NOW)
            self.assertEqual(report['fields_completed'], 0); self.assertEqual(before, self.dump())

    def test_nonfinal_known_score_provider_and_team_contradictions_hold(self):
        for change, reason in [({'status': 'live'}, 'not_final'),
                               ({'home_score': 99}, 'known_score_conflict'),
                               ({'provider_game_id': '0022400999'}, 'provider_identity_conflict'),
                               ({'provider_game_id': None}, 'missing_saved_provider_identity'),
                               ({'home_team': 'BOS'}, 'event_identity_conflict')]:
            with self.subTest(change=change):
                self.save(**change); before = self.dump()
                report = complete_verified_game_scores(self.store, self.bundle(), now=NOW)
                self.assertEqual(report['fields_completed'], 0)
                self.assertEqual(report['games_held_' + reason], 1); self.assertEqual(before, self.dump())

    def test_espn_provider_requires_independent_nba_crosswalk(self):
        source = {**self.source, 'source': ESPN, 'provider_game_id': '401705204'}
        path = self.bundle(source)
        self.assertEqual(complete_verified_game_scores(self.store, path, now=NOW)['fields_completed'], 0)
        result = complete_verified_game_scores(self.store, path, now=NOW,
                                               provider_crosswalk_path=self.crosswalk())
        self.assertEqual(result['fields_completed'], 2)
        self.assertEqual(self.game()['provider_game_id'], '0022400642')
        self.assertEqual(self.game()['source'], 'workbook:original.xlsx')

    def test_bad_crosswalk_and_crosswalk_score_disagreement_never_fill(self):
        source = {**self.source, 'source': ESPN, 'provider_game_id': '401705204'}
        path = self.bundle(source); before = self.dump()
        for change in ({'proof': {}}, {'source_content_sha256': 'bad'}, {'home_team': 'BOS'},
                       {'date': '2025-01-24'}, {'source_published_at': '2027-01-01T00:00:00Z'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                complete_verified_game_scores(self.store, path, now=NOW, provider_crosswalk_path=self.crosswalk(**change))
            self.assertEqual(before, self.dump())
        result = complete_verified_game_scores(self.store, path, now=NOW,
                                               provider_crosswalk_path=self.crosswalk(home_score=96))
        self.assertEqual(result['games_held_crosswalk_score_conflict'], 1)
        self.assertEqual(before, self.dump())

    def test_late_invalid_records_checksums_scores_dates_and_provider_are_atomic(self):
        for change in ({'bad_hash': True}, {'extra': [{'kind': 'unsupported', 'data': {}}]}):
            path = self.bundle(**change); before = self.dump()
            with self.assertRaises(ValueError):
                complete_verified_game_scores(self.store, path, now=NOW)
            self.assertEqual(before, self.dump())
        for change in ({'home_score': 109}, {'away_score': 900}, {'home_score': True},
                       {'id': 'nba:wrong'}, {'date': '2025-02-30'}, {'provider_game_id': 'bad'},
                       {'source_published_at': '2027-01-01T00:00:00Z'}):
            path = self.bundle({**self.source, **change}); before = self.dump()
            with self.subTest(change=change), self.assertRaises(ValueError):
                complete_verified_game_scores(self.store, path, now=NOW)
            self.assertEqual(before, self.dump())

    def test_complete_two_team_game_logs_prove_scores_without_player_box_readiness(self):
        proof = {'consistent_game_date_and_home_away_headers': True,
                 'both_source_teams_observed': True, 'provider_game_id': '0022400642',
                 'source_rows': 2, 'team_plus_minus_consistent': True,
                 'team_points_components_consistent': True,
                 'home_matchup_header': 'CHI vs. PHI', 'away_matchup_header': 'PHI @ CHI',
                 'home_raw_team_abbreviation': 'CHI', 'away_raw_team_abbreviation': 'PHI',
                 'home_source_team_id': '1610612741', 'away_source_team_id': '1610612755',
                 'home_wl': 'L', 'away_wl': 'W'}
        path = self.bundle({**self.source, 'upstream_full_game_validated': False})
        for change in ({'away_matchup_header': 'PHI @ BOS'}, {'source_rows': 1},
                       {'team_plus_minus_consistent': False}, {'home_wl': 'W'},
                       {'home_source_team_id': '1610612744'}):
            before = self.dump()
            result = complete_verified_game_scores(self.store, path, now=NOW,
                provider_crosswalk_path=self.crosswalk(proof={**proof, **change}, season_type='regular-season'))
            self.assertEqual(result['fields_completed'], 0); self.assertEqual(before, self.dump())
        result = complete_verified_game_scores(self.store, path, now=NOW,
            provider_crosswalk_path=self.crosswalk(proof=proof, season_type='regular-season'))
        self.assertEqual(result['fields_completed'], 2)
        self.assertEqual(result['verified_game_log_score_proofs'], 1)
        with self.store.connect() as db:
            evidence = json.loads(db.execute('SELECT source_evidence_json FROM game_score_completions').fetchone()[0])
        self.assertEqual(evidence['game']['proof_kind'], 'two_team_game_log_final_scores')
        self.assertFalse(evidence['game']['upstream_full_game_validated'])
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'), {'status': 'unavailable'})

    def test_transaction_rechecks_concurrent_known_score_and_protected_change_rolls_back(self):
        path = self.bundle(); original_backup = self.store.backup
        def backup_and_change(destination):
            result = original_backup(destination)
            with self.store.connect() as db:
                db.execute('UPDATE games SET home_score=95')
            return result
        with patch.object(self.store, 'backup', backup_and_change):
            result = complete_verified_game_scores(self.store, path, now=NOW)
        self.assertEqual(result['fields_completed'], 0); self.assertEqual(self.game()['home_score'], 95)
        self.save()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER break_preservation AFTER UPDATE ON games BEGIN UPDATE games SET source='changed'; END")
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'protected field'):
            complete_verified_game_scores(self.store, path, now=NOW)
        self.assertEqual(before, self.dump())


if __name__ == '__main__':
    unittest.main()
