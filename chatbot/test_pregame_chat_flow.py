"""Real stored-data explanations keep every proposed leg and its game identity."""
import unittest
from unittest.mock import patch
import datetime as dt

import app
import test_chat_flow as fixtures
from chat_flow import answer_conversation


class RuntimeReadinessClockTests(unittest.TestCase):
    def test_post_generation_recheck_uses_the_actual_runtime_clock(self):
        from chat_flow import _changed_readiness
        self.assertIsNone(_changed_readiness(None, []))


class PregameChatFlowTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChatFlowTests.setUp
    add_player = fixtures.ChatFlowTests.add_player
    explain = fixtures.ChatFlowTests.explain
    snapshot = fixtures.ChatFlowTests.snapshot
    ask = fixtures.ChatFlowTests.ask

    def turn(self, picks, **slots):
        return {'intent':'explanation', 'slots':slots, 'constraints':{},
                'recent_picks':picks, 'complete_prop':False}

    async def run_turn(self, picks, **slots):
        before = self.snapshot()
        result = await answer_conversation(None, self.master,
            app.ChatRequest(message='Why these picks?'), {}, self.generate,
            turn=self.turn(picks, **slots))
        self.assertEqual(before, self.snapshot())
        return result

    async def test_mixed_research_and_priced_legs_both_get_explained(self):
        self.add_player('Josh Hart', 'NYK', [17,19,23,21,18,19,20,22], 'pra')
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line_kind':'research_target'},
            {'player':'Julius Randle','stat':'tpm','line':1.5,'side':'more'}],
            game_date='2026-11-02', opponent='BKN')
        generated = self.generated[-1]
        self.assertEqual({c['entity'] for c in generated['cards']}, {'Josh Hart','Julius Randle'})
        self.assertEqual({a['player'] for a in generated['dialogue']['assessments']},
                         {'Josh Hart','Julius Randle'})
        research = next(c for c in generated['cards'] if c['entity']=='Josh Hart')
        self.assertEqual(research['line_kind'], 'research_target')
        self.assertFalse(research['offer_verified'])
        priced = next(c for c in generated['cards'] if c['entity']=='Julius Randle')
        self.assertEqual(priced['decision'], 'more')
        self.assertIn('hypothetical', generated['dialogue']['action'])
        self.assertIn('Julius Randle', result['answer'])

    async def test_missing_priced_leg_does_not_disappear_beside_supported_leg(self):
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line':22.5,'side':'less'},
            {'player':'Unobserved Player','stat':'pts','line':20.5,'side':'more'}])
        held = next(c for c in self.generated[-1]['cards'] if c['entity']=='Unobserved Player')
        self.assertEqual(held['decision'], 'pass')
        self.assertIn('Unobserved Player', result['answer'])
        self.assertIn('Hold.', result['answer'])

    async def test_failed_research_leg_does_not_hide_behind_valid_priced_leg(self):
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line_kind':'research_target'},
            {'player':'Julius Randle','stat':'tpm','line':1.5,'side':'more'}])
        cards = self.generated[-1]['cards']
        self.assertEqual({c['entity'] for c in cards}, {'Josh Hart','Julius Randle'})
        self.assertEqual(next(c for c in cards if c['entity']=='Josh Hart')['decision'], 'pass')
        self.assertIn('recompute', result['answer'])
        self.assertIn('Julius Randle', result['answer'])

    async def test_changed_date_cannot_relabel_saved_offer_in_older_state(self):
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line':22.5,'side':'less','date':'2026-11-02'}],
            game_date='2026-11-04')
        self.assertEqual(self.generated[-1]['cards'][0]['decision'], 'pass')
        self.assertIn('different game date', result['answer'])
        self.assertNotIn('above in', result['answer'].lower())

    async def test_generation_omitting_a_leg_returns_complete_computed_explanation(self):
        self.generate.side_effect = None
        self.generate.return_value = {'answer':'Josh Hart is the selected pick.', 'sources':[]}
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line':22.5,'side':'less'},
            {'player':'Julius Randle','stat':'tpm','line':1.5,'side':'more'}])
        self.assertFalse(result['ai_used'])
        self.assertIn('Josh Hart', result['answer'])
        self.assertIn('Julius Randle', result['answer'])

    async def test_unambiguous_surnames_do_not_discard_a_complete_generated_explanation(self):
        self.generate.side_effect = None
        answer = 'Hart: the stored workload supports that cautious comparison. Randle: shooting accuracy remains a risk.'
        self.generate.return_value = {'answer':answer, 'sources':[]}
        result = await self.run_turn([
            {'player':'Josh Hart','stat':'pra','line':22.5,'side':'less'},
            {'player':'Julius Randle','stat':'tpm','line':1.5,'side':'more'}])
        self.assertEqual(result['answer'], answer)

    async def test_unverified_refresh_cannot_leave_the_computed_pass_implicit(self):
        self.master.set_checkpoint('daily_nba:2026-11-01', {})
        self.generate.side_effect = None
        self.generate.return_value = {'answer':'Julius Randle has recorded attempts, but coverage is unverified.', 'sources':[]}
        result = await self.ask('Julius Randle more 1.5 threes', through_app=True)
        self.assertEqual(result['discussion']['decision'], 'pass')
        self.assertTrue(result['answer'].startswith('Pass for now.'))

    async def test_tipoff_during_generation_holds_the_previously_eligible_pick(self):
        async def late_answer(*args, **kwargs):
            clock = patch('pick_discussion._now', return_value=dt.datetime(2026,11,3,0,31,tzinfo=dt.timezone.utc))
            clock.start()
            self.addCleanup(clock.stop)
            return {'answer':'Julius Randle: tentative More based on the supplied sample.', 'sources':[]}
        self.generate.side_effect = late_answer
        result = await self.ask('Julius Randle more 1.5 threes', through_app=True)
        self.assertEqual(result['discussion']['decision'], 'pass')
        self.assertFalse(result['ai_used'])
        self.assertIn('readiness changed', result['answer'])

    async def test_six_leg_explanation_keeps_every_leg_without_oversized_ai_request(self):
        picks = [{'player':f'Fixture Player {i}','stat':'pts','line':19.5,'side':'more'}
                 for i in range(6)]
        result = await self.run_turn(picks)
        self.generate.assert_not_awaited()
        for pick in picks:
            self.assertIn(pick['player'], result['answer'])

    async def test_second_pick_and_line_correction_use_only_selected_leg(self):
        history = [{'role':'assistant','content':'My shortlist\n'
            '1. Josh Hart — Less 22.5 PTS+REB+AST (standard offer)\n'
            '2. Julius Randle — More 1.5 3-pointers made (Demon offer)'}]
        result = await self.ask('Why the second one?', history, through_app=True)
        self.assertEqual(result['discussion']['player'], 'Julius Randle')
        self.assertEqual(result['discussion']['stat'], 'tpm')
        self.assertEqual(self.generated[-1]['dialogue']['selected_pick']['position'], 2)
        history += [{'role':'user','content':'Why the second one?'},
                    {'role':'assistant','content':result['answer']}]
        corrected = await self.ask('Actually 2.5, not 1.5', history, through_app=True)
        self.assertEqual(corrected['discussion']['player'], 'Julius Randle')
        self.assertEqual(corrected['discussion']['line'], 2.5)


if __name__ == '__main__':
    unittest.main()
