"""Trusted runtime relays, independent failure and source-clock preservation."""
import asyncio
import copy
import datetime as dt
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from feeds import canonical_game, canonical_player
from master import MasterStore
import measured_history as mh
import supplemental_data as sd
from structured_availability import build_availability_document, normalize_official_pdf
from test_measured_history import SCOPE, PAIRS, observation

NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)
SOURCE = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
          'espn_nba_player_boxscores/player_box_2025.parquet')


class SupplementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = MasterStore(self.root / 'master.sqlite')
        self.identity = {'player': 'Josh Giddey', 'player_id': canonical_player('Josh Giddey'),
                         'espn_player_id': '4871145', 'aliases': ['Josh Giddey']}
        self.game = {'id': canonical_game('2025-01-25', 'CHI', 'PHI'), 'date': '2025-01-25',
            'status': 'final', 'home_team': 'CHI', 'away_team': 'PHI', 'home_score': 97,
            'away_score': 109, 'provider_game_id': '401705204', 'season_type': 'Regular Season',
            'source': SOURCE, 'source_published_at': '2026-11-03T12:00:00+00:00',
            'source_content_sha256': 'a'*64, 'upstream_full_game_validated': True}
        self.row = {'game_id': self.game['id'], **self.identity, 'team': 'CHI', 'opponent': 'PHI',
            'date': '2025-01-25', 'season': '2024-25', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 34.5, 'pts': 19, 'reb': 7, 'ast': 5,
            'fgm': 7, 'fga': 14, 'tpm': 2, 'tpa': 5, 'ftm': 3, 'fta': 4,
            'oreb': 2, 'dreb': 5, 'stl': 1, 'blk': 0, 'tov': 2, 'fouls': 2,
            'plus_minus': -7, 'starter': 0, 'source': SOURCE,
            'source_published_at': self.game['source_published_at'],
            'source_content_sha256': 'a'*64, 'upstream_full_game_validated': True}
        self.store.upsert_game(self.game)
        self.store.upsert_player_game({**self.row, 'plus_minus': None, 'starter': None,
                                      'source': 'workbook:original.xlsx'})
        self.store.set_checkpoint('nba_schedule_today', {'phase': 'unavailable', 'fresh': False})
        self.store.set_checkpoint('daily_nba:2026-11-04', {'complete': False})
        self.store.set_checkpoint('chat:1', {'keep': 'conversation'})
        self.store.set_checkpoint('dataset_refresh_scope', SCOPE)
        legs = [{'game_id': self.game['id'], 'player': 'Josh Giddey', 'team': 'CHI',
                 'date': self.game['date'], 'stat': stat, 'line': line, 'side': 'more'}
                for stat, line in [('pts', 10.5), ('reb', 4.5)]]
        self.store.create_board(legs); self.store.create_entry(legs, 5, 'power', 2)
        self.requests = []; self.routes = {}; self.publish_repairs(); self.publish_measured()
        self.availability = build_availability_document(normalize_official_pdf(
            [{'game_date': '2026-11-05', 'away_team': 'CHI', 'home_team': 'NYK', 'team': 'CHI',
              'player': 'Josh Giddey', 'status': 'Out', 'reason': 'Injury'}],
            source_url='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-05_12_45PM.pdf',
            report_at='2026-11-05T12:45:00-05:00', text_sha256='b'*64, now=NOW), now=NOW)
        self.routes[sd.AVAILABILITY_URL] = gzip.compress(json.dumps(self.availability).encode())
        self.measured_patch = patch.object(sd, 'refresh_measured_history',
            functools.partial(mh.refresh_measured_history, scope=SCOPE, pair_scope=PAIRS))
        self.measured_patch.start(); self.addCleanup(self.measured_patch.stop)

    def publish_repairs(self, *, extra=None, change=None):
        game2 = {**self.game, 'id': canonical_game('2025-01-26', 'CHI', 'PHI'),
                 'date': '2025-01-26', 'provider_game_id': '401705205'}
        row2 = {**self.row, 'game_id': game2['id'], 'date': game2['date']}
        events = [{'kind': 'game', 'data': self.game}, {'kind': 'player_game', 'data': self.row},
                  {'kind': 'game', 'data': game2}, {'kind': 'player_game', 'data': row2}] + (extra or [])
        part = gzip.compress(('\n'.join(json.dumps(e) for e in events) + '\n').encode())
        self.manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'source_data_through_date': '2026-11-04', 'players': [self.identity],
            'parts': [{'file': 'repair-part-001.jsonl.gz', 'bytes': len(part),
                       'sha256': hashlib.sha256(part).hexdigest(), 'records': len(events)}]}
        if change:
            change(self.manifest)
        self.routes[sd.REPAIR_BASE + sd.HISTORY_MANIFEST] = json.dumps(self.manifest).encode()
        self.routes[sd.REPAIR_BASE + 'repair-part-001.jsonl.gz'] = part
        # The actual repair row already publishes an explicit starter flag.
        roster = gzip.compress(b'')
        self.routes[sd.REPAIR_BASE + sd.STARTER_ROSTER] = roster
        self.metadata = {'schema_version': 1, 'format': 'nba-supplemental-data-v1'}
        for field, name in [('history_manifest', sd.HISTORY_MANIFEST), ('starter_roster', sd.STARTER_ROSTER)]:
            content = self.routes[sd.REPAIR_BASE + name]
            self.metadata[field] = {'file': name, 'bytes': len(content),
                                    'sha256': hashlib.sha256(content).hexdigest()}
        self.routes[sd.REPAIR_BASE + sd.SUPPLEMENTAL_MANIFEST] = json.dumps(self.metadata).encode()

    def publish_measured(self):
        row = observation(); body = gzip.compress((mh._json({'kind': 'measured_pair', 'data': row})+'\n').encode())
        digest = hashlib.sha256(body).hexdigest(); name = f'tracking_pair_history_{digest[:12]}_001.jsonl.gz'
        manifest = {'schema_version': 1, 'format': 'nba-measured-pair-history-jsonl-gzip-v1',
            'scope_revision': SCOPE['revision'], 'pair_scope_sha256': hashlib.sha256(mh._json(PAIRS).encode()).hexdigest(),
            'parts': [{'file': name, 'bytes': len(body), 'sha256': digest, 'records': 1}],
            'measured_pair_games': 1, 'season_aggregate_records': 0,
            'first_observed_game_date': row['date'], 'last_observed_game_date': row['date']}
        self.routes[sd.MEASURED_BASE + sd.MEASURED_MANIFEST] = json.dumps(manifest).encode()
        self.routes[sd.MEASURED_BASE + name] = body

    def publish_crosswalk(self, records):
        content = gzip.compress(('\n'.join(json.dumps(row) for row in records)+'\n').encode())
        self.routes[sd.REPAIR_BASE+sd.SCORE_CROSSWALK] = content
        self.metadata['provider_crosswalk'] = {'file': sd.SCORE_CROSSWALK, 'bytes': len(content),
                                             'sha256': hashlib.sha256(content).hexdigest()}
        self.routes[sd.REPAIR_BASE+sd.SUPPLEMENTAL_MANIFEST] = json.dumps(self.metadata).encode()

    def client(self):
        def handler(request):
            url = str(request.url); self.requests.append(url); value = self.routes.get(url)
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, content=value) if value is not None else httpx.Response(404)
        return httpx.Client(transport=httpx.MockTransport(handler))

    def refresh(self, client, now=NOW):
        return sd._refresh(self.store, self.root, client, now)

    def protected(self):
        with self.store.connect() as db:
            tables = {name: [tuple(row) for row in db.execute('SELECT * FROM '+name)]
                      for name in ('boards', 'entries', 'grade_events')}
        return tables, {key: self.store.get_checkpoint(key) for key in
                        ('nba_schedule_today', 'daily_nba:2026-11-04', 'chat:1')}

    def test_actual_additive_import_null_completion_and_independent_sources(self):
        protected = self.protected()
        with self.client() as client:
            result = self.refresh(client)
            self.assertFalse(client.is_closed)
        self.assertEqual(result['phase'], 'updated'); self.assertTrue(result['changed'])
        self.assertEqual(result['repairs']['history_import']['player_game_inserted'], 1)
        self.assertEqual(result['repairs']['stat_completion']['fields_completed'], 2)
        self.assertEqual(result['measured_history']['added'], 1)
        self.assertEqual(result['availability']['records_added'], 1)
        self.assertEqual(protected, self.protected())
        with self.store.connect() as db:
            old = dict(db.execute('SELECT * FROM player_games WHERE game_id=?', (self.game['id'],)).fetchone())
            saved = json.loads(db.execute('SELECT payload_json FROM structured_availability').fetchone()[0])
        self.assertEqual((old['pts'], old['minutes'], old['plus_minus'], old['starter']), (19, 34.5, -7, 0))
        self.assertEqual(old['source'], 'workbook:original.xlsx')
        self.assertEqual(saved['report_at'], self.availability['records'][0]['report_at'])
        self.assertEqual(self.store.get_checkpoint(sd.CHECKPOINT), result)

    def test_hourly_retry_throttling_and_unchanged_static_assets_are_not_downloaded(self):
        with self.client() as client:
            self.refresh(client); count = len(self.requests)
            held = self.refresh(client, NOW + dt.timedelta(minutes=59))
            self.assertTrue(held['throttled']); self.assertFalse(held['changed'])
            self.assertEqual(len(self.requests), count)
            result = self.refresh(client, NOW + dt.timedelta(hours=1))
        self.assertFalse(result['changed'])
        self.assertEqual(self.requests[count:], [sd.REPAIR_BASE + sd.SUPPLEMENTAL_MANIFEST,
            sd.MEASURED_BASE + sd.MEASURED_MANIFEST, sd.AVAILABILITY_URL])
        self.assertEqual(result['availability']['source_generated_at'], self.availability['generated_at'])
        self.assertEqual(result['availability']['phase'], 'unchanged')

    def test_corrupt_repair_checksum_retains_statistics_but_other_steps_succeed(self):
        self.routes[sd.REPAIR_BASE + 'repair-part-001.jsonl.gz'] = b'bad'
        protected = self.protected()
        with self.client() as client:
            result = self.refresh(client)
        self.assertEqual(result['phase'], 'partial'); self.assertTrue(result['changed'])
        self.assertEqual(result['repairs']['phase'], 'unavailable')
        self.assertEqual(result['measured_history']['added'], 1)
        self.assertEqual(result['availability']['records_added'], 1)
        self.assertEqual(self.protected(), protected)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT plus_minus FROM player_games').fetchone()[0])
        self.assertFalse(list(self.root.rglob('*.download')))

    def test_malformed_late_record_rolls_back_all_repair_rows(self):
        self.publish_repairs(extra=[{'kind': 'unsupported', 'data': {}}])
        with self.client() as client:
            result = self.refresh(client)
        self.assertEqual(result['repairs']['phase'], 'unavailable')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT plus_minus FROM player_games').fetchone()[0])

    def test_each_import_failure_is_isolated_and_backup_failure_keeps_existing_values(self):
        with self.client() as client, patch.object(self.store, 'backup', side_effect=OSError('full')):
            result = self.refresh(client)
        self.assertEqual(result['repairs']['history_import']['phase'], 'unavailable')
        self.assertEqual(result['repairs']['stat_completion']['phase'], 'unavailable')
        self.assertEqual(result['measured_history']['added'], 1)
        self.assertEqual(result['availability']['records_added'], 1)

    def test_fixed_paths_redirects_future_coverage_and_bounds_are_rejected(self):
        for name in ('../secret.json', 'https://untrusted.invalid/secret.json'):
            with self.subTest(name=name):
                value = copy.deepcopy(self.metadata); value['history_manifest']['file'] = name
                with self.assertRaises(ValueError):
                    sd._metadata(json.dumps(value).encode())
        self.routes[sd.REPAIR_BASE + sd.SUPPLEMENTAL_MANIFEST] = httpx.Response(
            302, headers={'Location': 'https://untrusted.invalid/secret.json'})
        with self.client() as client:
            result = self.refresh(client)
        self.assertEqual(result['repairs']['http_status'], 302)
        self.assertTrue(all(url.startswith(sd.BASE + '/') for url in self.requests))
        self.publish_repairs(change=lambda m: m.update(source_data_through_date='2026-11-06'))
        with self.client() as client:
            result = self.refresh(client, NOW + dt.timedelta(hours=1))
        self.assertEqual(result['repairs']['phase'], 'unavailable')
        with self.assertRaises(ValueError):
            sd._metadata(b' '*(sd.MAX_METADATA_BYTES+1))

    def test_live_availability_updates_and_invalid_new_reports_do_not_replace_prior_evidence(self):
        with self.client() as client:
            self.refresh(client)
            self.routes[sd.AVAILABILITY_URL] = gzip.compress(json.dumps({
                **self.availability, 'generated_at': '2026-11-06T18:00:00+00:00'}).encode())
            result = self.refresh(client, NOW + dt.timedelta(hours=1))
        self.assertEqual(result['availability']['phase'], 'unavailable')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM structured_availability').fetchone()[0], 1)
        self.assertFalse(result['changed'])

    def test_metadata_builder_pins_exact_published_bytes(self):
        history = self.root/'history_manifest.json'; history.write_bytes(self.routes[sd.REPAIR_BASE+sd.HISTORY_MANIFEST])
        roster = self.root/sd.STARTER_ROSTER; roster.write_bytes(self.routes[sd.REPAIR_BASE+sd.STARTER_ROSTER])
        output = self.root/sd.SUPPLEMENTAL_MANIFEST
        self.assertEqual(sd.build_supplemental_manifest(history, roster, output)['history_manifest'],
                         self.metadata['history_manifest'])

    def test_active_measured_scope_revision_is_used_and_changed_scope_revalidates(self):
        with self.client() as client:
            result = self.refresh(client)
            self.assertEqual(result['measured_history']['scope_revision'], SCOPE['revision'])
            self.store.set_checkpoint('dataset_refresh_scope', {**SCOPE, 'revision': 'changed-active-scope'})
            result = self.refresh(client, NOW + dt.timedelta(hours=1))
        self.assertEqual(result['measured_history']['phase'], 'unavailable')
        self.assertEqual(len(mh.measured_records(self.store)), 1)
        self.assertIn('scope', result['measured_history']['detail'])

    def test_pruning_retains_two_own_backups_and_other_backups_are_untouched(self):
        root = self.root/'supplemental'; folder = root/'backups'; folder.mkdir(parents=True)
        own = []
        for number in range(4):
            path = folder/f'before-supplemental-history-{number:016x}.sqlite'
            path.write_bytes(b'fixture'); os.utime(path, (number+1, number+1)); own.append(path)
        others = [folder/'before-history-1111111111111111.sqlite', folder/'manual.sqlite',
                  folder/'before-supplemental-history-not-a-hash.sqlite']
        for path in others:
            path.write_bytes(b'preserve')
        sd._prune(root)
        self.assertFalse(own[0].exists()); self.assertFalse(own[1].exists())
        self.assertTrue(own[2].exists()); self.assertTrue(own[3].exists())
        self.assertTrue(all(p.read_bytes() == b'preserve' for p in others))

    def test_real_score_helper_repairs_static_and_cached_rolling_games_with_pinned_crosswalk(self):
        with self.client() as client:
            self.refresh(client)
        game = {**self.game, 'id': canonical_game('2025-01-27', 'CHI', 'PHI'),
                'date': '2025-01-27', 'provider_game_id': '401705206'}
        self.store.upsert_game({**game, 'home_score': None, 'away_score': None,
                                'provider_game_id': '0022400642'})
        with self.store.connect() as db:
            db.execute('UPDATE games SET home_score=NULL WHERE id=?', (self.game['id'],))
        events = [{'kind': 'game', 'data': game}, {'kind': 'player_game', 'data': {
            **self.row, 'game_id': game['id'], 'date': game['date']}}]
        body = gzip.compress(('\n'.join(json.dumps(r) for r in events)+'\n').encode())
        manifest = {**self.manifest, 'parts': [{'file': 'rolling-part-001.jsonl.gz', 'bytes': len(body),
            'sha256': hashlib.sha256(body).hexdigest(), 'records': len(events)}]}
        blob = json.dumps(manifest).encode(); digest = hashlib.sha256(blob).hexdigest()
        folder = self.root/'history'/'rolling'/digest; folder.mkdir(parents=True)
        (folder/sd.HISTORY_MANIFEST).write_bytes(blob); (folder/'rolling-part-001.jsonl.gz').write_bytes(body)
        self.store.set_checkpoint('matchup_career_history_status', {'manifest_sha256': digest})
        self.publish_crosswalk([{**{k: game[k] for k in ('date', 'home_team', 'away_team', 'home_score', 'away_score')},
            'game_id': game['id'], 'provider_game_id': '0022400642',
            'source': 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
                      'nba_stats_player_game_logs/player_game_logs_2025.parquet',
            'source_content_sha256': 'c'*64, 'source_published_at': '2026-11-03T12:00:00+00:00',
            'proof': {'consistent_game_date_and_home_away_headers': True,
                      'both_source_teams_observed': True, 'provider_game_id': '0022400642'}}])
        with self.client() as client:
            result = self.refresh(client, NOW+dt.timedelta(hours=1))
            again = self.refresh(client, NOW+dt.timedelta(hours=2))
        scores = result['repairs']['game_score_completion']
        self.assertEqual(scores['static']['fields_completed'], 1)
        self.assertEqual(scores['rolling']['fields_completed'], 2)
        self.assertEqual(scores['fields_completed'], 3); self.assertTrue(result['changed'])
        self.assertFalse(again['changed']); self.assertIsNotNone(sd.cached_provider_crosswalk(self.root))
        with self.store.connect() as db:
            saved = dict(db.execute('SELECT * FROM games WHERE id=?', (game['id'],)).fetchone())
        self.assertEqual((saved['home_score'], saved['away_score'], saved['provider_game_id']),
                         (97, 109, '0022400642'))

    def test_bad_crosswalk_hash_holds_scores_without_blocking_other_repair_steps(self):
        self.publish_crosswalk([])
        self.routes[sd.REPAIR_BASE+sd.SCORE_CROSSWALK] = b'bad'
        with self.client() as client:
            result = self.refresh(client)
        self.assertEqual(result['repairs']['phase'], 'partial')
        self.assertEqual(result['repairs']['game_score_completion']['phase'], 'unavailable')
        self.assertEqual(result['repairs']['history_import']['player_game_inserted'], 1)
        self.assertEqual(result['repairs']['stat_completion']['fields_completed'], 2)
        self.assertIsNone(sd.cached_provider_crosswalk(self.root))

    def test_only_cached_rolling_source_can_complete_an_old_players_missing_fields(self):
        game = {**self.game, 'id': canonical_game('2025-01-27', 'CHI', 'PHI'),
                'date': '2025-01-27', 'provider_game_id': '401705206'}
        row = {**self.row, 'game_id': game['id'], 'date': game['date']}
        self.store.upsert_game(game)
        self.store.upsert_player_game({**row, 'starter': None, 'plus_minus': None,
                                      'source': 'workbook:old rolling-only row'})
        events = [{'kind':'game','data':game},{'kind':'player_game','data':row}]
        body = gzip.compress(('\n'.join(json.dumps(r) for r in events)+'\n').encode())
        manifest = {**self.manifest, 'parts':[{'file':'rolling-only-001.jsonl.gz', 'bytes':len(body),
            'sha256':hashlib.sha256(body).hexdigest(),'records':len(events)}]}
        blob = json.dumps(manifest).encode(); digest = hashlib.sha256(blob).hexdigest()
        folder = self.root/'history'/'rolling'/digest; folder.mkdir(parents=True)
        (folder/sd.HISTORY_MANIFEST).write_bytes(blob); (folder/'rolling-only-001.jsonl.gz').write_bytes(body)
        self.store.set_checkpoint('matchup_career_history_status', {'manifest_sha256':digest})
        with self.client() as client:
            result = self.refresh(client)
            again = self.refresh(client, NOW+dt.timedelta(hours=1))
        completed = result['repairs']['stat_completion']
        self.assertEqual(completed['static']['fields_completed'], 2)
        self.assertEqual(completed['rolling']['fields_completed'], 2)
        self.assertEqual(completed['fields_completed'], 4)
        self.assertEqual(completed['fields_fillable'], 4)
        self.assertEqual(completed['completed_by_statistic'], {'plus_minus':2, 'starter':2})
        self.assertFalse(again['changed'])
        self.assertFalse(any('/live/career_history/' in url for url in self.requests))
        with self.store.connect() as db:
            saved = dict(db.execute('SELECT * FROM player_games WHERE game_id=?',(game['id'],)).fetchone())
        self.assertEqual((saved['starter'],saved['plus_minus'],saved['minutes'],saved['source']),
                         (0,-7,34.5,'workbook:old rolling-only row'))

    def test_corrupt_cached_rolling_manifest_cannot_block_safe_static_completion(self):
        digest = 'd'*64; folder = self.root/'history'/'rolling'/digest; folder.mkdir(parents=True)
        (folder/sd.HISTORY_MANIFEST).write_text('{}')
        self.store.set_checkpoint('matchup_career_history_status', {'manifest_sha256':digest})
        with self.client() as client:
            result = self.refresh(client)
        completed = result['repairs']['stat_completion']
        self.assertEqual(completed['phase'], 'partial')
        self.assertEqual(completed['static']['fields_completed'], 2)
        self.assertEqual(completed['rolling']['phase'], 'unavailable')
        self.assertEqual(completed['fields_completed'], 2)
        self.assertTrue(result['changed'])


class AsyncSupplementalTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_entry_point_runs_synchronous_network_and_database_off_loop(self):
        store = object(); client = object(); now = NOW
        with patch.object(sd, '_refresh', return_value={'changed': False}) as work:
            result = await sd.refresh_supplemental_data(store, '/tmp/test', client, now)
        self.assertFalse(result['changed']); work.assert_called_once_with(store, '/tmp/test', client, now)


if __name__ == '__main__':
    unittest.main()
