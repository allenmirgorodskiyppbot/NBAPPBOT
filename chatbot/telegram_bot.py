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
BOARD_EXAMPLE = '/board YYYY-MM-DD\nJalen Brunson | points | more | 24.5\nJosh Hart | rebounds | less | 8.5'
HELP = '''Knicks Research bot
/today — today’s NBA games
/news — recent relevant news
/summary [injuries|news|trades] — fast summary of collected reports (default: injuries)
/status — feed freshness and bot status
/ai_status — local AI readiness, busy state and last failure category
/selftest — isolated data/backup tests and read-only live health checks
/playerlog NAME | points | 5 — stored game logs and averages without waiting for AI
/clear — clear this chat's recent conversation context
/diagnose — check the built-in public NBA feed connections
/importstats — attach CSV/JSON box scores with this caption to import manual stats
/stats <JSON list> — import manually supplied box-score rows
/newsnote HEADLINE then optional details — save an unverified personal news note
/boards — uploaded PrizePicks boards
/board DATE then player | stat | more/less | line — save available lines
/analyze BOARD_ID — analyze an uploaded board
/track BOARD_ID STAKE power|flex [MULTIPLIER] — record that board as an entry you placed
/entry <JSON object> — record an entry you actually placed
/entries — tracked entries and outcomes
/results — results and confirmed profit/loss
/settle ENTRY_ID PAYOUT [note] — confirm the actual total return shown by PrizePicks

You can also send a question, a CSV/JSON board file, or a screenshot. Screenshot OCR is a draft: correct the player, market, line, side and game date before saving it. Available lines are separate from entries actually placed.

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
    if not isinstance(legs, list) or not legs or len(legs) > 200:
        raise ValueError('The board must contain 1–200 line objects.')
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
               'points+rebounds': 'pr', 'points+assists': 'pa', 'rebounds+assists': 'ra'}
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
    if not 1 <= len(legs) <= 200:
        raise ValueError('Use 1–200 board lines.')
    return legs


class TelegramBot:
    def __init__(self, store, answer_callback, *, token=None, allowed_user_ids=None, client=None,
                 model_status_callback=None, self_test_callback=None):
        self.store = store
        self.answer_callback = answer_callback
        self.model_status_callback = model_status_callback
        self.self_test_callback = self_test_callback
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
            db.execute('INSERT INTO telegram_conversation VALUES (?,?,?,?,?) '
                       'ON CONFLICT(chat_id,update_id) DO UPDATE SET answer=excluded.answer',
                       (chat_id, update_id, question[:3000], answer[:4000] if answer else None, utcnow()))
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

    def _queue(self, chat_id, text, event_key):
        if int(chat_id) not in self.allowed_user_ids:
            return
        with self._db() as db:
            for number, part in enumerate(chunks(text)):
                db.execute('INSERT OR IGNORE INTO telegram_outbox '
                           '(event_key,chat_id,text,created_at) VALUES (?,?,?,?)',
                           (f'{event_key}:{chat_id}:{number}', int(chat_id), part, utcnow()))

    async def notify(self, text, event_key=None):
        """Queue a private alert. Delivery retries are handled by run()."""
        if not self.enabled:
            return False
        key = event_key or 'notification:' + uuid.uuid4().hex
        for user_id in self.allowed_user_ids:
            self._queue(user_id, text, key)
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

    async def flush_outbox(self):
        if not self.enabled:
            return
        with self._db() as db:
            rows = db.execute("SELECT * FROM telegram_outbox WHERE status='pending' "
                              'AND next_attempt<=? ORDER BY id LIMIT 20', (time.time(),)).fetchall()
        for row in rows:
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
        photo = max(message['photo'], key=lambda p: p.get('file_size', 0))
        payload = await self._download(photo['file_id'], photo.get('file_size', 0))
        text = await asyncio.to_thread(self._ocr, payload)
        if text is None:
            return 'Screenshot OCR is not installed in this deployment. Send a CSV/JSON board or paste the lines using /board.\n' + BOARD_EXAMPLE
        draft_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO telegram_drafts '
                       '(id,update_id,chat_id,kind,text,created_at) VALUES (?,?,?,?,?,?)',
                       (draft_id, update_id, message['chat']['id'], 'ocr', text, utcnow()))
        return (f'Screenshot draft saved: {draft_id}\nNo placed entry has been recorded. '
                'OCR can misread names and numbers. Check the extracted text and save corrected lines with /board. '
                'Include the game date (YYYY-MM-DD), statistic, line and More/Less side.\n\n'
                + (text[:2400] or '(No readable text found.)') + '\n\nBoard format:\n' + BOARD_EXAMPLE)

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
            return f'PrizePicks board saved: {board["id"]}\n{len(board["legs"])} lines. No placed entry recorded.\n/analyze {board["id"]}'
        text = str(message.get('text', '')).strip()
        if not text:
            return 'Send a question, /help, or a PrizePicks board as text, CSV, JSON or a photo.'
        pieces = text.split(maxsplit=1)
        command, argument = pieces[0], pieces[1] if len(pieces) > 1 else ''
        command = command.split('@', 1)[0].lower()
        argument = argument.strip()
        if command in {'/start', '/help'}:
            return HELP
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
        if command == '/entries':
            return 'Tracked entries:\n' + self._compact(self.store.list_entries())
        if command == '/results':
            return 'Results — projected grades and confirmed account returns are separate:\n' + self._compact(self.store.ledger_summary())
        if command == '/board':
            if not argument:
                return 'Paste available lines with their game date:\n' + BOARD_EXAMPLE
            legs = parse_board_text(argument)
            board = self.store.create_board(legs, source=f'telegram:{update_id}')
            return f'Board saved: {board["id"]}\n{len(board["legs"])} lines. No placed entry recorded.\n/analyze {board["id"]}'
        if command == '/track':
            parts = argument.split()
            if len(parts) not in {3, 4}:
                return 'Use /track BOARD_ID STAKE power|flex [DISPLAYED_MULTIPLIER]. This records that you actually placed the entire 2–6 pick board.'
            board = self.store.get_board(parts[0])
            if not board:
                raise ValueError('Board not found. Use /boards to see uploaded boards.')
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
            from analysis_engine import analyze_board
            report = await asyncio.to_thread(analyze_board, self.store, argument)
            return 'Board research — descriptive evidence, not a validated prediction of a win:\n' + self._compact(report)
        if command.startswith('/'):
            return 'Unknown command. Use /help to see available commands.'
        if len(text) > 3000:
            raise ValueError('Please keep questions under 3000 characters, or upload a CSV/JSON board.')
        chat_id = message['chat']['id']
        history = self._conversation(chat_id)
        self._remember(chat_id, update_id, text)
        response = await self.answer_callback(text, history)
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
            await asyncio.gather(delivery_task, *command_tasks, return_exceptions=True)
            if self._owns_client:
                await self.client.aclose()
                self.client = None
