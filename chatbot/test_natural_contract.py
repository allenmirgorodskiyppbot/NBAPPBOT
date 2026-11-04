"""A user's demand for a side cannot override a computed Pass assessment."""
import json
import unittest
from unittest.mock import patch

import httpx

import app
import efficient_ai
import test_pick_discussion as fixtures


class NaturalContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.PickDiscussionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.add('Josh Hart', [21,16,13,14,23,27,24], stat='pra', team='NYK')
        self.report = self.fixture.assess('Josh Hart', 'pra', 22.5)
        self.assertEqual(self.report['decision'], 'pass')
        efficient_ai._CACHE.clear()

    async def generate(self, decisions, explanation='Pass: the saved sample does not support choosing a side.'):
        requests = []
        real_client = httpx.AsyncClient

        def reply(request):
            payload = json.loads(request.content)
            requests.append(payload)
            index = min(len(requests)-1, len(decisions)-1)
            return httpx.Response(200, json={'message':{'content':json.dumps({
                'computed_decision':decisions[index], 'evidence_ids':['P3'],
                'reply':explanation,
                'needs_more_data':False})}})

        with patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(reply), **kw)):
            result = await app.generated_evidence_reply(
                app.ChatRequest(message='Up or down choose, I want More'), {}, self.fixture.store,
                self.report['evidence_cards'], natural=True,
                dialogue={'decision':'pass'}, fallback=self.report['fallback_answer'])
        return result, requests

    async def test_pass_is_anchored_before_explanation_and_preserved(self):
        result, requests = await self.generate(['pass'])
        self.assertEqual(len(requests), 1)
        schema = requests[0]['format']
        self.assertEqual(next(iter(schema['properties'])), 'computed_decision')
        self.assertEqual(schema['properties']['computed_decision']['enum'], ['pass'])
        self.assertTrue(result['answer'].startswith('Pass:'))
        self.assertNotIn('ai_failure_category', result)

    async def test_changed_decision_is_repaired_not_delivered(self):
        result, requests = await self.generate(['more','pass'])
        self.assertEqual(len(requests), 2)
        self.assertTrue(result['answer'].startswith('Pass:'))
        self.assertNotIn('ai_failure_category', result)
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'], 1)

    async def test_supported_workload_does_not_fail_for_an_omitted_model_citation(self):
        result, requests = await self.generate(['pass'],
            'Pass: his recent median workload is 34 minutes, but the sample does not support a clear side.')
        self.assertEqual(len(requests), 1)
        self.assertIn('34 minutes', result['answer'])
        self.assertIn('P2', [source['evidence_id'] for source in result['sources']])
        self.assertNotIn('ai_failure_category', result)

    async def test_repeated_changed_decision_uses_computed_summary(self):
        result, requests = await self.generate(['more'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['answer'], self.report['fallback_answer'])
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['ai_failure_category'], 'grounding_error')
