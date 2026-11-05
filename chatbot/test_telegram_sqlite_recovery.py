"""Actual SQLite writer contention must not cancel independent data updates."""
import asyncio
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master import MasterStore
from telegram_bot import TelegramBot, sqlite_contention
from test_telegram import TOKEN, USER_ID, update


class TelegramSQLiteRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'master.sqlite'; self.store = MasterStore(self.path)
        self.bot = TelegramBot(self.store, AsyncMock(return_value='Normal reply.'), token=TOKEN,
                               allowed_user_ids=[USER_ID], client=object())
        self.bot._typing = AsyncMock()
        self.writer = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=.1)
        self.addCleanup(self.writer.close)
        self.addAsyncCleanup(self.bot.shutdown)

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    async def begin_writer(self):
        await asyncio.to_thread(self.writer.execute, 'BEGIN IMMEDIATE')

    async def finish_jobs(self):
        if self.bot._dataset_refresh_tasks:
            await asyncio.gather(*list(self.bot._dataset_refresh_tasks))

    async def test_real_busy_receipt_retries_off_loop_and_preserves_offset_deduplication(self):
        await self.begin_writer()
        task = asyncio.create_task(self.bot.process_update(update(1, '/help')))
        ticks = 0
        for _ in range(12):
            await asyncio.sleep(.02); ticks += 1
        self.assertFalse(task.done())
        self.assertGreaterEqual(ticks, 10)
        self.assertEqual(self.bot._database_health['sqlite_errorcode'] & 255, sqlite3.SQLITE_BUSY)
        self.assertEqual(self.rows('telegram_updates'), [])
        self.writer.rollback()
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertEqual(self.rows('telegram_updates')[0]['status'], 'done')
        self.assertEqual(self.bot._get_state('offset'), '2')
        count = len(self.rows('telegram_outbox'))
        self.assertFalse(await self.bot.process_update(update(1, '/help')))
        self.assertEqual(len(self.rows('telegram_outbox')), count)

    async def test_batch_receipts_and_max_offset_commit_atomically_before_dispatch(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON telegram_updates WHEN NEW.update_id=102 "
                       "BEGIN SELECT RAISE(ABORT,'fixture rejection'); END")
        batch = [update(101, '/help'), update(102, '/help')]
        with self.assertRaises(sqlite3.IntegrityError):
            await self.bot._retry_db(self.bot._receive_batch, batch)
        self.assertEqual(self.rows('telegram_updates'), [])
        self.assertEqual(self.bot._get_state('offset', '0'), '0')
        with self.store.connect() as db: db.execute('DROP TRIGGER reject_second')
        received = await self.bot._retry_db(self.bot._receive_batch, batch)
        self.assertEqual(received, {101, 102})
        self.assertEqual(self.bot._get_state('offset'), '103')
        self.assertEqual({r['update_id'] for r in self.rows('telegram_updates')}, {101, 102})
        self.assertEqual(await self.bot._retry_db(self.bot._receive_batch, batch), set())

    async def test_busy_poll_status_write_does_not_restart_or_interrupt_manual_job(self):
        started, release_job, locked, next_poll = (asyncio.Event() for _ in range(4))
        async def refresh():
            started.set(); await release_job.wait(); return 'Refresh completed after writer released.'
        self.bot.manual_refresh_callback = refresh
        await self.bot.process_update(update(10, '/refresh')); await started.wait()
        polls = 0
        async def api(method, payload):
            nonlocal polls
            if method != 'getUpdates': return True
            polls += 1
            if polls == 1:
                await self.begin_writer(); locked.set(); return []
            next_poll.set(); await asyncio.Event().wait()
        self.bot._api = api
        service = asyncio.create_task(self.bot.run())
        try:
            await asyncio.wait_for(locked.wait(), 2)
            await asyncio.sleep(.25)
            self.assertFalse(service.done())
            self.assertTrue(self.bot._dataset_refresh_tasks)
            self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'running')
            self.assertEqual(self.bot._database_health['sqlite_errorcode'] & 255, sqlite3.SQLITE_BUSY)
            self.assertFalse(any('interrupted' in r['text'] for r in self.rows('telegram_outbox')))
            self.writer.rollback(); release_job.set()
            await asyncio.wait_for(next_poll.wait(), 2); await self.finish_jobs()
            self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'completed')
        finally:
            self.writer.rollback(); release_job.set(); service.cancel()
            await asyncio.gather(service, return_exceptions=True)

    async def test_polling_restart_keeps_same_instance_job_and_recovery_does_not_relabel_it(self):
        started, release, polling = asyncio.Event(), asyncio.Event(), asyncio.Event()
        refresh = AsyncMock()
        async def work(): started.set(); await release.wait(); return 'Independent refresh finished.'
        refresh.side_effect = work; self.bot.manual_refresh_callback = refresh
        async def api(method, payload):
            if method == 'getUpdates': polling.set(); await asyncio.Event().wait()
            return True
        self.bot._api = api
        await self.bot.process_update(update(20, '/refresh')); await started.wait()
        first = asyncio.create_task(self.bot.run()); await asyncio.wait_for(polling.wait(), 2)
        first.cancel(); await asyncio.gather(first, return_exceptions=True)
        self.assertTrue(self.bot._dataset_refresh_tasks)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'running')
        polling.clear(); second = asyncio.create_task(self.bot.run())
        try:
            await asyncio.wait_for(polling.wait(), 2)
            self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'running')
            refresh.assert_awaited_once()
            release.set(); await self.finish_jobs()
            self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'completed')
            self.assertFalse(any('interrupted' in r['text'] for r in self.rows('telegram_outbox')))
        finally:
            release.set(); second.cancel(); await asyncio.gather(second, return_exceptions=True)

    async def test_known_successful_send_is_not_resent_while_delivery_mark_waits_for_writer(self):
        locked = asyncio.Event(); sent = []
        await self.bot.notify('One durable reply.', event_key='real-writer-send')
        async def api(method, payload):
            sent.append(payload['text']); await self.begin_writer(); locked.set(); return True
        self.bot._api = api
        sending = asyncio.create_task(self.bot.flush_outbox())
        try:
            await asyncio.wait_for(locked.wait(), 2); await asyncio.sleep(.25)
            self.assertEqual(sent, ['One durable reply.']); self.assertFalse(sending.done())
            self.writer.rollback(); await asyncio.wait_for(sending, 2)
            self.assertEqual(self.rows('telegram_outbox')[0]['status'], 'delivered')
            await self.bot.flush_outbox(); self.assertEqual(sent, ['One durable reply.'])
        finally:
            self.writer.rollback()
            if not sending.done(): sending.cancel(); await asyncio.gather(sending, return_exceptions=True)

    async def test_known_send_survives_sender_cancellation_before_busy_mark_is_saved(self):
        locked = asyncio.Event(); sent = []
        await self.bot.notify('Keep a known delivery across polling restart.', event_key='known-before-restart')
        async def api(method, payload):
            sent.append(payload['text']); await self.begin_writer(); locked.set(); return True
        self.bot._api = api
        sending = asyncio.create_task(self.bot.flush_outbox())
        await asyncio.wait_for(locked.wait(), 2); await asyncio.sleep(.15)
        sending.cancel(); await asyncio.gather(sending, return_exceptions=True)
        self.assertTrue(self.bot._known_deliveries)
        resumed = asyncio.create_task(self.bot.flush_outbox())
        await asyncio.sleep(.15); self.assertEqual(len(sent), 1)
        self.writer.rollback(); await asyncio.wait_for(resumed, 2)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.rows('telegram_outbox')[0]['status'], 'delivered')

    async def test_readonly_is_not_retried_or_hidden_and_does_not_cancel_manual_job(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def refresh(): started.set(); await release.wait(); return 'Data update remained owned.'
        self.bot.manual_refresh_callback = refresh
        await self.bot.process_update(update(30, '/refresh')); await started.wait()
        readonly_calls = []
        def readonly(values):
            readonly_calls.append(True)
            db = sqlite3.connect('file:'+str(self.path)+'?mode=ro', uri=True)
            try: db.execute("INSERT INTO telegram_state VALUES ('readonly-proof','x')")
            finally: db.close()
        async def api(method, payload):
            if method == 'getUpdates': return []
            return True
        self.bot._api = api
        with patch.object(self.bot, '_transport_states', side_effect=readonly):
            service = asyncio.create_task(self.bot.run())
            with self.assertRaises(sqlite3.OperationalError) as raised:
                await asyncio.wait_for(service, 2)
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        self.assertFalse(sqlite_contention(raised.exception))
        self.assertEqual(self.bot._database_health['phase'], 'failed')
        self.assertTrue(self.bot._dataset_refresh_tasks)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'running')
        self.assertLessEqual(len(readonly_calls), 2)
        release.set(); await self.finish_jobs()

    async def test_full_and_unclassified_operational_errors_are_not_busy(self):
        path = Path(self.temp.name)/'full.sqlite'
        db = sqlite3.connect(path, check_same_thread=False)
        db.execute('CREATE TABLE payload (value BLOB)'); db.commit()
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute('PRAGMA max_page_count='+str(pages))
        calls = []
        def full(): calls.append(True); db.execute('INSERT INTO payload VALUES (zeroblob(1000000))')
        try:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                await self.bot._retry_db(full)
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
            self.assertFalse(sqlite_contention(raised.exception)); self.assertEqual(len(calls), 1)
            self.assertFalse(sqlite_contention(sqlite3.OperationalError('database is locked')))
        finally: db.close()

    async def test_actual_shutdown_cancels_and_joins_manual_jobs_deliberately(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def refresh():
            started.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: cancelled.set(); raise
        self.bot.manual_refresh_callback = refresh
        await self.bot.process_update(update(40, '/refresh')); await started.wait()
        await self.bot.shutdown()
        self.assertTrue(cancelled.is_set()); self.assertEqual(self.bot._dataset_refresh_tasks, set())
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'interrupted')

    async def test_delayed_pre_clear_turn_cannot_restore_history_or_preferences(self):
        await self.bot.process_update(update(45, '/clear'))
        await self.bot._retry_db(self.bot._remember, USER_ID, 44, 'I prefer field goal attempts.')
        self.assertEqual(self.bot._conversation(USER_ID), [])
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        await self.bot.process_update(update(46, 'I prefer assists.'))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {'preferred_stat':'ast'})

    async def test_refresh_status_aliases_are_read_only_show_stage_elapsed_and_last_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        refresh = AsyncMock()
        async def work(): started.set(); await release.wait(); return 'Completed check; tracking remains partial.'
        refresh.side_effect = work; self.bot.manual_refresh_callback = refresh
        await self.bot.process_update(update(50, '/refresh')); await started.wait()
        self.store.set_checkpoint('dataset_update_status', {'phase':'checking','detail':'Rebuilding master Excel','stage':'excel'})
        with self.store.connect() as db:
            db.execute('UPDATE telegram_dataset_jobs SET created_at=?',
                       ((dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=125)).isoformat(),))
        await self.bot.process_update(update(51, '/refresh status'))
        await self.bot.process_update(update(52, '/update status'))
        text = self.rows('telegram_outbox')[-1]['text']
        self.assertIn('Rebuilding master Excel', text); self.assertIn('2m', text)
        self.assertEqual(len(self.rows('telegram_dataset_jobs')), 1); refresh.assert_awaited_once()
        release.set(); await self.finish_jobs()
        await self.bot.process_update(update(53, '/update status'))
        self.assertIn('tracking remains partial', self.rows('telegram_outbox')[-1]['text'])
        self.assertEqual(len(self.rows('telegram_dataset_jobs')), 1)
