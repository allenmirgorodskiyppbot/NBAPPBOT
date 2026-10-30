"""Bounded reply reuse and inference counters; no claims about ACT billing."""
from __future__ import annotations

from collections import OrderedDict
import asyncio
import datetime as dt
import hashlib
import json
import os
import time

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
        for key in ('research_evidence_revision','season_calendar:'+current_season(dt.datetime.now(dt.timezone.utc))):
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


async def efficient_chat(client, payload, store=None, url=None, diagnostics=None):
    # Database identity prevents identical fixture/user contexts from sharing results.
    actual=_store(store)
    identity=str(getattr(actual,'path',getattr(actual,'database_path','no-master')))
    key=hashlib.sha256(json.dumps([identity,data_revision(store),payload],sort_keys=True,default=str).encode()).hexdigest()
    current=time.monotonic()
    cached=_CACHE.get(key)
    if diagnostics is not None:
        diagnostics['cache_hit'] = False
    if cached and current-cached[0] < CACHE_SECONDS:
        if diagnostics is not None:
            diagnostics['cache_hit'] = True
        _CACHE.move_to_end(key)
        record_usage(store,'cache_hit')
        return json.loads(cached[1])
    started=time.monotonic()
    try:
        response=await client.post((url or os.getenv('OLLAMA_URL','http://model:11434')).rstrip('/')+'/api/chat',json=payload)
        response.raise_for_status()
        body=response.json()
    except asyncio.CancelledError:
        record_usage(store,'cancelled_request',time.monotonic()-started)
        raise
    except Exception:
        record_usage(store,'failed_request',time.monotonic()-started)
        raise
    record_usage(store,'model_request',time.monotonic()-started,body if isinstance(body,dict) else {})
    message=body.get('message') if isinstance(body,dict) else None
    if (actual is not None and isinstance(message,dict) and not body.get('error') and body.get('done_reason')!='length'
            and (message.get('content') or message.get('tool_calls'))):
        _CACHE[key]=(time.monotonic(),json.dumps(body))
        _CACHE.move_to_end(key)
        while len(_CACHE)>MAX_CACHE_ITEMS:
            _CACHE.popitem(last=False)
    return body
