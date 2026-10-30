"""Mocked model transport tests; these do not measure real Akash AI latency."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import app
import efficient_ai
from grounded_reply import evidence_cards
from master import MasterStore
from test_grounded_reply import context


class MockedModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master = MasterStore(Path(self.directory.name) / 'master.sqlite')
        prior_state = dict(app.app.state._state)
        prior_cache = efficient_ai._CACHE.copy()

        def restore():
            app.app.state._state.clear()
            app.app.state._state.update(prior_state)
            efficient_ai._CACHE.clear()
            efficient_ai._CACHE.update(prior_cache)

        self.addCleanup(restore)
        efficient_ai._CACHE.clear()
        app.app.state.phase = 'ready'
        app.app.state.chat_lock = asyncio.Lock()
        app.app.state.ai_started_at = None
        app.app.state.last_ai_failure = {}
        app.app.state.last_ai_success = None
        app.app.state.last_ai_request = None

    def client(self, handler):
        original = httpx.AsyncClient
        return patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw:
                            original(transport=httpx.MockTransport(handler), **kw))

    async def test_empty_warmup_loads_same_context_and_keeps_model_resident(self):
        calls = []

        def reply(request):
            calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={'done': True, 'done_reason': 'load',
                                            'load_duration': 2500000000})

        with self.client(reply):
            timing = await app.warm_model()
        self.assertEqual(len(calls), 1)
        path, payload = calls[0]
        self.assertEqual(path, '/api/generate')
        self.assertEqual(payload['prompt'], '')
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['keep_alive'], -1)
        self.assertEqual(payload['options']['num_ctx'], 4096)
        self.assertEqual(payload['options']['num_ctx'], app.model_options()['num_ctx'])
        self.assertEqual(payload['options']['num_thread'], app.model_options()['num_thread'])
        self.assertEqual(payload['options']['num_predict'], 1)
        self.assertEqual(timing, {'model_load_seconds': 2.5, 'finish_reason': 'load'})

    async def test_warmup_rejects_http_error_and_incomplete_load(self):
        with self.client(lambda request: httpx.Response(503, text='private server detail')):
            with self.assertRaises(httpx.HTTPStatusError):
                await app.warm_model()
        with self.client(lambda request: httpx.Response(200, json={'done': False})):
            with self.assertRaises(app.ModelReplyError) as caught:
                await app.warm_model()
        self.assertEqual(caught.exception.category, 'invalid_model_response')

    async def test_timeout_has_truthful_message_and_scoped_evidence_without_retry(self):
        calls = []

        def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout('private upstream detail', request=request)

        cards = evidence_cards(context(), 'Explain scoring')
        with self.client(timeout):
            result = await app.generated_evidence_reply(
                app.ChatRequest(message='Explain scoring'), {}, self.master, cards)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['ai_failure_category'], 'timeout')
        self.assertFalse(result['ai_used'])
        self.assertTrue(result['answer'].startswith('The AI request timed out.'))
        self.assertNotIn('could not verify', result['answer'])
        self.assertIn('all opponents', result['answer'])
        self.assertIn('coverage is incomplete', result['answer'])
        self.assertNotIn('private upstream detail', result['answer'])
        self.assertEqual(app.app.state.last_ai_request['status'], 'failed')
        self.assertEqual(app.app.state.last_ai_request['failure_category'], 'timeout')
        self.assertEqual(app.app.state.last_ai_request['cache_hit'], False)

    async def test_outer_deadline_cancels_pending_transport_and_records_failure(self):
        calls = []
        cancelled = asyncio.Event()

        async def pending(request):
            calls.append(request)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(app, 'COMPACT_AI_TIMEOUT_SECONDS', .02), self.client(pending):
            result = await app.generated_evidence_reply(
                app.ChatRequest(message='Explain scoring'), {}, self.master,
                evidence_cards(context(), 'Explain scoring'))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['ai_failure_category'], 'timeout')
        diagnostics = app.app.state.last_ai_request
        self.assertEqual(diagnostics['status'], 'failed')
        self.assertEqual(diagnostics['failure_category'], 'timeout')
        self.assertGreaterEqual(diagnostics['elapsed_seconds'], 0)
        self.assertLess(diagnostics['elapsed_seconds'], 1)
        usage = {r['kind']: r for r in efficient_ai.usage_status(self.master)['inference']}
        self.assertEqual(usage['cancelled_request']['calls'], 1)

    async def test_http_error_and_output_limit_are_distinguished_from_grounding(self):
        examples = [
            (httpx.Response(503, text='private server error'), 'model_http_503', 'HTTP 503'),
            (httpx.Response(200, json={'message': {'content': '{"evidence_ids":'},
                                      'done_reason': 'length'}), 'output_limit', 'output limit'),
        ]
        for response, category, text in examples:
            with self.subTest(category=category):
                calls = []

                def reply(request):
                    calls.append(request)
                    return response

                with self.client(reply):
                    result = await app.generated_evidence_reply(
                        app.ChatRequest(message='Explain scoring'), {}, self.master,
                        evidence_cards(context(), 'Explain scoring'))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['ai_failure_category'], category)
                self.assertIn(text, result['answer'])
                self.assertNotIn('could not verify', result['answer'])
                self.assertNotIn('private server error', result['answer'])

    async def test_structured_reply_keeps_sources_cache_and_safe_runtime_diagnostics(self):
        calls = []
        secret_marker = 'private-input-marker'
        question = 'Explain scoring ' + secret_marker
        cards = evidence_cards(context(), question)
        source = next(c for c in cards if c['kind'] == 'player_all_opponent_averages')
        analysis = 'The stored season average is a baseline; a confirmed role and lineup would help interpret this matchup.'

        def reply(request):
            payload = json.loads(request.content)
            calls.append(payload)
            return httpx.Response(200, json={
                'message': {'content': json.dumps({'evidence_ids': [source['id']],
                            'analysis': analysis, 'needs_more_data': False})},
                'done_reason': 'stop', 'load_duration': 120000000,
                'prompt_eval_duration': 2300000000, 'eval_duration': 4500000000,
                'prompt_eval_count': 700, 'eval_count': 65,
                'private_debug': 'upstream-secret-marker'})

        request = app.ChatRequest(message=question)
        with self.client(reply):
            first = await app.generated_evidence_reply(request, {}, self.master, cards)
            first_diagnostics = dict(app.app.state.last_ai_request)
            second = await app.generated_evidence_reply(request, {}, self.master, cards)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first['sources'][0]['source'], source['source'])
        self.assertIn('all opponents', first['answer'])
        self.assertIn(analysis, first['answer'])
        payload = calls[0]
        self.assertEqual(payload['keep_alive'], -1)
        self.assertEqual(payload['options']['num_ctx'], app.model_options()['num_ctx'])
        model_cards = json.loads(payload['messages'][-1]['content'])['evidence']
        self.assertTrue(model_cards)
        self.assertTrue(all(set(c) == {'id', 'kind', 'text'} for c in model_cards))
        self.assertNotIn(source['source'], json.dumps(model_cards))
        self.assertEqual(first_diagnostics['cache_hit'], False)
        self.assertEqual(first_diagnostics['model_load_seconds'], .12)
        self.assertEqual(first_diagnostics['prompt_eval_seconds'], 2.3)
        self.assertEqual(first_diagnostics['generation_seconds'], 4.5)
        self.assertEqual(first_diagnostics['prompt_tokens'], 700)
        self.assertEqual(first_diagnostics['generated_tokens'], 65)
        self.assertEqual(app.app.state.last_ai_request['cache_hit'], True)
        self.assertEqual(app.app.state.last_ai_request['status'], 'completed')

        status = await app.ai_status()
        serialized = json.dumps(status)
        for private_text in (secret_marker, analysis, 'upstream-secret-marker', source['source']):
            self.assertNotIn(private_text, serialized)
        self.assertNotIn('messages', serialized)
        self.assertNotIn('evidence_ids', serialized)
        self.assertIn('prompt_chars', status['last_request'])

    async def test_model_timing_allowlist_ignores_untrusted_and_nonfinite_values(self):
        diagnostics = app.model_timings({'load_duration': float('nan'),
            'prompt_eval_duration': float('inf'), 'eval_duration': -1,
            'prompt_eval_count': True, 'eval_count': '123', 'done_reason': 'private text',
            'message': {'content': 'private model content'}, 'token': 'private token'})
        self.assertEqual(diagnostics, {})


if __name__ == '__main__':
    unittest.main()
