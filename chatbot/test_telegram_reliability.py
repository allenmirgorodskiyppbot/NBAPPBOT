"""Offline regressions for delivery recovery and overloaded command polling."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import telegram_bot
from telegram_bot import TelegramBot, TelegramAPIError
from test_telegram import FakeStore, TOKEN, USER_ID, update


class TelegramReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FakeStore(Path(self.directory.name) / 'master.sqlite')

    def bot(self, answer=None, **kwargs):
        async def unused(text, history):
            self.fail('Unexpected inference')
        return TelegramBot(self.store, answer or unused, token=TOKEN,
                           allowed_user_ids=[USER_ID], **kwargs)

    def outbox(self, bot):
        with bot._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox ORDER BY id')]

    async def test_rate_limit_cooldown_survives_restart_and_protects_new_messages(self):
        calls = []
        limited = True

        def transport(request):
            calls.append(json.loads(request.content))
            if limited:
                return httpx.Response(429, json={'ok': False, 'error_code': 429,
                                                'parameters': {'retry_after': 240},
                                                'description': TOKEN})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(calls)}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            bot = self.bot(client=client)
            await bot.notify('First reply', event_key='reply:first')
            with patch.object(telegram_bot.time, 'time', return_value=1000):
                await bot.flush_outbox()
            row = self.outbox(bot)[0]
            self.assertEqual(row['next_attempt'], 1240)
            self.assertEqual(row['attempts'], 1)
            self.assertNotIn(TOKEN, str(bot.status()))
            restarted = self.bot(client=client)
            await restarted.notify('Later reply', event_key='reply:later')
            with patch.object(telegram_bot.time, 'time', return_value=1100):
                await restarted.flush_outbox()
            self.assertEqual(len(calls), 1)
            limited = False
            with patch.object(telegram_bot.time, 'time', return_value=1241):
                await restarted.flush_outbox()
            self.assertEqual([row['status'] for row in self.outbox(bot)], ['delivered', 'delivered'])
            self.assertIsNone(restarted.status()['last_delivery_error'])
            self.assertIsNone(restarted.status()['last_error'])
            self.assertEqual(len(calls), 3)

    async def test_poll_success_does_not_hide_pending_delivery_failure(self):
        bot = self.bot()
        await bot.notify('Failed reply', event_key='reply:failure')

        async def api(method, payload):
            raise RuntimeError('Private network message')

        bot._api = api
        await bot.flush_outbox()
        bot._set_state('last_error', '')  # A subsequent successful poll.
        self.assertIn('queued for retry', bot.status()['last_error'])
        self.assertIsNone(bot.status()['last_poll_error'])
        self.assertEqual(bot.status()['pending_messages'], 1)
        self.assertNotIn('Private', str(bot.status()))

    async def test_overlapping_outbox_flushes_send_each_message_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        sends = []
        bot = self.bot()
        await bot.notify('One reply', event_key='reply:once')

        async def api(method, payload):
            sends.append(payload['text'])
            started.set()
            await release.wait()
            return {'message_id': 1}

        bot._api = api
        first = asyncio.create_task(bot.flush_outbox())
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(bot.flush_outbox())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(sends, ['One reply'])
        self.assertEqual(self.outbox(bot)[0]['status'], 'delivered')

    async def test_api_malformed_errors_never_leak_provider_data(self):
        responses = [[], {'ok': False, 'error_code': TOKEN, 'description': TOKEN},
                     {'ok': False, 'error_code': 429, 'parameters': {'retry_after': TOKEN}}]
        for response in responses:
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(429, json=response))) as client:
                    bot = self.bot(client=client)
                    with self.assertRaises(RuntimeError) as raised:
                        await bot._api('sendMessage', {})
                    self.assertNotIn(TOKEN, str(raised.exception))
                    if isinstance(raised.exception, TelegramAPIError):
                        self.assertIsNone(raised.exception.retry_after)

    async def test_full_command_capacity_keeps_polling_help_and_status_responsive(self):
        continued = asyncio.Event()
        pending_answers = []
        polls = 0

        async def answer(text, history):
            pending_answers.append(text)
            await asyncio.Event().wait()

        async def model_status():
            return {'phase': 'ready', 'busy': True}

        bot = self.bot(answer, client=object(), model_status_callback=model_status)
        entry = '/entry ' + json.dumps({'legs': [{}, {}], 'stake': 10, 'entry_type': 'power'})

        async def api(method, payload):
            nonlocal polls
            if method != 'getUpdates':
                return {'message_id': 1}
            polls += 1
            if polls == 1:
                return [update(3000, 'Slow request one'), update(3001, 'Slow request two')]
            if polls == 2:
                return [update(3002, entry), update(3003, '/status'), update(3004, '/help'),
                        update(3005, '/ai_status')]
            continued.set()
            await asyncio.Event().wait()

        bot._api = api
        with patch.object(telegram_bot, 'MAX_ACTIVE_COMMANDS', 2):
            task = asyncio.create_task(bot.run())
            try:
                await asyncio.wait_for(continued.wait(), 2)
                replies = '\n'.join(row['text'] for row in self.outbox(bot))
                self.assertIn('This request was not started', replies)
                self.assertIn('Data and worker status:', replies)
                self.assertIn('Knicks Research bot', replies)
                self.assertIn('Local AI status:', replies)
                self.assertEqual(len(pending_answers), 2)
                self.assertEqual(self.store.entries_data, [])
                self.assertEqual(bot.status()['offset'], 3006)
                self.assertFalse(await bot.process_update(update(3002, entry)))
                self.assertEqual(self.store.entries_data, [])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_delivery_worker_recovers_when_failure_diagnostic_cannot_be_written(self):
        recovered = asyncio.Event()
        attempts = 0
        bot = self.bot(client=object())
        original_set_state = bot._set_state

        def set_state(name, value):
            if name == 'last_delivery_error':
                raise OSError('Temporary storage failure')
            original_set_state(name, value)

        async def flush():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError('Temporary storage failure')
            recovered.set()

        async def api(method, payload):
            await asyncio.Event().wait()

        bot._set_state = set_state
        bot.flush_outbox = flush
        bot._api = api
        task = asyncio.create_task(bot.run())
        try:
            await asyncio.wait_for(recovered.wait(), 2)
            self.assertEqual(attempts, 2)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_owned_client_close_failure_does_not_poison_next_run(self):
        polled = asyncio.Event()

        class Client:
            def __init__(self, *, broken=False):
                self.broken = broken
                self.closed = False

            async def aclose(self):
                self.closed = True
                if self.broken:
                    raise OSError('Temporary close failure')

        first, replacement = Client(broken=True), Client()
        bot = self.bot(client=first)
        bot._owns_client = True

        async def api(method, payload):
            polled.set()
            await asyncio.Event().wait()

        bot._api = api
        initial = asyncio.create_task(bot.run())
        await asyncio.wait_for(polled.wait(), 1)
        initial.cancel()
        result = (await asyncio.gather(initial, return_exceptions=True))[0]
        self.assertIsInstance(result, OSError)
        self.assertTrue(first.closed)
        self.assertIsNone(bot.client)
        self.assertFalse(bot._owns_client)
        polled.clear()
        with patch.object(telegram_bot.httpx, 'AsyncClient', return_value=replacement) as factory:
            restarted = asyncio.create_task(bot.run())
            try:
                await asyncio.wait_for(polled.wait(), 1)
                factory.assert_called_once()
                self.assertIs(bot.client, replacement)
            finally:
                restarted.cancel()
                await asyncio.gather(restarted, return_exceptions=True)
        self.assertTrue(replacement.closed)
        self.assertIsNone(bot.client)
        self.assertFalse(bot._owns_client)

    async def test_restart_clears_cancelled_album_collector_for_later_fragments(self):
        continued = asyncio.Event()
        polls = 0
        bot = self.bot(client=object())

        def photo(number):
            value = update(number, '')
            value['message'].update(media_group_id='album-one', photo=[{'file_id': str(number)}])
            return value

        async def api(method, payload):
            nonlocal polls
            if method != 'getUpdates':
                return True
            polls += 1
            if polls == 1:
                return [photo(3100)]
            continued.set()
            await asyncio.Event().wait()

        async def process_photos(items):
            return 'Combined image research'

        bot._api = api
        bot._process_photos = process_photos
        task = asyncio.create_task(bot.run())
        await asyncio.wait_for(continued.wait(), 1)
        self.assertTrue(bot._album_batches)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(bot._album_batches, {})
        self.assertEqual(bot._album_tasks, set())
        bot._recover_interrupted()
        self.assertTrue(await asyncio.wait_for(bot.process_update(photo(3101)), 2))
        replies = [row['text'] for row in self.outbox(bot)]
        self.assertEqual(replies.count('Combined image research'), 1)


if __name__ == '__main__':
    unittest.main()
