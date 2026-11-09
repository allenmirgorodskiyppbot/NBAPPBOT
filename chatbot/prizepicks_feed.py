"""Read-only public PrizePicks offers, separate from account access and entries.

The website endpoint is unofficial. A refusal or schema change stays visible;
unknown offer directions never become invented More/Less selections.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import json
import math
import os
from collections import Counter
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from feeds import canonical_player, canonical_team, TEAM_WORDS
from master import normalized_name
from news_digest import _aware
from pick_discussion import normalize_stat

SOURCE = 'https://api.prizepicks.com/projections'
STATUS_CHECKPOINT = 'prizepicks_feed_status'
SNAPSHOT_CHECKPOINT = 'prizepicks_feed_snapshot'
CACHE_SECONDS = 90
POLL_SECONDS = 60
REQUEST_SECONDS = 8
MAX_PAGES = 16
PER_PAGE = 250
MAX_RESPONSE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 6_000_000
EASTERN = ZoneInfo('America/New_York')


class FeedProblem(Exception):
    def __init__(self, code, *, http_status=None, retry_seconds=None):
        super().__init__(code)
        self.code, self.http_status, self.retry_seconds = code, http_status, retry_seconds


def _now(value=None):
    value = value() if callable(value) else value
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _game_signature(game):
    if not isinstance(game, dict):
        return None
    return {key: game.get(key) for key in ('id', 'date', 'home_team', 'away_team', 'start_time', 'status')}


def _resource(value, key):
    relationships = value.get('relationships')
    rel = relationships.get(key) if isinstance(relationships, dict) else None
    data = rel.get('data') if isinstance(rel, dict) else None
    if (not isinstance(data, dict) or not isinstance(data.get('id'), (str, int))
            or len(str(data.get('id'))) > 256 or len(str(data.get('type') or key)) > 80):
        return None
    return str(data.get('type') or key), str(data['id'])


def _directions(attributes):
    """Only explicit direction fields establish available selections."""
    groups = []
    for key in ('available_sides', 'available_directions', 'sides'):
        if key not in attributes:
            continue
        values = attributes[key]
        if not isinstance(values, list):
            return []
        normalized = []
        for value in values:
            text = value.get('side') if isinstance(value, dict) else value
            text = str(text or '').strip().lower()
            side = {'more': 'more', 'over': 'more', 'less': 'less', 'under': 'less'}.get(text)
            if side is None:
                return []
            normalized.append(side)
        groups.append(set(normalized))
    if not groups or any(group != groups[0] for group in groups[1:]):
        return []
    return sorted(groups[0])


def _roster(store, now):
    """Resolve latest recent team identity, retaining same-day conflicts."""
    upper = now.astimezone(EASTERN).date()
    lower = (upper - dt.timedelta(days=14)).isoformat()
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            'SELECT player,team,date FROM player_games WHERE date>=? AND date<=? '
            'ORDER BY date DESC,updated_at DESC', (lower, upper.isoformat()))]
    result = {}
    for row in rows:
        key = normalized_name(row['player'])
        old = result.get(key)
        if old is None:
            result[key] = {'name': row['player'], 'date': row['date'], 'teams': {canonical_team(row['team'])}}
        elif row['date'] == old['date']:
            old['teams'].add(canonical_team(row['team']))
    return result


def parse_offers(pages, game, store, now, retrieved_at):
    """Join JSON:API resources and match only this exact saved NBA fixture."""
    resources, projections = {}, {}
    for page in pages:
        for resource in page.get('included', []):
            if (not isinstance(resource, dict) or not resource.get('type') or resource.get('id') is None
                    or len(str(resource.get('id'))) > 256 or len(str(resource.get('type'))) > 80):
                raise FeedProblem('invalid_included_resource')
            key = str(resource['type']), str(resource['id'])
            if key in resources and resources[key] != resource:
                raise FeedProblem('conflicting_included_resource')
            resources[key] = resource
        for projection in page['data']:
            if (not isinstance(projection, dict) or projection.get('id') is None
                    or len(str(projection.get('id'))) > 256):
                raise FeedProblem('invalid_projection_resource')
            key = str(projection['id'])
            if key in projections:
                raise FeedProblem('duplicate_projection_across_pages')
            projections[key] = projection
    nba_leagues = {key for key, value in resources.items() if key[0] == 'league'
                   and str((value.get('attributes') or {}).get('name') or '').strip().upper() == 'NBA'}
    if not nba_leagues:
        raise FeedProblem('nba_league_identity_unverified')
    roster = _roster(store, now)
    teams = {canonical_team(game['home_team']), canonical_team(game['away_team'])}
    tip = _aware(game.get('start_time'))
    if (len(teams) != 2 or not teams <= set(TEAM_WORDS) or not tip or tip <= now
            or tip.astimezone(EASTERN).date().isoformat() != game.get('date')):
        raise FeedProblem('saved_matchup_unavailable')
    rejected, offers, legs = Counter(), [], []
    for identity, projection in projections.items():
        attrs = projection.get('attributes')
        if not isinstance(attrs, dict) or _resource(projection, 'league') not in nba_leagues:
            rejected['unverified_nba_projection'] += 1
            continue
        player_ref = _resource(projection, 'new_player')
        player = resources.get(player_ref, {})
        player_attrs = player.get('attributes') or {}
        name = str(player_attrs.get('name') or '').strip()
        team = canonical_team(player_attrs.get('team'))
        current = roster.get(normalized_name(name))
        if not name or team not in teams:
            rejected['other_or_unknown_team'] += 1
            continue
        if not current or current['teams'] != {team}:
            rejected['current_player_team_unverified'] += 1
            continue
        start = _timestamp(attrs.get('start_time'))
        if not start or start != tip or start <= now:
            rejected['event_clock_mismatch'] += 1
            continue
        status = str(attrs.get('status') or '').strip().lower()
        if status not in {'pre_game', 'pregame', 'scheduled', 'active', 'open'}:
            rejected['inactive_or_unknown_source_status'] += 1
            continue
        line = _number(attrs.get('line_score'))
        updated = _timestamp(attrs.get('updated_at')) if attrs.get('updated_at') else None
        if (line is None or attrs.get('updated_at') and not updated
                or updated and updated > now + dt.timedelta(minutes=5)):
            rejected['invalid_line_or_source_clock'] += 1
            continue
        variant = str(attrs.get('odds_type') or '').strip().lower()
        if variant not in {'standard', 'goblin', 'demon'}:
            variant = 'unknown'
        stat_type = str(attrs.get('stat_type') or attrs.get('stat_display_name') or '').strip()[:120]
        display_name = str(attrs.get('stat_display_name') or stat_type).strip()[:120]
        stat = normalize_stat(stat_type)
        if normalize_stat(display_name) != stat:
            stat = None
        projection_type = str(attrs.get('projection_type') or '').strip()[:120]
        scope = projection_type.lower().replace('_', ' ').replace('-', ' ')
        scope_known = scope in {'single stat', 'standard', 'full game'}
        for field in ('period', 'game_segment', 'segment', 'market_period', 'game_scope'):
            if field in attrs and str(attrs[field]).strip().lower().replace('_', ' ') not in {'full', 'full game', 'game'}:
                scope_known = False
        promo_known = attrs.get('is_promo') is False
        sides = _directions(attrs)
        opponent = next(value for value in teams if value != team)
        event_ref = _resource(projection, 'event')
        event = resources.get(event_ref, {}) if event_ref else {}
        event_attrs = event.get('attributes') or {}
        declared_opponent = canonical_team(event_attrs.get('opponent') or attrs.get('opponent'))
        if declared_opponent and declared_opponent != opponent:
            rejected['event_opponent_mismatch'] += 1
            continue
        offer = {'projection_id': identity, 'provider_player_id': player_ref[1],
            'player': current['name'], 'team': team, 'opponent': opponent,
            'date': game['date'], 'game_id': game['id'], 'line': line,
            'stat': stat, 'stat_type': stat_type, 'stat_display_name': display_name,
            'is_promo': attrs.get('is_promo') if type(attrs.get('is_promo')) is bool else None,
            'projection_type': projection_type, 'game_scope_verified': scope_known,
            'offer_variant': variant,
            'status': status, 'start_time': start.isoformat(),
            'provider_updated_at': updated.isoformat() if updated else None,
            'retrieved_at': retrieved_at, 'available_sides': sides,
            'source': SOURCE, 'source_kind': 'PrizePicks public website feed',
            'offer_verified': False, 'requires_offer_confirmation': not bool(
                sides and stat and variant != 'unknown' and promo_known and scope_known)}
        offers.append(offer)
        if offer['requires_offer_confirmation']:
            rejected['offer_terms_need_confirmation'] += 1
            continue
        for side in sides:
            legs.append({**offer, 'side': side, 'player_id': canonical_player(current['name'])})
    return offers, legs, [{'reason': reason, 'count': count} for reason, count in sorted(rejected.items())], len(projections)


def _pagination(document, page):
    if (not isinstance(document, dict) or not isinstance(document.get('data'), list)
            or not isinstance(document.get('included', []), list) or len(document['data']) > 1000
            or len(document.get('included', [])) > 10000):
        raise FeedProblem('invalid_json_api_schema')
    meta = document.get('meta')
    if isinstance(meta, dict) and 'total_pages' in meta:
        total = meta['total_pages']
        current = meta.get('current_page', page)
        if type(total) is not int or type(current) is not int or current != page or total < 0:
            raise FeedProblem('invalid_pagination_metadata')
        if total > MAX_PAGES:
            raise FeedProblem('pagination_limit_exceeded')
        if total == 0:
            if document['data'] or page != 1:
                raise FeedProblem('invalid_empty_pagination')
            return False, total
        if page > total:
            raise FeedProblem('pagination_page_exceeds_total')
        return page < total, total
    links = document.get('links')
    if isinstance(links, dict) and 'next' in links:
        next_url = links['next']
        if next_url is None:
            return False, page
        parsed = urlsplit(str(next_url))
        query = parse_qs(parsed.query)
        if (parsed.scheme != 'https' or parsed.netloc != 'api.prizepicks.com'
                or parsed.path != '/projections' or query.get('page') != [str(page + 1)]):
            raise FeedProblem('unsafe_or_incoherent_pagination')
        if page >= MAX_PAGES:
            raise FeedProblem('pagination_limit_exceeded')
        return True, None
    raise FeedProblem('pagination_completeness_unverified')


class PrizePicksFeed:
    """One supervised, bounded public-data reader owned by the bot process."""
    def __init__(self, store, *, client=None, now=None, enabled=None):
        self.store, self._client, self._clock = store, client, now
        self._owns_client = client is None
        self.enabled = (os.getenv('PRIZEPICKS_FEED_ENABLED', '1') == '1') if enabled is None else bool(enabled)
        self._lock = asyncio.Lock()
        self._closed = False
        self._response_http_status = None
        self._generation = 0

    def status(self):
        value = self.store.get_checkpoint(STATUS_CHECKPOINT, {}) or {}
        if not isinstance(value, dict):
            value = {}
        allowed = {'phase', 'checked_at', 'last_attempt', 'last_success_at', 'next_check_at',
                   'http_status', 'error_code', 'detail', 'matched_offers', 'readable_selections',
                   'source_projection_count', 'game_id', 'game_date'}
        result = {key: value[key] for key in allowed if key in value}
        return {**result, 'enabled': self.enabled, 'source': SOURCE,
                'phase': 'disabled' if not self.enabled else 'closed' if self._closed else result.get('phase', 'not_checked')}

    def _game(self, game, now):
        from independent_picks import _select_game
        selected, _ = _select_game(self.store, game.get('date') if isinstance(game, dict) else None, 'NYK', now)
        if game is not None and (not selected or selected.get('id') != game.get('id')):
            return None
        return selected

    def offers_for_game(self, game=None, now=None):
        now = _now(now or self._clock)
        game = self._game(game, now)
        status = self.status()
        snapshot = self.store.get_checkpoint(SNAPSHOT_CHECKPOINT, {}) or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        stamp = _aware(snapshot.get('retrieved_at'))
        tip = _aware(game.get('start_time')) if game else None
        fresh = bool(self.enabled and game and tip and tip > now and
                     status.get('phase') in {'ready', 'no_offers'} and
                     game.get('status') == 'scheduled' and
                     snapshot.get('game') == _game_signature(game) and
                     status.get('game_id') == game.get('id') and stamp and
                     -5 <= (now-stamp).total_seconds() <= CACHE_SECONDS)
        return {'game': game, 'fresh': fresh, 'offers': snapshot.get('offers', []) if fresh else [],
                'legs': snapshot.get('legs', []) if fresh else [],
                'exclusions': snapshot.get('exclusions', []) if fresh else [], 'status': status}

    async def _save(self, key, value):
        from game_schedule_check import _database_write, _metadata
        await _database_write(_metadata(self.store).set_checkpoint, key, value)

    async def _request_pages(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_SECONDS, follow_redirects=False,
                headers={'Accept': 'application/json', 'User-Agent': 'KnicksResearchBot/1.0'})
        pages, total_bytes, expected_total, expected_count = [], 0, None, None
        for number in range(1, MAX_PAGES + 1):
            async with self._client.stream('GET', SOURCE,
                    params={'league_id': '7', 'per_page': str(PER_PAGE), 'single_stat': 'true', 'page': str(number)}) as response:
                self._response_http_status = response.status_code
                if response.status_code != 200:
                    retry = None
                    if response.status_code == 429:
                        raw = response.headers.get('retry-after', '')
                        try:
                            retry = float(raw)
                        except ValueError:
                            try:
                                stamp = email.utils.parsedate_to_datetime(raw)
                                retry = (stamp - _now(self._clock)).total_seconds()
                            except (ValueError, TypeError, OverflowError):
                                pass
                    code = 'access_refused' if response.status_code in {401, 403} else 'rate_limited' if response.status_code == 429 else 'http_error'
                    raise FeedProblem(code, http_status=response.status_code, retry_seconds=retry)
                try:
                    age = float(response.headers.get('age', '0'))
                except ValueError:
                    raise FeedProblem('invalid_response_age') from None
                if not math.isfinite(age) or age > CACHE_SECONDS or age < 0:
                    raise FeedProblem('stale_source_response')
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    total_bytes += len(part)
                    if len(content) > MAX_RESPONSE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                        raise FeedProblem('response_size_limit')
            try:
                document = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                raise FeedProblem('invalid_json_response') from None
            more, total = _pagination(document, number)
            meta = document.get('meta') or {}
            if not isinstance(meta, dict):
                raise FeedProblem('invalid_pagination_metadata')
            for key in ('total_count', 'total_projections'):
                if key in meta:
                    count = meta[key]
                    if type(count) is not int or count < 0 or expected_count is not None and count != expected_count:
                        raise FeedProblem('pagination_count_changed_during_read')
                    expected_count = count
            if expected_total is not None and total is not None and total != expected_total:
                raise FeedProblem('pagination_changed_during_read')
            if total is not None:
                expected_total = total
            pages.append(document)
            if not more:
                if expected_count is not None and sum(len(page['data']) for page in pages) != expected_count:
                    raise FeedProblem('pagination_count_incomplete')
                return pages
        raise FeedProblem('pagination_limit_exceeded')

    async def refresh(self, force=False, game=None):
        if not self.enabled or self._closed:
            return self.status()
        initial_generation = self._generation
        waited = self._lock.locked()
        async with self._lock:
            now = _now(self._clock)
            old = self.status()
            selected = self._game(game, now)
            if ((waited or self._generation != initial_generation) and selected
                    and old.get('game_id') == selected.get('id')):
                return {**old, 'coalesced': True}
            until = _aware(old.get('next_check_at'))
            if until and until > now and (not force or old.get('phase') in {'blocked', 'rate_limited'}):
                return {**old, 'cooldown': True}
            if (not selected or selected.get('status') != 'scheduled'
                    or not _aware(selected.get('start_time')) or _aware(selected['start_time']) <= now):
                status = {'phase': 'no_saved_game', 'checked_at': now.isoformat(),
                          'last_success_at': old.get('last_success_at'), 'matched_offers': 0,
                          'readable_selections': 0, 'game_id': None, 'game_date': None,
                          'next_check_at': (now+dt.timedelta(seconds=POLL_SECONDS)).isoformat(),
                          'detail': 'No saved upcoming Knicks matchup is available.'}
                await self._save(STATUS_CHECKPOINT, status)
                return self.status()
            base = {'last_attempt': now.isoformat(), 'checked_at': now.isoformat(),
                    'last_success_at': old.get('last_success_at'), 'game_id': selected['id'],
                    'game_date': selected['date'], 'matched_offers': 0, 'readable_selections': 0}
            self._response_http_status = None
            self._generation += 1
            try:
                async with asyncio.timeout(REQUEST_SECONDS):
                    pages = await self._request_pages()
                    offers, legs, exclusions, count = await asyncio.to_thread(
                        parse_offers, pages, selected, self.store, now, now.isoformat())
                snapshot = {'game_id': selected['id'], 'game': _game_signature(selected),
                            'retrieved_at': now.isoformat(),
                            'source': SOURCE, 'offers': offers, 'legs': legs, 'exclusions': exclusions}
                await self._save(SNAPSHOT_CHECKPOINT, snapshot)
                status = {**base, 'phase': 'ready' if offers else 'no_offers', 'http_status': 200,
                          'last_success_at': now.isoformat(), 'matched_offers': len(offers),
                          'readable_selections': len(legs), 'source_projection_count': count,
                          'next_check_at': (now+dt.timedelta(seconds=POLL_SECONDS)).isoformat(),
                          'detail': 'Public feed read; offer directions remain unknown unless explicitly supplied.'}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, FeedProblem):
                    code, http_status = error.code, (error.http_status if error.http_status is not None
                                                    else self._response_http_status)
                    retry = error.retry_seconds
                else:
                    code = 'request_timeout' if isinstance(error, (TimeoutError, httpx.TimeoutException)) else 'connection_failed' if isinstance(error, httpx.HTTPError) else 'feed_validation_failed'
                    http_status, retry = self._response_http_status, None
                phase = 'blocked' if code == 'access_refused' else 'rate_limited' if code == 'rate_limited' else 'partial' if 'pagination' in code else 'schema_unavailable' if isinstance(error, FeedProblem) and http_status in (None, 200) else 'unavailable'
                delay = 900 if phase == 'blocked' else 300
                retry_at = now+dt.timedelta(seconds=delay)
                if retry is not None:
                    if not math.isfinite(retry):
                        retry_at = now+dt.timedelta(days=1)
                    elif retry > delay:
                        try:
                            retry_at = now+dt.timedelta(seconds=retry)
                        except OverflowError:
                            retry_at = dt.datetime.max.replace(tzinfo=now.tzinfo)
                status = {**base, 'phase': phase, 'http_status': http_status, 'error_code': code,
                          'next_check_at': retry_at.isoformat(),
                          'detail': 'The public feed could not be verified. Saved master research and uploads remain available.'}
            await self._save(STATUS_CHECKPOINT, status)
            return self.status()

    async def run(self):
        while not self._closed:
            await self.refresh()
            await asyncio.sleep(POLL_SECONDS)

    async def aclose(self):
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
