"""Explicit hosted chat is private and independent of local-model startup."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app
from master import MasterStore


class GeneralBackendRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.master = MasterStore(Path(temp.name) / 'master.sqlite')
        self.state = SimpleNamespace(master=self.master, store=None, phase='model',
            detail='Local model is still loading', chat_lock=asyncio.Lock(), ai_started_at=None)
        state = patch.object(app.app, 'state', self.state)
        state.start(); self.addCleanup(state.stop)
        env = patch.dict(os.environ, {'DIALOGUE_BACKEND': 'ollama', 'OPENAI_API_KEY': ''})
        env.start(); self.addCleanup(env.stop)

    async def test_key_alone_does_not_enable_hosted_chat(self):
        self.assertFalse(app.conversation_ready(self.state))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'private-test-value'}):
            self.assertFalse(app.conversation_ready(self.state))
            self.assertEqual((await app.ai_status())['conversation_engine']['backend'], 'ollama')
        self.state.phase = 'ready'
        self.assertTrue(app.conversation_ready(self.state))

    async def test_hosted_chat_does_not_wait_for_local_model(self):
        reply = AsyncMock(return_value={'answer': 'An average and a median describe different parts of a sample.',
                                        'ai_used': True})
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-test-value'}), \
                patch('app.answer_chat_message', reply), \
                patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No real API call'))):
            result = await app.telegram_answer('Explain what a median tells me in plain language.')
        self.assertIn('median', result)
        reply.assert_awaited_once()
        self.assertEqual(self.state.phase, 'model')

    async def test_missing_key_neither_falls_back_nor_claims_readiness(self):
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': ''}):
            self.state.phase = 'ready'
            self.assertFalse(app.conversation_ready(self.state))
            self.assertIn('not configured', app.conversation_not_ready(self.state))
            self.assertFalse((await app.ai_status())['conversation_engine']['configured'])
            self.assertEqual((await app.status())['phase'], 'configuration')

    async def test_status_does_not_expose_key_or_claim_live_provider_connection(self):
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-test-value'}), \
                patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No status network call'))):
            status = await app.status()
            ai = await app.ai_status()
        self.assertEqual(status['phase'], 'ready')
        self.assertEqual(status['model'], 'gpt-4.1-mini')
        self.assertIn('not verified', status['detail'])
        self.assertNotIn('private-test-value', json.dumps([status, ai]))
        self.assertEqual(ai['phase'], 'model')

    async def test_valid_plan_records_success_for_application_rendered_facts(self):
        app.observe_ai_result({'ai_used': False, 'conversation_diagnostics': {'model_calls': 1}})
        self.assertIsNotNone(self.state.last_ai_success)

    async def test_hosted_news_uses_selected_engine_while_local_model_is_loading(self):
        self.master.record_news({'id': 'fixture-report', 'title': 'Fixture player: Listed as questionable',
            'source': 'Fixture publisher', 'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'url': 'https://example.test/synthetic-injury-report'})
        call = AsyncMock(return_value={'message': {'content': '{"reports":[1]}'}, 'done': True})
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-test-value'}), \
                patch('dialogue_model.invoke_dialogue', call), \
                patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No local-model request'))):
            text = await app.telegram_answer('Summarize the latest injury news.')
        self.assertIn('Fixture player', text)
        call.assert_awaited_once()
        self.assertEqual(call.await_args.kwargs['timeout'], 45)

    async def test_original_worksheet_request_does_not_start_an_unready_local_model(self):
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-test-value'}), \
                patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No local-model request'))):
            result = await app.answer_chat_message(None, app.ChatRequest(message='Read worksheet Player_Metrics row 2.'), {}, self.master)
        self.assertEqual(result['response_mode'], 'worksheet_starting')
        self.assertIn('still starting', result['answer'])

    async def test_hosted_news_authentication_failure_retains_safe_service_category(self):
        from dialogue_model import DialogueModelError
        self.master.record_news({'id': 'fixture-auth-report', 'title': 'Fixture player: injury update',
            'source': 'Fixture publisher', 'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'url': 'https://example.test/synthetic-injury-report'})
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-test-value'}), \
                patch('dialogue_model.invoke_dialogue', AsyncMock(side_effect=DialogueModelError('access_refused'))):
            text = await app.telegram_answer('Summarize the latest injury news.')
            status = await app.ai_status()
        self.assertIn('refused the connection', text)
        self.assertIn('Fixture player', text)
        self.assertEqual(status['last_failure_category'], 'access_refused')
        self.assertNotIn('private-test-value', text + json.dumps(status))
