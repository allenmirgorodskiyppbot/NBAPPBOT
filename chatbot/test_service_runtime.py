"""Offline recovery tests; no live workers, model calls, or Telegram messages."""
import asyncio
import unittest
from unittest.mock import patch

import service_runtime


class ServiceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_failure_restarts_fresh_coroutine_and_preserves_safe_diagnostics(self):
        registry, attempts, phases, delays = {}, [], [], []

        async def service():
            attempts.append(len(attempts))
            phases.append(dict(registry['feeds']))
            if len(attempts) == 1:
                raise OSError('DO_NOT_LEAK private token or source URL')

        async def wait(delay):
            delays.append(delay)
            self.assertEqual(registry['feeds']['phase'], 'retrying')
            self.assertIsNotNone(registry['feeds']['next_restart_at'])

        with patch.object(service_runtime, 'sleep', wait), self.assertLogs('knicks.services') as logs:
            await service_runtime.supervise_service('feeds', service, registry)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(delays, [1.0])
        self.assertEqual([state['restart_count'] for state in phases], [0, 1])
        self.assertEqual(registry['feeds']['last_failure_category'], 'OSError')
        self.assertEqual(registry['feeds']['phase'], 'stopped')
        self.assertEqual(registry['feeds']['stopped_reason'], 'completed')
        self.assertNotIn('DO_NOT_LEAK', repr(registry) + repr(logs.output))

    async def test_retry_backoff_is_bounded_and_resets_after_stable_run(self):
        registry, attempts, delays = {}, [], []

        async def service():
            attempts.append(None)
            if len(attempts) < 7:
                raise RuntimeError('failure')

        async def wait(delay):
            delays.append(delay)

        # Each failed attempt reads a start and failure time. The sixth run
        # lasts 300 seconds, resetting the delay instead of keeping the cap.
        clock = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 305, 306]
        with patch.object(service_runtime, 'sleep', wait), \
                patch.object(service_runtime, 'monotonic', side_effect=clock), \
                self.assertLogs('knicks.services'):
            await service_runtime.supervise_service('feeds', service, registry,
                                                     initial_delay=10, max_delay=60)
        self.assertEqual(delays, [10, 20, 40, 60, 60, 10])
        self.assertEqual(registry['feeds']['restart_count'], 6)

    async def test_disabled_service_normal_exit_never_restarts(self):
        calls, registry = [], {}

        async def disabled():
            calls.append(None)

        with patch.object(service_runtime, 'sleep', side_effect=AssertionError('No retry expected')):
            await service_runtime.supervise_service('telegram', disabled, registry)
        self.assertEqual(len(calls), 1)
        self.assertEqual(registry['telegram']['restart_count'], 0)
        self.assertIsNone(registry['telegram']['last_failure_category'])
        self.assertEqual(registry['telegram']['stopped_reason'], 'completed')

    async def test_cancel_during_service_propagates_and_does_not_restart(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        calls, registry = [], {}

        async def service():
            calls.append(None)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(service_runtime.supervise_service('feeds', service, registry))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(registry['feeds']['stopped_reason'], 'cancelled')
        self.assertEqual(registry['feeds']['restart_count'], 0)
        self.assertIsNone(registry['feeds']['next_restart_at'])

    async def test_cancel_during_backoff_never_starts_next_attempt(self):
        retrying = asyncio.Event()
        calls, registry = [], {}

        async def service():
            calls.append(None)
            raise OSError('unavailable')

        async def wait(delay):
            retrying.set()
            await asyncio.Event().wait()

        with patch.object(service_runtime, 'sleep', wait), self.assertLogs('knicks.services'):
            task = asyncio.create_task(service_runtime.supervise_service('feeds', service, registry))
            await asyncio.wait_for(retrying.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(calls), 1)
        self.assertEqual(registry['feeds']['restart_count'], 0)
        self.assertEqual(registry['feeds']['stopped_reason'], 'cancelled')

    async def test_synchronous_factory_failure_is_also_recoverable(self):
        calls, registry = [], {}

        async def completed():
            return

        def factory():
            calls.append(None)
            if len(calls) == 1:
                raise ConnectionError('failure before coroutine creation')
            return completed()

        async def wait(delay):
            return

        with patch.object(service_runtime, 'sleep', wait), self.assertLogs('knicks.services'):
            await service_runtime.supervise_service('feeds', factory, registry)
        self.assertEqual(len(calls), 2)
        self.assertEqual(registry['feeds']['last_failure_category'], 'ConnectionError')

    async def test_invalid_backoff_configuration_fails_before_running_service(self):
        async def service():
            self.fail('Invalid settings must not start a worker')

        for config in [{'initial_delay': -1}, {'initial_delay': 0}, {'max_delay': float('inf')},
                       {'stable_seconds': float('nan')}, {'initial_delay': 2, 'max_delay': 1}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                await service_runtime.supervise_service('feeds', service, {}, **config)


if __name__ == '__main__':
    unittest.main()
