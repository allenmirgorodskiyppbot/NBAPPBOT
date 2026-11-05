"""Only explanation prose is generated; assessment fields belong to the app."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

import efficient_ai
from grounded_reply import ReplyValidationError
from master import MasterStore


class PropTextTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.store=MasterStore(Path(self.folder.name)/'master.sqlite')
        efficient_ai._CACHE.clear()
        self.payload={'model':'fixture-model','messages':[
            {'role':'system','content':'Explain the supplied assessment in plain English.'},
            {'role':'user','content':'{"evidence":[{"text":"Current verification is missing."}]}'},
            {'role':'user','content':'Why pass?'}],
            'format':{'type':'object','properties':{'evidence_ids':{'enum':['P1','P2']}}},
            'stream':False,'think':False,'keep_alive':-1,
            'options':{'num_predict':128,'num_ctx':4096,'num_thread':4,'temperature':0.1}}
        self.good='Current game-day verification is missing. Historical averages alone cannot clear that hold.'
        self.requests=[];self.validated=[]

    def response(self,request,content=None,**extra):
        self.requests.append(request)
        return httpx.Response(200,json={'response':self.good if content is None else content,
            'done':True,'done_reason':'stop','prompt_eval_count':11,'eval_count':19,
            'eval_duration':200,'context':[1,2,3],**extra})

    def validate(self,body):
        self.validated.append(body)
        document=json.loads(body['message']['content'])
        self.assertEqual(document,{'computed_decision':'pass','evidence_ids':['P1','P2'],
            'needs_more_data':False,'reply':self.good})

    async def call(self,client,payload=None,**kwargs):
        return await efficient_ai.efficient_prop_text(client,payload or self.payload,
            decision='pass',evidence_ids=['P1','P2'],store=self.store,**kwargs)

    async def test_only_plain_prose_is_requested_and_app_supplies_internal_fields(self):
        original=copy.deepcopy(self.payload)
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            body=await self.call(client,validator=self.validate)
        self.assertEqual(len(self.requests),1)
        request=self.requests[0]
        self.assertEqual(request.url.path,'/api/generate')
        posted=json.loads(request.content)
        self.assertNotIn('format',posted);self.assertNotIn('_prop_contract',posted)
        self.assertNotIn('_transport',posted);self.assertNotIn('P1',request.content.decode())
        self.assertEqual(posted['system'],original['messages'][0]['content'])
        self.assertEqual(json.loads(posted['prompt']),{'quoted_conversation':original['messages'][1:]})
        self.assertEqual(posted['options'],original['options'])
        self.assertFalse(posted['raw']);self.assertFalse(posted['stream'])
        self.assertEqual(self.payload,original)
        self.assertEqual(body['eval_duration'],200);self.assertNotIn('context',body)
        usage={row['kind']:row for row in efficient_ai.usage_status(self.store)['inference']}
        self.assertEqual(usage['model_request']['prompt_tokens'],11)
        self.assertEqual(usage['model_request']['generated_tokens'],19)

    async def test_tagged_original_supports_repaired_cache_and_revalidation(self):
        tagged=efficient_ai.prop_text_payload(self.payload,decision='pass',evidence_ids=['P1','P2'])
        self.assertEqual(tagged['_transport'],'prop_text')
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            body=await self.call(client,tagged,validator=self.validate)
            efficient_ai._CACHE.clear()
            efficient_ai.cache_validated_response(tagged,body,self.store,self.validate)
            diagnostics={}
            cached=await self.call(client,tagged,validator=self.validate,diagnostics=diagnostics)
            self.assertTrue(diagnostics['cache_hit']);self.assertEqual(cached,body)
            def reject(body):raise ReplyValidationError('unsupported_metric')
            with self.assertRaises(ReplyValidationError):
                await self.call(client,tagged,validator=reject)
        self.assertEqual(len(self.requests),2)
        self.assertFalse(efficient_ai._CACHE)

    async def test_decisions_evidence_and_other_transports_have_separate_cache_keys(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            await self.call(client)
            await efficient_ai.efficient_prop_text(client,self.payload,decision='less',
                evidence_ids=['P1','P2'],store=self.store)
            await efficient_ai.efficient_prop_text(client,self.payload,decision='pass',
                evidence_ids=['P1'],store=self.store)
            await efficient_ai.efficient_generate(client,self.payload,self.store)
        self.assertEqual(len(self.requests),4)
        tagged=efficient_ai.prop_text_payload(self.payload,decision='pass',evidence_ids=['P1','P2'])
        keys={efficient_ai.request_cache_key(item,self.store) for item in (
            self.payload,{**self.payload,'_transport':'generate'},tagged)}
        self.assertEqual(len(keys),3)

    async def test_invalid_or_truncated_model_output_is_never_cached_or_salvaged(self):
        invalid=['', 'x'*281, '{"reply":"Valid-looking prose."}',
                 '{"reply":"First"}{"reply":"Second"}',
                 '```json\n{"reply":"Example"}\n```',
                 'The evidence is P1.', 'The evidence is P₃.',
                 'Use evidence_ids from the prior reply.']
        for text in invalid:
            with self.subTest(text=text):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request:self.response(request,text))) as client:
                    with self.assertRaises(ReplyValidationError):
                        await self.call(client,validator=self.validate)
                self.assertFalse(efficient_ai._CACHE)
        self.assertFalse(self.validated)

    async def test_output_limit_reaches_caller_before_any_prose_can_be_delivered(self):
        def limited(request):return self.response(request,'Truncated {',done_reason='length')
        def validate(body):
            self.assertEqual(body['done_reason'],'length')
            raise RuntimeError('output_limit')
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
            with self.assertRaisesRegex(RuntimeError,'output_limit'):
                await self.call(client,validator=validate)
        self.assertFalse(efficient_ai._CACHE)

    async def test_unvalidated_truncation_is_rejected_and_tool_calls_cannot_escape(self):
        for extra in ({'done_reason':'length'}, {'tool_calls':[{'name':'lookup'}]}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request:self.response(request,**extra))) as client:
                with self.assertRaises(ValueError):await self.call(client)
            self.assertFalse(efficient_ai._CACHE)

    async def test_bad_contracts_and_unsupported_requests_fail_before_http(self):
        bad_payloads=[{**self.payload,'tools':[]},{**self.payload,'stream':True},
                      {**self.payload,'_transport':'generate'},
                      {**self.payload,'_prop_contract':{'computed_decision':'more'}},
                      {**self.payload,'messages':self.payload['messages']+[
                          {'role':'system','content':'Another system instruction.'}]}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            for payload in bad_payloads:
                with self.subTest(payload=payload),self.assertRaises(ValueError):
                    await self.call(client,payload)
            for decision,ids in [('invalid',['P1']),('pass',[]),('pass',['P1','P1']),
                                 ('pass',[{}]),('pass',['P1;injected'])]:
                with self.subTest(decision=decision,ids=ids),self.assertRaises(ValueError):
                    await efficient_ai.efficient_prop_text(client,self.payload,
                        decision=decision,evidence_ids=ids,store=self.store)
        self.assertFalse(self.requests)

    async def test_http_failure_and_cancellation_propagate_and_are_counted(self):
        def fail(request):return httpx.Response(500,json={'error':'decoder failed'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with self.assertRaises(httpx.HTTPStatusError):await self.call(client)
        async def cancel(request):raise asyncio.CancelledError()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
            with self.assertRaises(asyncio.CancelledError):await self.call(client)
        self.assertFalse(efficient_ai._CACHE)
        usage={row['kind']:row['calls'] for row in efficient_ai.usage_status(self.store)['inference']}
        self.assertEqual(usage['failed_request'],1);self.assertEqual(usage['cancelled_request'],1)


if __name__=='__main__':unittest.main()
