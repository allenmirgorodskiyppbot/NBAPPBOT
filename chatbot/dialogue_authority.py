"""Bounded authority checks for open conversation, independent of intent wording.

These checks are defense in depth around source grounding, not semantic proof of
arbitrary prose. Reader results own current decisions; a planner label or an
unexecuted read request cannot authorize a recommendation.
"""
from __future__ import annotations

import math
import re

from grounded_reply import _entity_mentions

_DIRECTION = r'(?P<side>more|less|over|under|up|down)'
_RECOMMEND = re.compile(
    r'\b(?:I|we)(?:[’\x27]d|\s+would)?\s+(?:like|love|choose|prefer|recommend|take|pick|play|back|favor|favour|go\s+with|lean(?:\s+towards?)?)\s+(?:the\s+)?' + _DIRECTION + r'\b|'
    r'\b(?:my|the)\s+(?:pick|lean|play|choice|recommendation|assessment)\s+(?:is|would\s+be)\s+(?:the\s+)?(?P<label>more|less|over|under|up|down)\b|'
    r'\b(?:lean(?:s|ing)?|recommend(?:s|ing)?|prefer(?:s|ring)?|favor(?:s|ing)?|favour(?:s|ing)?)\s+(?:(?:slightly|cautiously|tentatively)\s+)?(?:(?:towards?|to|on)\s+)?(?:the\s+)?(?P<lean>more|less|over|under|up|down)\b|'
    r'^\s*(?:take|choose|pick|play|bet|back)\s+(?:the\s+)?(?P<imperative>more|less|over|under|up|down)\b|'
    r'\b(?:the\s+)?(?P<assertion>more|less|over|under|up|down)\s+(?:is|looks|seems)\s+(?:(?:like\s+)?(?:my|the|a)\s+)?(?:pick|play|bet|choice|call|better|best|solid|good)\b|'
    r'^\s*(?:[^:;.!?]{1,65}:\s*)?(?:lean\s*[:—-]?\s*)?(?P<heading>more|less|over|under|up|down)\s*(?:[.,:;—-]|on\b|$)', re.I)

_BASKETBALL = re.compile(r'\b(?:nba|basketball|prizepicks|goblin|demon|player\s+prop|parlay|bet(?:ting)?|wager|rebounds?|assists?|PRA|FG\s+attempts?|three[- ]pointers?)\b', re.I)
_MARKET_TAIL = re.compile(r'^\s*(?:[.,:;—–!?]|$|\d|(?:for\s+(?:him|her|them)|here|tonight|today|on\b|in\s+this\s+game|because|since|as\s+(?:my|the)\s+(?:pick|play))\b|(?:points?|rebounds?|assists?|PRA|PTS|REB|AST|FGA|3PTM|threes?|fantasy|turnovers?|minutes|field\s+goals?|offensive\s+rebounds?)\b)', re.I)
_NONBETTING_OBJECT = re.compile(r'^\s+(?:on\s+(?:(?:observed|saved|relevant|current)\s+)?(?:data|evidence|facts)\b|sleep|rest|water|exercise|time|detail|context|evidence|data|information|clarity|practice|food|support|space|memory|CPU|RAM|storage|caution|patience|research|attention|testing|tests?|accuracy|consistency|often|carefully|closely)\b', re.I)

# Predicates are bounded claim checks, not a phrase-to-answer intent router.
# A conditional mechanism or attributed user report remains discussable.
_UNCERTAIN = re.compile(r'\b(?:if|assuming|provided|whether|could|may|might|would|unknown|unverified|unclear|unconfirmed|cannot\s+confirm|can[’\x27]t\s+confirm|do\s+not\s+know|don[’\x27]t\s+know|not|never)\b', re.I)
_ATTRIBUTED = re.compile(r'\b(?:you\s+(?:said|reported|mentioned|told\s+me)|according\s+to\s+(?:you|your\s+(?:message|report))|your\s+(?:message|report)\s+(?:says|said))\b', re.I)
_STATUS = re.compile(r'\b(?:is|are|was|were|will\s+be|has\s+been|have\s+been|[’\x27]s|[’\x27]re)\s+(?:(?:confirmed|officially|currently|still|now|reportedly|listed\s+as|expected\s+to\s+be)\s+){0,3}(?:active|healthy|available|unavailable|out|ruled\s+out|cleared|starting|injured|limited|questionable|doubtful|probable|playing\s+tonight|expected\s+to\s+(?:play|start)|a\s+game[- ]time\s+decision|on\s+(?:a\s+)?minutes?\s+(?:restriction|limit))\b', re.I)
_FUTURE_STATUS = re.compile(r'\b(?:will|is\s+(?:expected|confirmed)\s+to|has\s+been\s+cleared\s+to)\s+(?:play|start|sit|miss\s+the\s+game)\b', re.I)
_ROLE = re.compile(r'\b(?:has|have|gets?|plays?|is\s+getting|is\s+playing|will\s+have|will\s+get)\s+(?:(?:a|an|the|much|far|significantly)\s+){0,3}(?:larger|bigger|smaller|increased|reduced|expanded|limited|starting|lead|primary)\s+(?:role|minutes?|workload|usage|shot\s+share|responsibilit\w*)\b', re.I)
_HABIT = re.compile(r'\b(?:usually|normally|regularly|consistently|typically|always|often)\s+(?:dominates?|scores?|gets?|hits?|averages?|outperforms?|rebounds?|shoots?|plays?|assists?|clears?|exceeds?|beats?|finishes?|makes?)\b', re.I)
_HISTORY = re.compile(r'\b(?:he|she|they)\s+(?:has\s+|have\s+)?(?:averag(?:es|ed)|scored|recorded|made|shot|grabbed|posted|finished\s+with|dominat(?:es|ed))\b', re.I)
_TRANSACTION = re.compile(r'\b(?:traded|waived|signed|acquired|released|drafted|suspended|fired|hired)\s+(?:him|her|them|[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})\b|\b(?:he|she|they)\s+(?:(?:has|have)\s+been|was|were|is|are)\s+(?:traded|waived|signed|acquired|released|drafted|suspended)\b')
_BASELINE_RELATION = re.compile(r'\b(?:baseline|average|mean|median|stored\s+sample|saved\s+sample)\b\s+(?:(?:is|was|has\s+been|being|sits?|lies?|looks?|appears?|currently|still|already|well|comfortably|slightly)\s+){1,4}(?:above|below|over|under|higher\s+than|lower\s+than)\s+(?:(?:the|this|that|offered|current)\s+){0,2}(?:line|threshold)\b', re.I)
_SCHEDULE = re.compile(r'\b(?:next|upcoming)\s+(?:game|matchup|opponent)\s+(?:is|will\s+be)|\b(?:tipoff|tip-off|the\s+game)\s+(?:is|starts?|will\s+(?:be|start))\s+(?:at|on|in)\b|\b(?:plays?|faces?|hosts?|visits?|will\s+face|are\s+playing)\s+[^.;!?]{0,65}\b(?:tonight|today|tomorrow|this\s+(?:evening|weekend))\b', re.I)

_ACTION_SUCCESS = re.compile(
    r'\b(?:your|the|this|that|an?|my)\s+(?:entry|bet|wager|lineup)\s+(?:(?:has|have)\s+(?:now\s+)?been|was|were|is(?:\s+now)?|got)\s+(?:successfully\s+)?(?:placed|submitted|recorded|saved|confirmed|entered)\b|'
    r'\bI(?:[’\x27]ve|\s+have)?\s+(?:placed|submitted|saved|recorded|confirmed)\s+(?:(?:your|the|an?|this)\s+)?(?:entry|bet|wager|lineup)\b|'
    r'\b(?:database|master(?:\s+(?:file|dataset|workbook))?|dataset|workbook|Excel\s+file)\s+(?:(?:has|have)\s+(?:now\s+)?been|was|were|is\s+now)\s+(?:successfully\s+)?(?:updated|edited|changed|refreshed|rebuilt|replaced|saved|uploaded|deleted)\b|'
    r'\bI(?:[’\x27]ve|\s+have)?\s+(?:updated|edited|changed|refreshed|rebuilt|replaced|deleted)\s+(?:(?:your|the|this)\s+)?(?:database|master(?:\s+(?:file|dataset|workbook))?|dataset|workbook|Excel\s+file)\b|'
    r'\b(?:the\s+|your\s+)?(?:(?:dataset|daily|data)\s+)?refresh\s+(?:has\s+)?(?:finished|completed|succeeded)\b|'
    r'\brefresh\s+is\s+(?:done|complete)\b', re.I)


def _check_read_only_actions(text):
    for clause in _clauses(text):
        for match in _ACTION_SUCCESS.finditer(clause):
            if _qualified(clause, match.start()):
                continue
            if re.search(r'\b(?:when|once|after|before)\b', clause[:match.start()], re.I):
                continue
            raise ValueError('unsupported_action_confirmation')


def _focus(focus):
    if not isinstance(focus, dict):
        return {}
    return focus.get('slots') if isinstance(focus.get('slots'), dict) else focus


def _entities(cards, reads, focus):
    result = {str(card['entity']) for card in cards if isinstance(card, dict) and card.get('entity')}
    result.update(str(read.get('args', {}).get('player')) for read in reads
                  if isinstance(read, dict) and isinstance(read.get('args'), dict) and read['args'].get('player'))
    result.update(str(focus[key]) for key in ('player', 'team', 'opponent') if focus.get(key))
    return result


def _authorities(reads):
    values = []
    for read in reads:
        if not isinstance(read, dict) or read.get('tool') != 'prop_assessment' or read.get('available') is not True:
            continue
        args = read.get('args')
        if not isinstance(args, dict) or read.get('decision') not in {'more', 'less', 'pass'}:
            continue
        if not isinstance(args.get('player'), str) or not isinstance(args.get('stat'), str):
            continue
        line = args.get('line')
        if type(line) not in (int, float) or not math.isfinite(line):
            continue
        values.append((args['player'], args['stat'], line, read['decision']))
    return list(dict.fromkeys(values))


def _clauses(text):
    # Preserve the direction's subject but keep a later caveat from licensing an
    # earlier assertion. Splitting a hypothetical's comma is deliberately avoided.
    return re.split(r'(?<=[.!?;])\s+|\n|\b(?:but|however|whereas)\b', text)


def _qualified(clause, start):
    prefix = clause[:start]
    return bool(_UNCERTAIN.search(prefix) or _ATTRIBUTED.search(prefix)
                or re.search(r'\bno\s+(?:(?:clear|strong|good)\s+)?(?:basis|reason|support|evidence|justification)\b', prefix, re.I))


def _side_claims(text, *, basketball, entities):
    active = set()
    aliases = {alias for entity in entities for alias in (entity, entity.split()[-1]) if alias}
    bare = (re.compile(r'^\s*(?:' + '|'.join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
                       + r')(?:\s*[:—-]\s*|\s+)(?P<bare>more|less|over|under|up|down)\s+(?=\d)', re.I)
            if aliases else None)
    for clause in _clauses(text):
        mentioned = _entity_mentions(clause, entities)
        if mentioned:
            active = mentioned
        matches = list(_RECOMMEND.finditer(clause))
        if bare and (matched := bare.search(clause)):
            matches.append(matched)
        for match in matches:
            group = next(key for key, value in match.groupdict().items() if value is not None)
            side = match.group(group).casefold()
            suffix = clause[match.end(group):]
            prefix = clause[:match.start()]
            # "More sleep" and "lean more on data" are ordinary advice even
            # while a player happens to be focused in the preceding conversation.
            if _NONBETTING_OBJECT.match(suffix):
                continue
            tail_mentions = _entity_mentions(suffix, entities)
            if not (_MARKET_TAIL.match(suffix) or tail_mentions or group == 'assertion'):
                continue
            explicit_bet = bool(re.search(r'\b(?:bet|wager|parlay|pick|play|line|prop)\b', clause, re.I)
                                or re.fullmatch(r'\s*[.!]?\s*', suffix))
            if not basketball and not explicit_bet:
                continue
            if _qualified(clause, match.start()):
                continue
            if re.search(r'\bonly\s+if\b', clause[match.end():], re.I):
                continue
            # A side attributed to somebody else is a report, not our decision.
            if re.search(r'\b(?:you\s+(?:said|picked|chose)|your\s+(?:pick|choice)|previously|earlier)\b', prefix, re.I):
                continue
            target = re.split(r'\b(?:because|since|with|whereas)\b', clause[match.end():], maxsplit=1, flags=re.I)[0]
            targets = _entity_mentions(target, entities) or mentioned or active
            yield ('more' if side in {'more', 'over', 'up'} else 'less'), clause, targets


def _check_recommendations(text, cards, reads, focus, *, allow_decisions):
    entities = _entities(cards, reads, focus)
    basketball = bool(entities or _BASKETBALL.search(text) or any(isinstance(r, dict) and r.get('tool') for r in reads))
    authorities = _authorities(reads) if allow_decisions else []
    for side, clause, targets in _side_claims(text, basketball=basketball, entities=entities):
        if not authorities:
            raise ValueError('unsupported_recommendation')
        if not targets:
            if focus.get('player'):
                targets = {focus['player']}
            elif len({value[0] for value in authorities}) == 1:
                targets = {authorities[0][0]}
        if len(targets) != 1:
            raise ValueError('ambiguous_recommendation')
        player = next(iter(targets))
        candidates = [value for value in authorities if value[0] == player]
        # Different markets or thresholds never lend one another authority.
        from conversation import _market
        stat, error = _market(clause)
        if error:
            raise ValueError('ambiguous_recommendation')
        if stat:
            candidates = [value for value in candidates if value[1] == stat]
        numbers = {float(value) for value in re.findall(
            r'\b(?:more|less|over|under|up|down)\s+(\d+(?:\.\d+)?)', clause, re.I)}
        if numbers:
            candidates = [value for value in candidates if value[2] in numbers]
        if len(candidates) != 1 or candidates[0][3] != side:
            raise ValueError('unsupported_recommendation')


def _known_status(clause, match, reads, focus, entities):
    """Only a matching explicit fresh official designation authorizes status.

    Availability does not imply health, starting, no minutes limit, or that the
    player will actually enter the game. A conflicting later report preserves
    the dated official attribution but does not authorize a current clearance.
    """
    status_match = re.search(r'\b(available|out|ruled\s+out|questionable|doubtful|probable)\b', match.group(), re.I)
    if not status_match:
        return False
    status = status_match.group(1).casefold()
    status = 'out' if status.startswith('ruled') else status
    targets = _entity_mentions(clause, entities)
    prefix_name = re.search(r'(?:^|\s)([A-Z][A-Za-z’\x27-]+(?:\s+[A-Z][A-Za-z’\x27-]+){0,3})\s*$', clause[:match.start()])
    if prefix_name and prefix_name.group(1).casefold() not in {'he', 'she', 'they'}:
        named_targets = _entity_mentions(prefix_name.group(1), entities)
        if not named_targets:
            return False  # An explicitly different player cannot borrow focus.
        targets = named_targets
    if not targets and focus.get('player'):
        targets = {focus['player']}
    dated_attribution = bool(re.search(r'\b(?:official|NBA)\b', clause, re.I)
                             and re.search(r'\breport\b', clause, re.I)
                             and re.search(r'\b(?:as\s+of|reported|dated|published)\b', clause, re.I))
    for read in reads:
        if read.get('tool') != 'availability' or read.get('available') is not True:
            continue
        args, data = read.get('args') or {}, read.get('data') or {}
        if not isinstance(args, dict) or not isinstance(data, dict) or args.get('player') not in targets:
            continue
        official = data.get('official_designation') or {}
        if not isinstance(official, dict) or official.get('known') is not True:
            continue
        if not official.get('report_at') or not official.get('source_url'):
            continue
        if official.get('game_date') and args.get('date') != official['game_date']:
            continue
        if str(official.get('status', '')).casefold() != status:
            continue
        if data.get('official_superseded_or_limited') and not dated_attribution:
            continue
        return True
    return False


def _known_role(clause, targets, cards):
    # No current reader manufactures role/workload confirmation. This narrow
    # extension point only accepts a verbatim fact explicitly marked current.
    normalized = re.sub(r'\s+', ' ', clause).strip().casefold().rstrip('.!?')
    for card in cards:
        if card.get('entity') not in targets or card.get('verified_current') is not True:
            continue
        for fact in card.get('approved_facts') or []:
            if not isinstance(fact, dict) or fact.get('fact_type') != 'current_conditions':
                continue
            if fact.get('verified_current') is not True:
                continue
            evidence = re.sub(r'\s+', ' ', str(fact.get('text', ''))).strip().casefold().rstrip('.!?')
            if normalized and normalized == evidence:
                return True
    return False


def _check_unsourced_empirical(text, focus, *, cards, reads):
    has_cards = any(isinstance(card, dict) and card.get('text') for card in cards)
    entities = _entities(cards, reads, focus)
    basketball = bool(entities or _BASKETBALL.search(text))
    aliases = {alias for entity in entities for alias in (entity, entity.split()[-1]) if alias}
    named_history = (re.compile(r'\b(?:' + '|'.join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
                    + r')\s+(?:(?:has|have|is|are)\s+)?(?:averag(?:es|ed|ing)|scored|scoring|recorded|recording|made|shot|grabbed|posted|finished\s+with|dominat(?:es|ed|ing)|won|lost|defeated)\b', re.I)
                     if aliases else None)
    current_patterns = (_STATUS, _FUTURE_STATUS, _ROLE)
    for clause in _clauses(text):
        mentioned = _entity_mentions(clause, entities)
        person = bool(re.search(r'\b(?:he|she|they|him|her|them|player|team|knicks|cavaliers)\b', clause, re.I))
        for pattern in (*current_patterns, _HABIT, _HISTORY, _TRANSACTION, _SCHEDULE, _BASELINE_RELATION, named_history):
            if pattern is None:
                continue
            # Statistics cards may support historical descriptions, but never
            # silently confer current health or workload authority.
            if has_cards and pattern not in current_patterns:
                continue
            for match in pattern.finditer(clause):
                prefix = clause[:match.start()]
                named_person = bool(re.search(r'(?:^|\s)[A-Z][A-Za-z’\x27-]+(?:\s+[A-Z][A-Za-z’\x27-]+){0,3}\s*$', prefix))
                software = bool(re.search(r'\b(?:service|server|model|bot|chatbot|database|dataset|application|app|tool|endpoint|API|Ollama|Docker|Python|FastAPI|Telegram)\b[^.;!?]{0,25}$', prefix, re.I))
                if not (basketball or mentioned or person or named_person or pattern is _BASELINE_RELATION):
                    continue
                if pattern in current_patterns and (software or not (mentioned or person or named_person)):
                    continue
                if _qualified(clause, match.start()):
                    continue
                if re.search(r'\b(?:hypothetical|example|definition|generally|in\s+general)\b', prefix, re.I):
                    continue
                # A question about a status does not assert that status. A later
                # question never excuses an earlier factual sentence.
                if clause.rstrip().endswith('?') and re.match(r'\s*(?:is|are|was|were|will|could|would|does|do|what|which|how|whether)\b', clause, re.I):
                    continue
                if pattern is _STATUS and _known_status(clause, match, reads, focus, entities):
                    continue
                if pattern is _ROLE and _known_role(clause, mentioned or {focus.get('player')}, cards):
                    continue
                if pattern is _HISTORY and not person and not mentioned:
                    continue
                raise ValueError('unsupported_empirical_claim')


def validate_authority(text, *, cards=(), reads=(), focus=None, allow_decisions=False):
    """Reject observed authority errors; return None when these checks pass.

    ``reads`` are executed reader envelopes, never planner task requests. Current
    More/Less authority requires one exact, successful prop_assessment result.
    This complements numerical/source validation; it does not certify every
    possible qualitative assertion.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('empty_reply')
    clean = re.sub(r'[*_`]+', '', text)
    focus = _focus(focus)
    cards = tuple(card for card in cards if isinstance(card, dict))
    reads = tuple(read for read in reads if isinstance(read, dict))
    _check_read_only_actions(clean)
    _check_recommendations(clean, cards, reads, focus, allow_decisions=allow_decisions)
    _check_unsourced_empirical(clean, focus, cards=cards, reads=reads)
