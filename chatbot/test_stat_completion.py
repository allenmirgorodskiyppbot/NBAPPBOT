"""NULL completion preserves known statistics and records actual source evidence."""
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
from stat_completion import complete_verified_player_statistics

NOW = dt.datetime(2026, 11, 5, 16, tzinfo=dt.timezone.utc)
SOURCE = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
          'espn_nba_player_boxscores/player_box_2025.parquet')


class StatCompletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.store = MasterStore(self.root / 'master.sqlite')
        self.game = {'id': canonical_game('2025-01-25', 'CHI', 'PHI'), 'date': '2025-01-25',
            'status': 'final', 'home_team': 'CHI', 'away_team': 'PHI', 'home_score': 97,
            'away_score': 109, 'provider_game_id': '401705204',
            'season_type': 'Regular Season', 'source': SOURCE,
            'source_published_at': '2026-11-03T12:00:00+00:00',
            'source_content_sha256': 'a' * 64, 'upstream_full_game_validated': True}
        self.identity = {'player': 'Josh Giddey', 'player_id': canonical_player('Josh Giddey'),
                         'espn_player_id': '4871145', 'aliases': ['Josh Giddey']}
        self.row = {'game_id': self.game['id'], **self.identity, 'team': 'CHI', 'opponent': 'PHI',
            'date': '2025-01-25', 'season': '2024-25', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 34.5, 'pts': 19, 'reb': 7, 'ast': 5,
            'fgm': 7, 'fga': 14, 'tpm': 2, 'tpa': 5, 'ftm': 3, 'fta': 4,
            'oreb': 2, 'dreb': 5, 'stl': 1, 'blk': 0, 'tov': 2, 'fouls': 2,
            'plus_minus': -7, 'starter': 0, 'source': SOURCE,
            'source_published_at': self.game['source_published_at'],
            'source_content_sha256': 'a' * 64, 'upstream_full_game_validated': True}
        self.store.upsert_game(self.game)
        self.save(plus_minus=None, starter=None)
        self.store.set_checkpoint('nba_schedule_today', {'status': 'unavailable'})
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete': False})
        self.store.set_checkpoint('chat:1', {'keep': 'conversation'})
        legs = [{'game_id': self.game['id'], 'player': 'Josh Giddey', 'team': 'CHI',
                 'date': '2025-01-25', 'stat': stat, 'line': line, 'side': 'more'}
                for stat, line in [('pts', 10.5), ('reb', 4.5)]]
        self.store.create_board(legs); self.store.create_entry(legs, 5, 'power', 2)

    def save(self, **changes):
        self.store.upsert_player_game({**self.row, 'source': 'workbook:original.xlsx', **changes})

    def bundle(self, row=None, game=None, *, extra=None, bad_hash=False):
        events = [{'kind': 'game', 'data': game or self.game},
                  {'kind': 'player_game', 'data': row or self.row}] + (extra or [])
        blob = gzip.compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode(), mtime=0)
        (self.root / 'history-001.jsonl.gz').write_bytes(blob)
        manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'source_data_through_date': '2026-11-04', 'players': [self.identity],
            'parts': [{'file': 'history-001.jsonl.gz', 'bytes': len(blob), 'records': len(events),
                       'sha256': '0' * 64 if bad_hash else hashlib.sha256(blob).hexdigest()}]}
        path = self.root / 'history_manifest.json'; path.write_text(json.dumps(manifest)); return path

    def nba_bundle(self, **changes):
        self.identity['nba_player_id'] = '1630581'
        source = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
                  'nba_stats_player_boxscores/player_boxscores_2025.parquet')
        row = {**self.row, 'source': source, 'nba_player_id': '1630581',
               'source_minutes_raw': '34:24', 'minutes': 34.4, **changes}
        game = {**self.game, 'source': source, 'provider_game_id': '0022400642'}
        return self.bundle(row=row, game=game)

    def roster(self, **changes):
        row = {'provider_game_id': self.game['provider_game_id'],
               'espn_player_id': self.identity['espn_player_id'], 'player': 'Josh Giddey',
               'team': 'CHI', 'starter': 0, 'did_not_play': False, 'season_end_year': 2025,
               'source': 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
                         'espn_nba_game_rosters/game_rosters_2025.parquet',
               'source_published_at': '2026-11-03T12:00:00+00:00', 'source_content_sha256': 'b' * 64,
               'proof': {key: True for key in ('exactly_five_starters_on_both_team_rosters',
                   'both_team_rosters_have_unique_named_player_ids', 'explicit_starter_boolean',
                   'no_starter_DNP_contradiction')}, **changes}
        path = self.root / 'rosters.jsonl.gz'
        path.write_bytes(gzip.compress((json.dumps(row) + '\n').encode()))
        return path

    def dated_roster(self, **changes):
        values = dict(game_id=self.game['id'], date=self.game['date'],
            home_team='CHI', away_team='PHI', date_mapping_source=SOURCE,
            date_mapping_source_published_at='2026-11-03T12:00:00+00:00',
            date_mapping_source_content_sha256='c' * 64,
            date_mapping_proof={'consistent_game_date_and_home_away_headers': True,
                'both_source_teams_observed': True, 'provider_game_id': self.game['provider_game_id'],
                'season_end_year': 2025})
        return self.roster(**{**values, **changes})

    def player(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM player_games').fetchone())

    def protected(self):
        with self.store.connect() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                    for name in ('games', 'boards', 'entries', 'grade_events', 'metadata', 'checkpoints')}

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def test_source_observed_zero_and_negative_values_fill_without_overwriting_known(self):
        before, protected = self.player(), self.protected()
        report = complete_verified_player_statistics(self.store, self.bundle(), now=NOW)
        self.assertEqual(report['rows_completed'], 1)
        self.assertEqual(report['fields_completed'], 2)
        self.assertEqual(report['completed_by_statistic'], {'plus_minus': 1, 'starter': 1})
        self.assertTrue(Path(report['backup']).exists())
        after = self.player()
        self.assertEqual((after['plus_minus'], after['starter']), (-7, 0))
        self.assertTrue(all(after[key] == value for key, value in before.items()
                            if key not in {'plus_minus', 'starter', 'updated_at'}))
        self.assertEqual(protected, self.protected())
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM player_stat_completions')]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row['old_value_json'] == 'null' and row['source'] == SOURCE for row in rows))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE player_stat_completions SET source='changed'")
        original = self.dump()
        self.assertEqual(complete_verified_player_statistics(self.store, self.root / 'history_manifest.json', now=NOW)['fields_completed'], 0)
        self.assertEqual(self.dump(), original)

    def test_dry_run_validates_without_creating_audit_or_backup(self):
        before = self.dump()
        result = complete_verified_player_statistics(self.store, self.bundle(), now=NOW, dry_run=True)
        self.assertEqual(result['fields_fillable'], 2)
        self.assertEqual(result['fields_completed'], 0)
        self.assertIsNone(result['backup'])
        self.assertEqual(self.dump(), before)

    def test_any_known_count_or_minutes_disagreement_holds_the_whole_row(self):
        for change in ({'pts': 20}, {'minutes': 34}, {'reb': 8}, {'starter': 1}):
            with self.subTest(change=change):
                self.save(**{'plus_minus': None, 'starter': None, **change})
                before = self.dump()
                result = complete_verified_player_statistics(self.store, self.bundle(), now=NOW)
                self.assertEqual(result['fields_completed'], 0)
                self.assertEqual(result['rows_held_known_statistics_conflict'], 1)
                self.assertEqual(self.dump(), before)

    def test_dnp_unknown_live_and_unverified_source_cannot_fill(self):
        for row_change, game_change, source_change in [
            ({'appearance_state': 'DNP'}, {}, {}), ({'appearance_state': 'Unknown'}, {}, {}),
            ({}, {'status': 'live'}, {}), ({}, {}, {'upstream_full_game_validated': False}),
            ({}, {}, {'source': 'https://untrusted.example/data'}),
            ({}, {}, {'source_content_sha256': None})]:
            with self.subTest(row_change=row_change, game_change=game_change, source_change=source_change):
                self.store.upsert_game({**self.game, **game_change})
                self.save(plus_minus=None, starter=None, **row_change)
                path = self.bundle(row={**self.row, **source_change})
                before = self.dump()
                self.assertEqual(complete_verified_player_statistics(self.store, path, now=NOW)['fields_completed'], 0)
                self.assertEqual(self.dump(), before)

    def test_bad_checksum_or_late_record_rolls_back_everything(self):
        for change in ({'bad_hash': True}, {'extra': [{'kind': 'unsupported', 'data': {}}]}):
            with self.subTest(change=change):
                path = self.bundle(**change); before = self.dump()
                with self.assertRaises(ValueError):
                    complete_verified_player_statistics(self.store, path, now=NOW)
                self.assertEqual(self.dump(), before)

    def test_incoherent_source_and_future_publication_are_rejected(self):
        for change in ({'fgm': 8}, {'oreb': 3}, {'fga': 6}, {'pts': float('nan')},
                       {'source_published_at': '2026-11-06T12:00:00+00:00'}):
            with self.subTest(change=change):
                path = self.bundle(row={**self.row, **change}); before = self.dump()
                with self.assertRaises(ValueError):
                    complete_verified_player_statistics(self.store, path, now=NOW)
                self.assertEqual(self.dump(), before)

    def test_cross_player_id_and_game_participants_never_supply_statistics(self):
        for change in ({'player_id': canonical_player('Unrelated Player')}, {'team': 'BOS', 'opponent': 'PHI'}):
            with self.subTest(change=change):
                with self.store.connect() as db:
                    db.execute('DELETE FROM player_games')
                self.save(plus_minus=None, starter=None, **change)
                before = self.dump()
                self.assertEqual(complete_verified_player_statistics(self.store, self.bundle(), now=NOW)['fields_completed'], 0)
                self.assertEqual(self.dump(), before)

    def test_collector_update_after_backup_wins_on_transaction_recheck(self):
        backup = self.store.backup
        def concurrent(path):
            result = backup(path)
            self.save(plus_minus=12, starter=1)
            return result
        with patch.object(self.store, 'backup', side_effect=concurrent):
            report = complete_verified_player_statistics(self.store, self.bundle(), now=NOW)
        self.assertEqual(report['fields_completed'], 0)
        self.assertEqual((self.player()['plus_minus'], self.player()['starter']), (12, 1))

    def test_backup_or_audit_failure_leaves_player_statistics_unchanged(self):
        path = self.bundle()
        for name, target, effect in [('backup', self.store, OSError('failed backup')),
                                    ('_snapshot', self.store, OSError('failed audit'))]:
            before = self.dump()
            with patch.object(target, name, side_effect=effect), self.assertRaises(OSError):
                complete_verified_player_statistics(self.store, path, now=NOW)
            self.assertEqual(self.dump(), before)

    def test_original_espn_whole_minute_and_nba_clock_prove_compatible_precision(self):
        self.save(plus_minus=None, starter=None, minutes=34, source=SOURCE)
        report = complete_verified_player_statistics(self.store, self.nba_bundle(), now=NOW)
        self.assertEqual(report['fields_completed'], 1)
        self.assertEqual(self.player()['minutes'], 34)
        self.assertEqual(self.player()['plus_minus'], -7)
        with self.store.connect() as db:
            audit = json.loads(db.execute("SELECT source_evidence_json FROM player_stat_completions "
                                         "WHERE statistic='plus_minus'").fetchone()[0])
        proof = audit['minute_precision_verified']
        self.assertEqual(proof['stored_source_minutes'], 34)
        self.assertEqual(proof['source_clock_raw'], '34:24')
        self.assertFalse(proof['stored_minutes_changed'])

    def test_arbitrary_workbook_minutes_or_unmatched_clock_do_not_get_precision_exception(self):
        for changes in ({}, {'source_minutes_raw': '35:24'}, {'minutes': 35.4, 'source_minutes_raw': '35:24'}):
            with self.subTest(changes=changes):
                self.save(plus_minus=None, starter=None, minutes=34)
                before = self.dump()
                result = complete_verified_player_statistics(self.store, self.nba_bundle(**changes), now=NOW)
                self.assertEqual(result['fields_completed'], 0)
                self.assertEqual(self.dump(), before)

    def test_official_person_id_disagreement_rejects_nba_input(self):
        path = self.nba_bundle(nba_player_id='2544')
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'person identity'):
            complete_verified_player_statistics(self.store, path, now=NOW)
        self.assertEqual(self.dump(), before)

    def test_independent_actual_roster_fills_only_starter_despite_rounded_minutes(self):
        row = {**self.row, 'minutes': 35}
        path = self.bundle(row=row)
        report = complete_verified_player_statistics(self.store, path, now=NOW,
                                                     starter_roster_path=self.roster())
        self.assertEqual(report['completed_by_statistic'], {'starter': 1})
        self.assertEqual(self.player()['minutes'], 34.5)
        self.assertIsNone(self.player()['plus_minus'])
        self.assertEqual(self.player()['starter'], 0)
        with self.store.connect() as db:
            audit = dict(db.execute('SELECT * FROM player_stat_completions').fetchone())
        self.assertIn('espn_nba_game_rosters', audit['source'])
        self.assertTrue(json.loads(audit['source_evidence_json'])['starter_source']['proof']
                        ['exactly_five_starters_on_both_team_rosters'])

    def test_roster_cannot_bypass_counting_stat_conflicts_or_bad_five_player_proof(self):
        path = self.bundle(row={**self.row, 'minutes': 35})
        self.save(plus_minus=None, starter=None, pts=20)
        before = self.dump()
        report = complete_verified_player_statistics(self.store, path, now=NOW,
                                                     starter_roster_path=self.roster())
        self.assertEqual(report['fields_completed'], 0)
        self.assertEqual(self.dump(), before)
        with self.assertRaisesRegex(ValueError, 'starter proof'):
            complete_verified_player_statistics(self.store, path, now=NOW,
                starter_roster_path=self.roster(proof={'exactly_five_starters_on_both_team_rosters': False}))
        self.assertEqual(self.dump(), before)

    def test_starter_flag_disagreement_and_dnp_are_held(self):
        path = self.bundle()
        for changes in ({'starter': 1}, {'did_not_play': True}):
            with self.subTest(changes=changes):
                before = self.dump()
                result = complete_verified_player_statistics(self.store, path, now=NOW,
                                                             starter_roster_path=self.roster(**changes))
                self.assertEqual(result['fields_completed'], 0)
                self.assertEqual(self.dump(), before)

    def test_roster_position_is_never_treated_as_a_starting_position(self):
        path = self.nba_bundle(starter=1, source_position_raw='F',
            starter_source_proof={'exactly_five_starters_on_both_team_rosters': True,
                'explicit_start_position': True, 'source_field': 'NBA BoxScoreTraditional position',
                'starter_counts': {'CHI': 5, 'PHI': 5}, 'full_roster_players': 26,
                'position_value': 'F'})
        before = self.dump()
        report = complete_verified_player_statistics(self.store, path, now=NOW)
        self.assertEqual(report['fields_completed'], 0)
        self.assertEqual(self.dump(), before)
        # Matching minute/stat numbers must not make an invented starter flag
        # acceptable either; roster position still is not START_POSITION.
        self.save(plus_minus=-7, starter=None)
        path = self.nba_bundle(starter=1, minutes=34.5, source_minutes_raw='34:30',
                              source_position_raw='F')
        before = self.dump()
        report = complete_verified_player_statistics(self.store, path, now=NOW)
        self.assertEqual(report['fields_completed'], 0)
        self.assertEqual(self.dump(), before)

    def test_verified_date_headers_link_espn_starters_to_nba_game_ids(self):
        path = self.nba_bundle()
        report = complete_verified_player_statistics(self.store, path, now=NOW,
                                                     starter_roster_path=self.dated_roster())
        self.assertEqual(report['completed_by_statistic'], {'starter': 1})
        self.assertEqual(self.player()['starter'], 0)
        self.assertIsNone(self.player()['plus_minus'])
        self.assertEqual(self.player()['minutes'], 34.5)

    def test_unverified_or_conflicting_date_headers_never_supply_starters(self):
        path = self.nba_bundle(); before = self.dump()
        with self.assertRaisesRegex(ValueError, 'date mapping'):
            complete_verified_player_statistics(self.store, path, now=NOW,
                starter_roster_path=self.dated_roster(date_mapping_proof={}))
        self.assertEqual(self.dump(), before)


if __name__ == '__main__':
    unittest.main()
