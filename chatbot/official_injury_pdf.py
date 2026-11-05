"""Collector-only official injury PDFs; ambiguous text never becomes a status.

No runtime PDF dependency: pypdf is imported only after a validated PDF download.
This is availability evidence, never a schedule or projected-minutes feed.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from structured_availability import (EASTERN, MAX_COMPRESSED_BYTES, MAX_DOCUMENT_BYTES,
    RELAY_NAME, _identity, _json, _source_allowed, build_availability_document,
    load_availability_document, normalize_official_pdf)

TEAM_NAMES = {'Atlanta Hawks':'ATL', 'Boston Celtics':'BOS', 'Brooklyn Nets':'BKN',
    'Charlotte Hornets':'CHA', 'Chicago Bulls':'CHI', 'Cleveland Cavaliers':'CLE',
    'Dallas Mavericks':'DAL', 'Denver Nuggets':'DEN', 'Detroit Pistons':'DET',
    'Golden State Warriors':'GSW', 'Houston Rockets':'HOU', 'Indiana Pacers':'IND',
    'LA Clippers':'LAC', 'Los Angeles Clippers':'LAC', 'Los Angeles Lakers':'LAL',
    'Memphis Grizzlies':'MEM', 'Miami Heat':'MIA', 'Milwaukee Bucks':'MIL',
    'Minnesota Timberwolves':'MIN', 'New Orleans Pelicans':'NOP', 'New York Knicks':'NYK',
    'Oklahoma City Thunder':'OKC', 'Orlando Magic':'ORL', 'Philadelphia 76ers':'PHI',
    'Phoenix Suns':'PHX', 'Portland Trail Blazers':'POR', 'Sacramento Kings':'SAC',
    'San Antonio Spurs':'SAS', 'Toronto Raptors':'TOR', 'Utah Jazz':'UTA',
    'Washington Wizards':'WAS'}
HEADER = re.compile(r'Injury Report:\s*(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})\s*(AM|PM)')
STATUS = re.compile(r'\b(Out|Questionable|Doubtful|Probable|Available)\b')
NAME = re.compile(r"[\w.'’\- ]+,\s*[\w.'’\- ]+", re.UNICODE)


def report_clock(url):
    if not _source_allowed(url, 'official_pdf'):
        raise ValueError('Unsupported official PDF URL.')
    match = re.search(r'Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf$', urlsplit(url).path)
    return dt.datetime.strptime(' '.join(match.groups()), '%Y-%m-%d %I %M %p').replace(tzinfo=EASTERN)


def latest_report_url(index_html, index_url, now):
    """Only a real current-day link on the official index may be selected."""
    if not re.fullmatch(r'https://official\.nba\.com/nba-injury-report-\d{4}-\d{2}-season/', index_url):
        raise ValueError('Unsupported official injury index.')
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.urls = []
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                self.urls.extend(urljoin(index_url, value) for key, value in attrs if key == 'href' and value)
    parser = Links(); parser.feed(index_html)
    eligible = []
    for url in parser.urls:
        try: stamp = report_clock(url)
        except (ValueError, TypeError): continue
        if stamp.date() == now.astimezone(EASTERN).date() and stamp <= now:
            eligible.append((stamp, url))
    if not eligible:
        raise ValueError('Official index has no nonfuture current-day injury report.')
    return max(eligible)[1]


def parse_report_pages(pages, source_url):
    """Read explicit dates, matchup/team cells and comma-form player rows.

    A new matchup clears team and clock context. Only an explicit same-game
    team heading can establish a team; page boundaries retain that context.
    Unknown rows are excluded and make the result partial.
    """
    expected = report_clock(source_url)
    rows, excluded, headers = [], [], []
    game_date = away = home = team = clock = None
    last = None
    pending_name = None
    for page_number, page in enumerate(pages, 1):
        if not isinstance(page, str) or not page.strip():
            raise ValueError('Official PDF has an unreadable page.')
        # Some text extractors concatenate the page footer and next header.
        page = re.sub(r'(?<!\n)(Injury Report:)', r'\n\1', page)
        for original in page.splitlines():
            line = ' '.join(original.split())
            if not line: continue
            header = HEADER.fullmatch(line)
            if header:
                observed = dt.datetime.strptime(' '.join(header.groups()), '%m/%d/%y %I:%M %p').replace(tzinfo=EASTERN)
                if observed != expected:
                    raise ValueError('Official PDF header and filename report clocks disagree.')
                headers.append(observed)
                continue
            if re.fullmatch(r'Page \d+ of \d+', line) or line == 'Game Date Game Time Matchup Team Player Name Current Status Reason':
                continue
            changed = False
            date = re.match(r'^(\d{2}/\d{2}/\d{4})(?:\s+|$)', line)
            if date:
                game_date = dt.datetime.strptime(date.group(1), '%m/%d/%Y').date().isoformat()
                line = line[date.end():]; away = home = team = clock = None
                changed = True
            elif re.match(r'^\d{1,2}/\d{1,2}/\d{2,4}\b', line):
                excluded.append({'page':page_number, 'text':original, 'reason':'Unrecognized date; game context cleared.'})
                game_date = away = home = team = clock = last = pending_name = None
                continue
            time = re.match(r'^(\d{1,2}:\d{2}\s*\(ET\))(?:\s+|$)', line)
            explicit_clock = time.group(1) if time else None
            if time: line = line[time.end():]
            matchup = re.match(r'^([A-Z]{3})@([A-Z]{3})(?:\s+|$)', line)
            if matchup:
                away, home = matchup.groups(); team = None; clock = explicit_clock
                line = line[matchup.end():]; changed = True
                if away not in TEAM_NAMES.values() or home not in TEAM_NAMES.values() or away == home:
                    away = home = None
            elif explicit_clock:
                # A time without a matchup is not a new verified game context.
                team = None; changed = True
            if '@' in line:
                # Never bury an unreadable matchup inside the previous
                # player's wrapped medical reason, then reuse its team.
                excluded.append({'page':page_number, 'text':original, 'reason':'Unrecognized matchup; game context cleared.'})
                away = home = team = clock = last = pending_name = None
                continue
            found_team = False
            for name in sorted(TEAM_NAMES, key=len, reverse=True):
                if line == name or line.startswith(name + ' '):
                    candidate = TEAM_NAMES[name]
                    team = candidate if candidate in {away, home} else None
                    line = line[len(name):].strip(); changed = True
                    found_team = True
                    break
            if not found_team and any(
                    re.search(r'\b'+re.escape(name.split()[-1])+r'\b', line)
                    or (len(name.split()) > 1 and line == ' '.join(name.split()[:-1]))
                    for name in TEAM_NAMES):
                excluded.append({'page':page_number, 'text':original, 'reason':'Wrapped or ambiguous team heading; team context cleared.'})
                team = clock = last = pending_name = None
                continue
            if changed:
                if pending_name:
                    excluded.append({'page': page_number, 'text': pending_name, 'reason': 'Player name has no unambiguous status.'})
                last = None; pending_name = None
            if not line: continue
            if line == 'NOT YET SUBMITTED':
                if game_date and away and home and team:
                    rows.append({'game_date': game_date, 'away_team': away, 'home_team': home,
                        'team': team, 'player': None, 'status': 'Not Yet Submitted',
                        'reason': 'Team report not yet submitted', 'game_time_text': clock})
                else: excluded.append({'page':page_number, 'text':original, 'reason':'Unsubmitted team has unresolved game/team context.'})
                last = None; pending_name = None
                continue
            if pending_name:
                line = pending_name + ' ' + line
                pending_name = None
            status = STATUS.search(line)
            if status:
                player = line[:status.start()].strip()
                reason = line[status.end():].strip()
                if NAME.fullmatch(player) and game_date and away and home and team:
                    last = {'game_date': game_date, 'away_team':away, 'home_team':home,
                        'team':team, 'player':player, 'status':status.group(1),
                        'reason':reason or None, 'game_time_text':clock}
                    rows.append(last)
                else:
                    excluded.append({'page':page_number, 'text':original, 'reason':'Player/status or game/team context is ambiguous.'})
                    last = None
                continue
            if ',' in line and not line.startswith(('Injury/Illness', 'G League', 'Personal')):
                if re.fullmatch(r"[\w.'’\- ]+,\s*[\w.'’\- ]*", line):
                    pending_name = line; last = None
                else:
                    excluded.append({'page':page_number, 'text':original, 'reason':'Unrecognized player row.'}); last = None
                continue
            if last and (last['reason'] or re.match(r'^(Injury/Illness\b|Not With Team\b|G League\b|Rest\b|Personal\b|League Suspension\b)', line)):
                last['reason'] = ((last['reason'] or '') + ' ' + line).strip()
            else:
                excluded.append({'page':page_number, 'text':original, 'reason':'Unrecognized report text.'}); last = None
    if pending_name:
        excluded.append({'page':len(pages), 'text':pending_name, 'reason':'Player name has no status.'})
    if not headers or not rows:
        raise ValueError('Official PDF has no verified report header or unambiguous rows.')
    # Conflicting same-report duplicate designations are excluded together.
    grouped = {}
    for row in rows:
        key = (row['game_date'], row['away_team'], row['home_team'], row['team'], row['player'])
        grouped.setdefault(key, []).append(row)
    clean = []
    for key, group in grouped.items():
        if len({(r['status'], r['reason']) for r in group}) != 1:
            excluded.append({'reason':'Conflicting repeated report row.', 'identity':key})
        else: clean.append(group[0])
    if not clean: raise ValueError('No unambiguous official injury rows remain.')
    return {'rows':clean, 'excluded':excluded, 'status':'partial' if excluded else 'collected',
            'report_at':expected.isoformat(), 'pages':len(pages), 'headers_verified':len(headers)}


async def _bounded_get(client, url, maximum, *, pdf=False):
    async with client.stream('GET', url, follow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('Official injury redirect was not followed.')
        if pdf and response.headers.get('content-type','').split(';')[0].strip().lower() != 'application/pdf':
            raise ValueError('Official injury response is not a PDF MIME type.')
        if int(response.headers.get('content-length', '0')) > maximum:
            raise ValueError('Official injury source exceeds its byte bound.')
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > maximum: raise ValueError('Official injury source exceeds its byte bound.')
        if pdf and not chunks.startswith(b'%PDF-'):
            raise ValueError('Official injury response has no PDF signature.')
        return bytes(chunks)


def extract_pdf_pages(content):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted or not 0 < len(reader.pages) <= 30:
        raise ValueError('Official injury PDF is encrypted or has an unsupported page count.')
    pages = [page.extract_text(extraction_mode='layout') for page in reader.pages]
    if sum(len(text or '') for text in pages) > 2_000_000:
        raise ValueError('Official injury PDF text exceeds its bound.')
    return pages


async def collect_official_pdf(client, output, now=None, *, page_extractor=None):
    """Try the original publisher once; failure retains prior evidence clocks."""
    now = now or dt.datetime.now(dt.timezone.utc)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    target = output / RELAY_NAME
    previous = load_availability_document(target.read_bytes()) if target.exists() else {}
    year = now.astimezone(EASTERN).year - (now.astimezone(EASTERN).month < 7)
    index_url = f'https://official.nba.com/nba-injury-report-{year}-{str(year+1)[-2:]}-season/'
    status = {'source_kind':'official_pdf', 'attempted_at':now.isoformat(), 'index_url':index_url}
    try:
        index = await _bounded_get(client, index_url, 2_000_000)
        url = latest_report_url(index.decode('utf-8'), index_url, now)
        content = await _bounded_get(client, url, 8_000_000, pdf=True)
        pages = (page_extractor or extract_pdf_pages)(content)
        parsed = parse_report_pages(pages, url)
        text_hash = hashlib.sha256('\f'.join(pages).encode()).hexdigest()
        content_hash = hashlib.sha256(content).hexdigest()
        records = normalize_official_pdf(parsed['rows'], source_url=url, report_at=parsed['report_at'],
            text_sha256=text_hash, pdf_sha256=content_hash, now=now)
        old = {row['record_id']:row for row in previous.get('records', [])}
        # Two report files bound this relay; the bot database retains history.
        pdf_urls = {row['source_url']:row['report_at'] for row in old.values() if row.get('source_kind') == 'official_pdf'}
        pdf_urls[url] = parsed['report_at']
        retained_urls = set(sorted(pdf_urls, key=lambda value:pdf_urls[value], reverse=True)[:2])
        merged = {key:row for key,row in old.items() if row.get('source_kind') != 'official_pdf' or row['source_url'] in retained_urls}
        for row in records: merged[row['record_id']] = old.get(row['record_id'], row)
        status.update(status=parsed['status'], source_url=url, report_at=parsed['report_at'],
            source_content_sha256=content_hash, source_text_sha256=text_hash, records=len(records),
            excluded_rows=len(parsed['excluded']), exclusions=parsed['excluded'][:20],
            unsubmitted_team_games=sum(row['status']=='Not Yet Submitted' for row in records),
            complete_current_player_availability=False,
            note='Only explicit official designations are archived. Unsubmitted teams and absent names remain unknown; game-time text does not verify a schedule.')
        coverage = [row for row in previous.get('coverage',[]) if row.get('source_kind') != 'official_pdf'] + [status]
        doc = build_availability_document(merged.values(), now=now, coverage=coverage)
        encoded = (_json(doc)+'\n').encode()
        if len(encoded)>MAX_DOCUMENT_BYTES: raise ValueError('Availability document exceeds its expanded bound.')
        compressed = gzip.compress(encoded, mtime=0)
        if len(compressed)>MAX_COMPRESSED_BYTES: raise ValueError('Availability document exceeds its compressed bound.')
        temporary=target.with_suffix('.pdf.tmp'); temporary.write_bytes(compressed); temporary.replace(target)
    except Exception as error:
        status.update(status='unavailable', error_type=type(error).__name__,
            http_status=getattr(getattr(error,'response',None),'status_code',None),
            retained_pdf_records=sum(row.get('source_kind')=='official_pdf' for row in previous.get('records',[])),
            note='Original publisher was unavailable or could not be parsed reliably; previous evidence and its original report/retrieval times were retained.')
    health=output/'official_injury_pdf_status.json'; temporary=health.with_suffix('.tmp')
    temporary.write_text(_json(status)+'\n'); temporary.replace(health)
    return status
