"""Scheduled NBA game, box-score, news and availability collection.

ESPN public endpoints are a best-effort baseline, not a licensed low-latency feed.
Source and retrieval timestamps are retained; unavailable values remain missing.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import hashlib
import html
import inspect
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger('knicks.feeds')
# Provider query strings may hold a subscriber key; never emit full request URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
EASTERN = ZoneInfo('America/New_York')
ESPN = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/'
TEAM_ALIASES = {'NY': 'NYK', 'GS': 'GSW', 'SA': 'SAS', 'NO': 'NOP',
                'UTAH': 'UTA', 'WSH': 'WAS', 'PHO': 'PHX', 'BRK': 'BKN'}
TEAM_WORDS = {'ATL': 'hawks', 'BOS': 'celtics', 'BKN': 'nets', 'CHA': 'hornets',
              'CHI': 'bulls', 'CLE': 'cavaliers', 'DAL': 'mavericks', 'DEN': 'nuggets',
              'DET': 'pistons', 'GSW': 'warriors', 'HOU': 'rockets', 'IND': 'pacers',
              'LAC': 'clippers', 'LAL': 'lakers', 'MEM': 'grizzlies', 'MIA': 'heat',
              'MIL': 'bucks', 'MIN': 'timberwolves', 'NOP': 'pelicans', 'NYK': 'knicks',
              'OKC': 'thunder', 'ORL': 'magic', 'PHI': '76ers', 'PHX': 'suns',
              'POR': 'trail blazers', 'SAC': 'kings', 'SAS': 'spurs', 'TOR': 'raptors',
              'UTA': 'jazz', 'WAS': 'wizards'}
MAX_RESPONSE_BYTES = 8_000_000
MATERIAL = re.compile(r'\b(injur\w*|out|questionable|doubtful|probable|ruled|return\w*|'
                      r'lineup|start\w*|trade\w*|sign\w*|waiv\w*|suspend\w*|minutes?|'
                      r'sidelin\w*|rest|illness|surgery|availability)\b', re.I)


def canonical_team(value):
    team = str(value or '').upper().strip()
    return TEAM_ALIASES.get(team, team)


def canonical_player(name):
    folded = unicodedata.normalize('NFKD', str(name)).encode('ascii', 'ignore').decode().lower()
    return 'name:' + re.sub('[^a-z0-9]', '', folded)


def canonical_game(date, home, away):
    return 'nba:' + str(date)[:10] + ':' + ':'.join(sorted([canonical_team(home), canonical_team(away)]))


def eastern_date(value):
    instant = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.timezone.utc)
    return instant.astimezone(EASTERN).date().isoformat()


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(',', '').strip())
        return int(number) if number.is_integer() else number
    except (ValueError, TypeError):
        return None


def minutes(value):
    if isinstance(value, str) and ':' in value:
        try:
            mins, seconds = value.split(':', 1)
            return float(mins) + float(seconds) / 60
        except ValueError:
            return None
    return numeric(value)


def clean_text(value, limit=4000):
    return html.unescape(re.sub(r'<[^>]+>', ' ', str(value or ''))).strip()[:limit]


def news_url(article):
    links = article.get('links') or {}
    web = links.get('web') or {}
    return web.get('href') or article.get('url') or ''


def parse_game(event):
    competition = (event.get('competitions') or [{}])[0]
    teams = {t.get('homeAway'): t for t in competition.get('competitors', [])}
    if 'home' not in teams or 'away' not in teams:
        raise ValueError('Scoreboard event has no home/away pair.')
    home = canonical_team(teams['home'].get('team', {}).get('abbreviation'))
    away = canonical_team(teams['away'].get('team', {}).get('abbreviation'))
    if not home or not away:
        raise ValueError('Scoreboard event has no team abbreviation.')
    date = eastern_date(event.get('date') or competition['date'])
    status = (event.get('status') or competition.get('status') or {}).get('type', {})
    name = str(status.get('name', ''))
    state = status.get('state')
    if status.get('completed') and name not in {'STATUS_POSTPONED', 'STATUS_CANCELED'}:
        normalized_status = 'final'
    elif name == 'STATUS_POSTPONED':
        normalized_status = 'postponed'
    elif name == 'STATUS_CANCELED':
        normalized_status = 'canceled'
    elif state == 'in':
        normalized_status = 'live'
    else:
        normalized_status = 'scheduled'
    return {
        'id': canonical_game(date, home, away), 'date': date, 'status': normalized_status,
        'home_team': home, 'away_team': away,
        'home_score': numeric(teams['home'].get('score')) if state != 'pre' else None,
        'away_score': numeric(teams['away'].get('score')) if state != 'pre' else None,
        'provider_game_id': str(event.get('id') or competition.get('id')),
        'start_time': event.get('date') or competition.get('date'),
        'source': ESPN + 'scoreboard?dates=' + date.replace('-', ''),
    }


def parse_boxscore(summary, game):
    season_info = (summary.get('header') or {}).get('season') or {}
    year = int(season_info.get('year') or (int(game['date'][:4]) + (int(game['date'][5:7]) >= 7)))
    phase = {1: 'Preseason', 2: 'Regular Season', 3: 'Playoffs'}.get(season_info.get('type'), 'Unknown')
    rows = []
    for team in (summary.get('boxscore') or {}).get('players', []):
        abbrev = canonical_team((team.get('team') or {}).get('abbreviation'))
        opponent = game['away_team'] if abbrev == game['home_team'] else game['home_team']
        for group in team.get('statistics', []):
            # labels/names change order; mapping by supplied names avoids position assumptions.
            labels = group.get('labels') or group.get('names') or []
            for athlete in group.get('athletes', []):
                person = athlete.get('athlete') or {}
                name = person.get('displayName') or person.get('fullName')
                if not name:
                    continue
                values = dict(zip(labels, athlete.get('stats') or []))
                played = not athlete.get('didNotPlay', False)
                row = {
                    'game_id': game['id'], 'player_id': canonical_player(name), 'player': name,
                    'provider_player_id': str(person.get('id', '')), 'team': abbrev,
                    'opponent': opponent, 'date': game['date'],
                    'season': f'{year-1}-{str(year)[-2:]}', 'season_type': phase,
                    'appearance_state': 'Played' if played else 'DNP',
                    'source': ESPN + 'summary?event=' + game['provider_game_id'],
                    'minutes': minutes(values.get('MIN')) if played else None,
                    'starter': athlete.get('starter'),
                }
                for source, target in [('PTS', 'pts'), ('REB', 'reb'), ('AST', 'ast'),
                                       ('STL', 'stl'), ('BLK', 'blk'), ('TO', 'tov'),
                                       ('OREB','oreb'),('DREB','dreb'),('PF','fouls'),('+/-','plus_minus')]:
                    row[target] = numeric(values.get(source)) if played else None
                for labels_options, made, attempts in [(('FG', 'FGM-A'), 'fgm', 'fga'),
                                                        (('3PT', '3PM-A', '3PTM-A'), 'tpm', 'tpa'),
                                                        (('FT', 'FTM-A'), 'ftm', 'fta')]:
                    value = next((values[k] for k in labels_options if k in values), None)
                    pair = str(value).split('-') if value is not None else []
                    row[made] = numeric(pair[0]) if played and len(pair) == 2 else None
                    row[attempts] = numeric(pair[1]) if played and len(pair) == 2 else None
                rows.append(row)
    return rows


def parse_rss(content, source):
    if len(content) > MAX_RESPONSE_BYTES or re.search(br'<!\s*(DOCTYPE|ENTITY)', content, re.I):
        raise ValueError('Unsupported or oversized RSS document.')
    root = ET.fromstring(content)
    entries = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
    result = []
    for entry in entries[:50]:
        def text(name):
            item = entry.find(name)
            if item is None:
                item = entry.find('{http://www.w3.org/2005/Atom}' + name)
            return (item.text or '').strip() if item is not None else ''
        link = text('link')
        if not link:
            atom_link = entry.find('{http://www.w3.org/2005/Atom}link')
            if atom_link is not None:
                link = atom_link.get('href', '')
        identity = text('guid') or text('id') or link or text('title')
        published = text('pubDate') or text('published') or text('updated')
        try:
            published = email.utils.parsedate_to_datetime(published).isoformat()
        except (ValueError, TypeError):
            pass
        result.append({'id': 'rss:' + hashlib.sha256((source + identity).encode()).hexdigest(),
                       'title': clean_text(text('title'), 500), 'url': link,
                       'published_at': published or None, 'source': source,
                       'body': clean_text(text('description') or text('summary'))})
    return result


class FeedWorker:
    def __init__(self, store, notify=None, client=None, now=None):
        self.store, self.notify, self.client = store, notify, client
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.poll_seconds = max(60, int(os.getenv('FEED_POLL_SECONDS', '300')))
        self.fast_poll_seconds = max(60, min(self.poll_seconds, int(os.getenv('FEED_FAST_POLL_SECONDS', '60'))))
        self.backfill_days = max(1, min(30, int(os.getenv('FEED_BACKFILL_DAYS', '7'))))
        self.correction_days = max(1, min(14, int(os.getenv('FEED_CORRECTION_DAYS', '3'))))
        self.rss_urls = [u.strip() for u in os.getenv('NEWS_FEED_URLS', '').split(',') if u.strip()]
        self.injury_url = os.getenv('INJURY_FEED_URL', '').strip()
        self.interest = {canonical_team(t) for t in os.getenv('ALERT_TEAMS', 'NYK').split(',') if t.strip()}
        self._last_request = 0.0
        self._notified_bootstrap = bool(self.store.get_checkpoint('feeds_initialized', False))
        self._cycle_lock = asyncio.Lock()
        self.data_source = os.getenv('NBA_DATA_SOURCE', 'espn').strip().lower()
        if self.data_source not in {'espn', 'nba_cdn', 'manual'}:
            raise ValueError('NBA_DATA_SOURCE must be espn, nba_cdn or manual.')
        self.news_source = os.getenv('NEWS_SOURCE', 'espn').strip().lower()
        if self.news_source not in {'espn', 'rss'}:
            raise ValueError('NEWS_SOURCE must be espn or rss.')
        self._nba_source = None
        if self.data_source == 'nba_cdn':
            from nba_cdn import NBADataSource
            self._nba_source = NBADataSource(store, self.request, now=self.now)
            for name in ('espn_games', 'espn_schedule', 'espn_reconciliation', 'espn_live_carryover'):
                if any(feed['name'] == name for feed in store.status().get('feeds', [])):
                    store.feed_failure(name, 'Disabled: NBA_DATA_SOURCE=nba_cdn; see nba_cdn_* feed status.')
        if self.news_source == 'rss':
            store.feed_failure('espn_news', 'Disabled: NEWS_SOURCE=rss; see rss_* feed status.')
        if self.data_source == 'manual':
            daily_enabled=os.getenv('DAILY_NBA_REFRESH','1')=='1'
            collection_note=('Intraday collector disabled; automatic previous-day statistics use nba_daily_refresh.'
                             if daily_enabled else 'Game statistics require explicit uploads.')
            for feed in store.status().get('feeds', []):
                if feed['name'].startswith(('espn_', 'nba_cdn_')) and feed['name'] != 'espn_news':
                    store.feed_failure(feed['name'], 'Disabled: '+collection_note)
            store.feed_failure('automatic_games', 'Disabled: '+collection_note)

    def check_cooldown(self, host):
        state = self.store.get_checkpoint('feed_http_cooldown:' + host, {})
        if state.get('until', 0) > time.time():
            remaining = max(1, int(state['until'] - time.time()))
            raise RuntimeError(f"HTTP {state.get('status')} cooldown for {host}; retry in {remaining}s. Use /diagnose for a manual public-source check.")

    def http_status(self, response):
        status = response.status_code
        host = response.request.url.host
        if status in {401, 403, 429}:
            key = 'feed_http_cooldown:' + host
            old = self.store.get_checkpoint(key, {})
            attempts = min(4, int(old.get('attempts', 0)) + 1)
            delay = min(3600, 900 * 2 ** (attempts - 1))
            retry = response.headers.get('retry-after', '')
            if retry.isdigit():
                delay = max(delay, min(86400, int(retry)))
            self.store.set_checkpoint(key, {'status': status, 'until': time.time() + delay,
                                           'attempts': attempts})
        response.raise_for_status()
        self.store.set_checkpoint('feed_http_cooldown:' + host, {})

    async def request(self, url, json_response=True):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Feed URLs must use HTTPS without credentials in the URL.')
        self.check_cooldown(parsed.hostname)
        # Global pacing also applies to startup/catchup, without concurrent hammering.
        delay = max(0, 0.3 - (time.monotonic() - self._last_request))
        if delay:
            await asyncio.sleep(delay)
        self._last_request = time.monotonic()
        if self.client is None:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                         headers={'User-Agent': 'NBAPPBOT/2.0 personal research'}) as client:
                async with client.stream('GET', url) as response:
                    self.http_status(response)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ValueError('Feed response exceeds the configured size limit.')
        else:
            response = await self.client.get(url)
            self.http_status(response)
            data = response.content
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError('Feed response exceeds the configured size limit.')
        return json.loads(data) if json_response else bytes(data)

    async def announce(self, text, event_key):
        if self.notify is None:
            return
        response = self.notify(text, event_key)
        if inspect.isawaitable(response):
            await response

    def relevant_news(self, item):
        from news_digest import material_categories
        text = (item.get('title', '') + ' ' + item.get('body', '')).lower()
        team = canonical_team(item.get('team'))
        interested_teams = set(self.interest)
        today = self.now().astimezone(EASTERN).date().isoformat()
        for game in self.store.games(today):
            if game.get('home_team') in self.interest or game.get('away_team') in self.interest:
                interested_teams.update([game.get('home_team'), game.get('away_team')])
        tracked_names = []
        entries = getattr(self.store, 'list_entries', lambda: [])()
        for entry in entries:
            if entry.get('settled'):
                continue
            for leg in entry.get('legs') or []:
                name = str(leg.get('player') or '').strip().lower()
                if len(name) > 4:
                    tracked_names.append(name)
        return bool(material_categories(item) and (team in interested_teams or
                    any(re.search(r'\b' + re.escape(TEAM_WORDS.get(t, 'no-such-team')) + r'\b', text)
                        for t in interested_teams) or
                    any(name in text for name in tracked_names) or
                    ('NYK' in self.interest and any(term in text for term in ['knicks', 'brunson', 'towns',
                                                        'anunoby', 'mikal bridges', 'josh hart', 'mcbride']))))

    def next_delay(self):
        now = self.now()
        today = now.astimezone(EASTERN).date().isoformat()
        if self.data_source=='manual' and callable(getattr(self.store,'connect',None)):
            with self.store.connect() as db:
                rows=[dict(row) for row in db.execute("SELECT start_time,status FROM season_schedule WHERE date=? AND (home_team='NYK' OR away_team='NYK')",(today,))]
            if rows:
                from news_digest import _aware
                for row in rows:
                    tip=_aware(row.get('start_time'))
                    if row.get('status')=='live' or tip and 0<=(tip-now).total_seconds()<=5400:
                        return self.fast_poll_seconds
                    if tip and 0<=(tip-now).total_seconds()<=21600:
                        return self.poll_seconds
                return max(900,self.poll_seconds)
            from season_calendar import current_season
            if self.store.get_checkpoint('season_calendar:'+current_season(now)):
                return max(900,self.poll_seconds)
        for game in self.store.games(today):
            if game.get('status') == 'live':
                return self.fast_poll_seconds
            if game.get('home_team') not in self.interest and game.get('away_team') not in self.interest:
                continue
            try:
                start = dt.datetime.fromisoformat(game.get('start_time', '').replace('Z', '+00:00'))
                if 0 <= (start - now).total_seconds() <= 5400:
                    return self.fast_poll_seconds
            except (ValueError, TypeError):
                pass
        return self.poll_seconds

    async def save_news(self, item, alerts=True):
        from news_digest import is_promotion, material_categories
        from gameday_digest import alert_mode
        item = dict(item)
        if not item.get('verification'):
            text = (item.get('title', '') + ' ' + item.get('body', '')).lower()
            rumor = re.search(r'\b(rumou?rs?|could|may|might|reportedly|speculat\w*|trade interest|trade target)\b', text)
            item['verification'] = 'rumor/unconfirmed' if rumor else 'publisher-reported; not independently verified'
        changed = await asyncio.to_thread(self.store.record_news, item)
        material = bool(material_categories(item))
        league_alert = os.getenv('NEWS_ALERT_SCOPE', 'relevant') == 'league' and material
        if (alert_mode() == 'instant' and changed and alerts and self._notified_bootstrap and not is_promotion(item)
                and (league_alert or self.relevant_news(item))):
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:16]
            await self.announce('NBA update [' + item['verification'] + ']: ' + item['title'] + '\n'
                                + 'Published: ' + str(item.get('published_at') or 'not supplied') + '\n'
                                + 'Source: ' + str(item.get('source', '')) + '\n' + str(item.get('url', '')),
                                'news:' + item['id'] + ':' + digest)
        return changed

    async def update_games(self, day, summaries=True):
        if self.data_source == 'manual':
            return 0, 0
        if self._nba_source is not None:
            return await self._nba_source.update_games(day, with_boxscores=summaries)
        document = await self.request(ESPN + 'scoreboard?dates=' + day.replace('-', ''))
        if 'events' not in document or not isinstance(document['events'], list):
            raise ValueError('Scoreboard response is missing its event list.')
        count, stat_count = 0, 0
        errors = []
        for event in document['events']:
            game = parse_game(event)
            count += 1
            if (not summaries and game['status'] != 'final') or game['status'] in {'postponed', 'canceled'}:
                await asyncio.to_thread(self.store.upsert_game, game)
                continue
            try:
                summary = await self.request(ESPN + 'summary?event=' + game['provider_game_id'])
                # Scheduled summaries provide availability, but do not invent empty box scores.
                if game['status'] in {'live', 'final'}:
                    rows = parse_boxscore(summary, game)
                    if game['status'] == 'final' and not any(
                            row.get('appearance_state') == 'Played'
                            and row.get('pts') is not None and row.get('minutes') is not None
                            for row in rows):
                        raise ValueError('Final game box score is not yet available.')
                    played = [row for row in rows if row['appearance_state'] == 'Played']
                    if any(row.get('pts') is None or row.get('minutes') is None for row in played):
                        raise ValueError('Played box-score rows are missing points or minutes.')
                    if hasattr(self.store, 'upsert_game_bundle'):
                        await asyncio.to_thread(self.store.upsert_game_bundle, game, rows)
                    else:
                        for row in rows:
                            await asyncio.to_thread(self.store.upsert_player_game, row)
                        await asyncio.to_thread(self.store.upsert_game, game)
                    stat_count += len(rows)
                else:
                    await asyncio.to_thread(self.store.upsert_game, game)
                current_day = self.now().astimezone(EASTERN).date().isoformat()
                availability = (summary.get('injuries') or []) if day == current_day else []
                for team in availability:
                    abbrev = canonical_team((team.get('team') or {}).get('abbreviation'))
                    for injury in team.get('injuries') or []:
                        athlete = injury.get('athlete') or {}
                        name = athlete.get('displayName') or athlete.get('fullName')
                        if not name:
                            continue
                        state = clean_text(injury.get('status'), 100)
                        details = injury.get('details') or {}
                        body = clean_text(injury.get('longComment') or injury.get('shortComment')
                                          or details.get('detail') or details.get('type'))
                        await self.save_news({
                            'id': 'espn-injury:' + abbrev + ':' + canonical_player(name),
                            'title': name + ' (' + abbrev + '): ' + state,
                            'url': game['source'], 'published_at': injury.get('date'),
                            'source': 'ESPN game-summary availability', 'team': abbrev,
                            'player': name, 'body': body + '\nAvailability snapshot; not the official NBA injury report.'},
                            alerts=(day == self.now().astimezone(EASTERN).date().isoformat()))
            except Exception as error:
                # Store a terse non-secret diagnostic; continue collecting the other games.
                errors.append(game['id'] + ': ' + type(error).__name__)
        if errors:
            raise RuntimeError(f'{count} games, {stat_count} player rows; incomplete summaries: ' + '; '.join(errors[:8]))
        return count, stat_count

    async def collect_news(self):
        document = await self.request(ESPN + 'news?limit=50')
        if not isinstance(document.get('articles'), list):
            raise ValueError('News response is missing articles.')
        for article in document['articles'][:50]:
            categories = article.get('categories') or []
            team = next((c.get('team', {}).get('abbreviation') for c in categories
                         if c.get('type') == 'team'), '')
            await self.save_news({'id': 'espn-news:' + str(article.get('id') or article.get('nowId')),
                                  'title': clean_text(article.get('headline'), 500),
                                  'url': news_url(article), 'published_at': article.get('published'),
                                  'source': 'ESPN NBA news', 'team': canonical_team(team),
                                  'body': clean_text(article.get('description'))})
        return len(document['articles'])

    async def collect_injuries(self):
        if not self.injury_url:
            return
        document = await self.request(self.injury_url)
        items = document if isinstance(document, list) else document.get('injuries')
        if not isinstance(items, list):
            raise ValueError('INJURY_FEED_URL must return an injuries array or an array of records.')
        for record in items[:2000]:
            player = clean_text(record.get('player'), 120)
            state = clean_text(record.get('status'), 120)
            if not player or not state:
                raise ValueError('Each injury record needs player and status fields.')
            await self.save_news({'id': 'injury:' + str(record.get('id') or canonical_player(player)),
                                  'title': player + ': ' + state,
                                  'source': clean_text(record.get('source') or 'Configured injury feed', 160),
                                  # Subscriber URLs can carry keys in query strings; only
                                  # a provider-supplied public article URL may be published.
                                  'url': (record.get('url') if record.get('url') != self.injury_url else None)
                                         or urlparse(self.injury_url)._replace(query='', fragment='').geturl(),
                                  'published_at': record.get('published_at'),
                                  'body': clean_text(record.get('detail') or record.get('body')),
                                  'team': canonical_team(record.get('team')), 'player': player})

    async def guarded(self, name, operation, detail=''):
        try:
            result = await operation()
            await asyncio.to_thread(self.store.feed_success, name, detail or str(result or 'Updated'))
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Avoid exception strings because HTTP URLs may contain private feed query keys.
            message = type(error).__name__
            if isinstance(error, httpx.HTTPStatusError):
                message += ': HTTP ' + str(error.response.status_code)
            elif isinstance(error, (ValueError, RuntimeError)):
                message += ': ' + str(error)[:350]
            await asyncio.to_thread(self.store.feed_failure, name, message)
            log.warning('Feed %s failed (%s)', name, type(error).__name__)
            return False

    async def collect_once(self, date=None):
        async with self._cycle_lock:
            today = self.now().astimezone(EASTERN).date()
            prefix = self.data_source
            if self.data_source == 'manual':
                if date:
                    raise ValueError('Manual statistics mode does not fetch game data.')
                await self.collect_news_sources()
                await asyncio.to_thread(self.store.set_checkpoint, 'feeds_initialized', True)
                self._notified_bootstrap = True
                return
            if date:
                target = dt.date.fromisoformat(str(date))
                return await self.guarded(prefix + '_games', lambda: self.update_games(target.isoformat()))
            current_ok = await self.guarded(prefix + '_games', lambda: self.update_games(today.isoformat()),
                                            'All NBA teams; current-day scoreboard and available player box scores')
            carry_dates = {game.get('date') for game in self.store.games()
                           if game.get('status') == 'live' and game.get('date')
                           and (today - dt.timedelta(days=3)).isoformat() <= game['date'] < today.isoformat()}
            for old_date in sorted(carry_dates):
                await self.guarded(prefix + '_live_carryover', lambda d=old_date: self.update_games(d),
                                   'Live NBA games that started before Eastern midnight')
            await self.guarded(prefix + '_schedule', lambda: self.update_games((today + dt.timedelta(days=1)).isoformat(), False),
                               'Next-day NBA schedule')
            checkpoint = self.store.get_checkpoint('last_reconciled_date')
            if checkpoint != today.isoformat():
                # Initial bootstrap collects 7 days. Normal daily reconciliation revisits 3
                # recent days for stat corrections; outage recovery revisits missed days.
                floor = today - dt.timedelta(days=self.backfill_days)
                start = today - dt.timedelta(days=self.correction_days)
                if checkpoint:
                    try:
                        start = min(start, dt.date.fromisoformat(checkpoint))
                    except ValueError:
                        start = floor
                else:
                    start = floor
                reconciled = True
                day = start
                # Resume a long outage in bounded batches instead of dropping days older
                # than a rolling window. On a normal day this is just the correction window.
                batch_end = min(today, start + dt.timedelta(days=self.backfill_days))
                while day < batch_end:
                    ok = await self.guarded(prefix + '_reconciliation', lambda d=day: self.update_games(d.isoformat()),
                                             'Recent NBA games revisited for corrections and restart recovery')
                    reconciled = ok and reconciled
                    day += dt.timedelta(days=1)
                if reconciled and current_ok:
                    await asyncio.to_thread(self.store.set_checkpoint, 'last_reconciled_date', batch_end.isoformat())
            await self.collect_news_sources()
            if current_ok and self.data_source == 'espn':
                await asyncio.to_thread(self.store.feed_success, 'availability_coverage',
                                        'ESPN game-summary snapshots only; no complete official NBA injury-report adapter')
            elif self.data_source == 'espn':
                await asyncio.to_thread(self.store.feed_failure, 'availability_coverage',
                                        'Current ESPN game-summary collection incomplete; no full official injury-report coverage.')
            await asyncio.to_thread(self.store.set_checkpoint, 'feeds_initialized', True)
            self._notified_bootstrap = True

    async def collect_news_sources(self):
        if self.news_source == 'espn':
            await self.guarded('espn_news', self.collect_news,
                               'ESPN NBA article headlines/descriptions; best-effort news coverage')
        elif not self.rss_urls:
            await asyncio.to_thread(self.store.feed_failure, 'rss_news',
                                    'NEWS_SOURCE=rss but NEWS_FEED_URLS is empty. Add published RSS feed URLs.')
        for number, url in enumerate(self.rss_urls, 1):
            async def update_rss(feed_url=url):
                content = await self.request(feed_url, json_response=False)
                baseline_key = 'rss_baseline:' + hashlib.sha256(feed_url.encode()).hexdigest()
                initialized = self.store.get_checkpoint(baseline_key, False)
                for item in parse_rss(content, urlparse(feed_url).hostname):
                    await self.save_news(item, alerts=initialized)
                self.store.set_checkpoint(baseline_key, True)
            await self.guarded('rss_' + str(number), update_rss, 'Configured RSS/Atom news feed')
        if self.injury_url:
            await self.guarded('injuries_configured', self.collect_injuries, 'Configured injury provider; source coverage applies')
        else:
            await asyncio.to_thread(self.store.feed_failure, 'injuries_configured',
                                    'Disabled: INJURY_FEED_URL not configured. News and box scores do not establish complete injury coverage.')
        if self.data_source != 'espn':
            await asyncio.to_thread(self.store.feed_failure, 'availability_coverage',
                                    'Unavailable: collected headlines/manual statistics are not a complete official injury report. Verify current player availability.')

    async def run(self):
        while True:
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Collector cycle failed')
                await asyncio.to_thread(self.store.feed_failure, 'collector', 'Collector cycle failed; check server logs.')
            await asyncio.sleep(self.next_delay())
