"""Next-matchup research loads saved team records before coverage approval."""
import datetime as dt
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from feeds import canonical_game, canonical_player
from independent_picks import build_independent_picks, format_independent_picks
from master import MasterStore


NOW = dt.datetime(2026, 11, 8, 18, tzinfo=dt.timezone.utc)
DATE = '2026-11-10'
UNKNOWN = {'ready': False, 'statistics_ready': False, 'timing_verified': False,
           'statistics_issues': ['Complete recent team-game inventory is unverified.'],
           'reasons': ['Complete recent team-game inventory is unverified.'],
           'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga']}


class NextMatchupMasterLookupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'master.sqlite')
        self.store.upsert_game({'id': canonical_game(DATE, 'NYK', 'MIL'), 'date': DATE,
            'home_team': 'NYK', 'away_team': 'MIL', 'status': 'scheduled',
            'start_time': '2026-11-11T00:30:00+00:00', 'source': 'synthetic saved calendar'})
        for name, team in [('Jalen Brunson', 'NYK'), ('Karl-Anthony Towns', 'NYK'),
                           ('Giannis Antetokounmpo', 'MIL'), ('AJ Green', 'MIL')]:
            self.observe(name, team)

    def observe(self, name, team, age=0):
        for offset in range(20):
            day = (dt.date(2026, 11, 7) - dt.timedelta(days=offset + age)).isoformat()
            key = canonical_game(day, team, 'BOS')
            self.store.upsert_game({'id': key, 'date': day, 'home_team': team, 'away_team': 'BOS',
                                   'status': 'final', 'source': 'synthetic team history'})
            self.store.upsert_player_game({'game_id': key, 'date': day, 'player': name,
                'player_id': canonical_player(name), 'team': team, 'opponent': 'BOS',
                'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
                'minutes': 34, 'pts': 20+offset % 4, 'reb': 6+offset % 2,
                'ast': 5+offset % 2, 'fga': 16+offset % 4, 'source': 'synthetic team history'})

    def build(self, readiness=None, **kwargs):
        with patch('independent_picks.assess_pregame_freshness', return_value=readiness or UNKNOWN):
            return build_independent_picks(self.store, now=NOW, allow_unverified_timing=True, **kwargs)

    def test_next_saved_matchup_loads_four_team_profiles_without_claiming_verified_picks(self):
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        report = self.build()
        self.assertEqual(report['game_date'], DATE)
        self.assertEqual(report['master_lookup']['teams'], ['MIL', 'NYK'])
        self.assertEqual(report['master_lookup']['players_loaded'], {'MIL': 2, 'NYK': 2})
        self.assertEqual(len(report['master_profiles']), 4)
        self.assertEqual(report['picks'], [])
        self.assertFalse(report['statistics_verified'])
        self.assertFalse(report['live_verified'])
        self.assertFalse(report['lineup_complete'])
        self.assertTrue(report['master_profile_coverage']['two_team_coverage'])
        self.assertTrue(all(not p['approved_for_entry'] and not p['eligible_for_ranking']
                            for p in report['master_profiles']))
        text = format_independent_picks(report, compact=True, max_chars=1400)
        self.assertIn('NYK vs MIL', text)
        self.assertIn('coverage unverified', text)
        self.assertEqual(text.count('More if line'), 4)
        self.assertEqual(text.count('Less if line'), 4)
        self.assertNotIn('No supported projections', text)
        self.assertNotIn('0 of 4', text)
        self.assertLessEqual(len(text), 1400)
        with self.store.connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)

    def test_strict_builder_does_not_opt_into_saved_master_fallback(self):
        with patch('independent_picks.assess_pregame_freshness', return_value=UNKNOWN):
            report = build_independent_picks(self.store, now=NOW)
        self.assertEqual(report['picks'], [])
        self.assertEqual(report['master_profiles'], [])

    def test_historical_and_other_team_records_cannot_starve_the_current_matchup(self):
        for number in range(90):
            self.observe(f'000 Retired Fixture {number}', 'NYK', age=40)
        self.observe('Other Team Fixture', 'PHI')
        report = self.build()
        self.assertEqual(report['master_lookup']['players_loaded'], {'MIL': 2, 'NYK': 2})
        self.assertEqual(len(report['master_profiles']), 4)
        self.assertNotIn('Other Team Fixture', [p['player'] for p in report['master_profiles']])

    def test_missing_current_player_metric_does_not_get_a_historical_target(self):
        report = self.build({**UNKNOWN, 'current_player_stat_coverage': []})
        self.assertEqual(report['master_profiles'], [])

    def test_known_out_and_started_games_do_not_show_targets(self):
        with patch('independent_picks.player_availability', return_value={
                'blocked': True, 'note': 'Officially out.'}):
            self.assertEqual(self.build()['master_profiles'], [])
        with patch('independent_picks.assess_pregame_freshness', return_value=UNKNOWN):
            report = build_independent_picks(self.store, date=DATE,
                now=dt.datetime(2026, 11, 11, 2, tzinfo=dt.timezone.utc), allow_unverified_timing=True)
        self.assertEqual(report['master_profiles'], [])
        self.assertIsNone(report['game'])


if __name__ == '__main__':
    unittest.main()
