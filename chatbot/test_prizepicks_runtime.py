"""Akash lifecycle wiring starts the public-feed service independently of AI."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app


class PrizePicksRuntimeTests(unittest.TestCase):
    def test_feed_runs_from_application_and_is_cancelled_before_client_close(self):
        import prizepicks_feed

        started = threading.Event()
        events, instances = [], []

        async def idle(*args, **kwargs):
            await asyncio.Future()

        def counts(store):
            with store.connect() as db:
                return {name: db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                        for name in ('player_games', 'entries', 'grade_events')}

        class RuntimeFeed:
            def __init__(self, store, **kwargs):
                self.store = store
                self.before = counts(store)
                self.stopped = False
                self.closed = False
                instances.append(self)

            async def run(self):
                events.append('feed-started')
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    self.stopped = True
                    events.append('feed-stopped')

            def status(self):
                return {'phase': 'unverified', 'offers': 0}

            async def aclose(self):
                self.closed = True
                events.append('feed-closed')

        class Telegram:
            enabled = False
            allowed_user_ids = set()

            def __init__(self, store, callback, **kwargs):
                self.store = store
                self.prizepicks_feed = kwargs.get('prizepicks_feed')

            async def notify(self, *args, **kwargs):
                raise AssertionError('No real Telegram delivery is part of this test.')

            async def run(self):
                await idle()

            async def shutdown(self):
                events.append('telegram-closed')

        worker = SimpleNamespace(run=idle)
        service = SimpleNamespace(run=idle)
        book = Mock(side_effect=RuntimeError('Controlled workbook startup failure before model access.'))
        forbidden_http = Mock(side_effect=AssertionError('Live HTTP is forbidden in this lifecycle fixture.'))

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(app, 'DATA_DIR', Path(directory)))
            stack.enter_context(patch.object(prizepicks_feed, 'PrizePicksFeed', RuntimeFeed))
            stack.enter_context(patch.object(app, 'PrizePicksFeed', RuntimeFeed, create=True))
            stack.enter_context(patch.object(app, 'TelegramBot', Telegram))
            stack.enter_context(patch.object(app, 'FeedWorker', return_value=worker))
            stack.enter_context(patch('gameday_digest.GamedayDigest', return_value=service))
            stack.enter_context(patch('nightly_data.NightlyNBARefresh', return_value=service))
            stack.enter_context(patch.object(app, 'maintenance', idle))
            stack.enter_context(patch('history_service.history_upgrade_service', idle))
            stack.enter_context(patch.object(app, 'configure_dataset_updates'))
            stack.enter_context(patch.object(app, 'WorkbookStore', book))
            stack.enter_context(patch.object(app.httpx, 'AsyncClient', forbidden_http))
            stack.enter_context(patch.object(app.log, 'exception'))
            with TestClient(app.app):
                self.assertTrue(started.wait(5), 'The app never started its PrizePicks service.')
                self.assertEqual(len(instances), 1)
                feed = instances[0]
                self.assertIs(app.app.state.prizepicks_feed, feed)
                self.assertIs(app.app.state.telegram.prizepicks_feed, feed)
                self.assertEqual(app.app.state.service_health['prizepicks-feed']['phase'], 'running')
                self.assertNotEqual(app.app.state.phase, 'ready')
                self.assertFalse(feed.closed)
                self.assertEqual(counts(feed.store), feed.before)
                forbidden_http.assert_not_called()
            self.assertTrue(feed.stopped)
            self.assertTrue(feed.closed)
            self.assertLess(events.index('feed-stopped'), events.index('feed-closed'))
            self.assertTrue(all(task.done() for task in app.app.state.service_tasks))
            self.assertEqual(counts(feed.store), feed.before)
            forbidden_http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
