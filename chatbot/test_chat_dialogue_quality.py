"""Follow-up meaning and checked answers survive a failed small-model reply."""
import asyncio
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import (resolve_conversation, validated_lineup_preferences,
                          lineup_preference_updates)
from grounded_reply import natural_fallback_reply
import test_chat_flow as fixtures
from test_conversation import NAMES, NOW
from test_natural_reply import prop_cards


class DialogueResolutionQualityTests(unittest.TestCase):
    def setUp(self):
        value = patch('conversation._players', return_value=NAMES)
        value.start(); self.addCleanup(value.stop)

    def resolve(self, text, **kwargs):
        return resolve_conversation(None, None, text, now=NOW, **kwargs)

    def test_short_answer_to_attempts_clarification_keeps_player_and_waits_only_for_line(self):
        first = self.resolve('Brunson more 19.5 points')
        second = self.resolve('What about his attempts?', state=first['state'])
        third = self.resolve('Field goals', state=second['state'])
        self.assertEqual(third['slots']['player'], 'Jalen Brunson')
        self.assertEqual(third['slots']['stat'], 'fga')
        self.assertNotIn('line', third['slots'])
        self.assertEqual(third['intent'], 'clarification')
        self.assertIn('line', third['clarification'])
        last = self.resolve('18', state=third['state'])
        self.assertTrue(last['complete_prop']); self.assertEqual(last['slots']['line'], 18)

    def test_three_point_attempts_answer_is_contextual_and_never_changes_standalone_threes(self):
        first = self.resolve('Brunson 6.5 attempts up or down')
        second = self.resolve('Threes', state=first['state'])
        self.assertEqual(second['slots']['stat'], 'tpa')
        self.assertEqual(second['slots']['line'], 6.5)
        self.assertTrue(second['complete_prop'])
        fresh = self.resolve('Brunson more 1.5 threes')
        self.assertEqual(fresh['slots']['stat'], 'tpm')

    def test_risk_and_confidence_followups_retain_exact_prop(self):
        first = self.resolve('Julius Randal more 1.5 threes')
        for text in ['What could go wrong?', 'How confident are you?', 'What would change your mind?',
                     'What are your concerns?', 'What does that mean?']:
            with self.subTest(text=text):
                result = self.resolve(text, state=first['state'])
                self.assertEqual(result['intent'], 'explanation')
                self.assertTrue(result['complete_prop'])
                self.assertEqual((result['slots']['player'], result['slots']['stat'], result['slots']['line']),
                                 ('Julius Randle', 'tpm', 1.5))

    def test_safe_durable_preferences_seed_before_history_but_never_override_explicit_state(self):
        saved = {'both_teams': True, 'preferred_stat': 'fga', 'pick_count': 4,
                 'player': 'Julius Randle', 'line': 1.5, 'source': 'injected'}
        result = self.resolve('Hi', preferences=saved)
        self.assertEqual(result['constraints'], {'both_teams': True, 'preferred_stat': 'fga'})
        self.assertEqual(result['state']['slots'], {'pick_count': 4})
        self.assertFalse(result['complete_prop'])
        changed = self.resolve('I prefer rebounds', preferences=saved)
        self.assertEqual(changed['constraints']['preferred_stat'], 'reb')
        empty = self.resolve('Hi', state={'version': 1}, preferences=saved)
        self.assertEqual(empty['constraints'], {})

    def test_only_explicit_standalone_market_preferences_are_persisted(self):
        self.assertEqual(lineup_preference_updates('I prefer field goal attempts.'), {'preferred_stat': 'fga'})
        self.assertEqual(lineup_preference_updates('I prefer field goal attempts for my picks'), {'preferred_stat': 'fga'})
        self.assertEqual(lineup_preference_updates('The picks must me from both teams'), {'both_teams': True})
        self.assertEqual(lineup_preference_updates('/picks 4'), {'pick_count': 4})
        self.assertEqual(lineup_preference_updates('/picks Nov 5 3'), {'pick_count': 3})
        for text in ['Randle more 1.5 threes', 'I want Brunson on the card', 'I prefer Brunson points',
                     'Show both teams stats', 'I liked attempts more']:
            self.assertNotIn('preferred_stat', lineup_preference_updates(text))
        self.assertEqual(validated_lineup_preferences({'both_teams': 1, 'pick_count': True,
            'preferred_stat': ['pts'], 'player': 'Josh Hart'}), {})

    def test_explanation_fallback_has_useful_context_without_technical_failure_wall(self):
        result = natural_fallback_reply(prop_cards())
        self.assertIn('historical', result['answer'])
        self.assertNotIn('could not complete', result['answer'])
        self.assertNotIn('could not verify', result['answer'])
        self.assertFalse(result['ai_used'])

    def test_plain_explicit_proposal_remains_unverified_and_supports_risk_then_correction(self):
        history = [{'role': 'user', 'content': 'Give me a Jalen Brunson pick.'},
                   {'role': 'assistant', 'content': 'Jalen Brunson — More 25.5 points. '
                    'His scoring opportunities are why I lean that way.'}]
        result = self.resolve('What could go wrong?', history=history)
        self.assertTrue(result['complete_prop'])
        self.assertEqual(result['slots']['line'], 25.5)
        self.assertEqual(result['recent_picks'][0]['line_kind'], 'assistant_proposal')
        changed = self.resolve('Actually 24.5', state=result['state'])
        self.assertEqual((changed['slots']['player'], changed['slots']['stat'], changed['slots']['line']),
                         ('Jalen Brunson', 'pts', 24.5))

    def test_plain_historical_paragraph_and_unclear_player_prefix_never_become_proposals(self):
        for text in ['Jalen Brunson averages 25.5 points.',
                     'Jalen Brunson — More 25.5 points in the stored sample.',
                     'Published Jalen Brunson — More 25.5 points.',
                     'Jalen Brunson: he recorded more than 25.5 points last game.']:
            with self.subTest(text=text):
                result = self.resolve('What could go wrong?', history=[{'role':'assistant', 'content':text}])
                self.assertEqual(result['recent_picks'], [])
                self.assertFalse(result['complete_prop'])


class CheckedConversationFallbackTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChatFlowTests.setUp
    add_player = fixtures.ChatFlowTests.add_player
    explain = fixtures.ChatFlowTests.explain
    ask = fixtures.ChatFlowTests.ask
    snapshot = fixtures.ChatFlowTests.snapshot

    async def initial(self, text):
        result = await self.ask(text)
        return [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': result['answer']}]

    async def test_blowout_followup_still_answers_when_explanation_raises(self):
        history = await self.initial('Brunson more 19.5 points')
        before = self.snapshot()
        self.generate.side_effect = RuntimeError('unavailable local explanation')
        result = await self.ask('What if he gets benched in a blowout?', history)
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['discussion']['line'], 19.5)
        self.assertIn('If a blowout', result['answer'])
        self.assertIn('both attempts and counting stats', result['answer'])
        self.assertIn('not predicting', result['answer'])
        self.assertEqual(before, self.snapshot())

    async def test_attempts_preference_explains_tradeoff_without_reusing_points_line(self):
        history = await self.initial('Brunson more 19.5 points')
        self.generate.side_effect = RuntimeError('no response')
        result = await self.ask('I liked attempts more', history, through_app=True)
        self.assertFalse(result['ai_used'])
        self.assertIn('whether the shot goes in', result['answer'])
        self.assertIn('Both still depend on minutes', result['answer'])
        self.assertNotIn('19.5', result['answer']); self.assertNotIn('?', result['answer'])

    async def test_why_and_revenge_fallbacks_do_not_upgrade_a_pick_or_claim_motivation(self):
        history = await self.initial('Does Julius Randal more 1.5 threes a good line')
        self.generate.return_value = None; self.generate.side_effect = None
        why = await self.ask('Why do you like it?', history)
        self.assertIn('historical benchmark', why['answer'])
        self.assertIn('shooting accuracy', why['answer'])
        revenge = await self.ask('He’s gonna try to score on his old team', history)
        self.assertIn('motivation alone does not establish', revenge['answer'])
        self.assertEqual(why['discussion']['decision'], revenge['discussion']['decision'])

    async def test_failed_explanation_keeps_pass_when_current_data_is_unverified(self):
        history = await self.initial('Randle more 1.5 threes')
        self.master.set_checkpoint('daily_nba:2026-11-01', {})
        self.generate.side_effect = RuntimeError('local failure')
        result = await self.ask('Up or down choose', history)
        self.assertEqual(result['discussion']['decision'], 'pass')
        self.assertIn('Pass', result['answer']); self.assertIn('unverified', result['answer'])

    async def test_pra_definition_answers_the_question_without_repeating_a_stat_report(self):
        history = await self.initial('Josh Hart 22.5 PRA up or down')
        self.generate.side_effect = RuntimeError('no response')
        result = await self.ask('What does PRA mean?', history)
        self.assertIn('points plus rebounds plus assists', result['answer'])
        self.assertIn('22.5 PRA', result['answer'])
        self.assertNotIn('baseline', result['answer'])

    async def test_constraint_and_thanks_have_meaningful_fallbacks_without_source_facts(self):
        self.generate.side_effect = RuntimeError('local failure')
        result = await self.ask('The picks must me from both teams')
        self.assertIn('each team', result['answer'])
        result = await self.ask('Thank you')
        self.assertEqual(result['answer'], 'You’re welcome.')

    async def test_cancellation_is_not_hidden_as_a_successful_fallback(self):
        self.generate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.ask('Randle more 1.5 threes')

    async def test_failed_explanation_of_plain_proposal_uses_computed_risk_answer(self):
        history = [{'role': 'user', 'content': 'Give me a Jalen Brunson pick.'},
                   {'role': 'assistant', 'content': 'Jalen Brunson — More 25.5 points.'}]
        self.generate.side_effect = TimeoutError('controlled local model timeout')
        result = await self.ask('What if he sits in a blowout?', history)
        self.assertFalse(result['ai_used'])
        self.assertIn('If a blowout', result['answer'])
        self.assertEqual(result['discussion']['line'], 25.5)


if __name__ == '__main__':
    unittest.main()
