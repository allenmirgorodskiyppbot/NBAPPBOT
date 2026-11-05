"""Compact prop prose compares historical centers with the actual offered line."""
import unittest
from unittest.mock import AsyncMock, patch

import app
import test_compact_fact_rendering as fixtures
from chat_flow import _pick_model_evidence
from pick_discussion import build_pick_discussion


BAD=('Historical data shows the stat above median and mean, but no verified schedule confirmation exists. '
     'A real-time check is required before final assessment.')
GOOD=('The historical mean and median are above the offered line. '
      'Pass remains a hold because the current game checks are unverified.')


class PropComparisonLanguageTests(unittest.IsolatedAsyncioTestCase):
    add_player=fixtures.CompactFactRenderingTests.add_player
    explain=fixtures.CompactFactRenderingTests.explain
    snapshot=fixtures.CompactFactRenderingTests.snapshot
    model_response=fixtures.CompactFactRenderingTests.model_response

    def setUp(self):
        fixtures.CompactFactRenderingTests.setUp(self)
        self.report=build_pick_discussion(self.master,{'player':'Jalen Brunson','stat':'pts','line':22.5,
            'side':'more','game_date':'2026-11-07'},now=fixtures.fixtures.NOW)
        self.assertTrue(self.report['available'])
        self.assertEqual(self.report['decision'],'pass')
        self.assertGreater(self.report['baseline']['reference_projection'],self.report['line'])
        self.assertGreater(self.report['baseline']['baseline_median'],self.report['line'])
        self.brief=_pick_model_evidence(self.report)

    async def ask(self,replies):
        values=iter(replies);latest=replies[-1]
        async def response(*args,**kwargs):
            nonlocal latest
            latest=next(values,latest);self.model_reply=latest
            return await self.model_response(*args,**kwargs)
        with patch('efficient_ai.efficient_chat',AsyncMock(side_effect=response)):
            return await app.generated_evidence_reply(app.ChatRequest(message='Why this pick?'),{},self.master,
                self.report['evidence_cards'],natural=True,dialogue={
                    '_compact':True,'_brief_evidence':self.brief,'intent':'explanation',
                    'slots':{'player':'Jalen Brunson','stat':'pts','line':22.5},'decision':'pass'},
                fallback=self.report['fallback_answer'])

    async def test_actual_bad_live_paragraph_is_rejected_and_cannot_change_the_checked_fallback(self):
        before=self.snapshot()
        result=await self.ask([BAD])
        self.assertEqual(len(self.payloads),2)
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['answer'],self.report['fallback_answer'])
        self.assertNotIn('the stat above median and mean',result['answer'])
        self.assertEqual(self.snapshot(),before)
        self.assertIn('historical mean/median with the offered line',self.payloads[0]['messages'][0]['content'])
        self.assertIn('actual hold reason',self.payloads[0]['messages'][0]['content'])
        self.network.assert_not_awaited()

    async def test_bad_comparison_can_be_repaired_to_a_valid_mean_to_line_comparison(self):
        result=await self.ask([BAD,GOOD])
        self.assertEqual(len(self.payloads),2)
        self.assertNotIn('ai_failure_category',result)
        self.assertIn(GOOD,result['answer'])
        self.assertNotIn('the stat above median and mean',result['answer'])
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'],1)

    async def test_valid_varied_comparisons_and_individual_outcomes_remain_available(self):
        variants=[GOOD,
            'The offered line is below the historical mean and median. Current game checks still require a hold.',
            'The historical average is higher than the posted line, but the missing checks still matter.',
            'The posted line sits lower than both historical mean and median. Pass means holding off.',
            'Individual games can finish above or below their historical mean. The current game checks remain unverified.',
            'The recent sample includes results above and below its mean. This does not establish a future result.']
        for text in variants:
            with self.subTest(text=text):
                self.payloads.clear();result=await self.ask([text])
                self.assertEqual(len(self.payloads),1)
                self.assertNotIn('ai_failure_category',result)
                self.assertIn(text,result['answer'])

    async def test_clear_direction_reversals_do_not_pass_just_because_they_have_no_digits(self):
        for text in ['The historical mean and median are below the offered line.',
                     'The offered line is above the historical mean and median.']:
            with self.subTest(text=text):
                self.payloads.clear();result=await self.ask([text])
                self.assertEqual(len(self.payloads),2)
                self.assertFalse(result['ai_used'])
                self.assertEqual(result['answer'],self.report['fallback_answer'])

    async def test_parsed_analysis_field_enforces_qualitative_mode_even_for_supported_numbers(self):
        # This is an approved value, so the legacy numerical fact checker alone
        # would accept it. Compact mode must inspect parse_reply's internal
        # analysis field and leave the quantity to the application renderer.
        median=self.report['sample']['median']
        result=await self.ask([f"Pass: Jalen Brunson's sample median is {median:g} points."])
        self.assertEqual(len(self.payloads),2)
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['answer'],self.report['fallback_answer'])
        self.assertEqual(app.app.state.last_ai_request['validation_reason'],
                         'numerical_prose')


if __name__=='__main__':unittest.main()
