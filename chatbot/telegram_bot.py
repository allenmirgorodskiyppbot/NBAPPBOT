"""Private Telegram interface with durable updates, upload drafts and an outbox.

The bot does not log Telegram API URLs because those URLs contain its token.
Only explicitly allowlisted users in private chats can invoke commands.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import csv
from datetime import datetime, timezone, timedelta
import io
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

import httpx

# httpx's INFO messages include request URLs and would expose Telegram bot tokens.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('knicks.telegram')

MAX_UPLOAD = 8 * 1024 * 1024
MAX_MASTER_UPLOAD = 20 * 1024 * 1024
TYPING_INTERVAL_SECONDS = 4
MAX_ACTIVE_COMMANDS = 10
RESPONSIVE_COMMANDS = {'/start', '/help', '/status', '/ai_status', '/clear', '/newboard',
                       '/reminder', '/usage', '/freshness', '/strategy', '/refresh', '/update', '/selftest',
                       '/pp_status', '/pp_refresh', '/pp'}


TRANSPORT_BUSY_TIMEOUT_MS = 100


def sqlite_contention(error):
    """Only genuine SQLite BUSY/LOCKED codes are transient writer contention."""
    code = getattr(error, 'sqlite_errorcode', None)
    return (isinstance(error, sqlite3.OperationalError) and type(code) is int
            and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def sqlite_diagnostic(error):
    code = getattr(error, 'sqlite_errorcode', None)
    name = getattr(error, 'sqlite_errorname', None)
    return {'error_type': type(error).__name__,
            'sqlite_errorcode': code if type(code) is int else None,
            'sqlite_errorname': name if isinstance(name, str) and re.fullmatch(r'SQLITE_[A-Z0-9_]{1,60}', name) else None}


class TelegramAPIError(RuntimeError):
    """Safe error metadata; never retain provider text or token-bearing URLs."""
    def __init__(self, code, retry_after=None):
        super().__init__(f'Telegram API returned status {code}; retrying when available.')
        self.code = code
        self.retry_after = retry_after


def safe_ai_request_details(value):
    """Expose request performance metadata without model text or private URLs."""
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key in ('elapsed_seconds', 'model_load_seconds', 'prompt_eval_seconds', 'generation_seconds'):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item) and item >= 0:
            safe[key] = round(item, 3)
    for key in ('prompt_chars', 'evidence_count', 'omitted_evidence_count', 'prompt_tokens', 'generated_tokens',
                'model_calls', 'repair_attempts'):
        item = value.get(key)
        if type(item) is int and item >= 0:
            safe[key] = item
    if type(value.get('cache_hit')) is bool:
        safe['cache_hit'] = value['cache_hit']
    enums = {
        'phase': {'evidence_reply'},
        'status': {'running', 'repairing', 'needs_retrieval', 'completed', 'cancelled', 'failed'},
        'validation_reason': {'invalid_json', 'invalid_schema', 'invalid_evidence', 'analysis_too_long',
                              'retrieval_with_analysis', 'empty_reply', 'numerical_prose', 'unsupported_assignment',
                              'unsupported_scope', 'unsupported_number', 'unsupported_metric',
                              'unsupported_recommendation', 'unsupported_availability', 'unsupported_certainty'},
        'finish_reason': {'stop', 'length', 'load'},
        'failure_category': {'timeout', 'connection_error', 'model_error', 'invalid_model_response',
                             'empty_model_answer', 'output_limit', 'tool_limit', 'grounding_error', 'internal_error'},
    }
    for key, allowed in enums.items():
        item = value.get(key)
        if isinstance(item, str) and (item in allowed or
                key == 'failure_category' and re.fullmatch(r'model_http_[1-5]\d{2}', item)):
            safe[key] = item
    return safe


BOARD_EXAMPLE = '/board YYYY-MM-DD\nJalen Brunson | points | more | 24.5\nJosh Hart | rebounds | less | 8.5'
HELP = '''Knicks Research bot
/today — today’s NBA games
/news — recent relevant news
/summary [injuries|news|trades] — fast summary of collected reports (default: injuries)
/reminder — next Knicks game-day digest time and schedule health
/usage — AI work counters and how hosting credits are charged
/status — feed freshness and bot status
/freshness — daily import coverage and next game’s data readiness, without AI
/refresh (or /update) — check for dataset updates now; results arrive when the check finishes
/refresh status (or /update status) — current stage, elapsed time, and last result
/ai_status — local AI readiness, busy state and last failure category
/selftest — start background isolated data/backup tests; the report arrives when finished
/selftest status — current check, elapsed time and last self-test report
/playerlog NAME | points | 5 — stored game logs and averages without waiting for AI
/picks [Nov 2|today|tomorrow] [2-6] — own projections; date/count optional
/pp_refresh (or /pp) — check the public PrizePicks feed from this bot's server
/pp_status — saved PrizePicks connection status without another request
/clear — clear this chat's conversation context and lineup preferences
/diagnose — check the built-in public NBA feed connections
/importstats — attach CSV/JSON box scores with this caption to import manual stats
/importmaster — attach an updated master Excel with this caption to merge missing data
/strategy — explain the current selection strategy
/stats <JSON list> — import manually supplied box-score rows
/newsnote HEADLINE then optional details — save an unverified personal news note
/boards — uploaded PrizePicks boards
/newboard — start a fresh screenshot set (saved boards and entries stay saved)
/board DATE then player | stat | more/less | line — save available lines
/analyze BOARD_ID — analyze an uploaded board
/track BOARD_ID STAKE power|flex [MULTIPLIER] — record that board as an entry you placed
/entry <JSON object> — record an entry you actually placed
/entries — tracked entries and outcomes
/results — results and confirmed profit/loss
/settle ENTRY_ID PAYOUT [note] — confirm the actual total return shown by PrizePicks

Ask for picks or use /picks to get the bot's own projections and conditional target lines before checking PrizePicks. You can also send just "Nov 2", "November 2", or "11/2". Dates without a year use the current NBA season (July–June); numeric dates are month/day. With no date, it uses the next unstarted saved Knicks game; "tonight" or an explicit date stays on that day. It can return fewer candidates when evidence is insufficient. These are model targets, not observed PrizePicks offers or placed entries.

When the public PrizePicks connection is available, /picks also compares fresh cached offers for that exact matchup. /pp_refresh tests collection on the bot's server; /pp_status shows its last result. A refused, stale or incomplete feed does not become a current offer. Available sides and payout terms stay unknown when the source does not provide them.

The daily dataset refresh runs at 11:30 AM Eastern. The bot sends a result and coverage summary; unavailable sources stay marked unavailable. /refresh checks again on demand.

You can also send a question, a CSV/JSON board file, or screenshots for optional line comparison. Readable full-game cards are combined into a research shortlist; unclear cards stay out. Both visible More/Less choices are compared. The upload's Eastern date is used unless you caption the image YYYY-MM-DD; it must match a saved game. Send images as files for original quality. Check every displayed line. Available lines are separate from entries actually placed.

For the manual-stats fallback, attach a CSV/JSON file with the caption /importstats. A file without that caption is treated as a PrizePicks board. Screenshots stay OCR drafts and never import master statistics automatically. Imported stats are labeled as manual user data. Missing statistics must remain blank, not zero.

/newsnote saves your note as user-supplied and unverified; it does not broadcast an alert or claim the report is official.

Board example (use the actual game date):
''' + BOARD_EXAMPLE + '''

The readable format defaults to team NYK. For an opponent player, add the correct team as a fifth field, for example:
Derrick White | assists | less | 4.5 | BOS

To record the exact two-pick board above as a placed $10 Power entry with a displayed 3x return:
/track BOARD_ID 10 power 3

Placed entry format:
/entry {"legs":[...],"stake":10,"entry_type":"power","payout_multiplier":3}
Use 2–6 legs and copy the actual entry type and displayed multiplier from your slip. PAYOUT means total returned, including stake, not profit.'''


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _prizepicks_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return 'unknown'
        return stamp.astimezone(ZoneInfo('America/New_York')).strftime('%b %d, %I:%M %p Eastern').replace(', 0', ', ')
    except (TypeError, ValueError):
        return 'unknown'


def prizepicks_status_reply(status, *, compact=False):
    """Only operational metadata, never response bodies, cookies or account data."""
    status = status if isinstance(status, dict) else {}
    phase = status.get('phase')
    descriptions = {
        'disabled': 'PrizePicks public feed: disabled.',
        'ready': 'PrizePicks public feed: responding.',
        'no_offers': 'PrizePicks public feed: responding; no matching offers were found.',
        'blocked': 'PrizePicks public feed: access was refused on the bot server.',
        'rate_limited': 'PrizePicks public feed: source rate limit is active.',
        'unavailable': 'PrizePicks public feed: unavailable on the last check.',
        'schema_unavailable': 'PrizePicks public feed: response could not be validated.',
        'partial': 'PrizePicks public feed: incomplete response; current offers are held.',
        'no_saved_game': 'PrizePicks public feed: no saved upcoming Knicks game is available.',
        'closed': 'PrizePicks public feed: worker is stopped.',
    }
    first = descriptions.get(phase, 'PrizePicks public feed: waiting for a verified server check.')
    code = status.get('http_status')
    if type(code) is int and 100 <= code <= 599:
        first += f' HTTP {code}.'
    error_code = status.get('error_code')
    safe_codes = {'access_refused', 'rate_limited', 'http_error', 'request_timeout', 'connection_failed',
        'feed_validation_failed', 'invalid_included_resource', 'conflicting_included_resource',
        'invalid_projection_resource', 'duplicate_projection_across_pages', 'nba_league_identity_unverified',
        'saved_matchup_unavailable', 'invalid_json_api_schema', 'invalid_pagination_metadata',
        'pagination_limit_exceeded', 'invalid_empty_pagination', 'pagination_page_exceeds_total',
        'unsafe_or_incoherent_pagination', 'pagination_completeness_unverified', 'invalid_response_age',
        'stale_source_response', 'response_size_limit', 'invalid_json_response',
        'pagination_count_changed_during_read', 'pagination_changed_during_read', 'pagination_count_incomplete'}
    if isinstance(error_code, str) and error_code in safe_codes:
        first += ' Check: ' + error_code + '.'
    if compact:
        return first + ' Automatic checks continue; /pp_status shows the last server result.'
    lines = [first, 'This connection is checked by the deployed bot.']
    if status.get('checked_at'):
        lines.append('Last check: ' + _prizepicks_time(status['checked_at']) + '.')
    if status.get('last_success_at'):
        lines.append('Last source success: ' + _prizepicks_time(status['last_success_at']) + '.')
    for field, label in (('source_projection_count', 'Returned projection records'),
                         ('matched_offers', 'Matched offer records'), ('readable_selections', 'Confirmed offered sides')):
        number = status.get(field)
        if type(number) is int and 0 <= number <= 50000:
            lines.append(f'{label}: {number}.')
    if status.get('next_check_at'):
        lines.append('Next allowed check: ' + _prizepicks_time(status['next_check_at']) + '.')
    if phase == 'blocked':
        lines.append('The source refusal is retained; the bot does not retry through alternate hosts or account sessions.')
    lines.append('Only public line collection is configured. No entry is placed.')
    return '\n'.join(lines)


def freshness_report(store, now=None):
    """Summarize saved collection evidence; never fetch, infer, or mark it fresh."""
    from independent_picks import _select_game
    from pregame_freshness import assess_pregame_freshness

    now = now or datetime.now(timezone.utc)
    eastern = ZoneInfo('America/New_York')

    def day(value):
        try:
            value = str(value)
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
        except (ValueError, TypeError):
            return None

    def stamp(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return 'not recorded'
            return parsed.astimezone(eastern).strftime('%b %d, %I:%M %p Eastern').replace(', 0', ', ')
        except (ValueError, TypeError):
            return 'not recorded'

    status = store.status()
    status = status if isinstance(status, dict) else {}
    refresh = status.get('nightly_nba_refresh') or {}
    refresh = refresh if isinstance(refresh, dict) else {}
    target = day(refresh.get('target_game_date')) or 'not recorded'
    through = day(refresh.get('source_data_through_date')) or 'unknown'
    lines = ['Data freshness — saved checks', f'Daily import target: {target}; covered through: {through}.']
    feeds = status.get('feeds') or []
    last_success = next((feed.get('last_success') for feed in feeds if isinstance(feed, dict)
                         and feed.get('name') == 'nba_daily_refresh'), None)
    lines.append('Last successful import: ' + stamp(last_success or refresh.get('last_success')) + '.')
    pending = refresh.get('pending_dates')
    if isinstance(pending, list):
        dates = sorted({value for raw in pending if (value := day(raw))})
        detail = ', '.join(dates[:3]) + (f' (+{len(dates) - 3} more)' if len(dates) > 3 else '')
        lines.append('Pending import dates: ' + (detail if dates else 'none queued' if not pending else 'unknown') + '.')
    else:
        lines.append('Pending import dates: not recorded.')
    tracking = status.get('tracking_refresh') or {}
    coverage = tracking.get('coverage') if isinstance(tracking, dict) else None
    if isinstance(coverage, list) and coverage:
        collected = [row for row in coverage if isinstance(row, dict) and row.get('status') == 'collected']
        unknown = sum(not day(row.get('source_data_through_date')) for row in collected)
        unavailable = sum(not isinstance(row, dict) or row.get('status') != 'collected' for row in coverage)
        lines.append(f'Tracking: {unknown} collected categories have unknown covered-game dates; {unavailable} unavailable.')
    else:
        lines.append('Tracking coverage: not recorded; freshness unknown.')
    try:
        game, error = _select_game(store, None, 'NYK', now)
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        game, error = None, 'The saved schedule is unavailable.'
    if game:
        teams = list(dict.fromkeys([game['home_team'], game['away_team']]))
        lines.append(f"Next saved game: {game['away_team']} at {game['home_team']} — {stamp(game.get('start_time'))}.")
        reports = [assess_pregame_freshness(store, game['date'], team, now) for team in teams]
        ready = all(report.get('ready') is True for report in reports)
        statistics_ready = all(report.get('statistics_ready') is True for report in reports)
        if ready:
            lines.append('Game-stat readiness: current for both teams.')
        elif statistics_ready:
            if game['date'] == now.astimezone(eastern).date().isoformat():
                lines.append('Game-stat readiness: completed-game coverage verified for both teams; research ready.')
                lines.append('Live timing: unverified; current-game eligibility is incomplete.')
            else:
                lines.append('Game-stat readiness: completed-game inputs verified as of today; future game-day checks remain required.')
                lines.append('Live timing: unverified for the upcoming game.')
        else:
            lines.append('Game-stat readiness: waiting for verified coverage.')
        if not ready:
            schedule = next((report.get('schedule_check') for report in reports
                             if isinstance(report.get('schedule_check'), dict)), {})
            if schedule.get('status'):
                # The checker stores a safe error category/status, never the
                # remote exception body or a configured URL with credentials.
                error = str(schedule.get('error') or '')
                safe_error = (error if re.fullmatch(r'[A-Za-z]+(?:: HTTP [1-5][0-9]{2})?', error)
                              else 'event inventory mismatch' if schedule.get('status') == 'partial'
                              else 'source check failed' if error else None)
                label = schedule['status'] if schedule['status'] in {'success','failed','partial','checking'} else 'unknown'
                lines.append('Schedule check: ' + label + (' — ' + safe_error if safe_error else '')
                             + '; checked ' + stamp(schedule.get('checked_at')) + '.')
                attempts = schedule.get('attempts')
                if isinstance(attempts, list) and attempts:
                    sources = []
                    for attempt in attempts[:3]:
                        if not isinstance(attempt, dict):
                            continue
                        source = str(attempt.get('source') or '')
                        name = ('ESPN' if source.startswith('https://site.api.espn.com/') else
                                'NBA' if source.startswith('https://cdn.nba.com/') else
                                'schedule relay' if source.startswith('https://raw.githubusercontent.com/') else 'other source')
                        code = str(attempt.get('error') or '')
                        code = code if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,50}(?:: HTTP [0-9]{3})?', code) else 'check failed'
                        sources.append(name + ': ' + code)
                    if sources:
                        lines.append('Schedule source checks: ' + '; '.join(sources) + '.')
            reasons = list(dict.fromkeys(str(reason).replace('\n', ' ').strip()[:220]
                           for report in reports for reason in report.get('reasons', []) if reason))
            lines.extend('Needs: ' + reason for reason in reasons[:3])
            if not reasons:
                lines.append('Needs: current game-stat coverage has not been verified.')
    else:
        lines.append('Next game: ' + str(error or 'No upcoming saved game is available.')[:220])
        lines.append('Game-stat readiness: not verified without a saved upcoming game.')
    if refresh.get('enabled') is False:
        lines.append('Automatic refresh is disabled. Configured schedule: 11:30 AM Eastern daily, with hourly retries.')
    elif refresh.get('enabled') is True:
        lines.append('Automatic refresh: 11:30 AM Eastern for the previous day; hourly retries while waiting.')
    else:
        lines.append('Refresh schedule: 11:30 AM Eastern for the previous day; hourly retries. Worker state not recorded.')
    return '\n'.join(lines)


def chunks(text: str, size: int = 3500):
    text = str(text)
    while text:
        boundary = min(len(text), size)
        if boundary < len(text):
            newline = text.rfind('\n', 0, boundary)
            if newline > size // 2:
                boundary = newline + 1
        yield text[:boundary]
        text = text[boundary:]


_PICK_MONTHS = {name: number for number, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
_SHORT_PICK_DATE = re.compile(
    r'(?<![\w/-])(?:'
    r'(?P<name>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?'
    r'|(?P<month_number>\d{1,2})/(?P<day_number>\d{1,2})(?:/(?P<year_number>\d{4}))?'
    r')(?![\w/–—-]|\.\d)')


def _normalize_pick_dates(argument, current):
    """Keep a short date in the current season, including past dates."""
    from season_calendar import current_season
    season_start = int(current_season(current).split('-')[0])
    def replace(match):
        month = (_PICK_MONTHS[match['name'][:3]] if match['name'] else int(match['month_number']))
        day = int(match['day'] or match['day_number'])
        explicit_year = match['year'] or match['year_number']
        year = int(explicit_year) if explicit_year else season_start + (month < 7)
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            raise ValueError('That date is not valid. Try Nov 2, 11/2, today, or a full YYYY-MM-DD date.') from None
    return _SHORT_PICK_DATE.sub(replace, argument)


def parse_picks_request(text, now=None, default_count=4):
    """Route explicit research requests; questions about old picks stay in chat.

    The routing only chooses a capability. All players, projections and targets
    come from the research builder, never from question-specific canned replies.
    """
    value = str(text).strip()
    lower = value.casefold().replace('’', "'")
    command = re.match(r'^/picks(?:@[a-z0-9_]+)?(?:\s+(.*))?$', lower, re.S)
    if value.startswith('/') and not command:
        return None
    if command:
        argument = (command.group(1) or '').strip()
    elif _SHORT_PICK_DATE.fullmatch(lower.rstrip('?! .')):
        # An entire date is an explicit shortcut, not a phrase inside ordinary
        # chat. It uses the same research path as /picks with no model call.
        argument = lower.rstrip('?! .')
    else:
        lower = re.sub(r'\bnew york(?:\s+knicks)?\b', 'knicks', lower)
        original_argument = lower
        # Mask complete date spans for intent checks only. A month in a player
        # name or unrelated prose must not broaden the supported request grammar.
        lower = _SHORT_PICK_DATE.sub('2000-01-01', lower)
        quantity = r'(?:[2-6]|two|three|four|five|six)'
        request_head = (r'^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?'
                        r'(?:(?:give|make|generate|find|show|recommend|suggest)|'
                        r'(?:i\s+)?(?:need|want)|i(?:\s+would|\'d)\s+like)\s+'
                        r'(?:(?:me|us|your|some|the|a|an|best|own|good|knicks|nyk)\s+)*'
                        r'(?:' + quantity + r'[ -]legs?\b|(?:' + quantity + r'\s+)?'
                        r'(?:(?:knicks|nyk)\s+)?(?:picks?|projections?)\b)')
        intent = re.match(request_head, lower) or re.match(r'^(?:(?:knicks|nyk)\s+)?(?:picks?|projections?)\b', lower)
        noun = re.search(r'\b(?:picks?|projections?)\b|\b(?:[2-6]|two|three|four|five|six)[ -]leg\b', lower)
        old = re.search(r'\b(?:why|explain|explanation|previous|last|placed|submitted|won|lost|'
                        r'winning|losing|settled|results?|these|those|my picks|existing)\b', lower)
        if not intent or not noun or old:
            return None
        # Player-specific questions keep the ordinary research path. This check
        # concerns request grammar, not a hard-coded list of player names.
        if re.search(r'\b(?:about|against|versus)\b|\bfor\s+'
                     r'(?!tonight\b|today\b|tomorrow\b|next\b|the\b|this\b|knicks\b|nyk\b|\d{4}-)[a-z]', lower):
            return None
        generic_words = set(('please can could would you give make generate find show recommend suggest '
            'i need want like me us your some the a an best own good picks pick projections projection '
            'two three four five six leg legs for on today tonight tomorrow next this game games '
            'knicks nyk nba now before checking check prizepicks pp with reasons reason brief briefly '
            'concise and model targets using saved data dataset no uploads screenshots').split())
        if any(word not in generic_words for word in re.findall(r"[a-z]+", lower)):
            return None
        argument = original_argument
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    today = current.astimezone(ZoneInfo('America/New_York')).date()
    argument = _normalize_pick_dates(argument, current)
    if '/' in argument:
        raise ValueError('Use a complete month/day date such as 11/2 or Nov 2. A year, if included, needs four digits.')
    dates = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', argument))
    relative = set(re.findall(r'\b(?:today|tonight|tomorrow|next)\b', argument))
    if 'today' in relative:
        relative.discard('tonight')
    if len(dates) > 1 or len(relative) > 1 or dates and relative:
        raise ValueError('Use one date, today, tomorrow, or next for the requested game.')
    date = None
    if dates:
        date = datetime.strptime(next(iter(dates)), '%Y-%m-%d').date().isoformat()
    elif relative & {'today', 'tonight'}:
        date = today.isoformat()
    elif 'tomorrow' in relative:
        date = (today + timedelta(days=1)).isoformat()
    without_date = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', argument)
    number_words = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
    numbers = [int(n) for n in re.findall(r'\b\d+\b', without_date)]
    numbers.extend(number_words[n] for n in re.findall(r'\b(?:two|three|four|five|six)\b', without_date))
    if len(set(numbers)) > 1 or numbers and not 2 <= numbers[0] <= 6:
        raise ValueError('Request between 2 and 6 distinct-player targets; fewer may be supported.')
    count = numbers[0] if numbers else default_count
    if type(count) is not int or not 2 <= count <= 6:
        count = 4
    if command:
        remaining = re.sub(r'\b(?:\d{4}-\d{2}-\d{2}|\d+|two|three|four|five|six|today|tonight|tomorrow|next)\b', '', argument)
        if remaining.strip():
            raise ValueError('Use /picks, /picks Nov 2, /picks 11/2, or /picks today. An optional count is 2–6.')
    return {'date': date, 'count': count, 'team': 'NYK'}


def parse_board_document(filename: str, payload: bytes):
    if len(payload) > MAX_UPLOAD:
        raise ValueError('Board upload is too large; use a smaller CSV or JSON file.')
    text = payload.decode('utf-8-sig')
    if filename.lower().endswith('.json'):
        parsed = json.loads(text)
        legs = parsed.get('legs') if isinstance(parsed, dict) else parsed
    elif filename.lower().endswith('.csv'):
        legs = list(csv.DictReader(io.StringIO(text)))
    else:
        raise ValueError('Send a .csv or .json board file, or upload the screenshot as a photo.')
    if not isinstance(legs, list) or not legs or len(legs) > 100:
        raise ValueError('The board must contain 1–100 line objects.')
    return legs


def parse_board_text(argument: str):
    """Parse readable, explicitly dated lines; JSON remains available for power users."""
    if argument.lstrip().startswith(('[', '{')):
        value = json.loads(argument)
        return value.get('legs') if isinstance(value, dict) else value
    lines = [line.strip() for line in argument.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError('Use /board YYYY-MM-DD followed by one player | statistic | more/less | line per row.')
    date = lines[0]
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        raise ValueError('The first board line must be the actual game date, YYYY-MM-DD.') from None
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise ValueError('Use YYYY-MM-DD for the game date.')
    aliases = {'points': 'pts', 'rebounds': 'reb', 'assists': 'ast', 'threes': 'tpm',
               '3pm': 'tpm', '3-pointers': 'tpm', 'steals': 'stl', 'blocks': 'blk',
               'turnovers': 'tov', 'points+rebounds+assists': 'pra',
               'points+rebounds': 'pr', 'points+assists': 'pa', 'rebounds+assists': 'ra',
               'offensiverebounds': 'oreb', 'defensiverebounds': 'dreb',
               'fgattempted': 'fga', 'fieldgoalsattempted': 'fga'}
    legs = []
    for row in lines[1:]:
        parts = [part.strip() for part in row.split('|')]
        if len(parts) not in {4, 5}:
            raise ValueError('Each row needs player | statistic | more/less | line, with optional | TEAM.')
        player, stat, side, line = parts[:4]
        key = stat.lower().replace(' ', '')
        normalized_stat = aliases.get(key, key)
        if not player:
            raise ValueError('Each row needs a player name.')
        if side.lower() not in {'more', 'less', 'over', 'under'}:
            raise ValueError('Choose more or less for each line.')
        try:
            numeric_line = float(line)
        except ValueError:
            raise ValueError('Each line must be a number, such as 24.5.') from None
        legs.append({'player': player, 'stat': normalized_stat, 'side': side.lower(),
                     'line': numeric_line, 'date': date, 'team': parts[4].upper() if len(parts) == 5 else 'NYK'})
    if not 1 <= len(legs) <= 100:
        raise ValueError('Use 1–100 board lines.')
    return legs


class TelegramBot:
    def __init__(self, store, answer_callback, *, token=None, allowed_user_ids=None, client=None,
                 model_status_callback=None, self_test_callback=None, research_refresh_callback=None,
                 answer_preferences_callback=None, manual_refresh_callback=None,
                 manual_refresh_status_callback=None, self_test_status_callback=None,
                 prizepicks_feed=None):
        self.store = store
        self.answer_callback = answer_callback
        self.answer_preferences_callback = answer_preferences_callback
        self.manual_refresh_callback = manual_refresh_callback
        self.manual_refresh_status_callback = manual_refresh_status_callback
        self._database_health = {'phase': 'ready'}
        self._database_waiters = 0
        self._shutting_down = False
        self._dataset_refresh_tasks = set()
        self._dataset_refresh_pending_results = {}
        self._self_test_tasks = set()
        self._self_test_pending_results = {}
        self.model_status_callback = model_status_callback
        self.self_test_callback = self_test_callback
        self.self_test_status_callback = self_test_status_callback
        self.research_refresh_callback = research_refresh_callback
        self.prizepicks_feed = prizepicks_feed
        self._photo_locks = {}
        self._photo_semaphore = asyncio.Semaphore(1)
        self._album_batches = {}
        self._album_tasks = set()
        self._photo_progress = {}
        self._photo_recent = []
        self._delivery_lock = asyncio.Lock()
        self._known_deliveries = {}
        self.token = token if token is not None else os.getenv('TELEGRAM_BOT_TOKEN', '')
        configured = (os.getenv('TELEGRAM_ALLOWED_USER_IDS') or
                      os.getenv('TELEGRAM_ALLOWED_USER_ID', ''))
        if allowed_user_ids is None:
            allowed_user_ids = [int(v.strip()) for v in configured.split(',')
                                if v.strip().isdigit() and int(v.strip()) > 0]
        self.allowed_user_ids = {int(v) for v in allowed_user_ids if str(v).isdigit() and int(v) > 0}
        self.enabled = bool(self.token and not self.token.startswith('REPLACE_') and self.allowed_user_ids)
        self.database_path = str(getattr(store, 'database_path', getattr(store, 'path', '')))
        if not self.database_path:
            raise ValueError('Telegram requires a persistent master database path.')
        self.client = client
        self._owns_client = False
        self.connected = False
        self._initialize_database()

    def _db(self, *, busy_timeout_ms=30000):
        db = sqlite3.connect(self.database_path, timeout=busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=' + str(int(busy_timeout_ms)))
        return db

    async def _retry_db(self, operation, *args, **kwargs):
        """Retry fresh idempotent transactions off-loop only for BUSY/LOCKED."""
        delay, waiting = .05, False
        try:
            while True:
                try:
                    return await asyncio.to_thread(operation, *args, **kwargs)
                except sqlite3.Error as error:
                    transient = sqlite_contention(error)
                    self._database_health = {'phase': 'waiting_for_writer' if transient else 'failed',
                                             'checked_at': utcnow(), **sqlite_diagnostic(error)}
                    if not transient:
                        raise
                    if not waiting:
                        waiting = True
                        self._database_waiters += 1
                    await asyncio.sleep(delay)
                    delay = min(1, delay * 2)
        finally:
            if waiting:
                self._database_waiters -= 1
                if self._database_waiters == 0 and self._database_health.get('phase') == 'waiting_for_writer':
                    self._database_health = {**self._database_health, 'phase': 'ready', 'recovered_at': utcnow()}

    def _transport_sql(self, sql, values=(), *, fetch=None):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                cursor = db.execute(sql, values)
                if fetch == 'one':
                    row = cursor.fetchone()
                    return dict(row) if row is not None else None
                if fetch == 'all':
                    return [dict(row) for row in cursor.fetchall()]
                return cursor.rowcount
        finally:
            db.close()

    def _transport_state(self, name, default=''):
        row = self._transport_sql('SELECT value FROM telegram_state WHERE name=?', (name,), fetch='one')
        return row['value'] if row else default

    def _transport_states(self, values):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.executemany('INSERT INTO telegram_state(name,value) VALUES (?,?) '
                               'ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                               [(name, str(value)) for name, value in values.items()])
        finally:
            db.close()

    def _initialize_database(self):
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS telegram_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_updates (
                    update_id INTEGER PRIMARY KEY, chat_id INTEGER,
                    status TEXT NOT NULL, received_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT UNIQUE NOT NULL, chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS telegram_drafts (
                    id TEXT PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                    chat_id INTEGER NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_conversation (
                    chat_id INTEGER NOT NULL,update_id INTEGER NOT NULL,
                    question TEXT NOT NULL,answer TEXT,created_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,update_id)
                );
                CREATE TABLE IF NOT EXISTS telegram_dataset_jobs (
                    id TEXT PRIMARY KEY,update_id INTEGER UNIQUE NOT NULL,chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,result_text TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS telegram_dataset_one_active
                    ON telegram_dataset_jobs((1)) WHERE status='running';
                CREATE TABLE IF NOT EXISTS telegram_selftest_jobs (
                    id TEXT PRIMARY KEY,update_id INTEGER UNIQUE NOT NULL,chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,result_text TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS telegram_selftest_one_active
                    ON telegram_selftest_jobs((1)) WHERE status='running';
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(telegram_outbox)')}
            if 'expires_at' not in columns:
                db.execute('ALTER TABLE telegram_outbox ADD COLUMN expires_at REAL')
            from gameday_digest import alert_mode
            if alert_mode() != 'instant':
                # Do not replay previously queued headline/health alerts on upgrade.
                db.execute("UPDATE telegram_outbox SET status='cancelled' WHERE status='pending' "
                           "AND (event_key LIKE 'news:%' OR event_key LIKE 'feed-health:%')")

    def _conversation(self, chat_id):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        with self._db() as db:
            rows = db.execute('SELECT question,answer FROM telegram_conversation WHERE chat_id=? '
                              'AND created_at>=? ORDER BY update_id DESC LIMIT 12', (chat_id, cutoff)).fetchall()
        history = []
        for row in reversed(rows):
            history.append({'role': 'user', 'content': row['question']})
            if row['answer']:
                history.append({'role': 'assistant', 'content': row['answer']})
        return history[-24:]

    async def _handle_ordered_message(self, message, update_id):
        """A quick correction waits for the preceding answer in the same chat.

        Health/clear commands remain responsive. Album uploads have their own
        collector and must not wait behind one another on this dialogue lock.
        """
        text = str(message.get('text') or '').strip()
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text else ''
        conversational = bool(text and (not text.startswith('/') or command == '/picks'))
        if not conversational or message.get('photo') or message.get('document'):
            return await self._handle_message(message, update_id)
        if not hasattr(self, '_conversation_locks'):
            self._conversation_locks = {}
        lock = self._conversation_locks.setdefault(message['chat']['id'], asyncio.Lock())
        async with lock:
            return await self._handle_message(message, update_id)

    def _remember(self, chat_id, update_id, question, answer=None):
        with self._db() as db:
            reset = db.execute('SELECT value FROM telegram_state WHERE name=?',
                               (f'conversation_reset_update:{chat_id}',)).fetchone()
            if reset and update_id <= int(reset['value']):
                return
            if answer is None:
                inserted = db.execute('INSERT OR IGNORE INTO telegram_conversation VALUES (?,?,?,?,?)',
                           (chat_id, update_id, question[:3000], None, utcnow())).rowcount
                if inserted:
                    # Persist user lineup choices in the same transaction as the
                    # user turn. An answer completion never writes preferences.
                    from conversation import validated_lineup_preferences
                    key = f'lineup_preferences:{chat_id}'
                    saved = db.execute('SELECT value FROM telegram_state WHERE name=?', (key,)).fetchone()
                    try:
                        prior = validated_lineup_preferences(json.loads(saved['value'])) if saved else {}
                    except (ValueError, TypeError):
                        prior = {}
                    # Adopt still-recent explicit user choices once when an
                    # existing deployment gains durable preference storage.
                    # Answers, player slots and offer values are never migrated.
                    if saved is None:
                        cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
                        recent = db.execute('SELECT question FROM telegram_conversation WHERE chat_id=? '
                                            'AND created_at>=? ORDER BY update_id LIMIT 13', (chat_id, cutoff))
                        for row in recent:
                            prior.update(self._user_preference_changes(row['question'], prior))
                    changes = self._user_preference_changes(question, prior)
                    if changes or saved is None and prior:
                        preferences = validated_lineup_preferences({**prior, **changes})
                        db.execute('INSERT INTO telegram_state(name,value) VALUES (?,?) '
                                   'ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                                   (key, json.dumps(preferences, sort_keys=True)))
            else:
                # A completed older answer must not resurrect a conversation that
                # was cleared, or a question already removed by the history limit.
                db.execute('UPDATE telegram_conversation SET answer=? WHERE chat_id=? AND update_id=?',
                           (answer[:6500], chat_id, update_id))
            db.execute('DELETE FROM telegram_conversation WHERE chat_id=? AND update_id NOT IN '
                       '(SELECT update_id FROM telegram_conversation WHERE chat_id=? '
                       'ORDER BY update_id DESC LIMIT 12)', (chat_id, chat_id))

    @staticmethod
    def _user_preference_changes(question, prior):
        from conversation import lineup_preference_updates
        changes = lineup_preference_updates(question[:3000])
        if re.match(r'^/picks(?:@\w+)?(?:\s|$)', question, re.I):
            try:
                request = parse_picks_request(question, default_count=prior.get('pick_count', 4))
            except ValueError:
                return {}
            if request is not None:
                changes['pick_count'] = request['count']
        return changes

    def _clear_conversation(self, chat_id, update_id):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                key = f'conversation_reset_update:{chat_id}'
                old = db.execute('SELECT value FROM telegram_state WHERE name=?', (key,)).fetchone()
                if old and int(old['value']) > update_id:
                    return
                db.execute('DELETE FROM telegram_conversation WHERE chat_id=?', (chat_id,))
                db.execute('DELETE FROM telegram_state WHERE name=?', (f'lineup_preferences:{chat_id}',))
                db.execute('INSERT INTO telegram_state(name,value) VALUES (?,?) '
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (key, str(update_id)))
        finally:
            db.close()

    def _lineup_preferences(self, chat_id):
        """Private durable user choices; never player focus or source evidence."""
        from conversation import validated_lineup_preferences
        try:
            return validated_lineup_preferences(json.loads(self._get_state(f'lineup_preferences:{chat_id}', '{}')))
        except (ValueError, TypeError):
            return {}

    def _get_state(self, name, default=''):
        with self._db() as db:
            row = db.execute('SELECT value FROM telegram_state WHERE name=?', (name,)).fetchone()
        return row['value'] if row else default

    def _set_state(self, name, value):
        with self._db() as db:
            db.execute('INSERT INTO telegram_state(name,value) VALUES (?,?) '
                       'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (name, str(value)))

    def _reset_screenshots(self, chat_id, update_id):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            key = f'screenshot_reset_update:{chat_id}'
            row = db.execute('SELECT value FROM telegram_state WHERE name=?', (key,)).fetchone()
            if row and int(row['value']) > update_id:
                return
            for name, value in ((key, str(update_id)), (f'screenshot_reset:{chat_id}', utcnow())):
                db.execute('INSERT INTO telegram_state VALUES (?,?) ON CONFLICT(name) '
                           'DO UPDATE SET value=excluded.value', (name, value))

    def status(self):
        with self._db() as db:
            pending = db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status='pending'").fetchone()[0]
        return {'enabled': self.enabled, 'connected': self.connected,
                'allowed_user_count': len(self.allowed_user_ids),
                'pending_messages': pending,
                'last_poll_success': self._get_state('last_poll_success') or None,
                'last_delivery': self._get_state('last_delivery') or None,
                'last_error': self._get_state('last_delivery_error') or self._get_state('last_error') or None,
                'last_poll_error': self._get_state('last_error') or None,
                'last_delivery_error': self._get_state('last_delivery_error') or None,
                'offset': int(self._get_state('offset', '0')), 'database': dict(self._database_health),
                'uploads': self._photo_progress_status()}

    def _photo_progress_status(self):
        """Bounded operational metadata only; never expose upload/chat contents."""
        current = time.monotonic()
        active = [dict(item) for item in tuple(self._photo_progress.values())]
        jobs = sorted(active, key=lambda item: item['clock'], reverse=True)
        jobs.extend(reversed(self._photo_recent))
        return {'active_count': len(active), 'jobs': [
            {key: value for key, value in item.items() if key != 'clock'} |
            {'elapsed_seconds': round(max(0, current-item['clock']) if item['state'] == 'running'
                                      else item['elapsed_seconds'], 1)}
            for item in jobs[:8]]}

    def _photo_stage(self, chat_id, phase, *, image_number=None):
        job = self._photo_progress.get(chat_id)
        if job is None:
            return
        labels = {'reading': 'Reading screenshots', 'matching': 'Matching saved players and game',
                  'uploaded_research': 'Comparing uploaded lines',
                  'master_targets': 'Calculating own master targets'}
        job['phase'] = phase
        job['stage'] = labels[phase]
        if image_number is not None:
            job['image_number'] = image_number
        if phase == 'reading':
            job['stage'] += f" {job.get('image_number', 1)}/{job['image_count']}"
        log.info('Screenshot job phase=%s elapsed=%.1fs', phase, time.monotonic()-job['clock'])

    def _authorized(self, message):
        sender = message.get('from') or {}
        chat = message.get('chat') or {}
        return (chat.get('type') == 'private' and sender.get('id') in self.allowed_user_ids
                and chat.get('id') == sender.get('id') and not sender.get('is_bot', False))

    def _queue_in_db(self, db, chat_id, text, event_key, expires_at=None):
        if int(chat_id) not in self.allowed_user_ids:
            return
        for number, part in enumerate(chunks(text)):
            db.execute('INSERT OR IGNORE INTO telegram_outbox '
                       '(event_key,chat_id,text,created_at,expires_at) VALUES (?,?,?,?,?)',
                       (f'{event_key}:{chat_id}:{number}', int(chat_id), part, utcnow(), expires_at))

    def _queue(self, chat_id, text, event_key, expires_at=None):
        with self._db() as db:
            self._queue_in_db(db, chat_id, text, event_key, expires_at)

    def _reserve_dataset_refresh(self, chat_id, update_id):
        job_id = uuid.uuid4().hex
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                active = db.execute("SELECT id,chat_id FROM telegram_dataset_jobs WHERE status='running'").fetchone()
                recovered = None
                if active:
                    pending = self._dataset_refresh_pending_results.get(active['id'])
                    if pending is None:
                        return 'A dataset refresh is already in progress. Use /refresh status for its stage and elapsed time.', None, None
                    # A completed task whose result write failed cannot strand the
                    # busy marker. Retry that exact result atomically before a new
                    # explicit request starts; never replay its data imports.
                    saved_text, saved_status = pending
                    self._queue_in_db(db, active['chat_id'], saved_text, 'dataset-refresh-result:' + active['id'])
                    db.execute('UPDATE telegram_dataset_jobs SET status=?,completed_at=?,result_text=? WHERE id=?',
                               (saved_status, utcnow(), saved_text[:10000], active['id']))
                    recovered = active['id']
                db.execute('INSERT INTO telegram_dataset_jobs VALUES (?,?,?,?,?,?,?)',
                           (job_id, update_id, chat_id, 'running', utcnow(), None, None))
                self._queue_in_db(db, chat_id, 'Checking dataset updates… Large Excel rebuilds can take several minutes. Use /refresh status for progress; I’ll send the result when the checks finish.',
                                  'dataset-refresh-start:' + job_id)
        finally:
            db.close()
        return None, job_id, recovered

    async def _start_dataset_refresh(self, chat_id, update_id):
        if self._shutting_down:
            return 'The bot is shutting down. Please try /refresh after it restarts.'
        if self.manual_refresh_callback is None:
            return 'Dataset refresh is not configured for this deployment. /status shows the saved update checks.'
        response, job_id, recovered = await self._retry_db(self._reserve_dataset_refresh, chat_id, update_id)
        if response is not None:
            return response
        if recovered is not None:
            self._dataset_refresh_pending_results.pop(recovered, None)
        task = asyncio.create_task(self._run_dataset_refresh(job_id), name='dataset-refresh:' + job_id)
        self._dataset_refresh_tasks.add(task)
        task.add_done_callback(self._dataset_refresh_finished)
        return None

    def _read_dataset_refresh_status(self):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            job = db.execute("SELECT id,status,created_at,completed_at,result_text FROM telegram_dataset_jobs "
                             "ORDER BY (status='running') DESC,rowid DESC LIMIT 1").fetchone()
            checkpoint = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone():
                row = db.execute("SELECT value_json FROM checkpoints WHERE name='dataset_update_status'").fetchone()
                if row:
                    try: checkpoint = json.loads(row['value_json'])
                    except (ValueError, TypeError): pass
            return dict(job) if job else None, checkpoint if isinstance(checkpoint, dict) else {}
        finally:
            db.close()

    async def _dataset_refresh_status(self):
        job, progress = await self._retry_db(self._read_dataset_refresh_status)
        if self.manual_refresh_status_callback is not None:
            value = await self.manual_refresh_status_callback()
            if isinstance(value, dict): progress = value
        if not job:
            return 'No dataset refresh is running. Use /refresh to check for updates.'
        pending_result = self._dataset_refresh_pending_results.get(job['id'])
        if pending_result is not None:
            return ('The refresh check finished, but saving its result is waiting for the database. '
                    'Use /refresh to retry saving it.\n' + str(pending_result[0])[:1800])
        running = job['status'] == 'running'
        lines = ['Dataset refresh is running.' if running else 'Last dataset refresh: ' + job['status'] + '.']
        if running:
            detail = progress.get('detail') or progress.get('stage')
            if isinstance(detail, str) and detail.strip(): lines.append('Stage: ' + detail[:200] + '.')
            try:
                started = datetime.fromisoformat(job['created_at'].replace('Z', '+00:00'))
                seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
                lines.append(f'Elapsed: {seconds // 60}m {seconds % 60}s.')
            except (ValueError, TypeError): pass
            lines.append('Large Excel rebuilds can take several minutes. The result will arrive when checks finish.')
        elif job.get('result_text'):
            lines.append(str(job['result_text'])[:1800])
        return '\n'.join(lines)

    def _reserve_self_test(self, chat_id, update_id):
        job_id = uuid.uuid4().hex
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM telegram_selftest_jobs WHERE update_id=?', (update_id,)).fetchone():
                    return 'This self-test request was already received. Use /selftest status for its report.', None, None
                active = db.execute("SELECT id,chat_id,update_id FROM telegram_selftest_jobs WHERE status='running'").fetchone()
                if active:
                    pending = self._self_test_pending_results.get(active['id'])
                    if pending is None:
                        return 'A self-test is already running. Use /selftest status for its stage and elapsed time.', None, None
                    text, status = pending
                    self._queue_in_db(db, active['chat_id'], text, 'selftest-result:' + active['id'])
                    db.execute('UPDATE telegram_selftest_jobs SET status=?,completed_at=?,result_text=? WHERE id=?',
                               (status, utcnow(), text, active['id']))
                    db.execute("UPDATE telegram_updates SET status='done',completed_at=? WHERE update_id=? AND status='processing'",
                               (utcnow(), active['update_id']))
                    return 'The previous self-test report is queued. The checks were not repeated. Use /selftest status to read it.', None, active['id']
                db.execute('INSERT INTO telegram_selftest_jobs VALUES (?,?,?,?,?,?,?)',
                           (job_id, update_id, chat_id, 'running', utcnow(), None, None))
                self._queue_in_db(db, chat_id, 'Self-test started in the background. Checks can take several minutes under load. '
                    'I’ll send the report when they finish. Use /selftest status for progress.', 'selftest-start:' + job_id)
        finally:
            db.close()
        return None, job_id, None

    async def _start_self_test(self, chat_id, update_id):
        if chat_id not in self.allowed_user_ids:
            return 'Self-tests are available only in the configured private chat.'
        if self._shutting_down:
            return 'The bot is shutting down. Try /selftest after it restarts.'
        if self.self_test_callback is None:
            return 'Self-test is not configured for this deployment. Use /status and /ai_status.'
        response, job_id, recovered = await self._retry_db(self._reserve_self_test, chat_id, update_id)
        if recovered:
            self._self_test_pending_results.pop(recovered, None)
        if response is not None:
            return response
        task = asyncio.create_task(self._run_self_test(job_id), name='self-test:' + job_id)
        self._self_test_tasks.add(task)
        task.add_done_callback(self._self_test_finished)
        return None

    def _finish_self_test(self, job_id, text, status):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT chat_id,update_id FROM telegram_selftest_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
                if row is None:
                    return
                self._queue_in_db(db, row['chat_id'], text, 'selftest-result:' + job_id)
                db.execute('UPDATE telegram_selftest_jobs SET status=?,completed_at=?,result_text=? WHERE id=?',
                           (status, utcnow(), text, job_id))
                db.execute("UPDATE telegram_updates SET status='done',completed_at=? WHERE update_id=? AND status='processing'",
                           (utcnow(), row['update_id']))
        finally:
            db.close()

    async def _run_self_test(self, job_id):
        cancelled = False
        try:
            text = await self.self_test_callback()
            if not isinstance(text, str) or not text.strip():
                raise TypeError('Unusable self-test report')
            text = text[:16000]
            failed = re.match(r'^Self-test:\s*\d+ passed,\s*(\d+) failed\b', text)
            status = ('busy' if text.startswith('A self-test is already running') else
                      'failed' if failed and int(failed[1]) else 'completed')
        except asyncio.CancelledError:
            cancelled = True
            status = 'interrupted'
            text = 'The self-test was interrupted by application shutdown. No checks were replayed. Use /selftest after restart.'
        except Exception as error:
            from release_checks import safe_check_category
            status = 'failed'
            text = ('Self-test could not finish (' + safe_check_category(error) + '). '
                    'Check chatbot logs for the stage. No sample entries were added to your master.')
        self._self_test_pending_results[job_id] = (text, status)

        async def persist():
            for attempt in range(3):
                try:
                    await self._retry_db(self._finish_self_test, job_id, text, status)
                    self._self_test_pending_results.pop(job_id, None)
                    return
                except sqlite3.Error:
                    raise  # BUSY/LOCKED already retry; permanent storage faults remain visible.
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(.1 * (2 ** attempt))

        # Once a report exists, shutdown must not replace it with interruption
        # while its atomic outbox/status transaction is finishing.
        from dataset_update import _complete_callback
        await _complete_callback(persist)
        if cancelled:
            raise asyncio.CancelledError

    def _self_test_finished(self, task):
        self._self_test_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning('Self-test report is retained in memory but could not be saved; /selftest status can show it.')

    def _read_self_test_status(self, chat_id):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            row = db.execute('SELECT * FROM telegram_selftest_jobs WHERE chat_id=? '
                "ORDER BY (status='running') DESC,rowid DESC LIMIT 1", (chat_id,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    async def _self_test_status(self, chat_id):
        job = await self._retry_db(self._read_self_test_status, chat_id)
        progress = {}
        if self.self_test_status_callback is not None:
            try:
                value = await asyncio.wait_for(self.self_test_status_callback(), timeout=3)
                if isinstance(value, dict): progress = value
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('Self-test progress callback unavailable; saved job status remains readable.')
        if not job:
            heading = 'Self-checks are running automatically.' if progress.get('active') else 'No manual self-test has run yet.'
            lines = [heading]
        else:
            pending = self._self_test_pending_results.get(job['id'])
            if pending:
                return 'Self-test checks finished; saving the report is waiting for the database. Use /selftest to retry saving it.\n\n' + pending[0]
            lines = ['Self-test is running in the background.' if job['status']=='running' else 'Last self-test: ' + job['status'] + '.']
            if job['status'] != 'running' and job.get('result_text'):
                return '\n'.join(lines) + '\n\n' + job['result_text']
        if progress.get('stage'):
            lines.append('Stage: ' + str(progress['stage']).replace('\n',' ')[:140] + '.')
        seconds = None if job and job['status']=='running' else progress.get('elapsed_seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            try:
                seconds = (datetime.now(timezone.utc)-datetime.fromisoformat(job['created_at'].replace('Z','+00:00'))).total_seconds() if job else 0
            except (ValueError, TypeError): seconds = 0
        seconds = max(0, int(seconds))
        lines.append(f'Elapsed: {seconds//60}m {seconds%60}s.')
        return '\n'.join(lines)

    def _recover_self_tests(self):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                owned = {task.get_name().split(':',1)[1] for task in list(self._self_test_tasks)
                         if not task.done() and task.get_name().startswith('self-test:')}
                owned.update(self._self_test_pending_results)
                rows = db.execute("SELECT id,chat_id,update_id FROM telegram_selftest_jobs WHERE status='running'").fetchall()
                for row in rows:
                    if row['id'] in owned:
                        continue
                    text = 'A self-test was interrupted by an application restart. No checks were replayed. Use /selftest to run a new check.'
                    self._queue_in_db(db, row['chat_id'], text, 'selftest-result:' + row['id'])
                    db.execute("UPDATE telegram_selftest_jobs SET status='interrupted',completed_at=?,result_text=? WHERE id=?",
                               (utcnow(), text, row['id']))
                    db.execute("UPDATE telegram_updates SET status='interrupted',completed_at=? WHERE update_id=? AND status='processing'",
                               (utcnow(), row['update_id']))
        finally:
            db.close()

    async def shutdown(self):
        """Only application shutdown owns cancellation of independent data jobs."""
        self._shutting_down = True
        tasks = [*self._dataset_refresh_tasks, *self._self_test_tasks]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self._dataset_refresh_tasks.clear()
        self._self_test_tasks.clear()

    def _dataset_refresh_finished(self, task):
        self._dataset_refresh_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning('Dataset refresh result could not be saved; a later request or startup will recover it.')

    def _finish_dataset_refresh(self, job_id, text, status):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT chat_id FROM telegram_dataset_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
            if row is None:
                return
            self._queue_in_db(db, row['chat_id'], text, 'dataset-refresh-result:' + job_id)
            db.execute('UPDATE telegram_dataset_jobs SET status=?,completed_at=?,result_text=? WHERE id=?',
                       (status, utcnow(), str(text)[:10000], job_id))

    async def _run_dataset_refresh(self, job_id):
        cancelled = False
        try:
            report = await self.manual_refresh_callback()
            if isinstance(report, str):
                text = report
            else:
                from dataset_update import format_update_report
                text = format_update_report(report)
            status = 'completed'
        except asyncio.CancelledError:
            cancelled = True
            status = 'interrupted'
            text = ('The dataset refresh was interrupted. Existing saved data remains available. '
                    'Use /refresh to check again.')
        except Exception:
            status = 'failed'
            text = ('The dataset refresh could not finish. Existing saved data remains available. '
                    'Check /status and try /refresh again.')
        for attempt in range(3):
            try:
                await asyncio.to_thread(self._finish_dataset_refresh, job_id, text, status)
                break
            except Exception:
                if attempt == 2:
                    self._dataset_refresh_pending_results[job_id] = (text, status)
                    raise
                await asyncio.sleep(.1 * (2 ** attempt))
        if cancelled:
            raise asyncio.CancelledError

    def _recover_dataset_refreshes(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,chat_id,update_id FROM telegram_dataset_jobs WHERE status='running'").fetchall()
            owned = {task.get_name().split(':', 1)[1] for task in list(self._dataset_refresh_tasks)
                     if not task.done() and task.get_name().startswith('dataset-refresh:')}
            owned.update(self._dataset_refresh_pending_results)
            for row in rows:
                if row['id'] in owned:
                    continue
                text = 'A dataset refresh was interrupted by a restart. Existing saved data remains available. Use /refresh to check again.'
                self._queue_in_db(db, row['chat_id'], text, 'dataset-refresh-result:' + row['id'])
                db.execute("UPDATE telegram_dataset_jobs SET status='interrupted',completed_at=?,result_text=? WHERE id=?",
                           (utcnow(), text, row['id']))
                db.execute("UPDATE telegram_updates SET status='interrupted',completed_at=? "
                           "WHERE update_id=? AND status='processing'", (utcnow(), row['update_id']))

    async def notify_dataset_refresh(self, text, report):
        """Queue the daily result once per Eastern run date, using the outbox."""
        day = str(report.get('notification_day') or '') if isinstance(report, dict) else ''
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            return False
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            return False
        return await self.notify(text, event_key='dataset-daily:' + day)

    async def notify(self, text, event_key=None, expires_at=None):
        """Queue a private alert. Delivery retries are handled by run()."""
        if not self.enabled:
            return False
        key = event_key or 'notification:' + uuid.uuid4().hex
        for user_id in self.allowed_user_ids:
            await self._retry_db(self._queue, user_id, text, key, expires_at=expires_at)
        return True

    async def _api(self, method, payload):
        if not self.client:
            raise RuntimeError('Telegram client is not running.')
        try:
            response = await self.client.post(f'https://api.telegram.org/bot{self.token}/{method}',
                                              json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Telegram request failed; retrying automatically.') from None
        if not isinstance(data, dict):
            raise RuntimeError('Telegram returned an invalid response; retrying automatically.')
        if response.status_code != 200 or not data.get('ok'):
            # Do not expose provider errors or URLs which might contain credentials.
            code = data.get('error_code', response.status_code)
            if type(code) is not int or not 100 <= code <= 599:
                code = response.status_code
            parameters = data.get('parameters')
            retry_after = parameters.get('retry_after') if isinstance(parameters, dict) else None
            if type(retry_after) not in (int, float) or not math.isfinite(retry_after) or retry_after < 0:
                retry_after = None
            raise TelegramAPIError(code, retry_after if code == 429 else None)
        return data.get('result')

    async def _typing(self, chat_id):
        """Temporary Telegram activity only, scoped to a pending private answer."""
        if not self.enabled or self.client is None or chat_id not in self.allowed_user_ids:
            return
        while True:
            try:
                await self._api('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
            except asyncio.CancelledError:
                raise
            except Exception:
                # Best effort: a typing failure must never interrupt the answer,
                # queue a permanent notification, or expose Telegram credentials.
                pass
            await asyncio.sleep(TYPING_INTERVAL_SECONDS)

    async def flush_outbox(self):
        if not self.enabled:
            return
        # One sender per bot instance prevents overlapping flushes from reading
        # and delivering the same pending rows before either marks them sent.
        async with self._delivery_lock:
            await self._flush_outbox()

    async def _flush_outbox(self):
        await self._retry_db(self._transport_sql, "UPDATE telegram_outbox SET status='expired' WHERE status='pending' "
                             'AND expires_at IS NOT NULL AND expires_at<=?', (time.time(),))
        rows = await self._retry_db(self._transport_sql, "SELECT * FROM telegram_outbox WHERE status='pending' "
                                   'AND next_attempt<=? ORDER BY id LIMIT 20', (time.time(),), fetch='all')
        if float(await self._retry_db(self._transport_state, 'delivery_retry_at', '0')) > time.time():
            return
        for row in rows:
            known_delivery = self._known_deliveries.get(row['id'])
            if known_delivery is not None:
                # Polling may restart while a successful send waits for its DB
                # mark. Preserve that known result on this bot instance and
                # finish only the mark, never send the same reply again.
                await self._retry_db(self._transport_sql,
                    "UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE id=?", (known_delivery, row['id']))
                self._known_deliveries.pop(row['id'], None)
                await self._retry_db(self._transport_states, {'last_delivery': known_delivery})
                continue
            if row['expires_at'] is not None and row['expires_at'] <= time.time():
                await self._retry_db(self._transport_sql, "UPDATE telegram_outbox SET status='expired' WHERE id=?", (row['id'],))
                continue
            if row['chat_id'] not in self.allowed_user_ids:
                await self._retry_db(self._transport_sql, "UPDATE telegram_outbox SET status='blocked' WHERE id=?", (row['id'],))
                continue
            try:
                await self._api('sendMessage', {'chat_id': row['chat_id'], 'text': row['text'],
                                               'link_preview_options': {'is_disabled': True}})
            except RuntimeError as error:
                attempts = row['attempts'] + 1
                retry_delay = min(900, 5 * 2 ** min(attempts, 8))
                if isinstance(error, TelegramAPIError) and error.retry_after is not None:
                    retry_delay = max(retry_delay, error.retry_after)
                    # Telegram rate limits can apply across outgoing messages.
                    # Retain the cooldown on disk so a restart will honor it.
                    await self._retry_db(self._transport_states, {'delivery_retry_at': time.time() + retry_delay})
                await self._retry_db(self._transport_sql, 'UPDATE telegram_outbox SET attempts=?,next_attempt=? WHERE id=?',
                                     (attempts, time.time() + retry_delay, row['id']))
                await self._retry_db(self._transport_states, {'last_delivery_error': 'Telegram delivery failed; queued for retry.'})
                return
            delivered_at = utcnow()
            self._known_deliveries[row['id']] = delivered_at
            await self._retry_db(self._transport_sql, "UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE id=?",
                                 (delivered_at, row['id']))
            self._known_deliveries.pop(row['id'], None)
            await self._retry_db(self._transport_states, {'last_delivery': delivered_at})
        failed_pending = await self._retry_db(self._transport_sql, "SELECT 1 FROM telegram_outbox WHERE status='pending' "
                                             'AND attempts>0 LIMIT 1', fetch='one')
        if not failed_pending:
            await self._retry_db(self._transport_states, {'last_delivery_error': '', 'delivery_retry_at': '0'})

    async def _download(self, file_id, declared_size=0, *, max_bytes=MAX_UPLOAD):
        limit_message = (f'Upload is too large. The limit is {max_bytes // (1024 * 1024)} MB. '
                         'Larger master workbooks can be imported from the bot website.')
        if declared_size and int(declared_size) > max_bytes:
            raise ValueError(limit_message)
        metadata = await self._api('getFile', {'file_id': file_id})
        file_path = str(metadata.get('file_path', ''))
        if (not re.fullmatch(r'[A-Za-z0-9_./-]+', file_path)
                or '..' in file_path.split('/') or not file_path):
            raise RuntimeError('Telegram returned an invalid file path.')
        if metadata.get('file_size', 0) > max_bytes:
            raise ValueError(limit_message)
        payload = bytearray()
        try:
            async with self.client.stream('GET', f'https://api.telegram.org/file/bot{self.token}/{file_path}') as response:
                if response.status_code != 200:
                    raise RuntimeError('Could not download the Telegram upload.')
                async for part in response.aiter_bytes():
                    payload.extend(part)
                    if len(payload) > max_bytes:
                        raise ValueError(limit_message)
        except httpx.HTTPError:
            raise RuntimeError('Telegram file download failed; please try the upload again.') from None
        return bytes(payload)

    async def _board_saved_reply(self, board, chat_id=None):
        heading=f'PrizePicks board saved: {board["id"]}\n{len(board["legs"])} uploaded lines. No placed entry recorded.'
        if not callable(getattr(self.store,'connect',None)):
            return heading+'\n/analyze '+board['id']
        await self._refresh_board_news()
        import analysis_engine
        report=await asyncio.to_thread(analysis_engine.analyze_board,self.store,board['id'])
        return heading+'\n\n'+await self._shortlist_reply(report, chat_id)

    async def _refresh_board_news(self):
        if self.research_refresh_callback is not None:
            try:
                await asyncio.wait_for(self.research_refresh_callback(), timeout=25)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Existing feed timestamps remain visible; failed refreshes do
                # not get relabeled fresh and never block screenshot processing.
                log.warning('Board news refresh incomplete; using saved source timestamps.')
        if callable(getattr(self.store,'connect',None)):
            try:
                from assignment_refresh import refresh_assignments
                await asyncio.to_thread(refresh_assignments,self.store)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('Tentative matchup recheck incomplete; original source dates retained.')

    def _pick_constraints(self, chat_id):
        if chat_id is None:
            return {}
        preferences = self._lineup_preferences(chat_id)
        if not callable(getattr(self.store, 'connect', None)):
            return preferences
        from conversation import resolve_conversation
        turn = resolve_conversation(None, self.store, 'Use my lineup preferences.', self._conversation(chat_id),
                                    preferences=preferences)
        constraints = dict(turn.get('constraints', {}))
        count = turn.get('state', {}).get('slots', {}).get('pick_count')
        if type(count) is int and 1 <= count <= 6:
            constraints['pick_count'] = count
        return constraints

    async def _shortlist_reply(self, report, chat_id=None, *, upload_progress=False):
        if callable(getattr(self.store, 'connect', None)) and 'original_legs' in report:
            from board_shortlist import enrich_shortlist, format_shortlist, build_master_targets
            if upload_progress:
                self._photo_stage(chat_id, 'uploaded_research')
            constraints = await asyncio.to_thread(self._pick_constraints, chat_id)
            # Screenshot research targets at least four distinct-player picks.
            # Preserve larger requests; eligibility still decides what can rank.
            requested = constraints.get('pick_count', 4)
            requested = max(4, requested) if type(requested) is int else 4
            constraints = {**constraints, 'pick_count':requested}
            shortlist = await asyncio.to_thread(enrich_shortlist, self.store, report,
                count=requested, constraints=constraints)
            from pregame_archive import record_research
            archive_incomplete = False
            try:
                await asyncio.to_thread(record_research, self.store, shortlist,
                                        owner_key=f'telegram:{chat_id}', kind='board_shortlist', source_report=report)
            except Exception as error:
                archive_incomplete = True
                log.warning('Uploaded research archive incomplete (%s).', type(error).__name__)
            uploaded_text = 'From your uploaded lines\n\n' + format_shortlist(shortlist)
            try:
                from independent_picks import format_independent_picks
                if upload_progress:
                    self._photo_stage(chat_id, 'master_targets')
                targets = await asyncio.to_thread(build_master_targets, self.store, report,
                                                 count=requested, constraints=constraints)
                target_text = format_independent_picks(targets, compact=True, max_chars=1400)
                try:
                    await asyncio.to_thread(record_research, self.store, targets,
                                            owner_key=f'telegram:{chat_id}', kind='independent')
                except Exception as error:
                    archive_incomplete = True
                    log.warning('Independent research archive incomplete (%s).', type(error).__name__)
            except Exception as error:
                log.warning('Uploaded-board master targets incomplete (%s).', type(error).__name__)
                target_text = 'The separate master targets could not finish. /picks can retry; the uploaded-line analysis above is available.'
            dates = {str(leg.get('date') or '') for leg in report.get('original_legs', [])}
            date_heading = ''
            if len(dates) == 1:
                day = next(iter(dates))
                try:
                    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                        datetime.strptime(day, '%Y-%m-%d')
                        date_heading = 'Game date: ' + day + ' Eastern.\n\n'
                except ValueError:
                    pass
            answer = date_heading + uploaded_text + '\n\nMy own calculated targets from your master\n\n' + target_text
            if archive_incomplete:
                answer += '\n\nResearch history could not be saved; the analysis above is still available.'
            return answer
        import analysis_engine
        formatter = getattr(analysis_engine, 'format_analysis', None)
        return formatter(report) if formatter else self._compact(report)

    def _prizepicks_board(self, legs, game, chat_id):
        """Reuse an immutable owner-scoped source snapshot; never make an entry."""
        digest = hashlib.sha256(json.dumps({'game': game['id'], 'legs': legs},
                                           sort_keys=True, allow_nan=False).encode()).hexdigest()
        source = f'PrizePicks public feed; telegram:{chat_id}; game:{game["id"]}; snapshot:{digest}'
        with self.store.connect() as db:
            existing = db.execute('SELECT id FROM boards WHERE source=? ORDER BY created_at DESC LIMIT 1',
                                  (source,)).fetchone()
        return self.store.get_board(existing[0]) if existing else self.store.create_board(legs, source=source)

    @staticmethod
    def _prizepicks_comparison_legs(bundle, game):
        """Choose one exact source offer per market, retaining its confirmed sides."""
        from feeds import canonical_player, canonical_team
        from master import STAT_COMPONENTS
        teams = {canonical_team(game.get('home_team')), canonical_team(game.get('away_team'))}
        grouped = {}
        for leg in bundle.get('legs', [])[:2000]:
            if not isinstance(leg, dict):
                continue
            variant = leg.get('offer_variant')
            line = leg.get('line')
            if (leg.get('game_id') != game.get('id') or leg.get('date') != game.get('date')
                    or canonical_team(leg.get('team')) not in teams
                    or leg.get('side') not in {'more', 'less'} or leg.get('stat') not in STAT_COMPONENTS
                    or variant not in {'standard', 'goblin', 'demon'}
                    or type(line) not in (int, float) or not math.isfinite(line) or line < 0):
                continue
            market = (canonical_player(leg.get('player')), leg['stat'])
            identity = (str(leg.get('provider_projection_id') or leg.get('projection_id') or ''), variant, line)
            grouped.setdefault(market, {}).setdefault(identity, []).append(dict(leg))
        chosen = []
        order = {'standard': 0, 'goblin': 1, 'demon': 2}
        for market in sorted(grouped):
            offers = grouped[market]
            identity = min(offers, key=lambda value: (order[value[1]], value[0], value[2]))
            sides = {}
            for leg in offers[identity]:
                sides.setdefault(leg['side'], leg)
            chosen.append(list(sides.values()))
        # Keep the bounded analysis balanced when the feed lists one team first.
        queues = {team: [group for group in chosen if canonical_team(group[0]['team']) == team]
                  for team in sorted(teams)}
        result = []
        while any(queues.values()):
            for team in sorted(teams):
                if queues[team]:
                    group = queues[team].pop(0)
                    if len(result) + len(group) > 100:
                        return result
                    result.extend(group)
        return result

    async def _prizepicks_cached_reply(self, game, chat_id, *, count=4):
        """Use only this exact game's fresh public snapshot, without HTTP work."""
        if self.prizepicks_feed is None:
            return '', False
        if not isinstance(game, dict) or not game.get('id'):
            return 'PrizePicks lines were not attached because this request has no matching upcoming game.', False
        try:
            bundle = await asyncio.to_thread(self.prizepicks_feed.offers_for_game, game)
            if not isinstance(bundle, dict):
                raise ValueError('Invalid public feed cache.')
            status = bundle.get('status') or await asyncio.to_thread(self.prizepicks_feed.status)
            if (bundle.get('fresh') is not True or not isinstance(bundle.get('game'), dict)
                    or bundle['game'].get('id') != game['id']):
                return prizepicks_status_reply(status, compact=True), False
            offers = [row for row in bundle.get('offers', []) if isinstance(row, dict)
                      and row.get('game_id') == game['id'] and row.get('date') == game['date']]
            if not offers:
                return prizepicks_status_reply(status, compact=True), False
            def snapshot_key(value):
                fields = ('projection_id', 'player', 'team', 'date', 'game_id', 'stat', 'line',
                          'offer_variant', 'status', 'available_sides')
                rows = [{key: row.get(key) for key in fields} for row in value.get('offers', [])
                        if isinstance(row, dict)]
                return json.dumps(rows, sort_keys=True, allow_nan=False)
            original_key = snapshot_key(bundle)
            stamp = offers[0].get('retrieved_at') or status.get('last_success_at')
            lines = [f"Game date: {game['date']} Eastern.", "From PrizePicks' public feed",
                     f"{len(offers)} cached offer records for {game['home_team']} vs {game['away_team']}. "
                     f"Retrieved {_prizepicks_time(stamp)}."]
            legs = self._prizepicks_comparison_legs(bundle, game)
            if legs:
                from analysis_engine import analyze_board
                from board_shortlist import enrich_shortlist, format_shortlist
                board = await asyncio.to_thread(self._prizepicks_board, legs, game, chat_id)
                report = await asyncio.to_thread(analyze_board, self.store, board['id'])
                constraints = await asyncio.to_thread(self._pick_constraints, chat_id)
                shortlist = await asyncio.to_thread(enrich_shortlist, self.store, report,
                    count=count, constraints=constraints)
                from pregame_archive import record_research
                try:
                    await asyncio.to_thread(record_research, self.store, shortlist,
                        owner_key=f'telegram:{chat_id}', kind='board_shortlist', source_report=report)
                except Exception as error:
                    log.warning('Public PrizePicks research archive incomplete (%s).', type(error).__name__)
                lines.append(format_shortlist(shortlist).replace('No uploaded side', 'No fetched side'))
                lines.append(f'Compared {len(legs)} explicitly confirmed sides, using one source offer per player/statistic; '
                             'alternative offers remain in the saved feed snapshot. No entry placed.')
            else:
                from board_shortlist import MARKET_NAMES
                for offer in offers[:8]:
                    name = re.sub(r'\s+', ' ', str(offer.get('player') or 'Unknown player'))[:70]
                    metric = MARKET_NAMES.get(offer.get('stat'), 'unsupported statistic')
                    value = offer.get('line')
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        continue
                    variant = offer.get('offer_variant')
                    label = variant if variant in {'standard', 'goblin', 'demon'} else 'offer type unknown'
                    lines.append(f'• {name} · {value:g} {metric} ({label})')
                lines.append('These are fetched line values. The feed did not confirm usable More/Less selections; '
                             'the displayed sides and offer terms require verification. No side or payout was assumed.')
            current = await asyncio.to_thread(self.prizepicks_feed.offers_for_game, game)
            if (not isinstance(current, dict) or current.get('fresh') is not True
                    or not isinstance(current.get('game'), dict) or current['game'].get('id') != game['id']
                    or snapshot_key(current) != original_key):
                return ('PrizePicks lines changed or expired during comparison. Current offers were held; '
                        'the master targets remain available and /picks can check the refreshed cache.'), False
            return '\n\n'.join(lines), True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning('Public PrizePicks comparison unavailable (%s).', type(error).__name__)
            return 'The cached PrizePicks comparison could not finish. Master targets remain available; /pp_status shows the source check.', False

    async def _prizepicks_command(self, *, refresh=False, chat_id=None):
        if self.prizepicks_feed is None:
            return 'PrizePicks public feed is not configured in this deployment.'
        try:
            if refresh:
                status = await self.prizepicks_feed.refresh(force=True)
            else:
                status = await asyncio.to_thread(self.prizepicks_feed.status)
            text = prizepicks_status_reply(status)
            if refresh:
                bundle = await asyncio.to_thread(self.prizepicks_feed.offers_for_game)
                game = bundle.get('game') if isinstance(bundle, dict) else None
                if game:
                    offers, available = await self._prizepicks_cached_reply(game, chat_id)
                    if available:
                        text += '\n\n' + offers
            return text
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning('Public PrizePicks command unavailable (%s).', type(error).__name__)
            return 'The PrizePicks server check could not finish. Saved master targets remain available through /picks.'

    async def _independent_picks_reply(self, request, chat_id, original_text=None):
        """Read the saved master; regular workers own network/data refreshes."""
        from independent_picks import build_independent_picks, format_independent_picks
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            constraints = await asyncio.to_thread(self._pick_constraints, chat_id)
            if original_text is not None:
                request = parse_picks_request(original_text, default_count=constraints.get('pick_count', 4)) or request
            report = await asyncio.to_thread(build_independent_picks, self.store, **request,
                                             constraints=constraints, allow_unverified_timing=True)
            from pregame_archive import record_research
            await asyncio.to_thread(record_research, self.store, report,
                                    owner_key=f'telegram:{chat_id}', kind='independent')
            master_text = format_independent_picks(report)
            if self.prizepicks_feed is not None:
                offered_text, available = await self._prizepicks_cached_reply(
                    report.get('game'), chat_id, count=report.get('requested_count', 4))
                if available:
                    return offered_text + '\n\nMy own calculated targets from your master\n\n' + master_text
                if offered_text:
                    master_text += '\n\n' + offered_text
            return master_text
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)

    @staticmethod
    def _ocr(payload):
        executable = shutil.which('tesseract')
        if not executable:
            return None
        with tempfile.TemporaryDirectory(prefix='knicks-ocr-') as directory:
            image = Path(directory) / 'upload.image'
            image.write_bytes(payload)
            try:
                result = subprocess.run([executable, str(image), 'stdout', '--psm', '11'],
                                        capture_output=True, text=True, timeout=45, check=True)
            except (subprocess.SubprocessError, OSError):
                raise ValueError('The screenshot could not be read. Send a clearer image or paste the lines as text.') from None
        return result.stdout.strip()[:16000]

    async def _handle_photo(self, message, update_id):
        group = message.get('media_group_id')
        if not group:
            return await self._process_photos([(message, update_id)])
        key = (message['chat']['id'], str(group))
        batch = self._album_batches.get(key)
        if batch is None:
            batch = {'items': []}
            self._album_batches[key] = batch
            async def collect_album():
                await asyncio.sleep(1.5)
                self._album_batches.pop(key, None)
                return await self._process_photos(batch['items'])
            task = asyncio.create_task(collect_album())
            batch['task'] = task
            self._album_tasks.add(task)
            task.add_done_callback(self._album_tasks.discard)
        batch['items'].append((message, update_id))
        response = await asyncio.shield(batch['task'])
        # All album updates retain their own durable receipt, but only one
        # sends the combined result. Later fragments still merge via the DB.
        return response if update_id == max(item[1] for item in batch['items']) else None

    async def _process_photos(self, items):
        from screenshot_board import extract_screenshot
        from screenshot_intake import screenshot_date, roster_candidates, resolve_cards, save_extraction, combined_board
        chat_id = items[0][0]['chat']['id']
        lock = self._photo_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            typing = asyncio.create_task(self._typing(chat_id))
            job = {'state': 'running', 'phase': 'reading', 'stage': 'Reading screenshots',
                   'image_count': len(items), 'image_number': 1,
                   'started_at': utcnow(), 'clock': time.monotonic()}
            self._photo_progress[chat_id] = job
            outcome = 'completed'
            try:
                self._photo_stage(chat_id, 'reading', image_number=1)
                group = items[0][0].get('media_group_id')
                ack_key = 'photo-reading:' + (str(group) if group else str(items[0][1]))
                await self._retry_db(self._queue, chat_id,
                    f"Reading your {len(items)} screenshot(s). I’ll compare readable lines with the master, "
                    'then calculate my own targets. /status shows the current stage while I work.',
                    event_key=ack_key)
                now = datetime.now(timezone.utc)
                dates, newly_read = set(), 0
                problems = []
                captions = {date for message, _ in items for date in
                            re.findall(r'\b\d{4}-\d{2}-\d{2}\b', str(message.get('caption', '')))}
                inherited = next(iter(captions)) if len(captions) == 1 else None
                for image_number, (message, update_id) in enumerate(sorted(items, key=lambda item: item[1]), 1):
                    self._photo_stage(chat_id, 'reading', image_number=image_number)
                    caption = str(message.get('caption', '')).strip()
                    if caption.startswith('/importstats'):
                        problems.append('Statistics screenshots are not imported. Send a CSV/JSON file with /importstats.')
                        continue
                    if caption.split(maxsplit=1)[:1] == ['/newboard']:
                        self._reset_screenshots(chat_id, update_id-1)
                    dated = dict(message)
                    if inherited and not re.search(r'\b\d{4}-\d{2}-\d{2}\b', caption):
                        dated['caption'] = inherited
                    date, basis = screenshot_date(dated, now)
                    roster = await asyncio.to_thread(roster_candidates, self.store, date)
                    if not roster:
                        problems.append('The saved player dataset is not ready yet; resend after /status shows the workbook import.')
                        continue
                    photo = (max(message['photo'], key=lambda p: p.get('width', 0)*p.get('height', 0))
                             if message.get('photo') else message['document'])
                    try:
                        payload = await self._download(photo['file_id'], photo.get('file_size', 0))
                        async with self._photo_semaphore:
                            extraction = await asyncio.to_thread(extract_screenshot, payload, roster)
                    except (ValueError, RuntimeError, OSError):
                        problems.append('One image could not be read; resend a PNG/JPEG crop under 8 MB.')
                        continue
                    self._photo_stage(chat_id, 'matching')
                    legs, unresolved = await asyncio.to_thread(resolve_cards, self.store, extraction, date, roster)
                    await asyncio.to_thread(save_extraction, self.store, chat_id, update_id, date, basis,
                                            extraction, legs, unresolved, now)
                    newly_read += len({(leg['player'], leg['stat'], leg['line']) for leg in legs})
                    dates.add(date)
                    problems.extend(unresolved)
                if not newly_read:
                    outcome = 'failed'
                    details = '\n'.join('• '+str(value)[:160] for value in list(dict.fromkeys(problems))[:3])
                    return ('I couldn’t read a complete, supported card confidently enough to choose a pick.\n'
                            + (details+'\n' if details else '') +
                            'Send a closer crop with the player, full statistic label, line and More/Less buttons visible, '
                            'preferably as an image file. I kept unclear cards out; no entry was recorded.')
                messages = []
                self._photo_stage(chat_id, 'matching')
                await self._refresh_board_news()
                for date in sorted(dates):
                    bundle = await asyncio.to_thread(combined_board, self.store, chat_id, date, now,
                        self._get_state(f'screenshot_reset:{chat_id}') or None,
                        int(self._get_state(f'screenshot_reset_update:{chat_id}', '-1')))
                    board = bundle.get('board')
                    if not board:
                        problems.extend(bundle.get('unresolved', []))
                        continue
                    import analysis_engine
                    report = await asyncio.to_thread(analysis_engine.analyze_board, self.store, board['id'])
                    header = f"Read {bundle['market_count']} distinct lines across {bundle['upload_count']} recent screenshot(s)."
                    header += ('\nAssumed game date: ' if bundle.get('date_assumed') else '\nGame date: ') + date + ' Eastern; matched saved schedule.'
                    detail = await self._shortlist_reply(report, chat_id, upload_progress=True)
                    skipped = list(dict.fromkeys(bundle.get('unresolved', []) + problems))
                    if skipped:
                        detail += '\nUnread/unsupported cards kept out: ' + '; '.join(skipped[:2])[:270]
                        if len(skipped)>2:
                            detail += f' (+{len(skipped)-2} more).'
                    detail += '\nOnly readable options were compared; check the displayed image-read lines. /newboard starts a fresh set.'
                    messages.append(header+'\n\n'+detail)
                if not messages:
                    outcome = 'failed'
                return '\n\n'.join(messages)[:7000] or 'No supported board could be assembled. Send fewer complete cards.'
            except asyncio.CancelledError:
                outcome = 'canceled'
                raise
            except Exception:
                outcome = 'failed'
                raise
            finally:
                job.update(state=outcome, finished_at=utcnow(),
                           elapsed_seconds=round(max(0, time.monotonic()-job['clock']), 1))
                self._photo_progress.pop(chat_id, None)
                self._photo_recent.append(dict(job))
                del self._photo_recent[:-8]
                log.info('Screenshot job state=%s phase=%s elapsed=%.1fs',
                         outcome, job['phase'], job['elapsed_seconds'])
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)

    def _compact(self, value, limit=10000):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if len(text) > limit:
            text = text[:limit] + '\n… More records are available in the web interface.'
        return text

    async def _handle_message(self, message, update_id):
        if message.get('photo'):
            return await self._handle_photo(message, update_id)
        document = message.get('document')
        if document:
            filename = document.get('file_name', '')
            if str(document.get('mime_type', '')).startswith('image/') or filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                return await self._handle_photo(message, update_id)
            caption = str(message.get('caption', '')).strip()
            caption_parts = caption.split(maxsplit=1)
            caption_command = caption_parts[0].split('@', 1)[0].lower() if caption_parts else ''
            if caption_command == '/importmaster':
                if len(caption_parts) > 1 or not filename.lower().endswith('.xlsx'):
                    raise ValueError('Attach the updated .xlsx master file with only /importmaster as its caption.')
                from master_upgrade import merge_enriched_workbook
                payload = await self._download(document['file_id'], document.get('file_size', 0),
                                               max_bytes=MAX_MASTER_UPLOAD)
                await self._retry_db(self._queue, message['chat']['id'],
                    'File received. Validating and merging the master dataset. A large file can take several minutes. '
                    'Keep your original file until the completion reply arrives.',
                    event_key=f'master-import-start:{update_id}')
                report = await asyncio.to_thread(merge_enriched_workbook, self.store, payload,
                                                 filename=filename)
                if report.get('skipped'):
                    return 'This master file was already imported. No changes were made.'
                return ('Master merge complete. Existing entries, conversations and observations were retained. '
                        'A database backup was saved before the merge.\n' + self._compact(report, 3000))
            import_stats_file = caption_command == '/importstats'
            if import_stats_file and len(caption_parts) > 1:
                raise ValueError('Use only /importstats as the file caption; box scores must be in the attached CSV/JSON file.')
            if not filename.lower().endswith(('.csv', '.json')):
                raise ValueError('Send board or manual-stat files as CSV/JSON; send screenshots as Telegram photos.')
            payload = await self._download(document['file_id'], document.get('file_size', 0))
            if import_stats_file:
                from manual_import import import_stats, parse_stats_document
                rows = await asyncio.to_thread(parse_stats_document, filename, payload)
                report = await asyncio.to_thread(import_stats, self.store, rows,
                                                  source=f'Manual user upload (Telegram {update_id})')
                return ('Manual statistics import complete — user-supplied data, not independently verified:\n'
                        + self._compact(report))
            legs = parse_board_document(filename, payload)
            board = await asyncio.to_thread(self.store.create_board, legs, source=f'telegram:{update_id}:{filename[:100]}')
            return await self._board_saved_reply(board, message['chat']['id'])
        text = str(message.get('text', '')).strip()
        if not text:
            return 'Send a question, /help, or a PrizePicks board as text, CSV, JSON or a photo.'
        pieces = text.split(maxsplit=1)
        command, argument = pieces[0], pieces[1] if len(pieces) > 1 else ''
        command = command.split('@', 1)[0].lower()
        argument = argument.strip()
        if command in {'/start', '/help'}:
            return HELP
        if command in {'/pp', '/pp_refresh', '/pp_status'}:
            if argument:
                return 'Use /pp_refresh to check public offers or /pp_status for the saved connection result.'
            return await self._prizepicks_command(refresh=command != '/pp_status', chat_id=message['chat']['id'])
        if command == '/strategy':
            from strategy_info import strategy_summary
            return strategy_summary()
        if command == '/importmaster':
            return ('Attach the updated NBA master .xlsx file with /importmaster as its caption. '
                    'The bot backs up its database and merges missing observations while retaining existing records. '
                    'For files over 20 MB, use Import master Excel on the bot website. '
                    'Importing history does not verify today’s schedule or injuries.')
        if command in {'/refresh', '/update'}:
            if argument.casefold() == 'status':
                return await self._dataset_refresh_status()
            if argument:
                return 'Use /refresh or /update to check for updates, or /refresh status to see progress.'
            return await self._start_dataset_refresh(message['chat']['id'], update_id)
        if command == '/picks':
            return await self._independent_picks_reply(parse_picks_request(text), message['chat']['id'], text)
        if command == '/clear':
            await self._retry_db(self._clear_conversation, message['chat']['id'], update_id)
            return 'Conversation context and lineup preferences cleared. Your dataset and picks remain saved.'
        if command == '/status':
            data = await self._retry_db(self.store.status)
            telegram = await self._retry_db(self.status)
            return 'Data and worker status:\n' + self._compact(data) + '\nTelegram:\n' + self._compact(telegram)
        if command == '/freshness':
            if argument:
                return 'Use /freshness alone to check saved daily imports and the next game’s data readiness without AI.'
            return await asyncio.to_thread(freshness_report, self.store)
        if command == '/playerlog':
            if not argument:
                return 'Use /playerlog Jalen Brunson | points | 5 (optional fourth field: 2026-27).'
            from player_stats import player_log_command
            return await asyncio.to_thread(player_log_command, self.store, argument)
        if command == '/selftest':
            if argument.casefold() == 'status':
                return await self._self_test_status(message['chat']['id'])
            if argument:
                return 'Use /selftest to start checks or /selftest status for progress and the last report.'
            return await self._start_self_test(message['chat']['id'], update_id)
        if command == '/ai_status':
            if argument:
                return 'Use /ai_status alone to check local model readiness without making an AI request.'
            if self.model_status_callback is None:
                return 'AI status reporting is not configured for this deployment. /status still shows data-feed and Telegram health.'
            try:
                report = await asyncio.wait_for(self.model_status_callback(), timeout=3)
                if not isinstance(report, dict):
                    raise ValueError('AI status metadata must be an object.')
                safe = {}
                for key in ('phase', 'model', 'last_failure_category', 'last_failure_time',
                            'last_success_time', 'source_ref'):
                    if key in report:
                        safe[key] = str(report[key])[:160] if report[key] is not None else None
                for key in ('active', 'busy'):
                    if key in report:
                        safe[key] = bool(report[key])
                elapsed = report.get('active_elapsed_seconds')
                if isinstance(elapsed, (int, float)) and math.isfinite(elapsed):
                    safe['active_elapsed_seconds'] = round(max(0, elapsed), 1)
                failure = report.get('last_failure')
                if isinstance(failure, dict):
                    safe['last_failure'] = {key: str(failure[key])[:160] if failure[key] is not None else None
                                            for key in ('category', 'time', 'at') if key in failure}
                if isinstance(report.get('last_request'), dict):
                    safe['last_request'] = safe_ai_request_details(report['last_request'])
                return 'Local AI status:\n' + self._compact(safe)
            except asyncio.CancelledError:
                raise
            except Exception:
                return 'AI status is temporarily unavailable. /status still shows data-feed and Telegram health.'
        if command == '/diagnose':
            if argument:
                return 'Use /diagnose alone. It checks only the built-in public feed endpoints; custom URLs are not accepted.'
            from feed_diagnostics import diagnose_feeds
            report = await diagnose_feeds()
            return 'Public feed connection checks:\n' + self._compact(report)
        if command == '/today':
            today = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
            return f'NBA games for {today} (Eastern):\n' + self._compact(await asyncio.to_thread(self.store.games, today))
        if command == '/news':
            return 'Recent collected news — check each source timestamp:\n' + self._compact(await asyncio.to_thread(self.store.recent_news, limit=10))
        if command == '/reminder':
            from gameday_digest import reminder_status
            return reminder_status(self.store)
        if command == '/usage':
            from efficient_ai import usage_status
            return 'AI efficiency and billing:\n' + self._compact(usage_status(self.store))
        if command == '/summary':
            topic = argument.lower() or 'injuries'
            if topic not in {'injuries', 'news', 'trades'}:
                return 'Use /summary for injuries, /summary news for all collected reports, or /summary trades for roster transactions.'
            from news_digest import news_summary
            report = await asyncio.to_thread(news_summary, self.store, topic=topic, limit=5)
            return report if isinstance(report, str) else self._compact(report)
        if command == '/importstats':
            return 'Attach a CSV/JSON box-score file with the caption /importstats. For pasted JSON rows, use /stats <JSON list>. Screenshots remain OCR drafts.'
        if command == '/stats':
            if not argument:
                return 'Use /stats followed by a JSON list of box-score rows, or attach a CSV/JSON file with caption /importstats.'
            from manual_import import import_stats
            rows = json.loads(argument)
            if not isinstance(rows, list):
                raise ValueError('Manual stats must be a JSON list of box-score rows.')
            report = await asyncio.to_thread(import_stats, self.store, rows,
                                              source=f'Manual user upload (Telegram {update_id})')
            return ('Manual statistics import complete — user-supplied data, not independently verified:\n'
                    + self._compact(report))
        if command == '/newsnote':
            if not argument:
                return 'Use /newsnote HEADLINE followed by optional details on later lines. The note is saved as user-supplied/unverified and does not send an automatic alert.'
            from manual_import import record_manual_news
            title, _, detail = argument.partition('\n')
            note = await asyncio.to_thread(record_manual_news, self.store, title.strip(),
                                            detail=detail.strip(), team='NYK')
            return ('Personal news note saved as user-supplied/unverified. No automatic alert was broadcast:\n'
                    + self._compact(note))
        if command == '/boards':
            method = getattr(self.store, 'list_boards', None) or self.store.boards
            return 'Uploaded boards:\n' + self._compact(method())
        if command == '/newboard':
            await self._retry_db(self._reset_screenshots, message['chat']['id'], update_id)
            return 'Fresh screenshot set started. Send the current cards; previous saved boards and entries are unchanged.'
        if command == '/entries':
            return 'Tracked entries:\n' + self._compact(await asyncio.to_thread(self.store.list_entries))
        if command == '/results':
            return 'Results — projected grades and confirmed account returns are separate:\n' + self._compact(await asyncio.to_thread(self.store.ledger_summary))
        if command == '/board':
            if not argument:
                return 'Paste available lines with their game date:\n' + BOARD_EXAMPLE
            legs = parse_board_text(argument)
            board = await asyncio.to_thread(self.store.create_board, legs, source=f'telegram:{update_id}')
            return await self._board_saved_reply(board, message['chat']['id'])
        if command == '/track':
            parts = argument.split()
            if len(parts) not in {3, 4}:
                return 'Use /track BOARD_ID STAKE power|flex [DISPLAYED_MULTIPLIER]. This records that you actually placed the entire 2–6 pick board.'
            board = await asyncio.to_thread(self.store.get_board, parts[0])
            if not board:
                raise ValueError('Board not found. Use /boards to see uploaded boards.')
            if str(board.get('source', '')).startswith('screenshot:'):
                return ('That board contains image-read research alternatives. Save only the exact selections you actually '
                        'placed with /board, then /track that board with its stake and displayed payout.')
            if not 2 <= len(board['legs']) <= 6:
                raise ValueError('A placed entry needs 2–6 selections. Save a smaller board containing the exact selections you placed.')
            entry = await asyncio.to_thread(self.store.create_entry, board['legs'], float(parts[1]), parts[2],
                                            payout_multiplier=float(parts[3]) if len(parts) == 4 else None,
                                            source=f'telegram:{update_id}:board:{board["id"]}')
            return f'Placed entry recorded: {entry["id"]}\nOriginal selections and stake saved. Final-stat grades will be provisional until reconciled with PrizePicks settlement.'
        if command == '/entry':
            parsed = json.loads(argument)
            if not isinstance(parsed, dict):
                raise ValueError('Use /entry followed by an object with legs, stake, entry_type and the actual payout multiplier.')
            entry = await asyncio.to_thread(self.store.create_entry, parsed['legs'], parsed['stake'], parsed['entry_type'],
                                            payout_multiplier=parsed.get('payout_multiplier'),
                                            source=f'telegram:{update_id}')
            return f'Placed entry recorded: {entry["id"]}\nThe original selections are saved. Results will update after final stats; PrizePicks settlement exceptions may need review.'
        if command == '/settle':
            pieces = argument.split(maxsplit=2)
            if len(pieces) < 2:
                return 'Use /settle ENTRY_ID TOTAL_RETURN [note]. Total return includes the original stake; enter 0 for a loss.'
            result = await asyncio.to_thread(self.store.mark_settlement, pieces[0], float(pieces[1]),
                                                pieces[2] if len(pieces) > 2 else 'User confirmed via Telegram')
            return 'Actual PrizePicks return recorded:\n' + self._compact(result)
        if command == '/analyze':
            if not argument:
                return 'Use /analyze BOARD_ID after uploading available lines.'
            board = await asyncio.to_thread(self.store.get_board, argument)
            if not board:
                raise ValueError('Board not found. Use /boards to see uploaded boards.')
            await self._refresh_board_news()
            import analysis_engine
            report = await asyncio.to_thread(analysis_engine.analyze_board, self.store, argument)
            return await self._shortlist_reply(report, message['chat']['id'])
        if command.startswith('/'):
            return 'Unknown command. Use /help to see available commands.'
        independent_request = parse_picks_request(text)
        if independent_request is not None:
            await self._retry_db(self._remember, message['chat']['id'], update_id, text)
            response = await self._independent_picks_reply(independent_request, message['chat']['id'], text)
            await self._retry_db(self._remember, message['chat']['id'], update_id, text, response)
            return response
        if len(text) > 3000:
            raise ValueError('Please keep questions under 3000 characters, or upload a CSV/JSON board.')
        chat_id = message['chat']['id']
        history = await self._retry_db(self._conversation, chat_id)
        await self._retry_db(self._remember, chat_id, update_id, text)
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            if self.answer_preferences_callback is not None:
                response = await self.answer_preferences_callback(text, history, (await self._retry_db(self._lineup_preferences, chat_id)))
            else:
                response = await self.answer_callback(text, history)
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
        await self._retry_db(self._remember, chat_id, update_id, text, response)
        return response

    def _receive_batch(self, updates):
        """Persist the entire fetched inventory before confirming its max offset."""
        if not isinstance(updates, list):
            raise RuntimeError('Telegram returned an invalid update inventory.')
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                received = set()
                old = db.execute("SELECT value FROM telegram_state WHERE name='offset'").fetchone()
                offset = int(old['value']) if old else 0
                for update in updates:
                    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
                        continue
                    update_id = update['update_id']; message = update.get('message') or {}
                    allowed = self._authorized(message)
                    chat_id = message.get('chat', {}).get('id') if allowed else None
                    inserted = db.execute('INSERT OR IGNORE INTO telegram_updates '
                        '(update_id,chat_id,status,received_at) VALUES (?,?,?,?)',
                        (update_id, chat_id, 'processing' if allowed else 'ignored', utcnow())).rowcount
                    if inserted: received.add(update_id)
                    offset = max(offset, update_id + 1)
                db.execute("INSERT INTO telegram_state(name,value) VALUES ('offset',?) "
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (str(offset),))
                return received
        finally:
            db.close()

    def _receive_update(self, update_id, chat_id, authorized):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                inserted = db.execute('INSERT OR IGNORE INTO telegram_updates '
                    '(update_id,chat_id,status,received_at) VALUES (?,?,?,?)',
                    (update_id, chat_id, 'processing' if authorized else 'ignored', utcnow())).rowcount
                old = db.execute("SELECT value FROM telegram_state WHERE name='offset'").fetchone()
                offset = max(int(old['value']) if old else 0, update_id + 1)
                db.execute("INSERT INTO telegram_state(name,value) VALUES ('offset',?) "
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (str(offset),))
                return inserted
        finally:
            db.close()

    def _complete_update(self, update_id, chat_id, authorized, response):
        db = self._db(busy_timeout_ms=TRANSPORT_BUSY_TIMEOUT_MS)
        try:
            with db:
                if authorized and response is not None:
                    self._queue_in_db(db, chat_id, response, f'reply:{update_id}')
                db.execute('UPDATE telegram_updates SET status=?,completed_at=? WHERE update_id=?',
                           ('done' if authorized else 'ignored', utcnow(), update_id))
                old = db.execute("SELECT value FROM telegram_state WHERE name='offset'").fetchone()
                offset = max(int(old['value']) if old else 0, update_id + 1)
                db.execute("INSERT INTO telegram_state(name,value) VALUES ('offset',?) "
                           'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (str(offset),))
        finally:
            db.close()

    async def process_update(self, update, *, overloaded=False, received=False):
        update_id = update.get('update_id')
        if not isinstance(update_id, int):
            return False
        message = update.get('message') or {}
        authorized = self._authorized(message)
        chat_id = message.get('chat', {}).get('id') if authorized else None
        inserted = True if received else await self._retry_db(self._receive_update, update_id, chat_id, authorized)
        response = None
        if not inserted:
            return False
        if authorized:
            try:
                if overloaded:
                    response = ('I’m finishing earlier requests. This request was not started and no records '
                                'were changed. Try again shortly; /status, /ai_status and /help still work.')
                else:
                    text = str(message.get('text') or message.get('caption') or '').strip()
                    context_command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text else ''
                    remember_result = bool(message.get('photo') or message.get('document') or
                                           context_command in {'/picks','/board','/analyze'})
                    if remember_result:
                        await self._retry_db(self._remember, chat_id, update_id, text or 'Analyze these uploaded player lines.')
                    response = await self._handle_ordered_message(message, update_id)
                    if remember_result and response:
                        await self._retry_db(self._remember, chat_id, update_id, text, response)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
                # Validation errors contain user data or field names, not network credentials.
                response = 'Could not save that input: ' + str(error)[:500] + '\nUse /help for the required format.'
            except asyncio.CancelledError:
                raise
            except sqlite3.Error as error:
                if not sqlite_contention(error):
                    raise
                self._database_health = {'phase': 'waiting_for_writer', 'checked_at': utcnow(), **sqlite_diagnostic(error)}
                response = ('The database is finishing an update. This command could not finish. '
                            'Check /status, /boards and /entries before retrying.')
            except Exception:
                response = 'That command could not finish. Check /status, /boards and /entries before trying again.'
                log.warning('Telegram command failed for update %s; no credentials logged.', update_id)
        await self._retry_db(self._complete_update, update_id, chat_id, authorized, response)
        return authorized

    def _recover_interrupted(self):
        self._recover_dataset_refreshes()
        self._recover_self_tests()
        with self._db() as db:
            rows = db.execute("SELECT update_id,chat_id FROM telegram_updates WHERE status='processing'").fetchall()
        owned = {task.get_name().split(':', 1)[1] for task in list(self._dataset_refresh_tasks)
                 if not task.done() and task.get_name().startswith('dataset-refresh:')}
        with self._db() as db:
            owned_updates = {row['update_id'] for row in db.execute(
                "SELECT id,update_id FROM telegram_dataset_jobs WHERE status='running'") if row['id'] in owned}
            self_owned = {task.get_name().split(':',1)[1] for task in list(self._self_test_tasks)
                          if not task.done() and task.get_name().startswith('self-test:')}
            self_owned.update(self._self_test_pending_results)
            owned_updates.update(row['update_id'] for row in db.execute(
                "SELECT id,update_id FROM telegram_selftest_jobs WHERE status='running'") if row['id'] in self_owned)
        for row in rows:
            if row['update_id'] in owned_updates:
                continue
            if row['chat_id'] in self.allowed_user_ids:
                self._queue(row['chat_id'], 'A previous command was interrupted by a restart. '
                            'Check /boards and /entries before retrying so an entry is not recorded twice.',
                            f'interrupted:{row["update_id"]}')
            with self._db() as db:
                db.execute("UPDATE telegram_updates SET status='interrupted',completed_at=? WHERE update_id=?",
                           (utcnow(), row['update_id']))
            self._set_state('offset', max(int(self._get_state('offset', '0')), row['update_id'] + 1))

    async def run(self):
        if not self.enabled:
            log.info('Telegram is disabled until a bot token and numeric user allowlist are configured privately.')
            return
        await self._retry_db(self._recover_interrupted)
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))
            self._owns_client = True
        command_tasks = set()

        async def deliver_messages():
            while True:
                try:
                    await self.flush_outbox()
                except asyncio.CancelledError:
                    raise
                except sqlite3.Error as error:
                    self._database_health = {'phase': 'waiting_for_writer' if sqlite_contention(error) else 'failed',
                                             'checked_at': utcnow(), **sqlite_diagnostic(error)}
                    if not sqlite_contention(error):
                        raise
                except Exception:
                    # Do not let failure diagnostics cause a second DB failure.
                    with suppress(Exception):
                        await self._retry_db(self._set_state, 'last_delivery_error', 'Telegram message delivery is retrying.')
                await asyncio.sleep(1)

        delivery_task = asyncio.create_task(deliver_messages())

        command_failure = asyncio.get_running_loop().create_future()

        async def poll_updates(payload):
            poll = asyncio.create_task(self._api('getUpdates', payload))
            try:
                await asyncio.wait({poll, delivery_task, command_failure}, return_when=asyncio.FIRST_COMPLETED)
                if delivery_task.done():
                    delivery_task.result()
                    raise RuntimeError('Telegram delivery worker stopped unexpectedly.')
                if command_failure.done():
                    raise command_failure.result()
                return await poll
            finally:
                if not poll.done():
                    poll.cancel()
                    await asyncio.gather(poll, return_exceptions=True)

        def command_finished(task):
            command_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                error = task.exception()
                if isinstance(error, sqlite3.Error) and not command_failure.done():
                    command_failure.set_result(error)
                log.warning('Telegram command task failed; check persistent worker health.')

        try:
            while True:
                try:
                    updates = await poll_updates({
                        'offset': int(await self._retry_db(self._transport_state, 'offset', '0')),
                        'timeout': 25, 'allowed_updates': ['message'], 'limit': 20})
                    self.connected = True
                    await self._retry_db(self._transport_states, {'last_poll_success': utcnow(), 'last_error': ''})
                    received = await self._retry_db(self._receive_batch, updates or [])
                    for update in updates or []:
                        if update.get('update_id') not in received:
                            continue
                        # Keep polling and short commands responsive while a
                        # self-hosted CPU model is answering another question.
                        if len(command_tasks) >= MAX_ACTIVE_COMMANDS:
                            message = update.get('message') or {}
                            text = str(message.get('text', '')).strip().split(maxsplit=1)
                            command = text[0].split('@', 1)[0].lower() if text else ''
                            responsive = (command in RESPONSIVE_COMMANDS and not message.get('photo')
                                          and not message.get('document'))
                            # Do not stop polling behind slow inference/uploads.
                            # Receipt and any busy response remain deduplicated.
                            await self.process_update(update, overloaded=not responsive, received=True)
                            continue
                        task = asyncio.create_task(self.process_update(update, received=True))
                        command_tasks.add(task)
                        task.add_done_callback(command_finished)
                    # Let newly created tasks persist receipt/checkpoint before
                    # starting the next long poll.
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except sqlite3.Error:
                    # Permanent errors (FULL/READONLY/etc.) remain visible to
                    # the supervisor. BUSY/LOCKED retry inside _retry_db.
                    raise
                except Exception as error:
                    self.connected = False
                    await self._retry_db(self._transport_states, {'last_error': 'Telegram polling failed; check bot token, existing webhook and connectivity.'})
                    log.warning('Telegram polling failed; retrying without logging credentials.')
                    delay = max(10, error.retry_after or 0) if isinstance(error, TelegramAPIError) else 10
                    await asyncio.sleep(delay)
        finally:
            self.connected = False
            delivery_task.cancel()
            for task in list(command_tasks):
                task.cancel()
            for task in list(self._album_tasks):
                task.cancel()
            await asyncio.gather(delivery_task, *command_tasks, *self._album_tasks, return_exceptions=True)
            # run() can be restarted on this instance. Do not let a later album
            # fragment attach to a collector cancelled during the previous run.
            self._album_batches.clear()
            self._album_tasks.clear()
            if self._owns_client:
                # A close failure must not leave a closed/broken client attached
                # for the supervisor's next run of this same bot instance.
                client = self.client
                self.client = None
                self._owns_client = False
                if client is not None:
                    await client.aclose()
