"""Independent refresh clocks, honest coverage, and export/rollback regressions."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from dataset_refresh import initialize_scope
from feeds import canonical_player
from master import MasterStore
from nightly_data import NightlyNBARefresh, import_daily_snapshot
from test_nightly_data import snapshot


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 31, 15, 30, tzinfo=UTC)  # 11:30 Eastern.
TARGET = '2026-10-30'


async def _inline_executor(function, *args, **kwargs):
    # These cases verify scheduling/publication semantics with real importers
    # and a temporary SQLite database, not thread-pool scheduling. Running their
    # synchronous work inline also avoids the sandbox's executor wake-up delay.
    return function(*args, **kwargs)


class RefreshSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.scope = initialize_scope(self.store)
        self.now = [NOW]
        self.calls = []
        self.fail_games = set()
        self.fail_tracking = False
        self.points = 100
        self.tracking_through = '2026-10-29'
        self.file_through = None
        self.env = patch.dict('os.environ', {
            'DAILY_NBA_REFRESH': '1',
            'NBA_DAILY_DATA_URL': 'https://example.com/fixtures',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        owner = self

        class Worker:
            async def request(self, url):
                name = url.rsplit('/', 1)[-1]
                owner.calls.append(name)
                if name == 'existing_tracking.json':
                    if owner.fail_tracking:
                        raise httpx.ReadTimeout('fixture publication delayed')
                    return owner.tracking()
                day = name[:-5]
                if day in owner.fail_games:
                    response = httpx.Response(404, request=httpx.Request('GET', url))
                    response.raise_for_status()
                return owner.empty_day(day)

        self.worker = Worker()
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [],
            'handled_target_date': '2026-10-29',
        })

    def empty_day(self, day):
        return {
            'schema_version': 1, 'scope_revision': self.scope['revision'],
            'upstream_full_game_validated': True, 'source': 'espn',
            'date': day, 'published_at': self.now[0].isoformat(),
            'games': [], 'player_games': [], 'expected_games': 0,
            'complete': True, 'pending_game_ids': [], 'errors': [],
            'inventory_scope': 'Completed games present in the fixture publication only.',
            'source_data_through_date': self.file_through or day,
        }

    def tracking(self):
        record = {
            'entity': 'Tyrese Maxey', 'player_id': canonical_player('Tyrese Maxey'),
            'category': 'Player totals', 'season': '2026-27',
            'season_type': 'Regular Season', 'team': 'PHI', 'gp': 5,
            'fields': {'pts': self.points, 'reb': 20, 'ast': 25, 'minutes': 150},
            'published_at': self.now[0].isoformat(),
            'data_through_date': self.tracking_through,
            'coverage_basis': 'source_reported',
            'source': 'https://example.com/fixture-player-totals',
            'population': 'All actual opponents',
        }
        return {
            'schema_version': 1, 'scope_revision': self.scope['revision'],
            'season': '2026-27', 'last_attempt': self.now[0].isoformat(),
            'source_data_through_date': self.tracking_through,
            'coverage': [{'category': 'Player totals', 'status': 'collected',
                          'records': 1, 'source_data_through_date': self.tracking_through}],
            'records': [record],
        }

    def service(self, exporter=None):
        return NightlyNBARefresh(self.store, self.worker, now=lambda: self.now[0],
                                 exporter=exporter, data_dir=self.root)

    def run_tick(self, service):
        with patch('nightly_data.asyncio.to_thread', _inline_executor):
            asyncio.run(service.tick())

    def status(self):
        return self.store.get_checkpoint('nightly_nba_refresh')

    def test_failed_tracking_retries_hourly_after_game_pending_list_is_empty(self):
        self.fail_tracking = True
        service = self.service()
        self.run_tick(service)
        first = self.status()
        self.assertEqual(first['pending_dates'], [])
        self.assertEqual(first['game_refresh_phase'], 'updated')
        self.assertEqual(first['tracking_refresh_phase'], 'unavailable')
        game_attempt = first['last_attempt']
        self.calls.clear()

        self.now[0] += dt.timedelta(seconds=3599)
        self.run_tick(service)
        self.assertEqual(self.calls, [])

        self.fail_tracking = False
        self.now[0] += dt.timedelta(seconds=1)
        self.run_tick(service)
        self.assertEqual(self.calls, ['existing_tracking.json'])
        self.assertEqual(self.status()['last_attempt'], game_attempt)
        self.assertEqual(self.status()['last_tracking_attempt'], self.now[0].isoformat())
        self.assertEqual(self.status()['tracking_refresh_phase'], 'collected')

    def test_later_tracking_publication_updates_even_after_prior_tracking_success(self):
        service = self.service()
        self.run_tick(service)
        self.calls.clear()
        self.points = 125
        self.now[0] += dt.timedelta(hours=1)
        self.run_tick(service)
        self.assertEqual(self.calls, ['existing_tracking.json'])
        with self.store.connect() as db:
            row = db.execute("SELECT payload_json FROM refreshed_tracking WHERE entity='Tyrese Maxey'").fetchone()
        record = json.loads(row[0])
        self.assertEqual(record['fields']['pts'], 125)
        self.assertEqual(record['data_through_date'], '2026-10-29')
        self.assertEqual(self.status()['source_data_through_date'], TARGET)
        self.assertEqual(self.status()['tracking_source_data_through_date'], '2026-10-29')

    def test_tracking_coverage_does_not_make_missing_game_day_current(self):
        self.fail_games = {TARGET}
        self.tracking_through = TARGET
        service = self.service()
        self.run_tick(service)
        state = self.status()
        self.assertEqual(state['source_data_through_date'], '2026-10-29')
        self.assertEqual(state['tracking_source_data_through_date'], TARGET)
        self.assertEqual(state['game_refresh_phase'], 'waiting_for_publication')
        self.assertEqual(state['tracking_refresh_phase'], 'collected')
        self.assertIn(TARGET, state['pending_dates'])

    def test_publication_wide_newer_date_and_legacy_status_do_not_advance_imported_games(self):
        self.file_through = '2026-10-31'
        self.fail_games = {TARGET}
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [],
            'handled_target_date': '2026-10-29',
            'source_data_through_date': '2026-10-31',
        })
        self.run_tick(self.service())
        self.assertEqual(self.status()['source_data_through_date'], '2026-10-29')
        self.assertEqual(self.status()['phase'], 'waiting_for_publication')
        self.assertFalse(self.store.get_checkpoint('daily_nba:2026-10-31'))

    def test_older_changed_publication_cannot_undo_newer_final_correction(self):
        old = snapshot(20)
        old['published_at'] = '2026-10-30T14:00:00+00:00'
        newer = copy.deepcopy(old)
        newer['published_at'] = '2026-10-31T14:00:00+00:00'
        newer['player_games'][0]['pts'] = 27
        newer['games'][0]['home_score'] += 7
        accepted = import_daily_snapshot(self.store, newer, newer['date'], NOW)
        ignored = import_daily_snapshot(self.store, old, old['date'], NOW)
        self.assertTrue(ignored['ignored_older_publication'])
        self.assertEqual(self.store.get_checkpoint('daily_nba:' + old['date'])['sha256'], accepted['sha256'])
        with self.store.connect() as db:
            row = db.execute('SELECT pts FROM player_games WHERE player_id=?',
                             (canonical_player('Tyrese Maxey'),)).fetchone()
        self.assertEqual(row['pts'], 27)

    def test_export_observes_persisted_current_failure_status_even_without_new_statistics(self):
        seen = []

        def exporter(store, directory):
            seen.append(copy.deepcopy(store.get_checkpoint('nightly_nba_refresh')))
            self.assertEqual(directory, self.root / 'exports')

        self.fail_tracking = True
        service = self.service(exporter)
        self.run_tick(service)
        self.assertEqual(seen[-1]['last_tracking_attempt'], self.now[0].isoformat())
        self.assertEqual(seen[-1]['tracking_refresh_phase'], 'unavailable')
        self.assertEqual(seen[-1]['game_refresh_phase'], 'updated')
        self.now[0] += dt.timedelta(hours=1)
        self.run_tick(service)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[-1]['last_tracking_attempt'], self.now[0].isoformat())
        self.assertEqual(seen[-1]['tracking_refresh_phase'], 'unavailable')
        self.assertEqual(self.status()['master_export_updated_at'], self.now[0].isoformat())

    def test_before_deadline_and_frequent_ticks_do_not_hammer_sources_or_exports(self):
        self.now[0] = NOW - dt.timedelta(seconds=1)
        exports = []
        service = self.service(lambda store, directory: exports.append(directory))
        self.run_tick(service)
        self.assertEqual(self.calls, [])
        self.assertEqual(exports, [])
        self.now[0] = NOW
        self.run_tick(service)
        calls = list(self.calls)
        self.assertEqual(len(exports), 1)
        for seconds in (1, 30, 300, 3599):
            self.now[0] = NOW + dt.timedelta(seconds=seconds)
            self.run_tick(service)
        self.assertEqual(self.calls, calls)
        self.assertEqual(len(exports), 1)

    def test_winter_deadline_remains_1130_eastern(self):
        self.now[0] = dt.datetime(2026, 11, 3, 16, 29, 59, tzinfo=UTC)
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [], 'handled_target_date': '2026-11-01',
        })
        service = self.service()
        self.run_tick(service)
        self.assertEqual(self.calls, [])
        self.now[0] += dt.timedelta(seconds=1)
        self.run_tick(service)
        self.assertEqual(self.calls[0], '2026-11-02.json')
        self.assertEqual(self.status()['next_daily_run'], '2026-11-04T11:30:00-05:00')

    def test_current_target_success_keeps_older_missing_backfill_visible(self):
        self.fail_games = {'2026-10-27'}
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': ['2026-10-27'],
            'handled_target_date': '2026-10-29',
        })
        self.run_tick(self.service())
        state = self.status()
        self.assertEqual(state['game_refresh_phase'], 'updated')
        self.assertEqual(state['source_data_through_date'], TARGET)
        self.assertEqual(state['older_pending_dates'], ['2026-10-27'])
        self.assertIn('2026-10-27', state['pending_dates'])
        self.assertIn('not independently verified', state['completeness_scope'])
        feed = next(row for row in self.store.status()['feeds'] if row['name'] == 'nba_daily_refresh')
        self.assertIsNone(feed['error'])
        self.assertIn('1 older dates remain pending', feed['detail'])

    def test_multi_day_outage_catches_every_missed_date_in_bounded_restartable_batches(self):
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [], 'handled_target_date': '2026-10-20',
            'last_attempt': (NOW - dt.timedelta(minutes=5)).isoformat(),
        })
        expected = [(dt.date(2026, 10, 30) - dt.timedelta(days=n)).isoformat()
                    for n in range(10)]
        self.run_tick(self.service())
        first_dates = [name[:-5] for name in self.calls if name != 'existing_tracking.json']
        self.assertEqual(first_dates, expected[:3])
        self.assertEqual(self.status()['pending_dates'], expected[3:])
        self.assertEqual(self.status()['handled_target_date'], TARGET)

        for _ in range(3):
            self.now[0] += dt.timedelta(hours=1)
            before = len(self.calls)
            # A new service instance models a restart with the persisted queue.
            self.run_tick(self.service())
            self.assertLessEqual(len([name for name in self.calls[before:]
                                      if name != 'existing_tracking.json']), 3)
        imported_dates = [name[:-5] for name in self.calls if name != 'existing_tracking.json']
        self.assertEqual(imported_dates, expected)
        self.assertEqual(self.status()['pending_dates'], [])
        for day in expected:
            self.assertTrue(self.store.get_checkpoint('daily_nba:' + day)['complete'])

    def test_export_failure_preserves_last_success_time_and_reports_error(self):
        previous = '2026-10-30T15:30:00+00:00'
        state = self.status()
        state['master_export_updated_at'] = previous
        self.store.set_checkpoint('nightly_nba_refresh', state)

        def failed_export(store, directory):
            raise OSError('fixture read-only export directory')

        self.run_tick(self.service(failed_export))
        self.assertEqual(self.status()['master_export_updated_at'], previous)
        self.assertEqual(self.status()['master_export_error'], 'OSError')
        errors = {r['name']: r['error'] for r in self.store.status()['feeds']}
        self.assertIn('previous Excel export retained', errors['master_export'])

    def test_export_failure_retries_without_waiting_for_another_source_refresh(self):
        # Before the morning deadline, an otherwise current service has no game
        # or tracking fetch to trigger export recovery. It must still retry the
        # failed export, but must not spin once a minute while the disk is full.
        self.now[0] = dt.datetime(2026, 11, 1, 14, 0, tzinfo=UTC)  # 9 AM Eastern.
        old_attempt = self.now[0] - dt.timedelta(minutes=30)
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [], 'handled_target_date': TARGET,
            'last_export_attempt': old_attempt.isoformat(),
            'master_export_updated_at': '2026-10-31T15:00:00+00:00',
            'master_export_error': 'OSError',
        })
        self.store.feed_failure('master_export', 'OSError; previous Excel export retained.')
        exports = []
        service = self.service(lambda store, directory: exports.append(directory))
        self.run_tick(service)
        self.assertEqual(exports, [])
        self.assertEqual(self.calls, [])

        self.now[0] += dt.timedelta(minutes=30)
        self.run_tick(service)
        self.assertEqual(exports, [self.root / 'exports'])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.status()['master_export_error'])
        self.assertEqual(self.status()['master_export_updated_at'], self.now[0].isoformat())
        feed = next(row for row in self.store.status()['feeds'] if row['name'] == 'master_export')
        self.assertIsNone(feed['error'])
        self.assertIn('source freshness remains separate', feed['detail'])

        self.now[0] += dt.timedelta(minutes=1)
        self.run_tick(service)
        self.assertEqual(len(exports), 1)

    def test_repeated_export_failure_does_not_retry_every_worker_tick(self):
        self.now[0] = dt.datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
        self.store.set_checkpoint('nightly_nba_refresh', {
            'initialized': True, 'pending_dates': [], 'handled_target_date': TARGET,
            'master_export_updated_at': '2026-10-31T15:00:00+00:00',
            'master_export_error': 'OSError',
        })
        attempts = []

        def failed_export(store, directory):
            attempts.append(directory)
            raise OSError('fixture disk temporarily unavailable')

        service = self.service(failed_export)
        self.run_tick(service)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.status()['last_export_attempt'], self.now[0].isoformat())
        self.now[0] += dt.timedelta(minutes=1)
        self.run_tick(service)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.status()['master_export_updated_at'], '2026-10-31T15:00:00+00:00')

    def test_import_keeps_publication_inventory_scope_instead_of_claiming_full_league(self):
        document = self.empty_day(TARGET)
        report = import_daily_snapshot(self.store, document, TARGET, NOW)
        self.assertEqual(report['inventory_scope'], document['inventory_scope'])
        self.assertEqual(self.store.get_checkpoint('daily_nba:' + TARGET)['inventory_scope'],
                         'Completed games present in the fixture publication only.')


if __name__ == '__main__':
    unittest.main()
