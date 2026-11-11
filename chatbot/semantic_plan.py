"""One bounded model plan for open-ended, read-only basketball conversation.

The plan describes an evidence need, not an answer's authority. Reader arguments
still go through the application registry; planner prose accompanying reads is
discarded before it can become a user-visible factual answer.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import json
import logging
import math
import os
import re
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger('knicks.semantic_plan')
PLAN_TIMEOUT_SECONDS = 45
MAX_PROMPT_CHARS = 3200
MAX_RESPONSE_CHARS = 8192
MAX_REPLY_CHARS = 800
MAX_CLARIFICATION_CHARS = 240
MAX_READS = 3
GOALS = ('explain', 'evaluate', 'research', 'compare', 'chat', 'clarify')
DOMAIN_CONTEXT = ('PR=points+rebounds; PA=points+assists; RA=rebounds+assists; PRA=all three. '
                  'FGA=2PA+3PA, excludes FTA; FTA=free-throw attempts. Points come from made shots and free throws. '
                  '3PM=made threes; 3PA=attempted threes. These are distinct valid markets. '
                  'Fewer minutes reduce opportunities, not necessarily per-minute efficiency.')

# These names also permit isolated planner validation while its reader module
# is being packaged. The installed registry remains the execution authority.
_CORE_TOOLS = ('player_games', 'player_profile', 'team_games', 'next_game', 'schedule',
               'availability', 'prop_assessment', 'research_table')
_CORE_CAPABILITIES = (
    'player_games(player,stat,window=5,opponent?,phase?,season?): final Played logs; career=all stored. '
    'player_profile(player,window=20): past averages. team_games(team,window=5): PAST final scores ONLY. '
    'next_game(team=NYK,date?,after_date?): fixture on/after date, or strictly after after_date. '
    'schedule(team=NYK,start_date?,end_date?,limit=10): calendar, including later fixtures. '
    'availability(player,date?): dated status; absent=unknown. '
    'prop_assessment(player,stat,line,date?,side?): authoritative decision. '
    'research_table(table,filters,limit=3): exact saved derivatives. ISO dates.'
)
_ARGUMENTS = {
    'player_games': {'player', 'stat', 'window', 'opponent', 'phase', 'season'},
    'player_profile': {'player', 'window'},
    'team_games': {'team', 'window'},
    'next_game': {'team', 'date', 'after_date'},
    'schedule': {'team', 'start_date', 'end_date', 'limit'},
    'availability': {'player', 'date'},
    'prop_assessment': {'player', 'stat', 'line', 'date', 'side'},
    'research_table': {'table', 'filters', 'limit'},
}
INSTRUCTIONS = (
    'NBA player-prop research assistant: plan JSON. Fixed glossary. Resolve temporal scope first. '
    'Conversation resolves references, never evidence. Reread facts before repeating prior stats, '
    'baselines or comparisons; pure mechanisms can be qualitative. '
    'Never invent players, teams, lines, dates or results. Changed player/market loses the old line. '
    'Goals: explain=reasoning, research=facts, '
    'compare=contrast, evaluate=choose/reassess a side, chat=discussion, clarify=missing essential input. '
    'Concepts need no player/reads: reply in 2-3 short qualitative sentences without figures. '
    'Saved facts/stats/injuries/schedules require reads(max3), never memory. Evaluate '
    'requires prop_assessment. Explanations need not qualify again. With reads, reply="". Clarify: '
    'reads=[],reply="",clarification=one specific question. No SQL/code/URLs/writes or fabricated predictions. '
    'Examples: concept -> {"goal":"explain","reads":[],"reply":"Minutes affect opportunities."}; '
    'assists facts -> {"goal":"research","reads":[{"tool":"player_games",'
    '"args":{"player":"name from question","stat":"ast","window":5}}],"reply":""}.'
)


def _registry():
    """Read only the installed registry's small public capability metadata."""
    try:
        import conversation_readers as readers
    except ImportError:
        return _CORE_TOOLS, _CORE_CAPABILITIES
    names = tuple(name for name in readers.TOOL_NAMES if isinstance(name, str)
                  and re.fullmatch(r'[a-z][a-z0-9_]{1,39}', name))
    if not names or len(names) > 16:
        return _CORE_TOOLS, _CORE_CAPABILITIES
    text = getattr(readers, 'PLAN_CAPABILITIES', readers.CAPABILITIES)
    text = text if isinstance(text, str) else _CORE_CAPABILITIES
    # Preserve complete signatures instead of truncating away the final tools.
    return names, text if len(text) <= 650 else _CORE_CAPABILITIES


def _clip(value, limit):
    if len(value) <= limit:
        return value
    marker = ' [middle omitted] '
    first = (limit-len(marker))*2//3
    return value[:first] + marker + value[-(limit-len(marker)-first):]


def _text(value, limit):
    return _clip(value.strip(), limit) if isinstance(value, str) and value.strip() else None


def _focus(value):
    """Bound trusted reference identity; no old model prose becomes evidence."""
    if not isinstance(value, dict):
        return None
    raw = value.get('slots') if isinstance(value.get('slots'), dict) else value
    slots = {}
    for key in ('player', 'stat', 'team', 'opponent', 'side'):
        if (item := _text(raw.get(key), 90 if key == 'player' else 24)):
            slots[key] = item
    if 'team' not in slots and (team := _text(value.get('team') or value.get('upcoming_game_team'), 24)):
        slots['team'] = team
    for key in ('game_date', 'date'):
        try:
            if raw.get(key) is not None:
                slots['date'] = dt.date.fromisoformat(str(raw[key])).isoformat()
                break
        except (TypeError, ValueError):
            continue
    line = raw.get('line')
    if type(line) in (int, float) and math.isfinite(line) and 0 <= line <= 1_000_000:
        slots['line'] = line
    result = {'slots': slots} if slots else {}
    context = value.get('game_context') or value.get('fixture')
    if isinstance(context, dict):
        fixture = {}
        try:
            game_date = dt.date.fromisoformat(str(context.get('date')))
            fixture['date'] = game_date.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
        for key in ('team', 'home_team', 'away_team'):
            if (item := _text(context.get(key), 12)):
                fixture[key] = item
        if fixture.get('date') and fixture.get('home_team') and fixture.get('away_team'):
            result['fixture'] = fixture
    choices = value.get('recent_picks') or value.get('picks') or []
    if isinstance(choices, list):
        picks = []
        for card in choices[:3]:
            if not isinstance(card, dict):
                continue
            brief = {key: text for key in ('player', 'stat', 'proposal_group')
                     if (text := _text(card.get(key), 90 if key == 'player' else 24))}
            if brief:
                picks.append(brief)
        if picks:
            result['picks'] = picks
    while len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > 300:
        if result.get('picks'):
            result['picks'].pop()
            if not result['picks']:
                result.pop('picks')
        elif len(result.get('slots', {})) > 3:
            for key in ('opponent', 'team', 'side', 'date'):
                if key in result['slots']:
                    result['slots'].pop(key)
                    break
            else:
                break
        elif len(result.get('slots', {}).get('player', '')) > 50:
            result['slots']['player'] = _clip(result['slots']['player'], 50)
        elif result.get('fixture', {}).get('team'):
            result['fixture'].pop('team')
        else:
            break
    return result or None


def _history(value):
    if not isinstance(value, (list, tuple)):
        return []
    recent = []
    for item in value[-2:]:
        if callable(getattr(item, 'model_dump', None)):
            item = item.model_dump()
        if not isinstance(item, dict) or item.get('role') not in {'user', 'assistant'}:
            continue
        if (content := _text(item.get('content'), 160)):
            recent.append({'role': item['role'], 'content': content})
    return recent


def _eastern_today():
    return dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()


def _read_schema(names):
    """Choose a reader and its exact argument schema together, never mixed args."""
    short = {'type': 'string', 'minLength': 1, 'maxLength': 150}
    window = {'type': 'integer', 'minimum': 1, 'maximum': 100}
    date = {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'}
    stat = {'type': 'string', 'enum': [
        'pts', 'reb', 'ast', 'pr', 'pa', 'ra', 'pra', 'fga', 'tpa', 'tpm',
        'minutes', 'stl', 'blk', 'stocks', 'tov', 'oreb', 'dreb', 'fta', 'ftm', 'fgm', 'fouls', 'plus_minus']}
    from current_research import TABLE_FILTERS
    research_args = {'oneOf': [
        {'type': 'object', 'properties': {
            'table': {'const': table},
            'filters': {'type': 'object', 'properties': {
                key: {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 120},
                                {'type': 'null'}]} for key in sorted(filters)},
                'additionalProperties': False},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
         'required': ['table'], 'additionalProperties': False}
        for table, filters in TABLE_FILTERS.items()]}
    definitions = {
        'player_games': ({'player': short, 'stat': stat, 'window': {'anyOf': [window, {'const': 'career'}]},
                          'opponent': short, 'phase': {'enum': ['Regular Season', 'Playoffs', 'Preseason']},
                          'season': short}, ['player', 'stat', 'window']),
        'player_profile': ({'player': short, 'window': window}, ['player']),
        'team_games': ({'team': short, 'window': window}, ['team']),
        'next_game': (None, None),
        'schedule': ({'team': short, 'start_date': date, 'end_date': date,
                       'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, []),
        'availability': ({'player': short, 'date': date}, ['player']),
        'prop_assessment': ({'player': short, 'stat': stat,
                             'line': {'type': 'number', 'minimum': 0, 'maximum': 1000000},
                             'date': date, 'side': {'enum': ['More', 'Less']}}, ['player', 'stat', 'line']),
        'research_table': (None, None),
    }
    choices = []
    for name in names:
        if name not in definitions:
            continue
        properties, required = definitions[name]
        if name == 'research_table':
            args = research_args
        elif name == 'next_game':
            args = {'oneOf': [
                {'type': 'object', 'properties': {'team': short, 'date': date},
                 'additionalProperties': False},
                {'type': 'object', 'properties': {'team': short, 'after_date': date},
                 'required': ['after_date'], 'additionalProperties': False}]}
        else:
            args = {'type': 'object', 'properties': properties,
                    'required': required, 'additionalProperties': False}
        choices.append({'type': 'object', 'properties': {
            'tool': {'const': name}, 'args': args},
            'required': ['tool', 'args'], 'additionalProperties': False})
    return {'oneOf': choices}


def plan_payload(request, focus=None):
    """Keep planning small enough for the installed CPU model's context."""
    latest = _text(getattr(request, 'message', None), 1000)
    if latest is None:
        return None
    names, capabilities = _registry()
    # Put reusable metadata before variable turns so local prompt-prefix caching
    # can retain the tool vocabulary across different questions.
    context = {'glossary': DOMAIN_CONTEXT, 'tools': capabilities, 'today_eastern': _eastern_today(),
               'history': _history(getattr(request, 'history', None))}
    if (reference := _focus(focus)):
        context['focus'] = reference
    context['question'] = latest
    def encoded():
        return json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    # Keep the actual newest question rather than evicting it for old replies.
    while len(INSTRUCTIONS) + len(encoded()) > MAX_PROMPT_CHARS and context['history']:
        context['history'].pop(0)
    if len(INSTRUCTIONS) + len(encoded()) > MAX_PROMPT_CHARS:
        context['tools'] = _CORE_CAPABILITIES
    # Long questions keep their beginning and final correction while resolved
    # reference identity survives; dropping that identity can change the query.
    while len(INSTRUCTIONS) + len(encoded()) > MAX_PROMPT_CHARS and len(context['question']) > 500:
        excess = len(INSTRUCTIONS) + len(encoded()) - MAX_PROMPT_CHARS
        context['question'] = _clip(context['question'], max(500, len(context['question']) - excess))
    if len(INSTRUCTIONS) + len(encoded()) > MAX_PROMPT_CHARS:
        context.pop('focus', None)
    if len(INSTRUCTIONS) + len(encoded()) > MAX_PROMPT_CHARS:
        return None
    common = {
        'goal': {'type': 'string', 'enum': [goal for goal in GOALS if goal != 'clarify']},
        'reads': {'type': 'array', 'maxItems': MAX_READS, 'items': _read_schema(names)},
        'reply': {'type': 'string', 'maxLength': MAX_REPLY_CHARS},
        'clarification': {'const': None}}
    schema = {'oneOf': [
        {'type': 'object', 'properties': common,
         'required': ['goal', 'reads', 'reply'], 'additionalProperties': False},
        {'type': 'object', 'properties': {
            'goal': {'const': 'clarify'}, 'reads': {'type': 'array', 'maxItems': 0},
            'reply': {'const': ''}, 'clarification': {'type': 'string', 'minLength': 1,
                                                     'maxLength': MAX_CLARIFICATION_CHARS}},
         'required': ['goal', 'reads', 'reply', 'clarification'], 'additionalProperties': False}]}
    try:
        threads = min(16, max(1, int(os.getenv('MODEL_THREADS', '4'))))
    except ValueError:
        threads = 4
    return {'model': os.getenv('OLLAMA_MODEL', 'qwen3:4b-instruct-2507-q4_K_M'),
            'messages': [{'role': 'system', 'content': INSTRUCTIONS},
                         {'role': 'user', 'content': encoded()}],
            'format': schema, 'stream': False, 'think': False, 'keep_alive': -1,
            'options': {'temperature': 0, 'num_ctx': 4096, 'num_predict': 256,
                        'num_thread': threads}}


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate plan field')
        value[key] = item
    return value


def _json_value(value):
    """Small JSON arguments only; no executable objects or nested task lists."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= 160
    if type(value) in (int, float):
        return math.isfinite(value) and abs(value) <= 1_000_000
    return False


def _task(value, names):
    if not isinstance(value, dict) or set(value) != {'tool', 'args'}:
        return None
    name, args = value['tool'], value['args']
    if not isinstance(name, str) or name not in names or not isinstance(args, dict) or len(args) > 10:
        return None
    allowed = _ARGUMENTS.get(name)
    if allowed is None or set(args)-allowed:
        return None
    clean = {}
    for key, item in args.items():
        if item is None:
            continue
        if key == 'filters':
            if (not isinstance(item, dict) or len(item) > 12 or any(
                    not isinstance(k, str) or not 0 < len(k) <= 64
                    or v is not None and (not isinstance(v, str) or len(v) > 120)
                    for k, v in item.items())):
                return None
            clean[key] = dict(item)
        elif key == 'window' and not (type(item) is int and 1 <= item <= 100 or item == 'career'):
            return None
        elif key == 'limit' and not (type(item) is int and 1 <= item <= 20):
            return None
        elif key == 'line' and not (type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 1_000_000):
            return None
        elif not _json_value(item):
            return None
        else:
            clean[key] = item
    if name == 'next_game' and {'date', 'after_date'} <= set(clean):
        return None
    return {'tool': name, 'args': clean}


def validate_plan(value):
    """Validate the protocol only; the reader still validates every argument."""
    if isinstance(value, dict) and 'message' in value:
        if value.get('error') or value.get('done_reason') == 'length' or value.get('done') is False:
            return None
        message = value.get('message')
        if (not isinstance(message, dict) or message.get('tool_calls')
                or message.get('role', 'assistant') != 'assistant'):
            return None
        value = message.get('content')
    if isinstance(value, str):
        if not 0 < len(value) <= MAX_RESPONSE_CHARS:
            return None
        try:
            value = json.loads(value, object_pairs_hook=_unique_fields,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
        except (TypeError, ValueError, RecursionError):
            return None
    if (not isinstance(value, dict) or set(value)-{'goal', 'reads', 'reply', 'clarification'}
            or not {'goal', 'reads', 'reply'} <= set(value)):
        return None
    goal, reads, reply, clarification = (value.get(key) for key in ('goal', 'reads', 'reply', 'clarification'))
    if (not isinstance(goal, str) or goal not in GOALS
            or not isinstance(reads, list) or len(reads) > MAX_READS
            or not isinstance(reply, str) or len(reply) > MAX_REPLY_CHARS
            or clarification is not None and (not isinstance(clarification, str)
                                               or len(clarification) > MAX_CLARIFICATION_CHARS)):
        return None
    names, _ = _registry()
    tasks = [_task(read, names) for read in reads]
    if any(task is None for task in tasks):
        return None
    reply = reply.strip()
    clarification = clarification.strip() if isinstance(clarification, str) else None
    if goal == 'clarify':
        if tasks or reply or not clarification:
            return None
    elif clarification:
        return None
    elif not tasks and (not reply or goal in {'research', 'evaluate'}):
        return None
    if goal == 'evaluate' and not any(task['tool'] == 'prop_assessment' for task in tasks):
        return None
    return {'goal': goal, 'reads': tasks, 'reply': '' if tasks else reply,
            'clarification': clarification or None}


async def infer_plan(request, *, focus=None, invoke=None, progress=None):
    """One model call, with no data scan or capability to mutate the bot."""
    try:
        payload = plan_payload(request, focus)
        if payload is None:
            return None
        if isinstance(progress, dict):
            progress['detail'] = 'Understanding the question and choosing relevant records'
        async with asyncio.timeout(PLAN_TIMEOUT_SECONDS):
            if invoke is not None:
                value = invoke(payload)
                if inspect.isawaitable(value):
                    value = await value
            else:
                from efficient_ai import efficient_chat
                endpoint = os.getenv('OLLAMA_URL', 'http://model:11434').rstrip('/')
                async with httpx.AsyncClient(timeout=httpx.Timeout(PLAN_TIMEOUT_SECONDS, connect=3)) as client:
                    value = await efficient_chat(client, payload, None, endpoint)
            return validate_plan(value)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.info('Conversation plan unavailable: %s', type(error).__name__)
        return None
