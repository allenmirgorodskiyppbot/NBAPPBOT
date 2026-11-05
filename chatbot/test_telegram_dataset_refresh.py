"""Manual data updates acknowledge immediately and finish through the outbox."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import app
from master import MasterStore
from telegram_bot import TelegramBot, HELP
from test_telegram import TOKEN, USER_ID, update


class TelegramDatasetRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MasterStore(Path(self.temp.name) / 'master.sqlite')
        self.bots = []
        self.answer = AsyncMock(return_value='AI must not run for a dataset update.')
        self.addAsyncCleanup(self.cancel_jobs)

    async def cancel_jobs(self):
        tasks = [task for bot in self.bots for task in bot._dataset_refresh_tasks]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

    def bot(self, callback=None):
        bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID, USER_ID + 1],
                          manual_refresh_callback=callback)
        bot._typing = AsyncMock(); self.bots.append(bot); return bot

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    async def finish(self, bot):
        if bot._dataset_refresh_tasks:
            await asyncio.gather(*list(bot._dataset_refresh_tasks))

    def financial(self):
        with self.store.connect() as db:
            return {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)]
                    for name in ('entries', 'grade_events', 'boards')}

    async def test_manual_update_acknowledges_before_long_callback_and_deduplicates_busy_commands(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def refresh():
            calls.append(True); entered.set(); await release.wait(); return 'Dataset updated. Coverage through Nov 4.'
        bot = self.bot(refresh); before = self.financial()
        self.assertTrue(await asyncio.wait_for(bot.process_update(update(1, '/refresh')), .5))
        self.assertIn('Checking dataset updates', self.rows('telegram_outbox')[0]['text'])
        self.assertEqual(self.rows('telegram_updates')[0]['status'], 'done')
        await asyncio.wait_for(entered.wait(), .5)
        await bot.process_update(update(2, '/update'))
        self.assertIn('already in progress', self.rows('telegram_outbox')[-1]['text'])
        self.assertEqual(len(calls), 1)
        release.set(); await self.finish(bot)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'completed')
        self.assertEqual(self.rows('telegram_outbox')[-1]['text'], 'Dataset updated. Coverage through Nov 4.')
        self.answer.assert_not_awaited(); self.assertEqual(before, self.financial())
        await bot.process_update(update(3, '/update')); await self.finish(bot)
        self.assertEqual(len(calls), 2)

    async def test_result_is_durable_in_original_chat_and_duplicate_update_does_not_repeat_import(self):
        refresh = AsyncMock(return_value='Dataset already current. Tracking coverage remains partial.')
        bot = self.bot(refresh)
        await bot.process_update(update(10, '/update', user=USER_ID + 1)); await self.finish(bot)
        self.assertFalse(await bot.process_update(update(10, '/update', user=USER_ID + 1)))
        refresh.assert_awaited_once()
        rows = self.rows('telegram_outbox')
        self.assertEqual(len(rows), 2); self.assertEqual({r['chat_id'] for r in rows}, {USER_ID + 1})
        reopened = self.bot(refresh); reopened._recover_interrupted()
        self.assertEqual(len(self.rows('telegram_outbox')), 2)
        reopened._api = AsyncMock(side_effect=[RuntimeError('private provider response'), True, True])
        await reopened.flush_outbox()
        with self.store.connect() as db: db.execute('UPDATE telegram_outbox SET next_attempt=0')
        await reopened.flush_outbox()
        self.assertTrue(all(r['status'] == 'delivered' for r in self.rows('telegram_outbox')))
        self.assertEqual({call.args[1]['chat_id'] for call in reopened._api.await_args_list}, {USER_ID + 1})

    async def test_failed_callback_reports_safe_failure_and_releases_busy_state(self):
        refresh = AsyncMock(side_effect=RuntimeError('secret token=https://private.example/token'))
        bot = self.bot(refresh)
        await bot.process_update(update(20, '/refresh')); await self.finish(bot)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'failed')
        text = self.rows('telegram_outbox')[-1]['text']
        self.assertIn('could not finish', text); self.assertNotIn('private.example', text)
        bot.manual_refresh_callback = AsyncMock(return_value='The retry checked current source coverage.')
        await bot.process_update(update(21, '/refresh')); await self.finish(bot)
        self.assertEqual(self.rows('telegram_dataset_jobs')[-1]['status'], 'completed')

    async def test_restart_marks_unfinished_job_interrupted_once_and_does_not_replay_callback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def refresh():
            entered.set(); await release.wait(); return 'An old completion.'
        bot = self.bot(refresh)
        await bot.process_update(update(30, '/refresh')); await entered.wait()
        with self.store.connect() as db:
            # Simulate a crash after durable job/ack insertion but before the
            # surrounding update was marked complete.
            db.execute("UPDATE telegram_updates SET status='processing' WHERE update_id=30")
        new_refresh = AsyncMock(return_value='A new requested check.')
        reopened = self.bot(new_refresh); reopened._recover_interrupted(); reopened._recover_interrupted()
        new_refresh.assert_not_awaited()
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'interrupted')
        self.assertEqual(sum('interrupted by a restart' in r['text'] for r in self.rows('telegram_outbox')), 1)
        self.assertFalse(any('previous command' in r['text'] for r in self.rows('telegram_outbox')))
        self.assertEqual(self.rows('telegram_updates')[0]['status'], 'interrupted')
        release.set(); await self.finish(bot)
        self.assertFalse(any(r['text'] == 'An old completion.' for r in self.rows('telegram_outbox')))
        await reopened.process_update(update(31, '/refresh')); await self.finish(reopened)
        new_refresh.assert_awaited_once()

    async def test_cancellation_saves_interrupted_notice_and_allows_retry(self):
        entered = asyncio.Event()
        async def refresh():
            entered.set(); await asyncio.Event().wait()
        bot = self.bot(refresh)
        await bot.process_update(update(40, '/refresh')); await entered.wait()
        tasks = list(bot._dataset_refresh_tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'interrupted')
        self.assertIn('interrupted', self.rows('telegram_outbox')[-1]['text'])
        bot.manual_refresh_callback = AsyncMock(return_value='Finished retry.')
        await bot.process_update(update(41, '/refresh')); await self.finish(bot)
        self.assertEqual(self.rows('telegram_dataset_jobs')[-1]['status'], 'completed')

    async def test_transient_result_write_failure_retries_without_repeating_data_import(self):
        refresh = AsyncMock(return_value='Checked dataset coverage.')
        bot = self.bot(refresh); original = bot._finish_dataset_refresh
        failures = [True, True, False]
        def finish(*args):
            if failures.pop(0): raise sqlite3.OperationalError('temporary storage failure')
            return original(*args)
        with patch.object(bot, '_finish_dataset_refresh', side_effect=finish):
            await bot.process_update(update(45, '/refresh')); await self.finish(bot)
        refresh.assert_awaited_once()
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'completed')
        self.assertEqual(sum(r['text'] == 'Checked dataset coverage.' for r in self.rows('telegram_outbox')), 1)

    async def test_exhausted_result_writes_recover_on_next_command_without_permanent_busy(self):
        refresh = AsyncMock(return_value='The completed earlier coverage check.')
        bot = self.bot(refresh)
        with patch.object(bot, '_finish_dataset_refresh', side_effect=sqlite3.OperationalError('temporary storage failure')):
            await bot.process_update(update(46, '/refresh'))
            await asyncio.gather(*list(bot._dataset_refresh_tasks), return_exceptions=True)
        self.assertEqual(self.rows('telegram_dataset_jobs')[0]['status'], 'running')
        await bot.process_update(update(47, '/refresh')); await self.finish(bot)
        self.assertEqual(refresh.await_count, 2)
        self.assertTrue(all(r['status'] == 'completed' for r in self.rows('telegram_dataset_jobs')))
        self.assertEqual(sum(r['event_key'].startswith('dataset-refresh-result:') for r in self.rows('telegram_outbox')), 2)
        self.assertEqual(bot._dataset_refresh_pending_results, {})

    async def test_authorization_missing_configuration_and_argument_validation(self):
        refresh = AsyncMock(return_value='Done.')
        bot = self.bot(refresh)
        await bot.process_update(update(50, '/refresh', user=999))
        await bot.process_update(update(51, '/update', chat_type='group'))
        await bot.process_update(update(52, '/refresh now'))
        self.assertEqual(self.rows('telegram_dataset_jobs'), [])
        refresh.assert_not_awaited()
        legacy = self.bot()
        await legacy.process_update(update(53, '/refresh'))
        self.assertIn('not configured', self.rows('telegram_outbox')[-1]['text'])
        self.assertIn('/refresh', HELP); self.assertIn('/update', HELP)

    async def test_daily_notification_is_private_durable_and_deduplicated_by_eastern_day(self):
        bot = self.bot()
        report = {'notification_day': '2026-11-05', 'notification_event_key': 'dataset-daily:2026-11-05'}
        self.assertTrue(await bot.notify_dataset_refresh('Daily update checked. Coverage is partial.', report))
        self.assertTrue(await bot.notify_dataset_refresh('A retry must not duplicate the daily result.', report))
        self.assertEqual(len(self.rows('telegram_outbox')), 2)
        self.assertEqual({r['chat_id'] for r in self.rows('telegram_outbox')}, {USER_ID, USER_ID + 1})
        reopened = self.bot()
        await reopened.notify_dataset_refresh('Retry after restart.', report)
        self.assertEqual(len(self.rows('telegram_outbox')), 2)
        await reopened.notify_dataset_refresh('Next daily result.', {'notification_day': '2026-11-06'})
        self.assertEqual(len(self.rows('telegram_outbox')), 4)
        for invalid in ({}, {'notification_day':'2026-02-31'}, {'notification_day':'today'}):
            self.assertFalse(await reopened.notify_dataset_refresh('Invalid daily event.', invalid))
        self.assertEqual(len(self.rows('telegram_outbox')), 4)
        disabled = TelegramBot(self.store, self.answer, token='', allowed_user_ids=[USER_ID])
        self.assertFalse(await disabled.notify_dataset_refresh('Disabled delivery.', {'notification_day':'2026-11-07'}))
        self.assertEqual(len(self.rows('telegram_outbox')), 4)

    async def test_structured_partial_report_uses_shared_formatter_for_completion(self):
        report = {'phase': 'partial', 'changed': True}
        bot = self.bot(AsyncMock(return_value=report))
        with patch('dataset_update.format_update_report', return_value='Dataset updated partially; tracking remains unavailable.') as formatter:
            await bot.process_update(update(60, '/refresh')); await self.finish(bot)
        formatter.assert_called_once_with(report)
        self.assertIn('partially', self.rows('telegram_outbox')[-1]['text'])

    async def test_app_installs_force_supplement_schedule_and_final_export_callbacks(self):
        bot = self.bot()
        state = SimpleNamespace(master=self.store, nightly=SimpleNamespace(), telegram=bot)
        controller = SimpleNamespace(refresh=AsyncMock())
        with patch('dataset_update.DatasetUpdateController', return_value=controller) as constructor, \
             patch('supplemental_data.refresh_supplemental_data', AsyncMock(return_value={'changed': True})) as supplemental, \
             patch.object(app, 'refresh_pregame_schedule', AsyncMock()) as schedule, \
             patch.object(app, 'export_master', return_value='updated.xlsx') as export:
            app.configure_dataset_updates(state)
            options = constructor.call_args.kwargs
            await options['supplemental_refresh']()
            await options['schedule_refresh']()
            await options['export_refresh']()
        self.assertIs(state.dataset_updates, controller)
        self.assertIs(constructor.call_args.args[0], state.nightly)
        supplemental.assert_awaited_once_with(self.store, app.DATA_DIR, force=True)
        schedule.assert_awaited_once_with(self.store)
        export.assert_called_once_with(self.store, app.DATA_DIR / 'exports')
        self.assertEqual(state.nightly.notifier, bot.notify_dataset_refresh)

    async def test_app_manual_refresh_does_not_require_the_ai_model(self):
        controller = SimpleNamespace(refresh=AsyncMock(return_value={'phase': 'current'}))
        with patch.object(app.app.state, 'dataset_updates', controller, create=True), \
             patch.object(app.app.state, 'phase', 'model', create=True):
            self.assertEqual(await app.telegram_refresh_dataset(), {'phase': 'current'})
        controller.refresh.assert_awaited_once(); self.answer.assert_not_awaited()
        with patch.object(app.app.state, 'dataset_updates', None, create=True):
            self.assertIn('still starting', await app.telegram_refresh_dataset())
