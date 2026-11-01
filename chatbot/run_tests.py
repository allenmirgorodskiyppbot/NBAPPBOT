"""Run offline regressions consistently in CI and the managed workspace.

Keep an idle test loop ticking while mocked services wait for worker threads.
This changes no assertions, network responses or application code. A separate
deadline fails any async test that genuinely stops making progress.
"""
import asyncio
import faulthandler
import inspect
from pathlib import Path
import sys
import unittest


def main():
    root = str(Path(__file__).resolve().parent)
    sys.path.insert(0, root)
    original_run = asyncio.BaseEventLoop.run_forever
    original_test = unittest.IsolatedAsyncioTestCase._callTestMethod

    def run_with_tick(loop):
        def tick():
            nonlocal handle
            handle = loop.call_later(.05, tick)
        handle = loop.call_later(.05, tick)
        try:
            return original_run(loop)
        finally:
            handle.cancel()

    def bounded_test(case, method):
        if not inspect.iscoroutinefunction(method):
            return original_test(case, method)
        async def run():
            async with asyncio.timeout(30):
                return await method()
        return original_test(case, run)

    asyncio.BaseEventLoop.run_forever = run_with_tick
    unittest.IsolatedAsyncioTestCase._callTestMethod = bounded_test
    faulthandler.dump_traceback_later(90, repeat=True)
    try:
        suite = unittest.defaultTestLoader.discover(root, pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        asyncio.BaseEventLoop.run_forever = original_run
        unittest.IsolatedAsyncioTestCase._callTestMethod = original_test


if __name__ == '__main__':
    sys.exit(main())
