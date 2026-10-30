"""Tracked opportunities and peer scoring comparisons preserve source units."""
import copy
import json
import unittest

from grounded_reply import evidence_cards, prompt_evidence, render_reply, required_evidence


class TrackingDescriptorRateTests(unittest.TestCase):
    def context(self, category='Passing', fields=None, gp=5):
        return {'resolved_players': ['Example Player'], 'resolved_teams': [],
                'requested_population': {'seasons': ['2026-27']},
                'current_tracking': [{'entity': 'Example Player', 'category': category,
                    'fields': fields if fields is not None else {'potential_assists': 63, 'assists': 25, 'passes': 335},
                    'gp': gp, 'season': '2026-27', 'season_type': 'Regular Season',
                    'published_at': '2026-10-29T14:18:20+00:00', 'data_through_date': None,
                    'source': 'https://example.com/tracking'}]}

    def test_potential_assist_average_uses_its_own_total_and_denominator(self):
        context = self.context()
        before = copy.deepcopy(context)
        question = "What is Example Player's average potential assists in the stored passing sheet?"
        cards = evidence_cards(context, question)
        passing = next(card for card in cards if card['kind'] == 'tracking_Passing')
        self.assertIn('63 potential assists total (12.6 potential assists/game)', passing['text'])
        self.assertIn('25 assists total (5 assists/game)', passing['text'])
        self.assertEqual(required_evidence(question, cards), [passing['id']])
        descriptor = prompt_evidence([passing])[0]['text']
        self.assertIn('potential assists', descriptor)
        self.assertIn('average request is answerable', descriptor)
        self.assertIn('distinct from recorded actual assists', descriptor)
        self.assertNotIn('12.6', descriptor)
        result = render_reply({'evidence_ids': [], 'analysis': 'The source distinguishes opportunities from actual assists.',
                               'needs_more_data': False}, cards, required_evidence(question, cards))
        self.assertIn('12.6 potential assists/game', result['answer'])
        self.assertEqual(context, before)

    def test_other_additive_tracking_fields_derive_rates_from_their_own_record(self):
        for category, fields, question, expected in [
            ('Rebounding', {'rebound_chances': 41, 'rebounds': 12}, 'Average rebound chances?', '8.2 rebound chances/game'),
            ('Possessions', {'touches': 452, 'possession_minutes': 51.8}, 'Average touches?', '90.4 touches/game'),
            ('Possessions', {'touches': 452, 'possession_minutes': 51.8}, 'Average possession time?', '10.36 possession minutes/game'),
        ]:
            with self.subTest(category=category, question=question):
                cards = evidence_cards(self.context(category, fields), question)
                self.assertIn(expected, cards[0]['text'])
                self.assertEqual(required_evidence(question, cards), [cards[0]['id']])

    def test_missing_or_invalid_games_denominator_never_generates_a_rate(self):
        for gp in (None, 0, -1, float('nan'), True):
            with self.subTest(gp=gp):
                cards = evidence_cards(self.context(gp=gp), 'Average potential assists?')
                self.assertIn('63 potential assists total', cards[0]['text'])
                self.assertNotIn('potential assists/game', cards[0]['text'])
                self.assertIn('per-game averages are unavailable', prompt_evidence(cards)[0]['text'])

    def test_advanced_metric_requirement_does_not_substitute_actual_assists_or_other_entity(self):
        context = self.context(fields={'assists': 25})
        context['current_tracking'].append({**context['current_tracking'][0],
            'entity': 'Unrequested Player', 'fields': {'potential_assists': 75}})
        cards = evidence_cards(context, 'Average potential assists?')
        self.assertEqual(required_evidence('Average potential assists?', cards), [])
        self.assertNotIn('potential_assists', cards[0]['available_tracking_metrics'])


class PeerVolumeDescriptorTests(unittest.TestCase):
    def context(self, second_value=28.4):
        return {'resolved_players': ['First Player', 'Second Player'], 'resolved_teams': [],
                'requested_population': {'seasons': ['2026-27']},
                'current_player_statistics': [{'player': name, 'season': '2026-27',
                    'season_type': 'Regular Season', 'games': 5, 'ppg': value,
                    'points_observed': 5, 'published_at': '2026-10-29T14:18:20+00:00',
                    'data_through_date': None}
                    for name, value in [('First Player', 25.8), ('Second Player', second_value)]]}

    def test_volume_relationship_is_computed_and_unknown_freshness_is_explicit(self):
        for value, relationship in ((28.4, 'lower than'), (25.8, 'equal to'), (20, 'higher than')):
            with self.subTest(value=value):
                cards = evidence_cards(self.context(value), 'Compare them as scorers.')
                first = prompt_evidence(cards)[0]['text']
                self.assertIn("First Player's stored all-opponent scoring average is " + relationship + " Second Player's", first)
                self.assertIn('covered-game date is unknown', first)
                self.assertIn('not overall scoring ability or efficiency', first)
                self.assertNotIn('25.8', first)
                self.assertIn('25.8 PPG', cards[0]['text'])
                self.assertEqual(required_evidence('Compare them as scorers.', cards), ['E1', 'E2'])

    def test_peer_comparison_requires_same_season_phase_and_observed_denominators(self):
        for field, value in [('season', '2025-26'), ('season_type', 'Playoffs'),
                             ('points_observed', 0), ('ppg', None)]:
            context = self.context()
            context['current_player_statistics'][1][field] = value
            with self.subTest(field=field):
                text = json.dumps(prompt_evidence(evidence_cards(context, 'Compare their scoring.')))
                self.assertNotIn('average is lower than', text)
                self.assertNotIn('average is higher than', text)

    def test_distinct_known_coverage_periods_are_not_called_synchronized(self):
        context = self.context()
        context['current_player_statistics'][0]['data_through_date'] = '2026-10-27'
        context['current_player_statistics'][1]['data_through_date'] = '2026-10-28'
        first = prompt_evidence(evidence_cards(context, 'Compare their scoring.'))[0]['text']
        self.assertIn('not synchronized current snapshots', first)


if __name__ == '__main__':
    unittest.main()
