"""Completion transport preserves source validation and native-chat isolation."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

import efficient_ai
from master import MasterStore


class EfficientCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.store=MasterStore(Path(self.folder.name)/'master.sqlite')
        efficient_ai._CACHE.clear()
        self.payload={'model':'fixture-model','messages':[
            {'role':'system','content':'Interpret only quoted source facts.'},
            {'role':'user','content':'{"facts":"saved approved facts"}'},
            {'role':'assistant','content':'An older answer, not current evidence.'},
            {'role':'user','content':'What should I do after the refresh?'}],
            'stream':False,'think':False,'keep_alive':-1,
            'format':{'type':'object','properties':{'reply':{'type':'string','maxLength':280,'pattern':r'^[^0-9]*$'}}},
            'options':{'num_predict':128,'num_ctx':4096,'num_thread':4,'temperature':0.1}}
        self.requests=[];self.validated=[]

    def validate(self,body):
        self.validated.append(body)
        if body['message']['content']!='approved':raise ValueError('Unverified facts')

    def response(self,request,content='approved'):
        self.requests.append(request)
        return httpx.Response(200,json={'model':'fixture-model','response':content,'done':True,
            'done_reason':'stop','prompt_eval_count':11,'eval_count':7,'prompt_eval_duration':100,
            'eval_duration':200,'context':[1,2,3]})

    async def test_endpoint_quoting_contract_normalization_validator_usage_and_cache(self):
        original=copy.deepcopy(self.payload);first_diag={};second_diag={}
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            first=await efficient_ai.efficient_generate(client,self.payload,self.store,'http://model:11434',first_diag,self.validate)
            second=await efficient_ai.efficient_generate(client,self.payload,self.store,'http://model:11434',second_diag,self.validate)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(str(self.requests[0].url),'http://model:11434/api/generate')
        body=json.loads(self.requests[0].content)
        self.assertEqual(body['system'],original['messages'][0]['content'])
        self.assertEqual(json.loads(body['prompt']),{'quoted_conversation':original['messages'][1:]})
        self.assertFalse(body['raw']);self.assertFalse(body['stream'])
        self.assertEqual(body['format'],original['format']);self.assertEqual(body['options'],original['options'])
        self.assertEqual(body['model'],original['model']);self.assertFalse(body['think'])
        self.assertNotIn('_transport',body);self.assertNotIn('messages',body)
        self.assertEqual(self.payload,original)
        self.assertEqual(first,second);self.assertEqual(first['message']['content'],'approved')
        self.assertEqual(first['prompt_eval_count'],11);self.assertEqual(first['eval_duration'],200)
        self.assertNotIn('context',first);self.assertNotIn('response',first)
        self.assertEqual(len(self.validated),2)
        self.assertFalse(first_diag['cache_hit']);self.assertTrue(second_diag['cache_hit'])
        counts={row['kind']:row for row in efficient_ai.usage_status(self.store)['inference']}
        self.assertEqual(counts['model_request']['calls'],1);self.assertEqual(counts['cache_hit']['calls'],1)
        self.assertEqual(counts['model_request']['prompt_tokens'],11)
        self.assertEqual(counts['model_request']['generated_tokens'],7)

    async def test_rejected_facts_are_not_cached_and_each_cache_hit_is_revalidated(self):
        contents=iter(['unverified','approved','approved'])
        def respond(request):return self.response(request,next(contents))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ValueError):
                await efficient_ai.efficient_generate(client,self.payload,self.store,validator=self.validate)
            await efficient_ai.efficient_generate(client,self.payload,self.store,validator=self.validate)
            def reject(body):raise ValueError('The contract changed')
            with self.assertRaises(ValueError):
                await efficient_ai.efficient_generate(client,self.payload,self.store,validator=reject)
        self.assertEqual(len(self.requests),3)
        self.assertEqual(len(efficient_ai._CACHE),0)

    async def test_native_chat_and_completion_cache_entries_cannot_mix(self):
        def respond(request):
            if request.url.path.endswith('/api/generate'):return self.response(request)
            self.requests.append(request)
            return httpx.Response(200,json={'message':{'role':'assistant','content':'approved'},'done_reason':'stop'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await efficient_ai.efficient_generate(client,self.payload,self.store,validator=self.validate)
            await efficient_ai.efficient_chat(client,self.payload,self.store,validator=self.validate)
        self.assertEqual([r.url.path for r in self.requests],['/api/generate','/api/chat'])
        self.assertNotEqual(efficient_ai.request_cache_key(self.payload,self.store),
            efficient_ai.request_cache_key({**self.payload,'_transport':'generate'},self.store))

    async def test_tools_streaming_extra_system_roles_and_unknown_fields_fail_before_http(self):
        bad=[{**self.payload,'tools':[]},{**self.payload,'tool_choice':'none'},
             {**self.payload,'stream':True},{**self.payload,'raw':True},
             {**self.payload,'messages':self.payload['messages']+[{'role':'tool','content':'tool result'}]},
             {**self.payload,'messages':self.payload['messages']+[{'role':'system','content':'new system'}]},
             {**self.payload,'messages':[self.payload['messages'][0],{'role':'assistant','content':'','tool_calls':[]}]},
             {**self.payload,'_transport':'chat'}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.response)) as client:
            for payload in bad:
                with self.subTest(payload=payload),self.assertRaises(ValueError):
                    await efficient_ai.efficient_generate(client,payload,self.store)
        self.assertFalse(self.requests);self.assertFalse(efficient_ai._CACHE)

    async def test_http_failure_and_cancellation_propagate_without_caching(self):
        def fail(request):
            self.requests.append(request);return httpx.Response(500,json={'error':'decoder failed'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await efficient_ai.efficient_generate(client,self.payload,self.store,validator=self.validate)
        async def cancel(request):raise asyncio.CancelledError()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
            with self.assertRaises(asyncio.CancelledError):
                await efficient_ai.efficient_generate(client,self.payload,self.store,validator=self.validate)
        self.assertFalse(efficient_ai._CACHE);self.assertFalse(self.validated)
        counts={row['kind']:row['calls'] for row in efficient_ai.usage_status(self.store)['inference']}
        self.assertEqual(counts['failed_request'],1);self.assertEqual(counts['cancelled_request'],1)


if __name__=='__main__':unittest.main()
