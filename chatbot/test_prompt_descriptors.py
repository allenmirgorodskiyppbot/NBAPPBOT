"""Qualitative prompt descriptors preserve numerical evidence for display."""
import copy
import json
import unittest

from grounded_reply import evidence_cards, prompt_evidence, render_reply, required_evidence
from test_grounded_reply import context


class PromptDescriptorTests(unittest.TestCase):
    def sample_context(self):
        source = context('Jalen Brunson', 'PHI')
        source['opponent_player_statistics'][0].update(n=1, averages={'pts':21},
            observed_counts={'pts':1}, first_date='2026-10-20', data_through_date='2026-10-20')
        source['current_player_statistics'][0].update(ppg=25.8)
        return source

    def descriptors(self, source, question='Explain his scoring matchup'):
        cards = evidence_cards(source, question)
        return cards, {c['kind']: c['text'] for c in prompt_evidence(cards)}

    def test_actual_numeric_values_stay_in_source_cards_not_in_model_descriptors(self):
        cards, descriptions = self.descriptors(self.sample_context())
        before = copy.deepcopy(cards)
        encoded = json.dumps(prompt_evidence(cards))
        for raw in ('25.8', '21 PPG', '2026-10-20', '2026-10-28', '2026-10-29', 'Fixture Defender'):
            self.assertNotIn(raw, encoded)
        self.assertIn('Jalen Brunson', descriptions['player_opponent_sample'])
        self.assertIn('PHI', descriptions['player_opponent_sample'])
        self.assertIn('below', descriptions['player_opponent_sample'])
        self.assertIn('minimal stored sample', descriptions['player_opponent_sample'])
        self.assertIn('not a forecast', descriptions['player_all_opponent_averages'])
        result = render_reply({'evidence_ids': [], 'analysis':'The stored sample is limited.',
                               'needs_more_data':False}, cards, required_evidence('Explain his scoring matchup', cards))
        self.assertIn('25.8 PPG', result['answer'])
        self.assertIn('21 PPG', result['answer'])
        self.assertIn('2026-10-20', result['answer'])
        self.assertEqual(cards, before)

    def test_comparison_requires_matching_population_and_observed_denominators(self):
        for change in ('missing_observations', 'different_season', 'different_phase', 'missing_baseline_count'):
            source = self.sample_context()
            baseline = source['current_player_statistics'][0]
            if change == 'missing_observations':
                source['opponent_player_statistics'][0]['observed_counts']['pts'] = 0
            elif change == 'different_season':
                baseline['season'] = '2025-26'
            elif change == 'different_phase':
                baseline['season_type'] = 'Playoffs'
            else:
                baseline['published_at'] = None
            with self.subTest(change=change):
                _, descriptions = self.descriptors(source)
                self.assertNotIn('average is below', descriptions['player_opponent_sample'])
                self.assertNotIn('average is above', descriptions['player_opponent_sample'])

    def test_relationship_changes_with_source_values_and_never_becomes_a_forecast(self):
        for value, relationship in ((30, 'above'), (25.8, 'equal to'), (21, 'below')):
            source = self.sample_context()
            source['opponent_player_statistics'][0]['averages']['pts'] = value
            _, descriptions = self.descriptors(source)
            self.assertIn('average is '+relationship, descriptions['player_opponent_sample'])
            self.assertIn('cannot isolate', descriptions['player_opponent_sample'])
            self.assertIn('future projection', descriptions['player_opponent_sample'])

    def test_missing_opponent_sample_is_explicit_and_broad_totals_are_not_substituted(self):
        _, descriptions = self.descriptors(context())
        self.assertIn('no matching completed appearances', descriptions['player_opponent_sample'])
        self.assertIn('does not prove no real game occurred', descriptions['player_opponent_sample'])
        self.assertIn('cannot substitute', descriptions['player_opponent_sample'])
        self.assertNotIn('average is', descriptions['player_opponent_sample'])

    def test_team_defense_and_volume_have_explicit_measurement_limits(self):
        source = self.sample_context()
        source['current_tracking'].append({'entity':'Jalen Brunson','category':'Possessions',
            'season':'2026-27','season_type':'Regular Season','gp':5,
            'fields':{'touches':452,'possession_minutes':51.8}})
        _, descriptions = self.descriptors(source)
        self.assertIn('No league comparison baseline', descriptions['tracking_Team defense'])
        self.assertIn('individual defender traits', descriptions['tracking_Team defense'])
        self.assertIn('totals, not usage rates', descriptions['tracking_Possessions'])
        self.assertIn('does not measure shooting efficiency', descriptions['player_all_opponent_averages'])

    def test_required_evidence_contains_baseline_and_sample_beyond_assignment(self):
        cards, _ = self.descriptors(self.sample_context())
        required = required_evidence('How does he match up against Philadelphia?', cards)
        by_id = {c['id']:c for c in cards}
        self.assertEqual({by_id[i]['kind'] for i in required},
                         {'tentative_assignment','player_all_opponent_averages','player_opponent_sample'})
        self.assertLessEqual(len(required), 3)
        self.assertEqual(required_evidence('Thank you!', cards), [])

    def test_unlabeled_query_fallback_does_not_invent_population_or_copy_numeric_rows(self):
        card = {'id':'Q1','kind':'query_result','text':'[{"ppg": 99, "n": 2}]', 'query':'SELECT AVG(pts) FROM sample'}
        descriptor = prompt_evidence([card])[0]['text']
        self.assertNotIn('99', descriptor)
        self.assertNotIn('SELECT', descriptor)
        self.assertIn('filters, calculation and population require review', descriptor)
        self.assertEqual(card['text'], '[{"ppg": 99, "n": 2}]')


if __name__ == '__main__':
    unittest.main()
