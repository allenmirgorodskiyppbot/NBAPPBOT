"""Actual Telegram progress queries stay independent of a running Excel rebuild."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from dataset_update import DatasetUpdateController
from telegram_bot import TelegramBot
import test_dataset_update as update_fixtures
from test_telegram import TOKEN, USER_ID, update


class DatasetUpdateProgressIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = update_fixtures.DatasetUpdateTests(methodName='test_noop_checks_reuse_existing_good_excel_and_qualify_currentness')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.release = asyncio.Event(); self.entered = asyncio.Event()
        self.fixture.file.unlink()
        self.export_calls = 0
        async def export():
            self.export_calls += 1
            self.entered.set()
            await self.release.wait()
            self.fixture.file.write_bytes(b'Completed isolated export fixture')
        self.controller = DatasetUpdateController(self.fixture.nightly, export_refresh=export)
        async def status():
            return await asyncio.to_thread(self.controller.status)
        self.ai = AsyncMock(side_effect=AssertionError('Dataset progress must never invoke AI.'))
        self.bot = TelegramBot(self.store, self.ai, token=TOKEN, allowed_user_ids=[USER_ID],
                               manual_refresh_callback=self.controller.refresh, manual_refresh_status_callback=status)
        self.bot._typing = AsyncMock()
        self.addAsyncCleanup(self.cleanup_job)

    async def cleanup_job(self):
        self.release.set()
        if self.bot._dataset_refresh_tasks:
            await asyncio.gather(*list(self.bot._dataset_refresh_tasks), return_exceptions=True)
        await self.bot.shutdown()

    def latest(self):
        with self.store.connect() as db:
            return db.execute('SELECT text FROM telegram_outbox ORDER BY id DESC LIMIT 1').fetchone()[0]

    async def test_both_status_aliases_answer_during_export_without_another_refresh(self):
        await self.bot.process_update(update(800, '/refresh'))
        await asyncio.wait_for(self.entered.wait(), 2)
        self.assertEqual(self.controller.status()['stage'], 'excel')
        for number, command in ((801, '/refresh status'), (802, '/update status')):
            await asyncio.wait_for(self.bot.process_update(update(number, command)), 1)
            text = self.latest()
            self.assertIn('Dataset refresh is running.', text)
            self.assertIn('Rebuilding the master Excel', text)
            self.assertIn('Elapsed:', text)
        self.assertEqual(self.export_calls, 1)
        self.assertEqual(self.fixture.calls, [('nightly', False)])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_dataset_jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 0)
        self.ai.assert_not_awaited()
        self.release.set()
        await asyncio.gather(*list(self.bot._dataset_refresh_tasks))
        await self.bot.process_update(update(803, '/refresh status'))
        self.assertIn('Last dataset refresh: completed.', self.latest())
        self.assertEqual(self.export_calls, 1)
