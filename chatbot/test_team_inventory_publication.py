"""An unrelated source conflict cannot certify itself or block valid teams."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import published_game_inventory as pub
from feeds import canonical_game, canonical_player
from nba_daily_export import reconcile_day_inventory
from pregame_freshness import _verified_inventory_today
import test_published_game_inventory as fixtures

NOW, DAY = fixtures.NOW, fixtures.DAY


def population():
    calendar, results, snapshot = fixtures.fixture()
    provider = '0022600178'
    calendar.append({'game_id': provider, 'game_date': DAY, 'season': 2027,
        'season_type': 'regular-season', 'home_team_abbreviation': 'IND',
        'away_team_abbreviation': 'DEN', 'home_team_id': 1610612754, 'away_team_id': 1610612743})
    common = {**results[0], 'game_id': provider}
    results.extend([{**common, 'team_id': 1610612754, 'team_abbreviation': 'IND',
                     'matchup': 'IND @ DEN'},
                    {**results[1], 'game_id': provider, 'team_id': 1610612743,
                     'team_abbreviation': 'DEN', 'matchup': 'DEN @ IND'}])
    snapshot['expected_games'] = 2
    snapshot['games'].append({**snapshot['games'][0], 'id': canonical_game(DAY, 'IND', 'DEN'),
        'home_team': 'IND', 'away_team': 'DEN', 'provider_game_id': '401909926'})
    return calendar, results, snapshot


class TeamHistoricalInventoryTests(unittest.TestCase):
    def setUp(self):
        self.calendar, self.results, self.snapshot = population()
        self.cs = fixtures.source('nba_stats_schedules', 'nba_schedule_2027.parquet', '2026-11-03T15:00:00Z')
        self.rs = fixtures.source('nba_stats_player_game_logs', 'player_game_logs_2027.parquet')

    def check(self):
        return pub.historical_team_inventory(self.calendar, self.results, self.snapshot, DAY,
            checked_at=NOW, calendar_source=self.cs, results_source=self.rs)

    def test_bad_indiana_home_header_preserved_while_nyk_and_mil_are_verified(self):
        original = copy.deepcopy((self.calendar, self.results, self.snapshot))
        with self.assertRaisesRegex(ValueError, 'home/away'):
            pub.historical_inventory(self.calendar, self.results, self.snapshot, DAY,
                checked_at=NOW, calendar_source=self.cs, results_source=self.rs)
        result = self.check()
        self.assertFalse(result['inventory_verified'])
        certificates = result['team_inventory_verified']
        self.assertEqual(len(certificates), 28)
        self.assertNotIn('IND', certificates)
        self.assertNotIn('DEN', certificates)
        self.assertEqual(certificates['NYK']['inventory_game_ids'], [canonical_game(DAY, 'NYK', 'CHI')])
        self.assertFalse(certificates['NYK']['inventory_off_day_verified'])
        self.assertEqual(certificates['MIL']['inventory_game_ids'], [])
        self.assertTrue(certificates['MIL']['inventory_off_day_verified'])
        self.assertEqual((self.calendar, self.results, self.snapshot), original)

    def test_wrong_knicks_result_blocks_knicks_without_repair(self):
        self.results[0]['matchup'] = 'NYK @ CHI'
        result = self.check()
        self.assertNotIn('NYK', result['team_inventory_verified'])
        self.assertNotIn('CHI', result['team_inventory_verified'])
        self.assertFalse(result['inventory_verified'])

    def test_offday_certificate_rejected_when_conflicting_row_mentions_that_team(self):
        self.results[2]['matchup'] = 'IND vs. MIL'
        self.assertNotIn('MIL', self.check()['team_inventory_verified'])

    def test_offday_certificate_rejects_conflicting_numeric_team_identity(self):
        self.results[2]['team_id'] = 1610612749  # MIL, despite the IND abbreviation.
        self.assertNotIn('MIL', self.check()['team_inventory_verified'])

    def test_missing_or_extra_provider_event_ids_reject_all_certificates(self):
        original = copy.deepcopy(self.results)
        for rows in [original[:2], [*original, {**original[0], 'game_id': '0022600999'}]]:
            self.results = rows
            with self.assertRaisesRegex(ValueError, 'population'):
                self.check()

    def test_missing_extra_or_conflicting_espn_events_reject_all_certificates(self):
        original = copy.deepcopy(self.snapshot)
        for change in ('missing', 'duplicate', 'participant'):
            self.snapshot = copy.deepcopy(original)
            if change == 'missing':
                self.snapshot['games'].pop()
            elif change == 'duplicate':
                self.snapshot['games'].append(copy.deepcopy(self.snapshot['games'][0]))
            else:
                self.snapshot['games'][0]['away_team'] = 'MIL'
            with self.assertRaises(ValueError):
                self.check()

    def test_unknown_cup_population_or_unknown_result_mentions_fail_closed(self):
        original = copy.deepcopy(self.calendar)
        self.calendar[0]['away_team_abbreviation'] = 'TBD'
        with self.assertRaises(ValueError):
            self.check()
        self.calendar = original
        self.results[2]['matchup'] = 'IND @ UNKNOWN'
        with self.assertRaises(ValueError):
            self.check()

    def test_reconciliation_keeps_global_false_and_retains_team_certificate(self):
        result = reconcile_day_inventory(self.snapshot, self.check(), DAY, {'revision': 'scope'})
        self.assertFalse(result['inventory_verified'])
        self.assertIn('NYK', result['team_inventory_verified'])
        partial = copy.deepcopy(self.snapshot)
        partial['games'].pop()
        mismatch = reconcile_day_inventory(partial, self.check(), DAY, {'revision': 'scope'})
        self.assertFalse(mismatch['inventory_verified'])
        self.assertEqual(mismatch['team_inventory_verified'], {})

    def test_certificate_map_validates_games_sources_dates_and_offday_claim(self):
        good = self.check()['team_inventory_verified']
        self.assertEqual(pub.validate_team_inventory_certificates(good, DAY, self.snapshot['games'], NOW), good)
        for key, value in [('team', 'MIL'), ('date', '2026-11-03'),
                           ('inventory_game_ids', []), ('inventory_off_day_verified', True),
                           ('inventory_verified_at', '2026-11-06T18:00:00Z'),
                           ('inventory_verification_source', 'https://example.invalid/')]:
            bad = copy.deepcopy(good)
            bad['NYK'][key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                pub.validate_team_inventory_certificates(bad, DAY, self.snapshot['games'], NOW)
        bad = copy.deepcopy(good)
        bad['NYK']['inventory_sources'][1]['source_content_sha256'] = 'invalid'
        with self.assertRaises(ValueError):
            pub.validate_team_inventory_certificates(bad, DAY, self.snapshot['games'], NOW)

    def test_readiness_uses_only_requested_current_day_validated_team(self):
        certificates = self.check()['team_inventory_verified']
        record = {'date': DAY, 'inventory_verified': False, 'team_inventory_verified': certificates,
                  'team_final_game_ids': {'NYK': [canonical_game(DAY, 'NYK', 'CHI')]}}
        today = NOW.astimezone(pub.EASTERN).date()
        self.assertTrue(_verified_inventory_today(record, today, NOW, 'NYK'))
        self.assertTrue(_verified_inventory_today(record, today, NOW, 'MIL'))
        self.assertFalse(_verified_inventory_today(record, today, NOW, 'IND'))
        self.assertFalse(_verified_inventory_today(record, today, NOW))
        self.assertFalse(_verified_inventory_today(record, today + dt.timedelta(days=1),
                                                  NOW + dt.timedelta(days=1), 'NYK'))
        bad = copy.deepcopy(record)
        bad['team_inventory_verified']['NYK']['inventory_game_ids'] = []
        self.assertFalse(_verified_inventory_today(bad, today, NOW, 'NYK'))


class TeamInventoryFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_fallback_scopes_only_the_verified_teams(self):
        mirror = fixtures.FakeMirror()
        calendar, results, snapshot = population()
        mirror.values = {'nba_schedule_2027.parquet': json.dumps(calendar).encode(),
                         'player_game_logs_2027.parquet': json.dumps(results).encode()}
        with patch.object(pub, '_parquet_rows', side_effect=json.loads):
            result = await pub.fetch_historical_inventory(mirror, DAY, snapshot, checked_at=NOW)
        self.assertFalse(result['inventory_verified'])
        self.assertIn('MIL', result['team_inventory_verified'])
        self.assertNotIn('IND', result['team_inventory_verified'])


class TeamCertificateImportTests(unittest.TestCase):
    def setUp(self):
        from dataset_refresh import initialize_scope
        from master import MasterStore
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'team-certificate.sqlite')
        initialize_scope(self.store)
        calendar, results, snapshot = population()
        snapshot['player_games'] = [{'game_id': canonical_game(DAY, 'NYK', 'CHI'),
            'player': 'Jalen Brunson', 'player_id': canonical_player('Jalen Brunson'),
            'team': 'NYK', 'opponent': 'CHI', 'date': DAY, 'season': '2026-27',
            'season_type': 'Regular Season', 'appearance_state': 'Played',
            'minutes': 32, 'pts': 24, 'reb': 3, 'ast': 2,
            'source': 'synthetic source observation'}]
        proof = pub.historical_team_inventory(calendar, results, snapshot, DAY, checked_at=NOW,
            calendar_source=fixtures.source('nba_stats_schedules', 'nba_schedule_2027.parquet', '2026-11-03T15:00:00Z'),
            results_source=fixtures.source('nba_stats_player_game_logs', 'player_game_logs_2027.parquet'))
        self.document = reconcile_day_inventory(snapshot, proof, DAY,
            self.store.get_checkpoint('dataset_refresh_scope'))
        self.document['scope_revision'] = self.store.get_checkpoint('dataset_refresh_scope')['revision']

    def test_real_import_preserves_scoped_proof_and_both_teams_can_be_ready(self):
        from nightly_data import import_daily_snapshot
        from pregame_freshness import assess_pregame_freshness
        first = import_daily_snapshot(self.store, self.document, DAY, NOW)
        repeated = import_daily_snapshot(self.store, self.document, DAY, NOW)
        self.assertTrue(repeated['unchanged'])
        self.assertFalse(first['inventory_verified'])
        self.assertIn('NYK', first['team_inventory_verified'])
        self.assertIn('MIL', repeated['team_inventory_verified'])
        self.assertNotIn('IND', repeated['team_inventory_verified'])
        previous = '2026-11-03'
        old_id = canonical_game(previous, 'MIL', 'LAL')
        self.store.upsert_game({'id': old_id, 'date': previous, 'home_team': 'MIL', 'away_team': 'LAL',
                               'status': 'final', 'source': 'synthetic previous final'})
        self.store.set_checkpoint('daily_nba:' + previous, {'date': previous, 'complete': True,
            'last_checked_at': NOW.isoformat(), 'last_refresh_status': 'success', 'last_refresh_error': None,
            'source': 'espn', 'publisher_retrieved_at': NOW.isoformat(),
            'final_game_ids': [old_id], 'team_final_game_ids': {'MIL': [old_id]},
            'inventory_verified': True, 'inventory_verified_at': NOW.isoformat(),
            'inventory_verification_source': 'https://example.invalid/synthetic-previous-inventory'})
        today = NOW.astimezone(pub.EASTERN).date().isoformat()
        game = {'id': canonical_game(today, 'NYK', 'MIL'), 'date': today,
                'home_team': 'NYK', 'away_team': 'MIL', 'status': 'scheduled',
                'start_time': '2026-11-06T00:30:00+00:00', 'source': 'synthetic current schedule'}
        self.store.upsert_game(game)
        self.store.set_checkpoint('nba_schedule_today', {'date': today, 'status': 'success',
            'checked_at': NOW.isoformat(), 'source': 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261105',
            'games': [game], 'error': None})
        for team in ('NYK', 'MIL'):
            checked = assess_pregame_freshness(self.store, today, team, NOW)
            self.assertTrue(checked['statistics_ready'], checked['statistics_issues'])
            self.assertTrue(checked['ready'], checked['reasons'])
        self.assertFalse(self.store.get_checkpoint('daily_nba:' + DAY)['inventory_verified'])

    def test_malformed_team_map_rejects_import_atomically(self):
        from nightly_data import import_daily_snapshot
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        self.document['team_inventory_verified']['NYK']['inventory_game_ids'] = []
        with self.assertRaises(ValueError):
            import_daily_snapshot(self.store, self.document, DAY, NOW)
        with self.store.connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
