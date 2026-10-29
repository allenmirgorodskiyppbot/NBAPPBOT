"""CPU news routing, truthful fallbacks and secret-safe model diagnostics."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import app
from master import MasterStore


class NoWorkbook:
    def context(self):
        raise AssertionError('A news-only summary must not load workbook context')


class AIRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.master = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.master.record_news({'id': 'rw:smith', 'title': 'Jalen Smith: Listed as questionable',
            'body': 'Smith is questionable with a left finger sprain.',
            'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'source': 'RotoWire', 'url': 'https://www.rotowire.com/basketball/player/jalen-smith'})
        self.master.feed_success('rss_1', 'Fixture feed')
        state = app.app.state
        state.phase, state.detail = 'ready', 'Ready'
        state.chat_lock = asyncio.Lock()
        state.store, state.master = NoWorkbook(), self.master
        state.ai_started_at, state.last_ai_failure, state.last_ai_success = None, {}, None

    async def asyncTearDown(self):
        self.directory.cleanup()

    def mock_client(self, handler):
        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        return patch.object(app.httpx, 'AsyncClient',
                            side_effect=lambda **kwargs: real(transport=transport, **kwargs))

    async def test_news_uses_one_small_grounded_request_without_workbook_or_tools(self):
        seen = []
        def reply(request):
            data = json.loads(request.content)
            seen.append(data)
            self.assertNotIn('tools', data)
            self.assertLessEqual(data['options']['num_ctx'], 4096)
            self.assertLessEqual(data['options']['num_predict'], 512)
            if app.MODEL.startswith('qwen3:'):
                self.assertTrue(data['messages'][-1]['content'].endswith('/no_think'))
            self.assertIn('Jalen Smith', data['messages'][-1]['content'])
            self.assertIn('finger sprain', data['messages'][-1]['content'])
            self.assertEqual(data['format']['properties']['reports']['maxItems'], 1)
            return httpx.Response(200, json={'message': {'content': '{"reports":[1]}'},
                                           'done_reason': 'stop'})
        with self.mock_client(reply):
            answer = await app.telegram_answer('Summarize my latest collected injury news.')
        self.assertIn('Jalen Smith: Listed as questionable', answer)
        self.assertIn('Published:', answer)
        self.assertIn('https://www.rotowire.com/basketball/player/jalen-smith', answer)
        self.assertEqual(len(seen), 1)
        state = await app.ai_status()
        self.assertIsNotNone(state['last_success_time'])
        self.assertFalse(state['busy'])
        self.assertFalse(state['active'])

    async def test_news_timeout_returns_saved_reports_and_preserves_failure_diagnostic(self):
        def reply(request):
            raise httpx.ReadTimeout('DO_NOT_LEAK private query key', request=request)
        with self.mock_client(reply), self.assertLogs('knicks', level='WARNING') as logs:
            answer = await app.telegram_answer('Summarize the latest injury updates.')
        self.assertIn('timed out', answer)
        self.assertIn('not an AI summary', answer)
        self.assertIn('Jalen Smith', answer)
        self.assertNotIn('DO_NOT_LEAK', answer + '\n'.join(logs.output))
        state = await app.ai_status()
        self.assertEqual(state['last_failure_category'], 'timeout')
        self.assertIsNone(state['last_success_time'])
        self.assertFalse(state['busy'])

    async def test_truncated_reasoning_is_not_reported_as_finished_summary(self):
        with self.mock_client(lambda request: httpx.Response(200, json={
                'message': {'content': 'Hmm, I should think about the user'}, 'done_reason': 'length'})):
            answer = await app.telegram_answer('Summarize injury news.')
        self.assertIn('output limit', answer)
        self.assertIn('Saved reports', answer)
        self.assertNotIn('Hmm', answer)
        self.assertEqual((await app.ai_status())['last_failure_category'], 'output_limit')

    async def test_empty_or_malformed_model_output_returns_factual_fallback(self):
        for body, expected in [({'message': {'content': ''}}, 'empty_model_answer'),
                               ({'wrong': 'shape'}, 'invalid_model_response')]:
            with self.subTest(body=body), self.mock_client(lambda request: httpx.Response(200, json=body)):
                answer = await app.telegram_answer('Summarize injury news.')
                self.assertIn('Saved reports', answer)
                self.assertEqual((await app.ai_status())['last_failure_category'], expected)

    async def test_general_question_http_failure_is_safely_reported_and_lock_released(self):
        request = httpx.Request('POST', 'http://model:11434/private?token=DO_NOT_LEAK')
        error = httpx.HTTPStatusError('DO_NOT_LEAK', request=request,
                                     response=httpx.Response(500, request=request))
        with patch.object(app, 'answer_question', AsyncMock(side_effect=error)), \
                self.assertLogs('knicks', level='WARNING') as logs:
            answer = await app.telegram_answer('What is Brunson averaging?')
        self.assertIn('HTTP 500', answer)
        self.assertNotIn('DO_NOT_LEAK', answer + '\n'.join(logs.output))
        self.assertEqual((await app.ai_status())['last_failure_category'], 'model_http_500')
        self.assertFalse(app.app.state.chat_lock.locked())

    async def test_ai_status_remains_available_during_generation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'answer': 'Completed', 'sources': []}
        with patch.object(app, 'answer_question', slow_answer):
            task = asyncio.create_task(app.telegram_answer('What is Brunson averaging?'))
            await entered.wait()
            state = await asyncio.wait_for(app.ai_status(), 1)
            self.assertTrue(state['busy'])
            self.assertTrue(state['active'])
            release.set()
            self.assertEqual(await task, 'Completed')
        self.assertFalse((await app.ai_status())['active'])

    async def test_empty_news_does_not_claim_ai_inference_succeeded(self):
        with self.mock_client(lambda request: self.fail('No matching stories should not invoke AI')):
            answer = await app.telegram_answer('Summarize the latest trades.')
        self.assertIsInstance(answer, str)
        self.assertIsNone((await app.ai_status())['last_success_time'])

    async def test_invalid_or_duplicate_report_references_get_cited_fallback(self):
        for content in ['{"reports":[99]}', '{"reports":[1,1]}', '{"reports":[true]}',
                        'Here are five reports without dates']:
            with self.subTest(content=content), self.mock_client(lambda request: httpx.Response(200,
                    json={'message': {'content': content}, 'done_reason': 'stop'})):
                answer = await app.telegram_answer('Summarize injury news in one bullet.')
                self.assertEqual(len([line for line in answer.splitlines() if line.startswith('- ')]), 1)
                self.assertIn('Published:', answer)
                self.assertIn('Saved reports', answer)

    async def test_model_only_selects_report_numbers_count_and_dates_are_app_controlled(self):
        for index in range(4):
            self.master.record_news({'id': 'extra-' + str(index),
                'title': 'Fixture player ' + str(index) + ': Out with ankle injury',
                'source': 'Fixture publisher', 'published_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'url': 'https://example.com/injury-' + str(index)})
        seen = []
        def reply(request):
            data = json.loads(request.content)
            seen.append(data)
            return httpx.Response(200, json={'message': {'content': '{"reports":[1,2,3]}'}})
        with self.mock_client(reply):
            answer = await app.telegram_answer('Summarize the latest injury news in three short bullets.')
        self.assertEqual(len([line for line in answer.splitlines() if line.startswith('- ')]), 3)
        self.assertEqual(answer.count('Published:'), 3)
        self.assertEqual(seen[0]['format']['properties']['reports']['maxItems'], 3)
        self.assertNotIn('foo fabricated status', answer)

    def test_summary_routing_does_not_replace_statistics_or_parlay_analysis(self):
        self.assertEqual(app.news_summary_topic('Summarize latest injuries'), 'injuries')
        self.assertEqual(app.news_summary_topic('Show latest NBA trades'), 'trades')
        self.assertIsNone(app.news_summary_topic('Analyze recent injuries and my picks'))
        self.assertIsNone(app.news_summary_topic('Summarize latest news and Brunson averages'))


if __name__ == '__main__':
    unittest.main()
