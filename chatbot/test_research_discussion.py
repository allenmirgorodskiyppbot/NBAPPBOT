"""A follow-up about /picks explains computed targets without inventing an offer."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feeds import canonical_game, canonical_player
from master import MasterStore
from research_discussion import build_research_discussion
from test_pregame_discussion import record_fixture_refresh

NOW = dt.datetime(2026, 11, 2, 21, tzinfo=dt.timezone.utc)


class ResearchDiscussionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master = MasterStore(Path(self.directory.name) / 'fixture.sqlite')
        self.master.upsert_game({'id': 'tonight', 'date': '2026-11-02', 'home_team': 'NYK',
            'away_team': 'BKN', 'status': 'scheduled', 'start_time': '2026-11-03T00:30:00+00:00',
            'source': 'test schedule'})
        for name, team in [('Jalen Brunson', 'NYK'), ('Julius Randle', 'BKN')]:
            samples = [(i, (dt.date(2026, 11, 1) - dt.timedelta(days=i)).isoformat(), '2026-27')
                       for i in range(12)]
            samples += [(i, (dt.date(2026, 4, 10) - dt.timedelta(days=i)).isoformat(), '2025-26')
                        for i in range(8)]
            for i, day, season in samples:
                game = canonical_game(day, team, 'BOS')
                self.master.upsert_game({'id': game, 'date': day, 'home_team': team,
                    'away_team': 'BOS', 'status': 'final', 'source': 'test game'})
                self.master.upsert_player_game({'game_id': game, 'player': name, 'team': team,
                    'opponent': 'BOS', 'date': day, 'season': season, 'season_type': 'Regular Season',
                    'appearance_state': 'Played', 'minutes': 32 + i % 3,
                    'pts': 22 + i % 4, 'reb': 6 + i % 2, 'ast': 4 + i % 2, 'tpm': 2,
                    'source': 'test observations'})
        record_fixture_refresh(self.master)
        self.picks = [{'player': 'Jalen Brunson', 'stat': 'pts', 'line_kind': 'research_target'},
                      {'player': 'Julius Randle', 'stat': 'reb', 'line_kind': 'research_target'}]

    def report(self, **slots):
        return build_research_discussion(self.master, self.picks, slots, NOW)

    def test_recomputes_target_projections_and_keeps_hypothetical_status(self):
        result = self.report(game_date='2026-11-02')
        self.assertEqual(len(result['cards']), 2)
        self.assertEqual(len(result['assessments']), 2)
        for item, card in zip(result['assessments'], result['cards']):
            self.assertFalse(item['projection']['priced_offer'])
            self.assertIsNone(item['projection']['selected_side_margin'])
            self.assertFalse(card['offer_verified'])
            self.assertEqual(card['line_kind'], 'research_target')
            self.assertIn('hypothetical', card['text'])
            self.assertLessEqual(len(card['text']), 500)
            self.assertEqual(item['projection']['n'], 20)
            self.assertEqual(item['projection']['seasons'], ['2025-26', '2026-27'])
            self.assertEqual(sum(row['season'] == '2026-27'
                                 for row in item['projection']['source_games']), 12)
            self.assertNotIn('line', item)
        self.assertIn('not confirmed PrizePicks offers', result['fallback_answer'])

    def test_named_followup_explains_only_that_existing_player(self):
        result = self.report(player='Julius Randle')
        self.assertEqual([item['player'] for item in result['assessments']], ['Julius Randle'])
        result = self.report(player='OG Anunoby')
        self.assertFalse(result['cards'])

    def test_old_numerical_model_output_never_becomes_input_line(self):
        self.picks[0].update(line=99.5, side='more', projection=200)
        result = self.report()
        p = result['assessments'][0]['projection']
        self.assertFalse(p['priced_offer'])
        self.assertLess(p['reference_projection'], 30)
        self.assertNotIn('99.5', result['cards'][0]['text'])

    def test_past_or_unavailable_game_is_explained_without_new_recommendation(self):
        past = self.report(game_date='2026-11-01')
        self.assertFalse(past['cards'])
        self.assertIn('passed', past['fallback_answer'])
        missing = self.report(game_date='2026-11-05')
        self.assertFalse(missing['cards'])
        self.assertIn('No saved game', missing['fallback_answer'])
        with self.master.connect() as db:
            db.execute("UPDATE games SET status='live' WHERE id='tonight'")
        self.assertFalse(self.report()['cards'])

    def test_no_future_observations_or_new_players_enter_projection(self):
        before = self.report()
        self.master.upsert_player_game({'game_id': 'future', 'player': 'Jalen Brunson',
            'team': 'PHI', 'date': '2026-11-03', 'minutes': 48, 'pts': 90,
            'season': '2026-27', 'appearance_state': 'Played', 'source': 'future fixture'})
        after = self.report()
        self.assertEqual(before['assessments'], after['assessments'])
        self.assertEqual(len(after['assessments']), 2)

    def test_invalid_date_or_conflicting_opponent_is_held(self):
        self.assertFalse(self.report(game_date='2026-99-01')['cards'])
        self.assertFalse(self.report(opponent='PHI')['cards'])
        self.assertEqual(len(self.report(opponent='BKN')['cards']), 2)

    def test_sparse_data_returns_useful_reason(self):
        with self.master.connect() as db:
            db.execute("DELETE FROM player_games WHERE date<'2026-10-30'")
        result = self.report()
        self.assertFalse(result['cards'])
        self.assertIn('Only 3', result['fallback_answer'])
        self.assertIn('20 are required', result['fallback_answer'])

    def test_available_evidence_can_explain_but_not_recommend_out_player(self):
        self.master.record_news({'id': 'out', 'title': 'Julius Randle: Ruled out for Monday',
            'published_at': '2026-11-02T19:00:00+00:00', 'source': 'test publisher'})
        result = self.report(player='Julius Randle')
        self.assertEqual(result['assessments'][0]['status'], 'held')
        self.assertIn('Hold.', result['cards'][0]['text'])
        self.assertEqual(result['cards'][0]['decision'], 'pass')
        from grounded_reply import natural_prompt_evidence
        prompt = natural_prompt_evidence(result['cards'])
        self.assertIn('Hold.', prompt[0]['text'])
        self.assertIn('out', prompt[0]['text'])

    def test_projection_errors_are_bounded_and_no_database_writes_occur(self):
        with self.master.connect() as db:
            before = '\n'.join(db.iterdump())
        self.report()
        with self.master.connect() as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))
        with patch('research_discussion.projection_for_leg', side_effect=ValueError('bad fixture')):
            result = self.report()
        self.assertFalse(result['cards'])
        self.assertIn('could not be validated', result['fallback_answer'])


if __name__ == '__main__':
    unittest.main()
