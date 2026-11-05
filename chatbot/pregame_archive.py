"""Append-only research snapshots for prospective evaluation.

A saved screenshot is an unconfirmed user-supplied offer, and an independent
projection is not an offer. Capturing either does not place an entry or establish
a betting advantage. Capture time comes from this service; a report's own older
``generated_at`` can never make a late capture into a pregame observation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os

UTC = dt.timezone.utc
ARCHIVE_VERSION = 1
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
EXPORT_TABLES = ('research_runs', 'research_candidates')
RUN_COLUMNS = (
    'id', 'owner_key', 'kind', 'captured_at', 'generated_at', 'board_id',
    'model_versions_json', 'source_ref', 'offer_kind', 'timing_status',
    'payload_sha256', 'payload_json',
)
CANDIDATE_COLUMNS = (
    'run_id', 'ordinal', 'player_id', 'player', 'game_id', 'game_date', 'team',
    'stat', 'line', 'side', 'disposition', 'reason', 'reference_projection',
    'model_version', 'tipoff', 'timing_status', 'payload_json',
)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY, owner_key TEXT NOT NULL, kind TEXT NOT NULL,
    captured_at TEXT NOT NULL, generated_at TEXT, board_id TEXT,
    model_versions_json TEXT NOT NULL, source_ref TEXT, offer_kind TEXT NOT NULL,
    timing_status TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_owner_time ON research_runs(owner_key,captured_at);
CREATE TABLE IF NOT EXISTS research_candidates (
    run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, player_id TEXT, player TEXT,
    game_id TEXT, game_date TEXT, team TEXT, stat TEXT, line REAL, side TEXT,
    disposition TEXT NOT NULL, reason TEXT, reference_projection REAL,
    model_version TEXT, tipoff TEXT, timing_status TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id,ordinal), FOREIGN KEY(run_id) REFERENCES research_runs(id)
);
CREATE INDEX IF NOT EXISTS research_candidate_event ON research_candidates(game_id,player_id,stat);
CREATE TRIGGER IF NOT EXISTS research_runs_no_update BEFORE UPDATE ON research_runs
BEGIN SELECT RAISE(ABORT,'Research snapshots are immutable.'); END;
CREATE TRIGGER IF NOT EXISTS research_runs_no_delete BEFORE DELETE ON research_runs
BEGIN SELECT RAISE(ABORT,'Research snapshots are immutable.'); END;
CREATE TRIGGER IF NOT EXISTS research_candidates_no_update BEFORE UPDATE ON research_candidates
BEGIN SELECT RAISE(ABORT,'Research snapshots are immutable.'); END;
CREATE TRIGGER IF NOT EXISTS research_candidates_no_delete BEFORE DELETE ON research_candidates
BEGIN SELECT RAISE(ABORT,'Research snapshots are immutable.'); END;
'''


def initialize_tables(store):
    """Add archive tables without modifying observations, entries or reports."""
    with store.connect() as db:
        db.executescript(SCHEMA)


def _json(value):
    # Reject non-finite numbers and arbitrary objects rather than silently
    # changing the evidence through default=str or JSON's NaN extension.
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _instant(value):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('A scoped research owner key is required.')
    return value.strip()


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _leg(item):
    value = item.get('leg')
    return value if isinstance(value, dict) else item


def _key(item):
    leg = _leg(item)
    # Use saved identities exactly. Normalizing names across ambiguous aliases
    # here would risk joining two distinct offers or players.
    return tuple(leg.get(k) for k in ('player', 'stat', 'line', 'side', 'date', 'offer_variant'))


def _candidates(report, source_report, kind):
    if kind == 'independent':
        values = report.get('evaluated_candidates')
        if isinstance(values, list):
            return [dict(item) for item in values if isinstance(item, dict)]
        # Compatibility with older callers: preserve everything they supplied,
        # and explicitly describe the reduced coverage in the run payload.
        return [dict(item, status='selected') for item in report.get('picks', [])] + [
            dict(item, status='pass') for item in report.get('held', [])]
    chosen = {_key(item): item for item in report.get('shortlist', [])}
    skipped = report.get('skipped', [])
    values = source_report.get('legs', []) if source_report else report.get('legs', [])
    if not values:
        values = report.get('shortlist', []) + skipped
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        key = _key(item)
        selected = chosen.get(key)
        # Shortlist derives dates/offer variants from validated saved context.
        # Match only the exact player/stat/line/side when those fields were
        # absent upstream; never cross a differing known date or offer variant.
        if selected is None:
            matches = [row for row in chosen.values() if _same_offer(item, row)]
            selected = matches[0] if len(matches) == 1 else None
        matches = [row for row in skipped if _same_offer(item, row)]
        rejection = matches[0] if len(matches) == 1 else None
        if selected is not None:
            item = {**item, **selected, 'status': 'selected'}
        elif rejection is not None:
            item.update(status='pass', reason=rejection.get('reason'), rejection=rejection)
        elif item.get('eligible_for_ranking') is False or (item.get('projection') or {}).get('available') is False:
            item.update(status='pass', reason=item.get('reason') or (item.get('projection') or {}).get('reason'))
        elif report.get('shortlist') is not None:
            item.update(status='candidate_not_selected', reason='Not selected after lineup constraints and ranking.')
        elif item.get('eligible_for_ranking'):
            item['status'] = 'candidate'
        else:
            item['status'] = 'pass'
        result.append(item)
    return result


def _same_offer(first, second):
    a, b = _leg(first), _leg(second)
    for key in ('player', 'stat', 'line', 'side'):
        if a.get(key) != b.get(key):
            return False
    for key in ('date', 'game_id', 'offer_variant'):
        if a.get(key) is not None and b.get(key) is not None and a[key] != b[key]:
            return False
    return True


def _event(db, item, report):
    leg = _leg(item)
    event_id = leg.get('game_id') or (item.get('context') or {}).get('game_id')
    event = report.get('game') or {}
    if event_id is None and event:
        event_id = event.get('id')
    saved = {}
    if event_id:
        # Match existing event identity only. A team/date lookup could select a
        # rescheduled event, so unresolved identities retain unknown timing.
        for table in ('season_schedule', 'games'):
            row = db.execute(f'SELECT * FROM {table} WHERE id=?', (event_id,)).fetchone()
            if row:
                saved.update({key: value for key, value in dict(row).items() if value is not None})
    if event.get('id') == event_id:
        # The report is the as-used event. Prefer it to a subsequently corrected
        # store row, while retaining both in the candidate snapshot.
        used = {**saved, **{key: value for key, value in event.items() if value is not None}}
    else:
        used = saved
    context = item.get('context') or {}
    calendar = (item.get('projection') or {}).get('calendar') or {}
    tipoff = used.get('start_time') or context.get('start_time') or context.get('tipoff') or calendar.get('tipoff')
    return event_id, tipoff, saved


def _timing(tipoff, captured):
    tip = _instant(tipoff)
    if not tip:
        return 'timing_unknown'
    return 'before_saved_tipoff' if captured < tip else 'at_or_after_saved_tipoff'


def _candidate_row(db, run_id, ordinal, item, report, captured):
    leg = _leg(item)
    projection = item.get('projection') or {}
    event_id, tipoff, saved = _event(db, item, report)
    status = item.get('status') or item.get('disposition') or 'candidate'
    if status not in {'selected', 'candidate', 'candidate_not_selected', 'pass'}:
        status = 'pass'
    payload = {'candidate': item, 'saved_event_at_capture': saved,
               'availability_basis': 'Reported source evidence only; absence of a report does not confirm availability.',
               'assignment_basis': 'Preserve the supplied tentative/verified labels; coappearance is not guarding.'}
    return (
        run_id, ordinal, leg.get('player_id'), leg.get('player'), event_id,
        leg.get('date') or report.get('game_date'), leg.get('team'), leg.get('stat'),
        _number(leg.get('line')), leg.get('side'), status, item.get('reason'),
        _number(projection.get('reference_projection')), projection.get('method'),
        _instant(tipoff).isoformat() if _instant(tipoff) else None,
        _timing(tipoff, captured), _json(payload),
    )


def record_research(store, report, *, owner_key, kind, source_report=None,
                    source_refs=None, payout_terms=None, source_ref=None, now=None):
    """Freeze exactly the supplied research and offer context; return archive ID.

    Call after creating the final shortlist, before delivering it. ``owner_key``
    should be ``telegram:<chat_id>`` or another authenticated application scope.
    All candidates and passes are retained when callers provide them. Repeating
    identical inputs reuses the earliest capture instead of rewriting it.
    ``now`` is a trusted server clock override for tests, never a user timestamp.
    """
    owner_key = _owner(owner_key)
    if kind not in {'board_analysis', 'board_shortlist', 'independent'}:
        raise ValueError('Unknown research archive kind.')
    if not isinstance(report, dict) or (source_report is not None and not isinstance(source_report, dict)):
        raise ValueError('Research reports must be objects.')
    captured = _instant(now) if now is not None else dt.datetime.now(UTC)
    if captured is None:
        raise ValueError('Capture time needs an explicit timezone.')
    # Deep-copy and validate before any archive write. Input dictionaries remain
    # usable by the chat flow and cannot mutate an already persisted report.
    report = json.loads(_json(report))
    source_report = json.loads(_json(source_report)) if source_report is not None else None
    board_id = report.get('board_id') or (source_report or {}).get('board_id')
    board = store.get_board(board_id) if board_id else None
    source_ref = source_ref if source_ref is not None else os.getenv('SOURCE_REF') or None
    candidates = _candidates(report, source_report, kind)
    versions = sorted({str(row.get('projection', {}).get('method')) for row in candidates
                       if isinstance(row.get('projection'), dict) and row['projection'].get('method')})
    for value in (report.get('engine_version'), (source_report or {}).get('engine_version')):
        if value and value not in versions:
            versions.append(str(value))
    payload = {
        'archive_version': ARCHIVE_VERSION, 'report': report, 'source_report': source_report,
        'board_snapshot': board, 'source_refs': source_refs or [], 'payout_terms': payout_terms,
        'source_ref': source_ref,
        'candidate_coverage': ('all_evaluated_markets' if kind == 'independent' and 'evaluated_candidates' in report
                               else 'all_supplied_board_legs' if source_report is not None or report.get('legs')
                               else 'supplied_selected_and_held_only'),
        'limits': [
            'Capture time is distinct from source publication, source coverage and report generation time.',
            'Before a saved tipoff does not independently verify the schedule, availability, offer or payout.',
            'Unconfirmed OCR and manually supplied offers retain their verification flags.',
            'Independent target thresholds are simulations, not historical PrizePicks offers.',
            'No missing availability, verified defender, expected role or payout is inferred.',
        ],
    }
    encoded = _json(payload)
    if len(encoded.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ValueError('Research archive exceeds its supported size; no partial snapshot was saved.')
    content_hash = hashlib.sha256(encoded.encode()).hexdigest()
    run_id = hashlib.sha256(_json([owner_key, kind, content_hash]).encode()).hexdigest()
    initialize_tables(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT id FROM research_runs WHERE id=? AND owner_key=?', (run_id, owner_key)).fetchone()
        if old:
            return old['id']
        rows = [_candidate_row(db, run_id, n, item, report, captured)
                for n, item in enumerate(candidates)]
        stages = {row[-2] for row in rows}
        timing = next(iter(stages)) if len(stages) == 1 else 'mixed' if stages else 'timing_unknown'
        offer_kind = 'conditional_targets_not_offers' if kind == 'independent' else 'user_supplied_offers_unverified'
        db.execute('INSERT INTO research_runs (' + ','.join(RUN_COLUMNS) + ') VALUES (' +
                   ','.join('?' for _ in RUN_COLUMNS) + ')',
                   (run_id, owner_key, kind, captured.isoformat(), report.get('generated_at'), board_id,
                    _json(sorted(versions)), source_ref, offer_kind, timing, content_hash, encoded))
        db.executemany('INSERT INTO research_candidates (' + ','.join(CANDIDATE_COLUMNS) + ') VALUES (' +
                       ','.join('?' for _ in CANDIDATE_COLUMNS) + ')', rows)
    return run_id


def get_research(store, run_id, *, owner_key):
    """Read one archived run only within its originating authenticated scope."""
    owner_key = _owner(owner_key)
    initialize_tables(store)
    with store.connect() as db:
        row = db.execute('SELECT * FROM research_runs WHERE id=? AND owner_key=?', (str(run_id), owner_key)).fetchone()
        if row is None:
            raise ValueError('Research snapshot not found.')
        result = dict(row)
        result['payload'] = json.loads(result.pop('payload_json'))
        result['model_versions'] = json.loads(result.pop('model_versions_json'))
        result['candidates'] = []
        for candidate in db.execute('SELECT * FROM research_candidates WHERE run_id=? ORDER BY ordinal', (run_id,)):
            item = dict(candidate)
            item['payload'] = json.loads(item.pop('payload_json'))
            result['candidates'].append(item)
        return result
