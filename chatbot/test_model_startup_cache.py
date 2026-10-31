"""Local model cache detection and startup sequencing with mock HTTP only."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

import app
import assignment_refresh
import dataset_refresh
import release_checks


class LocalModelTagTests(unittest.TestCase):
    def test_exact_model_name_or_model_field_and_default_latest(self):
        for tags, model in [
            ({'models': [{'name': 'qwen2.5:3b'}]}, 'qwen2.5:3b'),
            ({'models': [{'model': 'qwen2.5:3b'}]}, 'qwen2.5:3b'),
            ({'models': [{'name': 'qwen2.5:latest'}]}, 'qwen2.5'),
            ({'models': [{'name': 'qwen2.5'}]}, 'qwen2.5:latest'),
            ({'models': [{'name': 'host:5000/library/model:latest'}]}, 'host:5000/library/model'),
        ]:
            with self.subTest(tags=tags, model=model):
                self.assertTrue(app.model_downloaded(tags, model))

    def test_prefixes_different_tags_and_malformed_catalog_never_skip_pull(self):
        for tags in [None, [], {}, {'models': None}, {'models': {}},
                     {'models': [{'name': 'qwen2.5:3b-instruct'}]},
                     {'models': [{'name': 'qwen2.5:latest'}]},
                     {'models': ['qwen2.5:3b']},
                     {'models': [{'name': 123, 'model': None}]}]:
            with self.subTest(tags=tags):
                self.assertFalse(app.model_downloaded(tags, 'qwen2.5:3b'))
        self.assertFalse(app.model_downloaded({'models': [{'name': ''}]}, ''))


class MockedStartupTests(unittest.IsolatedAsyncioTestCase):
    async def startup(self, tags, pull_status=200, warm_status=200):
        calls = []
        state = SimpleNamespace(service_tasks=[object()], store=SimpleNamespace(sheets={}),
                                master=object(), release_check_task=object())

        def response(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/api/tags':
                return httpx.Response(200, json=tags)
            if request.url.path == '/api/pull':
                return httpx.Response(pull_status, content=b'{"status":"success"}\n')
            if request.url.path == '/api/generate':
                self.assertEqual(state.phase, 'warming')
                payload = json.loads(request.content)
                self.assertEqual(payload['prompt'], '')
                return httpx.Response(warm_status, json={'done': True, 'done_reason': 'load'})
            self.fail('Unexpected model endpoint')

        async def direct(function, *args, **kwargs): return function(*args, **kwargs)
        original_client = httpx.AsyncClient
        with patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw:
                          original_client(transport=httpx.MockTransport(response), **kw)), \
                patch.object(app, 'seed_workbook', return_value={}), \
                patch.object(dataset_refresh, 'initialize_scope'), \
                patch.object(dataset_refresh, 'index_workbook_evidence'), \
                patch.object(dataset_refresh, 'supplement_existing_observation_fields'), \
                patch.object(assignment_refresh, 'refresh_assignments'), \
                patch.object(release_checks, 'model_startup_completed'), \
                patch.object(app.asyncio, 'to_thread', direct), patch.object(app.log, 'exception'):
            await app.initialize(SimpleNamespace(state=state))
        return state, calls

    async def test_cached_model_reaches_ready_even_when_registry_would_fail(self):
        state, calls = await self.startup({'models': [{'name': app.MODEL}]}, pull_status=503)
        self.assertEqual(state.phase, 'ready')
        self.assertEqual(calls, [('GET', '/api/tags'), ('POST', '/api/generate')])

    async def test_missing_model_downloads_then_warms_before_ready(self):
        state, calls = await self.startup({'models': []})
        self.assertEqual(state.phase, 'ready')
        self.assertEqual(calls, [('GET', '/api/tags'), ('POST', '/api/pull'),
                                 ('POST', '/api/generate')])

    async def test_failed_download_never_reports_ready(self):
        state, calls = await self.startup({'models': []}, pull_status=503)
        self.assertEqual(state.phase, 'error')
        self.assertNotIn(('POST', '/api/generate'), calls)

    async def test_existing_model_still_requires_successful_warmup(self):
        state, calls = await self.startup({'models': [{'name': app.MODEL}]}, warm_status=503)
        self.assertEqual(state.phase, 'error')
        self.assertEqual(calls, [('GET', '/api/tags'), ('POST', '/api/generate')])


if __name__ == '__main__':
    unittest.main()
