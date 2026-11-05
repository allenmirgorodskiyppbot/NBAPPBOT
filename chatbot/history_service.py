"""Install the release's verified career history without replacing live data."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

import httpx

from history_backfill import import_history_bundle

REPOSITORY = 'allenmirgorodskiyppbot/NBAPPBOT'
MANIFEST = Path(__file__).with_name('history_manifest.json')
MAX_PART_BYTES = 32_000_000


def install_pinned_history(store, directory, source_ref, manifest_path=MANIFEST, client=None):
    """Checksum every bounded part from the app's exact source commit before merging."""
    if not re.fullmatch(r'[0-9a-f]{40}', str(source_ref or '')):
        raise ValueError('History installation requires the full deployed SOURCE_REF.')
    manifest_bytes = Path(manifest_path).read_bytes()
    if len(manifest_bytes) > 4_000_000:
        raise ValueError('History manifest is too large.')
    manifest = json.loads(manifest_bytes)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    prior = store.get_checkpoint('career_history_bundle:' + digest, {})
    if prior.get('complete'):
        return {**prior, 'already_imported': True}
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
            or manifest.get('format') != 'nba-history-jsonl-gzip-v1'
            or not isinstance(manifest.get('parts'), list)):
        raise ValueError('Invalid history manifest.')
    if not 1 <= len(manifest['parts']) <= 100:
        raise ValueError('Invalid history bundle part count.')
    names = set()
    for part in manifest['parts']:
        if not isinstance(part, dict):
            raise ValueError('Invalid history part metadata.')
        name, size, expected = part.get('file'), part.get('bytes'), part.get('sha256')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.jsonl\.gz', name)
                or name in names or type(size) is not int or not 0 < size <= MAX_PART_BYTES
                or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise ValueError('Invalid history part metadata.')
        names.add(name)
    cache = Path(directory) / 'history' / digest
    cache.mkdir(parents=True, exist_ok=True)
    local_manifest = cache / 'history_manifest.json'
    local_manifest.write_bytes(manifest_bytes)
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)
    try:
        for part in manifest['parts']:
            name, size, expected = part.get('file'), part.get('bytes'), part.get('sha256')
            destination = cache / name
            if (destination.exists() and destination.stat().st_size == size
                    and hashlib.sha256(destination.read_bytes()).hexdigest() == expected):
                continue
            temporary = destination.with_suffix('.download')
            try:
                url = f'https://raw.githubusercontent.com/{REPOSITORY}/{source_ref}/data/history/{name}'
                count = 0
                content_hash = hashlib.sha256()
                with client.stream('GET', url) as response:
                    response.raise_for_status()
                    with temporary.open('wb') as output:
                        for block in response.iter_bytes():
                            count += len(block)
                            if count > size:
                                raise ValueError('History part exceeds its published size.')
                            content_hash.update(block)
                            output.write(block)
                if count != size or content_hash.hexdigest() != expected:
                    raise ValueError('History part failed its published checksum.')
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        if own_client:
            client.close()
    backup = Path(directory) / 'backups' / ('before-history-' + digest[:16] + '.sqlite')
    backup.parent.mkdir(parents=True, exist_ok=True)
    # A previous failed attempt may have left a backup while the live bot kept
    # collecting entries. Refresh it atomically immediately before this merge.
    store.backup(str(backup))
    result = import_history_bundle(store, local_manifest)
    return {**result, 'backup': str(backup)}


async def history_upgrade_service(store, directory, *, ready=lambda: True, exporter=None):
    """One idempotent upgrade per release manifest; retry failures hourly."""
    while True:
        if not ready():
            await asyncio.sleep(5)
            continue
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        store.set_checkpoint('history_upgrade_status', {'phase': 'checking', 'last_attempt': stamp})
        try:
            result = await asyncio.to_thread(install_pinned_history, store, directory, os.getenv('SOURCE_REF'))
            status = {'phase': 'updated', 'last_attempt': stamp, **result,
                      'note': 'Career history merged; current schedule, availability and daily coverage are separate checks.'}
            store.set_checkpoint('history_upgrade_status', status)
            if exporter and not result.get('already_imported'):
                try:
                    await asyncio.to_thread(exporter, store, Path(directory) / 'exports')
                except Exception as error:
                    store.set_checkpoint('history_upgrade_status', {**status, 'export_error': type(error).__name__})
            # The manifest is pinned for the lifetime of this process. Restart
            # after a deployment loads its new manifest and checks it once.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            store.set_checkpoint('history_upgrade_status', {'phase': 'retrying', 'last_attempt': stamp,
                'error_type': type(error).__name__,
                'next_retry_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
                'note': 'Existing master records retained; history download or validation did not complete.'})
            await asyncio.sleep(3600)
