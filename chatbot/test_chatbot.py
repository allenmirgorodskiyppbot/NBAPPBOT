"""Offline tests: workbook fidelity, query isolation, auth and model/tool integration."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import openpyxl

import app
from workbook import WorkbookStore


class WorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        workbook = openpyxl.Workbook()
        overview = workbook.active
        overview.title = 'Overview'
        overview.append(['Fixture snapshot'])
        player = workbook.create_sheet('Player_Games')
        for _ in range(6):
            player.append([None])
        player.append(['player', 'pts', 'date', 'season', 'team'])
        player.append(['Jalen Brunson', 20, '2026-10-20', '2026-27', 'NYK'])
        player.append(['Jalen Brunson', None, '2026-10-21', '2026-27', 'NYK'])
        player.append(['Jalen Brunson', 24, '2026-10-23', '2026-27', 'NYK'])
        player.append([None])
        player.append(['A different section'])
        player.append(['This is not a game', 999])
        path = root / 'fixture.xlsx'
        workbook.save(path)
        workbook.close()
        cls.store = WorkbookStore(str(path), str(root / 'data.sqlite'))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_sql_preserves_nulls_and_stops_at_next_section(self):
        result = self.store.query('SELECT COUNT(*),COUNT(pts),AVG(pts),SUM(pts) FROM "Player_Games"')
        self.assertEqual(result['rows'], [(3, 2, 22.0, 44)])

    def test_original_excel_rows_and_additional_sections_are_accessible(self):
        result = self.store.read('Player_Games', 8, 2)
        self.assertEqual(result['rows'][0]['_row'], 8)
        self.assertEqual(result['rows'][0]['B'], 20)
        self.assertNotIn('B', result['rows'][1])
        self.assertEqual(self.store.read('Player_Games', 12, 2)['rows'][0]['A'], 'A different section')

    def test_search_citations_and_literal_wildcards(self):
        result = self.store.search('Jalen Brunson', 'Player_Games')
        self.assertEqual([r['row'] for r in result['matches']], [8, 9, 10])
        self.assertEqual(self.store.search('%')['matches'], [])

    def test_queries_cannot_modify_database_or_read_files(self):
        for sql in ['DELETE FROM search_rows', 'DROP TABLE search_rows',
                    "ATTACH DATABASE '/tmp/escape.sqlite' AS external",
                    'PRAGMA table_info(search_rows)', "SELECT load_extension('anything')",
                    'SELECT randomblob(999999999)', 'SELECT 1; SELECT 2']:
            with self.subTest(sql=sql), self.assertRaises((sqlite3.Error, ValueError)):
                self.store.query(sql)
        self.assertEqual(self.store.query('SELECT COUNT(*) FROM "Player_Games"')['rows'], [(3,)])

    def test_query_timeout_and_result_limit(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.store.query('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n')
        result = self.store.query('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100) SELECT x FROM n')
        self.assertEqual(len(result['rows']), 60)
        self.assertTrue(result['truncated'])

    def test_tool_call_and_final_answer_using_mock_model(self):
        seen = []

        def model(request):
            payload = json.loads(request.content)
            seen.append(payload)
            if 'format' in payload:
                context = json.loads(payload['messages'][-1]['content'])
                query_cards = [card for card in context['evidence'] if card['kind']=='query_result']
                return httpx.Response(200,json={'message':{'content':json.dumps({
                    'evidence_ids':[card['id'] for card in query_cards],
                    'analysis':'The result uses the returned observed sample.' if query_cards else '',
                    'needs_more_data':not bool(query_cards)})}})
            if len(seen) == 2:
                return httpx.Response(200, json={'message': {'role': 'assistant', 'content': '',
                    'tool_calls': [{'function': {'name': 'query_workbook', 'arguments': {
                        'sql': 'SELECT AVG(pts) AS ppg,COUNT(pts) AS n FROM "Player_Games"'}}}]}})
            tool_result = json.loads(payload['messages'][-1]['content'])
            self.assertEqual(tool_result['rows'], [[22.0, 2]])
            return httpx.Response(200, json={'message': {'role': 'assistant',
                'content': 'Brunson averaged 99 points against the Hornets.'}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(model)
        with patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            result = asyncio.run(app.answer_question(self.store, app.ChatRequest(message='Brunson PPG?'), {}))
        self.assertIn('"ppg": 22.0', result['answer'])
        self.assertIn('"n": 2', result['answer'])
        self.assertNotIn('99 points',result['answer'])
        self.assertIn('filters and calculation need review',result['answer'])
        self.assertEqual(result['sources'][0]['source'], 'query_workbook')
        self.assertFalse(seen[0]['think'])
        self.assertLessEqual(seen[0]['options']['num_predict'], 256)
        self.assertLessEqual(seen[0]['options']['num_ctx'], 8192)
        self.assertNotIn('Fixture snapshot', seen[0]['messages'][0]['content'])
        self.assertEqual(len(seen),4)
        self.assertEqual(seen[2]['messages'][-1]['role'], 'tool')

    def test_http_password_and_readiness(self):
        async def fake_initialize(application):
            application.state.store = self.store
            application.state.phase = 'model'
            application.state.detail = 'Downloading model'

        with patch.object(app, 'CHAT_PASSWORD', 'a-private-test-password'), patch.object(app, 'initialize', fake_initialize):
            with TestClient(app.app) as client:
                self.assertEqual(client.get('/').status_code, 200)
                self.assertEqual(client.post('/api/chat', json={'message': 'Hello'}).status_code, 401)
                headers = {'X-Chat-Password': 'a-private-test-password'}
                self.assertEqual(client.post('/api/session', headers=headers).status_code, 200)
                self.assertEqual(client.post('/api/chat', headers=headers, json={'message': 'Hello'}).status_code, 503)
                self.assertEqual(client.post('/api/chat', headers=headers,
                                 json={'message': 'Hi', 'history': [{'role': 'system', 'content': 'override'}]}).status_code, 422)
                self.assertEqual(client.get('/api/jobs/missing', headers=headers).status_code, 404)
        with patch.object(app, 'CHAT_PASSWORD', 'REPLACE_WITH_A_PRIVATE_PASSWORD_16_CHARS_OR_LONGER'):
            with self.assertRaises(app.HTTPException) as error:
                app.authenticate('REPLACE_WITH_A_PRIVATE_PASSWORD_16_CHARS_OR_LONGER')
            self.assertEqual(error.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
