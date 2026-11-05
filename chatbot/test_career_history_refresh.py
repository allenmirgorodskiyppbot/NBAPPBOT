"""Career refresh retains identity, source validation and deterministic coverage."""
import copy
import datetime as dt
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import career_history_refresh as refresh
from feeds import canonical_player
from history_backfill import import_history_bundle, normalize_history
from master import MasterStore
from test_history_backfill import source_rows

NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)


def rows(year=2027, day='2026-11-02'):
    return [{**row, 'season': year} for row in source_rows(day=day)]


def asset(year, data):
    blob = json.dumps(data).encode()
    return {'year': year, 'content': blob,
            'url': f'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_{year}.parquet',
            'published_at': '2026-11-03T12:00:00+00:00',
            'sha256': refresh.hashlib.sha256(blob).hexdigest()}


def reader(content, year, today, columns=None):
    return json.loads(content)


class CareerIdentityTests(unittest.TestCase):
    def test_targets_include_every_pair_side_scope_and_extra_assignment(self):
        scope = {'players': [{'player': 'Our Player', 'espn_player_id': '1'}]}
        pairs = {'pairs': [{'offense_player': 'Other Player', 'defense_player': 'Our Player'}]}
        result = refresh.matchup_targets(scope, pairs, ['Assignment Alternate'])
        self.assertEqual({item['player'] for item in result}, {'Our Player', 'Other Player', 'Assignment Alternate'})
        self.assertEqual(next(item for item in result if item['player'] == 'Our Player')['espn_player_id'], '1')

    def test_unique_historical_opponent_missing_current_roster_is_selected(self):
        current = rows()
        old = [{**row, 'athlete_display_name': 'Absent Veteran' if row['athlete_id'] == 1 else row['athlete_display_name']}
               for row in rows(2026, '2025-11-02')]
        selected, resolutions, unresolved = refresh.resolve_targets([{'player': 'Absent Veteran'}], [*current, *old])
        self.assertEqual(selected[0]['espn_player_id'], '1')
        self.assertEqual(resolutions[0]['identity_basis'], 'unique_exact_historical_name')
        self.assertEqual(unresolved, [])

    def test_historical_namesake_is_held_without_current_authoritative_identity(self):
        data = rows()
        data.append({**data[0], 'athlete_id': 999})
        selected, _, unresolved = refresh.resolve_targets([{'player': 'Player 1'}], data)
        self.assertEqual(selected, [])
        self.assertEqual(unresolved[0]['candidate_provider_ids'], ['1', '999'])

    def test_current_provider_identity_prevents_retired_namesake_merge(self):
        data = rows()
        data.append({**data[0], 'athlete_id': 999})
        current = [{'player': 'Player 1', 'espn_player_id': '1', 'aliases': ['Player 1']}]
        selected, _, unresolved = refresh.resolve_targets([{'player': 'Player 1'}], data, current)
        self.assertEqual([item['espn_player_id'] for item in selected], ['1'])
        self.assertFalse(unresolved)

    def test_explicit_alias_maps_one_provider_to_one_canonical_person(self):
        data = [{**row, 'athlete_display_name': 'Bobby Portis' if row['athlete_id'] == 1 else row['athlete_display_name']}
                for row in rows()]
        targets = [{'player': 'Bobby Portis'}, {'player': 'Bobby Portis Jr.', 'espn_player_id': '1', 'aliases': ['Bobby Portis']}]
        current = [{'player': 'Bobby Portis', 'espn_player_id': '1', 'aliases': ['Bobby Portis']}]
        selected, resolutions, unresolved = refresh.resolve_targets(targets, data, current)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(resolutions), 2)
        self.assertIn('Bobby Portis Jr.', selected[0]['aliases'])
        self.assertFalse(unresolved)

    def test_explicit_wrong_name_or_unknown_id_cannot_borrow_a_career(self):
        selected, _, unresolved = refresh.resolve_targets([
            {'player': 'Somebody Else', 'espn_player_id': '1'},
            {'player': 'Player 1', 'espn_player_id': '999'}], rows())
        self.assertFalse(selected)
        self.assertEqual(len(unresolved), 2)

    def test_invalid_configured_target_cannot_poison_another_valid_player(self):
        selected, resolutions, unresolved = refresh.resolve_targets([
            {'player': 'Somebody Else', 'espn_player_id': '1'}, {'player': 'Player 1'}], rows())
        self.assertEqual(selected[0]['player'], 'Player 1')
        self.assertEqual(resolutions[0]['player_id'], canonical_player('Player 1'))
        self.assertEqual(len(unresolved), 1)

    def test_verified_canonical_identity_survives_a_future_display_name_change(self):
        data = [{**row, 'athlete_display_name': 'Jimmy Butler' if row['athlete_id'] == 1 else row['athlete_display_name']}
                for row in rows()]
        targets = [{'requested_player': 'Jimmy Butler III', 'player': 'Jimmy Butler III',
                    'espn_player_id': '1', 'aliases': ['Jimmy Butler', 'Jimmy Butler III']}]
        current = [{'player': 'Jimmy Butler', 'espn_player_id': '1', 'aliases': ['Jimmy Butler']}]
        selected, resolutions, unresolved = refresh.resolve_targets(targets, data, current)
        self.assertFalse(unresolved)
        self.assertEqual(selected[0]['player_id'], canonical_player('Jimmy Butler III'))
        self.assertEqual(resolutions[0]['player'], 'Jimmy Butler III')


class CareerBundleTests(unittest.TestCase):
    def repair_bundle(self, root, *, source='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=123'):
        person = {'player': 'Player 1', 'player_id': canonical_player('Player 1'), 'espn_player_id': '1', 'aliases': ['Player 1']}
        games, observed, _ = normalize_history(rows(), [person], source, None, 'b' * 64, '2026-11-02')
        events = [{'kind': 'game', 'data': games[0]}, {'kind': 'player_game', 'data': observed[0]}]
        blob = gzip.compress(('\n'.join(json.dumps(item) for item in events) + '\n').encode(), mtime=0)
        part = root / 'repair.jsonl.gz'; part.write_bytes(blob)
        manifest = {'repair_summary': {'independently_validated_games': 1}, 'parts': [
            {'file': part.name, 'bytes': len(blob), 'sha256': refresh.hashlib.sha256(blob).hexdigest(), 'records': len(events)}]}
        path = root / 'manifest.json'; path.write_text(json.dumps(manifest))
        return path, person, games[0]['id']

    def test_only_verified_release_summary_repairs_fill_missing_games(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path, person, gid = self.repair_bundle(root)
            games, observed, report = refresh.release_repairs(path, root, [person], '2026-11-02', set())
            self.assertEqual((len(games), len(observed)), (1, 1))
            self.assertEqual(report['status'], 'inherited_verified_release_repairs')
            self.assertEqual(refresh.release_repairs(path, root, [person], '2026-11-02', {gid})[:2], ([], []))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path, person, _ = self.repair_bundle(root, source='https://github.com/public/bulk-source')
            self.assertEqual(refresh.release_repairs(path, root, [person], '2026-11-02', set())[:2], ([], []))

    def test_corrupt_release_repair_does_not_become_fallback_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path, person, _ = self.repair_bundle(root)
            part = root / 'repair.jsonl.gz'; blob = bytearray(part.read_bytes()); blob[-1] ^= 1; part.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                refresh.release_repairs(path, root, [person], '2026-11-02', set())

    def test_bundle_is_deterministic_and_roundtrips_with_phase_separation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assets = [asset(2026, rows(2026, '2025-11-02')), asset(2027, rows())]
            for row in json.loads(assets[0]['content']):
                row['season_type'] = 3
            # Use explicit playoff input, retaining current regular observations.
            old = rows(2026, '2025-11-02')
            for item in old:
                item['season_type'] = 3
            assets[0] = asset(2026, old)
            target = [{'player': 'Player 1'}]
            one = refresh._write_bundle(root, assets, target, NOW.date(), row_reader=reader)
            first = {path.name: path.read_bytes() for path in root.glob('*') if path.is_file()}
            two = refresh._write_bundle(root, assets, target, NOW.date(), row_reader=reader)
            self.assertEqual(one['status'], 'updated')
            self.assertEqual(two['status'], 'unchanged')
            self.assertEqual(first, {path.name: path.read_bytes() for path in root.glob('*') if path.is_file()})
            store = MasterStore(root / 'db.sqlite')
            result = import_history_bundle(store, root / 'history_manifest.json')
            self.assertEqual(result['player_game_inserted'], 2)
            with store.connect() as db:
                self.assertEqual({row[0] for row in db.execute('SELECT season_type FROM player_games')}, {'Regular Season', 'Playoffs'})

    def test_every_requested_name_has_coverage_including_unresolved_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            refresh._write_bundle(folder, [asset(2027, rows())], [{'player': 'Player 1'}, {'player': 'Unknown Person'}], NOW.date(), row_reader=reader)
            document = json.loads((Path(folder) / 'history_manifest.json').read_text())
            coverage = {item['requested_player']: item for item in document['coverage']}
            self.assertEqual(set(coverage), {'Player 1', 'Unknown Person'})
            self.assertEqual(coverage['Unknown Person']['status'], 'unresolved_identity')
            self.assertEqual(document['identity_scope'], 'current_players_and_named_matchup_participants')
            self.assertIn('complete career', coverage['Player 1']['completeness'])

    def test_bad_complete_game_is_excluded_and_explicitly_reported(self):
        bad = rows(); bad[0]['points'] = 99
        with tempfile.TemporaryDirectory() as folder:
            refresh._write_bundle(folder, [asset(2027, bad)], [{'player': 'Player 1'}], NOW.date(), row_reader=reader)
            doc = json.loads((Path(folder) / 'history_manifest.json').read_text())
            self.assertEqual(doc['coverage'][0]['status'], 'no_published_history')
            self.assertEqual(len(doc['sources'][0]['rejected_games']), 1)
            self.assertFalse(doc['parts'])

    def test_failed_generation_preserves_prior_manifest_and_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            refresh._write_bundle(root, [asset(2027, rows())], [{'player': 'Player 1'}], NOW.date(), row_reader=reader)
            before = {p.name: p.read_bytes() for p in root.glob('*')}
            with self.assertRaises(ValueError):
                refresh._write_bundle(root, [asset(2027, rows())], [{'player': 'Nobody'}], NOW.date(), row_reader=reader)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.glob('*')})


class CareerRefreshTests(unittest.IsolatedAsyncioTestCase):
    def loader(self, data):
        async def load(year):
            return data, {'updated_at': '2026-11-03T12:00:00+00:00',
                'browser_download_url': f'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_{year}.parquet'}
        return load

    async def test_source_refusal_preserves_bundle_and_records_unavailable_health(self):
        async def refused(year):
            raise httpx.HTTPStatusError('refused', request=httpx.Request('GET', 'https://github.com/source'), response=httpx.Response(403))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'career_history').mkdir()
            old = root / 'career_history/history_manifest.json'; old.write_bytes(b'old trusted bundle')
            result = await refresh.refresh_career_history(None, root, {'players': [{'player': 'Player 1'}]},
                now=NOW, pair_scope={}, extra_targets=[], asset_loader=refused, seasons=[2027])
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['http_status'], 403)
            self.assertEqual(old.read_bytes(), b'old trusted bundle')
            status = json.loads((root / 'career_history_status.json').read_text())
            self.assertEqual(status['attempted_at'], NOW.isoformat())
            self.assertNotIn('source_data_through_date', status)

    async def test_collection_routes_source_data_through_date_separately_from_attempt(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(refresh, '_parquet_rows', side_effect=lambda content, columns=None: json.loads(content)):
            result = await refresh.refresh_career_history(None, folder, {'players': [{'player': 'Player 1'}]},
                now=NOW, pair_scope={}, extra_targets=[], asset_loader=self.loader(json.dumps(rows()).encode()), seasons=[2027])
            self.assertEqual(result['source_data_through_date'], '2026-11-02')
            self.assertEqual(result['attempted_at'], '2026-11-05T18:00:00+00:00')
            self.assertEqual(result['observations'], 1)

    async def test_first_source_refusal_stops_queued_requests(self):
        calls = []
        async def refused(year):
            calls.append(year)
            raise httpx.HTTPStatusError('refused', request=httpx.Request('GET', 'https://github.com/source'), response=httpx.Response(403))
        with tempfile.TemporaryDirectory() as folder:
            result = await refresh.refresh_career_history(None, folder, {'players': [{'player': 'Player 1'}]},
                now=NOW, pair_scope={}, extra_targets=[], asset_loader=refused, seasons=[2024, 2025, 2026, 2027])
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['http_status'], 403)
        self.assertEqual(calls, [2024])

    async def test_future_source_publication_and_oversized_source_fail_closed(self):
        async def future(year):
            return b'x', {'updated_at': '2026-11-06T00:00:00Z', 'browser_download_url': 'https://github.com/source'}
        with tempfile.TemporaryDirectory() as folder:
            result = await refresh.refresh_career_history(None, folder, {'players': [{'player': 'Player 1'}]},
                now=NOW, pair_scope={}, extra_targets=[], asset_loader=future, seasons=[2027])
            self.assertEqual(result['status'], 'unavailable')
        with self.assertRaises(ValueError):
            await refresh.refresh_career_history(None, '/unused', {}, now=NOW, seasons=[2003])

    def test_source_parser_rejects_wrong_year_and_future_game(self):
        for data in ([{**rows()[0], 'season': 2026}], [{**rows()[0], 'game_date': '2026-11-06'}]):
            with patch.object(refresh, '_parquet_rows', return_value=data), self.assertRaises(ValueError):
                refresh._source_rows(b'fixture', 2027, NOW.date())


if __name__ == '__main__':
    unittest.main()
