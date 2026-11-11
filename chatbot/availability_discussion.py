"""Focused, read-only answers about a player's status and workload risk.

User statements remain user reports. Current stored official designations keep
their own game date and report clock; neither source establishes normal minutes.
This discussion does not qualify a pick or fetch a live schedule.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from board_shortlist import MARKET_NAMES, player_availability
from pick_discussion import normalize_stat

EASTERN = ZoneInfo('America/New_York')
_SIGNAL = re.compile(r'\b(?:questionable|doubtful|probable|injur\w*|availability|'
    r'game[ -]time decision|playing (?:status|clearance)|ruled (?:out|in)|'
    r'minutes? (?:restriction|limit|cap)|minutes? restricted|limited minutes|'
    r'restricted minutes|minutes? (?:are|is|might be|could be) limited|'
    r'(?:if|when) (?:he|she|they) (?:only )?(?:plays?|sits?|is active|is cleared)|'
    r'(?:will|can|could|does|is) (?:he|she|they) (?:play|playing|out|active|available)|'
    r'(?:his|her|their) (?:status|minutes|workload)|'
    r'(?:is|was|listed as|reported as) (?:not |no longer )?(?:out|available|cleared|active))\b', re.I)
_ACTION = re.compile(r'\b(?:why (?:wait|hold|pass)|what (?:should|do) (?:we|i) do|'
    r'what (?:now|next)|what about (?:that|it)|does that change|so (?:wait|hold|pass)|'
    r'how (?:long|come)|why is that|what are (?:we|you) waiting for)\b', re.I)
_WORKFLOW = re.compile(r'\b(?:api|selftest|refresh|deployment|worker|model|download|'
    r'excel|dataset|database|source.ref|bot status)\b', re.I)
_NEGATION = re.compile(r"\b(?:not|never|no longer|isn.t|isnt|wasn.t|wasnt)\s+(?:yet\s+)?(?:listed (?:as )?)?$", re.I)


def _now(value=None):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            value = None
    value = value or dt.datetime.now(dt.timezone.utc)
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _day(slots, now):
    try:
        return dt.date.fromisoformat(str(slots.get('game_date') or slots.get('date') or now.astimezone(EASTERN).date())).isoformat()
    except (TypeError, ValueError):
        return None


def _history_turns(history):
    result = []
    for value in list(history or [])[-6:]:
        value = value.model_dump() if hasattr(value, 'model_dump') else value
        if isinstance(value, dict) and value.get('role') in {'user', 'assistant'}:
            result.append({'role': value['role'], 'content': str(value.get('content') or '')[:2500]})
    return result


def _focused_history(history, player, day):
    """Only our typed, player/date-scoped reply makes a generic follow-up focused."""
    for value in reversed(_history_turns(history)):
        if value['role'] != 'assistant':
            continue
        line = value['content'].split('\n', 1)[0]
        return bool(line.startswith(player + ' — availability') and '(' + day + ')' in line)
    return False


def can_handle_availability(question, turn, history=None):
    turn = turn if isinstance(turn, dict) else {}
    slots = turn.get('slots') or {}
    if not slots.get('player') or turn.get('clarification') or turn.get('errors'):
        return False
    if len((turn.get('entities') or {}).get('players', [])) > 1:
        return False
    text = str(question or '')
    if _WORKFLOW.search(text):
        return False
    if (re.search(r'\b(?:lines?|markets?|offers?|prizepicks)\b', text, re.I)
            and not re.search(r'\b(?:questionable|doubtful|injur\w*|ruled out|minutes?|workload|clearance)\b', text, re.I)):
        return False
    historical_minutes = (re.search(r'\bminutes?\b', text, re.I) and
        re.search(r'\b(?:last|past|previous|recent)\s+\d+\s+games?\b|'
                  r'\b(?:averag\w*|median|game logs?)\b', text, re.I))
    status_or_limit = re.search(r'\b(?:questionable|doubtful|injur\w*|'
        r'restrict\w*|limit\w*|cap(?:ped)?|clearance|ruled (?:out|in))\b', text, re.I)
    if historical_minutes and not status_or_limit:
        # A request for observed workload is still a statistics question.
        return False
    if _SIGNAL.search(text):
        return True
    day = _day(slots, _now())
    return bool(day and _ACTION.search(text) and _focused_history(history, slots['player'], day))


def _reported_status(text):
    """Extract a conversational assertion, not a clinical or publisher fact."""
    text = str(text or '').casefold().replace('’', "'")
    found = []
    for status in ('out', 'doubtful', 'questionable', 'probable', 'available', 'cleared'):
        for match in re.finditer(r'\b' + status + r'\b', text):
            # Only player-status uses of "out" count; "out of four" does not.
            if status == 'out' and not re.search(r'\b(?:ruled|is|he.s|he is|listed|not|isn.t|if he is)\s*$', text[max(0, match.start()-35):match.start()]):
                continue
            before = text[max(0, match.start()-35):match.start()]
            negated = bool(_NEGATION.search(before))
            boundary = [item.end() for item in re.finditer(r'[?,;.!]\s*|\b(?:but|however)\b', text[:match.start()])]
            clause = text[boundary[-1] if boundary else 0:match.start()].strip()
            mode = ('conditional' if re.search(r'\bif\b', clause) else
                    'question' if re.match(r'^(?:is|was|will|could|can|does|do|has|have|how|why|what|should)\b', clause)
                    or text[match.end():].lstrip().startswith('?')
                    or re.search(r'\bor\b', clause) else 'reported')
            found.append({'status': status, 'negated': negated, 'position': match.start(), 'mode': mode,
                          'negation': 'no longer' if negated and re.search(r'\bno longer\s+(?:listed (?:as )?)?$', before) else 'not'})
    if not found:
        return None
    latest = {}
    for item in sorted(found, key=lambda value: value['position']):
        latest[item['status']] = item
    reports = [item for item in latest.values() if item['mode'] == 'reported']
    positives = [item for item in reports if not item['negated']]
    chosen = max(positives or reports or list(latest.values()), key=lambda value: value['position'])
    return {key: chosen[key] for key in ('status', 'negated', 'mode', 'negation')}


def _user_report(question, history, player, day, now):
    current = _reported_status(question)
    if current:
        return current
    if not _focused_history(history, player, day):
        return None
    from basketball_research import _scan
    for turn in reversed(_history_turns(history)):
        if turn['role'] != 'user':
            continue
        text = turn['content']
        report = _reported_status(text)
        if not report:
            continue
        names = _scan(text, [player])['players']
        pronoun = re.search(r'^\s*(?:he|she|they|his|her|their)\b', text, re.I)
        if not names and not pronoun:
            continue
        from conversation import _date
        previous_day, error, _ = _date(text, now)
        if error or previous_day and previous_day != day:
            return None
        return {**report, 'from_history': True}
    return None


def _clock(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(EASTERN).strftime('%b %d, %I:%M %p Eastern').replace(', 0', ', ')
    except (TypeError, ValueError):
        return None


def _source_urls(availability):
    values = [(availability.get('official_designation') or {}).get('source_url')]
    values += [row.get('url') for row in availability.get('reports', []) if isinstance(row, dict)]
    result = []
    for value in values:
        parsed = urlsplit(str(value or ''))
        if parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username and not parsed.password:
            result.append(str(value))
    return list(dict.fromkeys(result))[:4]


def build_availability_discussion(store, slots, question, now=None, history=None):
    """Answer the status/workload question without repeating a schedule eligibility gate."""
    now, slots = _now(now), dict(slots or {})
    player, day = str(slots.get('player') or '').strip(), _day(slots, now)
    if not player or not day:
        return {'answer': 'Which player and game date should I use for the availability question?',
                'sources': [], 'ai_used': False, 'response_mode': 'availability_discussion'}
    stat = normalize_stat(slots.get('stat'))
    line = slots.get('line')
    line = float(line) if type(line) in (int, float) and math.isfinite(line) and line >= 0 else None
    metric = {'pr': 'points + rebounds', 'pa': 'points + assists',
              'ra': 'rebounds + assists', 'pra': 'points + rebounds + assists'}.get(stat, MARKET_NAMES.get(stat))
    offer = f'{line:g} {metric}' if stat and line is not None else 'this pick'
    report = _user_report(question, history, player, day, now)
    read_ok = True
    try:
        availability = player_availability(store.recent_news(150), player, day, now, store=store) if store is not None else {}
    except Exception:
        # A focused explanation remains possible, but a failed read is not a
        # successful check that found no injury or minutes restriction.
        availability, read_ok = {}, False
    official = availability.get('official_designation') or {}
    status = official.get('status') if official.get('known') and official.get('status') in {
        'Out', 'Doubtful', 'Questionable', 'Probable', 'Available'} else None
    clock = _clock(official.get('report_at')) if status else None
    paragraphs = [f'{player} — availability' + (f' for {offer}' if stat and line is not None else '') + f' ({day}).']
    if report and report['mode'] == 'reported':
        prefix = 'You previously reported' if report.get('from_history') else 'You report'
        described = ((report.get('negation', 'not') + ' ') if report['negated'] else '') + report['status']
        paragraphs.append(f'{prefix} that he is {described}; I have not independently verified that update.')
    query = str(question or '').casefold()
    if re.search(r'\b(?:what.*mean|meaning|define)\b', query) and 'questionable' in query:
        paragraphs.append('Questionable means playing status is undecided. It does not establish that he will play or receive his usual minutes.')
    if status:
        paragraphs.append(f'The latest current saved NBA injury report lists {status}' +
                          (f' as of {clock}.' if clock else '.'))
    else:
        paragraphs.append('No current, dated official designation is verified in the saved records for this game.' if read_ok
                          else 'Saved availability records could not be checked in this reply.')
    negative_report = bool(report and report['mode'] == 'reported' and not report['negated'] and
                           report['status'] in {'out', 'doubtful', 'questionable'})
    restriction = bool(re.search(r'\b(?:minutes? (?:restriction|limit|cap)|limited minutes|restricted minutes)\b', query))
    no_restriction = bool(re.search(r'\b(?:no|without|not under|not on)\s+(?:a\s+)?minutes? (?:restriction|limit|cap)\b', query))
    source_caution = bool(availability.get('blocked') or availability.get('note'))
    why_wait = bool(re.search(r'\b(?:why (?:wait|hold|pass)|what are (?:we|you) waiting for|why is that)\b', query))
    if_plays = bool(re.search(r'\b(?:if|when) (?:he|she|they) (?:only )?(?:plays?|is active|is cleared)\b', query))
    if status == 'Out':
        paragraphs.append('Leave him out of this game’s selections while that Out designation stands. A later dated clearance would need a new review of his role and minutes.')
    elif why_wait:
        unresolved = status in {'Questionable', 'Doubtful'} or negative_report or not status
        paragraphs.append(('The unresolved questions are whether he will play and what workload he would receive. '
                           if unresolved else 'A playing-status designation does not establish his expected workload. ') +
                          'His usual full-game history cannot settle those questions. A dated clearance and a clear role/minutes expectation would let us reassess ' +
                          offer + '; until that context is clear, I’d hold it.')
    elif if_plays:
        paragraphs.append('Playing would settle participation, but it would not by itself establish normal minutes. '
                          'If he is active with his usual role and no reported limit, reassess ' + offer +
                          ' against the master history. If he is eased back or capped, the usual full-game sample is a poor workload match. '
                          'Neither workload scenario is confirmed here.')
    elif status in {'Questionable', 'Doubtful'} or negative_report or source_caution:
        paragraphs.append('I’d hold this pick while playing status or workload is unresolved. If he is ruled out, leave him out; if he is cleared with his normal role and minutes and no reported limit, reassess ' + offer + ' using the master history.')
    elif status == 'Available':
        paragraphs.append('Available establishes the saved designation, not a starting role or unrestricted minutes. Check the expected workload before reassessing ' + offer + '; clearance alone does not qualify the pick.')
    else:
        paragraphs.append('I’d wait for a dated playing-status update and a clear workload expectation before choosing a side. If he is cleared with his normal role, reassess ' + offer + '; if he sits, leave him out.')
    numeric_workload = bool(re.search(r'\b\d+(?:\.\d+)?\s*(?:minutes?|mins?)\b', query))
    stored_minutes_caution = any(re.search(r'\bminutes?\s+(?:restriction|limit|cap)\b', str(row.get('title') or ''), re.I)
                                 for row in availability.get('reports', []) if isinstance(row, dict))
    if restriction and not no_restriction or stored_minutes_caution or numeric_workload:
        paragraphs.append('A minutes limit would weaken the comparison with usual full-game totals. No numerical minutes forecast is established here, so that condition does not create a new projection.')
    elif no_restriction:
        paragraphs.append('A report of no minutes restriction would help, but it would not guarantee normal minutes or erase a conflicting availability report.')
    elif re.search(r'\b(?:blowout|bench|benched|foul trouble)\b', query):
        paragraphs.append('A blowout or foul trouble could also cut minutes after clearance. Those are conditional risks, not events established by the saved averages.')
    return {'answer': '\n\n'.join(paragraphs), 'sources': _source_urls(availability),
            'ai_used': False, 'response_mode': 'availability_discussion',
            'discussion': {'player': player, 'stat': stat, 'line': line, 'date': day,
                           'decision': 'hold', 'focus': 'availability'},
            'availability_discussion': {'player': player, 'game_date': day,
                'user_report': report, 'official_status': status, 'official_report_at': official.get('report_at') if status else None,
                'focus': 'why_wait' if why_wait else 'if_plays' if if_plays else 'status_plan',
                'saved_records_read': read_ok, 'research_only': True,
                'availability_verified': status is not None, 'minutes_verified': False,
                'offer_verified': False}}
