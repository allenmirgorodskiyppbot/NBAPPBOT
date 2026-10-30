"""Bounded generated-reply recovery with mocked transport, not live-model proof."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import app
import efficient_ai
from grounded_reply import evidence_cards, parse_reply, render_reply
from master import MasterStore
from test_grounded_reply import context


class GeneratedRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.cards = evidence_cards(context('Jalen Brunson', 'PHI'), 'Explain his scoring matchup')
        self.request = app.ChatRequest(message='How does Jalen Brunson match up against Philadelphia?')
        self.prior_cache = efficient_ai._CACHE.copy()
        efficient_ai._CACHE.clear()
        self.prior_state = dict(app.app.state._state)

        def restore():
            efficient_ai._CACHE.clear()
            efficient_ai._CACHE.update(self.prior_cache)
            app.app.state._state.clear()
            app.app.state._state.update(self.prior_state)

        self.addCleanup(restore)

    def client(self, handler):
        original = httpx.AsyncClient
        return patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw:
                            original(transport=httpx.MockTransport(handler), **kw))

    def body(self, analysis, **extra):
        return {'message': {'content': json.dumps({'evidence_ids': ['E3'],
                'analysis': analysis, 'needs_more_data': False})}, 'done_reason': 'stop', **extra}

    async def generate(self):
        return await app.generated_evidence_reply(self.request, {}, self.master, self.cards)

    async def test_basketball_words_generate_successfully_without_repair(self):
        calls = []
        analysis = 'The 76ers may change their coverage; his three-point shooting could affect scoring.'

        def reply(request):
            calls.append(request)
            return httpx.Response(200, json=self.body(analysis))

        with self.client(reply):
            result = await self.generate()
        self.assertIn(analysis, result['answer'])
        self.assertNotIn('ai_failure_category', result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(app.app.state.last_ai_request['model_calls'], 1)
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'], 0)

    async def test_numeric_misstatement_is_repaired_once_then_reused_for_original_request(self):
        wrong = 'Brunson averages 17.6 points against Philadelphia.'
        repaired = 'The season average covers all opponents. The stored pairing remains tentative.'
        calls = []

        def reply(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=self.body(wrong if len(calls) == 1 else repaired,
                prompt_eval_count=100, eval_count=20, load_duration=100000000,
                prompt_eval_duration=2000000000, eval_duration=3000000000))

        with self.client(reply):
            first = await self.generate()
            first_diagnostics = dict(app.app.state.last_ai_request)
            second = await self.generate()
        self.assertEqual(len(calls), 2)
        self.assertNotIn(wrong, first['answer'])
        self.assertIn(repaired, first['answer'])
        self.assertIn('all opponents', first['answer'])
        self.assertEqual(first, second)
        self.assertIn('numerical_prose', calls[1]['messages'][-1]['content'])
        self.assertNotIn(wrong, json.dumps(calls[1]['messages']))
        self.assertEqual(first_diagnostics['model_calls'], 2)
        self.assertEqual(first_diagnostics['repair_attempts'], 1)
        self.assertEqual(first_diagnostics['validation_reason'], 'numerical_prose')
        self.assertEqual(first_diagnostics['status'], 'completed')
        self.assertEqual(first_diagnostics['prompt_tokens'], 200)
        self.assertEqual(first_diagnostics['generated_tokens'], 40)
        self.assertEqual(first_diagnostics['model_load_seconds'], .2)
        self.assertEqual(first_diagnostics['prompt_eval_seconds'], 4)
        self.assertEqual(first_diagnostics['generation_seconds'], 6)
        self.assertEqual(app.app.state.last_ai_request['model_calls'], 0)
        self.assertTrue(app.app.state.last_ai_request['cache_hit'])

    async def test_double_rejection_is_never_cached_or_delivered_as_analysis(self):
        calls = []
        wrong = 'He averaged forty points against Philadelphia.'

        def reply(request):
            calls.append(request)
            return httpx.Response(200, json=self.body(wrong))

        with self.client(reply):
            first = await self.generate()
            second = await self.generate()
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(efficient_ai._CACHE), 0)
        for result in (first, second):
            self.assertEqual(result['ai_failure_category'], 'grounding_error')
            self.assertNotIn(wrong, result['answer'])
        self.assertEqual(app.app.state.last_ai_request['model_calls'], 2)
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'], 1)
        self.assertEqual(app.app.state.last_ai_request['validation_reason'], 'numerical_prose')

    async def test_legacy_rejected_cache_is_evicted_before_a_new_transport_request(self):
        calls = []
        payload = {'model': app.MODEL, 'messages': [{'role': 'user', 'content': 'Explain scoring'}]}
        bad = self.body('He averaged forty points.')
        good = self.body('The stored sample is limited.')
        key = efficient_ai.request_cache_key(payload, self.master)
        efficient_ai._CACHE[key] = (efficient_ai.time.monotonic(), json.dumps(bad))

        def validator(body):
            render_reply(parse_reply(body['message']['content']), self.cards)

        def reply(request):
            calls.append(request)
            return httpx.Response(200, json=good)

        details = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            result = await efficient_ai.efficient_chat(client, payload, self.master,
                diagnostics=details, validator=validator)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, good)
        self.assertFalse(details['cache_hit'])
        self.assertEqual(json.loads(efficient_ai._CACHE[key][1]), good)

    async def test_timeout_http_and_output_limit_do_not_start_repair(self):
        cases = [('timeout', None), ('model_http_503', httpx.Response(503)),
                 ('output_limit', httpx.Response(200, json={'message': {'content': '{'}, 'done_reason': 'length'}))]
        for expected, response in cases:
            with self.subTest(expected=expected):
                calls = []

                def reply(request):
                    calls.append(request)
                    if expected == 'timeout':
                        raise httpx.ReadTimeout('private detail', request=request)
                    return response

                with self.client(reply):
                    result = await self.generate()
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['ai_failure_category'], expected)
                self.assertEqual(app.app.state.last_ai_request['repair_attempts'], 0)
                self.assertEqual(app.app.state.last_ai_request['model_calls'], 1)
                self.assertNotIn('private detail', result['answer'])

    async def test_single_total_deadline_cancels_pending_repair(self):
        calls = []
        cancelled = asyncio.Event()
        original_timeout = asyncio.timeout

        async def reply(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json=self.body('He scores twenty points.'))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        # Accelerate the real outer cancellation context, retaining the normal
        # repair-budget calculation so this exercises cancellation mid-repair.
        with patch.object(app.asyncio, 'timeout', side_effect=lambda seconds: original_timeout(.03)), self.client(reply):
            result = await self.generate()
        self.assertEqual(len(calls), 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result['ai_failure_category'], 'timeout')
        details = app.app.state.last_ai_request
        self.assertEqual(details['repair_attempts'], 1)
        self.assertEqual(details['model_calls'], 2)
        self.assertLess(details['elapsed_seconds'], 1)
        self.assertEqual(details['status'], 'failed')

    async def test_insufficient_remaining_budget_does_not_attempt_repair(self):
        calls = []

        def reply(request):
            calls.append(request)
            return httpx.Response(200, json=self.body('He scores twenty points.'))

        with patch.object(app, 'COMPACT_AI_TIMEOUT_SECONDS', 29), self.client(reply):
            result = await self.generate()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['ai_failure_category'], 'grounding_error')
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'], 0)

    async def test_long_social_message_stays_on_compact_generated_route(self):
        question = ('Thanks for explaining that. I am learning how to read basketball research and '
                    'appreciate the patience. I will come back after I have had some breakfast and '
                    'then we can continue our conversation.')
        self.assertGreater(len(question), 160)
        calls = []
        phrase = 'You are welcome. Enjoy breakfast, and we can continue whenever you are ready.'

        def reply(request):
            payload = json.loads(request.content)
            calls.append(payload)
            return httpx.Response(200, json={'message': {'content': json.dumps({
                'evidence_ids': [], 'analysis': phrase, 'needs_more_data': False})}, 'done_reason': 'stop'})

        with self.client(reply):
            result = await app.answer_question(None, app.ChatRequest(message=question), {}, self.master)
        self.assertEqual(result['answer'], phrase)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('tools', calls[0])
        self.assertLessEqual(calls[0]['options']['num_ctx'], 4096)


class CompactHistoryTests(unittest.TestCase):
    def test_tail_interpretation_survives_long_source_preface(self):
        interpretation = 'The comparison remains uncertain because the sample is small.'
        prior = 'Source evidence. ' * 70 + '\n\n' + interpretation
        history = [app.Message(role='user', content='Explain this matchup.'),
                   app.Message(role='assistant', content=prior)]
        compact = app.compact_conversation(history)
        self.assertIn(interpretation, compact[-1]['content'])
        self.assertIn('Source evidence.', compact[-1]['content'])
        self.assertLess(len(compact[-1]['content']), 500)
        self.assertIn('omitted', compact[-1]['content'])


if __name__ == '__main__':
    unittest.main()
