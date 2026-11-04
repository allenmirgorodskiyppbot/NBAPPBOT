"""Small, read-only dialogue state for conversational basketball research.

This module resolves what a user is referring to. It does not choose a wager,
create an entry, or treat a prior assistant answer as statistical evidence.
Only explicit user values (or clearly labelled proposal cards) become prop
slots. Missing markets, ambiguous names, and mixed lines stay unresolved.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

from basketball_research import _players, _scan, _words, TEAM_NAMES
from master import STAT_COMPONENTS

EASTERN = ZoneInfo('America/New_York')
HISTORY_TURNS = 24
FOCUS_HOURS = 12
MARKET_LABELS = {
    'pts': 'points', 'reb': 'rebounds', 'ast': 'assists', 'tpm': 'made threes',
    'fga': 'field-goal attempts', 'tpa': 'three-point attempts', 'pra': 'PRA',
    'pr': 'points + rebounds', 'pa': 'points + assists', 'ra': 'rebounds + assists',
    'stl': 'steals', 'blk': 'blocks', 'tov': 'turnovers', 'oreb': 'offensive rebounds',
    'dreb': 'defensive rebounds', 'stocks': 'steals + blocks', 'minutes': 'minutes',
}
_NUMBER = r'(?:\d+(?:\.\d+)?|\.\d+)'
_ASK = re.compile(r'\b(?:choose|picks?|recommend\w*|lean|good|bad|like|take|bet|play|'
                  r'worth|over|under|more|less|higher|lower|up|down|line|prop|'
                  r'safer|riskier|replace|swap)\b', re.I)
_EXPLAIN = re.compile(r'\b(?:why|explain|reason\w*|clarif\w*|meaning|mean by|tell me more|elaborate|more detail|'
                      r'how come|what makes|what if|risk\w*|blowout|benched|'
                      r'bench|minutes limit|revenge|old team)\b', re.I)
_FOLLOWUP = re.compile(r'^\s*(?:and\b|but\b|why\b|how\b|what about\b|'
                       r'actually\b|no\b|nah\b|instead\b|i mean\b|'
                       r'up or down\b|more or less\b|over or under\b)|'
                       r'\b(?:he|him|his|that|those|it|they|their|them|same|instead)\b', re.I)
_PREFERENCE = re.compile(r'\b(?:i (?:like|liked|prefer|want|wanted)|i(?:[’\']m| am) '
                         r'(?:not )?a fan|rather|prefer|keep|leave|don.t change)\b', re.I)
_RESEARCH = re.compile(r'\b(?:average\w*|stats?|statistics|last \w+ games|'
                       r'game logs?|who guards?|defensive percentage|injur\w*|'
                       r'news|schedule|score|scores|results?|records?|workbooks?|'
                       r'worksheets?|sheets?|rows?|cells?|columns?|tracking|passing|'
                       r'potential assists?|stored data|source data|dataset|database|'
                       r'look up|lookup|retrieve|query)\b', re.I)
_SOCIAL = re.compile(r'^\s*(?:thanks?(?: you)?|thank you|thank u|ty|thx|'
                     r'hello|hi|hey|good (?:morning|night)|okay|ok|cool|great|'
                     r'got it|sounds good|nice|you.re welcome)[.!?\s]*$', re.I)
_MONTHS = {name.casefold(): index for index, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'), 1)}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})


def _now(value=None):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            value = None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time(12), EASTERN)
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _turn(turn):
    if hasattr(turn, 'model_dump'):
        turn = turn.model_dump()
    if not isinstance(turn, dict) or turn.get('role') not in {'user', 'assistant'}:
        return None
    return {'role': turn['role'], 'content': str(turn.get('content') or '')[:5000]}


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1000 else None
    except (ValueError, TypeError):
        return None


def _opponent(text):
    """A team mention is not an opponent assignment without a relation."""
    aliases = {alias: team for team, names in TEAM_NAMES.items() for alias in (team, *names)}
    pattern = '|'.join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    relation = r'\b(?:against|versus|vs\.?|opponent(?:\s+is|\s*:))\s+(?:the\s+)?'
    found = set()
    for match in re.finditer(relation + '(' + pattern + r')(?!\w)', text, re.I):
        found.update(team for alias, team in aliases.items() if alias.casefold() == match.group(1).casefold())
    return next(iter(found)) if len(found) == 1 else None


def _ambiguous_names(text, names, resolved):
    """An ambiguous first/surname must not silently inherit the old player."""
    aliases = {}
    for name in names:
        words = _words(name)
        for token in {words[0], words[-1]}:
            if len(token) > 2:
                aliases.setdefault(token, set()).add(name)
    occupied = {word for name in resolved for word in _words(name)}
    candidates = set()
    for token in _words(text):
        choices = aliases.get(token, set())
        if token not in occupied and len(choices) > 1:
            candidates.update(choices)
    return sorted(candidates)[:4]


def _date(text, now):
    """Return (explicit date, error, spans); never interpret date parts as lines."""
    found, spans = [], []
    today = now.astimezone(EASTERN).date()
    pattern = r'\b20\d{2}-\d{2}-\d{2}\b|\b(?:today|tonight|tomorrow|yesterday)\b'
    for match in re.finditer(pattern, text, re.I):
        token = match.group().casefold()
        spans.append(match.span())
        if token in {'today', 'tonight', 'tomorrow', 'yesterday'}:
            found.append((today + dt.timedelta(days={'tomorrow': 1, 'yesterday': -1}.get(token, 0))).isoformat())
        else:
            try:
                found.append(dt.date.fromisoformat(token).isoformat())
            except ValueError:
                return None, 'invalid_date', spans
    month_names = '|'.join(sorted(_MONTHS, key=len, reverse=True))
    for match in re.finditer(r'\b(' + month_names + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?'
                             r'(?:,?\s+(20\d{2}))?\b|(?<![\d/])(\d{1,2})/(\d{1,2})'
                             r'(?:/(20\d{2}))?(?![\d/])', text, re.I):
        spans.append(match.span())
        month = _MONTHS[match.group(1).casefold()] if match.group(1) else int(match.group(4))
        day = int(match.group(2) or match.group(5))
        year_text = match.group(3) or match.group(6)
        season_start = today.year if today.month >= 7 else today.year - 1
        year = int(year_text) if year_text else season_start + (month < 7)
        try:
            found.append(dt.date(year, month, day).isoformat())
        except ValueError:
            return None, 'invalid_date', spans
    unique = set(found)
    return (next(iter(unique)) if len(unique) == 1 else None,
            'conflicting_dates' if len(unique) > 1 else None, spans)


def _market(text):
    """Resolve explicitly named full-game markets, preserving ambiguity."""
    lower = text.casefold().replace('–', '-').replace('—', '-')
    if re.search(r'\b(?:first|second|third|fourth|1st|2nd|3rd|4th)\s*(?:half|quarter)|'
                 r'\b(?:q[1-4]|[1-4]q|1h|2h|quarters? with|qtrs? w|fantasy|dunks)\b', lower):
        return None, 'unsupported_market'
    if re.search(r'\b(?:2[- ]?(?:pt|point)|two[- ]point)\s*(?:attempts?|att|made)\b', lower):
        return None, 'unsupported_market'
    patterns = [
        ('tpa', r'\b(?:3pta|tpa|(?:3[- ]?(?:pt|point)|three[- ]point|three[- ]pointer)s?\s*(?:attempts?|att)|three attempts)\b'),
        ('fga', r'\b(?:fga|fg\s*(?:attempts?|attempted)|field[- ]goals?\s*(?:attempts?|attempted)|shot attempts?)\b'),
        ('tpm', r'\b(?:3ptm|3pm|tpm|made (?:threes|three[- ]pointers)|threes|three[- ]pointers?(?: made)?|3[- ]pointers? made|3[- ]pt made)\b'),
        ('oreb', r'\b(?:oreb|offensive rebounds?)\b'),
        ('dreb', r'\b(?:dreb|defensive rebounds?)\b'),
        ('pra', r'\b(?:pra|p\s*\+\s*r\s*\+\s*a)\b'),
        ('pr', r'\bpr\b'), ('pa', r'\bpa\b'), ('ra', r'\bra\b'),
        ('stocks', r'\bstocks\b'),
    ]
    for stat, pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            # More-specific three-point attempts take precedence over threes.
            return stat, None
    components = []
    for stat, pattern in [('pts', r'\b(?:points?|pts|ppg)\b'), ('reb', r'\b(?:rebounds?|rebs?|rpg)\b'),
                           ('ast', r'\b(?:assists?|asts?|apg)\b'), ('stl', r'\b(?:steals?|stl)\b'),
                           ('blk', r'\b(?:blocks?|blk)\b'), ('tov', r'\b(?:turnovers?|tov|to)\b'),
                           ('minutes', r'\bminutes\b')]:
        # "to" is ordinary prose, not a market without explicit typography.
        if stat == 'tov':
            pattern = r'\b(?:turnovers?|tov)\b'
        if re.search(pattern, lower):
            components.append(stat)
    if len(components) == 1:
        return components[0], None
    if components:
        if re.search(r'\b(?:or|versus|vs|instead|rather|than)\b', lower):
            return None, 'ambiguous_market'
        combinations = {frozenset(value): key for key, value in STAT_COMPONENTS.items() if len(value) > 1}
        combined = combinations.get(frozenset(components))
        return (combined, None) if combined else (None, 'ambiguous_market')
    if re.search(r'\battempts?\b', lower):
        return None, 'ambiguous_attempts'
    return None, None


def _line(text, date_spans, eligible):
    if not eligible:
        return None, None
    masked = list(text)
    for start, end in date_spans:
        masked[start:end] = ' ' * (end - start)
    text = ''.join(masked)
    text = re.sub(r'\b[23]-(?:points?|pointers?)\b', ' market ', text, flags=re.I)
    text = re.sub(r'\b(?:last|latest|past|recent)\s+\d+\s+games?\b|\b\d+\s*[- ]?legs?\b|'
                  r'\b\d+\s*(?:games|picks|teams)\b|\$\s*\d+(?:\.\d+)?|'
                  r'\b\d+(?:\.\d+)?\s*(?:x|percent|%)|\b20\d{2}[-–]\d{2}\b', ' ', text, flags=re.I)
    numbers = re.findall(r'(?<![\w.])(' + _NUMBER + r')(?![\w.%])', text)
    values = list(dict.fromkeys(value for value in (_finite(x) for x in numbers) if value is not None))
    if len(values) > 1:
        return None, 'multiple_lines'
    return (values[0], None) if values else (None, None)


def _side(text):
    if re.search(r'\b(?:tell me more|explain more|more details?|more information)\b', text, re.I):
        return None, False
    if re.search(r'\b(?:more\s*(?:or|/)\s*less|over\s*(?:or|/)\s*under|up\s*(?:or|/)\s*down)\b', text, re.I):
        return None, True
    values = set()
    for value, pattern in [('more', r'\b(?:more|over|higher)\b|↑'), ('less', r'\b(?:less|under|lower)\b|↓')]:
        if re.search(pattern, text, re.I):
            values.add(value)
    return (next(iter(values)), False) if len(values) == 1 else (None, len(values) > 1)


def _constraint(text):
    both = bool(re.search(r'\b(?:both teams|each team|either team|opposing teams|'
                           r'at least one (?:player|pick)?\s*(?:from|on) (?:each|both))\b', text, re.I))
    prohibition = bool(re.search(r"\b(?:can(?:not|.t)|should(?: not|n.t)|must(?: not|n.t)|"
                                  r"don.t|not|no)\s+(?:be\s+|have\s+)?all\s+(?:the\s+)?", text, re.I))
    lineup = bool(re.search(r'\b(?:picks?|legs?|lineups?|cards?|slips?)\b', text, re.I))
    composition = bool(re.search(r'\b(?:from|on)\s+(?:both|each|either)\s+teams?\b|'
                                 r'\b(?:include|need|require|use|want)\s+(?:both|each)\s+teams?\b', text, re.I))
    if both and (lineup or composition):
        negative = bool(re.search(r"\b(?:don.t|do not|no longer|needn.t|not necessary to)\s+"
                                  r"(?:need|require|include|use|want)?\s*(?:players? from )?both teams", text, re.I))
        return not negative
    if prohibition and (lineup or re.search(r'\b(?:Knicks|NYK|Nets|BKN|teams?)\b', text, re.I)):
        return True
    return None


def _count(text):
    words = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
    match = re.search(r'\b([2-6]|two|three|four|five|six)\s*[- ]?\s*(?:legs?|picks?)\b', text, re.I)
    if not match:
        return None
    token = match.group(1).casefold()
    return words[token] if token in words else int(token)


def _empty_state():
    return {'version': 1, 'slots': {}, 'constraints': {}, 'recent_picks': [], 'ambiguous_players': []}


def _clean_state(state, names, now):
    result = _empty_state()
    if not isinstance(state, dict):
        return result
    constraints = state.get('constraints', {})
    if isinstance(constraints, dict):
        if type(constraints.get('both_teams')) is bool:
            result['constraints']['both_teams'] = constraints['both_teams']
        if constraints.get('preferred_stat') in STAT_COMPONENTS:
            result['constraints']['preferred_stat'] = constraints['preferred_stat']
    stale = False
    if state.get('updated_at'):
        try:
            age = (now - dt.datetime.fromisoformat(str(state['updated_at']).replace('Z', '+00:00'))).total_seconds()
            stale = age < 0 or age > FOCUS_HOURS * 3600
        except (ValueError, TypeError):
            stale = True
    if stale:
        return result
    raw = state.get('slots', {})
    if isinstance(raw, dict):
        if raw.get('player') in names:
            result['slots']['player'] = raw['player']
        if raw.get('stat') in STAT_COMPONENTS:
            result['slots']['stat'] = raw['stat']
        line = _finite(raw.get('line'))
        if line is not None:
            result['slots']['line'] = line
        if raw.get('side') in {'more', 'less'}:
            result['slots']['side'] = raw['side']
        if raw.get('opponent') in TEAM_NAMES:
            result['slots']['opponent'] = raw['opponent']
        if type(raw.get('pick_count')) is int and 2 <= raw['pick_count'] <= 6:
            result['slots']['pick_count'] = raw['pick_count']
        try:
            result['slots']['game_date'] = dt.date.fromisoformat(str(raw['game_date'])).isoformat()
        except (KeyError, TypeError, ValueError):
            pass
    for card in state.get('recent_picks', [])[:8]:
        if isinstance(card, dict) and card.get('player') in names and card.get('stat') in STAT_COMPONENTS:
            clean = {key: card[key] for key in ('player', 'stat')}
            if _finite(card.get('line')) is not None:
                clean['line'] = _finite(card['line'])
            if card.get('side') in {'more', 'less'}:
                clean['side'] = card['side']
            if card.get('line_kind') in {'research_target', 'assistant_proposal'}:
                clean['line_kind'] = card['line_kind']
            try:
                clean['game_date'] = dt.date.fromisoformat(str(card['game_date'])).isoformat()
            except (KeyError, TypeError, ValueError):
                pass
            result['recent_picks'].append(clean)
    result['ambiguous_players'] = [name for name in state.get('ambiguous_players', [])[:4] if name in names]
    return result


def _assistant_cards(text, names, now):
    """Read proposal identity only; source paragraphs cannot become current offers."""
    cards = []
    lines = text.splitlines()[:70]
    game_date = None
    for heading in lines[:5]:
        if re.match(r'^\s*(?:My projections|My shortlist)\b', heading, re.I):
            date, error, _ = _date(heading, now)
            if date and not error:
                game_date = date
    for index, line in enumerate(lines):
        if not re.match(r'^\s*(?:[-•*]|\d+[.)]|\|)', line):
            continue
        entities = _scan(line, names)
        if len(entities['players']) != 1:
            continue
        stat, error = _market(line)
        if not stat or error:
            continue
        side, choose = _side(line)
        # Numbered explanation prose and data tables are not betting offers.
        if not side or choose:
            # The independent-research renderer puts its hypothetical targets
            # on the following line. Preserve its subject, never promote the
            # projection or either target to a confirmed sportsbook offer.
            next_line = lines[index + 1] if index + 1 < len(lines) else ''
            if re.search(r'\b(?:More|Less) if line\s*[≤≥]', next_line, re.I):
                cards.append({'player': entities['players'][0], 'stat': stat,
                              'line_kind': 'research_target'})
            continue
        without_number = re.sub(r'^\s*\d+[.)]\s*', '', line)
        value, error = _line(without_number, [], True)
        if value is None or error:
            continue
        card = {'player': entities['players'][0], 'stat': stat, 'line': value, 'side': side,
                'line_kind': 'assistant_proposal'}
        if re.search(r'\b(?:if|target|projection)\b|[≤≥]', line, re.I):
            card['line_kind'] = 'research_target'
        cards.append(card)
    if game_date:
        for card in cards:
            card['game_date'] = game_date
    return cards[:8]


def _apply_user(text, state, names, now):
    # Mobile dictation/typing commonly joins a comparison and its number.
    # Separate only known market syntax; never rewrite names or arbitrary prose.
    text = re.sub(r'\b(more|less|over|under)(?=\d)', r'\1 ', text, flags=re.I)
    text = re.sub(r'(?<=\d)(?=(?:points?|pts|rebounds?|rebs?|assists?|asts?|threes|pra)\b)',
                  ' ', text, flags=re.I)
    entities = _scan(text, names)
    players = entities['players']
    ambiguous_names = _ambiguous_names(text, names, players)
    slots = state['slots']
    prior = dict(slots)
    game_date, date_error, spans = _date(text, now)
    stat, market_error = _market(text)
    followup = bool(_FOLLOWUP.search(text) or (slots.get('player') and
                    re.fullmatch(r'\s*' + _NUMBER + r'\s*[!?]?\s*', text)))
    explanation = bool(_EXPLAIN.search(text))
    preference = bool(_PREFERENCE.search(text))
    social = bool(_SOCIAL.fullmatch(text))
    both_teams = _constraint(text)
    count = _count(text)
    asks = bool(_ASK.search(text))
    research = bool(_RESEARCH.search(text))
    line, line_error = _line(text, spans, bool(asks or stat or market_error or followup or players))
    side, choosing = _side(text)
    # Minutes are also explanatory workload, not necessarily a newly proposed
    # betting market. A follow-up about playing time keeps the existing prop
    # unless the user supplies a threshold or explicitly switches markets.
    minutes_market = re.search(r'\b(?:minutes?\s+(?:line|prop)|(?:line|prop)\s+(?:for\s+)?minutes?|'
                               r'actually|instead|switch|change to)\b', text, re.I)
    if (stat == 'minutes' and prior.get('player') and prior.get('stat') != 'minutes'
            and line is None and not choosing and not minutes_market and (explanation or followup)):
        stat = None
        explanation = True
    inherited = []
    intent = 'chat'
    if social:
        return {'intent': intent, 'slots': {}, 'original_question': text, 'canonical_question': text,
                'clarification': None, 'errors': [], 'inherited': [], 'entities': entities}
    if both_teams is not None:
        state['constraints']['both_teams'] = both_teams
        if count is not None:
            slots['pick_count'] = count
        if game_date:
            slots['game_date'] = game_date
        return {'intent': 'lineup_constraint', 'slots': dict(slots), 'original_question': text,
                'canonical_question': text, 'clarification': None, 'errors': [],
                'inherited': [], 'entities': entities}
    # Explicit unrelated subjects must never borrow an old betting line.
    if len(players) > 1 or ambiguous_names:
        for field in ('player', 'stat', 'line', 'side'):
            slots.pop(field, None)
        state['ambiguous_players'] = players or ambiguous_names
    elif len(players) == 1:
        player = players[0]
        if slots.get('player') != player:
            for field in ('stat', 'line', 'side', 'opponent'):
                slots.pop(field, None)
            # A named player can select one previously presented card, but a
            # hypothetical target cannot silently become an actual quoted line.
            candidates = [card for card in state['recent_picks'] if card['player'] == player
                          and card.get('line_kind') != 'research_target']
            if followup and len(candidates) == 1 and not stat and line is None:
                slots.update({k: v for k, v in candidates[0].items() if k in {'stat', 'line', 'side'}})
        slots['player'] = player
        state['ambiguous_players'] = []
    elif not (followup or asks or stat or line is not None or market_error):
        # Ordinary discussion does not destroy focus, but it also must not route
        # through an unrelated previous player's statistics.
        return {'intent': 'research' if research else 'chat', 'slots': {}, 'original_question': text,
                'canonical_question': text, 'clarification': None, 'errors': [],
                'inherited': [], 'entities': entities}
    if stat:
        if slots.get('stat') and slots['stat'] != stat:
            # "What about his attempts?" cannot carry a 19.5-points threshold.
            slots.pop('line', None)
            slots.pop('side', None)
        slots['stat'] = stat
    elif market_error:
        slots.pop('stat', None)
        slots.pop('line', None)
        slots.pop('side', None)
    if line is not None:
        slots['line'] = line
    elif line_error:
        slots.pop('line', None)
        slots.pop('side', None)
    if side and (line is not None or (asks and slots.get('line') is not None)):
        slots['side'] = side
    elif choosing:
        slots.pop('side', None)
    if game_date:
        slots['game_date'] = game_date
    opponent = _opponent(text)
    if opponent:
        slots['opponent'] = opponent
    if count is not None:
        slots['pick_count'] = count
    if preference and stat:
        state['constraints']['preferred_stat'] = stat
    for key, value in slots.items():
        if value == prior.get(key):
            inherited.append(key)
    is_prop = bool((slots.get('player') or state['ambiguous_players']) and
                   (line is not None or slots.get('line') is not None or asks or choosing
                    or (followup and (market_error or (stat and prior.get('line') is not None)))))
    if preference:
        intent = 'preference'
    elif explanation and (is_prop or prior.get('player') or state['recent_picks']):
        intent = 'explanation'
    elif is_prop:
        intent = 'prop'
    elif research or players or stat or entities['teams']:
        intent = 'research'
    errors = [error for error in (date_error, market_error, line_error) if error]
    clarification = None
    # A request for "why" may also correct a market or introduce an ambiguous
    # threshold. Resolve that essential input before explaining a different bet.
    changed_prop = bool(errors or line is not None or
                        (stat and stat != prior.get('stat') and prior.get('line') is not None))
    if intent == 'prop' or (intent == 'explanation' and changed_prop):
        if date_error:
            clarification = 'Which game date should I use? Please give one valid date.'
        elif len(players) > 1 or state['ambiguous_players']:
            clarification = 'Which player and line should I assess first?'
        elif line_error:
            clarification = 'Which line should I assess? Give one number for this player and statistic.'
        elif market_error == 'unsupported_market':
            clarification = 'That is a special or partial-game market. I need the full market label to assess it accurately.'
        elif market_error == 'ambiguous_attempts':
            clarification = 'Do you mean field-goal attempts or three-point attempts, and what is the line?'
        elif not slots.get('stat'):
            player = slots.get('player', 'that player')
            value = f"{slots['line']:g}" if slots.get('line') is not None else 'that'
            clarification = f'Is {player}’s {value} line for points, rebounds, assists, PRA, or another statistic?'
        elif slots.get('line') is None:
            clarification = 'What is the line for ' + slots['player'] + ' ' + MARKET_LABELS[slots['stat']] + '?'
    elif (choosing or (asks and line is not None)) and not slots.get('player'):
        intent = 'clarification'
        clarification = 'Which player, statistic, and line should I choose More or Less for?'
    if clarification:
        intent = 'clarification'
    canonical = entities['question']
    if slots.get('player') and intent in {'prop', 'explanation', 'preference'}:
        details = [slots['player']]
        if slots.get('line') is not None:
            details.append(f"line {slots['line']:g}")
        if slots.get('stat'):
            details.append(MARKET_LABELS[slots['stat']])
        if slots.get('side'):
            details.append('proposed ' + slots['side'])
        if slots.get('opponent'):
            details.append('opponent ' + slots['opponent'])
        if slots.get('game_date'):
            details.append('game date ' + slots['game_date'])
        canonical += '\nConversation context: ' + '; '.join(details) + '.'
    return {'intent': intent, 'slots': dict(slots), 'original_question': text,
            'canonical_question': canonical, 'clarification': clarification, 'errors': errors,
            'inherited': inherited, 'entities': entities}


def resolve_conversation(store, master, text, history=None, state=None, now=None):
    """Resolve a turn using bounded conversation history or an existing state.

    Persist ``state`` per user/chat if desired. When supplied, it is authoritative
    dialogue memory and history is not replayed over it. Statistical evidence
    must always be retrieved separately; the returned slots are user context.
    """
    now = _now(now)
    names = _players(store, master)
    current = _clean_state(state, names, now)
    if state is None:
        for turn in (history or [])[-HISTORY_TURNS:]:
            item = _turn(turn)
            if not item:
                continue
            if item['role'] == 'user':
                _apply_user(item['content'], current, names, now)
            else:
                cards = _assistant_cards(item['content'], names, now)
                if cards:
                    current['recent_picks'] = cards
                    # A multi-player proposal requires an explicit selection
                    # before "his" or "that line" can resolve to one leg.
                    if len({card['player'] for card in cards}) > 1:
                        current['ambiguous_players'] = list(dict.fromkeys(card['player'] for card in cards))[:4]
                        for field in ('player', 'stat', 'line', 'side'):
                            current['slots'].pop(field, None)
    result = _apply_user(str(text)[:3000], current, names, now)
    result['original_question'] = str(text)[:3000]
    current['updated_at'] = now.isoformat()
    result['constraints'] = dict(current['constraints'])
    result['recent_picks'] = list(current['recent_picks'])
    result['state'] = current
    result['complete_prop'] = bool(result['slots'].get('player') and result['slots'].get('stat')
                                   and result['slots'].get('line') is not None and not result['errors'])
    return result
