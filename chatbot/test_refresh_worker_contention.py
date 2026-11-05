"""Real upstream writer locks cannot freeze refresh timing or leave orphan writes."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from game_schedule_check import CHECKPOINT, refresh_game_schedule
from master import MasterStore
from nightly_data import NightlyNBARefresh
from test_pregame_schedule import NOW, DAY, ID, TIP, scoreboard


class RefreshWorkerContentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MasterStore(Path(self.temp.name)/'master.sqlite')
        self.writer = sqlite3.connect(self.store.path, check_same_thread=False, isolation_level=None)
        self.addCleanup(self.writer.close); self.addCleanup(self.writer.rollback)
        self.calls = []

    def seed_game(self):
        self.store.upsert_game({'id':ID,'date':DAY,'home_team':'NYK','away_team':'CHI',
                               'start_time':TIP,'status':'scheduled','source':'isolated saved schedule'})

    async def heartbeat_run(self, operation, hold_seconds=.4):
        stamps = []; stop = False
        async def pulse():
            while not stop: stamps.append(time.monotonic()); await asyncio.sleep(.01)
        ticker = asyncio.create_task(pulse()); await asyncio.sleep(.02)
        timer = threading.Timer(hold_seconds, self.writer.rollback); timer.start()
        try: result = await operation
        finally:
            stop = True; await ticker; timer.join(); self.writer.rollback()
        self.assertGreater(len(stamps), 15)
        self.assertLess(max(b-a for a,b in zip(stamps,stamps[1:])), .2)
        return result

    async def test_schedule_merge_writer_contention_keeps_loop_responsive_and_scores_unknown(self):
        self.seed_game(); owner = self
        class Worker:
            async def request(self, url):
                owner.calls.append(url); owner.writer.execute('BEGIN IMMEDIATE'); return scoreboard()
        result = await self.heartbeat_run(refresh_game_schedule(self.store, Worker(), now=NOW))
        self.assertEqual(result['status'], 'success')
        game = self.store.games(DAY)[0]
        self.assertEqual(game['status'], 'scheduled'); self.assertIsNone(game['home_score'])
        self.assertEqual(self.store.status()['counts']['player_games'], 0)
        self.assertEqual(len(self.calls), 1)

    async def test_schedule_cancel_drains_merge_attempt_and_cannot_write_after_lock_release(self):
        self.seed_game(); entered = asyncio.Event(); owner = self
        class Worker:
            async def request(self, url):
                owner.writer.execute('BEGIN IMMEDIATE'); entered.set(); return scoreboard()
        worker = Worker(); task = asyncio.create_task(refresh_game_schedule(self.store, worker, now=NOW))
        await asyncio.wait_for(entered.wait(), 1); await asyncio.sleep(.03); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, .7)
        self.assertFalse(worker._schedule_check_lock.locked())
        before = self.store.games(DAY)[0]
        self.assertEqual(before['source'], 'isolated saved schedule')
        self.assertNotEqual(self.store.get_checkpoint(CHECKPOINT).get('status'), 'success')
        self.writer.rollback(); await asyncio.sleep(.15)
        self.assertEqual(self.store.games(DAY)[0], before)

    async def test_nightly_checkpoint_writer_contention_keeps_loop_responsive(self):
        worker = AsyncMock(); nightly = NightlyNBARefresh(self.store, worker, now=lambda:NOW)
        nightly.enabled = False; self.writer.execute('BEGIN IMMEDIATE')
        await self.heartbeat_run(nightly.tick())
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh'), {'enabled':False})
        worker.request.assert_not_awaited()

    async def test_nightly_cancel_during_busy_checkpoint_leaves_no_later_writer(self):
        worker = AsyncMock(); nightly = NightlyNBARefresh(self.store, worker, now=lambda:NOW)
        nightly.enabled = False; self.writer.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(nightly.tick()); await asyncio.sleep(.03); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, .7)
        self.assertFalse(nightly.lock.locked())
        self.writer.rollback(); await asyncio.sleep(.15)
        self.assertFalse(self.store.get_checkpoint('nightly_nba_refresh'))

    async def test_schedule_readonly_fault_is_fatal_instead_of_remote_source_failure(self):
        self.seed_game()
        @contextmanager
        def readonly():
            db = sqlite3.connect(Path(self.store.path).as_uri()+'?mode=ro', uri=True); db.row_factory=sqlite3.Row
            try:
                with db: yield db
            finally: db.close()
        worker = AsyncMock()
        with patch.object(self.store, 'connect', readonly):
            with self.assertRaises(sqlite3.OperationalError) as raised:
                await refresh_game_schedule(self.store, worker, now=NOW)
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        worker.request.assert_not_awaited()

    async def test_nightly_service_propagates_native_readonly_without_softening_it(self):
        db = sqlite3.connect(Path(self.store.path).as_uri()+'?mode=ro', uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as actual:
                db.execute("INSERT INTO metadata VALUES ('readonly-proof','x')")
        finally: db.close()
        nightly = NightlyNBARefresh(self.store, AsyncMock(), now=lambda:NOW)
        nightly.tick = AsyncMock(side_effect=actual.exception)
        nightly._feed_failure = AsyncMock()
        with self.assertRaises(sqlite3.OperationalError) as raised:
            await nightly.run()
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        nightly._feed_failure.assert_not_awaited()
