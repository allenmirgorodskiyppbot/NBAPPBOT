"""Readable uploaded offers yield explicit, unapproved historical directions."""
import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis_engine import analyze_board
from board_shortlist import enrich_shortlist, format_shortlist
from feeds import canonical_game, canonical_player
from master import MasterStore


NOW = dt.datetime(2026, 11, 7, 23, 0, tzinfo=dt.timezone.utc)
DATE = '2026-11-07'
GAME = canonical_game(DATE, 'CLE', 'NYK')
TIMING_ONLY = {'ready': False, 'statistics_ready': True, 'timing_verified': False,
              'reasons': ['A fresh independent game timing check is required.'],
              'timing_issues': ['A fresh independent game timing check is required.'],
              'statistics_issues': [],
              'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'oreb']}


class UploadedResearchPickTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MasterStore(Path(temporary.name) / 'master.sqlite')
        self.store.upsert_game({'id': GAME, 'date': DATE, 'home_team': 'CLE',
            'away_team': 'NYK', 'status': 'scheduled', 'start_time': '2026-11-08T01:00:00Z',
            'source': 'synthetic fixture'})

    def observe(self, player, team, stat, values):
        latest = dt.date(2026, 11, 4 if team == 'NYK' else 6)
        for offset, value in enumerate(values):
            day = ((latest - dt.timedelta(days=offset)) if offset < 5 else
                   dt.date(2026, 4, 15) - dt.timedelta(days=offset)).isoformat()
            game = canonical_game(day, team, 'BOS')
            self.store.upsert_game({'id': game, 'date': day, 'home_team': team,
                'away_team': 'BOS', 'status': 'final', 'source': 'synthetic fixture'})
            fields = {'pts': 20, 'reb': 6, 'ast': 5, 'oreb': 0}
            if stat == 'pra':
                fields.update(pts=value, reb=0, ast=0)
            else:
                fields[stat] = value
            self.store.upsert_player_game({'game_id': game, 'date': day,
                'player_id': canonical_player(player), 'player': player, 'team': team,
                'opponent': 'BOS', 'appearance_state': 'Played',
                'season': '2026-27' if offset < 5 else '2025-26',
                'season_type': 'Regular Season', 'minutes': 32, **fields,
                'source': 'synthetic fixture'})

    def offers(self, player, team, stat, line, sides=('more', 'less'), variant='standard'):
        return [{'player': player, 'team': team, 'opponent': 'CLE' if team == 'NYK' else 'NYK',
                 'date': DATE, 'game_id': GAME, 'stat': stat, 'line': line,
                 'side': side, 'offer_variant': variant} for side in sides]

    def build(self, offers, freshness=TIMING_ONLY, now=NOW):
        board = self.store.create_board(offers)
        with patch('analysis_engine.assess_pregame_freshness', return_value=freshness), \
                patch('board_shortlist.assess_pregame_freshness', return_value=freshness):
            report = analyze_board(self.store, board['id'], as_of=now)
            return enrich_shortlist(self.store, report, now)

    def user_fixture(self):
        # Counts, centers and actual lines match the user's failed 4-card reply.
        kat = [14, 15, 15, 16, 19, 8, 9, 10, 10, 11, 11, 11, 12, 12, 12, 12, 13, 13, 14, 14]
        mitchell = [37, 38, 39, 40, 41, 15, 20, 23, 25, 28, 30, 32, 34, 35, 36, 42, 43, 44, 45, 47]
        mobley = [8, 8, 8, 9, 9, 5, 6, 6, 7, 7, 7, 8, 10, 10, 11, 11, 12, 12, 13, 13]
        brunson = [1] * 5 + [0] * 13 + [1] * 2
        specs = [('Karl-Anthony Towns', 'NYK', 'reb', 9.5, kat, ('more',), 'goblin'),
                 ('Donovan Mitchell', 'CLE', 'pra', 32.5, mitchell, ('more', 'less'), 'standard'),
                 ('Evan Mobley', 'CLE', 'reb', 8.5, mobley, ('more', 'less'), 'standard'),
                 ('Jalen Brunson', 'NYK', 'oreb', .5, brunson, ('more', 'less'), 'standard')]
        offers = []
        for player, team, stat, line, values, sides, variant in specs:
            self.observe(player, team, stat, values)
            offers += self.offers(player, team, stat, line, sides, variant)
        return offers

    def test_four_actual_directions_with_exact_user_sample_math_never_become_approved(self):
        data = self.build(self.user_fixture())
        self.assertEqual(data['shortlist'], [])
        self.assertFalse(data['lineup_complete'])
        choices = data['research_picks']
        self.assertEqual(len(choices), 4)
        self.assertEqual(len({row['player'] for row in choices}), 4)
        self.assertEqual({row['team'] for row in choices}, {'NYK', 'CLE'})
        expected = {'Karl-Anthony Towns': (12.55, 12, 18, 2, 'more'),
                    'Donovan Mitchell': (34.7, 36.5, 13, 7, 'more'),
                    'Evan Mobley': (9, 8.5, 10, 10, 'less'),
                    'Jalen Brunson': (.35, 0, 7, 13, 'less')}
        for row in choices:
            self.assertEqual((row['mean'], row['median'], row['above'], row['below'], row['lean_side']),
                             expected[row['player']])
            self.assertFalse(row['eligible_for_ranking'])
            self.assertFalse(row['approved_for_entry'])
            self.assertTrue(row['research_only'])
            self.assertTrue(row['statistics_ready'])
            self.assertFalse(row['timing_verified'])
            self.assertIn(row['lean_side'], row['offered_sides'])
            self.assertEqual(row['sample_seasons'], ['2025-26', '2026-27'])
            self.assertEqual(row['current_sample_season_n'], 5)
        self.assertEqual(choices[-1]['player'], 'Jalen Brunson')
        self.assertTrue(choices[-1]['recent_conflict'])
        self.assertEqual(choices[-1]['recent_side_count'], 0)
        text = format_shortlist(data)
        self.assertEqual(text.count('RESEARCH PICK:'), 4)
        self.assertIn('More 9.5 rebounds', text)
        self.assertIn('More 32.5 PTS+REB+AST', text)
        self.assertIn('Less 8.5 rebounds', text)
        self.assertIn('Less 0.5 offensive rebounds', text)
        self.assertIn('Last 5: 0/5 on this side', text)
        self.assertIn('Mixed historical support', text)
        self.assertIn('Narrow research lean', text)
        self.assertNotIn('Hold:', text)
        self.assertEqual(text.count('live game timing is unverified'), 1)
        self.assertIn('No entry placed', text)
        self.assertLessEqual(len(text), 2400)

    def test_missing_statistics_or_legacy_unverified_metadata_keeps_held_comparisons(self):
        offers = self.user_fixture()
        for state in ({**TIMING_ONLY, 'statistics_ready': False},
                      {'ready': False, 'reasons': ['Unavailable coverage.']}):
            with self.subTest(state=state):
                data = self.build(offers, state)
                self.assertEqual(data['research_picks'], [])
                self.assertTrue(data['research_candidates'])
                self.assertNotIn('RESEARCH PICK:', format_shortlist(data))

    def test_an_unreadable_opposite_side_is_not_invented_or_replaced_by_forced_demon(self):
        self.observe('Jalen Brunson', 'NYK', 'oreb', [0] * 18 + [1, 1])
        data = self.build(self.offers('Jalen Brunson', 'NYK', 'oreb', .5, ('more',), 'demon'))
        self.assertEqual(data['research_picks'], [])
        self.assertEqual(len(data['research_candidates']), 1)
        self.assertNotIn('— Less 0.5', format_shortlist(data))
        self.assertNotIn('— More 0.5', format_shortlist(data))

    def test_all_tied_outcomes_and_unsupported_fantasy_scoring_do_not_create_directions(self):
        self.observe('Evan Mobley', 'CLE', 'reb', [9] * 20)
        offers = self.offers('Evan Mobley', 'CLE', 'reb', 9)
        data = self.build(offers)
        self.assertEqual(data['research_picks'], [])
        with self.assertRaisesRegex(ValueError, 'Unsupported statistic'):
            self.build(self.offers('Jalen Brunson', 'NYK', 'fantasy_score', 41.5))

    def test_out_started_canceled_and_conflicting_thresholds_never_get_research_directions(self):
        offers = self.user_fixture()
        with patch('board_shortlist.player_availability', return_value={
                'blocked': True, 'note': 'Hold: officially Out.', 'reports': []}):
            self.assertEqual(self.build(offers)['research_picks'], [])
        self.assertEqual(self.build(offers, now=NOW + dt.timedelta(hours=3))['research_picks'], [])
        self.store.upsert_game({'id': GAME, 'date': DATE, 'home_team': 'CLE', 'away_team': 'NYK',
            'status': 'canceled', 'start_time': '2026-11-08T01:00:00Z', 'source': 'synthetic fixture'})
        self.assertEqual(self.build(offers)['research_picks'], [])
        self.store.upsert_game({'id': GAME, 'date': DATE, 'home_team': 'CLE', 'away_team': 'NYK',
            'status': 'scheduled', 'start_time': '2026-11-08T01:00:00Z', 'source': 'synthetic fixture'})
        altered = copy.deepcopy(offers)
        altered += [{**offers[0], 'line': 10.5}]
        self.assertNotIn('Karl-Anthony Towns', {row['player'] for row in self.build(altered)['research_picks']})

    def test_fewer_than_twenty_source_games_stays_comparison_without_padding(self):
        self.observe('Evan Mobley', 'CLE', 'reb', [12] * 8)
        data = self.build(self.offers('Evan Mobley', 'CLE', 'reb', 8.5))
        self.assertEqual(data['research_picks'], [])
        self.assertEqual(data['research_candidates'][0]['n'], 8)

    def test_last_research_slot_covers_missing_team_after_three_strict_knicks_selections(self):
        offers = []
        for player in ('Karl-Anthony Towns', 'Josh Hart', 'Mikal Bridges'):
            self.observe(player, 'NYK', 'pts', [32] * 20)
            offers += self.offers(player, 'NYK', 'pts', 19.5)
        # The fourth NYK player ranks above the CLE research choice, so an
        # isolated one-slot selection would choose the wrong team.
        self.observe('Jalen Brunson', 'NYK', 'pts', [20] * 20)
        offers += self.offers('Jalen Brunson', 'NYK', 'pts', 19.5)
        self.observe('Donovan Mitchell', 'CLE', 'pra',
                     [37, 38, 39, 40, 41, 15, 20, 23, 25, 28, 30, 32, 34, 35, 36, 42, 43, 44, 45, 47])
        offers += self.offers('Donovan Mitchell', 'CLE', 'pra', 32.5)
        ready = {**TIMING_ONLY, 'ready': True, 'timing_verified': True,
                 'reasons': [], 'timing_issues': []}
        data = self.build(offers, ready)
        self.assertEqual(len(data['shortlist']), 3)
        self.assertEqual({row['leg']['team'] for row in data['shortlist']}, {'NYK'})
        self.assertEqual([row['player'] for row in data['research_picks']], ['Donovan Mitchell'])
        self.assertEqual(data['research_picks'][0]['leg']['side'], 'more')
        self.assertFalse(data['lineup_complete'])
        self.assertFalse(data['two_team_coverage'])
        self.assertEqual(data['missing_teams'], ['CLE'])
        combined = data['research_choice_coverage']
        self.assertEqual(combined['selected_count'], 4)
        self.assertEqual(combined['selected_teams'], ['CLE', 'NYK'])
        self.assertTrue(combined['complete'])
        self.assertTrue(combined['research_only'])
        self.assertFalse(combined['approved_for_entry'])


if __name__ == '__main__':
    unittest.main()
