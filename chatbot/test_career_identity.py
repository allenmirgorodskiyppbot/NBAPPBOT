"""Documented aliases share a player key; namesakes never inherit history."""
import json
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from career_identity import resolve_career_identity, career_player_key, matchup_career_coverage
from master import MasterStore


class CareerIdentityTests(unittest.TestCase):
    def test_status_keeps_coverage_summary_without_dumping_every_player(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MasterStore(Path(folder) / 'master.sqlite')
            saved = {'phase': 'updated', 'requested_players': 488, 'covered_players': 488,
                     'missing_players': 0, 'source_data_through_date': '2026-11-04',
                     'coverage': [{'player': f'Player {index}'} for index in range(488)]}
            store.set_checkpoint('matchup_career_history_status', saved)
            public = store.status()['matchup_career_history']
            self.assertEqual(public['covered_players'], 488)
            self.assertEqual(public['missing_players'], 0)
            self.assertEqual(public['source_data_through_date'], '2026-11-04')
            self.assertNotIn('coverage', public)
            self.assertEqual(store.get_checkpoint('matchup_career_history_status'), saved)

    def lookup(self, rows, name, saved=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'targets.json'
            path.write_text(json.dumps({'players': rows}))
            class Store:
                def get_checkpoint(self, key, default):
                    return saved or default
            return resolve_career_identity(Store(), name, targets_path=path)

    def test_explicit_alias_shares_identity_without_renaming_rows(self):
        result = self.lookup([{'player': 'Bobby Portis', 'requested_player': 'Bobby Portis Jr.',
            'espn_player_id': '3064482', 'aliases': ['Bobby Portis', 'Bobby Portis Jr.']}], 'Bobby Portis Jr.')
        self.assertEqual(result['player'], 'Bobby Portis')
        self.assertEqual(result['player_ids'], ['name:bobbyportis', 'name:bobbyportisjr'])
        self.assertFalse(result['ambiguous'])

    def test_suffix_is_not_automatically_stripped(self):
        result = self.lookup([{'player': 'Unrelated Player', 'espn_player_id': '12'}], 'Unrelated Player Jr.')
        self.assertIsNone(result['espn_player_id'])
        self.assertEqual(result['player'], 'Unrelated Player Jr.')

    def test_namesake_provider_collision_does_not_borrow_history(self):
        result = self.lookup([{'player': 'Same Name', 'espn_player_id': '12'},
                              {'player': 'Same Name', 'espn_player_id': '13'}], 'Same Name')
        self.assertTrue(result['ambiguous'])
        self.assertIsNone(result['espn_player_id'])

    def test_connected_alias_conflict_is_also_ambiguous(self):
        result = self.lookup([{'player': 'First Name', 'espn_player_id': '12', 'aliases': ['Shared Alias']},
            {'player': 'Other Name', 'espn_player_id': '13', 'aliases': ['Shared Alias']}], 'First Name')
        self.assertTrue(result['ambiguous'])

    def test_validated_rolling_identity_can_add_documented_alias(self):
        result = self.lookup([], 'Documented Alias', {'players': [
            {'player': 'Current Player', 'espn_player_id': '44', 'aliases': ['Documented Alias']}]})
        self.assertEqual(result['player'], 'Current Player')
        self.assertEqual(result['espn_player_id'], '44')

    def test_real_packaged_crosswalk_binds_portis_variants(self):
        self.assertEqual(career_player_key('Bobby Portis'), career_player_key('Bobby Portis Jr.'))

    def test_coverage_deduplicates_aliases_and_excludes_unknown_dnp_and_future(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'targets.json'
            path.write_text(json.dumps({'players': [{'player': 'Bobby Portis',
                'requested_player': 'Bobby Portis Jr.', 'espn_player_id': '3064482',
                'aliases': ['Bobby Portis Jr.']}, {'player': 'Missing Player'}]}))
            games = [{'id': key, 'date': date, 'status': status} for key, date, status in
                [('a', '2025-01-02', 'final'), ('b', '2025-01-03', 'unknown'),
                 ('c', '2025-01-04', 'final'), ('d', '2099-01-01', 'final')]]
            base = {'game_id': 'a', 'date': '2025-01-02', 'player': 'Bobby Portis',
                'player_id': 'name:bobbyportis', 'season': '2024-25', 'opponent': 'NYK',
                'appearance_state': 'Played', 'season_type': 'Regular Season', 'pts': 15}
            rows = [base, {**base, 'player': 'Bobby Portis Jr.', 'player_id': 'name:bobbyportisjr'},
                    {**base, 'game_id': 'b', 'date': '2025-01-03'},
                    {**base, 'game_id': 'c', 'date': '2025-01-04', 'appearance_state': 'DNP'},
                    {**base, 'game_id': 'd', 'date': '2099-01-01'},
                    {**base, 'player_id': 'name:unrelatedplayer'}]
            coverage = matchup_career_coverage(None, rows, games,
                now=dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc), targets_path=path)
            self.assertEqual(coverage[0]['played_regular_games'], 1)
            self.assertEqual(coverage[0]['duplicate_alias_rows'], 1)
            self.assertEqual(coverage[0]['regular_games_against_NYK'], 1)
            self.assertEqual(coverage[1]['status'], 'missing_played_history')


if __name__ == '__main__':
    unittest.main()
