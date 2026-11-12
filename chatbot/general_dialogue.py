"""Open conversation over typed, read-only master-data readers.

The model chooses evidence and explains it. It never chooses the statistical
population, computes a wager decision or changes application state by prose.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import re
import time

log = logging.getLogger('knicks.general_dialogue')
TOTAL_SECONDS = 95
SYNTHESIS_SECONDS = 45
MAX_EVIDENCE_CHARS = 2700

SYNTHESIS_INSTRUCTIONS = (
    'Answer the actual question in at most three short sentences, using the supplied reader facts. '
    'Explain mechanisms, comparisons and conditional scenarios; do not repeat an old assessment '
    'or schedule warning unless it answers this question. Saved observations are partial history, '
    'not current health, a forecast or a win probability. Never invent facts, compute new numbers '
    'or recommend a current side without an authoritative prop_assessment decision. '
    'A hypothetical is conditional, never confirmation. No source or URL from memory. '
    'Use only complete supplied facts for numeric claims, preserving entity, metric, period and qualifiers. '
    'Return JSON {"evidence_ids":[used IDs],"analysis":"answer","needs_more_data":false}. '
    'If evidence cannot answer, explain the specific missing information without guessing.'
)


async def _call(callback, *args, **kwargs):
    value = callback(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _focus(turn):
    turn = turn if isinstance(turn, dict) else {}
    slots = dict(turn.get('slots') or {})
    # Parser ambiguity is not permission for the planner to reuse an old quote.
    if turn.get('errors') or turn.get('clarification') and not turn.get('complete_prop'):
        slots.pop('line', None)
    state = turn.get('state') or {}
    if not slots and not (turn.get('entities') or {}).get('players'):
        slots = {key: value for key, value in (state.get('slots') or {}).items()
                 if key in {'player', 'stat', 'line', 'date'}}
    picks = turn.get('recent_picks') or []
    if not slots and len(picks) == 1 and not (turn.get('entities') or {}).get('players') and not turn.get('errors'):
        slots = {key: value for key, value in picks[0].items()
                 if key in {'player', 'stat', 'line', 'date', 'side'}}
    return {'slots': slots, 'recent_picks': turn.get('recent_picks') or [],
            **({'team': state['upcoming_game_team']} if state.get('upcoming_game_team') else {}),
            **({'game_context': state['game_context']} if state.get('game_context') else {})}


def _renumber(reads):
    cards = []
    groups = []
    for result in reads:
        group = []
        for original in result.get('cards') or []:
            if not isinstance(original, dict) or not original.get('text'):
                continue
            card = copy.deepcopy(original)
            card['id'] = 'E' + str(len(cards) + 1)
            cards.append(card)
            group.append(card)
        groups.append(group)
    # Preserve every requested entity's summary before adding detailed logs.
    return [group[level] for level in range(max((len(group) for group in groups), default=0))
            for group in groups if level < len(group)]


def _validation_cards(cards, reads, focus, *, allow_decisions):
    """A narrative cannot confer recommendation authority on an ordinary read."""
    result = copy.deepcopy(cards)
    authorized = {r.get('args', {}).get('player'): r.get('decision') for r in reads
                  if allow_decisions and r.get('tool') == 'prop_assessment'
                  and r.get('decision') in {'more', 'less', 'pass'}}
    entities = {c.get('entity') for c in cards if c.get('entity')}
    if focus.get('player'):
        entities.add(focus['player'])
    if not entities:
        entities.add(None)
    # Single authority per entity. Baseline/sample context must not combine an
    # older decision with the newly computed one and disable contradiction checks.
    for card in result:
        card.pop('decision', None)
    for entity in entities:
        result.append({'id': 'G' + str(len(result) + 1), 'entity': entity,
                       'kind': 'prop_assessment', 'decision': authorized.get(entity, 'pass'),
                       'text': 'Validation authority only; not a source fact.'})
    return result


def _parse_content(value):
    if isinstance(value, dict) and 'message' in value:
        if value.get('done_reason') == 'length' or value.get('done') is False or value.get('error'):
            raise ValueError('Incomplete model reply')
        message = value.get('message') or {}
        if message.get('tool_calls'):
            raise ValueError('Unexpected action')
        value = message.get('content')
    if isinstance(value, str):
        if len(value) > 8000:
            raise ValueError('Oversized model reply')
        value = json.loads(value)
    return value


def _render(document, cards, reads, focus, question, *, allow_decisions=False):
    from dialogue_authority import validate_authority
    from grounded_reply import render_natural_reply
    validate_authority(document.get('analysis', document.get('reply', '')) if isinstance(document, dict) else '',
                       cards=[card for card in cards if card.get('kind') not in {'metric_definition', 'user_offer_context'}],
                       reads=reads, focus=focus, allow_decisions=allow_decisions)
    validation = _validation_cards(cards, reads, focus, allow_decisions=allow_decisions)
    # Guard-only records stay out of evidence IDs, prompt facts and citations.
    ids = document.get('evidence_ids', []) if isinstance(document, dict) else []
    if any(identity not in {card['id'] for card in cards} for identity in ids):
        raise ValueError('Unretrieved citation')
    return render_natural_reply(document, validation, question=question)


def _definition_card():
    """Application definitions can support component counts, never observations."""
    from master import STAT_COMPONENTS
    from board_shortlist import MARKET_NAMES
    facts = []
    for stat in ('pr', 'pa', 'ra', 'pra', 'stocks'):
        components = STAT_COMPONENTS[stat]
        names = [MARKET_NAMES.get(component, component) for component in components]
        count = len(components)
        text = f"{MARKET_NAMES.get(stat, stat)} combines {count} statistics: " + ', '.join(names) + '.'
        facts.append({'text': text, 'entity': None, 'metric': stat, 'fact_type': 'definition',
                      'values': [count], 'scope': 'Metric construction; no player observation or prediction.'})
    return {'id': 'D1', 'kind': 'metric_definition', 'entity': None,
            'source': 'Installed statistic definitions', 'text': ' '.join(f['text'] for f in facts),
            'approved_facts': facts}


def _offer_context(focus):
    """Permit quoting the resolved input without verifying it as a live offer."""
    import math
    from board_shortlist import MARKET_NAMES
    player, stat, line = (focus.get(key) for key in ('player', 'stat', 'line'))
    if not player or stat not in MARKET_NAMES or type(line) not in (int, float) or not math.isfinite(line):
        return []
    text = f'{player}: the conversation supplied a line of {line:g} {MARKET_NAMES[stat]}. This is input context, not a verified current offer or forecast.'
    return [{'id': 'Q1', 'kind': 'user_offer_context', 'entity': player, 'source': 'Conversation input; offer unverified',
             'text': text, 'approved_facts': [{'text': text, 'entity': player, 'metric': stat,
                 'fact_type': 'line', 'values': [line], 'scope': 'Conversation input only; not verified live.'}]}]


def synthesis_payload(question, reads, cards, *, focus=None):
    from grounded_reply import natural_prompt_evidence
    from semantic_plan import DOMAIN_CONTEXT
    evidence = natural_prompt_evidence(cards, max_chars=MAX_EVIDENCE_CHARS)
    context = {'question': question[:900], 'evidence': evidence,
               'missing': list(dict.fromkeys(str(m)[:160] for r in reads for m in r.get('missing', [])))[:3]}
    if focus:
        context['reference'] = {k: v for k, v in focus.items() if k in {'player', 'stat', 'line', 'date', 'side'}}
    return {'model': os.getenv('OLLAMA_MODEL', 'qwen3:4b-instruct-2507-q4_K_M'),
            'messages': [{'role': 'system', 'content': SYNTHESIS_INSTRUCTIONS + ' ' + DOMAIN_CONTEXT},
                         {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, separators=(',', ':'))}],
            'format': {'type': 'object', 'properties': {
                'evidence_ids': {'type': 'array', 'items': {'type': 'string'}},
                'analysis': {'type': 'string', 'maxLength': 700}, 'needs_more_data': {'type': 'boolean'}},
                'required': ['evidence_ids', 'analysis', 'needs_more_data'], 'additionalProperties': False},
            'stream': False, 'think': False, 'keep_alive': -1,
            'options': {'temperature': 0.1, 'num_ctx': 4096, 'num_predict': 256,
                        'num_thread': _threads()}}


def _threads():
    try:
        return max(1, min(16, int(os.getenv('MODEL_THREADS', '4'))))
    except ValueError:
        return 4


async def _synthesize(payload):
    from dialogue_model import invoke_dialogue
    return await invoke_dialogue(payload, timeout=SYNTHESIS_SECONDS)


def _result(answer, *, sources=(), mode='facts', ai_used=False, diagnostics=None):
    return {'answer': answer, 'sources': list(sources), 'response_mode': 'general_' + mode,
            'ai_used': ai_used, 'conversation_diagnostics': diagnostics or {}}


def _fallback(reads, diagnostics):
    answers = list(dict.fromkeys(str(r.get('answer', '')).strip() for r in reads if r.get('answer')))
    if answers:
        sources = [source for r in reads for source in r.get('sources', [])]
        return _result('\n\n'.join(answers)[:3600], sources=sources, diagnostics=diagnostics)
    return _result('I couldn’t read the records needed for that question. Which player, statistic or topic should I check?',
                   mode='clarification', diagnostics=diagnostics)


def _connection_failure(category, reads, diagnostics):
    """A connection failure is not an unclear question or discarded data."""
    messages = {
        'access_refused': 'The AI provider refused access. Check the selected engine’s credentials; /ai_status shows details.',
        'rate_limited': 'The AI provider is temporarily limiting requests. Please try again shortly.',
        'timeout': 'The AI connection timed out. Please try again.',
        'connection_error': 'The bot couldn’t reach its AI provider. Please try again shortly.',
        'provider_error': 'The AI provider could not complete the request. Please try again shortly.',
        'invalid_response': 'The AI provider returned an unreadable reply. Please try again.',
        'incomplete_response': 'The AI provider returned an incomplete reply. Please try again.',
    }
    text = messages.get(category, 'The conversation engine could not complete its request. Check /ai_status for details.')
    if category in {'unsupported_backend', 'missing_or_invalid_api_key', 'missing_local_url', 'invalid_hosted_model'}:
        text = 'The conversation engine is not configured correctly. Check its server settings; /ai_status shows safe details.'
    diagnostics['dialogue_failure_category'] = category
    if reads:
        result = _fallback(reads, diagnostics)
        result['answer'] = text + '\n\nSaved records:\n' + result['answer']
    else:
        result = _result(text, mode='connection_error', diagnostics=diagnostics)
    result['ai_failure_category'] = category
    return result


async def answer_general_question(master, request, progress, *, turn=None, infer=None,
                                  synthesize=None, read=None, now=None):
    """One semantic plan, at most three typed reads and one optional explanation.

    Injection seams accept the same validated protocol as production. Neither
    planner prose accompanying reads nor unvalidated synthesis is displayed.
    """
    from semantic_plan import (infer_plan, validate_plan, dialogue_failure_category,
                               DIALOGUE_FAILURE_CATEGORIES)
    from conversation_readers import execute_read
    started = time.monotonic()
    diagnostics = {'model_calls': 0, 'reads': 0, 'phase': 'planning'}
    if isinstance(progress, dict):
        progress.pop('dialogue_failure_category', None)
    if turn is None and master is not None:
        from conversation import resolve_conversation
        history = [item.model_dump() if hasattr(item, 'model_dump') else item for item in getattr(request, 'history', [])]
        turn = await asyncio.to_thread(resolve_conversation, None, master, request.message, history,
                                      now=now, preferences=getattr(request, 'lineup_preferences', None))
    focus = _focus(turn)
    slots = focus.get('slots') or {}
    plan = None
    reads = []
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            diagnostics['model_calls'] += 1
            plan = validate_plan(await _call(infer or infer_plan, request, focus=focus, progress=progress))
            if plan is None:
                category = progress.get('dialogue_failure_category') if isinstance(progress, dict) else None
                if category in DIALOGUE_FAILURE_CATEGORIES:
                    return _connection_failure(category, reads, diagnostics)
                return None  # Caller may still provide an exact deterministic lookup.
            goal = plan['goal']
            diagnostics['goal'] = goal
            if goal == 'clarify':
                from dialogue_authority import validate_authority
                validate_authority(plan['clarification'], focus=slots)
                return _result(plan['clarification'], mode='clarification', ai_used=True, diagnostics=diagnostics)
            if not plan['reads']:
                from grounded_reply import _numbers
                definitions = [_definition_card()] if _numbers(plan['reply']) else []
                definitions += _offer_context(slots) if _numbers(plan['reply']) else []
                rendered = _render({'evidence_ids': [c['id'] for c in definitions], 'analysis': plan['reply'], 'needs_more_data': False},
                                   definitions, [], slots, request.message)
                return {**rendered, 'ai_used': True, 'response_mode': 'general_discussion',
                        'conversation_diagnostics': diagnostics} if rendered else None
            diagnostics['phase'] = 'reading'
            progress['detail'] = 'Reading the relevant saved master records'
            seen = set()
            for task in plan['reads']:
                task = copy.deepcopy(task)
                identity = json.dumps(task, sort_keys=True, separators=(',', ':'))
                if identity in seen:
                    continue
                seen.add(identity)
                if task['tool'] == 'prop_assessment' and 'line' in task['args']:
                    # The semantic model may select a reader, never invent or
                    # transfer an offered threshold that the input parser cleared.
                    from pick_discussion import normalize_stat
                    from master import normalized_name
                    args = task['args']
                    same_player = not args.get('player') or normalized_name(args['player']) == normalized_name(slots.get('player', ''))
                    if not same_player and args.get('player') and slots.get('player'):
                        from conversation_readers import _player
                        try:
                            canonical_player = _player(master, args['player'])[0]
                            same_player = normalized_name(canonical_player) == normalized_name(slots['player'])
                            if same_player:
                                args['player'] = canonical_player
                        except ValueError:
                            pass
                    same_stat = not args.get('stat') or normalize_stat(args['stat']) == normalize_stat(slots.get('stat', ''))
                    if not (same_player and same_stat and slots.get('line') == args['line']):
                        return _result('What is the offered line for this player and statistic?',
                                       mode='clarification', diagnostics=diagnostics)
                if read is None:
                    result = await asyncio.to_thread(execute_read, master, task, focus=slots, now=now)
                else:
                    result = await _call(read, master, task, focus=slots, now=now)
                if isinstance(result, dict):
                    reads.append(result)
                    diagnostics['reads'] += 1
            # A factual lookup or actual computed side needs no presentation call.
            if goal in {'research', 'evaluate'}:
                return _fallback(reads, diagnostics)
            cards = _renumber(reads)
            if not cards:
                return _fallback(reads, diagnostics)
            diagnostics['phase'] = 'explaining'
            progress['detail'] = 'Explaining the retrieved facts in context'
            payload = synthesis_payload(request.message, reads, cards, focus=slots)
            included = {item['id'] for item in json.loads(payload['messages'][-1]['content'])['evidence']}
            diagnostics['omitted_evidence_count'] = len(cards) - len(included)
            cards = [card for card in cards if card['id'] in included]
            if not cards:
                return _fallback(reads, diagnostics)
            diagnostics['prompt_chars'] = sum(len(m['content']) for m in payload['messages'])
            diagnostics['model_calls'] += 1
            async with asyncio.timeout(SYNTHESIS_SECONDS):
                document = _parse_content(await _call(synthesize or _synthesize, payload))
            rendered = _render(document, cards, reads, slots, request.message, allow_decisions=goal == 'evaluate')
            if rendered:
                return {**rendered, 'ai_used': True, 'response_mode': 'general_explanation',
                        'conversation_diagnostics': diagnostics}
    except asyncio.CancelledError:
        raise
    except Exception as error:
        diagnostics['failure'] = type(error).__name__
        log.info('General conversation fallback: phase=%s error=%s', diagnostics['phase'], type(error).__name__)
        category = dialogue_failure_category(error)
        if category:
            if isinstance(progress, dict):
                progress['dialogue_failure_category'] = category
            return _connection_failure(category, reads, diagnostics)
    finally:
        diagnostics['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return _fallback(reads, diagnostics) if reads else None
