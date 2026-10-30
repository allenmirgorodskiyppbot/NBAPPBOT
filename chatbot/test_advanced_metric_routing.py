"""Tracking questions must not silently become ordinary box-score averages."""
from pathlib import Path
import tempfile
import unittest

from master import MasterStore
from player_stats import answer_player_question


class AdvancedMetricRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        for day, assists in [('2026-03-01', 5), ('2026-03-03', 9)]:
            game_id = 'fixture:' + day
            self.store.upsert_game({'id': game_id, 'date': day, 'status': 'final',
                                   'home_team': 'NYK', 'away_team': 'BOS'})
            self.store.upsert_player_game({'game_id': game_id, 'date': day,
                'player': 'Jalen Brunson', 'team': 'NYK', 'opponent': 'BOS',
                'season': '2025-26', 'season_type': 'Regular Season',
                'appearance_state': 'Played', 'pts': 20, 'reb': 4, 'ast': assists,
                'minutes': 35, 'source': 'Fixture box score: actual assists only'})

    def test_potential_assists_never_substitute_actual_assists(self):
        for question in (
            'What are Jalen Brunson’s average potential assists in 2025-26?',
            'Show Jalen Brunson’s last two games and potential assists.',
            'What is Jalen Brunson averaging in potential_assists in 2025-26?',
            'What is his average potential assists in 2025-26?',
            'What are Jalen Brunson’s average actual and potential assists in 2025-26?',
        ):
            with self.subTest(question=question):
                self.assertIsNone(answer_player_question(self.store, question,
                    [{'role': 'user', 'content': 'Tell me about Jalen Brunson'}]))

    def test_other_explicit_tracking_metrics_reach_structured_research(self):
        for metric in ('passing assists', 'passes', 'touches', 'possession minutes',
                       'usage', 'USG%', 'rebound chances', 'rebounding chances',
                       'reb_chances', 'contested rebounds', 'uncontested rebounds',
                       'offensive rebounds', 'defensive rebounds', 'box outs',
                       'screen assists', 'secondary assists', 'hockey assists',
                       'drives', 'deflections', 'hustle', 'defensive rating',
                       'field goals', 'field goal percentage', 'FG%',
                       'true shooting', 'points per 100 possessions'):
            with self.subTest(metric=metric):
                self.assertIsNone(answer_player_question(self.store,
                    f'What are Jalen Brunson’s average {metric} in 2025-26?'))

    def test_plain_assists_still_return_actual_assists_without_model(self):
        for question in ('What are Jalen Brunson’s average assists in 2025-26?',
                         'Show Jalen Brunson’s last two games and assists.',
                         'What is Jalen Brunson’s APG in 2025-26?'):
            with self.subTest(question=question):
                result = answer_player_question(self.store, question)
                self.assertIsNotNone(result)
                self.assertEqual(result['data']['averages'], {'ast': 7})
                self.assertFalse(result['ai_used'])
                self.assertIn('Average ast: 7.00', result['answer'])


if __name__ == '__main__':
    unittest.main()
