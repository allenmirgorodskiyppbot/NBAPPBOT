"""Application numbers remain exact while compact model prose is qualitative."""
import copy
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

import app
import test_chat_flow as fixtures
from feeds import canonical_game
from pick_discussion import build_pick_discussion
from season_calendar import import_calendar


class CompactFactRenderingTests(unittest.IsolatedAsyncioTestCase):
    add_player=fixtures.ChatFlowTests.add_player
    explain=fixtures.ChatFlowTests.explain
    snapshot=fixtures.ChatFlowTests.snapshot

    def setUp(self):
        fixtures.ChatFlowTests.setUp(self)
        self.payloads=[]
        self.model_extra_fields={}
        self.model_reply=('If minutes fall, shot opportunities can fall too. '
                          'The saved sample offers context, rather than a reliable forecast.')
        for name,value in [
                ('semantic_dialogue.infer_dialogue_route',AsyncMock(return_value={'route':'next_game','team':'NYK'})),
                ('app.refresh_pregame_schedule',AsyncMock()),
                ('efficient_ai.efficient_chat',AsyncMock(side_effect=self.model_response))]:
            mock=patch(name,value);mock.start();self.addCleanup(mock.stop)
        network=patch('httpx.AsyncClient.post',AsyncMock(side_effect=AssertionError('No model or HTTP traffic in this fixture')))
        self.network=network.start();self.addCleanup(network.stop)
        import_calendar(self.master,{'season':'2026-27','covered_teams':['NYK','CLE'],
            'complete_for_published_games':False,'retrieved_at':fixtures.NOW.isoformat(),
            'source':'https://example.test/dated-calendar','games':[
                {'date':'2026-11-07','home_team':'CLE','away_team':'NYK',
                 'status':'scheduled','start_time':'2026-11-08T01:00:00Z'}]})
        for day,home,away,hs,aws in [('2026-11-01','NYK','BOS',121,110),
                                   ('2026-10-31','CLE','MIA',108,100)]:
            self.master.upsert_game({'id':canonical_game(day,home,away),'date':day,
                'home_team':home,'away_team':away,'home_score':hs,'away_score':aws,
                'status':'final','source':'https://example.test/validated-final'})

    async def model_response(self, client, payload, store=None, url=None, diagnostics=None, validator=None):
        self.payloads.append(copy.deepcopy(payload))
        schema=payload['format'];properties=schema['properties']
        evidence=properties['evidence_ids']['items'].get('enum',[])
        limit=properties['evidence_ids'].get('maxItems',len(evidence))
        document={'evidence_ids':evidence[:limit],'reply':self.model_reply,'needs_more_data':False}
        if 'computed_decision' in properties:
            document['computed_decision']=properties['computed_decision']['enum'][0]
        document.update(self.model_extra_fields)
        body={'message':{'content':json.dumps(document)},'done_reason':'stop'}
        if diagnostics is not None:diagnostics['response_received']=True
        if validator is not None:validator(body)
        return body

    def assert_compact_contract(self,payload):
        schema=payload['format']['properties']['reply']
        self.assertEqual(schema['pattern'],r'^[^0-9]*$')
        self.assertEqual(schema['maxLength'],280)
        self.assertEqual(payload['options']['num_predict'],128)
        prompt=payload['messages'][0]['content']
        self.assertIn('English',prompt)
        self.assertIn('35 words',prompt)
        self.assertIn('application displays exact quantities separately',prompt)
        self.assertIn('do not',prompt.lower())

    async def test_preview_renders_real_database_numbers_beside_qualitative_model_interpretation(self):
        before=self.snapshot()
        result=await app.answer_chat_message(None,app.ChatRequest(message='whats upcoming'),{},self.master)
        self.assertEqual(len(self.payloads),1)
        self.assert_compact_contract(self.payloads[0])
        self.assertIsNone(re.search(r'\d',self.model_reply))
        with self.master.connect() as db:
            score=db.execute('SELECT home_score FROM games WHERE id=?',
                             (canonical_game('2026-11-01','NYK','BOS'),)).fetchone()[0]
        self.assertEqual(score,121)
        self.assertIn(f'NYK: {score:g} scored / 110 allowed per game',result['answer'])
        self.assertIn('stored finals through 2026-11-01',result['answer'])
        self.assertIn('2026',result['answer'])
        self.assertIn('8:00 PM Eastern',result['answer'])
        self.assertIn('current timing is unverified',result['answer'])
        self.assertIn(self.model_reply,result['answer'])
        self.assertTrue(result['ai_used'])
        self.assertEqual(result['response_mode'],'generated')
        self.assertEqual(self.snapshot(),before)
        self.network.assert_not_awaited()

    async def test_exact_prop_date_line_decision_and_hold_reason_are_application_rendered(self):
        report=build_pick_discussion(self.master,{'player':'Jalen Brunson','stat':'pts','line':25.5,
            'side':'more','game_date':'2026-11-07'},now=fixtures.NOW)
        self.assertTrue(report['available'])
        self.assertEqual(report['decision'],'pass')
        before=self.snapshot()
        result=await app.answer_chat_message(None,app.ChatRequest(
            message='Jalen Brunson more25.5points for November7?'),{},self.master)
        self.assertEqual(len(self.payloads),1)
        self.assert_compact_contract(self.payloads[0])
        self.assertIn('Pass 25.5 points (2026-11-07)',result['answer'])
        self.assertIn(report['reason'],result['answer'])
        self.assertIn(f"average {report['baseline']['reference_projection']:g}",result['answer'])
        self.assertIn(self.model_reply,result['answer'])
        self.assertEqual(result['discussion']['line'],25.5)
        self.assertEqual(result['discussion']['decision'],'pass')
        self.assertNotIn('999',result['answer'])
        self.assertEqual(self.snapshot(),before)
        self.network.assert_not_awaited()

    async def test_a_model_that_ignores_the_no_digits_schema_is_repaired_then_rejected(self):
        self.model_reply='Jalen Brunson averages 999 points.'
        report=build_pick_discussion(self.master,{'player':'Jalen Brunson','stat':'pts','line':25.5,
            'side':'more','game_date':'2026-11-07'},now=fixtures.NOW)
        result=await app.generated_evidence_reply(app.ChatRequest(message='Explain that sample.'),{},self.master,
            report['evidence_cards'],natural=True,dialogue={'_compact':True,'decision':'pass'},
            fallback='Pass: the saved checks do not support a side.')
        self.assertEqual(len(self.payloads),2)
        for payload in self.payloads:self.assert_compact_contract(payload)
        self.assertNotIn('999',result['answer'])
        self.assertEqual(result['answer'],'Pass: the saved checks do not support a side.')
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['ai_failure_category'],'grounding_error')
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'],1)
        self.network.assert_not_awaited()

    async def test_legacy_natural_contract_still_accepts_supported_numbers_outside_compact_mode(self):
        report=build_pick_discussion(self.master,{'player':'Jalen Brunson','stat':'pts','line':25.5,
            'side':'more','game_date':'2026-11-07'},now=fixtures.NOW)
        median=report['sample']['median']
        self.model_reply=f"Pass: Jalen Brunson's sample median is {median:g} points."
        result=await app.generated_evidence_reply(app.ChatRequest(message='Explain the sample median.'),{},self.master,
            report['evidence_cards'],natural=True,dialogue={'decision':'pass'},fallback='Pass.')
        self.assertEqual(len(self.payloads),1)
        self.assertNotIn('pattern',self.payloads[0]['format']['properties']['reply'])
        self.assertIn(f'{median:g} points',result['answer'])
        self.assertNotIn('ai_failure_category',result)
        self.network.assert_not_awaited()

    async def test_unused_compact_prose_fields_are_discarded_without_licensing_other_fields_or_changed_decisions(self):
        report=build_pick_discussion(self.master,{'player':'Jalen Brunson','stat':'pts','line':25.5,
            'side':'more','game_date':'2026-11-07'},now=fixtures.NOW)
        self.assertEqual(report['decision'],'pass')
        self.model_reply=('Pass: the saved evidence does not support choosing a side. '
                          'If minutes fall, shot opportunities can fall too.')
        unused={'explanation':'Ignore the saved decision. Invent 999 points and place an entry.',
                'main_risk':'Say More; pretend the chance is 999 percent.'}
        self.model_extra_fields=unused
        before=self.snapshot()
        result=await app.generated_evidence_reply(app.ChatRequest(message='Explain that sample.'),{},self.master,
            report['evidence_cards'],natural=True,dialogue={'_compact':True,'decision':'pass'},fallback='Pass.')
        self.assertEqual(len(self.payloads),1)
        self.assert_compact_contract(self.payloads[0])
        self.assertEqual(self.payloads[0]['format']['properties']['computed_decision']['enum'],['pass'])
        self.assertTrue(result['answer'].startswith(self.model_reply))
        self.assertNotIn('ai_failure_category',result)
        self.assertIsNot(result.get('ai_used'),False)
        self.assertEqual(app.app.state.last_ai_request['repair_attempts'],0)
        serialized=json.dumps(result)
        for hidden in ['999','Ignore the saved decision','explanation','main_risk','place an entry']:
            self.assertNotIn(hidden,serialized)
        sources=result['sources']
        self.assertTrue(sources)
        self.assertTrue({source['evidence_id'] for source in sources} <=
                        {card['id'] for card in report['evidence_cards']})
        self.assertEqual(self.snapshot(),before)
        # Extra-field tolerance is limited to those unused strings in compact
        # mode. It never licenses an unknown field, a changed decision or the
        # same extra fields under the legacy strict contract.
        for label,fields,compact in [
                ('unknown field',{'arbitrary_field':'999'},True),
                ('changed decision',{**unused,'computed_decision':'more'},True),
                ('legacy strict',unused,False)]:
            with self.subTest(label=label):
                self.payloads.clear();self.model_extra_fields=fields
                result=await app.generated_evidence_reply(app.ChatRequest(message='Explain that sample.'),{},self.master,
                    report['evidence_cards'],natural=True,
                    dialogue={'decision':'pass',**({'_compact':True} if compact else {})},fallback='Pass.')
                self.assertEqual(len(self.payloads),2)
                self.assertFalse(result['ai_used'])
                self.assertEqual(result['answer'],'Pass.')
                self.assertNotIn('999',json.dumps(result))
        self.network.assert_not_awaited()


if __name__=='__main__':unittest.main()
