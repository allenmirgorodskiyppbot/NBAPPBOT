"""Connect dialogue context, read-only prop analysis and natural explanation.

The language model explains computed evidence; history supplies conversational
references, never invented statistics or permission to create financial records.
"""
from __future__ import annotations

import asyncio
import re

from conversation import resolve_conversation
from pick_discussion import build_pick_discussion


def _context(turn):
    return {key:turn[key] for key in ('intent','slots','constraints') if turn.get(key)}


async def answer_conversation(store, master, request, progress, generate, turn=None):
    history = [m.model_dump() if hasattr(m, 'model_dump') else m for m in request.history]
    turn = turn or await asyncio.to_thread(resolve_conversation, store, master, request.message, history)
    if turn.get('clarification'):
        return {'answer':turn['clarification'], 'sources':[], 'ai_used':False}
    intent = turn['intent']
    if intent == 'research':
        return None
    discussion = _context(turn)
    if intent == 'lineup_constraint':
        # This is an instruction about selecting a lineup, not a team-stat query.
        # Both recommendation engines enforce the requirement on future drafts.
        both = turn.get('constraints', {}).get('both_teams', True)
        discussion['action'] = ('Acknowledge that a complete lineup must include at least one qualified '
            'player from each team. If one side lacks evidence, explain that rather than fill a leg.'
            if both else 'Acknowledge the requested team preference. The game and offer rules still need checking.')
        return await generate(request, progress, master, [], natural=True, dialogue=discussion,
            fallback=('I’ll include at least one qualified player from each team when building the lineup. '
                      'If I can’t support both sides, I’ll tell you instead of filling a leg.' if both else
                      'I understand the team preference. I’ll still check the available evidence and offer rules.'))
    if intent in {'prop','explanation'} and turn.get('complete_prop'):
        report = await asyncio.to_thread(build_pick_discussion, master, turn['slots'])
        if not report.get('available'):
            return {'answer':report['fallback_answer'], 'sources':report.get('sources',[]), 'ai_used':False}
        discussion.update(decision=report['decision'], assessment=report['reason'],
                          date=report['date'], date_assumed=report['date_assumed'])
        risks = report.get('conditional_risks', [])
        # Show the model the mechanism the user is asking about rather than all
        # possible warnings in every answer. The underlying report stays intact.
        query = request.message.casefold()
        focused = [r for r in risks if any(word in query and word in r.casefold()
                   for word in ('blowout','minutes','payout','threes','attempts','motivation'))]
        discussion['relevant_risks'] = (focused or risks[-1:])[:2]
        progress['detail'] = 'Explaining the line and its main risk'
        result = await generate(request, progress, master, report['evidence_cards'], natural=True,
                                dialogue=discussion, fallback=report['fallback_answer'])
        if result is None:
            result = {'answer':report['fallback_answer'], 'sources':report.get('sources',[]), 'ai_used':False}
        result['discussion'] = {'player':report['player'],'stat':report['stat'],'line':report['line'],
                                'date':report['date'],'decision':report['decision']}
        return result
    if intent == 'explanation' and turn.get('recent_picks'):
        # Explain the selected identities, recomputing every claim from data.
        # Prior model prose is not recycled as authoritative evidence.
        targets = [p for p in turn['recent_picks'] if p.get('line_kind') == 'research_target']
        if targets:
            from research_discussion import build_research_discussion
            research = await asyncio.to_thread(build_research_discussion, master, targets, turn.get('slots', {}))
            if not research.get('cards'):
                return {'answer':research['fallback_answer'], 'sources':research.get('sources', []), 'ai_used':False}
            discussion.update(assessments=[{key:item[key] for key in
                ('player','stat','status','reason','cautions') if key in item}
                for item in research['assessments']], date=research.get('date'),
                              action='Explain these hypothetical research targets. No actual sportsbook line has been supplied.')
            return await generate(request, progress, master, research['cards'], natural=True,
                                  dialogue=discussion, fallback=research['fallback_answer'])
        cards = []
        summaries = []
        for pick in turn['recent_picks'][:4]:
            if pick.get('line_kind') == 'research_target':
                continue
            slots = {**turn.get('slots', {}), **pick}
            report = await asyncio.to_thread(build_pick_discussion, master, slots)
            if not report.get('available'):
                continue
            card = dict(report['evidence_cards'][0])
            card['id'] = 'P'+str(len(cards)+1)
            card['decision'] = report['decision']
            card['approved_facts'] = card['approved_facts'][:2] + [{
                'text':report['reason'], 'entity':report['player'], 'fact_type':'assessment'}]
            card['text'] = ' '.join(f['text'] for f in card['approved_facts'])
            cards.append(card)
            summaries.append({'player':report['player'],'decision':report['decision'],
                              'reason':report['reason']})
        if cards:
            discussion['assessments'] = summaries
            return await generate(request, progress, master, cards, natural=True, dialogue=discussion)
    if intent == 'preference':
        discussion['action'] = ('Acknowledge the preference and explain the relevant tradeoff. '
            'Do not reverse an assessment merely to agree, and do not ask for a new line unless '
            'the user actually asks you to evaluate a different market.')
    elif intent == 'explanation':
        discussion['action'] = ('Explain the general mechanism using the conversation. If several '
            'picks could be meant, ask which one; do not choose an arbitrary player.')
    return await generate(request, progress, master, [], natural=True, dialogue=discussion)
