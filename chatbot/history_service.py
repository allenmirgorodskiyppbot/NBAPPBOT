"""Install verified NBA career history without replacing current live records."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from zoneinfo import ZoneInfo

import httpx

from history_backfill import import_history_bundle

REPOSITORY = 'allenmirgorodskiyppbot/NBAPPBOT'
MANIFEST = Path(__file__).with_name('history_manifest.json')
ROLLING_BASE = f'https://raw.githubusercontent.com/{REPOSITORY}/main/data/live/career_history'
ROLLING_MANIFEST_URL = ROLLING_BASE + '/history_manifest.json'
MAX_MANIFEST_BYTES = 4_000_000
MAX_PART_BYTES = 32_000_000
POLL_SECONDS = 3600
RETRY_SECONDS = 3600
EASTERN = ZoneInfo('America/New_York')


def _now(value=None):
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        raise ValueError('Career history checks require a timezone-aware clock.')
    return value.astimezone(dt.timezone.utc)


def _manifest(blob, now=None):
    if not blob or len(blob) > MAX_MANIFEST_BYTES:
        raise ValueError('History manifest is too large or empty.')
    document = json.loads(blob)
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or document.get('format') != 'nba-history-jsonl-gzip-v1'
            or not isinstance(document.get('parts'), list)):
        raise ValueError('Invalid history manifest.')
    if not 1 <= len(document['parts']) <= 100:
        raise ValueError('Invalid history bundle part count.')
    try:
        day = dt.date.fromisoformat(document['source_data_through_date'])
        if day.isoformat() != document['source_data_through_date']:
            raise ValueError()
        if now is not None and day > _now(now).astimezone(EASTERN).date():
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError('History manifest coverage date is invalid or in the future.') from None
    names = set()
    for part in document['parts']:
        if not isinstance(part, dict):
            raise ValueError('Invalid history part metadata.')
        name, size, expected = part.get('file'), part.get('bytes'), part.get('sha256')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.jsonl\.gz', name)
                or name in names or type(size) is not int or not 0 < size <= MAX_PART_BYTES
                or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
                or type(part.get('records')) is not int or not 0 < part['records'] <= 2_000_000):
            raise ValueError('Invalid history part metadata.')
        names.add(name)
    return document


def _valid_file(path, size, digest):
    return (path.is_file() and path.stat().st_size == size
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest)


def _stream(client, url, destination, *, maximum, expected_size=None, expected_hash=None):
    """Only callers construct URLs; manifests cannot provide hosts or redirects."""
    temporary = destination.with_suffix('.download')
    try:
        count = 0
        content_hash = hashlib.sha256()
        # Explicitly forbid redirects even when a caller supplies a permissive client.
        with client.stream('GET', url, follow_redirects=False) as response:
            response.raise_for_status()
            with temporary.open('wb') as output:
                for block in response.iter_bytes():
                    count += len(block)
                    if count > maximum:
                        raise ValueError('History part exceeds its published size.' if expected_size else
                                         'History manifest exceeds its size limit.')
                    content_hash.update(block)
                    output.write(block)
        if expected_size is not None and (count != expected_size or content_hash.hexdigest() != expected_hash):
            raise ValueError('History part failed its published checksum.')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install(store, directory, blob, document, base_url, client, *, rolling=False):
    digest = hashlib.sha256(blob).hexdigest()
    prior = store.get_checkpoint('career_history_bundle:' + digest, {})
    if prior.get('complete') and not rolling:
        return {**prior, 'already_imported': True}
    cache_root = Path(directory) / 'history'
    cache = (cache_root / 'rolling' if rolling else cache_root) / digest
    cache.mkdir(parents=True, exist_ok=True)
    local_manifest = cache / 'history_manifest.json'
    local_manifest.write_bytes(blob)
    shared = Path(directory) / 'history' / 'verified_parts'
    shared.mkdir(parents=True, exist_ok=True)
    for part in document['parts']:
        name, size, expected = part['file'], part['bytes'], part['sha256']
        target = cache / name
        verified = shared / (expected + '-' + name)
        if _valid_file(target, size, expected):
            if not _valid_file(verified, size, expected):
                shutil.copyfile(target, verified)
            continue
        if _valid_file(verified, size, expected):
            shutil.copyfile(verified, target)
            continue
        _stream(client, base_url + '/' + name, target, maximum=size,
                expected_size=size, expected_hash=expected)
        # Separate copies mean corruption in one manifest cache cannot mutate
        # an otherwise valid content-addressed copy used by another release.
        shutil.copyfile(target, verified)
    if prior.get('complete'):
        return {**prior, 'already_imported': True}
    backup_prefix = 'before-career-refresh-' if rolling else 'before-history-'
    backup = Path(directory) / 'backups' / (backup_prefix + digest[:16] + '.sqlite')
    backup.parent.mkdir(parents=True, exist_ok=True)
    store.backup(str(backup))
    result = import_history_bundle(store, local_manifest)
    return {**result, 'backup': str(backup)}


def _prune_rolling_artifacts(directory, digest):
    """Bound only this worker's new rolling caches/backups; keep pinned seeds."""
    root = Path(directory)
    backups = sorted((root / 'backups').glob('before-career-refresh-*.sqlite'),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for path in backups[2:]:
        path.unlink(missing_ok=True)
    folder = root / 'history' / 'rolling'
    caches = sorted((path for path in folder.iterdir()
                     if path.is_dir() and not path.is_symlink() and re.fullmatch(r'[0-9a-f]{64}', path.name)),
                    key=lambda path: path.stat().st_mtime, reverse=True)
    keep = {digest}
    keep.update(path.name for path in caches if path.name != digest and len(keep) < 2)
    for path in caches:
        if path.name not in keep:
            shutil.rmtree(path)


def install_pinned_history(store, directory, source_ref, manifest_path=MANIFEST, client=None):
    """Checksum every bounded part from the app's exact commit before merging."""
    if not re.fullmatch(r'[0-9a-f]{40}', str(source_ref or '')):
        raise ValueError('History installation requires the full deployed SOURCE_REF.')
    blob = Path(manifest_path).read_bytes()
    document = _manifest(blob)
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
    try:
        return _install(store, directory, blob, document,
                        f'https://raw.githubusercontent.com/{REPOSITORY}/{source_ref}/data/history', client)
    finally:
        if own_client:
            client.close()


def _authorize_rolling(store, document, matchup_path=None, targets_path=None):
    """Permit published current identities and explicitly named matchup players."""
    from dataset_refresh import load_scope
    from feeds import canonical_player
    bundled = load_scope()
    current = store.get_checkpoint('dataset_refresh_scope', bundled) or bundled
    known = {}
    for item in [*bundled.get('players', []), *current.get('players', [])]:
        key = canonical_player(item['player'])
        old = known.get(key)
        if old and old.get('espn_player_id') and item.get('espn_player_id') and str(old['espn_player_id']) != str(item['espn_player_id']):
            raise ValueError('Authorized player scope contains conflicting provider identities.')
        known[key] = item
    path = Path(matchup_path) if matchup_path else Path(__file__).with_name('matchup_refresh_scope.json')
    pairs = json.loads(path.read_text()).get('pairs', [])
    matchup_names = {canonical_player(row[field]) for row in pairs
                     for field in ('offense_player', 'defense_player') if row.get(field)}
    extra_path = Path(targets_path) if targets_path else Path(__file__).with_name('matchup_history_targets.json')
    for item in json.loads(extra_path.read_text()).get('players', []) if extra_path.exists() else []:
        item = {'player': item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not isinstance(item.get('player'), str):
            raise ValueError('Bundled matchup career targets have an invalid identity.')
        key = canonical_player(item['player'])
        registered = known.get(key)
        if (registered and registered.get('espn_player_id') and item.get('espn_player_id')
                and str(registered['espn_player_id']) != str(item['espn_player_id'])):
            raise ValueError('Bundled matchup target changes a registered provider identity.')
        known[key] = {**(registered or {}), **item}
        matchup_names.add(key)
    if document.get('identity_scope') != 'current_players_and_named_matchup_participants':
        raise ValueError('Rolling history manifest changes the authorized population.')
    players = document.get('players')
    if not isinstance(players, list) or not 1 <= len(players) <= 2000:
        raise ValueError('Rolling history player population is invalid.')
    names, providers, aliases, identity_providers = set(), set(), {}, {}
    for row in players:
        if not isinstance(row, dict) or not isinstance(row.get('player'), str):
            raise ValueError('Rolling player identity is invalid.')
        key = canonical_player(row['player'])
        provider = str(row.get('espn_player_id') or '')
        if (row.get('player_id') != key or not provider.isdigit() or int(provider) <= 0
                or key in names or provider in providers):
            raise ValueError('Rolling player identity is missing, ambiguous or duplicated.')
        registered = known.get(key)
        if not registered and key not in matchup_names:
            raise ValueError('Rolling history includes an unauthorized player.')
        if registered and registered.get('espn_player_id') and provider != str(registered['espn_player_id']):
            raise ValueError('Rolling history conflicts with a registered provider identity.')
        approved_aliases = row.get('aliases', [])
        if (not isinstance(approved_aliases, list) or len(approved_aliases) > 50
                or any(not isinstance(name, str) or not name.strip() for name in approved_aliases)):
            raise ValueError('Rolling history aliases are invalid.')
        names.add(key); providers.add(provider)
        aliases[key] = {canonical_player(name) for name in [row['player'], *approved_aliases]}
        identity_providers[key] = provider
    coverage = document.get('coverage')
    if not isinstance(coverage, list) or len(coverage) > 2000:
        raise ValueError('Rolling history lacks per-player coverage.')
    statuses = {'available_history', 'unresolved_identity', 'source_unavailable', 'no_published_history'}
    covered, missing, seen, covered_identities = [], [], set(), set()
    for row in coverage:
        if (not isinstance(row, dict) or not isinstance(row.get('player'), str)
                or row.get('status') not in statuses):
            raise ValueError('Rolling history coverage schema is invalid.')
        requested = row.get('requested_player') or row['player']
        if not isinstance(requested, str):
            raise ValueError('Rolling history coverage has an invalid requested identity.')
        key = canonical_player(requested)
        identity = row.get('player_id') or canonical_player(row['player'])
        if key in seen or key not in known and key not in matchup_names:
            raise ValueError('Rolling history coverage has a duplicate or unauthorized player.')
        if row.get('status') != 'unresolved_identity':
            if identity not in names or key not in aliases[identity]:
                raise ValueError('Rolling history coverage claims an absent or unapproved alias identity.')
            registered = known.get(key)
            if (registered and registered.get('espn_player_id')
                    and str(registered['espn_player_id']) != identity_providers[identity]):
                raise ValueError('Rolling coverage changes a registered alias provider identity.')
            if row.get('status') == 'available_history':
                covered_identities.add(identity)
        seen.add(key)
        (covered if row['status'] == 'available_history' else missing).append(requested)
    if not names <= (seen | covered_identities):
        raise ValueError('Rolling history omits player coverage records.')
    if not matchup_names <= seen:
        raise ValueError('Rolling history omits an explicitly named matchup participant.')
    resolutions = document.get('target_resolution', [])
    if not isinstance(resolutions, list) or len(resolutions) > 2000:
        raise ValueError('Rolling history target identity mappings are invalid.')
    resolution_names = set()
    for row in resolutions:
        if not isinstance(row, dict) or not isinstance(row.get('requested_player'), str):
            raise ValueError('Rolling history target identity mapping is invalid.')
        key = canonical_player(row['requested_player'])
        identity = row.get('player_id')
        if (key not in seen or key in resolution_names or identity not in names
                or key not in aliases[identity]
                or str(row.get('espn_player_id') or '') != identity_providers[identity]):
            raise ValueError('Rolling history target identity mapping is outside its approved alias/provider binding.')
        resolution_names.add(key)
    return {'requested_players': len(coverage), 'covered_players': len(covered),
            'missing_players': len(missing), 'missing_player_names': missing[:30],
            'coverage_claim': 'Available published history; complete careers are not independently verified.'}


def install_rolling_history(store, directory, *, now=None, client=None, matchup_path=None, targets_path=None):
    """Check the fixed public relay, then add verified available matchup careers."""
    now = _now(now)
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
    cache = Path(directory) / 'history'
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / 'rolling_history_manifest.json'
    try:
        _stream(client, ROLLING_MANIFEST_URL, destination, maximum=MAX_MANIFEST_BYTES)
        blob = destination.read_bytes()
        document = _manifest(blob, now)
        coverage = _authorize_rolling(store, document, matchup_path, targets_path)
        result = _install(store, directory, blob, document, ROLLING_BASE, client, rolling=True)
        from career_metadata import complete_verified_game_metadata
        local_manifest = (Path(directory) / 'history' / 'rolling' /
                          hashlib.sha256(blob).hexdigest() / 'history_manifest.json')
        repairs = complete_verified_game_metadata(store, local_manifest, now=now)
    finally:
        if own_client:
            client.close()
    status = {'phase': 'updated' if not coverage['missing_players'] else 'partial',
              'last_checked_at': now.isoformat(), 'source': ROLLING_MANIFEST_URL,
              'manifest_sha256': result['manifest_sha256'], 'source_data_through_date': document['source_data_through_date'],
              **coverage, 'coverage': document['coverage'], 'metadata_repairs': repairs,
              'note': 'Historical source checks do not establish current schedule, injury or game-stat readiness.'}
    store.set_checkpoint('matchup_career_history_status', status)
    store.set_checkpoint('career_history_identity_scope', {
        'players': document['players'], 'target_resolution': document.get('target_resolution', []),
        'coverage': document['coverage'], 'manifest_sha256': result['manifest_sha256'],
        'source': ROLLING_MANIFEST_URL, 'checked_at': now.isoformat(),
        'policy': 'Verified published identity aliases; no change to current-game freshness or roster membership.'})
    cleanup_error = None
    try:
        _prune_rolling_artifacts(directory, result['manifest_sha256'])
    except OSError as error:
        cleanup_error = type(error).__name__
    return {**result, **coverage, 'rolling': True, 'metadata_repairs': repairs,
            **({'cleanup_error': cleanup_error} if cleanup_error else {})}


async def history_upgrade_service(store, directory, *, ready=lambda: True, exporter=None):
    """Install pinned history once, then check opponent career additions hourly."""
    pinned = None
    while True:
        if not ready():
            await asyncio.sleep(5)
            continue
        stamp = dt.datetime.now(dt.timezone.utc)
        old = store.get_checkpoint('history_upgrade_status', {}) or {}
        try:
            if pinned is None:
                store.set_checkpoint('history_upgrade_status', {'phase': 'checking', 'last_attempt': stamp.isoformat()})
                pinned = await asyncio.to_thread(install_pinned_history, store, directory, os.getenv('SOURCE_REF'))
                status = {'phase': 'updated', 'last_attempt': stamp.isoformat(), **pinned,
                          'note': 'Pinned history merged; rolling matchup-career checks run hourly.'}
                store.set_checkpoint('history_upgrade_status', status)
                if exporter and not pinned.get('already_imported'):
                    try:
                        await asyncio.to_thread(exporter, store, Path(directory) / 'exports')
                    except Exception as error:
                        store.set_checkpoint('history_upgrade_status', {**status, 'export_error': type(error).__name__})
            rolling = await asyncio.to_thread(install_rolling_history, store, directory, now=stamp)
            status = {**store.get_checkpoint('history_upgrade_status', {}), 'last_attempt': stamp.isoformat(),
                      'phase': 'partial' if rolling.get('missing_players') else 'updated',
                      'pinned_complete': True, 'rolling_history': rolling,
                      'next_check_at': (stamp + dt.timedelta(seconds=POLL_SECONDS)).isoformat(),
                      'note': 'Available matchup career history checked; current game readiness remains a separate check.'}
            store.set_checkpoint('history_upgrade_status', status)
            if exporter and (not rolling.get('already_imported') or
                             (rolling.get('metadata_repairs') or {}).get('games_completed', 0) > 0):
                try:
                    await asyncio.to_thread(exporter, store, Path(directory) / 'exports')
                except Exception as error:
                    store.set_checkpoint('history_upgrade_status', {**status, 'export_error': type(error).__name__})
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = {**store.get_checkpoint('history_upgrade_status', old), 'phase': 'retrying',
                      'last_attempt': stamp.isoformat(), 'error_type': type(error).__name__,
                      'pinned_complete': bool(pinned),
                      'next_retry_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=RETRY_SECONDS)).isoformat(),
                      'note': 'Existing history retained; an unavailable source or unverified identity will be retried hourly.'}
            store.set_checkpoint('history_upgrade_status', status)
            await asyncio.sleep(RETRY_SECONDS)
