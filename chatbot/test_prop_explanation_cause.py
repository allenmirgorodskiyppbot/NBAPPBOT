"""A descriptive hit split never replaces the application's actual hold reason."""
import unittest
from unittest.mock import AsyncMock, patch

import app
from chat_flow import _pick_model_evidence, _sample_interpretation
from pick_discussion import discussion_evidence
import test_compact_fact_rendering as fixtures


BAD = ('Historical mean and median exceed the line, indicating consistent performance above it. '
       'Recent outcomes show balanced results, supporting current hold.')
SAMPLE_CAUSE = 'Recent outcomes show balanced results, supporting current hold.'
GOOD = ('Pass means waiting for game-day verification. The historical mean and median are above '
        'the line, but those centers do not establish consistent performance.')


class PropExplanationCauseTests(unittest.IsolatedAsyncioTestCase):
    add_player = fixtures.CompactFactRenderingTests.add_player
    explain = fixtures.CompactFactRenderingTests.explain
    snapshot = fixtures.CompactFactRenderingTests.snapshot
    model_response = fixtures.CompactFactRenderingTests.model_response

    def setUp(self):
        fixtures.CompactFactRenderingTests.setUp(self)
        self.report = {
            'player':'Jalen Brunson', 'stat':'pra', 'line':30, 'team':'NYK',
            'season':'2026-27', 'season_type':'Regular Season', 'decision':'pass',
            'reason':'Pass for now: A current game-day refresh is required; future dates remain historical research.',
            'data_readiness':{'ready':False},
            'conditional_risks':['Playing time can change.', 'This is a research benchmark.'],
            'sample':{'n':8, 'more':5, 'less':3, 'ties':0, 'mean':32.75, 'median':33,
                      'values':[34,38,32,35,26,29,40,28], 'last_date':'2026-11-04'},
            'baseline':{'available':True, 'n':20, 'reference_projection':35,
                        'baseline_median':35, 'seasons':['2025-26','2026-27'], 'last_date':'2026-11-04'},
            'workload':{'reference_minutes':None}}
        self.report['evidence_cards'] = discussion_evidence(self.report)
        self.brief = _pick_model_evidence(self.report)

    async def ask(self, paragraphs):
        values = iter(paragraphs)
        latest = paragraphs[-1]
        async def respond(*args, **kwargs):
            nonlocal latest
            latest = next(values, latest)
            self.model_reply = latest
            return await self.model_response(*args, **kwargs)
        with patch('efficient_ai.efficient_chat', AsyncMock(side_effect=respond)):
            return await app.generated_evidence_reply(
                app.ChatRequest(message='Why pass?'), {}, self.master,
                self.report['evidence_cards'], natural=True,
                dialogue={'_compact':True, '_brief_evidence':self.brief,
                          'intent':'explanation', 'decision':'pass'},
                fallback='Pass for now: A current game-day refresh is required.')

    async def test_exact_live_paragraph_and_its_standalone_wrong_cause_do_not_escape(self):
        for text in (BAD, SAMPLE_CAUSE, 'I am holding because the recent sample is mixed.'):
            with self.subTest(text=text):
                self.payloads.clear()
                result = await self.ask([text])
                self.assertFalse(result['ai_used'])
                self.assertEqual(len(self.payloads), 2)
                self.assertNotIn(text, result['answer'])
                self.assertIn('game-day refresh', result['answer'])
        self.network.assert_not_awaited()

    async def test_invalid_cause_is_repaired_to_the_actual_verification_reason(self):
        result = await self.ask([SAMPLE_CAUSE, GOOD])
        self.assertEqual(len(self.payloads), 2)
        self.assertNotIn('ai_failure_category', result)
        self.assertIn(GOOD, result['answer'])
        self.assertIn('actual hold reason', self.payloads[0]['messages'][0]['content'])

    async def test_descriptions_negated_causes_and_conditional_mechanisms_remain_valid(self):
        for text in (
            GOOD,
            'Recent results were mixed, but the hold is because game-day verification is missing.',
            'A mixed sample does not cause this hold. The future game requires current verification.',
            'If future results were split evenly, that could support a hold under a different strategy.',
        ):
            with self.subTest(text=text):
                self.payloads.clear()
                result = await self.ask([text])
                self.assertEqual(len(self.payloads), 1)
                self.assertNotIn('ai_failure_category', result)
                self.assertIn(text, result['answer'])

    async def test_actual_source_brief_preserves_majority_and_decision_roles(self):
        sample = next(item for item in self.brief if item['kind']=='prop_sample')
        assessment = next(item for item in self.brief if item['kind']=='prop_assessment')
        self.assertIn('more observed results were above', sample['text'].lower())
        self.assertNotIn('balanced', sample['text'].lower())
        self.assertEqual(sample['decision_role'], 'context_only')
        self.assertEqual(assessment['decision_role'], 'actual_decision_reason')
        self.assertEqual(assessment['reason_basis'], 'game_day_verification')
        self.assertIn(self.report['reason'][:180], assessment['text'])

    async def test_opposite_equal_and_tied_samples_keep_their_distinct_meanings(self):
        self.assertIn('below the line than above', _sample_interpretation({'more':3,'less':5,'ties':0}))
        self.assertIn('counts were equal', _sample_interpretation({'more':4,'less':4,'ties':1}))
        self.assertIn('tied the line', _sample_interpretation({'more':0,'less':0,'ties':8}))

    async def test_count_guard_does_not_override_baseline_or_untyped_legacy_reasoning(self):
        self.assertFalse(app._unsupported_sample_hold_reason(SAMPLE_CAUSE, None))
        self.assertFalse(app._unsupported_sample_hold_reason(SAMPLE_CAUSE, []))
        self.assertFalse(app._unsupported_sample_hold_reason(
            'The historical mean and median give mixed signals, which supports a Pass.', self.brief))


if __name__ == '__main__':
    unittest.main()
