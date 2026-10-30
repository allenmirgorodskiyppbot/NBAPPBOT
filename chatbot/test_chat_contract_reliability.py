"""General reply-contract and conversation-scope regressions; no live-model claim."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

from basketball_research import _requested_population, resolve_question
from grounded_reply import (ReplyValidationError, evidence_cards, has_numerical_assertion,
                            parse_reply, render_reply, required_evidence)
from test_grounded_reply import context


class ReplyContractReliabilityTests(unittest.TestCase):
    def document(self, ids=None, analysis='The stored sample is limited.'):
        return {'evidence_ids': ['E3'] if ids is None else ids,
                'analysis': analysis, 'needs_more_data': False}

    def test_complete_json_and_single_enclosing_fence_parse_without_prose_extraction(self):
        document = self.document()
        encoded = json.dumps(document)
        for text in (encoded, '```json\n' + encoded + '\n```', '```\n' + encoded + '\n```'):
            self.assertEqual(parse_reply(text), document)
        for text in ('Here is the answer: ' + encoded, '```python\n' + encoded + '\n```',
                     '```json\n' + encoded + '\n``` extra answer', encoded + encoded, None):
            with self.subTest(text=text), self.assertRaises(ReplyValidationError) as caught:
                parse_reply(text)
            self.assertEqual(caught.exception.reason, 'invalid_json')

    def test_basketball_nouns_are_not_mistaken_for_statistical_claims(self):
        for analysis in ('His three-point shooting could affect scoring.',
                         'The 76ers could change their coverage.',
                         'Two-way players and one-on-one coverage can change shot quality.',
                         'His two-point attempts and three-pointers depend on the coverage.'):
            with self.subTest(analysis=analysis):
                self.assertFalse(has_numerical_assertion(analysis))
                self.assertIn(analysis, render_reply(self.document(analysis=analysis), evidence_cards(context()))['answer'])
        for analysis in ('He averages twenty points against them.', 'His 25.8 PPG is against PHI.',
                         'He made three points.', 'A three-point lead proves an edge.',
                         'He made five three-point attempts.', 'The 76ers allowed 100 points.'):
            with self.subTest(analysis=analysis), self.assertRaises(ReplyValidationError) as caught:
                render_reply(self.document(analysis=analysis), evidence_cards(context()))
            self.assertEqual(caught.exception.reason, 'numerical_prose')

    def test_duplicates_and_missing_required_facts_use_only_canonical_source_text(self):
        cards = evidence_cards(context())
        document = self.document(ids=['E3', 'E3'])
        original = json.dumps(document)
        result = render_reply(document, cards, ['E1'])
        self.assertEqual([r['evidence_id'] for r in result['sources']], ['E1', 'E3'])
        self.assertEqual(json.dumps(document), original)
        self.assertEqual(result['answer'].count(cards[0]['text']), 1)
        self.assertEqual(result['answer'].count(cards[2]['text']), 1)

    def test_required_card_has_priority_within_existing_output_limit(self):
        cards = evidence_cards(context())
        result = render_reply(self.document(ids=['E2', 'E3', 'E4']), cards, ['E1'])
        self.assertEqual([r['evidence_id'] for r in result['sources']], ['E1', 'E2', 'E3'])
        self.assertNotIn(cards[3]['text'], result['answer'])

    def test_reason_codes_are_bounded_and_do_not_include_private_model_text(self):
        cards = evidence_cards(context())
        examples = [(self.document(ids=['PRIVATE']), 'invalid_evidence'),
                    ({'PRIVATE': 'private message'}, 'invalid_schema'),
                    (self.document(analysis='x' * 701), 'analysis_too_long'),
                    (self.document(ids=[], analysis=''), 'empty_reply'),
                    ({**self.document(), 'needs_more_data': True}, 'retrieval_with_analysis')]
        for document, reason in examples:
            with self.subTest(reason=reason), self.assertRaises(ReplyValidationError) as caught:
                render_reply(document, cards)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(str(caught.exception), reason)
        self.assertIsNone(render_reply({'evidence_ids': [], 'analysis': '', 'needs_more_data': True}, cards))

    def test_spaced_match_up_uses_the_same_required_source_as_matchup(self):
        cards = evidence_cards(context())
        self.assertEqual(required_evidence('How does he match up against Charlotte?', cards), ['E1','E3','E2'])
        self.assertEqual(required_evidence('Explain his matchup against Charlotte.', cards), ['E1','E3','E2'])


class ConversationScopeReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.names = ['Jalen Brunson', 'Tyrese Maxey', 'OG Anunoby']
        self.history = [{'role': 'user', 'content': 'Explain Jalen Brunson against Philadelphia.'}]

    def resolve(self, question, history=None):
        with patch('basketball_research._players', return_value=self.names):
            return resolve_question(None, None, question, self.history if history is None else history)

    def test_comparative_pronoun_keeps_both_players_without_assuming_shared_old_opponent(self):
        for question, expected in [('How does he compare to Maxey?', ['Jalen Brunson', 'Tyrese Maxey']),
                                   ('How does Maxey compare to him?', ['Tyrese Maxey', 'Jalen Brunson']),
                                   ('Compare his scoring to Maxey.', ['Jalen Brunson', 'Tyrese Maxey'])]:
            with self.subTest(question=question):
                result = self.resolve(question)
                self.assertEqual(result['players'], expected)
                self.assertEqual(result['teams'], [])
        explicit = self.resolve('How does he compare to Maxey against Boston?')
        self.assertEqual(explicit['players'], ['Jalen Brunson', 'Tyrese Maxey'])
        self.assertEqual(explicit['teams'], ['BOS'])

    def test_new_explicit_topic_and_same_player_possessive_do_not_merge_old_player(self):
        self.assertEqual(self.resolve('What about Maxey?')['players'], ['Tyrese Maxey'])
        self.assertEqual(self.resolve('Is Maxey scoring more than his last season?')['players'], ['Tyrese Maxey'])
        self.assertEqual(self.resolve('What about Boston?')['players'], ['Jalen Brunson'])
        self.assertEqual(self.resolve('Thanks!')['players'], [])

    def test_ambiguous_recent_pronoun_does_not_reach_back_to_an_older_player(self):
        history = [*self.history, {'role': 'user', 'content': 'Compare Brunson and OG Anunoby.'}]
        result = self.resolve('How does he compare to Maxey?', history)
        self.assertEqual(result['players'], ['Tyrese Maxey'])
        self.assertNotIn('inherited_player', result)

    def test_previous_season_words_preserve_explicit_seasons_and_phase(self):
        fixed = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
        with patch('basketball_research.dt.datetime') as clock:
            clock.now.return_value = fixed
            for word in ('last', 'previous', 'prior'):
                result = _requested_population({'original_question': 'Explain scoring '+word+' season'})
                self.assertEqual(result['seasons'], ['2025-26'])
                self.assertEqual(result['season_basis'], 'previous season')
            result = _requested_population({'original_question': 'Explain last season playoffs'})
            self.assertEqual(result['season_type'], 'Playoffs')
            self.assertEqual(result['seasons'], ['2025-26'])
            result = _requested_population({'original_question': 'Compare last season 2024-25 and 2025-26'})
            self.assertEqual(result['seasons'], ['2024-25', '2025-26'])
            self.assertEqual(result['season_basis'], 'explicit request')


if __name__ == '__main__':
    unittest.main()
