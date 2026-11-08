"""A timing-only failure permits explicit research, never missing statistics."""
import datetime as dt
import unittest
from unittest.mock import patch

from independent_picks import build_independent_picks, format_independent_picks
import test_independent_picks as fixtures

NOW, DATE = fixtures.NOW, fixtures.DATE


class IndependentResearchModeTests(unittest.TestCase):
    setUp = fixtures.IndependentPickTests.setUp
    observations = fixtures.IndependentPickTests.observations
    four_players = fixtures.IndependentPickTests.four_players
    table_counts = fixtures.IndependentPickTests.table_counts

    def readiness(self, **overrides):
        return {'ready': False, 'statistics_ready': True, 'timing_verified': False,
                'statistics_issues': [], 'timing_issues': ['Live schedule check expired.'],
                'reasons': ['Live schedule check expired.'],
                'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga'],
                **overrides}

    def build(self, readiness, **kwargs):
        with patch('independent_picks.assess_pregame_freshness', return_value=readiness):
            return build_independent_picks(self.store, DATE, now=NOW,
                                           allow_unverified_timing=True, **kwargs)

    def test_expired_timing_produces_four_explicit_research_targets_read_only(self):
        self.four_players()
        before = self.table_counts()
        report = self.build(self.readiness())
        self.assertEqual(len(report['picks']), 4)
        self.assertTrue(report['research_lineup_complete'])
        self.assertTrue(report['statistics_verified'])
        self.assertFalse(report['timing_verified'])
        self.assertFalse(report['live_verified'])
        self.assertFalse(report['live_lineup_complete'])
        self.assertEqual(self.table_counts(), before)
        for pick in report['picks']:
            self.assertTrue(pick['statistics_verified'])
            self.assertFalse(pick['live_verified'])
            self.assertTrue(pick['research_only'])
            self.assertNotIn('line', pick)
        text = format_independent_picks(report, compact=True, max_chars=1400)
        self.assertIn('Live timing unverified', text)
        self.assertEqual(text.count('More if line'), 4)
        self.assertEqual(text.count('Less if line'), 4)
        self.assertNotIn('missing or stale', text)
        self.assertLessEqual(len(text), 1400)

    def test_strict_default_remains_held(self):
        self.four_players()
        with patch('independent_picks.assess_pregame_freshness', return_value=self.readiness()):
            report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(report['picks'], [])
        self.assertFalse(report['allow_unverified_timing'])

    def test_failed_statistics_never_bypassed_by_timing_mode(self):
        self.four_players()
        report = self.build(self.readiness(statistics_ready=False,
                            statistics_issues=['A completed game is missing.']))
        self.assertEqual(report['picks'], [])
        self.assertFalse(report['statistics_verified'])
        self.assertIn('A completed game is missing.', report['held'][0]['reason'])

    def test_legacy_failed_check_does_not_imply_verified_statistics(self):
        self.four_players()
        report = self.build({'ready': False, 'reasons': ['Schedule check failed.']})
        self.assertEqual(report['picks'], [])

    def test_missing_latest_stat_remains_held_market_by_market(self):
        self.four_players()
        report = self.build(self.readiness(current_player_stat_coverage=['reb']))
        self.assertEqual(len(report['picks']), 4)
        self.assertEqual({row['stat'] for row in report['picks']}, {'reb'})
        missing = [row for row in report['evaluated_candidates'] if row['stat'] != 'reb']
        self.assertTrue(all(row['projection'] is None for row in missing))

    def test_player_specific_statistics_are_checked_after_team_readiness(self):
        self.four_players()
        def readiness(*args, **kwargs):
            if kwargs.get('player') == 'Jalen Brunson':
                return self.readiness(statistics_ready=False,
                                      statistics_issues=['Participation is missing.'])
            return self.readiness()
        with patch('independent_picks.assess_pregame_freshness', side_effect=readiness):
            report = build_independent_picks(self.store, DATE, now=NOW,
                                            allow_unverified_timing=True)
        self.assertEqual(len(report['picks']), 3)
        self.assertNotIn('Jalen Brunson', [row['player'] for row in report['picks']])

    def test_known_out_player_is_excluded_even_with_statistics_ready(self):
        self.four_players()
        self.store.record_news({'id': 'research-out', 'title': 'Jalen Brunson ruled out for Friday',
                               'published_at': '2026-10-30T20:00:00+00:00',
                               'source': 'synthetic research fixture', 'body': 'Will not play.'})
        report = self.build(self.readiness())
        self.assertNotIn('Jalen Brunson', [row['player'] for row in report['picks']])
        self.assertLess(len(report['picks']), 4)

    def test_started_game_is_not_reopened_by_research_mode(self):
        self.four_players()
        with patch('independent_picks.assess_pregame_freshness', return_value=self.readiness()):
            report = build_independent_picks(self.store, DATE,
                now=NOW + dt.timedelta(hours=3), allow_unverified_timing=True)
        self.assertEqual(report['picks'], [])
        self.assertIsNone(report['game'])

    def test_non_boolean_research_option_rejected(self):
        with self.assertRaises(ValueError):
            build_independent_picks(self.store, DATE, now=NOW, allow_unverified_timing='yes')


if __name__ == '__main__':
    unittest.main()
