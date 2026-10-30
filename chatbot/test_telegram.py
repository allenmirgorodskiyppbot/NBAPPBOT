"""Offline Telegram tests. Every HTTP exchange uses a mock transport."""
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

import httpx

from telegram_bot import TelegramBot, chunks, parse_board_document, parse_board_text


TOKEN = '123456789:UNIT_TEST_TOKEN_NOT_A_REAL_CREDENTIAL'
USER_ID = 123456


class FakeStore:
    def __init__(self, path):
        self.database_path = str(path)
        self.boards_data = []
        self.entries_data = []
        self.settlements = []

    def create_board(self, legs, source='manual'):
        if not isinstance(legs, list) or not legs:
            raise ValueError('A board needs legs.')
        board = {'id': f'board-{len(self.boards_data) + 1}', 'legs': legs, 'source': source}
        self.boards_data.append(board)
        return board

    def get_board(self, board_id):
        return next((b for b in self.boards_data if b['id'] == board_id), None)

    def boards(self):
        return self.boards_data

    def create_entry(self, legs, stake, entry_type, payout_multiplier=None, source='manual'):
        entry = {'id': f'entry-{len(self.entries_data) + 1}', 'legs': legs, 'stake': stake,
                 'entry_type': entry_type, 'payout_multiplier': payout_multiplier, 'source': source}
        self.entries_data.append(entry)
        return entry

    def list_entries(self):
        return self.entries_data

    def mark_settlement(self, entry_id, payout, note):
        result = {'entry_id': entry_id, 'actual_payout': payout, 'note': note}
        self.settlements.append(result)
        return result

    def ledger_summary(self):
        return {'entries': len(self.entries_data), 'confirmed_settlements': len(self.settlements)}

    def status(self):
        return {'feeds': []}

    def games(self, date=None):
        return []

    def recent_news(self, limit=10):
        return []


def update(number, text='/help', user=USER_ID, chat_type='private', chat_id=None):
    return {'update_id': number, 'message': {'message_id': number,
            'from': {'id': user, 'is_bot': False},
            'chat': {'id': user if chat_id is None else chat_id, 'type': chat_type}, 'text': text}}


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = FakeStore(Path(self.directory.name) / 'master.sqlite')
        self.received_questions = []

        async def answer(text, history):
            self.received_questions.append(text)
            return 'A researched answer with source timestamps.'

        self.answer = answer
        self.bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID])

    async def asyncTearDown(self):
        self.directory.cleanup()

    def rows(self, table):
        db = sqlite3.connect(self.store.database_path)
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]
        finally:
            db.close()

    async def test_fail_closed_without_token_or_allowlist(self):
        missing_token = TelegramBot(self.store, self.answer, token='', allowed_user_ids=[USER_ID])
        missing_user = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[])
        self.assertFalse(missing_token.enabled)
        self.assertFalse(missing_user.enabled)
        self.assertFalse(await missing_token.notify('Should never send'))
        await missing_user.run()
        self.assertEqual(self.rows('telegram_outbox'), [])

    async def test_only_allowlisted_private_chat_can_invoke_commands(self):
        self.assertFalse(await self.bot.process_update(update(1, user=999)))
        self.assertFalse(await self.bot.process_update(update(2, chat_type='group', chat_id=-100)))
        self.assertFalse(await self.bot.process_update(update(3, chat_id=999)))
        self.assertEqual(self.rows('telegram_outbox'), [])
        self.assertTrue(await self.bot.process_update(update(4, text='How is Brunson?')))
        self.assertEqual(self.received_questions, ['How is Brunson?'])
        self.assertEqual(len(self.rows('telegram_outbox')), 1)
        self.assertEqual(self.bot.status()['offset'], 5)

    async def test_board_is_distinct_from_placed_entry_and_update_replay_is_ignored(self):
        command = '/board 2026-10-29\nJalen Brunson | points | more | 24.5\nJosh Hart | rebounds | less | 8.5'
        await self.bot.process_update(update(10, command))
        self.assertEqual(len(self.store.boards_data), 1)
        self.assertEqual(self.store.entries_data, [])
        reopened = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID])
        self.assertFalse(await reopened.process_update(update(10, command)))
        self.assertEqual(len(self.store.boards_data), 1)
        await reopened.process_update(update(11, '/track board-1 10 power 3'))
        self.assertEqual(len(self.store.entries_data), 1)
        self.assertEqual(self.store.entries_data[0]['stake'], 10)
        self.assertEqual(self.store.entries_data[0]['payout_multiplier'], 3)
        await reopened.process_update(update(12, '/settle entry-1 30 confirmed in app'))
        self.assertEqual(self.store.settlements[0]['actual_payout'], 30)

    async def test_durable_outbox_dedup_delivery_and_retry(self):
        await self.bot.notify('Injury status changed', event_key='injury:1:v2')
        await self.bot.notify('Injury status changed', event_key='injury:1:v2')
        self.assertEqual(len(self.rows('telegram_outbox')), 1)
        fail = True
        delivered = []

        def transport(request):
            nonlocal fail
            if fail:
                return httpx.Response(500, json={'ok': False, 'error_code': 500})
            delivered.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 55}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            self.bot.client = client
            await self.bot.flush_outbox()
            row = self.rows('telegram_outbox')[0]
            self.assertEqual(row['status'], 'pending')
            self.assertEqual(row['attempts'], 1)
            self.assertNotIn(TOKEN, self.bot.status()['last_error'])
            fail = False
            with self.bot._db() as db:
                db.execute('UPDATE telegram_outbox SET next_attempt=0')
            reopened = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID], client=client)
            await reopened.flush_outbox()
            self.assertEqual(self.rows('telegram_outbox')[0]['status'], 'delivered')
            await reopened.flush_outbox()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['chat_id'], USER_ID)

    async def test_interrupted_command_is_not_replayed(self):
        with self.bot._db() as db:
            db.execute('INSERT INTO telegram_updates(update_id,chat_id,status,received_at) VALUES (?,?,?,?)',
                       (30, USER_ID, 'processing', '2026-10-29T00:00:00Z'))
        reopened = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID])
        reopened._recover_interrupted()
        self.assertEqual(self.rows('telegram_updates')[0]['status'], 'interrupted')
        self.assertFalse(await reopened.process_update(update(30, '/entry {}')))
        self.assertEqual(len(self.rows('telegram_outbox')), 1)
        self.assertIn('Check /boards and /entries', self.rows('telegram_outbox')[0]['text'])

    async def test_analysis_uses_deterministic_engine_and_natural_language_uses_callback(self):
        board = self.store.create_board([{'player': 'Jalen Brunson', 'stat': 'pts', 'line': 24.5,
                                         'side': 'more', 'date': '2026-10-29'}])
        observed = []

        def analyze(store, board_id):
            observed.append(board_id)
            return {'board_id': board_id, 'status': 'research_only', 'sample_size': 4}

        with patch.dict('sys.modules', {'analysis_engine': types.SimpleNamespace(analyze_board=analyze)}):
            await self.bot.process_update(update(40, '/analyze ' + board['id']))
        self.assertEqual(observed, [board['id']])
        self.assertEqual(self.received_questions, [])
        self.assertIn('sample_size', self.rows('telegram_outbox')[0]['text'])

    async def test_api_errors_do_not_expose_token_urls_or_provider_error_text(self):
        def transport(request):
            raise httpx.ConnectError('failed URL ' + str(request.url), request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            self.bot.client = client
            with self.assertRaises(RuntimeError) as error:
                await self.bot._api('getUpdates', {})
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertNotIn('https://', str(error.exception))
        self.assertGreaterEqual(logging.getLogger('httpx').level, logging.WARNING)

    async def test_diagnose_returns_safe_report_without_model_or_custom_urls(self):
        calls = []

        async def diagnose():
            calls.append(True)
            return [{'source': 'espn_scoreboard', 'status_code': 403,
                     'content_type': 'text/html', 'category': 'access_denied'}]

        with patch.dict('sys.modules', {'feed_diagnostics': types.SimpleNamespace(diagnose_feeds=diagnose)}):
            await self.bot.process_update(update(70, '/diagnose'))
            await self.bot.process_update(update(71, '/diagnose https://user-supplied.example'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.received_questions, [])
        messages = self.rows('telegram_outbox')
        self.assertIn('espn_scoreboard', messages[0]['text'])
        self.assertIn('403', messages[0]['text'])
        self.assertIn('custom URLs are not accepted', messages[1]['text'])

    async def test_summary_defaults_to_injuries_and_uses_collected_reports_without_ai(self):
        calls = []

        def summary(store, *, topic='injuries', limit=5):
            calls.append((store, topic, limit))
            return f'Collected {topic} summary with source timestamps.'

        with patch.dict('sys.modules', {'news_digest': types.SimpleNamespace(news_summary=summary)}):
            await self.bot.process_update(update(110, '/summary'))
            await self.bot.process_update(update(111, '/summary news'))
            await self.bot.process_update(update(112, '/summary trades'))
            await self.bot.process_update(update(113, '/summary unsupported'))
        self.assertEqual([(topic, limit) for _, topic, limit in calls],
                         [('injuries', 5), ('news', 5), ('trades', 5)])
        self.assertTrue(all(store is self.store for store, _, _ in calls))
        self.assertEqual(self.received_questions, [])
        messages = self.rows('telegram_outbox')
        self.assertEqual(messages[0]['text'], 'Collected injuries summary with source timestamps.')
        self.assertEqual(messages[1]['text'], 'Collected news summary with source timestamps.')
        self.assertIn('/summary trades', messages[3]['text'])

    async def test_selftest_is_owner_only_and_never_invokes_ai(self):
        calls = []
        async def self_test():
            calls.append(True)
            return 'Self-test: 12 passed, 0 failed. Isolated fixtures only.'
        bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID],
                          self_test_callback=self_test)
        self.assertFalse(await bot.process_update(update(145, '/selftest', user=999)))
        self.assertFalse(await bot.process_update(update(146, '/selftest', chat_type='group', chat_id=-100)))
        await bot.process_update(update(147, '/selftest extra'))
        self.assertEqual(calls, [])
        await bot.process_update(update(148, '/selftest'))
        self.assertEqual(calls, [True])
        self.assertEqual(self.received_questions, [])
        self.assertIn('12 passed', self.rows('telegram_outbox')[-1]['text'])
        await self.bot.process_update(update(149, '/selftest'))
        self.assertIn('not configured', self.rows('telegram_outbox')[-1]['text'])

    async def test_playerlog_is_private_and_does_not_invoke_model(self):
        calls = []
        def player_log(store, argument):
            calls.append((store, argument))
            return 'Fixture direct calculation: average 24.00 points'
        with patch.dict('sys.modules', {'player_stats': types.SimpleNamespace(player_log_command=player_log)}):
            self.assertFalse(await self.bot.process_update(update(160, '/playerlog Brunson | pts | 5', user=999)))
            self.assertFalse(await self.bot.process_update(update(161, '/playerlog Brunson | pts | 5',
                                                                chat_type='group', chat_id=-100)))
            await self.bot.process_update(update(162, '/playerlog'))
            self.assertEqual(calls, [])
            await self.bot.process_update(update(163, '/playerlog Brunson | pts | 5'))
        self.assertEqual(calls, [(self.store, 'Brunson | pts | 5')])
        self.assertEqual(self.received_questions, [])
        self.assertIn('24.00', self.rows('telegram_outbox')[-1]['text'])

    async def test_ai_status_uses_only_safe_local_metadata_and_does_not_call_model(self):
        calls = []

        async def model_status():
            calls.append(True)
            return {'phase': 'ready', 'model': 'qwen3:4b', 'busy': True, 'active': 'private-job-id',
                    'last_failure_category': 'timeout', 'last_failure_time': '2026-10-29T12:00:00Z',
                    'active_elapsed_seconds': 42.25, 'last_success_time': '2026-10-29T11:30:00Z',
                    'source_ref': 'a' * 40,
                    'last_failure': {'category': 'timeout', 'at': '2026-10-29T12:00:00Z',
                                     'text': 'PRIVATE ERROR BODY'},
                    'secret': TOKEN, 'answer': 'PRIVATE GENERATED TEXT'}

        bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID],
                          model_status_callback=model_status)
        await bot.process_update(update(114, '/ai_status'))
        await bot.process_update(update(115, '/ai_status extra'))
        self.assertEqual(calls, [True])
        self.assertEqual(self.received_questions, [])
        response = self.rows('telegram_outbox')[0]['text']
        self.assertIn('qwen3:4b', response)
        self.assertIn('timeout', response)
        self.assertIn('"busy": true', response)
        self.assertIn('"active_elapsed_seconds": 42.2', response)
        self.assertIn('2026-10-29T11:30:00Z', response)
        self.assertIn('a' * 40, response)
        for private in [TOKEN, 'private-job-id', 'PRIVATE ERROR BODY', 'PRIVATE GENERATED TEXT']:
            self.assertNotIn(private, response)
        await self.bot.process_update(update(116, '/ai_status'))
        self.assertIn('not configured', self.rows('telegram_outbox')[-1]['text'])

    async def test_summary_and_ai_status_are_private_and_listed_in_help(self):
        summary_calls, status_calls = [], []

        def summary(store, **kwargs):
            summary_calls.append(True)
            return 'Private news summary'

        async def model_status():
            status_calls.append(True)
            return {'phase': 'ready'}

        bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID],
                          model_status_callback=model_status)
        with patch.dict('sys.modules', {'news_digest': types.SimpleNamespace(news_summary=summary)}):
            for number, command in enumerate(['/summary', '/ai_status'], 120):
                self.assertFalse(await bot.process_update(update(number, command, user=999)))
                self.assertFalse(await bot.process_update(update(number + 10, command, chat_type='group', chat_id=-100)))
        self.assertEqual(summary_calls, [])
        self.assertEqual(status_calls, [])
        self.assertEqual(self.rows('telegram_outbox'), [])
        await bot.process_update(update(140, '/help'))
        help_text = ''.join(row['text'] for row in self.rows('telegram_outbox'))
        self.assertIn('/summary [injuries|news|trades]', help_text)
        self.assertIn('/ai_status', help_text)

    async def test_diagnose_cannot_be_invoked_by_unauthorized_users_or_groups(self):
        calls = []

        async def diagnose():
            calls.append(True)
            return []

        with patch.dict('sys.modules', {'feed_diagnostics': types.SimpleNamespace(diagnose_feeds=diagnose)}):
            self.assertFalse(await self.bot.process_update(update(72, '/diagnose', user=999)))
            self.assertFalse(await self.bot.process_update(update(73, '/diagnose', chat_type='group', chat_id=-100)))
        self.assertEqual(calls, [])
        self.assertEqual(self.rows('telegram_outbox'), [])

    async def test_stats_document_requires_explicit_caption_and_keeps_boards_separate(self):
        observed = []
        stats = [{'game_id': 'nba-test-1', 'player': 'Jalen Brunson', 'date': '2026-10-29', 'pts': 25}]
        board = [{'player': 'Jalen Brunson', 'stat': 'pts', 'line': 24.5,
                  'side': 'more', 'date': '2026-10-29'}]

        async def download(file_id, declared_size=0):
            return b'game_id,player,date,pts\nnba-test-1,Jalen Brunson,2026-10-29,25\n' if file_id == 'stats-file' else json.dumps(board).encode()

        def parse(filename, payload):
            observed.append(('parse', filename))
            return stats

        def import_rows(store, rows, source='Manual user upload'):
            observed.append(('import', rows, source))
            return {'imported_rows': len(rows), 'source': source}

        stats_message = update(80)
        stats_message['message'].update(document={'file_id': 'stats-file', 'file_name': 'boxscores.csv'}, caption='/importstats')
        board_message = update(81)
        board_message['message'].update(document={'file_id': 'board-file', 'file_name': 'board.json'})
        with patch.object(self.bot, '_download', download), patch.dict('sys.modules', {
                'manual_import': types.SimpleNamespace(parse_stats_document=parse, import_stats=import_rows)}):
            await self.bot.process_update(stats_message)
            await self.bot.process_update(board_message)
        self.assertEqual([call[0] for call in observed], ['parse', 'import'])
        self.assertEqual(observed[1][1], stats)
        self.assertTrue(observed[1][2].startswith('Manual user upload'))
        self.assertEqual(len(self.store.boards_data), 1)
        self.assertEqual(self.store.entries_data, [])
        messages = self.rows('telegram_outbox')
        self.assertIn('Manual statistics import complete', messages[0]['text'])
        self.assertIn('not independently verified', messages[0]['text'])
        self.assertIn('PrizePicks board saved', messages[1]['text'])

    async def test_manual_stat_and_news_commands_are_authorized_and_do_not_broadcast(self):
        imported, notes, downloads = [], [], []

        def import_rows(store, rows, source='Manual user upload'):
            imported.append((rows, source))
            return {'imported_rows': len(rows)}

        def record_note(store, title, detail='', url='', team='NYK'):
            notes.append((title, detail, team))
            return {'title': title, 'body': detail, 'source': 'Manual user note', 'verification': 'unverified'}

        async def download(file_id, declared_size=0):
            downloads.append(file_id)
            return b'[]'

        async def forbidden_notification(*args, **kwargs):
            self.fail('A manual note must not automatically broadcast a notification.')

        document = update(82, user=999)
        document['message'].update(document={'file_id': 'not-authorized', 'file_name': 'stats.json'}, caption='/importstats')
        with patch.dict('sys.modules', {'manual_import': types.SimpleNamespace(
                import_stats=import_rows, record_manual_news=record_note)}), \
                patch.object(self.bot, '_download', download), \
                patch.object(self.bot, 'notify', forbidden_notification):
            self.assertFalse(await self.bot.process_update(document))
            self.assertFalse(await self.bot.process_update(update(83, '/stats []', user=999)))
            self.assertFalse(await self.bot.process_update(update(84, '/newsnote Unverified rumor', user=999)))
            self.assertFalse(await self.bot.process_update(update(85, '/newsnote Group rumor', chat_type='group', chat_id=-100)))
            self.assertEqual(self.rows('telegram_outbox'), [])
            await self.bot.process_update(update(86, '/stats [{"game_id":"nba-test","player":"Jalen Brunson","date":"2026-10-29","pts":25}]'))
            await self.bot.process_update(update(87, '/newsnote Brunson availability note\nPersonal note: verify official injury report.'))
        self.assertEqual(downloads, [])
        self.assertEqual(len(imported), 1)
        self.assertEqual(notes, [('Brunson availability note', 'Personal note: verify official injury report.', 'NYK')])
        self.assertEqual(self.store.boards_data, [])
        self.assertEqual(self.store.entries_data, [])
        self.assertIn('user-supplied/unverified', self.rows('telegram_outbox')[-1]['text'])
        self.assertIn('No automatic alert was broadcast', self.rows('telegram_outbox')[-1]['text'])

    async def test_manual_stats_column_errors_are_safe_and_photo_stays_a_draft(self):
        imports, photos = [], []

        def parse(filename, payload):
            raise ValueError('Missing required columns: game_id, player, date.')

        def import_rows(*args, **kwargs):
            imports.append(True)
            return {}

        async def download(file_id, declared_size=0):
            return b'wrong_column\n123\n'

        async def photo_draft(message, update_id):
            photos.append(update_id)
            return 'Screenshot draft saved; no statistics were imported.'

        bad_file = update(88)
        bad_file['message'].update(document={'file_id': 'bad-stats', 'file_name': 'stats.csv'}, caption='/importstats')
        photo = update(89)
        photo['message'].update(photo=[{'file_id': 'photo-stats', 'file_size': 100}], caption='/importstats')
        with patch.dict('sys.modules', {'manual_import': types.SimpleNamespace(
                parse_stats_document=parse, import_stats=import_rows)}), \
                patch.object(self.bot, '_download', download), \
                patch.object(self.bot, '_handle_photo', photo_draft):
            await self.bot.process_update(bad_file)
            await self.bot.process_update(photo)
            await self.bot.process_update(update(90, '/stats {"pts":25}'))
        self.assertEqual(imports, [])
        self.assertEqual(photos, [89])
        self.assertEqual(self.store.boards_data, [])
        self.assertEqual(self.store.entries_data, [])
        messages = self.rows('telegram_outbox')
        self.assertIn('Missing required columns: game_id, player, date', messages[0]['text'])
        self.assertNotIn(TOKEN, messages[0]['text'])
        self.assertIn('Screenshot draft', messages[1]['text'])
        self.assertIn('JSON list of box-score rows', messages[2]['text'])

    async def test_slow_model_does_not_block_alerts_or_status_commands(self):
        started, release = asyncio.Event(), asyncio.Event()
        alert_delivered, status_delivered = asyncio.Event(), asyncio.Event()
        polls = 0

        async def slow_answer(text, history):
            started.set()
            await release.wait()
            return 'Delayed answer'

        async def transport(request):
            nonlocal polls
            if request.url.path.endswith('/getUpdates'):
                polls += 1
                await asyncio.sleep(.01)
                if polls == 1:
                    result = [update(100, 'A slow research question')]
                elif polls == 2:
                    result = [update(101, '/status')]
                else:
                    result = []
                return httpx.Response(200, json={'ok': True, 'result': result})
            body = json.loads(request.content)
            if body['text'] == 'Urgent injury update':
                alert_delivered.set()
            if 'Data and worker status' in body['text']:
                status_delivered.set()
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            bot = TelegramBot(self.store, slow_answer, token=TOKEN, allowed_user_ids=[USER_ID], client=client)
            task = asyncio.create_task(bot.run())
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                await bot.notify('Urgent injury update', event_key='urgent-test')
                await asyncio.wait_for(alert_delivered.wait(), timeout=3)
                await asyncio.wait_for(status_delivered.wait(), timeout=3)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class UploadTests(unittest.TestCase):
    def test_readable_board_csv_json_and_strict_game_date(self):
        legs = parse_board_text('2026-10-29\nJalen Brunson | points | more | 24.5\nDerrick White | assists | less | 4.5 | BOS')
        self.assertEqual(legs[0]['stat'], 'pts')
        self.assertEqual(legs[0]['team'], 'NYK')
        self.assertEqual(legs[1]['team'], 'BOS')
        self.assertEqual(parse_board_document('board.json', json.dumps(legs).encode()), legs)
        csv_payload = b'player,stat,line,side,date\nJalen Brunson,pts,24.5,more,2026-10-29\n'
        self.assertEqual(parse_board_document('board.csv', csv_payload)[0]['player'], 'Jalen Brunson')
        for text in ['tomorrow\nBrunson | pts | more | 20', '2026-2-9\nBrunson | pts | more | 20',
                     '2026-10-29\nBrunson | pts | maybe | 20']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_board_text(text)

    def test_message_chunks_fit_telegram_and_preserve_text(self):
        text = 'a' * 12000 + '\nFinal sentence.'
        parts = list(chunks(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part) <= 3500 for part in parts))


if __name__ == '__main__':
    unittest.main()
