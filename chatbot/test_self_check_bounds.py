"""Bounded real SQLite checks and native-thread lock ownership; no live I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from master import MasterStore
import self_check as sc


class BoundedSelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.live=MasterStore(self.root/'live.sqlite')
        self.live.feed_success('rss_1','fixture')

    def test_real_slow_sql_is_interrupted_by_readonly_vm_budget(self):
        started=time.monotonic()
        with self.assertRaises(TimeoutError):
            with sc._readonly(self.live.path,.005) as db:
                db.execute('WITH RECURSIVE n(v) AS (VALUES(0) UNION ALL SELECT v+1 FROM n WHERE v<10000000) SELECT sum(v) FROM n').fetchall()
        self.assertLess(time.monotonic()-started,.5)

    def test_snapshot_uses_one_readonly_connection_and_does_not_call_live_status(self):
        original=sqlite3.connect
        with patch.object(sc.sqlite3,'connect',wraps=original) as connect:
            report=sc._status_snapshot(self.live.path)
        self.assertEqual(connect.call_count,1)
        self.assertTrue(connect.call_args.kwargs['uri'])
        self.assertIn('?mode=ro',connect.call_args.args[0])
        self.assertEqual(report['counts']['entries'],0)
        with patch.object(self.live,'status',side_effect=AssertionError('live.status must not run')):
            report=sc.run_self_checks(self.live,self.root)
        self.assertEqual(report['failed'],0)

    def test_missing_live_file_is_unverified_with_unknown_counts_not_failed_typeerror(self):
        report=sc.run_self_checks(SimpleNamespace(path=self.root/'vanished.sqlite'),self.root)
        live=[c for c in report['checks'] if c['scope']=='live runtime']
        self.assertFalse(any(c['status']=='FAIL' for c in live))
        integrity=next(c for c in live if 'database integrity' in c['name'])
        self.assertEqual((integrity['status'],integrity['error_type']),('WARN','FileNotFoundError'))
        self.assertEqual(report['counts'],{})

    def test_actual_exclusive_writer_is_warning_without_waiting_thirty_seconds(self):
        path=self.root/'exclusive.sqlite'
        writer=sqlite3.connect(path);self.addCleanup(writer.close)
        writer.execute('CREATE TABLE fixture(value)');writer.commit();writer.execute('BEGIN EXCLUSIVE')
        self.addCleanup(writer.rollback)
        started=time.monotonic()
        with patch.object(sc,'INTEGRITY_BUDGET_SECONDS',.03),patch.object(sc,'STATUS_BUDGET_SECONDS',.03):
            report=sc.run_self_checks(SimpleNamespace(path=path),self.root)
        live=[c for c in report['checks'] if c['scope']=='live runtime']
        self.assertFalse(any(c['status']=='FAIL' for c in live))
        self.assertEqual(next(c for c in live if 'database integrity' in c['name'])['status'],'WARN')
        self.assertEqual(report['counts'],{})
        self.assertLess(time.monotonic()-started,3)

    def test_actual_corrupt_file_fails_without_repeating_private_contents(self):
        path=self.root/'corrupt.sqlite';path.write_text('PRIVATE_DATABASE_CONTENT')
        report=sc.run_self_checks(SimpleNamespace(path=path),self.root)
        integrity=next(c for c in report['checks'] if 'Live database integrity' in c['name'])
        self.assertEqual(integrity['status'],'FAIL')
        self.assertEqual(integrity['error_type'],'DatabaseError')
        self.assertNotIn('PRIVATE_DATABASE_CONTENT',repr(report)+sc.format_self_check(report))

    def test_deadline_is_warning_and_visible_with_elapsed_and_safe_error_type(self):
        with patch.object(sc,'INTEGRITY_BUDGET_SECONDS',0):
            report=sc.run_self_checks(self.live,self.root)
        integrity=next(c for c in report['checks'] if 'Live database integrity' in c['name'])
        self.assertEqual((integrity['status'],integrity['error_type']),('WARN','TimeoutError'))
        self.assertIn('TimeoutError',sc.format_self_check(report))
        self.assertTrue(all('elapsed_seconds' in c for c in report['checks']))
        self.assertEqual(report['failed'],0)

    def test_backup_pruned_during_discovery_is_warning_and_check_continues(self):
        folder=self.root/'backups';folder.mkdir();path=folder/'PRIVATE_BACKUP_NAME.sqlite'
        self.live.backup(str(path));original=Path.stat;deleted=[]
        def stat(candidate,*args,**kwargs):
            if candidate==path and not deleted:
                deleted.append(True);path.unlink();raise FileNotFoundError('PRIVATE_BACKUP_NAME')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'stat',stat):report=sc.run_self_checks(self.live,self.root)
        discovery=next(c for c in report['checks'] if 'backup discovery' in c['name'])
        self.assertEqual((discovery['status'],discovery['error_type']),('WARN','FileNotFoundError'))
        self.assertEqual(report['failed'],0)
        self.assertNotIn('PRIVATE_BACKUP_NAME',repr(report)+sc.format_self_check(report))

    def test_backup_pruned_after_discovery_is_warning_not_outer_failure(self):
        folder=self.root/'backups';folder.mkdir();path=folder/'backup.sqlite';self.live.backup(str(path))
        original=sc._sqlite_ok
        def integrity(candidate,*args,**kwargs):
            if candidate==path:path.unlink()
            return original(candidate,*args,**kwargs)
        with patch.object(sc,'_sqlite_ok',integrity):report=sc.run_self_checks(self.live,self.root)
        check=next(c for c in report['checks'] if 'Latest live backup integrity' in c['name'])
        self.assertEqual(check['status'],'WARN');self.assertEqual(report['failed'],0)

    def test_progress_is_fixed_stage_names_and_callback_failure_cannot_abort_checks(self):
        stages=[]
        def progress(stage):
            stages.append(stage)
            raise RuntimeError('PRIVATE_PROGRESS_EXCEPTION')
        report=sc.run_self_checks(self.live,self.root,progress=progress)
        self.assertEqual(report['failed'],0)
        self.assertTrue(all(c['name'] in stages for c in report['checks'] if c['scope']=='isolated fixtures'))
        self.assertIn('Resolving read-only live database path',stages)
        self.assertIn('Live database counts and feed snapshot (read-only)',stages)
        self.assertIn('Live backup discovery (read-only)',stages)
        self.assertIn('Finishing self-test report',stages)
        self.assertNotIn('PRIVATE_PROGRESS_EXCEPTION',repr(report))


class SerializedNativeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_caller_returns_promptly_while_real_worker_owns_lock(self):
        lock=asyncio.Lock();entered=threading.Event();release=threading.Event();calls=[]
        def worker():
            calls.append('first');entered.set();release.wait(2);return 'done'
        first=asyncio.create_task(sc.run_serialized_checks(lock,worker))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(first,.2)
            self.assertTrue(lock.locked())
            second=asyncio.create_task(sc.run_serialized_checks(lock,lambda:calls.append('second')))
            await asyncio.sleep(.01);self.assertEqual(calls,['first'])
            release.set();await asyncio.wait_for(second,1)
            self.assertEqual(calls,['first','second']);self.assertFalse(lock.locked())
        finally:
            release.set()
            if not first.done():first.cancel()

    async def test_direct_owner_task_cancellation_does_not_unlock_running_native_work(self):
        lock=asyncio.Lock();entered=threading.Event();release=threading.Event();before=set(sc._CHECK_WORKERS)
        def worker():entered.set();release.wait(2)
        caller=asyncio.create_task(sc.run_serialized_checks(lock,worker))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            owner=next(t for t in sc._CHECK_WORKERS if t not in before)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):await caller
            self.assertTrue(lock.locked());self.assertIn(owner,sc._CHECK_WORKERS)
            next_call=Mock(return_value=7)
            second=asyncio.create_task(sc.run_serialized_checks(lock,next_call))
            await asyncio.sleep(.01);next_call.assert_not_called()
            release.set();self.assertEqual(await asyncio.wait_for(second,1),7)
            self.assertFalse(lock.locked());self.assertNotIn(owner,sc._CHECK_WORKERS)
        finally:release.set()

    async def test_cancellation_while_waiting_for_lock_creates_no_worker(self):
        lock=asyncio.Lock();await lock.acquire();function=Mock();before=set(sc._CHECK_WORKERS)
        waiting=asyncio.create_task(sc.run_serialized_checks(lock,function))
        await asyncio.sleep(.01);waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiting
        function.assert_not_called();self.assertEqual(set(sc._CHECK_WORKERS),before)
        self.assertTrue(lock.locked());lock.release()

    async def test_owner_cancelled_before_native_start_cannot_run_late_after_unlock(self):
        loop=asyncio.get_running_loop();executor=ThreadPoolExecutor(max_workers=1);loop.set_default_executor(executor)
        occupied=threading.Event();release=threading.Event();function=Mock();before=set(sc._CHECK_WORKERS)
        def occupy():occupied.set();release.wait(2)
        busy=asyncio.create_task(asyncio.to_thread(occupy));await asyncio.sleep(.01)
        self.assertTrue(occupied.is_set())
        lock=asyncio.Lock();caller=asyncio.create_task(sc.run_serialized_checks(lock,function))
        try:
            await asyncio.sleep(.01)
            owner=next(t for t in sc._CHECK_WORKERS if t not in before);owner.cancel()
            with self.assertRaises(asyncio.CancelledError):await caller
            self.assertFalse(lock.locked());release.set();await busy
            await asyncio.sleep(.02);function.assert_not_called()
        finally:release.set()

    async def test_detached_worker_exception_is_consumed_after_real_cleanup(self):
        lock=asyncio.Lock();entered=threading.Event();release=threading.Event();contexts=[]
        loop=asyncio.get_running_loop();old=loop.get_exception_handler();loop.set_exception_handler(lambda loop,ctx:contexts.append(ctx))
        def worker():entered.set();release.wait(2);raise RuntimeError('PRIVATE_DETACHED_FAILURE')
        caller=asyncio.create_task(sc.run_serialized_checks(lock,worker))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1));caller.cancel()
            with self.assertRaises(asyncio.CancelledError):await caller
            self.assertTrue(lock.locked());release.set()
            async with asyncio.timeout(1):
                while lock.locked():await asyncio.sleep(.01)
            await asyncio.sleep(.02);self.assertEqual(contexts,[])
        finally:release.set();loop.set_exception_handler(old)


if __name__=='__main__':unittest.main()
