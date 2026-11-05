"""Publish dated, actual guarding observations from the public NBA archive.

The archive begins in 2017-18. Its observed pair-games are not proof of full
career coverage. Regular-season, playoff and play-in samples remain separate.
Only the collector needs PyArrow; the bot reads the bounded JSONL relay.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile

from tracking_publications import game_date_inventory, measured_matchups

FORMAT = 'nba-measured-pair-history-jsonl-gzip-v1'
MANIFEST = 'historical_matchups_manifest.json'
MATCHUP_TAG = 'nba_stats_game_matchups'
SCHEDULE_TAG = 'nba_stats_schedules'
REPO = 'sportsdataverse/sportsdataverse-data'
MAX_SOURCE_BYTES = 8_000_000
MAX_PART_BYTES = 8_000_000
MAX_MANIFEST_BYTES = 4_000_000
MAX_LINE_BYTES = 100_000
MAX_SEASON_ROWS = 1_000_000
FIRST_END_YEAR = 2018


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _aware(value):
    moment = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('Source timestamps must include a timezone.')
    return moment


def _source_url(tag, name):
    return f'https://github.com/{REPO}/releases/download/{tag}/{name}'


def _read_document(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}


def _configuration():
    pairs = json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
    path = Path(__file__).with_name('matchup_history_targets.json')
    aliases = json.loads(path.read_text()).get('players', []) if path.exists() else []
    return pairs, aliases


async def _asset(mirror, tag, name, cache, now, *, supplied=None, prior_sources=()):
    """Recheck release metadata, reusing only independently hash-checked bytes."""
    release = await mirror.release(tag)
    upstream = release.get(name)
    if not isinstance(upstream, dict):
        raise ValueError(f'Published source asset is absent: {name}')
    url = _source_url(tag, name)
    if upstream.get('browser_download_url') != url:
        raise ValueError('Unexpected public source URL.')
    published = upstream.get('updated_at')
    if not published or _aware(published) > now:
        raise ValueError('Source publication time is absent or in the future.')
    size = upstream.get('size')
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_SOURCE_BYTES:
        raise ValueError('Public source asset exceeds its byte bound.')
    digest = upstream.get('digest')
    if digest is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Unsupported public source digest.')
    signature = {'source': url, 'source_published_at': published, 'bytes': size, 'digest': digest}
    body_path = cache / name
    meta_path = cache / (name + '.json')
    previous = _read_document(meta_path)
    content = None
    if all(previous.get(key) == value for key, value in signature.items()) and body_path.exists():
        cached = body_path.read_bytes()
        if len(cached) == size and _sha(cached) == previous.get('source_content_sha256'):
            content = cached
    cache_valid = content is not None
    retrieved = previous.get('retrieved_at') if cache_valid else None
    if content is None:
        if supplied is None:
            content, fetched = await mirror.asset(tag, name)
        else:
            content, fetched = supplied
        if fetched.get('browser_download_url') != url:
            raise ValueError('Downloaded source identity differs from the release.')
        retrieved = fetched.get('_retrieved_at') or now.isoformat()
    actual = _sha(content)
    if len(content) != size or (digest and actual != digest[7:]):
        raise ValueError('Downloaded source length or SHA256 differs from its release.')
    # A workflow cache miss must not make unchanged historical evidence look new.
    for prior in prior_sources:
        if prior.get('source') == url and prior.get('source_content_sha256') == actual:
            retrieved = prior.get('retrieved_at') or retrieved
            break
    if not retrieved or _aware(retrieved) > now or _aware(retrieved) < _aware(published):
        raise ValueError('Source retrieval time is inconsistent.')
    meta = {**signature, 'source_content_sha256': actual, 'retrieved_at': retrieved}
    cache.mkdir(parents=True, exist_ok=True)
    if previous != meta or not cache_valid:
        temporary = body_path.with_suffix('.parquet.tmp')
        temporary.write_bytes(content)
        temporary.replace(body_path)
        meta_path.write_bytes(_json(meta) + b'\n')
    return content, meta


def _rows(content, scope=None):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(content))
    count = table.num_rows
    if count > MAX_SEASON_ROWS:
        raise ValueError('Public season source exceeds its row bound.')
    # All known IDs are checked again against exact names by the normalizer.
    # The Arrow filter avoids materializing 250,000 broad historical rows.
    if scope and all(player.get('nba_player_id') for player in scope['players']):
        for column in ('off_person_id', 'def_person_id'):
            if column not in table.column_names:
                raise ValueError('Actual matchup person identity is absent.')
            ids = [int(player['nba_player_id']) for player in scope['players']]
            value_set = pa.array(ids, type=table[column].type)
            table = table.filter(pc.is_in(table[column], value_set=value_set))
    return table.to_pylist(), count


def _phases(year, source, schedule, logs, scope, pairs, aliases, now):
    content, source_meta = source
    raw, raw_count = _rows(content, scope)
    schedule_rows, _ = _rows(schedule[0])
    log_rows = _rows(logs[0])[0] if logs else []
    mapping = [{key: meta[key] for key in ('source', 'source_published_at', 'source_content_sha256')}
               for _, meta in [schedule] + ([logs] if logs else [])]
    season = f'{year - 1}-{str(year)[-2:]}'
    result = []
    for phase in ('Regular Season', 'Playoffs', 'Play-In'):
        inventory, held = game_date_inventory(schedule_rows, log_rows, season,
            mapping_sources=mapping, season_type=phase)
        records, measured, report = measured_matchups(raw, inventory, scope, pairs, season,
            source=source_meta['source'], published_at=source_meta['source_published_at'],
            source_hash=source_meta['source_content_sha256'], now=now,
            alias_rows=aliases, season_type=phase)
        for row in records + measured:
            row['retrieved_at'] = source_meta['retrieved_at']
        report.update(season=season, season_type=phase, published_source_rows=raw_count,
                      scoped_source_rows=len(raw), verified_inventory_games=len(inventory),
                      date_mapping_exclusions=held, source=source_meta['source'],
                      source_content_sha256=source_meta['source_content_sha256'],
                      source_published_at=source_meta['source_published_at'])
        result.append((records, measured, report))
    return result


def _part_contents(items):
    # At most 7.5MB uncompressed makes the gzip 8MB bound conservative even
    # for incompressible JSON. Every single observation is independently bound.
    buffer = bytearray()
    count = 0
    for item in items:
        line = _json(item) + b'\n'
        if len(line) > MAX_LINE_BYTES:
            raise ValueError('One measured-pair record exceeds its byte bound.')
        if buffer and len(buffer) + len(line) > 7_500_000:
            yield gzip.compress(bytes(buffer), mtime=0), count
            buffer, count = bytearray(), 0
        buffer.extend(line)
        count += 1
    if buffer:
        yield gzip.compress(bytes(buffer), mtime=0), count


def _publish(output, manifest, items):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pair-publication-', dir=output.parent) as staging:
        staging = Path(staging)
        parts = []
        for index, (content, count) in enumerate(_part_contents(items), 1):
            if len(content) > MAX_PART_BYTES:
                raise ValueError('Compressed measured-pair part exceeds its byte bound.')
            digest = _sha(content)
            filename = f'tracking_pair_history_{digest[:12]}_{index:03d}.jsonl.gz'
            (staging / filename).write_bytes(content)
            parts.append({'file': filename, 'sha256': digest, 'bytes': len(content), 'records': count})
        manifest['parts'] = parts
        document = _json(manifest) + b'\n'
        if len(document) > MAX_MANIFEST_BYTES:
            raise ValueError('Measured-pair history manifest exceeds its byte bound.')
        # Content-addressed parts preserve the entire previously referenced
        # generation until the new manifest is atomically installed.
        for part in parts:
            (staging / part['file']).replace(output / part['file'])
        (staging / MANIFEST).write_bytes(document)
        (staging / MANIFEST).replace(output / MANIFEST)
        active = {part['file'] for part in parts}
        for path in output.glob('tracking_pair_history_*.jsonl.gz'):
            if path.name not in active:
                path.unlink()
    return parts


async def collect_historical_matchups(mirror, output, now=None, current_data=None, cache_directory=None):
    """Publish all available actual pair-years; retain prior files on failure.

    Calls are deliberately sequential. A refused/failed source stops the pass,
    rather than trying other URLs on the same host. Successful cached source
    bytes remain private and retain their original retrieval timestamps.
    """
    now = now or mirror.now
    if now.tzinfo is None:
        raise ValueError('Historical collection requires an aware clock.')
    output = Path(output)
    cache = Path(cache_directory) if cache_directory is not None else output.parent / '.nba-tracking-cache'
    prior = _read_document(output / MANIFEST)
    prior_sources = prior.get('sources', []) if isinstance(prior, dict) else []
    if not isinstance(prior_sources, list):
        prior_sources = []
    reports, sources, items = [], [], []
    attempted = 'historical_configuration'
    try:
        pairs, aliases = _configuration()
        named = {row[key] for row in pairs['pairs'] for key in ('offense_player', 'defense_player')}
        # Live roster expansion can add hundreds of players without NBA IDs;
        # only explicit named pairs belong to this historical relay.
        pair_players = [row for row in mirror.scope['players'] if row['player'] in named]
        if {row['player'] for row in pair_players} != named:
            raise ValueError('An authorized named-pair participant is missing from the player scope.')
        pair_scope = {**mirror.scope, 'players': pair_players}
        for year in range(FIRST_END_YEAR, mirror.end_year + 1):
            supplied = current_data if year == mirror.end_year and current_data else {}
            attempted = f'game_matchups_{year}.parquet'
            source = await _asset(mirror, MATCHUP_TAG, attempted, cache, now,
                supplied=(supplied['source_content'], supplied['source_meta']) if supplied else None,
                prior_sources=prior_sources)
            attempted = f'nba_schedule_{year}.parquet'
            schedule = await _asset(mirror, SCHEDULE_TAG, attempted, cache, now,
                supplied=(supplied['schedule_content'], supplied['schedule_meta']) if supplied else None,
                prior_sources=prior_sources)
            logs = None
            if supplied and supplied.get('team_log_content'):
                attempted = f'player_game_logs_{year}.parquet'
                logs = await _asset(mirror, 'nba_stats_player_game_logs', attempted, cache, now,
                    supplied=(supplied['team_log_content'], supplied['team_log_meta']), prior_sources=prior_sources)
            sources.extend(meta for _, meta in [source, schedule] + ([logs] if logs else []))
            for records, measured, report in _phases(year, source, schedule, logs,
                    pair_scope, pairs, aliases, now):
                reports.append(report)
                items.extend({'kind': 'measured_pair', 'data': row} for row in measured)
                items.extend({'kind': 'season_aggregate', 'data': row} for row in records)
        observed = [item['data']['date'] for item in items if item['kind'] == 'measured_pair']
        if not observed:
            raise ValueError('No authorized actual measured-pair history was returned.')
        observed_pairs = {(item['data']['offense_player'], item['data']['defense_player'])
                          for item in items if item['kind'] == 'measured_pair'}
        unobserved_pairs = [row for row in pairs['pairs']
                            if (row['offense_player'], row['defense_player']) not in observed_pairs]
        manifest = {'schema_version': 1, 'format': FORMAT, 'scope_revision': mirror.scope['revision'],
            'pair_scope_sha256': _sha(_json(pairs)), 'archive_first_season': '2017-18',
            'last_requested_season': f'{mirror.end_year - 1}-{str(mirror.end_year)[-2:]}',
            'first_observed_game_date': min(observed), 'last_observed_game_date': max(observed),
            'measured_pair_games': len(observed),
            'authorized_named_pairs': len(pairs['pairs']), 'observed_named_pairs': len(observed_pairs),
            'unobserved_named_pairs': unobserved_pairs,
            'season_aggregate_records': sum(item['kind'] == 'season_aggregate' for item in items),
            'sources': sources, 'seasons': reports,
            'coverage_claim': 'Actual named pair games present in public NBA matchup extracts, from 2017-18 onward; complete lifetime coverage is not asserted.',
            'phase_policy': 'Regular Season, Playoffs and Play-In remain separate; preseason, All-Star and Cup final samples are excluded.',
            'observation_identity': 'NBA game ID + offense NBA person ID + defense NBA person ID; source timestamps do not create another observation.'}
        parts = _publish(output, manifest, items)
        return {'phase': 'updated', 'manifest': MANIFEST, 'parts': len(parts),
                'measured_pair_games': len(observed), 'season_aggregate_records': manifest['season_aggregate_records'],
                'first_observed_game_date': min(observed), 'last_observed_game_date': max(observed)}
    except Exception as error:
        return {'phase': 'unavailable', 'error_type': type(error).__name__, 'failed_asset': attempted,
                'previous_publication_retained': (output / MANIFEST).exists(),
                'detail': str(error)[:250] if isinstance(error, ValueError) else 'Public source could not be retrieved or validated.'}
