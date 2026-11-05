"""Hourly, independent sync of public repairs, guarding history and availability.

Only fixed public repository paths are fetched. Downloads retain their source
clocks; this worker never marks today's schedule or game statistics ready.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading

import httpx

from history_backfill import import_history_bundle
from history_service import REPOSITORY, _manifest, _now, _stream, _valid_file
from measured_history import BASE as MEASURED_BASE, MANIFEST as MEASURED_MANIFEST
from measured_history import PART as MEASURED_PART, refresh_measured_history
from stat_completion import complete_verified_player_statistics
from structured_availability import (MAX_COMPRESSED_BYTES, import_availability_document,
                                     load_availability_document)

BASE = f'https://raw.githubusercontent.com/{REPOSITORY}/main/data'
REPAIR_BASE = BASE + '/gap_repairs/'
SUPPLEMENTAL_MANIFEST = 'supplemental_manifest.json'
HISTORY_MANIFEST = 'history_manifest.json'
STARTER_ROSTER = 'historical_starter_flags_dated.jsonl.gz'
SCORE_CROSSWALK = 'game_scores_provider_crosswalk.jsonl.gz'
AVAILABILITY_URL = BASE + '/live/structured_availability.json.gz'
CHECKPOINT = 'supplemental_data_status'
POLL_SECONDS = 3600
MAX_METADATA_BYTES = 64_000
MAX_MANIFEST_BYTES = 4_000_000
MAX_ROSTER_BYTES = 32_000_000
MAX_HISTORY_BYTES = 128_000_000
MAX_MEASURED_BYTES = 128_000_000
_LOCK = threading.Lock()


def _asset(path, name):
    blob = Path(path).read_bytes()
    return {'file': name, 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}


def build_supplemental_manifest(history_manifest_path, starter_roster_path, output_path,
                                *, provider_crosswalk_path=None):
    """Write the two immutable asset checksums to publish beside the repair files."""
    history = Path(history_manifest_path)
    _manifest(history.read_bytes())
    result = {'schema_version': 1, 'format': 'nba-supplemental-data-v1',
              'history_manifest': _asset(history, HISTORY_MANIFEST),
              'starter_roster': _asset(starter_roster_path, STARTER_ROSTER),
              'policy': 'Historical source evidence only; no current-game freshness certification.'}
    if provider_crosswalk_path is not None:
        result['provider_crosswalk'] = _asset(provider_crosswalk_path, SCORE_CROSSWALK)
    _metadata(json.dumps(result).encode())
    Path(output_path).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def _metadata(blob):
    if not 0 < len(blob) <= MAX_METADATA_BYTES:
        raise ValueError('Supplemental metadata exceeds its byte bound.')
    value = json.loads(blob)
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('format') != 'nba-supplemental-data-v1'):
        raise ValueError('Unsupported supplemental metadata.')
    assets = [('history_manifest', HISTORY_MANIFEST, MAX_MANIFEST_BYTES),
              ('starter_roster', STARTER_ROSTER, MAX_ROSTER_BYTES)]
    if 'provider_crosswalk' in value:
        assets.append(('provider_crosswalk', SCORE_CROSSWALK, 8_000_000))
    for field, name, maximum in assets:
        row = value.get(field)
        if (not isinstance(row, dict) or row.get('file') != name
                or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= maximum
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256') or ''))):
            raise ValueError('Invalid supplemental asset checksum or fixed path.')
    return value


def _ensure(client, base, folder, item):
    target = folder / item['file']
    if not _valid_file(target, item['bytes'], item['sha256']):
        _stream(client, base + item['file'], target, maximum=item['bytes'],
                expected_size=item['bytes'], expected_hash=item['sha256'])
    return target


def _failure(previous, error, now):
    result = {**previous, 'phase': 'unavailable', 'last_checked_at': now.isoformat(),
              'error_type': type(error).__name__, 'previous_evidence_retained': True}
    if isinstance(error, ValueError):
        result['detail'] = str(error)[:250]
    elif isinstance(error, httpx.HTTPStatusError):
        result['http_status'] = error.response.status_code
        result['detail'] = 'Public supplemental source returned an HTTP error.'
    else:
        result['detail'] = 'Public supplemental evidence could not be retrieved or validated.'
    return result


def _success(previous, now, **values):
    result = {key: value for key, value in previous.items()
              if key not in {'error_type', 'detail', 'http_status', 'previous_evidence_retained'}}
    return {**result, 'phase': 'updated', 'last_checked_at': now.isoformat(),
            'last_success_at': now.isoformat(), **values}


def cached_provider_crosswalk(data_dir):
    """Return only a locally checksum-matched crosswalk for the rolling worker."""
    root = Path(data_dir) / 'supplemental'
    try:
        metadata = _metadata((root / 'supplemental-check.json').read_bytes())
        item = metadata.get('provider_crosswalk')
        if item is not None:
            path = root / 'repairs' / metadata['history_manifest']['sha256'] / item['file']
            if _valid_file(path, item['bytes'], item['sha256']):
                return path
    except (OSError, ValueError, TypeError):
        pass
    return None


def _cached_rolling_manifest(store, root):
    rolling = store.get_checkpoint('matchup_career_history_status', {}) or {}
    digest = rolling.get('manifest_sha256')
    if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
        path = root.parent / 'history' / 'rolling' / digest / HISTORY_MANIFEST
        if path.is_file():
            return path, digest
    return None, None


def _game_scores(store, root, static_path, crosswalk, previous, now):
    from game_stat_completion import complete_verified_game_scores

    sources = [('static', static_path)]
    path, digest = _cached_rolling_manifest(store, root)
    if path is not None:
        sources.append(('rolling', path))
    result, changed = {}, False
    for key, path in sources:
        old = previous.get(key, {})
        try:
            if key == 'rolling' and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Cached rolling manifest differs from its verified saved identity.')
            repaired = complete_verified_game_scores(store, path, now=now,
                                                      provider_crosswalk_path=crosswalk)
            # Keep status readable while retaining the exact number of held games.
            conflicts = repaired.get('conflicts', [])
            repaired['conflicts_count'] = len(conflicts)
            repaired['conflicts'] = conflicts[:20]
            result[key] = _success(old, now, **repaired)
            changed |= repaired['fields_completed'] > 0
        except Exception as error:
            result[key] = _failure(old, error, now)
    if 'rolling' not in result:
        result['rolling'] = {'phase': 'not_saved', 'detail': 'No verified rolling career manifest is cached yet.'}
    phases = [result[key]['phase'] for key, _ in sources]
    result['phase'] = ('unavailable' if all(p == 'unavailable' for p in phases) else
                       'partial' if any(p == 'unavailable' for p in phases) else 'updated')
    result['fields_completed'] = sum(result[key].get('fields_completed', 0)
                                     for key, _ in sources if result[key]['phase'] == 'updated')
    return result, changed


def _player_statistics(store, root, static_path, roster, previous, now):
    """Complete independently from each verified source, without new downloads.

    Existing flat static counters remain available. Completed fields and the
    statistic totals aggregate both sources; nested reports retain each source's
    own coverage, audit and backup. Completed row totals count source operations,
    since one row can receive different missing fields from both sources.
    """
    sources = [('static', static_path)]
    rolling_path, digest = _cached_rolling_manifest(store, root)
    if rolling_path is not None:
        sources.append(('rolling', rolling_path))
    reports, changed = {}, False
    for key, path in sources:
        old = previous.get(key, previous if key == 'static' else {})
        try:
            if key == 'rolling' and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Cached rolling manifest differs from its verified saved identity.')
            completed = complete_verified_player_statistics(store, path, now=now,
                                                            starter_roster_path=roster)
            reports[key] = _success(old, now, **completed)
            changed |= completed['fields_completed'] > 0
        except Exception as error:
            reports[key] = _failure(old, error, now)
    static = reports['static']
    result = {**static, **reports}
    if 'rolling' not in result:
        result['rolling'] = {'phase': 'not_saved', 'detail': 'No verified rolling career manifest is cached yet.'}
    phases = [reports[key]['phase'] for key, _ in sources]
    result['phase'] = ('unavailable' if all(p == 'unavailable' for p in phases) else
                       'partial' if any(p == 'unavailable' for p in phases) else 'updated')
    current = [reports[key] for key, _ in sources if reports[key]['phase'] == 'updated']
    for name in ('fields_completed', 'rows_completed', 'fields_fillable', 'rows_fillable'):
        result[name] = sum(item[name] for item in current)
    for name in ('completed_by_statistic', 'fillable_by_statistic'):
        totals = Counter()
        for item in current:
            totals.update(item[name])
        result[name] = dict(totals)
    result['row_count_scope'] = 'Sum of per-source completed rows; one row may be completed by both sources.'
    return result, changed


def _repairs(store, root, client, previous, now):
    """A failed insert and a failed NULL completion are reported separately."""
    checked = root / 'supplemental-check.json'
    _stream(client, REPAIR_BASE + SUPPLEMENTAL_MANIFEST, checked, maximum=MAX_METADATA_BYTES)
    blob = checked.read_bytes(); metadata = _metadata(blob)
    digest = hashlib.sha256(blob).hexdigest()
    # A roster-only publication must not fetch the unchanged career parts again.
    folder = root / 'repairs' / metadata['history_manifest']['sha256']
    folder.mkdir(parents=True, exist_ok=True)
    path = _ensure(client, REPAIR_BASE, folder, metadata['history_manifest'])
    document = _manifest(path.read_bytes(), now)
    if sum(part['bytes'] for part in document['parts']) > MAX_HISTORY_BYTES:
        raise ValueError('Supplemental history exceeds its total download bound.')
    for part in document['parts']:
        _ensure(client, REPAIR_BASE, folder, part)
    roster = _ensure(client, REPAIR_BASE, folder, metadata['starter_roster'])
    crosswalk, crosswalk_error = None, None
    if 'provider_crosswalk' in metadata:
        try:
            crosswalk = _ensure(client, REPAIR_BASE, folder, metadata['provider_crosswalk'])
        except Exception as error:
            crosswalk_error = error
    result = {'phase': 'updated', 'source': REPAIR_BASE + SUPPLEMENTAL_MANIFEST,
              'metadata_sha256': digest, 'manifest_sha256': metadata['history_manifest']['sha256'],
              'starter_roster_sha256': metadata['starter_roster']['sha256'],
              'source_data_through_date': document['source_data_through_date'],
              'last_checked_at': now.isoformat()}
    changed = False
    # Validate the decompressed bundle before import_history_bundle can write.
    complete_verified_player_statistics(store, path, now=now, dry_run=True,
                                       starter_roster_path=roster)
    old_import = previous.get('history_import', {})
    try:
        manifest_digest = metadata['history_manifest']['sha256']
        imported_before = store.get_checkpoint('career_history_bundle:' + manifest_digest, {}) or {}
        if not imported_before.get('complete'):
            backup = root / 'backups' / ('before-supplemental-history-' + manifest_digest[:16] + '.sqlite')
            backup.parent.mkdir(parents=True, exist_ok=True)
            store.backup(str(backup))
        imported = import_history_bundle(store, path)
        result['history_import'] = _success(old_import, now, **imported)
        changed |= bool(not imported.get('already_imported') and
                        (imported.get('game_inserted', 0) or imported.get('player_game_inserted', 0)))
    except Exception as error:
        result['history_import'] = _failure(old_import, error, now)
    old_scores = previous.get('game_score_completion', {})
    try:
        if crosswalk_error is not None:
            raise crosswalk_error
        result['game_score_completion'], score_changed = _game_scores(
            store, root, path, crosswalk, old_scores, now)
        changed |= score_changed
    except Exception as error:
        result['game_score_completion'] = _failure(old_scores, error, now)
    old_completion = previous.get('stat_completion', {})
    try:
        result['stat_completion'], statistics_changed = _player_statistics(
            store, root, path, roster, old_completion, now)
        changed |= statistics_changed
    except Exception as error:
        result['stat_completion'] = _failure(old_completion, error, now)
    if any(result[key]['phase'] in {'unavailable', 'partial'} for key in
           ('history_import', 'game_score_completion', 'stat_completion')):
        result['phase'] = 'partial'
    else:
        result['last_success_at'] = now.isoformat()
    return result, changed


def _measured(store, root, client, previous, now):
    from dataset_refresh import load_scope

    scope = store.get_checkpoint('dataset_refresh_scope', load_scope()) or load_scope()
    checked = root / 'measured-check.json'
    _stream(client, MEASURED_BASE + MEASURED_MANIFEST, checked, maximum=MAX_MANIFEST_BYTES)
    blob = checked.read_bytes(); document = json.loads(blob)
    if not isinstance(document, dict) or not isinstance(document.get('parts'), list):
        raise ValueError('Measured history lacks a bounded part inventory.')
    parts = document['parts']
    if not 1 <= len(parts) <= 100:
        raise ValueError('Measured history has an invalid part count.')
    names, total = set(), 0
    for part in parts:
        if (not isinstance(part, dict) or not isinstance(part.get('file'), str)
                or not MEASURED_PART.fullmatch(part['file']) or part['file'] in names
                or type(part.get('bytes')) is not int or not 0 < part['bytes'] <= 8_000_000
                or not re.fullmatch(r'[0-9a-f]{64}', str(part.get('sha256') or ''))):
            raise ValueError('Measured history has an invalid asset checksum or path.')
        names.add(part['file']); total += part['bytes']
    if total > MAX_MEASURED_BYTES:
        raise ValueError('Measured history exceeds its total download bound.')
    digest = hashlib.sha256(blob).hexdigest()
    if (previous.get('manifest_sha256') == digest and previous.get('last_success_at')
            and previous.get('scope_revision') == scope.get('revision')):
        return _success(previous, now, phase='unchanged'), False
    folder = root / 'measured' / digest; folder.mkdir(parents=True, exist_ok=True)
    (folder / MEASURED_MANIFEST).write_bytes(blob)
    for part in parts:
        _ensure(client, MEASURED_BASE, folder, part)
    result = refresh_measured_history(store, folder, client, now=now, scope=scope)
    if result.get('phase') != 'updated':
        raise ValueError(result.get('detail') or 'Measured source did not pass its importer.')
    return _success(previous, now, **result, manifest_sha256=digest, scope_revision=scope.get('revision'),
                    source=MEASURED_BASE + MEASURED_MANIFEST), bool(result['added'] or result['conflicts'])


def _availability(store, root, client, previous, now):
    path = root / 'structured_availability.json.gz'
    _stream(client, AVAILABILITY_URL, path, maximum=MAX_COMPRESSED_BYTES)
    blob = path.read_bytes(); digest = hashlib.sha256(blob).hexdigest()
    if previous.get('content_sha256') == digest and previous.get('last_success_at'):
        return _success(previous, now, phase='unchanged'), False
    document = load_availability_document(blob)
    result = import_availability_document(store, document, now=now)
    return _success(previous, now, **result, source=AVAILABILITY_URL, content_sha256=digest,
                    source_generated_at=document.get('generated_at')), result['records_added'] > 0


def _prune(root, kept=2):
    # Delete only this worker's old cache directories; never touch user history.
    for name in ('repairs', 'measured'):
        folder = root / name
        if not folder.exists():
            continue
        children = sorted((p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()
                           and re.fullmatch(r'[0-9a-f]{64}', p.name)),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        for child in children[kept:]:
            shutil.rmtree(child)
    backups = root / 'backups'
    if backups.exists():
        own = sorted((p for p in backups.iterdir() if p.is_file() and not p.is_symlink()
                      and re.fullmatch(r'before-supplemental-history-[0-9a-f]{16}\.sqlite', p.name)),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        for backup in own[kept:]:
            backup.unlink()


def _refresh(store, data_dir, client, now):
    old = store.get_checkpoint(CHECKPOINT, {}) or {}
    try:
        attempted = dt.datetime.fromisoformat(old.get('last_attempt', ''))
    except (TypeError, ValueError):
        attempted = None
    if attempted and attempted.tzinfo and 0 <= (now - attempted).total_seconds() < POLL_SECONDS:
        return {**old, 'changed': False, 'throttled': True}
    if not _LOCK.acquire(blocking=False):
        return {'phase': 'busy', 'changed': False, 'retry_seconds': POLL_SECONDS}
    owned = client is None
    try:
        client = client or httpx.Client(timeout=httpx.Timeout(45, connect=15), follow_redirects=False)
        root = Path(data_dir) / 'supplemental'; root.mkdir(parents=True, exist_ok=True)
        result = {'phase': 'checking', 'last_attempt': now.isoformat(),
                  'next_check_at': (now + dt.timedelta(seconds=POLL_SECONDS)).isoformat(),
                  'retry_seconds': POLL_SECONDS, 'changed': False,
                  'note': 'Historical and availability source clocks are retained; current game readiness is checked independently.'}
        store.set_checkpoint(CHECKPOINT, {**old, **result})
        for key, action in [('repairs', _repairs), ('measured_history', _measured),
                            ('availability', _availability)]:
            previous = old.get(key, {})
            try:
                result[key], changed = action(store, root, client, previous, now)
                result['changed'] |= changed
            except Exception as error:
                result[key] = _failure(previous, error, now)
            store.set_checkpoint(CHECKPOINT, {**old, **result})
        phases = [result[key]['phase'] for key in ('repairs', 'measured_history', 'availability')]
        result['phase'] = ('unavailable' if all(p == 'unavailable' for p in phases) else
                           'partial' if any(p in {'unavailable', 'partial'} for p in phases) else 'updated')
        if result['phase'] == 'updated':
            result['last_success_at'] = now.isoformat()
        elif old.get('last_success_at'):
            result['last_success_at'] = old['last_success_at']
        try:
            _prune(root)
        except OSError:
            result['cache_cleanup_error'] = 'OSError'
        store.set_checkpoint(CHECKPOINT, result)
        return result
    finally:
        if owned and client is not None:
            client.close()
        _LOCK.release()


async def refresh_supplemental_data(store, data_dir, client=None, now=None):
    """Perform one bounded hourly sync off the event loop; safe to call repeatedly."""
    return await asyncio.to_thread(_refresh, store, data_dir, client, _now(now))
