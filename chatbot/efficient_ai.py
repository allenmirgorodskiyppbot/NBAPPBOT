"""Bounded reply reuse and inference counters; no claims about ACT billing."""
from __future__ import annotations

from collections import OrderedDict
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time
import unicodedata

_CACHE = OrderedDict()
MAX_CACHE_ITEMS = 32
CACHE_SECONDS = 180


def _store(store):
    return getattr(store,'master',store)


def data_revision(store):
    store=_store(store)
    if store is None or not callable(getattr(store,'connect',None)):
        return 'no-master'
    with store.connect() as db:
        parts=[tuple(db.execute('SELECT COUNT(*),MAX(updated_at) FROM '+table).fetchone())
               for table in ('player_games','games','news')]
        parts.append(tuple(db.execute('SELECT COUNT(*),MAX(created_at) FROM boards').fetchone()))
        parts.append(tuple(db.execute('SELECT COUNT(*),MAX(graded_at),MAX(settled_at) FROM entries').fetchone()))
        from season_calendar import current_season
        for key in ('research_evidence_revision','assignment_refresh_status',
                    'season_calendar:'+current_season(dt.datetime.now(dt.timezone.utc))):
            row=db.execute('SELECT value_json FROM checkpoints WHERE name=?',(key,)).fetchone()
            value=json.loads(row[0]) if row else None
            parts.append(value.get('revision') if isinstance(value,dict) else value)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='refreshed_tracking'").fetchone():
            parts.append([tuple(row) for row in db.execute('SELECT season,entity,category,payload_json FROM refreshed_tracking ORDER BY season,entity,category')])
    return hashlib.sha256(json.dumps(parts,sort_keys=True,default=str).encode()).hexdigest()


def record_usage(store, kind, seconds=0, body=None):
    store=_store(store)
    if store is None or not callable(getattr(store,'connect',None)):
        return
    stamp=dt.datetime.now(dt.timezone.utc).isoformat()
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS inference_usage (kind TEXT PRIMARY KEY,calls INTEGER NOT NULL,'
                   'seconds REAL NOT NULL,prompt_tokens INTEGER NOT NULL,generated_tokens INTEGER NOT NULL,last_at TEXT)')
        def count(key):
            value=(body or {}).get(key)
            return value if type(value) is int and value>=0 else 0
        db.execute('INSERT INTO inference_usage VALUES (?,?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET '
                   'calls=calls+1,seconds=seconds+excluded.seconds,prompt_tokens=prompt_tokens+excluded.prompt_tokens,'
                   'generated_tokens=generated_tokens+excluded.generated_tokens,last_at=excluded.last_at',
                   (kind,1,max(0,seconds),count('prompt_eval_count'),count('eval_count'),stamp))


def usage_status(store):
    store=_store(store)
    with store.connect() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='inference_usage'").fetchone()
        rows=[dict(row) for row in db.execute('SELECT * FROM inference_usage ORDER BY kind')] if exists else []
    return {'inference':rows,'reply_cache_seconds':CACHE_SECONDS,'max_cached_replies':MAX_CACHE_ITEMS,
            'billing':'Akash credits fund the reserved running lease, not individual bot messages. '
                      'These counters measure inference work, not ACT cost. Code efficiency alone does not change the lease price.'}


def request_cache_key(payload, store=None):
    actual=_store(store)
    identity=str(getattr(actual,'path',getattr(actual,'database_path','no-master')))
    return hashlib.sha256(json.dumps([identity,data_revision(store),payload],sort_keys=True,default=str).encode()).hexdigest()


def cache_validated_response(payload, body, store, validator):
    """Reuse a repaired answer only after the original request's contract passes."""
    validator(body)
    if _store(store) is None:
        return
    key=request_cache_key(payload,store)
    _CACHE[key]=(time.monotonic(),json.dumps(body))
    _CACHE.move_to_end(key)
    while len(_CACHE)>MAX_CACHE_ITEMS:
        _CACHE.popitem(last=False)


async def efficient_chat(client, payload, store=None, url=None, diagnostics=None, validator=None):
    # Database identity prevents identical fixture/user contexts from sharing results.
    actual=_store(store)
    key=request_cache_key(payload,store)
    current=time.monotonic()
    cached=_CACHE.get(key)
    if diagnostics is not None:
        diagnostics['cache_hit'] = False
        diagnostics['response_received'] = False
    if cached and current-cached[0] < CACHE_SECONDS:
        cached_body=json.loads(cached[1])
        try:
            if validator is not None:
                validator(cached_body)
        except (ValueError, TypeError, RuntimeError):
            _CACHE.pop(key,None)
        else:
            if diagnostics is not None:
                diagnostics['cache_hit'] = True
            _CACHE.move_to_end(key)
            record_usage(store,'cache_hit')
            return cached_body
    started=time.monotonic()
    try:
        response=await client.post((url or os.getenv('OLLAMA_URL','http://model:11434')).rstrip('/')+'/api/chat',json=payload)
        response.raise_for_status()
        body=response.json()
        if diagnostics is not None:
            diagnostics['response_received'] = True
    except asyncio.CancelledError:
        record_usage(store,'cancelled_request',time.monotonic()-started)
        raise
    except Exception:
        record_usage(store,'failed_request',time.monotonic()-started)
        raise
    record_usage(store,'model_request',time.monotonic()-started,body if isinstance(body,dict) else {})
    if validator is not None:
        validator(body)  # Rejected replies must never enter the response cache.
    message=body.get('message') if isinstance(body,dict) else None
    if (actual is not None and isinstance(message,dict) and not body.get('error') and body.get('done_reason')!='length'
            and (message.get('content') or message.get('tool_calls'))):
        _CACHE[key]=(time.monotonic(),json.dumps(body))
        _CACHE.move_to_end(key)
        while len(_CACHE)>MAX_CACHE_ITEMS:
            _CACHE.popitem(last=False)
    return body


def _completion_payload(payload):
    """Quote the existing no-tools dialogue without changing its model contract."""
    allowed={'model','messages','format','options','think','stream','keep_alive','_transport'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise ValueError('Completion transport accepts only a bounded no-tools chat payload.')
    if payload.get('stream',False) is not False:
        raise ValueError('Completion transport requires stream=False.')
    if payload.get('_transport','generate') != 'generate':
        raise ValueError('Invalid completion transport marker.')
    messages=payload.get('messages')
    if not isinstance(messages,list) or not messages:
        raise ValueError('Completion transport requires a system-led dialogue.')
    for index,message in enumerate(messages):
        if (not isinstance(message,dict) or set(message)-{'role','content'}
                or message.get('role') not in ({'system'} if index==0 else {'user','assistant'})
                or not isinstance(message.get('content'),str)):
            raise ValueError('Completion dialogue cannot contain tools, images or extra system instructions.')
    request={key:payload[key] for key in ('model','format','options','think','keep_alive') if key in payload}
    request.update(system=messages[0]['content'],
        prompt=json.dumps({'quoted_conversation':messages[1:]},ensure_ascii=False,separators=(',',':')),
        raw=False,stream=False)
    return request


class _CompletionResponse:
    def __init__(self,response):self.response=response

    def raise_for_status(self):return self.response.raise_for_status()

    def json(self):
        body=self.response.json()
        if not isinstance(body,dict):
            return body
        if body.get('tool_calls') or (isinstance(body.get('message'),dict) and body['message'].get('tool_calls')):
            raise ValueError('Completion transport cannot return tool calls.')
        content=body.get('response','')
        if not isinstance(content,str):
            raise ValueError('Completion response must contain text.')
        result={key:value for key,value in body.items() if key not in {'response','context','message'}}
        result['message']={'role':'assistant','content':content}
        return result


class _CompletionClient:
    def __init__(self,client):self.client=client

    async def post(self,endpoint,*,json):
        if not endpoint.endswith('/api/chat'):
            raise ValueError('Unexpected completion adapter endpoint.')
        response=await self.client.post(endpoint[:-len('/api/chat')]+'/api/generate',
                                       json=_completion_payload(json))
        return _CompletionResponse(response)


async def efficient_generate(client,payload,store=None,url=None,diagnostics=None,validator=None):
    """No-tools completion with the same validation, counters and bounded cache.

    The internal marker distinguishes native chat and completion cache entries;
    it is never sent to the model. Keep this marker when caching a repaired
    original request with ``cache_validated_response``.
    """
    _completion_payload(payload)  # Reject unsupported requests before any cache or HTTP work.
    tagged={**payload,'_transport':'generate'}
    return await efficient_chat(_CompletionClient(client),tagged,store,url,diagnostics,validator)


def prop_text_payload(payload, *, decision, evidence_ids):
    """Tag a computed assessment for prose-only inference and repair caching.

    The application already owns these three result fields. They are included
    in the cache identity, never in the model request. An internal ``format``
    schema may remain in the payload for the application's reply contract, but
    it is deliberately omitted from the completion API request.
    """
    allowed={'model','messages','format','options','think','stream','keep_alive',
             '_transport','_prop_contract'}
    if not isinstance(payload,dict) or set(payload)-allowed:
        raise ValueError('Prop prose accepts only a bounded no-tools payload.')
    if decision not in {'more','less','pass'}:
        raise ValueError('Prop prose requires a computed decision.')
    if (not isinstance(evidence_ids,(list,tuple)) or not evidence_ids
            or len(evidence_ids)>4
            or any(not isinstance(item,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}',item)
                   for item in evidence_ids)
            or len(set(evidence_ids))!=len(evidence_ids)):
        raise ValueError('Prop prose requires bounded application evidence IDs.')
    contract={'computed_decision':decision,'evidence_ids':list(evidence_ids),'needs_more_data':False}
    if payload.get('_transport','prop_text')!='prop_text':
        raise ValueError('Invalid prop prose transport marker.')
    if '_prop_contract' in payload and payload['_prop_contract']!=contract:
        raise ValueError('The prop prose application contract cannot change.')
    tagged={**payload,'_transport':'prop_text','_prop_contract':contract}
    _prop_completion_payload(tagged)
    return tagged


def _prop_completion_payload(payload):
    public={key:value for key,value in payload.items() if key not in {'format','_prop_contract'}}
    public['_transport']='generate'
    return _completion_payload(public)


class _PropTextResponse(_CompletionResponse):
    def __init__(self,response,contract):
        super().__init__(response)
        self.contract=contract

    def json(self):
        body=super().json()
        if isinstance(body,dict):
            content=body['message']['content']
            body['message']['content']=json.dumps({**self.contract,'reply':content.strip()},ensure_ascii=False)
        return body


class _PropTextClient:
    def __init__(self,client):self.client=client

    async def post(self,endpoint,*,json):
        if not endpoint.endswith('/api/chat'):
            raise ValueError('Unexpected prop prose adapter endpoint.')
        response=await self.client.post(endpoint[:-len('/api/chat')]+'/api/generate',
                                       json=_prop_completion_payload(json))
        return _PropTextResponse(response,json['_prop_contract'])


def _validate_prop_text(body,contract):
    """Validate the whole prose string; never extract or salvage model JSON."""
    from grounded_reply import ReplyValidationError
    try:
        document=json.loads(body['message']['content'])
    except (KeyError,TypeError,ValueError):
        raise ReplyValidationError('invalid_schema') from None
    if (not isinstance(document,dict) or set(document)!={*contract,'reply'}
            or any(document.get(key)!=value for key,value in contract.items())):
        raise ReplyValidationError('invalid_schema')
    reply=document.get('reply')
    if not isinstance(reply,str) or not reply.strip():
        raise ReplyValidationError('empty_reply')
    if len(reply)>280:
        raise ReplyValidationError('analysis_too_long')
    normalized=unicodedata.normalize('NFKC',reply)
    if (normalized.lstrip().startswith(('{','[','"')) or '`' in normalized
            or re.search(r'\b(?:computed_decision|evidence_ids|needs_more_data)\b',normalized)):
        raise ReplyValidationError('invalid_schema')
    if (any(re.search(r'(?<!\w)'+re.escape(item)+r'(?!\w)',normalized,re.I)
            for item in contract['evidence_ids'])
            or re.search(r'\b[A-Z]{1,3}\d+\b',normalized)):
        raise ReplyValidationError('invalid_evidence')


async def efficient_prop_text(client,payload,*,decision,evidence_ids,store=None,url=None,
                              diagnostics=None,validator=None):
    """Generate only qualitative prose for an already computed assessment.

    ``prop_text_payload`` returns the tagged original request to retain when
    caching a repaired normalized body with ``cache_validated_response``.
    Every cache hit still passes this prose contract and the caller's complete
    factual/decision validator. Truncation is left for that validator to report
    before the reply can be cached or delivered.
    """
    tagged=prop_text_payload(payload,decision=decision,evidence_ids=evidence_ids)
    def validate(body):
        # Preserve the caller's established model-error/output-limit handling.
        if isinstance(body,dict) and (body.get('error') or body.get('done_reason')=='length'):
            if validator is not None:
                return validator(body)
            raise ValueError('Prop prose inference did not finish.')
        _validate_prop_text(body,tagged['_prop_contract'])
        if validator is not None:
            return validator(body)
    return await efficient_chat(_PropTextClient(client),tagged,store,url,diagnostics,validate)
