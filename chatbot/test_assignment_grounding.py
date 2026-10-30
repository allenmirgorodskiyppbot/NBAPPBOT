"""Unverified pairing names cannot become asserted guarding assignments."""
import copy
import unittest

from grounded_reply import (ReplyValidationError, evidence_cards, has_unsupported_assignment,
                            prompt_evidence, render_reply)
from test_grounded_reply import context


class AssignmentGroundingTests(unittest.TestCase):
    def cards(self, player='Jalen Brunson', primary='VJ Edgecombe'):
        source = context(player, 'PHI')
        source['assignment_projections'][0]['primary'] = primary
        return evidence_cards(source, 'Explain the scoring matchup')

    def render(self, analysis, cards=None):
        return render_reply({'evidence_ids': ['E1', 'E3'], 'analysis': analysis,
                             'needs_more_data': False}, cards or self.cards())

    def test_real_failure_and_arbitrary_player_names_cannot_assert_guarding_direction(self):
        examples = [
            ('Jalen Brunson', 'VJ Edgecombe', 'VJ Edgecombe, who will likely guard him. Expect moderate defensive pressure.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Opponent is expected to guard him.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Scorer will be guarded by Fixture Opponent.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Opponent likely guards Fixture Scorer.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Opponent guards him.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Opponent guards Fixture Scorer.'),
            ('Fixture Scorer', 'Fixture Opponent', 'He is matched onto Fixture Opponent.'),
            ('Fixture Scorer', 'Fixture Opponent', 'Fixture Opponent is his likely defender.'),
        ]
        for player, primary, analysis in examples:
            with self.subTest(analysis=analysis), self.assertRaises(ReplyValidationError) as caught:
                self.render(analysis, self.cards(player, primary))
            self.assertEqual(caught.exception.reason, 'unsupported_assignment')

    def test_uncertainty_and_hypothetical_effects_remain_available(self):
        examples = [
            'It is unknown whether VJ Edgecombe will guard him.',
            'We do not know who will guard him.',
            'Who will guard him remains unclear.',
            'There is no evidence that VJ Edgecombe will guard him.',
            'The source does not establish that VJ Edgecombe will guard him.',
            'If VJ Edgecombe guards him, the effect would depend on the actual coverage.',
            'If the expected defender is confirmed, his workload could still matter.',
            'A different defensive assignment could affect his shot quality.',
            'The research explanation will be covered separately.',
            'Who will guard him?',
            'The stored pairing remains tentative; a confirmed lineup would help interpret it.',
        ]
        for analysis in examples:
            with self.subTest(analysis=analysis):
                self.assertIn(analysis, self.render(analysis)['answer'])

    def test_separate_caveat_does_not_license_an_assertion_after_it(self):
        for analysis in (
            'The assignment is unconfirmed, but VJ Edgecombe will guard him.',
            'The assignment is unknown; VJ Edgecombe is expected to guard him.',
            'VJ Edgecombe will guard him, although this is unconfirmed.',
            'VJ Edgecombe will guard him if he plays.',
        ):
            with self.subTest(analysis=analysis), self.assertRaises(ReplyValidationError):
                self.render(analysis)

    def test_direction_names_are_hidden_only_from_model_not_canonical_facts(self):
        cards = self.cards()
        before = copy.deepcopy(cards)
        prompted = prompt_evidence(cards)
        self.assertNotIn('VJ Edgecombe', prompted[0]['text'])
        self.assertNotIn('Fixture Alternate', prompted[0]['text'])
        self.assertIn('guarding direction', prompted[0]['text'])
        self.assertNotIn('2026-10-22', prompted[0]['text'])
        self.assertEqual(cards, before)
        self.assertIn('all-opponent', prompted[2]['text'])
        self.assertNotIn('17.6', prompted[2]['text'])
        result = self.render('The pairing remains tentative.', cards)
        self.assertIn('VJ Edgecombe', result['answer'])
        self.assertIn('Fixture Alternate', result['answer'])
        self.assertIn('guarding direction unconfirmed', result['answer'])
        self.assertIn('2026-10-22', result['answer'])
        self.assertEqual(result['sources'][0]['source'], 'Matchup_Assignments')
        self.assertEqual(cards[0]['primary'], 'VJ Edgecombe')
        self.assertEqual(cards[0]['assignment_direction'], 'unconfirmed')
        self.assertIs(cards[0]['verified_game_day_assignment'], False)

    def test_team_defense_does_not_establish_an_individual_guarding_assignment(self):
        cards = [c for c in self.cards() if c['kind'] == 'tracking_Team defense']
        self.assertTrue(has_unsupported_assignment('VJ Edgecombe will guard him.', cards))
        self.assertFalse(has_unsupported_assignment('Team defense alone cannot identify an individual assignment.', cards))

    def test_explicit_verified_direction_does_not_authorize_the_reverse_claim(self):
        verified = [{'id': 'V1', 'kind': 'verified_defensive_assignment',
                     'verified_game_day_assignment': True,
                     'defender': 'Fixture Defender', 'offensive_player': 'Fixture Scorer'}]
        self.assertFalse(has_unsupported_assignment('Fixture Defender will guard Fixture Scorer.', verified))
        self.assertTrue(has_unsupported_assignment('Fixture Scorer will guard Fixture Defender.', verified))
        self.assertTrue(has_unsupported_assignment('Another Defender will guard Fixture Scorer.', verified))


if __name__ == '__main__':
    unittest.main()
