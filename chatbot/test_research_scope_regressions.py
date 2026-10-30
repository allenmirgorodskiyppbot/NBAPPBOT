"""Conversation retrieval keeps entity scope and exposes safe coverage metadata."""
import json
from pathlib import Path
import tempfile
import unittest

from basketball_research import research_context, resolve_question
from grounded_reply import evidence_cards
from master import MasterStore


class ResearchScopeRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.master = MasterStore(Path(directory.name) / 'master.sqlite')
        self.book = type('Workbook', (), {'sheets': {}})()
        self.book._research_assignments = [
            {'_row': index + 1, 'selected_player': player, 'opponent_team': team,
             'primary': 'Example Defender', 'alternate': 'Other Defender',
             'assignment_asof': '2026-10-22', 'notes': 'Tentative projection, not a verified lineup.'}
            for index, (player, team) in enumerate(
                (player, team) for player in ('Jalen Brunson', 'Tyrese Maxey', 'OG Anunoby')
                for team in ('BOS', 'ATL', 'CLE', 'TOR', 'PHI', 'CHA'))]
        for index, (player, team, opponent, points) in enumerate([
                ('Jalen Brunson', 'NYK', 'PHI', 21), ('Tyrese Maxey', 'PHI', 'NYK', 28),
                ('OG Anunoby', 'NYK', 'PHI', 18)]):
            game = {'id': 'game-' + str(index), 'date': '2026-10-20', 'home_team': team,
                    'away_team': opponent, 'status': 'final'}
            self.master.upsert_game(game)
            self.master.upsert_player_game({'game_id': game['id'], 'date': game['date'],
                'player': player, 'team': team, 'opponent': opponent, 'season': '2026-27',
                'season_type': 'Regular Season', 'appearance_state': 'Played',
                'pts': points, 'source': 'https://example.com/observed-game'})
            publication = {'entity': player, 'category': 'Player totals', 'season': '2026-27',
                'season_type': 'Regular Season', 'gp': 5, 'fields': {'pts': points * 5 + 10},
                'data_through_date': None, 'published_at': '2026-10-29T14:18:20+00:00',
                'source': 'https://example.com/published-player-totals'}
            with self.master.connect() as db:
                db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                    ('2026-27', player, 'Player totals', json.dumps(publication)))
        self.history = [{'role': 'user', 'content': 'How does Jalen Brunson match up against '
                         'Philadelphia, and what could affect his scoring? Keep it brief.'},
                        {'role': 'assistant', 'content': 'The saved PHI sample has limited coverage.'}]

    def context(self, question, history=None):
        resolved = resolve_question(self.book, self.master, question, history or [])
        return resolved, research_context(self.book, self.master, resolved)

    def test_comparative_pronoun_keeps_both_baselines_without_unrelated_assignments(self):
        question = 'How does he compare to Tyrese Maxey as a scorer?'
        resolved, context = self.context(question, self.history)
        self.assertEqual(resolved['players'], ['Jalen Brunson', 'Tyrese Maxey'])
        self.assertEqual(resolved['teams'], [])
        self.assertEqual(context['assignment_projections'], [])
        self.assertEqual(context['opponent_player_statistics'], [])
        self.assertEqual({row['player'] for row in context['current_player_statistics']},
                         {'Jalen Brunson', 'Tyrese Maxey'})
        cards = evidence_cards(context, question)
        baselines = [card for card in cards if card['kind'] == 'player_all_opponent_averages']
        self.assertEqual(len(baselines), 2)
        self.assertTrue(any('Jalen Brunson: 23 PPG' in card['text'] for card in baselines))
        self.assertTrue(any('Tyrese Maxey: 30 PPG' in card['text'] for card in baselines))
        self.assertFalse(any(card['kind'] == 'tentative_assignment' for card in cards))
        self.assertLessEqual(len(json.dumps(context)), 5500)

    def test_explicit_new_player_and_opponent_replaces_old_pair(self):
        resolved, context = self.context('What about OG against Charlotte?', self.history)
        self.assertEqual(resolved['players'], ['OG Anunoby'])
        self.assertEqual(resolved['teams'], ['CHA'])
        self.assertEqual({(row['selected_player'], row['opponent_team'])
                          for row in context['assignment_projections']}, {('OG Anunoby', 'CHA')})
        self.assertEqual(context['opponent_player_statistics'][0]['opponent'], 'CHA')
        self.assertEqual(context['opponent_player_statistics'][0]['n'], 0)
        self.assertEqual({row['player'] for row in context['current_player_statistics']}, {'OG Anunoby'})

    def test_explicit_comparison_opponent_is_still_respected_for_both_players(self):
        resolved, context = self.context('How does he compare to Tyrese Maxey against Boston?', self.history)
        self.assertEqual(resolved['players'], ['Jalen Brunson', 'Tyrese Maxey'])
        self.assertEqual({row['opponent_team'] for row in context['assignment_projections']}, {'BOS'})
        self.assertEqual({row['player'] for row in context['opponent_player_statistics']},
                         {'Jalen Brunson', 'Tyrese Maxey'})

    def test_availability_context_is_read_only_and_excludes_raw_feed_errors(self):
        with self.master.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_error,last_attempt,error) VALUES (?,?,?,?)',
                       ('availability_coverage', '2026-10-30T12:00:00+00:00', '2026-10-30T12:00:00+00:00',
                        'private detail https://example.com/?token=SECRET'))
            db.execute('INSERT INTO feed_status(name,last_success,last_attempt,detail) VALUES (?,?,?,?)',
                       ('injuries_configured', '2026-10-30T12:00:00+00:00', '2026-10-30T12:00:00+00:00',
                        'private configuration SECRET'))
        with self.master.connect() as db:
            before = [dict(row) for row in db.execute('SELECT * FROM feed_status ORDER BY name')]
        _, context = self.context('Is Jalen Brunson confirmed healthy and available tonight?')
        coverage = context['availability_coverage']
        self.assertFalse(coverage['complete_official_report_in_context'])
        self.assertEqual({row['name'] for row in coverage['feeds']},
                         {'availability_coverage', 'injuries_configured'})
        self.assertTrue(all(set(row) == {'name', 'last_success', 'last_attempt', 'last_error'}
                            for row in coverage['feeds']))
        self.assertNotIn('SECRET', json.dumps(coverage))
        self.assertNotIn('example.com', json.dumps(coverage))
        with self.master.connect() as db:
            after = [dict(row) for row in db.execute('SELECT * FROM feed_status ORDER BY name')]
        self.assertEqual(before, after)

    def test_missing_coverage_is_explicit_and_unrelated_requests_do_not_load_it(self):
        _, context = self.context('Is OG Anunoby injured?')
        self.assertEqual(context['availability_coverage'],
                         {'feeds': [], 'complete_official_report_in_context': False})
        _, context = self.context('Compare Brunson and Maxey scoring.')
        self.assertNotIn('availability_coverage', context)

    def test_malformed_feed_timestamp_is_not_copied_into_context(self):
        with self.master.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_error) VALUES (?,?)',
                       ('injuries_configured', 'credential-like-invalid-timestamp'))
        _, context = self.context('Is OG Anunoby available?')
        self.assertIsNone(context['availability_coverage']['feeds'][0]['last_error'])
        self.assertNotIn('credential-like', json.dumps(context['availability_coverage']))


if __name__ == '__main__':
    unittest.main()
