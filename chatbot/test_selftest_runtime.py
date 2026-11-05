"""Self-check workers retain ownership through reply cancellation and export waits."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import app
from master import MasterStore
import release_checks
from telegram_bot import TelegramBot


def report():
    return {'checks': [], 'passed': 1, 'failed': 0, 'warnings': 0,
            'scope': 'Isolated fixture report.', 'source_ref': 'fixture'}


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.002)


class SelfTestRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.master = MasterStore(self.root / 'master.sqlite')
        self.state = SimpleNamespace(master=self.master, self_test_lock=asyncio.Lock(),
            phase='ready', store=None, telegram=SimpleNamespace(connected=True, status=lambda: {}))
        state = patch.object(app.app, 'state', self.state)
        state.start(); self.addCleanup(state.stop)
        directory = patch.object(app, 'DATA_DIR', self.root)
        directory.start(); self.addCleanup(directory.stop)

    def bot(self, callback):
        return TelegramBot(self.master, AsyncMock(side_effect=AssertionError('No AI expected')),
            token='123:TEST_TOKEN_ONLY', allowed_user_ids=[123], self_test_callback=callback,
            self_test_status_callback=app.self_test_status)

    async def test_real_tiny_fixture_export_finishes_while_full_export_lock_is_held(self):
        gate = threading.Lock(); gate.acquire()
        before = self.master.status()['counts']
        try:
            with patch.object(app, '_EXPORT_LOCK', gate):
                text = await asyncio.wait_for(app.self_test(), 5)
            self.assertIn('0 failed', text)
            self.assertIn('Master Excel export contains imported data', text)
            self.assertTrue(gate.locked())
            self.assertEqual(self.master.status()['counts'], before)
            self.assertEqual(self.state.self_test_progress['phase'], 'completed')
        finally:
            gate.release()

    async def test_full_master_export_still_serializes_two_concurrent_builds(self):
        entered, release = threading.Event(), threading.Event()
        guard = threading.Lock()
        active = maximum = calls = 0

        def build(*args):
            nonlocal active, maximum, calls
            with guard:
                active += 1; calls += 1; maximum = max(maximum, active)
                first = calls == 1
            try:
                if first:
                    entered.set()
                    if not release.wait(3): raise RuntimeError('Fixture release timed out')
                return self.root / 'fixture.xlsx'
            finally:
                with guard: active -= 1

        with patch('refresh_export.export_master', build), patch.object(app, '_EXPORT_LOCK', threading.Lock()):
            first = asyncio.create_task(asyncio.to_thread(app.export_master, object(), self.root))
            await eventually(entered.is_set)
            second = asyncio.create_task(asyncio.to_thread(app.export_master, object(), self.root))
            try:
                await asyncio.sleep(.02)
                self.assertEqual(calls, 1)
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertEqual((calls, maximum), (2, 1))

    async def test_background_manual_worker_keeps_singleflight_until_final_report(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def checks(*args, progress=None):
            calls.append(True)
            progress('Controlled fixture check')
            entered.set()
            if not release.wait(3): raise RuntimeError('Fixture release timed out')
            return report()

        bot = self.bot(app.self_test)
        message = {'text': '/selftest', 'chat': {'id': 123, 'type': 'private'}}
        with patch('self_check.run_self_checks', checks):
            try:
                answer = await bot._handle_message(message, 1)
                self.assertIsNone(answer)
                await eventually(entered.is_set)
                self.assertTrue(self.state.self_test_lock.locked())
                busy = await bot._handle_message(message, 2)
                self.assertIn('already running', busy)
                status = await bot._handle_message({'text':'/selftest status','chat':{'id':123}}, 3)
                self.assertIn('Controlled fixture check', status)
                self.assertIn('Elapsed', status)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await asyncio.gather(*list(bot._self_test_tasks))
                await eventually(lambda: not self.state.self_test_lock.locked())
        self.assertEqual(self.state.self_test_progress['phase'], 'completed')
        with self.master.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM telegram_selftest_jobs').fetchone()[0],'completed')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE event_key LIKE 'selftest-result:%'").fetchone()[0],1)

    async def test_automatic_check_cancellation_preserves_worker_lock_and_blocks_manual_duplicate(self):
        entered, release = threading.Event(), threading.Event()
        stamp = '2026-11-05T00:00:00+00:00'
        checkpoints = {}
        automatic_master = SimpleNamespace(
            set_checkpoint=lambda k,v: checkpoints.__setitem__(k,v),
            get_checkpoint=lambda k,d=None: {'checked_at': stamp},
            status=lambda: {'feeds': [{'name':'espn_news','last_attempt':stamp}]})
        self.state.master = automatic_master
        self.state.boot_started_at = stamp
        self.state.worker = SimpleNamespace(news_source='espn', rss_urls=[])

        def verify(*args, progress=None):
            progress('Automatic fixture check')
            entered.set()
            if not release.wait(3): raise RuntimeError('Fixture release timed out')
            return report()

        with patch.object(release_checks, 'verify_release', verify):
            task = asyncio.create_task(release_checks.automatic_release_checks(self.state, self.root, app.export_fixture))
            try:
                await eventually(entered.is_set)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, .5)
                self.assertTrue(self.state.self_test_lock.locked())
                self.assertIn('already running', await app.self_test())
                self.assertEqual(checkpoints['release_checks']['phase'], 'running')
            finally:
                release.set()
                await eventually(lambda: not self.state.self_test_lock.locked())
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.state.self_test_progress['phase'], 'completed')

    async def test_worker_error_keeps_stage_and_safe_category_without_private_exception_text(self):
        def checks(*args, progress=None):
            progress('Controlled integrity stage')
            raise OSError('PRIVATE_DATABASE_PATH PRIVATE_TOKEN')
        bot = self.bot(app.self_test)
        with patch('self_check.run_self_checks', checks), self.assertLogs('knicks', level='WARNING') as logs:
            self.assertIsNone(await bot._handle_message({'text':'/selftest','chat':{'id':123}}, 3))
            await asyncio.gather(*list(bot._self_test_tasks))
            answer = await bot._self_test_status(123)
        self.assertIn('OSError', answer)
        self.assertNotIn('PRIVATE_', answer + repr(logs.output) + repr(self.state.self_test_progress))
        self.assertEqual(self.state.self_test_progress['stage'], 'Controlled integrity stage')
        self.assertEqual(self.state.self_test_progress['error_type'], 'OSError')
        self.assertEqual(self.state.self_test_progress['phase'], 'failed')
        self.assertFalse(self.state.self_test_lock.locked())

    async def test_automatic_release_uses_the_isolated_fixture_exporter(self):
        self.state.boot_started_at = '2026-11-05T00:00:00+00:00'
        self.state.worker = SimpleNamespace(news_source='rss', rss_urls=[])
        self.master.set_checkpoint('knicks_digest_status', {'checked_at':'2026-11-05T00:00:00+00:00'})
        gate = threading.Lock(); gate.acquire()
        seen = []

        def verify(master, workbook, data_dir, exporter, model_phase, telegram, *, progress=None):
            seen.append(exporter)
            progress('Controlled automatic export')
            exporter(object(), data_dir)
            return report()

        try:
            with patch.object(release_checks, 'verify_release', verify), patch.object(app, '_EXPORT_LOCK', gate), \
                    patch('refresh_export.export_master', return_value=self.root/'unused.xlsx'):
                await asyncio.wait_for(release_checks.automatic_release_checks(self.state, self.root, app.export_fixture), 1)
            self.assertTrue(gate.locked())
            self.assertEqual(seen, [app.export_fixture])
            self.assertEqual(self.master.get_checkpoint('release_checks')['phase'], 'checked')
        finally:
            gate.release()

    async def test_application_shutdown_waits_for_detached_check_before_workbook_cleanup(self):
        from self_check import run_serialized_checks
        entered, release = threading.Event(), threading.Event()

        def checks():
            entered.set()
            if not release.wait(3): raise RuntimeError('Fixture release timed out')
            return report()

        class Bot:
            def __init__(self, *args, **kwargs): pass
            async def shutdown(self): pass

        async def startup(application):
            await asyncio.Event().wait()

        application = SimpleNamespace(state=SimpleNamespace())
        with patch.object(app, 'DATA_DIR', self.root/'shutdown'), patch.object(app, 'TelegramBot', Bot), \
                patch.object(app, 'initialize_with_retry', startup):
            manager = app.lifespan(application)
            await manager.__aenter__()
            state = application.state
            worker = asyncio.create_task(run_serialized_checks(state.self_test_lock, checks))
            await eventually(entered.is_set)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError): await worker
            self.assertTrue(state.self_test_lock.locked())
            with patch.object(state.temporary, 'cleanup', wraps=state.temporary.cleanup) as cleanup:
                shutdown = asyncio.create_task(manager.__aexit__(None, None, None))
                try:
                    await asyncio.sleep(.02)
                    self.assertFalse(shutdown.done())
                    cleanup.assert_not_called()
                finally:
                    release.set()
                    await asyncio.wait_for(shutdown, 1)
                cleanup.assert_called_once()


if __name__ == '__main__': unittest.main()
