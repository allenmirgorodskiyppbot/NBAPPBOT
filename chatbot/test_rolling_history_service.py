"""Current matchup careers keep updating without changing live readiness claims."""
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
from history_service import install_rolling_history, history_upgrade_service
from master import MasterStore
from test_history_backfill import identity, normalize_history, source_rows

NOW = dt.datetime(2026, 11, 5, 15, tzinfo=dt.timezone.utc)


class RollingCareerInstallTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.scope = {'population': 'all_current_nba_players', 'players': [identity()]}
        self.scope_patch = patch('dataset_refresh.load_scope', return_value=self.scope)
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)
        self.pairs = self.root / 'pairs.json'
        self.pairs.write_text(json.dumps({'pairs': [{'offense_player': 'Player 1', 'defense_player': 'Player 1'}]}))
        self.targets = self.root / 'targets.json'
        self.targets.write_text(json.dumps({'players': []}))
        self.requests = []
        raw = source_rows('2025-11-01')
        for row in raw:
            row['season'] = 2026
        games, rows, _ = normalize_history(raw, [identity()], 'https://example.org/historical-public-source',
            '2026-11-04T12:00:00+00:00', 'a' * 64, '2026-11-02')
        self.events = [{'kind': 'game', 'data': games[0]}, {'kind': 'player_game', 'data': rows[0]}]
        self.blob = gzip.compress(('\n'.join(json.dumps(row) for row in self.events) + '\n').encode())
        self.manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'identity_scope': 'current_players_and_named_matchup_participants',
            'source_data_through_date': '2026-11-02', 'players': [identity()],
            'parts': [{'file': 'career-001.jsonl.gz', 'bytes': len(self.blob),
                       'sha256': hashlib.sha256(self.blob).hexdigest(), 'records': len(self.events)}],
            'coverage': [{'requested_player': 'Player 1', **identity(), 'status': 'available_history',
                          'observations': 1, 'played_games': 1,
                          'first_date': '2025-11-01', 'last_date': '2025-11-01'}]}

    def client(self, manifest_status=200, part_status=200, redirect=False):
        def respond(request):
            self.requests.append(str(request.url))
            if redirect:
                return httpx.Response(302, headers={'Location': 'https://untrusted.invalid/manifest'})
            if str(request.url) == history_service.ROLLING_MANIFEST_URL:
                return httpx.Response(manifest_status, content=json.dumps(self.manifest).encode())
            return httpx.Response(part_status, content=self.blob)
        return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)

    def install(self, client):
        return install_rolling_history(self.store, self.root, now=NOW, client=client,
                                       matchup_path=self.pairs, targets_path=self.targets)

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def test_real_historical_import_preserves_live_rows_ledger_and_readiness(self):
        from test_history_backfill import normalized
        games, rows, _ = normalized()
        self.store.upsert_game(games[0]); self.store.upsert_player_game(rows[0])
        entry = self.store.create_entry([
            {'player': 'Player 1', 'stat': 'pts', 'line': 1.5, 'side': 'more', 'game_id': games[0]['id']},
            {'player': 'Player 11', 'stat': 'pts', 'line': 1.5, 'side': 'more', 'game_id': games[0]['id']}], 5, 'power', 3)
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete': False, 'inventory_verified': False})
        self.store.set_checkpoint('nba_schedule_today', {'status': 'failed', 'checked_at': '2026-11-05T14:00:00+00:00'})
        with self.store.connect() as db:
            live = dict(db.execute("SELECT * FROM player_games WHERE date='2026-11-02'").fetchone())
            entries = [dict(row) for row in db.execute('SELECT * FROM entries')]
        with self.client() as client:
            report = self.install(client)
        self.assertEqual(report['player_game_inserted'], 1)
        self.assertEqual((report['requested_players'], report['covered_players'], report['missing_players']), (1, 1, 0))
        self.assertEqual(self.requests, [history_service.ROLLING_MANIFEST_URL,
                                       history_service.ROLLING_BASE + '/career-001.jsonl.gz'])
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM player_games WHERE date='2026-11-02'").fetchone()), live)
            self.assertEqual([dict(row) for row in db.execute('SELECT * FROM entries')], entries)
            historical = dict(db.execute("SELECT * FROM player_games WHERE date='2025-11-01'").fetchone())
            self.assertEqual(historical['season'], '2025-26')
        self.assertEqual(self.store.get_entry(entry['id'])['id'], entry['id'])
        self.assertFalse(self.store.get_checkpoint('daily_nba:2026-11-04')['complete'])
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today')['status'], 'failed')
        self.assertEqual(self.store.get_checkpoint('matchup_career_history_status')['last_checked_at'], NOW.isoformat())
        with sqlite3.connect(report['backup']) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 1)

    def test_same_manifest_is_checked_without_reimport_and_unchanged_parts_reused_across_manifests(self):
        with self.client() as client:
            self.install(client)
            repeated = self.install(client)
            self.assertTrue(repeated['already_imported'])
            # A meaningful coverage note/revision can change the manifest without
            # requiring another copy of its identical historical part.
            self.manifest['limits'] = ['An independently documented source gap remains.']
            revised = self.install(client)
        self.assertTrue(revised['complete'])
        self.assertEqual(self.requests.count(history_service.ROLLING_BASE + '/career-001.jsonl.gz'), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 1)

    def test_rolling_parts_reuse_verified_pinned_cache_and_retention_preserves_pinned_artifacts(self):
        pinned = copy.deepcopy(self.manifest)
        pinned.pop('identity_scope')
        pinned.pop('coverage')
        path = self.root / 'pinned.json'
        path.write_text(json.dumps(pinned))
        with self.client() as client:
            history_service.install_pinned_history(self.store, self.root, 'd' * 40, path, client)
        pinned_dirs = {item for item in (self.root / 'history').iterdir()
                       if item.is_dir() and item.name != 'verified_parts'}
        pinned_backups = set((self.root / 'backups').glob('before-history-*.sqlite'))
        self.requests.clear()
        first = copy.deepcopy(self.manifest)
        with self.client() as client:
            for number in range(4):
                self.manifest['limits'] = [f'Documented source coverage revision {number}']
                self.install(client)
            # Returning to a previously imported, pruned manifest reconstructs
            # metadata-check input from the verified shared part cache.
            self.manifest = first
            self.install(client)
        self.assertTrue(all(request == history_service.ROLLING_MANIFEST_URL for request in self.requests))
        self.assertEqual(len(list((self.root / 'history' / 'rolling').iterdir())), 2)
        self.assertEqual(len(list((self.root / 'backups').glob('before-career-refresh-*.sqlite'))), 2)
        self.assertTrue(all(path.exists() for path in pinned_dirs | pinned_backups))

    def test_verified_rolling_game_metadata_completes_unknown_without_replacing_player_statistics(self):
        raw = source_rows('2025-11-01')
        for row in raw:
            row['season'] = 2026
            if row['team_abbreviation'] == 'CHI':
                row['team_score'] = 11
                if row['athlete_id'] == 11:
                    row['points'] = 3
            else:
                row['opponent_team_score'] = 11
        games, rows, _ = normalize_history(raw, [identity()],
            'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
            'espn_nba_player_boxscores/player_box_2026.parquet',
            '2026-11-04T12:00:00+00:00', 'a' * 64, '2026-11-02')
        game = {**games[0], 'upstream_full_game_validated': True}
        self.events = [{'kind': 'game', 'data': game}, {'kind': 'player_game', 'data': rows[0]}]
        self.blob = gzip.compress(('\n'.join(json.dumps(row) for row in self.events) + '\n').encode())
        self.manifest['parts'][0].update(bytes=len(self.blob), sha256=hashlib.sha256(self.blob).hexdigest())
        self.store.upsert_game({**game, 'status': 'unknown', 'home_score': None, 'away_score': None,
                                'source': 'workbook:dated original'})
        self.store.upsert_player_game(rows[0])
        self.store.set_checkpoint('nba_schedule_today', {'status': 'failed'})
        with self.store.connect() as db:
            before = dict(db.execute('SELECT * FROM player_games').fetchone())
        with self.client() as client:
            result = self.install(client)
        self.assertEqual(result['metadata_repairs']['games_completed'], 1)
        self.assertTrue(Path(result['metadata_repairs']['backup']).exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM games').fetchone()[0], 'final')
            self.assertEqual(dict(db.execute('SELECT * FROM player_games').fetchone()), before)
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'), {'status': 'failed'})

    def test_known_unresolved_matchup_is_reported_without_fabricated_rows(self):
        self.pairs.write_text(json.dumps({'pairs': [{'offense_player': 'Player 1', 'defense_player': 'Missing Veteran'}]}))
        self.manifest['coverage'].append({'player': 'Missing Veteran', 'requested_player': 'Missing Veteran',
            'status': 'unresolved_identity', 'reason': 'Two exact provider identities exist.'})
        with self.client() as client:
            report = self.install(client)
        self.assertEqual(report['missing_players'], 1)
        status = self.store.get_checkpoint('matchup_career_history_status')
        self.assertEqual(status['phase'], 'partial')
        self.assertEqual(status['missing_player_names'], ['Missing Veteran'])
        self.assertFalse(self.store.get_checkpoint('nba_schedule_today'))

    def test_omitted_named_matchup_cannot_be_hidden_by_successful_other_players(self):
        self.pairs.write_text(json.dumps({'pairs': [{'offense_player': 'Player 1', 'defense_player': 'Missing Veteran'}]}))
        with self.client() as client, self.assertRaisesRegex(ValueError, 'omits an explicitly named'):
            self.install(client)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.store.get_checkpoint('career_history_status'))

    def test_explicit_alias_keeps_the_one_registered_provider_and_canonical_identity(self):
        self.pairs.write_text(json.dumps({'pairs': [{'offense_player': 'Player 1 Jr.', 'defense_player': 'Player 1'}]}))
        self.targets.write_text(json.dumps({'players': [{'player': 'Player 1 Jr.', 'espn_player_id': '1',
                                                        'aliases': ['Player 1']}]}))
        self.manifest['players'][0]['aliases'].append('Player 1 Jr.')
        self.manifest['coverage'].append({'requested_player': 'Player 1 Jr.', 'player': 'Player 1',
            'player_id': identity()['player_id'], 'espn_player_id': '1', 'status': 'available_history'})
        self.manifest['target_resolution'] = [{'requested_player': 'Player 1 Jr.', 'player': 'Player 1',
            'player_id': identity()['player_id'], 'espn_player_id': '1', 'identity_basis': 'explicit_verified_alias'}]
        with self.client() as client:
            report = self.install(client)
        self.assertEqual(report['requested_players'], 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT player_id) FROM player_games').fetchone()[0], 1)
        registry = self.store.get_checkpoint('career_history_identity_scope')
        self.assertEqual(registry['players'][0]['espn_player_id'], '1')
        self.assertEqual(registry['target_resolution'][0]['requested_player'], 'Player 1 Jr.')

    def test_target_resolution_cannot_rebind_an_approved_alias_to_another_provider(self):
        self.manifest['target_resolution'] = [{'requested_player': 'Player 1', 'player': 'Player 1',
            'player_id': identity()['player_id'], 'espn_player_id': '999'}]
        with self.client() as client, self.assertRaisesRegex(ValueError, 'alias/provider binding'):
            self.install(client)
        self.assertEqual(self.requests, [history_service.ROLLING_MANIFEST_URL])
        self.assertIsNone(self.store.get_checkpoint('career_history_identity_scope'))

    def test_namesake_provider_mismatch_and_unrelated_retired_identity_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        for altered in ({**identity(), 'espn_player_id': '999'},
                        {'player': 'Unrelated Retiree', 'player_id': 'name:unrelatedretiree', 'espn_player_id': '9'}):
            self.manifest = copy.deepcopy(original)
            self.manifest['players'] = [altered]
            with self.client() as client, self.assertRaises(ValueError):
                self.install(client)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)

    def test_future_manifest_and_external_redirect_are_rejected_without_readiness_changes(self):
        self.store.set_checkpoint('nba_schedule_today', {'status': 'failed'})
        self.manifest['source_data_through_date'] = '2026-11-06'
        with self.client() as client, self.assertRaisesRegex(ValueError, 'future'):
            self.install(client)
        with self.client(redirect=True) as client, self.assertRaises(httpx.HTTPStatusError):
            self.install(client)
        self.assertFalse(any('untrusted.invalid' in request for request in self.requests))
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'), {'status': 'failed'})

    def test_refused_manifest_or_bad_part_retains_previous_history_and_status(self):
        with self.client() as client:
            self.install(client)
        before = self.dump()
        with self.client(manifest_status=403) as client, self.assertRaises(httpx.HTTPStatusError):
            self.install(client)
        self.assertEqual(self.dump(), before)
        self.manifest['parts'][0]['sha256'] = '0' * 64
        with self.client() as client, self.assertRaisesRegex(ValueError, 'checksum'):
            self.install(client)
        self.assertEqual(self.dump(), before)


class RollingCareerWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.supplemental_patch = patch('history_service._supplemental_cycle', return_value={'phase': 'updated', 'changed': False})
        self.supplemental = self.supplemental_patch.start()
        self.addCleanup(self.supplemental_patch.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = MasterStore(self.root / 'master.sqlite')

    async def test_supplemental_changes_reexport_even_when_career_is_unchanged(self):
        self.supplemental.return_value = {'phase': 'updated', 'changed': True}
        exporter = Mock()
        async def pause(seconds):
            raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', return_value={'complete': True, 'already_imported': True}), \
             patch('history_service.install_rolling_history', return_value={'already_imported': True}), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root, exporter=exporter)
        exporter.assert_called_once_with(self.store, self.root / 'exports')
        self.assertTrue(self.store.get_checkpoint('history_upgrade_status')['supplemental']['changed'])

    async def test_successful_rebuild_clears_only_a_previous_history_export_failure(self):
        self.store.set_checkpoint('history_upgrade_status', {
            'export_error': 'OSError', 'export_error_at': '2026-11-04T16:30:00+00:00'})
        self.supplemental.return_value = {'phase': 'updated', 'changed': True}
        exporter = Mock()
        async def pause(seconds):
            raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', return_value={'complete': True, 'already_imported': True}), \
             patch('history_service.install_rolling_history', return_value={'already_imported': True}), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root, exporter=exporter)
        saved = self.store.get_checkpoint('history_upgrade_status')
        self.assertNotIn('export_error', saved)
        self.assertNotIn('export_error_at', saved)
        self.assertIn('export_updated_at', saved)
        exporter.assert_called_once()

    async def test_failed_export_retries_even_when_all_source_data_is_unchanged(self):
        self.store.set_checkpoint('history_upgrade_status', {'export_error': 'OSError'})
        exporter = Mock()
        async def pause(seconds):
            raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', return_value={'complete': True, 'already_imported': True}), \
             patch('history_service.install_rolling_history', return_value={'already_imported': True}), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root, exporter=exporter)
        exporter.assert_called_once_with(self.store, self.root / 'exports')
        self.assertNotIn('export_error', self.store.get_checkpoint('history_upgrade_status'))

    async def test_career_outage_does_not_skip_new_availability_or_repaired_stats(self):
        self.supplemental.return_value = {'phase': 'updated', 'changed': True}
        exporter = Mock()
        async def pause(seconds):
            raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', side_effect=httpx.ReadTimeout('private source detail')), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root, exporter=exporter)
        self.supplemental.assert_awaited_once()
        exporter.assert_called_once_with(self.store, self.root / 'exports')
        status = self.store.get_checkpoint('history_upgrade_status')
        self.assertEqual(status['phase'], 'retrying')
        self.assertEqual(status['error_type'], 'ReadTimeout')
        self.assertNotIn('private source detail', json.dumps(status))

    async def test_worker_checks_again_hourly_without_restarting_or_reinstalling_pinned_bundle(self):
        pinned = Mock(return_value={'complete': True, 'already_imported': True})
        rolling = Mock(return_value={'complete': True, 'already_imported': True, 'missing_players': 0})
        sleeps = []
        async def pause(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', pinned), \
             patch('history_service.install_rolling_history', rolling), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root)
        self.assertEqual(pinned.call_count, 1)
        self.assertEqual(rolling.call_count, 2)
        self.assertEqual(sleeps, [3600, 3600])
        self.assertEqual(self.store.get_checkpoint('history_upgrade_status')['phase'], 'updated')

    async def test_rolling_failure_uses_hourly_retry_then_recovers_without_replacing_pinned_history(self):
        pinned = Mock(return_value={'complete': True, 'already_imported': True})
        rolling = Mock(side_effect=[httpx.ReadTimeout('private URL'),
            {'complete': True, 'already_imported': True, 'missing_players': 1}])
        sleeps = []
        async def pause(seconds):
            sleeps.append((seconds, self.store.get_checkpoint('history_upgrade_status')))
            if len(sleeps) == 2:
                raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', pinned), \
             patch('history_service.install_rolling_history', rolling), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root)
        self.assertEqual(pinned.call_count, 1)
        self.assertEqual([row[0] for row in sleeps], [3600, 3600])
        self.assertEqual(sleeps[0][1]['phase'], 'retrying')
        self.assertTrue(sleeps[0][1]['pinned_complete'])
        self.assertNotIn('private URL', json.dumps(sleeps[0][1]))
        self.assertEqual(sleeps[1][1]['phase'], 'partial')

    async def test_metadata_only_completion_reexports_previously_imported_history(self):
        pinned = Mock(return_value={'complete': True, 'already_imported': True})
        rolling = Mock(return_value={'complete': True, 'already_imported': True, 'missing_players': 0,
                                     'metadata_repairs': {'games_completed': 1}})
        exporter = Mock()
        async def pause(seconds):
            raise asyncio.CancelledError
        with patch('history_service.install_pinned_history', pinned), \
             patch('history_service.install_rolling_history', rolling), \
             patch('history_service.asyncio.sleep', side_effect=pause):
            with self.assertRaises(asyncio.CancelledError):
                await history_upgrade_service(self.store, self.root, exporter=exporter)
        exporter.assert_called_once_with(self.store, self.root / 'exports')


if __name__ == '__main__':
    unittest.main()
