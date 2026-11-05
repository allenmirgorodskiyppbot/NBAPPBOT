"""Connect dialogue context, read-only prop analysis and natural explanation.

The language model explains computed evidence; history supplies conversational
references, never invented statistics or permission to create financial records.
"""
from __future__ import annotations

import asyncio
import re

from conversation import resolve_conversation
from pick_discussion import build_pick_discussion, format_pick_discussion
from master import normalized_name
from board_shortlist import MARKET_NAMES

MODEL_DISCUSSION_SECONDS = 35
_DEEP_DISCUSSION = re.compile(r'\b(?:compar(?:e|ing|ison)|in[- ]depth|in detail|detail(?:ed|s)?|'
    r'deep(?:er)?|breakdown|walk me through|tell me more|elaborat\w*|other factors|alternatives?|what else)\b', re.I)


def _wants_model(question):
    """Use generation for requested synthesis, not a computed side or short follow-up."""
    return bool(_DEEP_DISCUSSION.search(question))


def _direct_reply(answer, cards=(), *, sources=None):
    from grounded_reply import _natural_sources
    return {'answer': answer, 'sources': _natural_sources(cards) if cards else (sources or []),
            'ai_used': False, 'response_mode': 'computed',
            'grounding': ('Computed from checked saved evidence; current-game checks remain separate.'
                          if cards else 'Deterministic conversation; no current statistics asserted.')}


def _bounded_request(request):
    # Resolve slots from the full history first. The model only needs a small
    # presentation excerpt alongside the independently retrieved source cards.
    if not callable(getattr(request, 'model_copy', None)):
        return request
    history = []
    for item in request.history[-4:]:
        if not callable(getattr(item, 'model_copy', None)):
            return request
        history.append(item.model_copy(update={'content': item.content[:300]}))
    while sum(len(item.content) for item in history) > 900 and len(history) > 1:
        history.pop(0)
    return request.model_copy(update={'history': history})


def _context(turn):
    context = {key:turn[key] for key in ('intent','slots','constraints') if turn.get(key)}
    index = turn.get('state', {}).get('selected_pick_index')
    if type(index) is int and turn.get('recent_picks'):
        context['selected_pick'] = {'position':index+1,
            **{k:v for k,v in turn['recent_picks'][0].items() if k in ('player','stat','line_kind')}}
    return context


def _conversational_fallback(turn, question):
    """General mechanisms need no invented statistics or current-game claims."""
    query = question.casefold()
    if turn['intent'] == 'preference':
        if 'attempt' in query:
            return ('That preference makes sense: attempts count whether the shot goes in, while points also '
                    'depend on shooting accuracy and free throws. Both still depend on minutes and shot share, '
                    'so a blowout can hurt either. I’ll keep the actual attempts offer separate from the points line.')
        return ('I’ll keep that preference in mind when comparing eligible picks. It doesn’t change what the '
                'saved evidence supports; I’ll explain any conflict rather than switch sides just to agree.')
    if re.search(r'\b(?:thanks?|thank you|thank u|ty|thx)\b', query):
        return 'You’re welcome.'
    if re.fullmatch(r'\s*(?:ok(?:ay)?|cool|got it|sounds good|nice)[.!?\s]*', query):
        return 'Got it.'
    if re.fullmatch(r'\s*(?:hi|hello|hey)[.!?\s]*', query):
        return 'Hi, I’m here.'
    if re.search(r'blowout|bench|minutes|playing time', query):
        return ('If playing time drops, shot attempts and other counting stats can drop too. That’s a risk to '
                'check, not a prediction that anyone will be benched.')
    if re.search(r'revenge|old team|motivat', query):
        return ('Playing a former team might add motivation, but it doesn’t establish more shot opportunities '
                'or better shooting accuracy. I’d look for evidence about minutes and role before changing a pick.')
    if turn['intent'] == 'explanation':
        return 'Which pick or part of the explanation do you mean? I can explain the evidence, the line, or the main risk.'
    return 'I can help talk through a pick, compare the actual lines, or explain what could change the assessment.'


async def _generate_reply(generate, request, progress, master, cards, *, dialogue, fallback):
    """A failed explanation must still deliver the checked answer, never model prose."""
    try:
        async with asyncio.timeout(MODEL_DISCUSSION_SECONDS):
            result = await generate(_bounded_request(request), progress, master, cards, natural=True,
                                    dialogue=dialogue, fallback=fallback)
    except asyncio.CancelledError:
        raise
    except Exception:
        result = None
    if not isinstance(result, dict) or not str(result.get('answer') or '').strip():
        return _direct_reply(fallback, cards)
    return result


def _preference_evidence(store, master, turn, history):
    from basketball_research import resolve_question, research_context
    from grounded_reply import evidence_cards
    resolved = resolve_question(store, master, turn['canonical_question'], history)
    if turn.get('slots', {}).get('player'):
        resolved['players'] = [turn['slots']['player']]
    context = research_context(store, master, resolved)
    return evidence_cards(context, turn['canonical_question'])


def _unavailable_card(pick, reason, index):
    text = f"{pick['player']}: Hold. {reason}"
    return {'id': f'H{index}', 'kind': 'prop_unavailable', 'entity': pick['player'],
            'decision': 'pass', 'source': 'Saved pick input checks', 'text': text,
            'approved_facts': [{'entity': pick['player'], 'fact_type': 'context',
                                'text': text, 'values': []}]}


def _changed_readiness(master, checks):
    """A model may take long enough for a tipoff or source status to change."""
    from pick_discussion import _now
    from pregame_freshness import assess_pregame_freshness
    now = _now(None)
    held = []
    for item in checks:
        current = assess_pregame_freshness(master, item['date'], item['team'], now,
                                           player=item['player'], stat=item['stat'])
        if not current['ready']:
            held.append(f"{item['player']}: {current['reasons'][0]}")
    if held:
        return {'answer':'Hold for now — readiness changed while I was preparing the answer.\n' + '\n'.join(held),
                'sources':[], 'ai_used':False}
    return None


async def _explain_picks(master, turn, request, progress, generate, discussion):
    """Recompute every selected leg, retaining unavailable and unpriced legs."""
    from research_discussion import build_research_discussion
    picks = turn['recent_picks']
    slots = turn.get('slots', {})
    targets = [p for p in picks if p.get('line_kind') == 'research_target']
    priced = [p for p in picks if p.get('line_kind') != 'research_target']
    cards, assessments, lines, sources, checks = [], [], [], [], []
    if targets:
        research = await asyncio.to_thread(build_research_discussion, master, targets, slots)
        if not research.get('cards') and not priced:
            return {'answer':research['fallback_answer'], 'sources':research.get('sources', []), 'ai_used':False}
        cards.extend(research.get('cards', []))
        checks.extend({key:item[key] for key in ('player','stat','team','date')}
                      for item in research.get('assessments', []) if item.get('data_readiness', {}).get('ready'))
        assessments.extend({key:item[key] for key in ('player','stat','status','reason','cautions',
                           'date','line_kind','offer_verified') if key in item}
                           for item in research.get('assessments', []))
        assessments.extend({**item, 'status':'held', 'decision':'pass'} for item in research.get('held', []))
        if not research.get('cards'):
            # A valid priced leg must not hide a failed research leg, including
            # whole-report failures such as a passed or conflicting game date.
            for pick in targets:
                reason = research['fallback_answer']
                cards.append(_unavailable_card(pick, reason, len(cards) + 1))
            if not research.get('held'):
                assessments.extend({'player':p['player'], 'status':'held', 'decision':'pass',
                                    'reason':research['fallback_answer']} for p in targets)
        lines.append(research['fallback_answer'])
        sources.extend(research.get('sources', []))
        discussion['date'] = research.get('date')
    for pick in priced:
        # A card's line and game belong together. A new date never turns an old
        # offer into a verified offer for another game; the resolver normally
        # clears the old card, and this check also protects older saved state.
        saved_date = pick.get('game_date') or pick.get('date')
        requested_date = slots.get('game_date') or slots.get('date')
        if saved_date and requested_date and saved_date != requested_date:
            reason = 'The saved line is for a different game date. Send the line for the requested game.'
            report = {'available':False, 'fallback_answer':reason}
        else:
            pick_slots = dict(pick)
            if requested_date:
                pick_slots['game_date'] = requested_date
            # A shared opponent describes the selected game, not the opponent
            # of every player on a two-team lineup. Only inherit it for a
            # genuinely focused single-player question.
            if (not pick_slots.get('opponent') and slots.get('player') and
                    normalized_name(slots['player']) == normalized_name(pick['player'])):
                pick_slots['opponent'] = slots.get('opponent')
            report = await asyncio.to_thread(build_pick_discussion, master, pick_slots)
        if not report.get('available'):
            reason = report['fallback_answer']
            cards.append(_unavailable_card(pick, reason, len(cards) + 1))
            assessments.append({'player':pick['player'], 'stat':pick.get('stat'),
                                'decision':'pass', 'status':'held', 'reason':reason})
            lines.append(f"{pick['player']}: Hold. {reason}")
            continue
        card = dict(report['evidence_cards'][0])
        card['id'] = 'P'+str(len(cards)+1)
        card['decision'] = report['decision']
        card['approved_facts'] = card['approved_facts'][:2] + [{
            'text':report['reason'], 'entity':report['player'], 'fact_type':'assessment'}]
        card['text'] = ' '.join(f['text'] for f in card['approved_facts'])
        cards.append(card)
        assessments.append({'player':report['player'], 'stat':report['stat'],
                            'decision':report['decision'], 'reason':report['reason']})
        sample = report['sample']
        metric = MARKET_NAMES[report['stat']]
        lines.append(f"{report['player']} — {report['decision'].title()} {report['line']:g} {metric}. "
                     f"Above in {sample['more']}/{sample['n']}, below in {sample['less']}/{sample['n']} "
                     f"stored games through {sample['last_date']}. {report['reason']}")
        sources.extend(report.get('sources', []))
        if report.get('data_readiness', {}).get('ready'):
            checks.append({key:report[key] for key in ('player','stat','team','date')})
    fallback = '\n\n'.join(lines)
    fallback_result = _direct_reply(fallback, cards, sources=list(dict.fromkeys(sources)))
    fallback_result['discussion'] = {**discussion, 'assessments':assessments}
    discussion.update(assessments=assessments, action=(
        'Explain every supplied pick, including held picks. Research targets are hypothetical, '
        'not actual offers. Supplied priced lines remain user-provided and unverified. '
        'Do not omit a player because its evidence is unavailable.'))
    if discussion.get('selected_pick'):
        discussion['action'] += ' The user’s ordinal identifies the selected player in the lineup; explain why that player and market were selected.'
    # The bounded model/validator supports four evidence cards. Larger lineups
    # still get every computed explanation instead of silently dropping legs.
    if len(cards) > 4 or not _wants_model(request.message):
        changed = await asyncio.to_thread(_changed_readiness, master, checks)
        if changed:
            return changed
        return fallback_result
    result = await _generate_reply(generate, request, progress, master, cards,
                                   dialogue=discussion, fallback=fallback)
    changed = await asyncio.to_thread(_changed_readiness, master, checks)
    if changed:
        return changed
    if result is None:
        return fallback_result
    from grounded_reply import _entity_mentions
    entities = {p['player'] for p in picks}
    mentioned = _entity_mentions(result.get('answer', ''), entities)
    if not entities <= mentioned:
        return fallback_result
    held = [item['player'] for item in assessments
            if item.get('status') == 'held' or item.get('decision') == 'pass']
    if held and not re.search(r'\b(?:pass|hold|held|skip)\b', result.get('answer', ''), re.I):
        result['answer'] = 'Hold for now: ' + ', '.join(dict.fromkeys(held)) + '.\n\n' + result['answer']
    return result


async def answer_conversation(store, master, request, progress, generate, turn=None, *, fast_only=False):
    history = [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.history]
    turn = turn or await asyncio.to_thread(resolve_conversation, store, master, request.message, history,
        preferences=getattr(request, 'lineup_preferences', None))
    if turn.get('clarification'):
        return {'answer':turn['clarification'], 'sources':[], 'ai_used':False}
    intent = turn['intent']
    if intent == 'upcoming_game':
        from upcoming_game import build_preview
        from pick_discussion import _now
        return await asyncio.to_thread(build_preview, master, request.message, history, now=_now(None),
                                       team=turn.get('state', {}).get('upcoming_game_team'))
    if intent == 'research':
        return None
    if fast_only and _wants_model(request.message) and intent != 'lineup_constraint':
        return None
    if (intent == 'explanation' and turn.get('slots', {}).get('player')
            and not turn.get('complete_prop') and not turn.get('recent_picks')):
        # A named-player explanation still needs factual context even when it
        # includes a style request or has no betting threshold. Keep the
        # existing scoped retrieval path instead of free-form unsupported chat.
        return None
    discussion = _context(turn)
    if intent == 'lineup_constraint':
        # This is an instruction about selecting a lineup, not a team-stat query.
        # Both recommendation engines enforce the requirement on future drafts.
        both = turn.get('constraints', {}).get('both_teams', True)
        discussion['action'] = ('Acknowledge that a complete lineup must include at least one qualified '
            'player from each team. If one side lacks evidence, explain that rather than fill a leg.'
            if both else 'Acknowledge the requested team preference. The game and offer rules still need checking.')
        answer = ('I’ll include at least one qualified player from each team when building the lineup. '
                      'If I can’t support both sides, I’ll tell you instead of filling a leg.' if both else
                      'I understand the team preference. I’ll still check the available evidence and offer rules.')
        result = _direct_reply(answer)
        result['constraints'] = turn.get('constraints', {})
        return result
    if intent in {'prop','explanation','preference'} and turn.get('complete_prop'):
        report = await asyncio.to_thread(build_pick_discussion, master, turn['slots'])
        if not report.get('available'):
            return {'answer':report['fallback_answer'], 'sources':report.get('sources',[]), 'ai_used':False}
        report['fallback_answer'] = format_pick_discussion(report, request.message)
        discussion.update(decision=report['decision'], assessment=report['reason'],
                          date=report['date'], date_assumed=report['date_assumed'])
        risks = report.get('conditional_risks', [])
        # Show the model the mechanism the user is asking about rather than all
        # possible warnings in every answer. The underlying report stays intact.
        query = request.message.casefold()
        focused = [r for r in risks if any(word in query and word in r.casefold()
                   for word in ('blowout','minutes','payout','threes','attempts','motivation'))]
        discussion['relevant_risks'] = (focused or risks[-1:])[:2]
        progress['detail'] = 'Checking the line and its main risk'
        if _wants_model(request.message):
            result = await _generate_reply(generate, request, progress, master, report['evidence_cards'],
                                           dialogue=discussion, fallback=report['fallback_answer'])
        else:
            result = _direct_reply(report['fallback_answer'], report['evidence_cards'])
        if report.get('data_readiness', {}).get('ready'):
            changed = await asyncio.to_thread(_changed_readiness, master,
                [{key:report[key] for key in ('player','stat','team','date')}])
            if changed:
                changed['discussion'] = {'player':report['player'], 'stat':report['stat'],
                                        'line':report['line'], 'date':report['date'], 'decision':'pass'}
                return changed
        if result is None:
            result = {'answer':report['fallback_answer'], 'sources':report.get('sources',[]), 'ai_used':False}
        if (report['decision'] == 'pass' and
                not re.search(r'\b(?:pass|hold|held|skip)\b', result.get('answer', ''), re.I)):
            result['answer'] = 'Pass for now.\n\n' + result['answer']
        result['discussion'] = {'player':report['player'],'stat':report['stat'],'line':report['line'],
                                'date':report['date'],'decision':report['decision']}
        if discussion.get('selected_pick'):
            result['discussion']['selected_pick'] = discussion['selected_pick']
        return result
    if intent == 'explanation' and turn.get('recent_picks'):
        # Explain the selected identities, recomputing every claim from data.
        # Prior model prose is not recycled as authoritative evidence.
        return await _explain_picks(master, turn, request, progress, generate, discussion)
    if intent == 'preference':
        discussion['action'] = ('Acknowledge the preference and explain the relevant tradeoff. '
            'Do not reverse an assessment merely to agree, and do not ask for a new line unless '
            'the user actually asks you to evaluate a different market.')
    elif intent == 'explanation':
        discussion['action'] = ('Explain the general mechanism using the conversation. If several '
            'picks could be meant, ask which one; do not choose an arbitrary player.')
    if intent == 'preference' and not _wants_model(request.message):
        result = _direct_reply(_conversational_fallback(turn, request.message))
        result['constraints'] = turn.get('constraints', {})
        return result
    if intent == 'chat' and re.fullmatch(r'\s*(?:thanks?(?: you)?|thank u|ty|thx|hi|hello|hey|'
                                      r'ok(?:ay)?|cool|got it|sounds good|nice)[.!?\s]*', request.message, re.I):
        return _direct_reply(_conversational_fallback(turn, request.message))
    cards = (await asyncio.to_thread(_preference_evidence, store, master, turn, history)
             if intent == 'preference' and turn.get('slots', {}).get('player') else [])
    if fast_only:
        return None
    return await _generate_reply(generate, request, progress, master, cards, dialogue=discussion,
                                fallback=_conversational_fallback(turn, request.message))


async def try_fast_answer(store, master, request, progress=None):
    """Return a checked routine answer before model readiness/queue guards.

    This path performs no inference or live schedule fetch. The saved freshness
    checks still decide whether a prop is eligible, so missing verification
    returns a Pass rather than an invented current recommendation.
    """
    return await answer_conversation(store, master, request, progress if progress is not None else {},
                                     None, fast_only=True)
