"""Explicit, bounded transport for general conversation.

The default DIALOGUE_BACKEND=ollama uses the existing local model. Hosted chat
stays off until an administrator sets DIALOGUE_BACKEND=openai and a private
OPENAI_API_KEY. OPENAI_CHAT_MODEL defaults to gpt-4.1-mini. Hosted requests have
API usage charges; this module never obtains credentials, enables billing or
falls back to a paid provider. Mock tests do not establish hosted answer quality.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re

import httpx

OPENAI_ENDPOINT = 'https://api.openai.com/v1/chat/completions'
DEFAULT_HOSTED_MODEL = 'gpt-4.1-mini'
MAX_OUTPUT_TOKENS = 512
MAX_SCHEMA_CHARS = 50_000
MAX_INPUT_CHARS = 64_000
MAX_CONTENT_CHARS = 16_000


class DialogueModelError(RuntimeError):
    """Expose a safe category, never a provider body, URL or credential."""

    def __init__(self, category):
        self.category = category
        super().__init__('Dialogue model request failed: ' + category + '.')


def _settings():
    backend = os.getenv('DIALOGUE_BACKEND', 'ollama').strip().casefold()
    if backend not in {'ollama', 'openai'}:
        raise DialogueModelError('unsupported_backend')
    if backend == 'ollama':
        if not os.getenv('OLLAMA_URL', 'http://model:11434').strip():
            raise DialogueModelError('missing_local_url')
        return backend, None, None
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key or len(key) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise DialogueModelError('missing_or_invalid_api_key')
    model = os.getenv('OPENAI_CHAT_MODEL', DEFAULT_HOSTED_MODEL).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}', model):
        raise DialogueModelError('invalid_hosted_model')
    return backend, key, model


def backend_status():
    """Return safe configuration only; no HTTP or verified connection claim."""
    try:
        backend, _, model = _settings()
    except DialogueModelError as error:
        backend = os.getenv('DIALOGUE_BACKEND', 'ollama').strip().casefold()
        return {'backend': backend if backend in {'ollama', 'openai'} else 'invalid',
                'model': None, 'configured': False, 'error': error.category}
    return {'backend': backend, 'configured': True,
            'model': model if backend == 'openai' else
                     os.getenv('OLLAMA_MODEL', 'qwen3:4b-instruct-2507-q4_K_M')}


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 120:
        raise DialogueModelError('invalid_timeout')
    return float(value)


def _messages(payload):
    if not isinstance(payload, dict):
        raise DialogueModelError('invalid_payload')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        raise DialogueModelError('invalid_messages')
    clean = []
    for message in messages:
        if (not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant'}
                or not isinstance(message.get('content'), str) or not message['content'].strip()
                or message.get('tool_calls') or message.get('function_call')):
            raise DialogueModelError('invalid_messages')
        clean.append({'role': message['role'], 'content': message['content']})
    if sum(len(message['content']) for message in clean) > MAX_INPUT_CHARS:
        raise DialogueModelError('input_too_large')
    return clean


def _hosted_payload(payload, model):
    messages = _messages(payload)
    schema = payload.get('format')
    if schema is not None:
        if not isinstance(schema, dict):
            raise DialogueModelError('invalid_response_contract')
        try:
            encoded = json.dumps(schema, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise DialogueModelError('invalid_response_contract') from None
        if len(encoded) > MAX_SCHEMA_CHARS:
            raise DialogueModelError('response_contract_too_large')
        contract = ('Return only JSON matching this application-owned response contract. '
                    'Use only its documented fields and reader arguments. The application '
                    'will validate the result before any reader or reply is used.\n' + encoded)
    else:
        contract = 'Return only a JSON object. The application will validate the response.'
    if messages[0]['role'] == 'system':
        messages[0] = {**messages[0], 'content': messages[0]['content'] + '\n\n' + contract}
    else:
        messages.insert(0, {'role': 'system', 'content': contract})
    if sum(len(message['content']) for message in messages) > MAX_INPUT_CHARS:
        raise DialogueModelError('input_too_large')
    options = payload.get('options') if isinstance(payload.get('options'), dict) else {}
    temperature = options.get('temperature', 0.1)
    if type(temperature) not in (int, float) or not math.isfinite(temperature):
        temperature = 0.1
    # No arbitrary tools, runtime options, destinations or caller model names.
    return {'model': model, 'messages': messages, 'stream': False,
            'response_format': {'type': 'json_object'},
            'max_completion_tokens': MAX_OUTPUT_TOKENS,
            'temperature': max(0, min(1, temperature)), 'n': 1}


def _hosted_result(value, model):
    if not isinstance(value, dict) or value.get('error'):
        raise DialogueModelError('invalid_response')
    choices = value.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise DialogueModelError('invalid_response')
    choice = choices[0]
    if choice.get('finish_reason') != 'stop':
        raise DialogueModelError('incomplete_response')
    message = choice.get('message')
    if (not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant'
            or message.get('tool_calls') or message.get('function_call') or message.get('refusal')
            or not isinstance(message.get('content'), str) or not message['content'].strip()
            or len(message['content']) > MAX_CONTENT_CHARS):
        raise DialogueModelError('invalid_response')
    return {'message': {'role': 'assistant', 'content': message['content']},
            'done': True, 'done_reason': 'stop', 'model': model, 'dialogue_backend': 'openai'}


async def _request(payload, backend, key, model, client, seconds):
    if backend == 'ollama':
        from efficient_ai import efficient_chat
        return await efficient_chat(client, payload, None,
                                    os.getenv('OLLAMA_URL', 'http://model:11434').strip().rstrip('/'))
    response = await client.post(OPENAI_ENDPOINT, json=_hosted_payload(payload, model),
        headers={'Authorization': 'Bearer ' + key},
        timeout=httpx.Timeout(seconds, connect=min(10, seconds)), follow_redirects=False)
    if response.status_code in {401, 403}:
        raise DialogueModelError('access_refused')
    if response.status_code == 429:
        raise DialogueModelError('rate_limited')
    if not 200 <= response.status_code < 300:
        raise DialogueModelError('provider_error')
    try:
        value = response.json()
    except (ValueError, UnicodeError):
        raise DialogueModelError('invalid_response') from None
    return _hosted_result(value, model)


async def invoke_dialogue(payload, *, timeout=45, client=None):
    """One bounded call to the explicitly selected backend; no retries.

    Injected clients remain caller-owned. Validate configuration and input before
    creating a client or making a hosted request. Cancellation is never swallowed.
    """
    seconds = _seconds(timeout)
    backend, key, model = _settings()
    _messages(payload)
    if backend == 'openai':
        _hosted_payload(payload, model)
    try:
        async with asyncio.timeout(seconds):
            if client is not None:
                return await _request(payload, backend, key, model, client, seconds)
            async with httpx.AsyncClient(timeout=httpx.Timeout(seconds, connect=min(10, seconds)),
                                         follow_redirects=False) as owned:
                return await _request(payload, backend, key, model, owned, seconds)
    except asyncio.CancelledError:
        raise
    except DialogueModelError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise DialogueModelError('timeout') from None
    except httpx.HTTPError:
        raise DialogueModelError('connection_error') from None
    except Exception:
        raise DialogueModelError('request_error') from None
