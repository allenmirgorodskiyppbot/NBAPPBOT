"""Complete compatible historical game metadata from verified career bundles.

This is deliberately separate from the insert-only statistics importer. It can
make an existing unknown game final only when a fully validated source box score
identifies that same event. Player observations, entries and freshness checks
are never rewritten. Call after the bundle download/import and its backup.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from feeds import canonical_game, canonical_team
from history_backfill import history_exclusion
from master import GAME_FIELDS, encoded

MAX_MANIFEST_BYTES = 4_000_000
MAX_PART_BYTES = 32_000_000
MAX_RECORD_BYTES = 100_000
MAX_RECORDS = 2_000_000


def _instant(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _source_known(value):
    parsed = urlsplit(str(value or ''))
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return False
    return ((parsed.hostname == 'github.com' and parsed.path.startswith(
        '/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/'))
        or (parsed.hostname == 'site.api.espn.com' and parsed.path ==
            '/apis/site/v2/sports/basketball/nba/summary'))


def _verified_game(row, through, current):
    """Reject missing proof; malformed asserted proof cannot partly update a DB."""
    if row.get('upstream_full_game_validated') is not True:
        return False
    if (not _source_known(row.get('source')) or
            not re.fullmatch(r'[0-9a-f]{64}', str(row.get('source_content_sha256') or ''))):
        return False
    published = _instant(row.get('source_published_at'))
    if published is None:
        return False
    if published > current:
        raise ValueError('Historical game source publication is in the future.')
    day = str(row.get('date') or '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('Historical game date must be YYYY-MM-DD.')
    parsed_day = dt.date.fromisoformat(day)
    if parsed_day > current.astimezone(ZoneInfo('America/New_York')).date() or day > through:
        raise ValueError('Historical game is outside verified source coverage.')
    if row.get('status') != 'final' or history_exclusion(row):
        return False
    home, away = canonical_team(row.get('home_team')), canonical_team(row.get('away_team'))
    if row.get('id') != canonical_game(day, home, away):
        raise ValueError('Historical game canonical identity conflicts with its participants.')
    if not re.fullmatch(r'\d{1,15}', str(row.get('provider_game_id') or '')):
        raise ValueError('Historical game requires a source provider identity.')
    for field in ('home_score', 'away_score'):
        value = row.get(field)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or value > 400 or int(value) != value):
            raise ValueError('Historical final score must be a sensible nonnegative integer.')
    if row['home_score'] == row['away_score']:
        raise ValueError('A completed NBA game cannot have a tied score.')
    if row.get('start_time') and _instant(row['start_time']) is None:
        raise ValueError('Historical tipoff needs an explicit timezone.')
    return True


def _games(path, current):
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Career manifest is too large.')
    manifest_bytes = path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or
            manifest.get('format') != 'nba-history-jsonl-gzip-v1'):
        raise ValueError('Unsupported career manifest.')
    through = dt.date.fromisoformat(manifest['source_data_through_date']).isoformat()
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
        raise ValueError('Invalid career part inventory.')
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    verified, names = [], set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('Invalid career part metadata.')
        name = part.get('file')
        size, records = part.get('bytes'), part.get('records')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.jsonl\.gz', name)
                or name in names or type(size) is not int or not 0 < size <= MAX_PART_BYTES
                or type(records) is not int or not 0 < records <= MAX_RECORDS
                or not re.fullmatch(r'[0-9a-f]{64}', str(part.get('sha256') or ''))):
            raise ValueError('Invalid career part metadata.')
        names.add(name)
        target = (path.parent / name).resolve()
        if target.parent != path.parent or target.stat().st_size != size:
            raise ValueError('Career part path or size is invalid.')
        blob = target.read_bytes()
        if hashlib.sha256(blob).hexdigest() != part['sha256']:
            raise ValueError('Career part checksum mismatch.')
        verified.append((part, target))
    games, totals, seen = {}, Counter(), set()
    for part, target in verified:
        count = 0
        with gzip.open(target, 'rb') as stream:
            while line := stream.readline(MAX_RECORD_BYTES + 1):
                if len(line) > MAX_RECORD_BYTES:
                    raise ValueError('Oversized career record.')
                count += 1
                totals['records_read'] += 1
                if count > part['records'] or totals['records_read'] > MAX_RECORDS:
                    raise ValueError('Career part exceeds its record inventory.')
                event = json.loads(line)
                if (not isinstance(event, dict) or event.get('kind') not in {'game', 'player_game'}
                        or not isinstance(event.get('data'), dict)):
                    raise ValueError('Malformed career record.')
                if event['kind'] != 'game':
                    continue
                row = event['data']
                identity = row.get('id')
                if not isinstance(identity, str) or identity in seen:
                    raise ValueError('Duplicate or missing career game identity.')
                seen.add(identity)
                totals['source_games'] += 1
                if _verified_game(row, through, current):
                    games[identity] = row
                else:
                    totals['source_games_without_required_proof'] += 1
        if count != part['records']:
            raise ValueError('Career part record count mismatch.')
    return digest, games, totals


def _compatible(old, incoming):
    for field in ('date', 'home_team', 'away_team', 'home_score', 'away_score'):
        known = old.get(field)
        if known not in (None, '') and known != incoming.get(field):
            return False
    if old.get('start_time') and incoming.get('start_time'):
        if _instant(old['start_time']) != _instant(incoming['start_time']):
            return False
    old_provider = str(old.get('provider_game_id') or '')
    new_provider = str(incoming['provider_game_id'])
    if old_provider and old_provider != new_provider:
        # Original workbook rows used NBA event IDs in this shared legacy field.
        # A verified ESPN identifier does not supersede that known NBA identity;
        # preserve it and record the ESPN identifier in the repair evidence.
        nba_legacy = (str(old.get('source') or '').startswith('workbook:') and
                      re.fullmatch(r'00[24]\d{7}', old_provider))
        if not nba_legacy:
            return False
    return True


def complete_verified_game_metadata(store, manifest_path, *, now=None):
    """Complete unknown historical games without altering valid existing facts.

    Every part is checked before the transaction. Re-read live rows inside that
    transaction so a concurrent collector's final/live status always wins.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Metadata repair time requires an explicit timezone.')
    path = Path(manifest_path).resolve()
    digest, games, counts = _games(path, current)
    counts['verified_source_games'] = len(games)
    backup = None
    # A previous install may have completed before this metadata repair existed.
    # Do not rely on its older backup, because live entries may have arrived
    # since then. Only create a fresh backup when a repair can actually occur.
    with store.connect() as db:
        possible = any((row := db.execute('SELECT * FROM games WHERE id=?', (identity,)).fetchone())
                       and str(row['status'] or '').strip().lower() in {'', 'unknown'}
                       and _compatible(dict(row), incoming)
                       for identity, incoming in games.items())
    if possible:
        destination = Path(store.path).parent / 'backups' / ('before-career-metadata-' + digest[:16] + '.sqlite')
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = store.backup(str(destination))
    repaired, conflicts = [], []
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for identity, incoming in games.items():
            found = db.execute('SELECT * FROM games WHERE id=?', (identity,)).fetchone()
            if found is None:
                counts['games_not_saved'] += 1
                continue
            old = dict(found)
            if str(old.get('status') or '').strip().lower() not in {'', 'unknown'}:
                counts['games_already_resolved'] += 1
                continue
            if not _compatible(old, incoming):
                conflicts.append(identity)
                continue
            # A saved player's contradictory team/date cannot be made eligible
            # merely by updating its enclosing game metadata.
            invalid = db.execute('SELECT 1 FROM player_games WHERE game_id=? AND '
                '(date IS NULL OR date!=? OR team IS NULL OR opponent IS NULL OR '
                'team NOT IN (?,?) OR opponent NOT IN (?,?) OR team=opponent) LIMIT 1',
                (identity, incoming['date'], incoming['home_team'], incoming['away_team'],
                 incoming['home_team'], incoming['away_team'])).fetchone()
            if invalid:
                conflicts.append(identity)
                continue
            after = {field: old.get(field) if old.get(field) not in (None, '') else incoming.get(field)
                     for field in GAME_FIELDS}
            after['status'] = 'final'
            # The new source verifies the completed metadata. Its original
            # workbook source remains verbatim in the immutable repair record.
            after['source'] = incoming['source']
            stamp = current.isoformat()
            db.execute('''CREATE TABLE IF NOT EXISTS career_game_metadata_repairs (
                game_id TEXT PRIMARY KEY, repaired_at TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL, before_json TEXT NOT NULL,
                after_json TEXT NOT NULL, source_evidence_json TEXT NOT NULL)''')
            store.upsert_game({**after, 'updated_at': stamp}, db=db)
            db.execute('INSERT INTO career_game_metadata_repairs VALUES (?,?,?,?,?,?)',
                (identity, stamp, digest, encoded(old), encoded({**after, 'updated_at': stamp}), encoded(incoming)))
            store._snapshot(db, 'career_game_metadata', identity,
                            {'source': incoming['source'], 'manifest_sha256': digest,
                             'before': old, 'after': after, 'verified_source': incoming})
            repaired.append(identity)
    return {**dict(counts), 'games_completed': len(repaired), 'games_conflicted': len(conflicts),
            'repaired_game_ids': repaired, 'conflicting_game_ids': conflicts,
            'backup': backup,
            'manifest_sha256': digest,
            'scope': 'Verified historical game metadata only; current readiness and player statistics are unchanged.'}
