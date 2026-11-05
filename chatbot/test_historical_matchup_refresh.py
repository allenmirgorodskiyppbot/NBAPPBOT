"""Historical relay integrity, actual phases, cache lineage and safe failures."""
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import historical_matchup_refresh as history

NOW = dt.datetime(2026, 11, 5, 15, tzinfo=dt.timezone.utc)
SCOPE = {'revision': 'test-revision', 'players': [
    {'player': 'Jalen Brunson', 'nba_player_id': '1628973'},
    {'player': 'Tyrese Maxey', 'nba_player_id': '1630178'}]}
PAIRS = {'pairs': [{'offense_player': 'Jalen Brunson', 'defense_player': 'Tyrese Maxey'}],
         'team_ids': {'1610612752': 'NYK', '1610612755': 'PHI'}}


def fixture():
    games, rows = [], []
    for game, phase, day in [('0022600001', '2', '2026-10-20'),
                              ('0042600001', '4', '2026-10-21')]:
        games.append({'game_id': game, 'season': 2027,
            'season_type': 'regular-season' if phase == '2' else 'playoffs',
            'game_date': day, 'home_team_abbreviation': 'NYK', 'away_team_abbreviation': 'PHI',
            'home_pts': 110, 'away_pts': 100, 'home_wl': 'W', 'away_wl': 'L'})
        rows.append({'game_id': game, 'season': 2027, 'season_type_id': phase,
            'off_person_id': 1628973, 'off_first_name': 'Jalen', 'off_family_name': 'Brunson',
            'def_person_id': 1630178, 'def_first_name': 'Tyrese', 'def_family_name': 'Maxey',
            'off_team_id': 1610612752, 'off_team_tricode': 'NYK', 'def_team_id': 1610612755,
            'matchup_minutes': '1:30', 'partial_possessions': 5.5,
            'matchup_field_goals_made': 1, 'matchup_field_goals_attempted': 2})
    return rows, games


class Mirror:
    now = NOW
    end_year = 2027
    scope = SCOPE

    def __init__(self, failure=False):
        rows, games = fixture()
        self.bodies = {'game_matchups_2027.parquet': history._json(rows),
                       'nba_schedule_2027.parquet': history._json(games)}
        self.calls, self.releases, self.failure = [], [], failure

    async def release(self, tag):
        self.releases.append(tag)
        if self.failure:
            raise ConnectionError('refused')
        name = ('game_matchups_2027.parquet' if tag == history.MATCHUP_TAG
                else 'nba_schedule_2027.parquet')
        body = self.bodies[name]
        return {name: {'browser_download_url': history._source_url(tag, name),
                       'size': len(body), 'updated_at': '2026-11-04T15:00:00Z',
                       'digest': 'sha256:' + hashlib.sha256(body).hexdigest()}}

    async def asset(self, tag, name):
        self.calls.append(name)
        return self.bodies[name], {'browser_download_url': history._source_url(tag, name),
                                  'updated_at': '2026-11-04T15:00:00Z'}


def json_rows(content, scope=None):
    rows = json.loads(content)
    return rows, len(rows)


class HistoricalRelayTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, mirror, output, **kwargs):
        with patch.object(history, 'FIRST_END_YEAR', 2027), \
             patch.object(history, '_configuration', return_value=(PAIRS, [])), \
             patch.object(history, '_rows', side_effect=json_rows):
            return await history.collect_historical_matchups(mirror, output, **kwargs)

    async def test_dated_actual_games_and_playoffs_have_separate_GP(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'live'
            result = await self.collect(Mirror(), output)
            self.assertEqual(result['phase'], 'updated')
            manifest = json.loads((output / history.MANIFEST).read_text())
            contents = []
            for part in manifest['parts']:
                body = (output / part['file']).read_bytes()
                self.assertEqual(hashlib.sha256(body).hexdigest(), part['sha256'])
                contents.extend(json.loads(line) for line in gzip.decompress(body).splitlines())
            measured = [r['data'] for r in contents if r['kind'] == 'measured_pair']
            aggregates = [r['data'] for r in contents if r['kind'] == 'season_aggregate']
            self.assertEqual({r['pair_observation_id'] for r in measured},
                             {'0022600001_1628973_1630178', '0042600001_1628973_1630178'})
            self.assertEqual({(r['season_type'], r['gp']) for r in aggregates},
                             {('Regular Season', 1), ('Playoffs', 1)})
            self.assertEqual(manifest['last_observed_game_date'], '2026-10-21')
            self.assertNotEqual(manifest['last_observed_game_date'], '2026-11-04')
            self.assertTrue((output.parent / '.nba-tracking-cache').exists())
            self.assertFalse((output / '.nba-tracking-cache').exists())

    async def test_unchanged_source_reuses_verified_cache_and_original_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            output, first = Path(directory) / 'live', Mirror()
            await self.collect(first, output)
            before = (output / history.MANIFEST).read_bytes()
            second = Mirror()
            result = await self.collect(second, output, now=NOW + dt.timedelta(hours=1))
            self.assertEqual(result['phase'], 'updated')
            self.assertEqual(second.calls, [])
            self.assertEqual((output / history.MANIFEST).read_bytes(), before)

    async def test_nested_publication_uses_explicit_private_cache_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'live' / 'historical_matchups'
            cache = root / '.nba-tracking-cache'
            result = await self.collect(Mirror(), output, cache_directory=cache)
            self.assertEqual(result['phase'], 'updated')
            self.assertTrue(cache.exists())
            self.assertFalse((root / 'live' / '.nba-tracking-cache').exists())
            self.assertTrue((output / history.MANIFEST).is_file())

    async def test_cache_loss_preserves_same_hash_retrieval_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'live'
            await self.collect(Mirror(), output)
            before = (output / history.MANIFEST).read_bytes()
            for path in (output.parent / '.nba-tracking-cache').iterdir():
                path.unlink()
            newer = Mirror()
            await self.collect(newer, output, now=NOW + dt.timedelta(hours=2))
            self.assertEqual(len(newer.calls), 2)
            self.assertEqual((output / history.MANIFEST).read_bytes(), before)

    async def test_failed_source_stops_pass_and_preserves_entire_old_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'live'
            await self.collect(Mirror(), output)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            broken = Mirror(failure=True)
            result = await self.collect(broken, output)
            self.assertEqual(result['phase'], 'unavailable')
            self.assertTrue(result['previous_publication_retained'])
            self.assertEqual(len(broken.releases), 1)
            self.assertEqual(broken.calls, [])
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)

    async def test_corrupt_source_digest_is_held_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Mirror()
            original = mirror.asset
            async def corrupt(tag, name):
                body, meta = await original(tag, name)
                return body + b'x', meta
            mirror.asset = corrupt
            output = Path(directory) / 'live'
            result = await self.collect(mirror, output)
            self.assertEqual(result['phase'], 'unavailable')
            self.assertFalse((output / history.MANIFEST).exists())
            self.assertEqual(len(mirror.calls), 1)

    async def test_tampered_cache_is_downloaded_and_reverified(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'live'
            await self.collect(Mirror(), output)
            cache = output.parent / '.nba-tracking-cache' / 'game_matchups_2027.parquet'
            cache.write_bytes(b'corrupted')
            mirror = Mirror()
            result = await self.collect(mirror, output)
            self.assertEqual(result['phase'], 'updated')
            self.assertEqual(mirror.calls, ['game_matchups_2027.parquet'])
            later = Mirror()
            await self.collect(later, output)
            self.assertEqual(later.calls, [])

    async def test_supplied_current_bytes_avoid_second_download(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Mirror()
            current = {'source_content': mirror.bodies['game_matchups_2027.parquet'],
                'source_meta': {'browser_download_url': history._source_url(history.MATCHUP_TAG, 'game_matchups_2027.parquet')},
                'schedule_content': mirror.bodies['nba_schedule_2027.parquet'],
                'schedule_meta': {'browser_download_url': history._source_url(history.SCHEDULE_TAG, 'nba_schedule_2027.parquet')}}
            result = await self.collect(mirror, Path(directory) / 'live', current_data=current)
            self.assertEqual(result['phase'], 'updated')
            self.assertEqual(mirror.calls, [])

    async def test_missing_authorized_player_is_explicitly_held(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Mirror()
            mirror.scope = {**SCOPE, 'players': SCOPE['players'][:1]}
            result = await self.collect(mirror, Path(directory) / 'live')
            self.assertEqual(result['phase'], 'unavailable')
            self.assertEqual(result['failed_asset'], 'historical_configuration')
            self.assertEqual(mirror.releases, [])


class HistoricalPartTests(unittest.TestCase):
    def test_content_addressed_parts_are_deterministic_and_bounded(self):
        items = [{'kind': 'measured_pair', 'data': {'pair_observation_id': str(i), 'note': 'x' * 500}}
                 for i in range(30)]
        first, second = list(history._part_contents(items)), list(history._part_contents(items))
        self.assertEqual(first, second)
        self.assertEqual(sum(count for _, count in first), 30)
        self.assertTrue(all(len(content) <= history.MAX_PART_BYTES for content, _ in first))

    def test_oversized_single_record_is_rejected(self):
        with self.assertRaises(ValueError):
            list(history._part_contents([{'kind': 'measured_pair', 'data': {'note': 'x' * 100_001}}]))


if __name__ == '__main__':
    unittest.main()
