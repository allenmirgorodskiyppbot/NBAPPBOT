"""Offline regressions for pending replies, conversation clearing and typing."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import telegram_bot
from telegram_bot import TelegramBot
from test_telegram import FakeStore, TOKEN, USER_ID, update


class TelegramLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FakeStore(Path(self.directory.name) / 'telegram.sqlite')

    def bot(self, answer, status=None):
        return TelegramBot(self.store, answer, token=TOKEN, allowed_user_ids=[USER_ID],
                           client=object(), model_status_callback=status)

    async def test_clear_during_pending_answer_does_not_restore_old_history(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(text, history):
            entered.set()
            await release.wait()
            return 'The earlier answer finished.'

        bot = self.bot(answer)
        async def api(method, payload): return True
        bot._api = api
        pending = asyncio.create_task(bot.process_update(update(2000, 'Old topic')))
        await entered.wait()
        await bot.process_update(update(2001, '/clear'))
        self.assertEqual(bot._conversation(USER_ID), [])
        release.set()
        await pending
        self.assertEqual(bot._conversation(USER_ID), [])
        with bot._db() as db:
            replies = [row['text'] for row in db.execute('SELECT text FROM telegram_outbox')]
        self.assertIn('The earlier answer finished.', replies)

    async def test_typing_repeats_during_answer_and_stops_after_completion(self):
        release, repeated = asyncio.Event(), asyncio.Event()
        calls = []

        async def answer(text, history):
            await release.wait()
            return 'Completed reply.'

        async def status(): return {'phase': 'ready', 'active': True}
        bot = self.bot(answer, status)

        async def api(method, payload):
            calls.append((method, payload))
            if len(calls) >= 2:
                repeated.set()
            return True

        bot._api = api
        with patch.object(telegram_bot, 'TYPING_INTERVAL_SECONDS', .01):
            pending = asyncio.create_task(bot.process_update(update(2010, 'Explain this matchup')))
            await asyncio.wait_for(repeated.wait(), 1)
            await asyncio.wait_for(bot.process_update(update(2011, '/ai_status')), 1)
            with bot._db() as db:
                queued = [row['text'] for row in db.execute('SELECT text FROM telegram_outbox')]
            self.assertTrue(any('Local AI status:' in text for text in queued))
            release.set()
            await pending
            count = len(calls)
            await asyncio.sleep(.03)
        self.assertEqual(len(calls), count)
        self.assertGreaterEqual(count, 2)
        self.assertTrue(all(method == 'sendChatAction' and payload == {
            'chat_id': USER_ID, 'action': 'typing'} for method, payload in calls))

    async def test_typing_error_does_not_fail_or_add_messages_to_answer(self):
        attempted = asyncio.Event()

        async def answer(text, history):
            await attempted.wait()
            return 'Successful answer.'

        bot = self.bot(answer)
        async def api(method, payload):
            attempted.set()
            raise RuntimeError('PRIVATE NETWORK DETAIL')
        bot._api = api
        await bot.process_update(update(2020, 'A question'))
        with bot._db() as db:
            queued = [row['text'] for row in db.execute('SELECT text FROM telegram_outbox')]
        self.assertEqual(queued, ['Successful answer.'])
        self.assertEqual(bot._conversation(USER_ID)[-1]['content'], 'Successful answer.')

    async def test_parent_cancellation_stops_inflight_typing_and_preserves_interrupted_marker(self):
        typing_entered, typing_cancelled = asyncio.Event(), asyncio.Event()

        async def answer(text, history): await asyncio.Event().wait()
        bot = self.bot(answer)
        async def api(method, payload):
            typing_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                typing_cancelled.set()
        bot._api = api
        pending = asyncio.create_task(bot.process_update(update(2030, 'Pending research')))
        await asyncio.wait_for(typing_entered.wait(), 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertTrue(typing_cancelled.is_set())
        with bot._db() as db:
            status = db.execute('SELECT status FROM telegram_updates WHERE update_id=2030').fetchone()[0]
        self.assertEqual(status, 'processing')

    async def test_unauthorized_input_never_triggers_typing_or_answer(self):
        async def answer(text, history): self.fail('Unauthorized inference')
        bot = self.bot(answer)
        async def api(method, payload): self.fail('Unauthorized typing')
        bot._api = api
        self.assertFalse(await bot.process_update(update(2040, 'Private question', user=USER_ID + 1)))
        await bot._typing(USER_ID + 1)

    async def test_answer_exception_also_cancels_typing(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def answer(text, history):
            await entered.wait()
            raise RuntimeError('PRIVATE ANSWER FAILURE')
        bot = self.bot(answer)
        async def api(method, payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        bot._api = api
        await bot.process_update(update(2050, 'A question'))
        self.assertTrue(cancelled.is_set())
        with bot._db() as db:
            queued = [row['text'] for row in db.execute('SELECT text FROM telegram_outbox')]
        self.assertEqual(len(queued), 1)
        self.assertNotIn('PRIVATE', queued[0])


if __name__ == '__main__':
    unittest.main()
