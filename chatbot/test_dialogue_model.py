"""Transport contracts use injected clients, never a paid API or real key."""
import asyncio
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import dialogue_model as model


KEY = 'private-test-key-never-a-real-credential'


def request_payload():
    return {'model': 'local-model-name', 'messages': [
        {'role': 'system', 'content': 'Return a JSON plan using saved records.'},
        {'role': 'user', 'content': 'Explain the saved sample.'}],
        'format': {'type': 'object', 'properties': {'reply': {'type': 'string'}},
                   'required': ['reply'], 'additionalProperties': False},
        'stream': False, 'think': False, 'options': {'temperature': 0, 'num_ctx': 4096, 'num_predict': 50}}


def completion(content='{"reply":"Saved records are dated observations."}', **message):
    return {'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': content, **message}}]}


class DialogueModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DIALOGUE_BACKEND': 'ollama', 'OPENAI_API_KEY': '',
            'OPENAI_CHAT_MODEL': 'gpt-4.1-mini', 'OLLAMA_URL': 'http://model:11434',
            'OLLAMA_MODEL': 'qwen3:4b-instruct-2507-q4_K_M'})
        env.start(); self.addCleanup(env.stop)

    def hosted(self, **values):
        return patch.dict(os.environ, {'DIALOGUE_BACKEND': 'openai', 'OPENAI_API_KEY': KEY, **values})

    async def call(self, handler, payload=None, *, timeout=45, follow=False):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=follow) as client:
            result = await model.invoke_dialogue(payload if payload is not None else request_payload(),
                                                 client=client, timeout=timeout)
            self.assertFalse(client.is_closed, 'Injected clients remain caller-owned')
            return result

    async def test_default_is_local_and_key_alone_does_not_select_paid_chat(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': KEY}):
            status = model.backend_status()
        self.assertEqual(status, {'backend': 'ollama', 'configured': True,
                                 'model': 'qwen3:4b-instruct-2507-q4_K_M'})
        self.assertNotIn(KEY, json.dumps(status))

    async def test_status_is_safe_configuration_not_a_live_connection_check(self):
        with self.hosted(), patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No HTTP'))):
            self.assertEqual(model.backend_status(), {'backend': 'openai', 'configured': True, 'model': 'gpt-4.1-mini'})
        with patch.dict(os.environ, {'DIALOGUE_BACKEND': KEY}):
            status = model.backend_status()
        self.assertEqual(status['backend'], 'invalid')
        self.assertFalse(status['configured'])
        self.assertNotIn(KEY, str(status))
        with patch.dict(os.environ, {'OLLAMA_URL': ''}):
            self.assertFalse(model.backend_status()['configured'])

    async def test_explicit_hosted_selection_requires_a_valid_private_key_before_http(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=completion())
        for key in ('', '  ', 'bad\nkey', 'x' * 513):
            with self.subTest(key_length=len(key)), self.hosted(OPENAI_API_KEY=key):
                with self.assertRaises(model.DialogueModelError) as error:
                    await self.call(handler)
                self.assertEqual(error.exception.category, 'missing_or_invalid_api_key')
                self.assertFalse(model.backend_status()['configured'])
        self.assertEqual(calls, [])

    async def test_hosted_payload_is_allowlisted_immutable_and_has_the_full_response_contract(self):
        payload = request_payload()
        payload['tools'] = [{'unapproved': 'action'}]
        payload['url'] = 'https://untrusted.invalid/'
        before = copy.deepcopy(payload)
        calls = []
        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertEqual(str(request.url), model.OPENAI_ENDPOINT)
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + KEY)
            self.assertEqual(set(body), {'model', 'messages', 'stream', 'response_format',
                                         'max_completion_tokens', 'temperature', 'n'})
            self.assertEqual(body['model'], 'gpt-4.1-mini')
            self.assertEqual(body['max_completion_tokens'], 512)
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertIn('"additionalProperties":false', body['messages'][0]['content'])
            self.assertIn('application-owned response contract', body['messages'][0]['content'])
            self.assertEqual(body['messages'][1], payload['messages'][1])
            self.assertNotIn('num_ctx', str(body))
            return httpx.Response(200, json=completion())
        with self.hosted():
            result = await self.call(handler, payload)
        self.assertEqual(payload, before)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result['done'])
        self.assertEqual(result['message']['role'], 'assistant')
        self.assertIn('Saved records', result['message']['content'])

    async def test_user_only_messages_get_a_json_contract_without_mutation(self):
        payload = {'messages': [{'role': 'user', 'content': 'Hello'}]}
        before = copy.deepcopy(payload)
        def handler(request):
            messages = json.loads(request.content)['messages']
            self.assertEqual(messages[0]['role'], 'system')
            self.assertIn('JSON', messages[0]['content'])
            self.assertEqual(messages[1], payload['messages'][0])
            return httpx.Response(200, json=completion())
        with self.hosted():
            await self.call(handler, payload)
        self.assertEqual(payload, before)

    async def test_bad_configuration_input_and_schema_do_not_make_requests(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=completion())
        bad = [None, {}, {'messages': []}, {'messages': [{'role': 'tool', 'content': 'bad'}]},
               {'messages': [{'role': 'user', 'content': ''}]},
               {'messages': [{'role': 'user', 'content': 'x' * 64001}]},
               {**request_payload(), 'format': 'json'},
               {**request_payload(), 'format': {'invalid': float('nan')}},
               {**request_payload(), 'format': {'description': 'x' * 50001}}]
        with self.hosted():
            for payload in bad:
                with self.subTest(payload_type=type(payload).__name__), self.assertRaises(model.DialogueModelError):
                    # Direct use preserves explicit None as an invalid input.
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        await model.invoke_dialogue(payload, client=client)
            with self.hosted(OPENAI_CHAT_MODEL='bad model/' + KEY), self.assertRaises(model.DialogueModelError):
                await self.call(handler)
        self.assertEqual(calls, [])

    async def test_provider_failures_are_safe_categories_without_retries_or_error_body(self):
        for code, category in ((401, 'access_refused'), (403, 'access_refused'),
                               (429, 'rate_limited'), (500, 'provider_error')):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(code, json={'error': {'message': KEY + ' and private question'}})
            with self.subTest(status=code), self.hosted():
                with self.assertRaises(model.DialogueModelError) as error:
                    await self.call(handler)
                self.assertEqual(error.exception.category, category)
                self.assertNotIn(KEY, str(error.exception))
                self.assertNotIn('private question', str(error.exception))
                self.assertNotIn('http', str(error.exception))
                self.assertEqual(len(calls), 1)

    async def test_redirects_never_forward_the_private_key_even_for_redirecting_clients(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'Location': 'https://untrusted.invalid/collect'})
        with self.hosted(), self.assertRaises(model.DialogueModelError) as error:
            await self.call(handler, follow=True)
        self.assertEqual(error.exception.category, 'provider_error')
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), model.OPENAI_ENDPOINT)

    async def test_incomplete_refused_and_action_responses_are_not_normalized_as_answers(self):
        bad = [None, {'error': {'message': KEY}}, {'choices': []},
               {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
               completion(''), completion('{}', refusal='refused'),
               completion('{}', tool_calls=[{'function': {'name': 'erase'}}]),
               completion('{}', function_call={'name': 'erase'}), completion('x' * 16001)]
        with self.hosted():
            for value in bad:
                with self.subTest(value_type=type(value).__name__), self.assertRaises(model.DialogueModelError):
                    await self.call(lambda request: httpx.Response(200, json=value))
            with self.assertRaises(model.DialogueModelError) as error:
                await self.call(lambda request: httpx.Response(200, content=b'not JSON'))
            self.assertEqual(error.exception.category, 'invalid_response')

    async def test_connection_and_timeout_exceptions_do_not_expose_private_diagnostics(self):
        for exception, category in ((httpx.ConnectError(KEY), 'connection_error'),
                                    (httpx.ReadTimeout(KEY), 'timeout'), (ValueError(KEY), 'request_error')):
            def handler(request):
                raise exception
            with self.subTest(exception_type=type(exception).__name__), self.hosted():
                with self.assertRaises(model.DialogueModelError) as error:
                    await self.call(handler)
                self.assertEqual(error.exception.category, category)
                self.assertNotIn(KEY, str(error.exception))

    async def test_cancellation_propagates_without_a_retry_or_successful_result(self):
        calls = []
        async def handler(request):
            calls.append(request)
            raise asyncio.CancelledError()
        with self.hosted(), self.assertRaises(asyncio.CancelledError):
            await self.call(handler)
        self.assertEqual(len(calls), 1)

    async def test_deadline_cancels_one_inflight_request_without_leaking_work(self):
        calls, cancellations = [], []
        async def handler(request):
            calls.append(request)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(True)
                raise
        with self.hosted(), self.assertRaises(model.DialogueModelError) as error:
            await self.call(handler, timeout=.01)
        self.assertEqual(error.exception.category, 'timeout')
        self.assertEqual((len(calls), len(cancellations)), (1, 1))

    async def test_local_backend_keeps_the_existing_no_master_scan_transport(self):
        payload = request_payload()
        result = {'message': {'role': 'assistant', 'content': '{}'}, 'done': True}
        invoke = AsyncMock(return_value=result)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail('No HTTP'))) as client:
            with patch('efficient_ai.efficient_chat', invoke):
                self.assertEqual(await model.invoke_dialogue(payload, client=client), result)
            invoke.assert_awaited_once_with(client, payload, None, 'http://model:11434')

    async def test_invalid_timeouts_fail_before_a_client_or_request_exists(self):
        for value in (True, 0, -1, 121, float('inf'), float('nan'), '45'):
            with self.subTest(timeout_type=type(value).__name__), self.assertRaises(model.DialogueModelError) as error:
                await model.invoke_dialogue(request_payload(), timeout=value)
            self.assertEqual(error.exception.category, 'invalid_timeout')


if __name__ == '__main__':
    unittest.main()
