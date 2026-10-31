"""Private Telegram interface with durable updates, upload drafts and an outbox.

The bot does not log Telegram API URLs because those URLs contain its token.
Only explicitly allowlisted users in private chats can invoke commands.
"""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone, timedelta
import io
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
TYPING_INTERVAL_SECONDS = 4


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
                              'retrieval_with_analysis', 'empty_reply', 'numerical_prose', 'unsupported_assignment', 'unsupported_scope'},
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
/ai_status — local AI readiness, busy state and last failure category
/selftest — isolated data/backup tests and read-only live health checks
/playerlog NAME | points | 5 — stored game logs and averages without waiting for AI
/picks [YYYY-MM-DD] [2-6] — own projections and target lines; no upload needed
/clear — clear this chat's recent conversation context
/diagnose — check the built-in public NBA feed connections
/importstats — attach CSV/JSON box scores with this caption to import manual stats
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

Ask for picks or use /picks to get the bot's own projections and conditional target lines before checking PrizePicks. With no date, it uses the next unstarted saved Knicks game; "tonight" or an explicit date stays on that day. It can return fewer candidates when evidence is insufficient. These are model targets, not observed PrizePicks offers or placed entries.

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


def parse_picks_request(text, now=None):
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
    else:
        lower = re.sub(r'\bnew york(?:\s+knicks)?\b', 'knicks', lower)
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
        argument = lower
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    today = current.astimezone(ZoneInfo('America/New_York')).date()
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
    count = numbers[0] if numbers else 4
    if command:
        remaining = re.sub(r'\b(?:\d{4}-\d{2}-\d{2}|\d+|two|three|four|five|six|today|tonight|tomorrow|next)\b', '', argument)
        if remaining.strip():
            raise ValueError('Use /picks [YYYY-MM-DD|today|tomorrow|next] [2-6].')
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
                 model_status_callback=None, self_test_callback=None, research_refresh_callback=None):
        self.store = store
        self.answer_callback = answer_callback
        self.model_status_callback = model_status_callback
        self.self_test_callback = self_test_callback
        self.research_refresh_callback = research_refresh_callback
        self._photo_locks = {}
        self._photo_semaphore = asyncio.Semaphore(1)
        self._album_batches = {}
        self._album_tasks = set()
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

    def _db(self):
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        return db

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
                              'AND created_at>=? ORDER BY update_id DESC LIMIT 3', (chat_id, cutoff)).fetchall()
        history = []
        for row in reversed(rows):
            history.append({'role': 'user', 'content': row['question']})
            if row['answer']:
                history.append({'role': 'assistant', 'content': row['answer']})
        return history[-6:]

    def _remember(self, chat_id, update_id, question, answer=None):
        with self._db() as db:
            if answer is None:
                db.execute('INSERT OR IGNORE INTO telegram_conversation VALUES (?,?,?,?,?)',
                           (chat_id, update_id, question[:3000], None, utcnow()))
            else:
                # A completed older answer must not resurrect a conversation that
                # was cleared, or a question already removed by the history limit.
                db.execute('UPDATE telegram_conversation SET answer=? WHERE chat_id=? AND update_id=?',
                           (answer[:4000], chat_id, update_id))
            db.execute('DELETE FROM telegram_conversation WHERE chat_id=? AND update_id NOT IN '
                       '(SELECT update_id FROM telegram_conversation WHERE chat_id=? '
                       'ORDER BY update_id DESC LIMIT 3)', (chat_id, chat_id))

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
                'last_error': self._get_state('last_error') or None,
                'offset': int(self._get_state('offset', '0'))}

    def _authorized(self, message):
        sender = message.get('from') or {}
        chat = message.get('chat') or {}
        return (chat.get('type') == 'private' and sender.get('id') in self.allowed_user_ids
                and chat.get('id') == sender.get('id') and not sender.get('is_bot', False))

    def _queue(self, chat_id, text, event_key, expires_at=None):
        if int(chat_id) not in self.allowed_user_ids:
            return
        with self._db() as db:
            for number, part in enumerate(chunks(text)):
                db.execute('INSERT OR IGNORE INTO telegram_outbox '
                           '(event_key,chat_id,text,created_at,expires_at) VALUES (?,?,?,?,?)',
                           (f'{event_key}:{chat_id}:{number}', int(chat_id), part, utcnow(), expires_at))

    async def notify(self, text, event_key=None, expires_at=None):
        """Queue a private alert. Delivery retries are handled by run()."""
        if not self.enabled:
            return False
        key = event_key or 'notification:' + uuid.uuid4().hex
        for user_id in self.allowed_user_ids:
            self._queue(user_id, text, key, expires_at=expires_at)
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
        if response.status_code != 200 or not data.get('ok'):
            # Do not expose provider errors or URLs which might contain credentials.
            code = int(data.get('error_code', response.status_code))
            raise RuntimeError(f'Telegram API returned status {code}; check private bot setup.')
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
        with self._db() as db:
            db.execute("UPDATE telegram_outbox SET status='expired' WHERE status='pending' "
                       'AND expires_at IS NOT NULL AND expires_at<=?', (time.time(),))
            rows = db.execute("SELECT * FROM telegram_outbox WHERE status='pending' "
                              'AND next_attempt<=? ORDER BY id LIMIT 20', (time.time(),)).fetchall()
        for row in rows:
            if row['expires_at'] is not None and row['expires_at'] <= time.time():
                with self._db() as db:
                    db.execute("UPDATE telegram_outbox SET status='expired' WHERE id=?", (row['id'],))
                continue
            if row['chat_id'] not in self.allowed_user_ids:
                with self._db() as db:
                    db.execute("UPDATE telegram_outbox SET status='blocked' WHERE id=?", (row['id'],))
                continue
            try:
                await self._api('sendMessage', {'chat_id': row['chat_id'], 'text': row['text'],
                                               'link_preview_options': {'is_disabled': True}})
            except RuntimeError:
                attempts = row['attempts'] + 1
                with self._db() as db:
                    db.execute('UPDATE telegram_outbox SET attempts=?,next_attempt=? WHERE id=?',
                               (attempts, time.time() + min(900, 5 * 2 ** min(attempts, 8)), row['id']))
                self._set_state('last_error', 'Telegram delivery failed; queued for retry.')
                return
            with self._db() as db:
                db.execute("UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE id=?",
                           (utcnow(), row['id']))
            self._set_state('last_delivery', utcnow())

    async def _download(self, file_id, declared_size=0):
        if declared_size and int(declared_size) > MAX_UPLOAD:
            raise ValueError('Upload is too large. Please send an image or board file under 8 MB.')
        metadata = await self._api('getFile', {'file_id': file_id})
        file_path = str(metadata.get('file_path', ''))
        if (not re.fullmatch(r'[A-Za-z0-9_./-]+', file_path)
                or '..' in file_path.split('/') or not file_path):
            raise RuntimeError('Telegram returned an invalid file path.')
        if metadata.get('file_size', 0) > MAX_UPLOAD:
            raise ValueError('Upload is too large. Please send an image or board file under 8 MB.')
        payload = bytearray()
        try:
            async with self.client.stream('GET', f'https://api.telegram.org/file/bot{self.token}/{file_path}') as response:
                if response.status_code != 200:
                    raise RuntimeError('Could not download the Telegram upload.')
                async for part in response.aiter_bytes():
                    payload.extend(part)
                    if len(payload) > MAX_UPLOAD:
                        raise ValueError('Upload exceeds 8 MB. Send a smaller file.')
        except httpx.HTTPError:
            raise RuntimeError('Telegram file download failed; please try the upload again.') from None
        return bytes(payload)

    async def _board_saved_reply(self, board):
        heading=f'PrizePicks board saved: {board["id"]}\n{len(board["legs"])} uploaded lines. No placed entry recorded.'
        if not callable(getattr(self.store,'connect',None)):
            return heading+'\n/analyze '+board['id']
        await self._refresh_board_news()
        import analysis_engine
        report=await asyncio.to_thread(analysis_engine.analyze_board,self.store,board['id'])
        return heading+'\n\n'+await self._shortlist_reply(report)

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

    async def _shortlist_reply(self, report):
        if callable(getattr(self.store, 'connect', None)) and 'original_legs' in report:
            from board_shortlist import enrich_shortlist, format_shortlist
            shortlist = await asyncio.to_thread(enrich_shortlist, self.store, report)
            return format_shortlist(shortlist)
        import analysis_engine
        formatter = getattr(analysis_engine, 'format_analysis', None)
        return formatter(report) if formatter else self._compact(report)

    async def _independent_picks_reply(self, request, chat_id):
        from independent_picks import build_independent_picks, format_independent_picks
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            await self._refresh_board_news()
            report = await asyncio.to_thread(build_independent_picks, self.store, **request)
            return format_independent_picks(report)
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
            try:
                now = datetime.now(timezone.utc)
                dates, newly_read = set(), 0
                problems = []
                captions = {date for message, _ in items for date in
                            re.findall(r'\b\d{4}-\d{2}-\d{2}\b', str(message.get('caption', '')))}
                inherited = next(iter(captions)) if len(captions) == 1 else None
                for message, update_id in sorted(items, key=lambda item: item[1]):
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
                    legs, unresolved = await asyncio.to_thread(resolve_cards, self.store, extraction, date, roster)
                    await asyncio.to_thread(save_extraction, self.store, chat_id, update_id, date, basis,
                                            extraction, legs, unresolved, now)
                    newly_read += len({(leg['player'], leg['stat'], leg['line']) for leg in legs})
                    dates.add(date)
                    problems.extend(unresolved)
                if not newly_read:
                    details = '\n'.join('• '+str(value)[:160] for value in list(dict.fromkeys(problems))[:3])
                    return ('I couldn’t read a complete, supported card confidently enough to choose a pick.\n'
                            + (details+'\n' if details else '') +
                            'Send a closer crop with the player, full statistic label, line and More/Less buttons visible, '
                            'preferably as an image file. I kept unclear cards out; no entry was recorded.')
                messages = []
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
                    detail = await self._shortlist_reply(report)
                    skipped = list(dict.fromkeys(bundle.get('unresolved', []) + problems))
                    if skipped:
                        detail += '\nUnread/unsupported cards kept out: ' + '; '.join(skipped[:2])[:270]
                        if len(skipped)>2:
                            detail += f' (+{len(skipped)-2} more).'
                    detail += '\nOnly readable options were compared; check the displayed image-read lines. /newboard starts a fresh set.'
                    messages.append(header+'\n\n'+detail)
                return '\n\n'.join(messages)[:7000] or 'No supported board could be assembled. Send fewer complete cards.'
            finally:
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
            board = self.store.create_board(legs, source=f'telegram:{update_id}:{filename[:100]}')
            return await self._board_saved_reply(board)
        text = str(message.get('text', '')).strip()
        if not text:
            return 'Send a question, /help, or a PrizePicks board as text, CSV, JSON or a photo.'
        pieces = text.split(maxsplit=1)
        command, argument = pieces[0], pieces[1] if len(pieces) > 1 else ''
        command = command.split('@', 1)[0].lower()
        argument = argument.strip()
        if command in {'/start', '/help'}:
            return HELP
        if command == '/picks':
            return await self._independent_picks_reply(parse_picks_request(text), message['chat']['id'])
        if command == '/clear':
            with self._db() as db:
                db.execute('DELETE FROM telegram_conversation WHERE chat_id=?', (message['chat']['id'],))
            return 'Recent conversation context cleared. Your dataset and picks remain saved.'
        if command == '/status':
            return 'Data and worker status:\n' + self._compact(self.store.status()) + '\nTelegram:\n' + self._compact(self.status())
        if command == '/playerlog':
            if not argument:
                return 'Use /playerlog Jalen Brunson | points | 5 (optional fourth field: 2026-27).'
            from player_stats import player_log_command
            return await asyncio.to_thread(player_log_command, self.store, argument)
        if command == '/selftest':
            if argument:
                return 'Use /selftest alone. Sample imports and entries stay in temporary test databases.'
            if self.self_test_callback is None:
                return 'Self-test is not configured for this deployment. Use /status and /ai_status.'
            try:
                report = await asyncio.wait_for(self.self_test_callback(), timeout=60)
                return report if isinstance(report, str) else 'Self-test did not return a usable report.'
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Self-test could not finish (%s)', type(error).__name__)
                return 'Self-test could not finish. Check the chatbot logs; no sample entries were added to your master.'
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
            return f'NBA games for {today} (Eastern):\n' + self._compact(self.store.games(today))
        if command == '/news':
            return 'Recent collected news — check each source timestamp:\n' + self._compact(self.store.recent_news(limit=10))
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
            self._reset_screenshots(message['chat']['id'], update_id)
            return 'Fresh screenshot set started. Send the current cards; previous saved boards and entries are unchanged.'
        if command == '/entries':
            return 'Tracked entries:\n' + self._compact(self.store.list_entries())
        if command == '/results':
            return 'Results — projected grades and confirmed account returns are separate:\n' + self._compact(self.store.ledger_summary())
        if command == '/board':
            if not argument:
                return 'Paste available lines with their game date:\n' + BOARD_EXAMPLE
            legs = parse_board_text(argument)
            board = self.store.create_board(legs, source=f'telegram:{update_id}')
            return await self._board_saved_reply(board)
        if command == '/track':
            parts = argument.split()
            if len(parts) not in {3, 4}:
                return 'Use /track BOARD_ID STAKE power|flex [DISPLAYED_MULTIPLIER]. This records that you actually placed the entire 2–6 pick board.'
            board = self.store.get_board(parts[0])
            if not board:
                raise ValueError('Board not found. Use /boards to see uploaded boards.')
            if str(board.get('source', '')).startswith('screenshot:'):
                return ('That board contains image-read research alternatives. Save only the exact selections you actually '
                        'placed with /board, then /track that board with its stake and displayed payout.')
            if not 2 <= len(board['legs']) <= 6:
                raise ValueError('A placed entry needs 2–6 selections. Save a smaller board containing the exact selections you placed.')
            entry = self.store.create_entry(board['legs'], float(parts[1]), parts[2],
                                            payout_multiplier=float(parts[3]) if len(parts) == 4 else None,
                                            source=f'telegram:{update_id}:board:{board["id"]}')
            return f'Placed entry recorded: {entry["id"]}\nOriginal selections and stake saved. Final-stat grades will be provisional until reconciled with PrizePicks settlement.'
        if command == '/entry':
            parsed = json.loads(argument)
            if not isinstance(parsed, dict):
                raise ValueError('Use /entry followed by an object with legs, stake, entry_type and the actual payout multiplier.')
            entry = self.store.create_entry(parsed['legs'], parsed['stake'], parsed['entry_type'],
                                            payout_multiplier=parsed.get('payout_multiplier'),
                                            source=f'telegram:{update_id}')
            return f'Placed entry recorded: {entry["id"]}\nThe original selections are saved. Results will update after final stats; PrizePicks settlement exceptions may need review.'
        if command == '/settle':
            pieces = argument.split(maxsplit=2)
            if len(pieces) < 2:
                return 'Use /settle ENTRY_ID TOTAL_RETURN [note]. Total return includes the original stake; enter 0 for a loss.'
            result = self.store.mark_settlement(pieces[0], float(pieces[1]),
                                                pieces[2] if len(pieces) > 2 else 'User confirmed via Telegram')
            return 'Actual PrizePicks return recorded:\n' + self._compact(result)
        if command == '/analyze':
            if not argument:
                return 'Use /analyze BOARD_ID after uploading available lines.'
            board = self.store.get_board(argument)
            if not board:
                raise ValueError('Board not found. Use /boards to see uploaded boards.')
            await self._refresh_board_news()
            import analysis_engine
            report = await asyncio.to_thread(analysis_engine.analyze_board, self.store, argument)
            return await self._shortlist_reply(report)
        if command.startswith('/'):
            return 'Unknown command. Use /help to see available commands.'
        independent_request = parse_picks_request(text)
        if independent_request is not None:
            response = await self._independent_picks_reply(independent_request, message['chat']['id'])
            self._remember(message['chat']['id'], update_id, text)
            self._remember(message['chat']['id'], update_id, text, response)
            return response
        if len(text) > 3000:
            raise ValueError('Please keep questions under 3000 characters, or upload a CSV/JSON board.')
        chat_id = message['chat']['id']
        history = self._conversation(chat_id)
        self._remember(chat_id, update_id, text)
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            response = await self.answer_callback(text, history)
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
        self._remember(chat_id, update_id, text, response)
        return response

    async def process_update(self, update):
        update_id = update.get('update_id')
        if not isinstance(update_id, int):
            return False
        message = update.get('message') or {}
        authorized = self._authorized(message)
        chat_id = message.get('chat', {}).get('id') if authorized else None
        with self._db() as db:
            inserted = db.execute('INSERT OR IGNORE INTO telegram_updates '
                                  '(update_id,chat_id,status,received_at) VALUES (?,?,?,?)',
                                  (update_id, chat_id, 'processing' if authorized else 'ignored', utcnow())).rowcount
            # Acknowledge only after a durable marker exists. A restart can then
            # identify an interrupted command without replaying ledger writes.
            old = db.execute("SELECT value FROM telegram_state WHERE name='offset'").fetchone()
            offset = max(int(old['value']) if old else 0, update_id + 1)
            db.execute("INSERT INTO telegram_state(name,value) VALUES ('offset',?) "
                       'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (str(offset),))
        if not inserted:
            return False
        if authorized:
            try:
                response = await self._handle_message(message, update_id)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
                # Validation errors contain user data or field names, not network credentials.
                response = 'Could not save that input: ' + str(error)[:500] + '\nUse /help for the required format.'
            except asyncio.CancelledError:
                raise
            except Exception:
                response = 'That command could not finish. Check /status, /boards and /entries before trying again.'
                log.warning('Telegram command failed for update %s; no credentials logged.', update_id)
            if response is not None:
                self._queue(chat_id, response, f'reply:{update_id}')
        with self._db() as db:
            db.execute('UPDATE telegram_updates SET status=?,completed_at=? WHERE update_id=?',
                       ('done' if authorized else 'ignored', utcnow(), update_id))
            old = db.execute("SELECT value FROM telegram_state WHERE name='offset'").fetchone()
            offset = max(int(old['value']) if old else 0, update_id + 1)
            db.execute("INSERT INTO telegram_state(name,value) VALUES ('offset',?) "
                       'ON CONFLICT(name) DO UPDATE SET value=excluded.value', (str(offset),))
        return authorized

    def _recover_interrupted(self):
        with self._db() as db:
            rows = db.execute("SELECT update_id,chat_id FROM telegram_updates WHERE status='processing'").fetchall()
        for row in rows:
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
        self._recover_interrupted()
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
                except Exception:
                    self._set_state('last_error', 'Telegram message delivery is retrying.')
                await asyncio.sleep(1)

        delivery_task = asyncio.create_task(deliver_messages())

        def command_finished(task):
            command_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.warning('Telegram command task failed; check persistent worker health.')

        try:
            while True:
                try:
                    updates = await self._api('getUpdates', {
                        'offset': int(self._get_state('offset', '0')),
                        'timeout': 25, 'allowed_updates': ['message'], 'limit': 20})
                    self.connected = True
                    self._set_state('last_poll_success', utcnow())
                    self._set_state('last_error', '')
                    for update in updates or []:
                        # Keep polling and short commands responsive while a
                        # self-hosted CPU model is answering another question.
                        if len(command_tasks) >= 10:
                            await asyncio.wait(command_tasks, return_when=asyncio.FIRST_COMPLETED)
                        task = asyncio.create_task(self.process_update(update))
                        command_tasks.add(task)
                        task.add_done_callback(command_finished)
                    # Let newly created tasks persist receipt/checkpoint before
                    # starting the next long poll.
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.connected = False
                    self._set_state('last_error', 'Telegram polling failed; check bot token, existing webhook and connectivity.')
                    log.warning('Telegram polling failed; retrying without logging credentials.')
                    await asyncio.sleep(10)
        finally:
            self.connected = False
            delivery_task.cancel()
            for task in list(command_tasks):
                task.cancel()
            for task in list(self._album_tasks):
                task.cancel()
            await asyncio.gather(delivery_task, *command_tasks, *self._album_tasks, return_exceptions=True)
            if self._owns_client:
                await self.client.aclose()
                self.client = None
