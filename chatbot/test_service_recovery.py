"""Failure isolation and bounded export work, without live network or databases."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

import app
import refresh_export


class MaintenanceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_grading_and_notification_failure_do_not_block_backup_or_export(self):
        master = SimpleNamespace(grade_entries=Mock(side_effect=OSError('private detail')),
                                 feed_success=Mock(), feed_failure=Mock())
        state = SimpleNamespace(master=master, telegram=SimpleNamespace(notify=AsyncMock()))
        with patch.object(app, 'notify_feed_health', AsyncMock(side_effect=RuntimeError())), \
                patch.object(app, 'save_backup') as backup, patch.object(app, 'export_master') as export:
            buckets = await app.maintenance_cycle(state)
        backup.assert_called_once()
        export.assert_called_once()
        self.assertTrue(buckets[0])
        self.assertEqual(buckets[0], buckets[1])
        self.assertEqual([c.args[0] for c in master.feed_failure.call_args_list],
                         ['feed_notifications', 'entry_grading'])

    async def test_failed_export_retries_without_repeating_successful_backup(self):
        master = SimpleNamespace(grade_entries=Mock(return_value=[]),
                                 feed_success=Mock(), feed_failure=Mock())
        state = SimpleNamespace(master=master, telegram=SimpleNamespace(notify=AsyncMock()))
        current = dt.datetime(2026, 11, 1, 15, 0, tzinfo=dt.timezone.utc)
        with patch.object(app, 'notify_feed_health', AsyncMock()), \
                patch.object(app, 'save_backup') as backup, \
                patch.object(app, 'export_master', side_effect=[OSError(), None]) as export:
            buckets = await app.maintenance_cycle(state, current=current)
            self.assertEqual(buckets, ('2026-11-01:2', ''))
            buckets = await app.maintenance_cycle(state, *buckets, current=current)
            self.assertEqual(export.call_count, 1)
            buckets = await app.maintenance_cycle(state, *buckets, current=current + dt.timedelta(hours=1))
        backup.assert_called_once()
        self.assertEqual(export.call_count, 2)
        self.assertEqual(buckets, ('2026-11-01:2', '2026-11-01:2'))
        self.assertIn('master_export', [c.args[0] for c in master.feed_success.call_args_list])

    async def test_failure_reporting_error_does_not_stop_other_maintenance_steps(self):
        master = SimpleNamespace(grade_entries=Mock(return_value=[]),
                                 feed_success=Mock(side_effect=OSError()),
                                 feed_failure=Mock(side_effect=OSError()))
        state = SimpleNamespace(master=master, telegram=SimpleNamespace(notify=AsyncMock()))
        with patch.object(app, 'notify_feed_health', AsyncMock()), \
                patch.object(app, 'save_backup') as backup, patch.object(app, 'export_master') as export:
            await app.maintenance_cycle(state)
        backup.assert_called_once()
        export.assert_called_once()


class DownloadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.locks = {'master': asyncio.Lock(), 'backup': asyncio.Lock()}
        self.context = patch.multiple(app.app.state, create=True,
                                     download_locks=self.locks, master=object())
        self.context.start()
        self.addCleanup(self.context.stop)
        self.root_patch = patch.object(app, 'DATA_DIR', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    async def test_duplicate_download_rejected_before_starting_another_export(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def export(function, master, directory):
            calls.append(directory)
            directory.mkdir(parents=True)
            entered.set()
            await release.wait()
            path = directory / 'file.xlsx'
            path.write_bytes(b'test')
            return path
        with patch.object(app, 'call_service', export):
            pending = asyncio.create_task(app.prepare_download('master', object(), 'file.xlsx', 'test'))
            await entered.wait()
            with self.assertRaises(HTTPException) as error:
                await app.prepare_download('master', object(), 'file.xlsx', 'test')
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(len(calls), 1)
            release.set()
            response = await pending
        await response({'type': 'http', 'method': 'GET', 'headers': []}, AsyncMock(), AsyncMock())
        self.assertFalse(calls[0].exists())
        self.assertFalse(self.locks['master'].locked())

    async def test_abandoned_file_transfer_removes_download_directory(self):
        directory = self.root / 'transfer'
        directory.mkdir()
        path = directory / 'file.xlsx'
        path.write_bytes(b'test')
        response = app.DownloadFileResponse(path, directory)
        with self.assertRaises(OSError):
            await response({'type': 'http', 'method': 'GET', 'headers': []},
                           AsyncMock(), AsyncMock(side_effect=OSError('connection lost')))
        self.assertFalse(directory.exists())

    async def test_export_failure_cleans_partial_directory_and_releases_lock(self):
        paths = []
        async def fail(function, master, directory):
            directory.mkdir(parents=True)
            (directory / 'partial').write_bytes(b'test')
            paths.append(directory)
            raise OSError('disk full')
        with patch.object(app, 'call_service', fail), self.assertRaises(OSError):
            await app.prepare_download('master', object(), 'file.xlsx', 'test')
        self.assertFalse(paths[0].exists())
        self.assertFalse(self.locks['master'].locked())

    async def test_browser_cancellation_keeps_work_locked_then_cleans_completed_file(self):
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        paths = []
        async def export(function, master, directory):
            directory.mkdir(parents=True)
            paths.append(directory)
            entered.set()
            await release.wait()
            path = directory / 'file.xlsx'
            path.write_bytes(b'test')
            completed.set()
            return path
        with patch.object(app, 'call_service', export):
            pending = asyncio.create_task(app.prepare_download('master', object(), 'file.xlsx', 'test'))
            await entered.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(self.locks['master'].locked())
            release.set()
            await completed.wait()
            await asyncio.sleep(0)
        self.assertFalse(self.locks['master'].locked())
        self.assertFalse(paths[0].exists())


class ExportConcurrencyTests(unittest.TestCase):
    def test_sheet_build_failure_cleans_openpyxl_spool_files_and_keeps_old_export(self):
        from openpyxl.worksheet._writer import ALL_TEMP_FILES
        before = set(ALL_TEMP_FILES)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'NBA_Master.xlsx'
            target.write_bytes(b'previous-good-export')
            with patch.object(refresh_export, 'build_export_tables', return_value={'test': [{'field': 'value'}]}), \
                    patch.object(refresh_export, '_cell', side_effect=['field', ValueError('invalid row')]), \
                    self.assertRaises(ValueError):
                refresh_export.export_master(object(), directory)
            self.assertEqual(target.read_bytes(), b'previous-good-export')
            self.assertFalse(list(Path(directory).glob('*.partial.xlsx')))
        self.assertEqual(set(ALL_TEMP_FILES), before)

    def test_scheduled_and_browser_exports_share_one_generation_slot(self):
        active = maximum = 0
        counter_lock = threading.Lock()
        def export(master, directory):
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.02)
            with counter_lock:
                active -= 1
            return directory
        with patch.object(refresh_export, 'export_master', export), ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda n: app.export_master(object(), n), range(3)))
        self.assertEqual(paths, [0, 1, 2])
        self.assertEqual(maximum, 1)


if __name__ == '__main__':
    unittest.main()
