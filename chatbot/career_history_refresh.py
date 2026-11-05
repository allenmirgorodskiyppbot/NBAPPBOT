"""Publish verified career history for current and named matchup participants.

The collector runs with PyArrow in GitHub Actions. The bot consumes only bounded,
checksummed JSONL bundles. A source publication or download never verifies today's
schedule, a complete career, or an actual defensive assignment.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import uuid
from zoneinfo import ZoneInfo

from feeds import canonical_player
from history_backfill import (_validate_bundle_record, current_identities,
                              history_exclusion, normalize_history)

FIRST_END_YEAR = 2004
MAX_SOURCE_BYTES = 8_000_000
MAX_SOURCE_ROWS = 100_000
MAX_PART_UNCOMPRESSED = 8_000_000
ROOT = Path(__file__).resolve().parent


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _aware(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Source publication timestamp needs a timezone.')
    return parsed.astimezone(dt.timezone.utc)


def matchup_targets(scope, pair_scope, extra_targets=()):
    """Resolve explicit names only; a named pair is not proof of guarding."""
    targets = {}
    for item in scope.get('players', []):
        name = str(item.get('player') or '').strip()
        if name:
            targets[canonical_player(name)] = dict(item)
    for pair in pair_scope.get('pairs', []):
        for key in ('offense_player', 'defense_player'):
            name = str(pair.get(key) or '').strip()
            if name:
                targets.setdefault(canonical_player(name), {'player': name})
    for item in extra_targets:
        item = {'player': item} if isinstance(item, str) else dict(item)
        name = str(item.get('player') or '').strip()
        if not name:
            raise ValueError('An explicit matchup target has no player name.')
        identity = canonical_player(item.get('requested_player') or name)
        targets[identity] = {**targets.get(identity, {}), **item}
    return sorted(targets.values(), key=lambda item: canonical_player(item['player']))


def resolve_targets(targets, identity_rows, current=()):
    """Bind exact observed names or explicit provider IDs, never fuzzy namesakes.

    An alias can be explicitly authorized by configuration. Each provider ID has
    one canonical imported player. ``target_resolution`` retains how every named
    matchup participant maps to that person, including unresolved names.
    """
    names = defaultdict(set)
    aliases = defaultdict(set)
    for row in identity_rows:
        if history_exclusion(row):
            continue
        pid = str(row.get('athlete_id') or '')
        name = str(row.get('athlete_display_name') or '').strip()
        if not pid.isdigit() or not name:
            continue
        names[canonical_player(name)].add(pid)
        aliases[pid].add(name)
    current_by_id = {str(item['espn_player_id']): item for item in current}
    current_names = defaultdict(set)
    for pid, item in current_by_id.items():
        for name in [item['player'], *item.get('aliases', [])]:
            current_names[canonical_player(name)].add(pid)
    requested = []
    unresolved = []
    for target in targets:
        name = target.get('requested_player') or target['player']
        explicit = str(target.get('espn_player_id') or '')
        approved_names = {canonical_player(value) for value in [name, target['player'], *target.get('aliases', [])]}
        if explicit:
            if not explicit.isdigit() or not aliases.get(explicit):
                unresolved.append({'player': name, 'reason': 'Configured provider identity has no observed NBA history.'})
                continue
            if not approved_names.intersection(canonical_player(value) for value in aliases[explicit]):
                unresolved.append({'player': name, 'reason': 'Configured provider ID has no approved observed name alias.'})
                continue
            choices = {explicit}
            basis = 'configured_provider_id_and_observed_alias'
        else:
            choices = set().union(*(current_names.get(alias, set()) for alias in approved_names))
            basis = 'current_roster_provider_identity'
            if not choices:
                choices = set().union(*(names.get(alias, set()) for alias in approved_names))
                basis = 'unique_exact_historical_name'
        if len(choices) != 1:
            unresolved.append({'player': name,
                'reason': 'Historical name matches multiple provider identities.' if choices else 'No exact verified NBA identity was found.',
                'candidate_provider_ids': sorted(choices)})
            continue
        requested.append((target, next(iter(choices)), basis))
    configured_by_id = {}
    for target, pid, _ in requested:
        if target.get('espn_player_id') and (pid not in configured_by_id or target.get('requested_player')):
            # Only a successfully validated binding may choose the persistent
            # canonical name. An unresolved bad configuration cannot poison a
            # different, valid target for the same provider ID.
            configured_by_id[pid] = target
    # A current authoritative spelling wins aliases; otherwise retain a supplied
    # canonical target. Do not import the same person twice under name variants.
    selected = {}
    resolutions = []
    for target, pid, basis in requested:
        if pid not in selected:
            canonical = configured_by_id.get(pid) or current_by_id.get(pid, target)
            name = canonical['player']
            selected[pid] = {'player': name, 'player_id': canonical_player(name),
                'espn_player_id': pid, 'aliases': sorted(aliases[pid] | {name})}
        person = selected[pid]
        requested_name = target.get('requested_player') or target['player']
        person['aliases'] = sorted(set(person['aliases']) | {requested_name, target['player'], *target.get('aliases', [])})
        resolutions.append({'requested_player': requested_name, 'player': person['player'],
            'player_id': person['player_id'], 'espn_player_id': pid, 'identity_basis': basis})
    return sorted(selected.values(), key=lambda item: item['player_id']), sorted(resolutions, key=lambda item: item['requested_player']), unresolved


def _parquet_rows(content, columns=None):
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(content), columns=columns)
    if table.num_rows > MAX_SOURCE_ROWS:
        raise ValueError('Historical season publication exceeds its row bound.')
    return table.to_pylist()


def _source_rows(content, year, today, columns=None):
    rows = _parquet_rows(content, columns)
    for row in rows:
        if str(row.get('season')) != str(year):
            raise ValueError('Historical source contains a mismatched season.')
        day = dt.date.fromisoformat(str(row.get('game_date'))[:10])
        if not dt.date(year - 1, 7, 1) <= day <= min(today, dt.date(year, 11, 1)):
            raise ValueError('Historical publication has an out-of-season or future record.')
    return rows


def release_repairs(manifest_path, parts_directory, players, through, existing_game_ids):
    """Retain only independently validated ESPN summary repairs from the release.

    The pinned manifest checksums authenticate the prior validation result. Bulk
    observations are deliberately not used as fallback data for rejected games.
    A newly valid bulk-source game takes precedence over an older repair.
    """
    manifest_path, parts_directory = Path(manifest_path), Path(parts_directory)
    if not manifest_path.exists() or not parts_directory.exists():
        return [], [], {'status': 'not_available_in_collector_checkout'}
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > 4_000_000:
        raise ValueError('Pinned repair manifest exceeds its bound.')
    manifest = json.loads(manifest_bytes)
    if not manifest.get('repair_summary'):
        return [], [], {'status': 'no_independent_repairs_in_release'}
    selected = {item['espn_player_id']: item for item in players}
    grouped = {}
    for part in manifest['parts']:
        name = part['file']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.jsonl\.gz', name):
            raise ValueError('Pinned repair part path is invalid.')
        path = parts_directory / name
        if path.stat().st_size != part['bytes'] or path.stat().st_size > 32_000_000:
            raise ValueError('Pinned repair part size is invalid.')
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != part['sha256']:
            raise ValueError('Pinned repair checksum failed.')
        count = 0
        with gzip.open(io.BytesIO(blob), 'rt', encoding='utf-8') as stream:
            for line in stream:
                count += 1
                if len(line) > 100_000 or count > part['records']:
                    raise ValueError('Pinned repair part exceeds its record bounds.')
                event = json.loads(line)
                row, kind = event.get('data'), event.get('kind')
                if not isinstance(row, dict) or not re.fullmatch(
                        r'https://site\.api\.espn\.com/apis/site/v2/sports/basketball/nba/summary\?event=\d+',
                        str(row.get('source') or '')):
                    continue
                identity = row.get('id') if kind == 'game' else row.get('game_id')
                if identity in existing_game_ids or str(row.get('date') or '') > through:
                    continue
                group = grouped.setdefault(identity, {'game': None, 'rows': []})
                if kind == 'game':
                    _validate_bundle_record(kind, row, selected, through)
                    group['game'] = row
                elif kind == 'player_game':
                    person = selected.get(str(row.get('espn_player_id') or ''))
                    if not person:
                        continue
                    if canonical_player(row.get('player')) not in {canonical_player(value) for value in person['aliases']}:
                        raise ValueError('Pinned repair player alias is not approved.')
                    if row.get('upstream_full_game_validated') is not True:
                        raise ValueError('Pinned repair lacks prior full-game validation.')
                    row = {**row, 'player': person['player'], 'player_id': person['player_id']}
                    _validate_bundle_record(kind, row, selected, through)
                    group['rows'].append(row)
        if count != part['records']:
            raise ValueError('Pinned repair record count does not match its manifest.')
    games, observations = [], []
    for identity, group in sorted(grouped.items()):
        if not group['rows']:
            continue
        if group['game'] is None:
            raise ValueError('Pinned repair observations have no validated game.')
        if any({row['team'], row['opponent']} != {group['game']['home_team'], group['game']['away_team']} for row in group['rows']):
            raise ValueError('Pinned repair participants conflict.')
        games.append(group['game'])
        observations.extend(group['rows'])
    return games, observations, {'status': 'inherited_verified_release_repairs',
        'release_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'games': len(games), 'observations': len(observations),
        'source': 'Previously independently validated ESPN event summaries, retained from the checksummed release.'}


def _write_bundle(directory, assets, targets, today, *, row_reader=_source_rows,
                  repair_manifest_path=None, repair_parts_directory=None):
    """Build all parts in staging before atomically replacing the manifest."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / ('.building-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        # Read only identity columns on the first pass to keep memory bounded.
        identity_columns = ['athlete_id', 'athlete_display_name', 'team_abbreviation',
                            'opponent_team_abbreviation', 'season_type', 'season', 'game_date']
        identity_rows = []
        current_rows = None
        for asset in assets:
            rows = row_reader(asset['content'], asset['year'], today, identity_columns)
            identity_rows.extend(rows)
            if asset['year'] == max(item['year'] for item in assets):
                current_rows = rows
        current = current_identities(current_rows or [], {'players': targets})
        players, resolutions, unresolved = resolve_targets(targets, identity_rows, current)
        if not players:
            raise ValueError('No requested matchup participant had a verified NBA identity.')
        through = max(str(row['game_date'])[:10] for row in identity_rows)
        del identity_rows
        parts = []
        events = []
        event_bytes = 0
        reports = []
        coverage = defaultdict(lambda: {'observations': 0, 'played_games': 0, 'seasons': set(), 'first_date': None, 'last_date': None})
        game_ids = set()
        def flush():
            nonlocal events, event_bytes
            if not events:
                return
            blob = gzip.compress(('\n'.join(events) + '\n').encode(), compresslevel=9, mtime=0)
            name = f'career-{len(parts) + 1:03}.jsonl.gz'
            (staging / name).write_bytes(blob)
            parts.append({'file': name, 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(), 'records': len(events)})
            events, event_bytes = [], 0
        def append(kind, row):
            nonlocal event_bytes
            line = _json({'kind': kind, 'data': row})
            events.append(line)
            event_bytes += len(line.encode()) + 1
            if event_bytes >= MAX_PART_UNCOMPRESSED:
                flush()
        def observe(row):
            entry = coverage[row['player_id']]
            entry['observations'] += 1
            entry['played_games'] += row['appearance_state'] == 'Played'
            entry['seasons'].add(row['season'])
            entry['first_date'] = min(entry['first_date'] or row['date'], row['date'])
            entry['last_date'] = max(entry['last_date'] or row['date'], row['date'])
        for asset in assets:
            rows = row_reader(asset['content'], asset['year'], today)
            games, observed, report = normalize_history(rows, players, asset['url'], asset['published_at'], asset['sha256'], through)
            for game in games:
                append('game', {**game, 'upstream_full_game_validated': True})
                game_ids.add(game['id'])
            for row in observed:
                append('player_game', row)
                observe(row)
            reports.append({'end_year': asset['year'], 'source': asset['url'],
                'source_published_at': asset['published_at'], 'source_content_sha256': asset['sha256'], **report})
        repair_report = {'status': 'not_configured'}
        if repair_manifest_path and repair_parts_directory:
            repaired_games, repaired_rows, repair_report = release_repairs(
                repair_manifest_path, repair_parts_directory, players, through, game_ids)
            for game in repaired_games:
                append('game', game)
            for row in repaired_rows:
                append('player_game', row)
                observe(row)
        flush()
        coverage_rows = []
        for resolution in resolutions:
            item = coverage[resolution['player_id']]
            coverage_rows.append({**resolution, **item, 'seasons': sorted(item['seasons']),
                'status': 'available_history' if item['observations'] else 'no_published_history',
                'completeness': 'Available validated source observations; complete career inventory is not independently verified.'})
        for item in unresolved:
            coverage_rows.append({'requested_player': item['player'], 'player': item['player'],
                'status': 'unresolved_identity', 'observations': 0, 'played_games': 0,
                'seasons': [], 'first_date': None, 'last_date': None, 'reason': item['reason']})
        manifest = {'schema_version': 1, 'format': 'nba-history-jsonl-gzip-v1',
            'identity_scope': 'current_players_and_named_matchup_participants',
            'population': 'Current authorized players and explicit matchup participants',
            'first_allowed_season': '2003-04', 'source_data_through_date': through,
            'players': players, 'target_resolution': resolutions, 'unresolved_targets': unresolved,
            'coverage': coverage_rows, 'parts': parts, 'sources': reports,
            'inherited_release_repairs': repair_report,
            'limits': ['Available published career observations, not independently reconciled complete careers.',
                       'Whole-game opponent history is not measured defensive guarding.',
                       'Source publication, covered-game date and collection time are separate.',
                       'Missing statistics and DNPs are never substituted with played zeros.']}
        content = (_json(manifest) + '\n').encode()
        if len(content) > 4_000_000:
            raise ValueError('Career coverage manifest exceeds its bound.')
        old_path = directory / 'history_manifest.json'
        unchanged = old_path.exists() and old_path.read_bytes() == content
        # Parts are written before the manifest. Readers require its hashes, so
        # no partially written generation can be mistaken for complete history.
        for part in parts:
            incoming, destination = staging / part['file'], directory / part['file']
            if not destination.exists() or destination.read_bytes() != incoming.read_bytes():
                incoming.replace(destination)
        temporary = directory / 'history_manifest.json.tmp'
        temporary.write_bytes(content)
        temporary.replace(old_path)
        return {'status': 'unchanged' if unchanged else 'updated',
            'manifest_sha256': hashlib.sha256(content).hexdigest(), 'source_data_through_date': through,
            'players': len(players), 'requested_participants': len(targets),
            'unresolved_targets': unresolved, 'observations': sum(item['observations'] for item in coverage.values()),
            'rejected_games': sum(len(item['rejected_games']) for item in reports), 'parts': len(parts)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def refresh_career_history(client, output, scope, *, now=None, pair_scope=None,
                                 extra_targets=None, asset_loader=None, seasons=None,
                                 repair_manifest_path=None, repair_parts_directory=None):
    """Refresh the trusted career relay; preserve prior published history on failure."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Career collection time needs a timezone.')
    now = now.astimezone(dt.timezone.utc)
    today = now.astimezone(ZoneInfo('America/New_York')).date()
    end_year = today.year + (today.month >= 7)
    seasons = list(seasons) if seasons is not None else list(range(FIRST_END_YEAR, end_year + 1))
    if not seasons or len(seasons) > 40 or len(set(seasons)) != len(seasons) or any(type(year) is not int or not FIRST_END_YEAR <= year <= end_year for year in seasons):
        raise ValueError('Career collection season scope is invalid.')
    if pair_scope is None:
        pair_scope = json.loads((ROOT / 'matchup_refresh_scope.json').read_text())
    if extra_targets is None:
        path = ROOT / 'matchup_history_targets.json'
        extra_targets = json.loads(path.read_text()).get('players', []) if path.exists() else []
    targets = matchup_targets(scope, pair_scope, extra_targets)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if asset_loader is None:
        from mirror_sources import CSV_TAG, PublishedNBAData
        publisher = PublishedNBAData(client, now)
        async def asset_loader(year):
            return await publisher.asset(CSV_TAG, f'player_box_{year}.parquet')
    semaphore = asyncio.Semaphore(3)
    source_failed = asyncio.Event()
    async def load(year):
        async with semaphore:
            if source_failed.is_set():
                raise ValueError('An earlier career source request failed; remaining requests were stopped.')
            try:
                content, meta = await asset_loader(year)
            except Exception:
                source_failed.set()
                raise
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_SOURCE_BYTES:
            raise ValueError('Historical source publication exceeds its size bound.')
        published = _aware(meta.get('updated_at'))
        if published > now + dt.timedelta(minutes=5):
            raise ValueError('Historical source publication timestamp is in the future.')
        return {'year': year, 'content': content, 'url': meta['browser_download_url'],
                'published_at': published.isoformat(), 'sha256': hashlib.sha256(content).hexdigest()}
    status_path = output / 'career_history_status.json'
    try:
        tasks = [asyncio.create_task(load(year)) for year in sorted(seasons)]
        try:
            assets = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        result = await asyncio.to_thread(_write_bundle, output / 'career_history', assets, targets, today,
            repair_manifest_path=repair_manifest_path or ROOT / 'history_manifest.json',
            repair_parts_directory=repair_parts_directory or ROOT.parent / 'data' / 'history')
    except (ValueError, OSError, KeyError, TypeError) as error:
        result = {'status': 'unavailable', 'error_type': type(error).__name__,
            'note': 'Prior career bundle preserved; this collection did not establish new career coverage.'}
    except Exception as error:
        # HTTP transport/status errors are expected external availability states.
        import httpx
        if not isinstance(error, httpx.HTTPError):
            raise
        result = {'status': 'unavailable', 'error_type': type(error).__name__,
            'http_status': getattr(getattr(error, 'response', None), 'status_code', None),
            'note': 'Prior career bundle preserved; source access did not succeed.'}
    result = {**result, 'attempted_at': now.isoformat(),
        'scope': 'Career-history collection only; not current-game readiness.'}
    temporary = status_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    temporary.replace(status_path)
    return result
