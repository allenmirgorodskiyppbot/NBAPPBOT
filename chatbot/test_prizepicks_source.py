"""Provider selection is explicit, credential-safe, and included in Akash files."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bootstrap import APP_FILES
from prizepicks_source import (InvalidPrizePicksSource, create_prizepicks_feed,
                               prizepicks_source_name)

ROOT = Path(__file__).resolve().parent


class PrizePicksSourceTests(unittest.TestCase):
    def selection(self, settings):
        public, odds = Mock(name='public_factory'), Mock(name='odds_factory')
        store, client = object(), object()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, settings, clear=True))
            stack.enter_context(patch.dict(sys.modules, {
                'prizepicks_feed': SimpleNamespace(PrizePicksFeed=public),
                'the_odds_api_feed': SimpleNamespace(TheOddsAPIFeed=odds)}))
            feed = create_prizepicks_feed(store, client=client)
            label = prizepicks_source_name()
        return feed, label, public, odds, store, client

    def test_auto_without_key_uses_existing_public_source(self):
        for settings in ({}, {'THE_ODDS_API_KEY': '  '}):
            with self.subTest(settings=settings):
                feed, label, public, odds, store, client = self.selection(settings)
                self.assertIs(feed, public.return_value)
                self.assertEqual(label, 'public_site_feed')
                public.assert_called_once_with(store, client=client)
                odds.assert_not_called()

    def test_auto_with_key_selects_odds_without_copying_key_into_arguments(self):
        feed, label, public, odds, store, client = self.selection({'THE_ODDS_API_KEY': 'synthetic-secret'})
        self.assertIs(feed, odds.return_value)
        self.assertEqual(label, 'the_odds_api')
        odds.assert_called_once_with(store, client=client)
        public.assert_not_called()

    def test_explicit_odds_without_key_stays_on_odds_for_missing_key_diagnostic(self):
        feed, label, public, odds, store, client = self.selection({'PRIZEPICKS_FEED_PROVIDER': 'the_odds_api'})
        self.assertIs(feed, odds.return_value)
        self.assertEqual(label, 'the_odds_api')
        odds.assert_called_once_with(store, client=client)
        public.assert_not_called()

    def test_explicit_public_with_key_never_uses_odds(self):
        feed, label, public, odds, store, client = self.selection({
            'THE_ODDS_API_KEY': 'synthetic-secret', 'PRIZEPICKS_FEED_PROVIDER': 'public'})
        self.assertIs(feed, public.return_value)
        self.assertEqual(label, 'public_site_feed')
        public.assert_called_once_with(store, client=client)
        odds.assert_not_called()

    def test_unknown_provider_is_closed_and_does_not_expose_the_bad_value(self):
        feed, label, public, odds, _, _ = self.selection({
            'THE_ODDS_API_KEY': 'synthetic-secret', 'PRIZEPICKS_FEED_PROVIDER': 'unknown-private-value'})
        self.assertIsInstance(feed, InvalidPrizePicksSource)
        self.assertEqual(label, 'configuration_error')
        self.assertEqual(feed.status()['phase'], 'configuration_error')
        self.assertEqual(feed.status()['error_code'], 'invalid_provider')
        self.assertNotIn('unknown-private-value', json.dumps(feed.status()))
        self.assertNotIn('synthetic-secret', json.dumps(feed.status()))
        public.assert_not_called()
        odds.assert_not_called()

    def test_disabled_source_context_names_manual_uploads(self):
        with patch.dict(os.environ, {'PRIZEPICKS_FEED_ENABLED': '0', 'THE_ODDS_API_KEY': 'synthetic-secret'}, clear=True):
            self.assertEqual(prizepicks_source_name(), 'manual_uploads')

    def test_deployment_manifest_contains_both_providers_and_factory_once(self):
        for name in ('prizepicks_feed.py', 'the_odds_api_feed.py', 'prizepicks_source.py'):
            self.assertEqual(APP_FILES.count(name), 1)
            self.assertTrue((ROOT / name).is_file(), name)

    def test_clean_downloaded_package_imports_factory_and_both_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in APP_FILES:
                shutil.copyfile(ROOT / name, Path(directory) / name)
            # Do not pass a configured service credential to the fixture process.
            test_environment = {key: value for key, value in os.environ.items()
                                if key != 'THE_ODDS_API_KEY'}
            result = subprocess.run([sys.executable, '-I', '-c',
                'import sys; sys.path.insert(0,sys.argv[1]); '
                'import prizepicks_source, prizepicks_feed, the_odds_api_feed', directory],
                cwd=directory, env=test_environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


class InvalidSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_configuration_has_no_offers_and_worker_is_cancellable(self):
        feed = InvalidPrizePicksSource()
        self.assertEqual(await feed.refresh(force=True), feed.status())
        self.assertEqual(await feed.ensure_for_game({'id': 'saved-game'}), feed.status())
        bundle = feed.offers_for_game({'id': 'saved-game'})
        self.assertFalse(bundle['fresh'])
        self.assertEqual(bundle['offers'], [])
        self.assertEqual(bundle['legs'], [])
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await feed.aclose()


if __name__ == '__main__':
    unittest.main()
