"""Pinned downloads, additive upgrade backups and bounded worker recovery."""
import asyncio
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

import history_service
from history_service import history_upgrade_service, install_pinned_history
from master import MasterStore
from test_history_backfill import identity, normalized

REF = 'd' * 40


class PartStream(httpx.SyncByteStream):
    def __init__(self, parts):
        self.parts = parts

    def __iter__(self):
        yield from self.parts


class HistoryServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        games, rows, _ = normalized()
        self.events = [{'kind': 'game', 'data': games[0]}, {'kind': 'player_game', 'data': rows[0]}]
        self.blob = gzip.compress(('\n'.join(json.dumps(r) for r in self.events) + '\n').encode())
        self.manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
                         'source_data_through_date': '2026-11-02', 'players': [identity()],
                         'parts': [{'file': 'career-part-001.jsonl.gz', 'bytes': len(self.blob),
                                    'sha256': hashlib.sha256(self.blob).hexdigest(), 'records': 2}]}
        self.path = self.root / 'release-manifest.json'
        self.write_manifest()
        self.requests = []

    def tearDown(self):
        self.directory.cleanup()

    def write_manifest(self):
        self.path.write_text(json.dumps(self.manifest))

    def client(self, blob=None, status=200, stream=None):
        def serve(request):
            self.requests.append(str(request.url))
            if stream is not None:
                return httpx.Response(status, stream=stream)
            return httpx.Response(status, content=self.blob if blob is None else blob)
        return httpx.Client(transport=httpx.MockTransport(serve))

    def install(self, client, **kwargs):
        return install_pinned_history(self.store, self.root, REF, self.path, client, **kwargs)

    def count(self, table):
        with self.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_exact_deployed_ref_url_checksum_backup_before_merge_and_repeat_idempotency(self):
        self.store.set_checkpoint('preserve_this', {'setting': 'live'})
        with self.client() as client:
            result = self.install(client)
            self.assertFalse(client.is_closed)
            again = self.install(client)
        expected = ('https://raw.githubusercontent.com/allenmirgorodskiyppbot/NBAPPBOT/' + REF +
                    '/data/history/career-part-001.jsonl.gz')
        self.assertEqual(self.requests, [expected])
        self.assertTrue(result['complete'])
        self.assertTrue(again['already_imported'])
        self.assertEqual(self.count('player_games'), 1)
        with sqlite3.connect(result['backup']) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)
            self.assertIn('live', db.execute("SELECT value_json FROM checkpoints WHERE name='preserve_this'").fetchone()[0])
        self.assertEqual(self.store.get_checkpoint('preserve_this'), {'setting': 'live'})

    def test_bad_source_ref_never_fetches(self):
        for ref in ('main', 'd' * 39, 'd' * 41, 'D' * 40, '../main', None):
            with self.subTest(ref=ref), self.client() as client:
                with self.assertRaisesRegex(ValueError, 'SOURCE_REF'):
                    install_pinned_history(self.store, self.root, ref, self.path, client)
        self.assertEqual(self.requests, [])

    def test_invalid_part_paths_and_manifest_shapes_rejected_before_any_download(self):
        cases = [[], {**self.manifest, 'schema_version': 9}, {**self.manifest, 'parts': [None]},
                 {**self.manifest, 'parts': []},
                 {**self.manifest, 'parts': self.manifest['parts'] * 2}]
        for name in ('../escape.jsonl.gz', '/abs.jsonl.gz', 'x/y.jsonl.gz', 'https://bad/x.jsonl.gz', 'bad.gz'):
            value = copy.deepcopy(self.manifest)
            value['parts'][0]['file'] = name
            cases.append(value)
        for case in cases:
            self.path.write_text(json.dumps(case))
            with self.subTest(case=case), self.client() as client:
                with self.assertRaises(ValueError):
                    self.install(client)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.count('games'), 0)

    def test_size_bound_and_checksum_failure_leave_database_and_no_partial_file(self):
        for blob, reason in ((self.blob + b'x', 'published size'),
                             (self.blob[:-1], 'checksum'),
                             (b'x' * len(self.blob), 'checksum')):
            with self.subTest(reason=reason), self.client(blob) as client:
                with self.assertRaisesRegex(ValueError, reason):
                    self.install(client)
            self.assertEqual(self.count('games'), 0)
            self.assertEqual(list(self.root.rglob('*.download')), [])
            self.assertEqual(list((self.root / 'backups').glob('*.sqlite')), [])

    def test_stream_overflow_stops_at_bound_without_requiring_large_blob(self):
        self.manifest['parts'][0]['bytes'] = 8
        self.write_manifest()
        stream = PartStream([b'1234', b'56789', b'unread'])
        with self.client(stream=stream) as client, self.assertRaisesRegex(ValueError, 'published size'):
            self.install(client)
        self.assertEqual(self.count('games'), 0)
        self.assertFalse(list(self.root.rglob('*.download')))

    def test_http_denial_does_not_merge_or_relabel_any_source(self):
        self.store.set_checkpoint('history_fixture', {'current': 'preserved'})
        with self.client(status=403) as client, self.assertRaises(httpx.HTTPStatusError):
            self.install(client)
        self.assertEqual(self.count('games'), 0)
        self.assertEqual(self.store.get_checkpoint('history_fixture'), {'current': 'preserved'})
        self.assertIsNone(self.store.get_checkpoint('career_history_status'))
        self.assertFalse(list(self.root.rglob('*.download')))

    def test_manifest_urls_cannot_override_pinned_host_and_redirect_is_not_followed(self):
        self.manifest['repository'] = 'untrusted/repository'
        self.manifest['parts'][0]['url'] = 'https://untrusted.invalid/replacement.gz'
        self.write_manifest()
        def redirect(request):
            self.requests.append(str(request.url))
            return httpx.Response(302, headers={'Location': 'https://untrusted.invalid/replacement.gz'})
        with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                self.install(client)
        self.assertEqual(self.requests, [
            f'https://raw.githubusercontent.com/{history_service.REPOSITORY}/{REF}/data/history/career-part-001.jsonl.gz'])
        self.assertEqual(self.count('games'), 0)

    def test_cached_verified_parts_are_reused_but_retry_backup_is_current(self):
        with self.client() as client:
            with patch('history_service.import_history_bundle', side_effect=ValueError('fixture rejected')):
                with self.assertRaises(ValueError):
                    self.install(client)
            self.store.set_checkpoint('arrived_between_attempts', {'retain': True})
            result = self.install(client)
        self.assertEqual(len(self.requests), 1)
        with sqlite3.connect(result['backup']) as db:
            self.assertIsNotNone(db.execute("SELECT value_json FROM checkpoints WHERE name='arrived_between_attempts'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)

    def test_corrupted_cached_part_is_downloaded_again(self):
        with self.client() as client:
            with patch('history_service.import_history_bundle', side_effect=ValueError('fixture')):
                with self.assertRaises(ValueError):
                    self.install(client)
            target = next((self.root / 'history').rglob('*.jsonl.gz'))
            target.write_bytes(b'x' * len(self.blob))
            result = self.install(client)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result['complete'])

    def test_backup_failure_prevents_row_merge(self):
        with self.client() as client, patch.object(self.store, 'backup', side_effect=OSError('disk full')), \
             patch('history_service.import_history_bundle') as merge:
            with self.assertRaises(OSError):
                self.install(client)
        merge.assert_not_called()
        self.assertEqual(self.count('games'), 0)

    def test_valid_checksum_but_invalid_identity_rolls_back_import(self):
        self.events[1]['data']['espn_player_id'] = 'not-in-manifest'
        self.blob = gzip.compress(('\n'.join(json.dumps(r) for r in self.events) + '\n').encode())
        self.manifest['parts'][0].update(bytes=len(self.blob), sha256=hashlib.sha256(self.blob).hexdigest())
        self.write_manifest()
        with self.client() as client, self.assertRaisesRegex(ValueError, 'identity'):
            self.install(client)
        self.assertEqual(self.count('games'), 0)
        self.assertEqual(self.count('player_games'), 0)
        self.assertIsNone(self.store.get_checkpoint('career_history_status'))


class HistoryUpgradeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.directory.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await self.original_sleep(.01)

    async def test_not_ready_waits_then_installs_once_and_exports(self):
        self.original_sleep = asyncio.sleep
        sleeps = []
        async def sleep(seconds):
            sleeps.append(seconds)
            await self.original_sleep(0)
        readiness = Mock(side_effect=[False, True])
        install = Mock(return_value={'complete': True, 'player_games_added': 8})
        export = Mock()
        with patch('history_service.asyncio.sleep', side_effect=sleep), \
             patch('history_service.install_pinned_history', install), patch.dict('os.environ', {'SOURCE_REF': REF}):
            task = asyncio.create_task(history_upgrade_service(self.store, self.root, ready=readiness, exporter=export))
            self.tasks.append(task)
            await self.until(lambda: export.call_count == 1)
            self.assertEqual(sleeps, [5])
            install.assert_called_once_with(self.store, self.root, REF)
            export.assert_called_once_with(self.store, self.root / 'exports')
            self.assertEqual(self.store.get_checkpoint('history_upgrade_status')['phase'], 'updated')
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_install_records_hourly_retry_and_keeps_existing_data(self):
        self.original_sleep = asyncio.sleep
        observed = []
        async def pause(seconds):
            observed.append((seconds, self.store.get_checkpoint('history_upgrade_status')))
            raise asyncio.CancelledError
        self.store.set_checkpoint('live_fixture', {'unchanged': True})
        with patch('history_service.install_pinned_history', side_effect=httpx.ConnectError('private details')), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root)
        seconds, status = observed[0]
        self.assertEqual(seconds, 3600)
        self.assertEqual(status['phase'], 'retrying')
        self.assertEqual(status['error_type'], 'ConnectError')
        self.assertNotIn('private details', json.dumps(status))
        retry = dt.datetime.fromisoformat(status['next_retry_at'])
        attempt = dt.datetime.fromisoformat(status['last_attempt'])
        self.assertAlmostEqual((retry - attempt).total_seconds(), 3600, delta=2)
        self.assertEqual(self.store.get_checkpoint('live_fixture'), {'unchanged': True})

    async def test_export_failure_does_not_label_completed_database_import_failed(self):
        self.original_sleep = asyncio.sleep
        with patch('history_service.install_pinned_history', return_value={'complete': True}), \
             patch.dict('os.environ', {'SOURCE_REF': REF}):
            task = asyncio.create_task(history_upgrade_service(self.store, self.root,
                                       exporter=Mock(side_effect=OSError('private filesystem details'))))
            self.tasks.append(task)
            await self.until(lambda: self.store.get_checkpoint('history_upgrade_status', {}).get('export_error'))
            status = self.store.get_checkpoint('history_upgrade_status')
            self.assertEqual(status['phase'], 'updated')
            self.assertTrue(status['complete'])
            self.assertEqual(status['export_error'], 'OSError')
            self.assertNotIn('private filesystem', json.dumps(status))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_already_imported_manifest_does_not_reexport(self):
        self.original_sleep = asyncio.sleep
        exporter = Mock()
        with patch('history_service.install_pinned_history', return_value={'complete': True, 'already_imported': True}):
            task = asyncio.create_task(history_upgrade_service(self.store, self.root, exporter=exporter))
            self.tasks.append(task)
            await self.until(lambda: self.store.get_checkpoint('history_upgrade_status', {}).get('phase') == 'updated')
            exporter.assert_not_called()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
