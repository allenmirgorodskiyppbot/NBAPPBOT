"""Live check progress remains cheap and honest while a report is pending."""
import asyncio
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import app


class AppSelfTestStatusTests(unittest.IsolatedAsyncioTestCase):
    async def status(self, progress, *, locked=False):
        lock = asyncio.Lock()
        if locked:
            await lock.acquire()
        state = SimpleNamespace(self_test_lock=lock, self_test_progress=progress)
        with patch.object(app.app, 'state', state):
            return await app.self_test_status()

    async def test_slow_active_check_reports_its_stage_without_database_or_model_access(self):
        value = await self.status({'phase':'running', 'owner':'manual',
            'stage':'Master Excel export contains imported data',
            'started_monotonic':time.monotonic()-75}, locked=True)
        self.assertTrue(value['active'])
        self.assertEqual(value['phase'], 'running')
        self.assertEqual(value['stage'], 'Master Excel export contains imported data')
        self.assertGreaterEqual(value['elapsed_seconds'], 75)

    async def test_finished_report_keeps_its_actual_duration(self):
        value = await self.status({'phase':'completed', 'stage':'Finishing self-test report',
            'started_monotonic':time.monotonic()-700, 'elapsed_seconds':78.8})
        self.assertFalse(value['active'])
        self.assertEqual(value['phase'], 'completed')
        self.assertEqual(value['elapsed_seconds'], 78.8)

    async def test_queued_new_worker_does_not_show_a_stale_completed_stage(self):
        value = await self.status({'phase':'completed', 'stage':'Old completed check',
            'started_monotonic':time.monotonic()-700, 'elapsed_seconds':78.8}, locked=True)
        self.assertTrue(value['active'])
        self.assertEqual(value['phase'], 'queued')
        self.assertEqual(value['stage'], 'Waiting for the check worker')
        self.assertEqual(value['elapsed_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
