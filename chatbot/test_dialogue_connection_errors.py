"""Safe provider failures survive planning, without paid requests or lost facts."""
import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from dialogue_model import DialogueModelError, invoke_dialogue
from general_dialogue import answer_general_question
from semantic_plan import infer_plan


PRIVATE = 'synthetic-private-key-do-not-leak'


class DialogueConnectionErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': PRIVATE})
        env.start(); self.addCleanup(env.stop)
        self.request = SimpleNamespace(message='Explain variance in plain language.', history=[])

    async def answer_through_transport(self, handler):
        calls = []
        def record(request):
            calls.append(request)
            return handler(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            async def provider(payload):
                return await invoke_dialogue(payload, client=client)
            async def infer(request, **kwargs):
                return await infer_plan(request, invoke=provider, **kwargs)
            progress = {}
            result = await answer_general_question(None, self.request, progress, turn={}, infer=infer)
        return result, progress, calls

    async def test_http_failures_are_connection_diagnostics_not_requests_to_rephrase(self):
        for status, category in ((401, 'access_refused'), (403, 'access_refused'),
                                 (429, 'rate_limited'), (500, 'provider_error')):
            with self.subTest(status=status):
                result, progress, calls = await self.answer_through_transport(lambda request:
                    httpx.Response(status, json={'error': {'message': PRIVATE + ' private provider body'}}))
                self.assertEqual(result['ai_failure_category'], category)
                self.assertEqual(result['response_mode'], 'general_connection_error')
                self.assertEqual(progress['dialogue_failure_category'], category)
                self.assertNotIn('restate', result['answer'].lower())
                self.assertNotIn(PRIVATE, json.dumps([result, progress]))
                self.assertNotIn('private provider body', str(result))
                self.assertEqual(len(calls), 1)

    async def test_timeout_is_preserved_without_private_exception_details(self):
        def handler(request):
            raise httpx.ReadTimeout(PRIVATE + ' private request', request=request)
        result, progress, calls = await self.answer_through_transport(handler)
        self.assertEqual(result['ai_failure_category'], 'timeout')
        self.assertEqual(progress['dialogue_failure_category'], 'timeout')
        self.assertIn('timed out', result['answer'])
        self.assertNotIn(PRIVATE, str(result))
        self.assertEqual(len(calls), 1)

    async def test_missing_key_does_not_make_a_request(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            result, progress, calls = await self.answer_through_transport(lambda request:
                self.fail('Configuration failure must precede HTTP'))
        self.assertEqual(result['ai_failure_category'], 'missing_or_invalid_api_key')
        self.assertIn('not configured', result['answer'])
        self.assertEqual(calls, [])

    async def test_unreadable_provider_response_is_not_an_interpretation_error(self):
        result, _, _ = await self.answer_through_transport(lambda request:
            httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{'}}]}))
        self.assertEqual(result['ai_failure_category'], 'incomplete_response')
        self.assertIn('incomplete', result['answer'])

    async def test_synthesis_failure_keeps_already_computed_facts_and_sources(self):
        plan = {'goal': 'explain', 'reads': [{'tool': 'player_games', 'args': {
            'player': 'Jalen Brunson', 'stat': 'reb', 'window': 5}}], 'reply': '', 'clarification': None}
        fact = 'Jalen Brunson: mean 9 rebounds in this saved sample.'
        read = AsyncMock(return_value={'tool': 'player_games', 'args': plan['reads'][0]['args'],
            'available': True, 'answer': fact, 'scope': 'Synthetic saved sample', 'sources': ['synthetic source'],
            'missing': [], 'cards': [{'id': 'R1', 'kind': 'saved_observations', 'entity': 'Jalen Brunson',
                'source': 'synthetic source', 'text': fact, 'approved_facts': [{'text': fact,
                    'entity': 'Jalen Brunson', 'metric': 'rebounds', 'fact_type': 'average', 'values': [9]}]}]})
        result = await answer_general_question(None, self.request, {}, turn={},
            infer=AsyncMock(return_value=plan), read=read,
            synthesize=AsyncMock(side_effect=DialogueModelError('rate_limited')))
        self.assertIn(fact, result['answer'])
        self.assertEqual(result['sources'], ['synthetic source'])
        self.assertEqual(result['response_mode'], 'general_facts')
        self.assertEqual(result['ai_failure_category'], 'rate_limited')
        self.assertNotIn(PRIVATE, str(result))

    async def test_stale_or_unrecognized_categories_cannot_become_user_visible_errors(self):
        progress = {'dialogue_failure_category': PRIVATE}
        result = await answer_general_question(None, self.request, progress, turn={},
            infer=AsyncMock(return_value=None))
        self.assertIsNone(result)
        self.assertNotIn('dialogue_failure_category', progress)
        with self.assertLogs('knicks.general_dialogue', level='INFO') as logs:
            result = await answer_general_question(None, self.request, {}, turn={},
                infer=AsyncMock(side_effect=DialogueModelError(PRIVATE)))
        self.assertIsNone(result)
        self.assertNotIn(PRIVATE, '\n'.join(logs.output))

    async def test_cancellation_remains_cancellation(self):
        with self.assertRaises(asyncio.CancelledError):
            await answer_general_question(None, self.request, {}, turn={},
                infer=AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_malformed_plan_does_not_become_a_connection_failure(self):
        async def infer(request, **kwargs):
            return await infer_plan(request, invoke=AsyncMock(return_value='not JSON'), **kwargs)
        progress = {}
        result = await answer_general_question(None, self.request, progress, turn={}, infer=infer)
        self.assertIsNone(result)
        self.assertNotIn('dialogue_failure_category', progress)
