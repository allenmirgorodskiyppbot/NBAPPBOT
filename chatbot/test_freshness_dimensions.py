"""Statistics coverage and current event timing have independent provenance."""
import datetime as dt
import unittest
from unittest.mock import patch

import test_pregame_freshness as fixtures


NOW = fixtures.NOW
TODAY = fixtures.TODAY
YESTERDAY = fixtures.YESTERDAY


class FreshnessDimensionTests(unittest.TestCase):
    setUp = fixtures.PregameFreshnessTests.setUp
    schedule = fixtures.PregameFreshnessTests.schedule
    game = fixtures.PregameFreshnessTests.game
    checked = fixtures.PregameFreshnessTests.checked
    assess = fixtures.PregameFreshnessTests.assess
    current = fixtures.PregameFreshnessTests.current

    def test_full_readiness_has_two_independently_satisfied_dimensions(self):
        self.current()
        result = self.assess(stat='pts')
        self.assertTrue(result['ready'])
        self.assertTrue(result['statistics_ready'])
        self.assertTrue(result['timing_verified'])
        self.assertEqual(result['statistics_issues'], [])
        self.assertEqual(result['timing_issues'], [])

    def test_expired_timing_does_not_relabel_current_statistics_as_stale(self):
        self.current()
        self.schedule(checked_at=(NOW-dt.timedelta(minutes=16)).isoformat())
        result = self.assess(stat='pts')
        self.assertFalse(result['ready'])
        self.assertTrue(result['statistics_ready'])
        self.assertFalse(result['timing_verified'])
        self.assertEqual(result['statistics_issues'], [])
        self.assertEqual(result['reasons'], result['timing_issues'])
        self.assertIn('within 15 minutes', result['timing_issues'][0])
        self.assertEqual(result['player_data_through_date'], YESTERDAY)
        self.assertIn('pts', result['current_player_stat_coverage'])

    def test_missing_or_blocked_timing_keeps_verified_input_coverage(self):
        self.current()
        for checkpoint in ({}, {'date':TODAY, 'status':'failed', 'error':'HTTPStatusError: HTTP 403',
                               'checked_at':NOW.isoformat()}):
            with self.subTest(checkpoint=checkpoint):
                self.store.set_checkpoint('nba_schedule_today', checkpoint)
                result = self.assess(stat='pts')
                self.assertFalse(result['ready'])
                self.assertTrue(result['statistics_ready'])
                self.assertFalse(result['timing_verified'])
                self.assertEqual(result['statistics_issues'], [])
                self.assertTrue(result['timing_issues'])

    def test_a_valid_schedule_cannot_make_missing_publication_coverage_current(self):
        self.current()
        with self.store.connect() as db:
            db.execute('DELETE FROM checkpoints WHERE name=?', ('daily_nba:'+YESTERDAY,))
        result = self.assess(stat='pts')
        self.assertTrue(result['timing_verified'])
        self.assertFalse(result['statistics_ready'])
        self.assertFalse(result['ready'])
        self.assertTrue(result['statistics_issues'])
        self.assertEqual(result['timing_issues'], [])

    def test_pending_latest_team_game_blocks_statistics_even_with_valid_timing(self):
        self.current()
        self.store.set_checkpoint('nightly_nba_refresh', {'pending_dates':[YESTERDAY]})
        result = self.assess(stat='pts')
        self.assertTrue(result['timing_verified'])
        self.assertFalse(result['statistics_ready'])
        self.assertTrue(any('pending import' in issue for issue in result['statistics_issues']))
        self.assertFalse(result['ready'])

    def test_missing_player_participation_is_a_statistics_failure(self):
        self.current(player=None)
        result = self.assess(stat='pts')
        self.assertTrue(result['timing_verified'])
        self.assertFalse(result['statistics_ready'])
        self.assertIsNone(result['player_data_through_date'])
        self.assertTrue(any('participation' in issue for issue in result['statistics_issues']))

    def test_requested_market_uses_its_actual_latest_game_component_coverage(self):
        self.current()
        points = self.assess(stat='pts')
        attempts = self.assess(stat='fga')
        self.assertTrue(points['statistics_ready'])
        self.assertFalse(attempts['statistics_ready'])
        self.assertNotIn('fga', attempts['current_player_stat_coverage'])
        self.assertTrue(attempts['timing_verified'])
        self.assertEqual(attempts['timing_issues'], [])

    def test_future_event_can_use_current_inputs_without_claiming_gameday_readiness(self):
        self.current()
        self.schedule(game_day='2026-11-05')
        result = self.assess(date='2026-11-05', stat='pts')
        self.assertTrue(result['statistics_ready'])
        self.assertFalse(result['timing_verified'])
        self.assertFalse(result['ready'])
        self.assertTrue(result['historical_only'])
        self.assertEqual(result['statistics_issues'], [])
        self.assertTrue(result['timing_issues'])
        self.assertNotIn('schedule_check', result)

    def test_past_and_invalid_targets_are_not_current_statistics_ready(self):
        self.current()
        for day in (YESTERDAY, 'invalid-date', None):
            with self.subTest(day=day):
                result = self.assess(date=day, stat='pts')
                self.assertFalse(result['statistics_ready'])
                self.assertFalse(result['timing_verified'])
                self.assertFalse(result['ready'])
                self.assertTrue(result['statistics_issues'])

    def test_unknown_team_never_has_a_ready_dimension(self):
        self.current()
        result = self.assess(team='', stat='pts')
        self.assertFalse(result['statistics_ready'])
        self.assertFalse(result['timing_verified'])
        self.assertFalse(result['ready'])
        self.assertIn('unresolved', result['statistics_issues'][0])

    def test_dimension_classification_does_not_guess_from_error_wording(self):
        self.current()
        message = 'Statistics words in a schedule error do not change its dimension.'
        with patch('pregame_freshness._current_schedule_check',
                   return_value=({'game_verified':False}, message)):
            result = self.assess(stat='pts')
        self.assertTrue(result['statistics_ready'])
        self.assertEqual(result['statistics_issues'], [])
        self.assertEqual(result['timing_issues'], [message])
        self.assertFalse(result['ready'])

    def test_assessing_dimensions_is_read_only(self):
        self.current()
        self.schedule(status='failed', error='ReadTimeout')
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        self.assess(stat='pts')
        self.assess(date='2026-11-05', stat='pts')
        self.assess(date=YESTERDAY, stat='pts')
        with self.store.connect() as db:
            after = '\n'.join(db.iterdump())
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
