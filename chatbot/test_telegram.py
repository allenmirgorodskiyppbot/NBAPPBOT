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
