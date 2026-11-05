"""Dated availability evidence, separate from game statistics and lineup forecasts.

An official report's explicit status is retained. An absent player, unsent team
report, date-only extract, or ESPN story never becomes confirmed availability.
PyArrow is used only by the public-data collector, not by the running chatbot.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import gzip
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from feeds import TEAM_WORDS, canonical_game, canonical_player, canonical_team

EASTERN = ZoneInfo('America/New_York')
MAX_RECORDS = 100_000
MAX_DOCUMENT_BYTES = 64_000_000
MAX_COMPRESSED_BYTES = 8_000_000
RELAY_NAME = 'structured_availability.json.gz'
OFFICIAL_STATUSES = {'Out', 'Questionable', 'Doubtful', 'Probable', 'Available'}
OFFICIAL_REPORT_INDEX = 'https://official.nba.com/nba-injury-report-2026-27-season/'
DOCUMENTED_ET_PRECISION = 'source_clock_with_documented_timezone'
VERIFIED_CLOCK_PRECISIONS = {'exact_source_timestamp', DOCUMENTED_ET_PRECISION}
# The official index labels links ET. Two independently read original PDFs
# match the mirrored clock, player, matchup and status, across the DST change.
OFFICIAL_CLOCK_VALIDATION = {
    'time_basis': 'official_publisher_ET_convention', 'timezone': 'America/New_York',
    'index_url': OFFICIAL_REPORT_INDEX,
    'matched_pdf_examples': [
        {'url': 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-20_09_15AM.pdf',
         'header': 'Injury Report: 10/20/26 09:15 AM',
         'matched_rows': [{'player': 'Devin Carter', 'status': 'Out', 'matchup': 'BOS@DET'},
                          {'player': 'Elijah Harkless', 'status': 'Available', 'matchup': 'BOS@DET'}]},
        {'url': 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-03_05_00PM.pdf',
         'header': 'Injury Report: 11/03/26 05:00 PM',
         'matched_rows': [{'player': 'RayJ Dennis', 'status': 'Questionable'},
                          {'player': 'Keshon Gilbert', 'status': 'Doubtful'},
                          {'player': 'Josh Hart', 'status': 'Questionable', 'matchup': 'CHI@NYK'}]}]}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False)


def _instant(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _day(value):
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return dt.date.fromisoformat(str(value)[:10]).isoformat()


def _team(value):
    direct = canonical_team(value)
    if direct in TEAM_WORDS:
        return direct
    text = ' '.join(re.sub('[^a-z0-9]', ' ', str(value or '').lower()).split())
    choices = [code for code, name in TEAM_WORDS.items()
               if text == name or text.endswith(' ' + name)]
    return choices[0] if len(choices) == 1 else None


def _player(value):
    text = str(value or '').strip()
    if ',' in text:
        last, first = text.split(',', 1)
        return (first.strip() + ' ' + last.strip()).strip()
    return text


def _numeric_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    text = str(value)
    return text if text.isdigit() and int(text) > 0 else None


def _source_allowed(url, kind):
    parsed = urlsplit(str(url or ''))
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return False
    if kind == 'official_pdf':
        return (parsed.hostname == 'ak-static.cms.nba.com' and
                re.fullmatch(r'/referee/injury/Injury-Report_\d{4}-\d{2}-\d{2}_\d{2}_\d{2}(?:AM|PM)\.pdf', parsed.path) is not None)
    tag = {'official_extract': 'nba_stats_injury_report', 'espn_injury_snapshot': 'espn_nba_injuries'}.get(kind)
    return bool(tag and parsed.hostname == 'github.com' and parsed.path.startswith(
        '/sportsdataverse/sportsdataverse-data/releases/download/' + tag + '/'))


def _base(kind, source_url, published_at, source_sha256, now):
    if not _source_allowed(source_url, kind):
        raise ValueError('Unsupported availability publisher.')
    if published_at is not None and _instant(published_at) is None:
        raise ValueError('Availability publication time needs an explicit timezone.')
    if source_sha256 is not None and not re.fullmatch('[0-9a-f]{64}', str(source_sha256)):
        raise ValueError('Availability content digest is invalid.')
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Availability collection time needs an explicit timezone.')
    if published_at and _instant(published_at) > now + dt.timedelta(minutes=5):
        raise ValueError('Availability publication time is in the future.')
    return {'source_kind': kind, 'official_report': kind in {'official_extract', 'official_pdf'},
            'source_url': source_url, 'source_published_at': published_at,
            'source_content_sha256': source_sha256, 'retrieved_at': now.isoformat(),
            'confirmed_starter': None, 'expected_minutes': None,
            'minutes_restriction': None, 'minutes_restriction_confirmed': None,
            'coverage_note': 'An absent player or unsent team report does not establish health, a starting role or unrestricted minutes.'}


def _identity(record):
    # Re-publishing the same source does not duplicate the same dated evidence.
    facts = {key: value for key, value in record.items() if key not in
             {'record_id', 'retrieved_at', 'source_published_at', 'source_content_sha256', 'source_text_sha256'}}
    return hashlib.sha256(_json(facts).encode()).hexdigest()


def normalize_nba_injuries(rows, source_url, published_at, source_sha256, now=None):
    """Use documented publisher ET for actual clocks; date-only stays unknown."""
    base = _base('official_extract', source_url, published_at, source_sha256, now)
    result = {}
    for raw in rows:
        day, report_date = _day(raw.get('game_date')), _day(raw.get('report_datetime'))
        if report_date > _instant(base['retrieved_at']).astimezone(EASTERN).date().isoformat():
            raise ValueError('Availability report date is in the future.')
        home, away, team = _team(raw.get('home_team_abbr')), _team(raw.get('away_team_abbr')), _team(raw.get('team'))
        if not home or not away or home == away or team not in {home, away}:
            raise ValueError('Official injury row has unresolved game/team identity.')
        name = _player(raw.get('report_player_name'))
        status = str(raw.get('current_status') or '').strip()
        reason = str(raw.get('reason') or '').strip()
        unsubmitted = not name and reason.upper() == 'NOT YET SUBMITTED'
        if not unsubmitted and (not name or status not in OFFICIAL_STATUSES):
            raise ValueError('Official injury row has unresolved player/status.')
        observed_at = _instant(raw.get('report_datetime'))
        # Midnight is indistinguishable from a date stripped of its clock.
        # Intraday clocks follow the official publisher's documented ET basis.
        exact_time = observed_at.isoformat() if observed_at else None
        naive = dt.datetime.fromisoformat(str(raw.get('report_datetime')))
        documented_et = not observed_at and naive.time() != dt.time()
        if documented_et:
            exact_time = naive.replace(tzinfo=EASTERN).isoformat()
        if exact_time and _instant(exact_time) > _instant(base['retrieved_at']) + dt.timedelta(minutes=5):
            raise ValueError('Availability report time is in the future.')
        precision = ('exact_source_timestamp' if exact_time else
                     'source_local_clock_timezone_unspecified' if naive.time() != dt.time() else
                     'calendar_date_only')
        if documented_et:
            precision = DOCUMENTED_ET_PRECISION
        record = {**base, 'player': name or None,
            'player_id': canonical_player(name) if name else None,
            'nba_player_id': _numeric_id(raw.get('person_id')) if raw.get('match_method') == 'exact' else None,
            'espn_player_id': None, 'identity_match_method': raw.get('match_method'),
            'raw_player_name': raw.get('report_player_name'), 'team': team,
            'opponent': away if team == home else home,
            'game_id': canonical_game(day, home, away), 'nba_game_id': str(raw.get('game_id') or '') or None,
            'game_date': day, 'game_time_text': raw.get('game_time'),
            'report_date': report_date, 'report_at': exact_time,
            'report_time_precision': precision,
            'report_datetime_raw': str(raw.get('report_datetime')),
            'time_basis': 'official_publisher_ET_convention' if documented_et else None,
            'report_timezone': 'America/New_York' if documented_et else None,
            'time_basis_source_url': OFFICIAL_REPORT_INDEX if documented_et else None,
            'status': 'Not Yet Submitted' if unsubmitted else status,
            'reason': reason or None, 'team_report_submitted': not unsubmitted,
            'injury_type': None, 'injury_location': None, 'injury_side': None,
            'expected_return_date': None, 'season': raw.get('season')}
        record['record_id'] = _identity(record)
        result[record['record_id']] = record
    return list(result.values())


def normalize_espn_injuries(rows, source_url, published_at, source_sha256, now=None):
    """Keep third-party injury snapshots separate from official designations."""
    base = _base('espn_injury_snapshot', source_url, published_at, source_sha256, now)
    result = {}
    for raw in rows:
        if str(raw.get('league') or '').lower() != 'nba':
            raise ValueError('Injury snapshot is not NBA data.')
        report_date = _day(raw.get('as_of_date'))
        player, team = str(raw.get('athlete_display_name') or '').strip(), _team(raw.get('team_display_name'))
        status = str(raw.get('status') or '').strip()
        if not player or not team or not status or not _numeric_id(raw.get('athlete_id')):
            raise ValueError('ESPN injury identity/status is unresolved.')
        injury_at = _instant(raw.get('injury_date'))
        if raw.get('injury_date') and not injury_at:
            raise ValueError('ESPN injury update time is unresolved.')
        if (report_date > _instant(base['retrieved_at']).astimezone(EASTERN).date().isoformat()
                or injury_at and injury_at > _instant(base['retrieved_at']) + dt.timedelta(minutes=5)):
            raise ValueError('Availability source clock is in the future.')
        record = {**base, 'player': player, 'player_id': canonical_player(player),
            'nba_player_id': None, 'espn_player_id': _numeric_id(raw.get('athlete_id')),
            'injury_id': _numeric_id(raw.get('injury_id')), 'team': team,
            'opponent': None, 'game_id': None, 'nba_game_id': None, 'game_date': None,
            'report_date': report_date, 'report_at': injury_at.isoformat() if injury_at else None,
            'report_time_precision': 'injury_item_update_not_game_report' if injury_at else 'calendar_date_only',
            'status': status, 'status_code': raw.get('type_name'),
            'reason': raw.get('short_comment') or raw.get('long_comment'),
            'detail': raw.get('long_comment'), 'injury_type': raw.get('detail_type'),
            'injury_location': raw.get('detail_location'), 'injury_side': raw.get('detail_side'),
            'expected_return_date': raw.get('detail_return_date'), 'team_report_submitted': None,
            'season': raw.get('season')}
        record['record_id'] = _identity(record)
        result[record['record_id']] = record
    return list(result.values())


def normalize_official_pdf(rows, *, source_url, report_at, text_sha256, now=None, pdf_sha256=None):
    """Accept verified PDF text rows while labeling text digest versus PDF bytes."""
    report = _instant(report_at)
    if not report or not re.fullmatch('[0-9a-f]{64}', str(text_sha256 or '')):
        raise ValueError('Official report text needs its actual clock and text digest.')
    base = _base('official_pdf', source_url, report_at, pdf_sha256, now)
    if report > _instant(base['retrieved_at']) + dt.timedelta(minutes=5):
        raise ValueError('Availability report time is in the future.')
    match = re.search(r'Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf$', source_url)
    expected = dt.datetime.strptime(' '.join(match.groups()), '%Y-%m-%d %I %M %p').replace(tzinfo=EASTERN)
    if expected != report.astimezone(EASTERN):
        raise ValueError('Official PDF filename and reported clock disagree.')
    result = []
    for row in rows:
        game_date = _day(row.get('game_date'))
        away, home = _team(row.get('away_team')), _team(row.get('home_team'))
        team = _team(row.get('team'))
        player = _player(row.get('player'))
        status = row.get('status')
        if not away or not home or home == away or team not in {home, away}:
            raise ValueError('Official PDF row has unresolved participants.')
        if status == 'Not Yet Submitted':
            if player:
                raise ValueError('Unsubmitted team reports cannot assert a player status.')
        elif not player or status not in OFFICIAL_STATUSES:
            raise ValueError('Official PDF status is unresolved.')
        record = {**base, 'source_text_sha256': text_sha256,
            'evidence_basis': ('Original official PDF bytes and extracted text are separately hashed.' if pdf_sha256
                               else 'Official PDF text extraction; raw PDF content digest is unavailable.'),
            'player': player or None, 'player_id': canonical_player(player) if player else None,
            'nba_player_id': None, 'espn_player_id': None, 'team': team,
            'opponent': away if team == home else home, 'game_id': canonical_game(game_date, home, away),
            'game_date': game_date, 'game_time_text': row.get('game_time_text'),
            'report_date': report.astimezone(EASTERN).date().isoformat(), 'report_at': report.isoformat(),
            'report_time_precision': 'exact_source_timestamp', 'status': status,
            'reason': row.get('reason'), 'team_report_submitted': status != 'Not Yet Submitted',
            'injury_type': None, 'injury_location': None, 'injury_side': None,
            'expected_return_date': None}
        record['record_id'] = _identity(record)
        result.append(record)
    return result


def build_availability_document(records, *, now=None, coverage=None):
    records = list({row['record_id']: row for row in records}.values())
    if len(records) > MAX_RECORDS:
        raise ValueError('Availability archive exceeds its record bound.')
    now = now or dt.datetime.now(dt.timezone.utc)
    return {'schema_version': 1, 'kind': 'structured_availability', 'generated_at': now.isoformat(),
            'records': records, 'coverage': coverage or [],
            'official_clock_validation': OFFICIAL_CLOCK_VALIDATION,
            'limits': ['Availability evidence is separate from confirmed starting lineups and expected minutes.',
                       'Date-only extracts cannot establish an exact pregame publication time.',
                       'Unsubmitted reports and absent players are unknown, not available.',
                       'ESPN snapshots are third-party reports, not official NBA designations.']}


def load_availability_document(payload):
    """Decode a bounded compressed relay; JSON metadata never invokes code."""
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('Availability relay must be bytes.')
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise ValueError('Availability relay exceeds its compressed bound.')
    if payload[:2] == b'\x1f\x8b':
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as source:
                content = source.read(MAX_DOCUMENT_BYTES + 1)
        except (OSError, EOFError) as error:
            raise ValueError('Availability gzip is invalid.') from error
    else:
        content = bytes(payload)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError('Availability relay exceeds its expanded bound.')
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Availability relay contains invalid JSON.') from error
    if not isinstance(document, dict) or document.get('kind') != 'structured_availability':
        raise ValueError('Availability relay has an unexpected population.')
    return document


def import_availability_document(store, document, *, now=None):
    """Append checked evidence; never change stats, rosters, entries or readiness."""
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or document.get('kind') != 'structured_availability'
            or not isinstance(document.get('records'), list)
            or len(document['records']) > MAX_RECORDS
            or len(_json(document).encode()) > MAX_DOCUMENT_BYTES):
        raise ValueError('Unsupported or oversized structured availability document.')
    now = now or dt.datetime.now(dt.timezone.utc)
    generated = _instant(document.get('generated_at'))
    if not generated or generated > now + dt.timedelta(minutes=5):
        raise ValueError('Availability document generation clock is invalid or in the future.')
    checked = []
    for row in document['records']:
        if not isinstance(row, dict) or row.get('record_id') != _identity(row):
            raise ValueError('Availability record digest does not match its facts.')
        kind = row.get('source_kind')
        if not _source_allowed(row.get('source_url'), kind):
            raise ValueError('Availability record has an unsupported publisher.')
        if row.get('official_report') is not (kind in {'official_extract', 'official_pdf'}):
            raise ValueError('Availability authority cannot be promoted.')
        if row.get('official_report') and row.get('status') not in OFFICIAL_STATUSES | {'Not Yet Submitted'}:
            raise ValueError('Unsupported official availability status.')
        if any(row.get(key) is not None for key in
               ('confirmed_starter', 'expected_minutes', 'minutes_restriction', 'minutes_restriction_confirmed')):
            raise ValueError('These availability sources do not verify lineups or projected minutes.')
        if row.get('report_at') and not _instant(row['report_at']):
            raise ValueError('Availability report clock needs a timezone.')
        retrieved = _instant(row.get('retrieved_at'))
        published = _instant(row.get('source_published_at'))
        report_at = _instant(row.get('report_at'))
        if not retrieved or retrieved > now + dt.timedelta(minutes=5):
            raise ValueError('Availability retrieval clock is invalid or in the future.')
        if ((row.get('source_published_at') and not published)
                or any(stamp and stamp > retrieved + dt.timedelta(minutes=5) for stamp in (published, report_at))):
            raise ValueError('Availability source clock is invalid or in the future.')
        if row.get('report_time_precision') == DOCUMENTED_ET_PRECISION:
            raw_clock = dt.datetime.fromisoformat(str(row.get('report_datetime_raw')))
            if (kind != 'official_extract' or raw_clock.tzinfo is not None or raw_clock.time() == dt.time()
                    or row.get('time_basis') != 'official_publisher_ET_convention'
                    or row.get('report_timezone') != 'America/New_York'
                    or row.get('time_basis_source_url') != OFFICIAL_REPORT_INDEX
                    or report_at != raw_clock.replace(tzinfo=EASTERN)):
                raise ValueError('Availability publisher timezone provenance is invalid.')
        _day(row['report_date'])
        if row['report_date'] > retrieved.astimezone(EASTERN).date().isoformat():
            raise ValueError('Availability report date is in the future.')
        if row.get('official_report') and report_at and row['report_date'] != report_at.astimezone(EASTERN).date().isoformat():
            raise ValueError('Availability report date and clock disagree.')
        if row.get('game_date'):
            _day(row['game_date'])
            if row.get('team') not in TEAM_WORDS or row.get('opponent') not in TEAM_WORDS or row['team'] == row['opponent']:
                raise ValueError('Availability game participants are unresolved.')
            if row.get('game_id') != canonical_game(row['game_date'], row['team'], row['opponent']):
                raise ValueError('Availability game identity does not match its participants.')
        if row.get('status') == 'Not Yet Submitted' and (row.get('player') or row.get('player_id') or row.get('team_report_submitted')):
            raise ValueError('Unsubmitted team report cannot assert player availability.')
        if row.get('team') not in TEAM_WORDS or not str(row.get('status') or '').strip():
            raise ValueError('Availability identity/status is unresolved.')
        checked.append(row)
    added = 0
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS structured_availability (
            record_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, player_id TEXT,
            player TEXT, team TEXT NOT NULL, game_id TEXT, game_date TEXT,
            report_date TEXT NOT NULL, report_at TEXT, status TEXT NOT NULL,
            payload_json TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS structured_availability_player_game ON structured_availability(player_id,game_date)')
        db.execute('CREATE INDEX IF NOT EXISTS structured_availability_team_game_status ON structured_availability(game_date,team,status)')
        for row in checked:
            cursor = db.execute('INSERT OR IGNORE INTO structured_availability VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [row['record_id'], row['source_kind'], row.get('player_id'), row.get('player'),
                 row['team'], row.get('game_id'), row.get('game_date'), row['report_date'],
                 row.get('report_at'), row['status'], _json(row)])
            added += cursor.rowcount
    counts = Counter(row['source_kind'] for row in checked)
    return {'records_added': added, 'records_checked': len(checked), 'by_source_kind': dict(counts),
            'official_exact_clock_records': sum(row.get('official_report') and row.get('report_time_precision') in VERIFIED_CLOCK_PRECISIONS for row in checked),
            'date_only_records': sum(row.get('report_time_precision') == 'calendar_date_only' for row in checked),
            'unsubmitted_team_records': sum(row['status'] == 'Not Yet Submitted' for row in checked),
            'note': 'Source evidence archived. Current readiness, injuries of absent players, confirmed starters and expected minutes were not inferred.'}


def current_official_availability(store, player, game_date, now=None, *, max_age_seconds=6 * 3600):
    """Read an explicit fresh official designation, never infer one from absence."""
    from career_identity import resolve_career_identity
    now = now or dt.datetime.now(dt.timezone.utc)
    identity = resolve_career_identity(store, player)
    keys = identity['player_ids']
    result = {'known': False, 'player': identity['player'], 'game_date': game_date,
              'status': None, 'blocked': False, 'confirmed_starter': None,
              'expected_minutes': None, 'minutes_restriction': None,
              'reason': 'No fresh official game-specific designation with an exact report time is saved.'}
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='structured_availability'").fetchone():
            return result
        rows = [json.loads(row[0]) for row in db.execute(
            'SELECT payload_json FROM structured_availability WHERE game_date=? AND player_id IN ('
            + ','.join('?' for _ in keys) + ')', (game_date, *keys))]
    fresh = [(row, _instant(row.get('report_at'))) for row in rows if row.get('official_report')
             and row.get('report_time_precision') in VERIFIED_CLOCK_PRECISIONS]
    fresh = [(row, stamp) for row, stamp in fresh if stamp and 0 <= (now - stamp).total_seconds() <= max_age_seconds]
    if not fresh:
        return result
    latest = max(stamp for _, stamp in fresh)
    selected = [row for row, stamp in fresh if stamp == latest]
    if len({row['status'] for row in selected}) != 1:
        return {**result, 'reason': 'Saved official reports conflict at the same report time.'}
    record = selected[0]
    with store.connect() as db:
        unsent = [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM structured_availability WHERE game_date=? AND player_id IS NULL AND team=? AND status='Not Yet Submitted'",
            (game_date, record['team']))]
    if any(row.get('official_report') and (
            (stamp := _instant(row.get('report_at'))) is not None and latest <= stamp <= now
            or row.get('report_time_precision') == 'calendar_date_only'
               and row.get('report_date', '') >= latest.astimezone(EASTERN).date().isoformat()
               and (published := _instant(row.get('source_published_at'))) is not None and latest < published <= now)
           for row in unsent):
        return {**result, 'reason': 'A newer official report says this team report has not yet been submitted.'}
    if any(row.get('official_report') and row.get('report_time_precision') == 'calendar_date_only'
           and row.get('report_date', '') >= latest.astimezone(EASTERN).date().isoformat()
           and row.get('status') != record['status']
           and (published := _instant(row.get('source_published_at'))) is not None and published > latest
           for row in rows):
        return {**result, 'reason': 'A newer official extract conflicts, but its report clock is unknown.'}
    return {**result, 'known': True, 'status': record['status'], 'blocked': record['status'] == 'Out',
            'report_at': latest.isoformat(), 'source_url': record['source_url'],
            'reason': record.get('reason'),
            'note': 'Explicit official designation as of this report; no starting role or unrestricted minutes is implied.'}


async def collect_availability(mirror, output, now=None, *, row_loader=None):
    """Publish structured report extracts via the existing bounded public mirror."""
    if row_loader is None:
        import pyarrow.parquet as pq
        row_loader = lambda content: pq.read_table(io.BytesIO(content)).to_pylist()
    now = now or dt.datetime.now(dt.timezone.utc)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / RELAY_NAME
    prior = load_availability_document(target.read_bytes()) if target.exists() else {}
    legacy = output / 'structured_availability.json'
    if not prior and legacy.exists():
        prior = json.loads(legacy.read_text())
    # A separately verified exact-clock PDF snapshot can coexist with the
    # mirror. Retain its real clock; a subsequent mirror fetch never freshens it.
    records = {row['record_id']: row for row in prior.get('records', [])
               if row.get('source_kind') == 'official_pdf'}
    previous = {row['record_id']: row for row in prior.get('records', [])}
    coverage = []
    normalizers = [('official_extract', 'nba_stats_injury_report', f'injury_report_{mirror.end_year}.parquet', normalize_nba_injuries),
                   ('espn_injury_snapshot', 'espn_nba_injuries', f'injuries_{mirror.end_year}.parquet', normalize_espn_injuries)]
    for kind, tag, name, normalize in normalizers:
        try:
            content, meta = await mirror.asset(tag, name)
            if len(content) > 8_000_000:
                raise ValueError('Availability source exceeds its bound.')
            rows = row_loader(content)
            # The source accumulates report history all season. A rolling
            # 30-day relay stays bounded; the database retains older imports.
            cutoff = (now.astimezone(EASTERN).date() - dt.timedelta(days=30)).isoformat()
            date_field = 'report_datetime' if kind == 'official_extract' else 'as_of_date'
            rows = [row for row in rows if _day(row.get(date_field)) >= cutoff]
            if len(rows) > MAX_RECORDS:
                raise ValueError('Availability source row bound exceeded.')
            digest = hashlib.sha256(content).hexdigest()
            parsed = normalize(rows, meta['browser_download_url'], meta['updated_at'], digest, now)
            for row in parsed:
                records[row['record_id']] = previous.get(row['record_id'], row)
            coverage.append({'source_kind': kind, 'status': 'collected', 'source_url': meta['browser_download_url'],
                'source_published_at': meta['updated_at'], 'source_content_sha256': digest,
                'source_rows': len(rows), 'distinct_records': len(parsed),
                'report_data_through_date': max((row['report_date'] for row in parsed), default=None),
                'game_dates_represented_through': max((row['game_date'] for row in parsed if row.get('game_date')), default=None),
                'report_clock_unknown': sum(row['report_time_precision'] not in VERIFIED_CLOCK_PRECISIONS for row in parsed)})
        except Exception as error:
            retained = [row for row in prior.get('records', []) if row.get('source_kind') == kind]
            for row in retained:
                records[row['record_id']] = row
            coverage.append({'source_kind': kind, 'status': 'unavailable', 'error_type': type(error).__name__,
                             'retained_records': len(retained), 'attempted_at': now.isoformat()})
    if records:
        document = build_availability_document(records.values(), now=now, coverage=coverage)
        temporary = target.with_suffix('.json.tmp')
        encoded = (_json(document) + '\n').encode()
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise ValueError('Availability relay expanded document exceeds its bound.')
        compressed = gzip.compress(encoded, mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ValueError('Availability relay compressed document exceeds its bound.')
        temporary.write_bytes(compressed)
        temporary.replace(target)
    return {'status': 'updated' if all(row['status'] == 'collected' for row in coverage) else 'partial',
            'records': len(records), 'coverage': coverage,
            'note': 'Structured source evidence; collection success does not establish complete current official availability.'}
