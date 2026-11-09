"""Budgeted PrizePicks lines from The Odds API's documented event-odds API.

This is a data-provider connection, not a PrizePicks account connection. Charged
requests run only when requested, never in the background service loop. Prices
reported for DFS selections are not probabilities or entry payout terms.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import hashlib
import json
import logging
import math
import os
import re
from collections import Counter

import httpx

from feeds import TEAM_WORDS, canonical_player, canonical_team
from master import normalized_name
from prizepicks_feed import (
    CACHE_SECONDS, EASTERN, FeedProblem, REQUEST_SECONDS,
    _game_signature, _now, _number, _roster, _timestamp,
)

SOURCE = 'https://api.the-odds-api.com'
EVENTS_PATH = '/v4/sports/basketball_nba/events'
STATUS_CHECKPOINT = 'the_odds_api_status'
SNAPSHOT_CHECKPOINT = 'the_odds_api_snapshot'
USAGE_CHECKPOINT = 'the_odds_api_usage'
MARKETS = {
    'player_points': ('pts', 'Points'),
    'player_rebounds': ('reb', 'Rebounds'),
    'player_assists': ('ast', 'Assists'),
    'player_threes': ('tpm', '3PTM'),
    'player_points_rebounds_assists': ('pra', 'PTS+REB+AST'),
}
MAX_CALL_CREDITS = len(MARKETS)
MAX_RESPONSE_BYTES = 2_000_000
MAX_MARKET_AGE_SECONDS = 300
DEFAULT_MONTHLY_LIMIT = 500


class _KeyRedactor(logging.Filter):
    def filter(self, record):
        # HTTPX logs request URLs at INFO. Provider auth is a query parameter.
        record.msg = re.sub(r'(?i)(apiKey=)[^&\s\"\'<>]+', r'\1[REDACTED]', record.getMessage())
        record.args = ()
        return True


def _protect_request_logs():
    for name in ('httpx', 'httpcore', 'httpcore.http11', 'httpcore.http2'):
        logger = logging.getLogger(name)
        if not any(isinstance(value, _KeyRedactor) for value in logger.filters):
            logger.addFilter(_KeyRedactor())


def _integer(value):
    try:
        if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _provider_team(name):
    """Use exact NBA names/abbreviations; substrings cannot match wrong clubs."""
    text = str(name or '').strip().casefold()
    if canonical_team(name) in TEAM_WORDS:
        return canonical_team(name)
    for team, nickname in TEAM_WORDS.items():
        if text == nickname or text.endswith(' ' + nickname):
            # The official provider uses full NBA team names. Verify the city as
            # well, so arbitrary text ending in "Knicks" is not accepted.
            names = {
                'ATL': 'Atlanta Hawks', 'BOS': 'Boston Celtics', 'BKN': 'Brooklyn Nets',
                'CHA': 'Charlotte Hornets', 'CHI': 'Chicago Bulls', 'CLE': 'Cleveland Cavaliers',
                'DAL': 'Dallas Mavericks', 'DEN': 'Denver Nuggets', 'DET': 'Detroit Pistons',
                'GSW': 'Golden State Warriors', 'HOU': 'Houston Rockets', 'IND': 'Indiana Pacers',
                'LAC': 'Los Angeles Clippers', 'LAL': 'Los Angeles Lakers', 'MEM': 'Memphis Grizzlies',
                'MIA': 'Miami Heat', 'MIL': 'Milwaukee Bucks', 'MIN': 'Minnesota Timberwolves',
                'NOP': 'New Orleans Pelicans', 'NYK': 'New York Knicks', 'OKC': 'Oklahoma City Thunder',
                'ORL': 'Orlando Magic', 'PHI': 'Philadelphia 76ers', 'PHX': 'Phoenix Suns',
                'POR': 'Portland Trail Blazers', 'SAC': 'Sacramento Kings', 'SAS': 'San Antonio Spurs',
                'TOR': 'Toronto Raptors', 'UTA': 'Utah Jazz', 'WAS': 'Washington Wizards',
            }
            return team if text == names[team].casefold() else None
    return None


def _event_matches(event, game, now):
    if not isinstance(event, dict) or event.get('sport_key') != 'basketball_nba':
        return False
    tip, expected = _timestamp(event.get('commence_time')), _timestamp(game.get('start_time'))
    return bool(tip and expected and tip == expected and tip > now and
                tip.astimezone(EASTERN).date().isoformat() == game.get('date') and
                _provider_team(event.get('home_team')) == canonical_team(game.get('home_team')) and
                _provider_team(event.get('away_team')) == canonical_team(game.get('away_team')) and
                game.get('status') == 'scheduled')


def parse_offers(document, game, store, now, retrieved_at, *, event_id=None):
    """Read only returned PrizePicks standard full-game selections."""
    if not _event_matches(document, game, now) or event_id and document.get('id') != event_id:
        raise FeedProblem('event_identity_mismatch')
    books = document.get('bookmakers')
    if not isinstance(books, list) or len(books) > 100:
        raise FeedProblem('invalid_provider_schema')
    selected = [book for book in books if isinstance(book, dict) and book.get('key') == 'prizepicks']
    if len(selected) > 1:
        raise FeedProblem('duplicate_bookmaker')
    if not selected:
        return [], [], [{'reason': 'no_prizepicks_markets', 'count': 1}], 0
    markets = selected[0].get('markets')
    if not isinstance(markets, list) or len(markets) > 200:
        raise FeedProblem('invalid_provider_schema')
    roster, rejected, grouped, raw_count, seen_markets = _roster(store, now), Counter(), {}, 0, set()
    teams = {canonical_team(game['home_team']), canonical_team(game['away_team'])}
    for market in markets:
        if not isinstance(market, dict):
            raise FeedProblem('invalid_provider_schema')
        key = market.get('key')
        if key not in MARKETS:
            rejected['unsupported_market'] += 1
            continue
        if key in seen_markets:
            raise FeedProblem('duplicate_market')
        seen_markets.add(key)
        stamp = _timestamp(market.get('last_update'))
        if not stamp or not -5 <= (now-stamp).total_seconds() <= MAX_MARKET_AGE_SECONDS:
            rejected['stale_or_unverified_market_clock'] += 1
            continue
        outcomes = market.get('outcomes')
        if not isinstance(outcomes, list) or len(outcomes) > 1000:
            raise FeedProblem('invalid_provider_schema')
        stat, label = MARKETS[key]
        for outcome in outcomes:
            raw_count += 1
            if not isinstance(outcome, dict):
                raise FeedProblem('invalid_provider_schema')
            side = {'Over': 'more', 'Under': 'less'}.get(outcome.get('name'))
            name = outcome.get('description')
            line = _number(outcome.get('point'))
            if not isinstance(name, str) or not name.strip() or len(name) > 150 or line is None or side is None:
                rejected['invalid_or_unknown_selection'] += 1
                continue
            current = roster.get(normalized_name(name))
            if not current or len(current['teams']) != 1 or not current['teams'] <= teams:
                rejected['current_player_team_unverified'] += 1
                continue
            # Explicit special/promo markers cannot be silently treated as a
            # standard offer. Omitted fields retain their unknown value.
            if outcome.get('is_promo') is True or market.get('is_promo') is True:
                rejected['promotion_needs_confirmation'] += 1
                continue
            team = next(iter(current['teams']))
            identity = (key, normalized_name(name), line)
            existing = grouped.get(identity)
            multiplier = _number(outcome.get('multiplier'))
            if existing is None:
                digest = hashlib.sha256(json.dumps([document['id'], *identity], separators=(',', ':')).encode()).hexdigest()[:32]
                existing = {
                    'projection_id': 'odds:' + digest, 'provider_event_id': document['id'],
                    'provider_player_id': canonical_player(current['name']), 'player': current['name'],
                    'team': team, 'opponent': next(value for value in teams if value != team),
                    'date': game['date'], 'game_id': game['id'], 'start_time': game['start_time'],
                    'line': line, 'stat': stat, 'stat_type': label, 'stat_display_name': label,
                    'market_key': key, 'projection_type': 'Full game', 'game_scope_verified': True,
                    'offer_variant': 'standard', 'is_promo': None, 'status': 'pre_game',
                    'provider_updated_at': stamp.isoformat(), 'source_market_updated_at': stamp.isoformat(),
                    'retrieved_at': retrieved_at, 'available_sides': [], 'selection_multipliers': {},
                    'source': SOURCE, 'source_kind': 'PrizePicks via The Odds API',
                    'offer_verified': False, 'requires_offer_confirmation': False,
                }
                grouped[identity] = existing
            if side in existing['available_sides']:
                raise FeedProblem('duplicate_or_conflicting_selection')
            existing['available_sides'].append(side)
            if multiplier is not None:
                existing['selection_multipliers'][side] = multiplier
    offers = []
    for identity in sorted(grouped):
        offer = grouped[identity]
        offer['available_sides'].sort()
        offers.append(offer)
    legs = [{**offer, 'side': side, 'player_id': canonical_player(offer['player'])}
            for offer in offers for side in offer['available_sides']]
    return offers, legs, [{'reason': key, 'count': value} for key, value in sorted(rejected.items())], raw_count


class TheOddsAPIFeed:
    """One coalesced on-demand reader, with a persistent free-plan credit cap."""
    def __init__(self, store, *, client=None, now=None, enabled=None, api_key=None, monthly_limit=None):
        self.store, self._client, self._clock = store, client, now
        from game_schedule_check import _metadata
        self._metadata = _metadata(store)
        self._owns_client = client is None
        raw_key = os.getenv('THE_ODDS_API_KEY', '') if api_key is None else api_key
        self._api_key = str(raw_key or '').strip()
        self.enabled = (os.getenv('PRIZEPICKS_FEED_ENABLED', '1') == '1') if enabled is None else bool(enabled)
        limit = _integer(os.getenv('THE_ODDS_API_MONTHLY_CREDIT_LIMIT', DEFAULT_MONTHLY_LIMIT) if monthly_limit is None else monthly_limit)
        self.monthly_limit = min(DEFAULT_MONTHLY_LIMIT, limit) if limit is not None else DEFAULT_MONTHLY_LIMIT
        self._lock, self._closed = asyncio.Lock(), False
        self._generation, self._http_status = 0, None
        self._response_headers = {}
        self._volatile_status = None
        _protect_request_logs()

    def _usage(self, now):
        month = now.astimezone(dt.timezone.utc).strftime('%Y-%m')
        value = self._metadata.get_checkpoint(USAGE_CHECKPOINT, {})
        if not isinstance(value, dict) or value.get('month') != month:
            return {'month': month, 'credits_reserved': 0, 'credits_remaining': None}
        return {'month': month, 'credits_reserved': _integer(value.get('credits_reserved')) or 0,
                'credits_remaining': _integer(value.get('credits_remaining')),
                'provider_credits_used': _integer(value.get('provider_credits_used')),
                'provider_last_request_credits': _integer(value.get('provider_last_request_credits'))}

    def status(self):
        saved = self._metadata.get_checkpoint(STATUS_CHECKPOINT, {})
        if not isinstance(saved, dict):
            saved = {}
        if self._volatile_status:
            saved = {**saved, **self._volatile_status}
        allowed = {'phase', 'checked_at', 'last_attempt', 'last_success_at', 'next_check_at',
                   'http_status', 'error_code', 'matched_offers', 'readable_selections',
                   'source_projection_count', 'game_id', 'game_date', 'provider_event_id'}
        result = {key: saved[key] for key in allowed if key in saved}
        phase = 'disabled' if not self.enabled else 'closed' if self._closed else 'not_configured' if not self._api_key else result.get('phase', 'not_checked')
        if phase == 'not_configured':
            result['error_code'] = 'api_key_missing'
        usage = self._usage(_now(self._clock))
        return {**result, **usage, 'phase': phase, 'provider': 'the_odds_api',
                'provider_label': 'The Odds API', 'source': SOURCE, 'enabled': self.enabled,
                'configured': bool(self._api_key), 'monthly_credit_limit': self.monthly_limit,
                'local_credits_reserved': usage['credits_reserved'],
                'max_credits_per_refresh': MAX_CALL_CREDITS, 'background_paid_polling': False}

    def _game(self, game, now):
        from independent_picks import _select_game
        selected, _ = _select_game(self.store, game.get('date') if isinstance(game, dict) else None, 'NYK', now)
        if game is not None and (not selected or selected.get('id') != game.get('id')):
            return None
        return selected

    def offers_for_game(self, game=None, now=None):
        now = _now(now or self._clock)
        game, status = self._game(game, now), self.status()
        snapshot = self._metadata.get_checkpoint(SNAPSHOT_CHECKPOINT, {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        stamp = _timestamp(snapshot.get('retrieved_at'))
        tip = _timestamp(game.get('start_time')) if game else None
        fresh = bool(self.enabled and self._api_key and not self._closed and game and tip and tip > now and
                     game.get('status') == 'scheduled' and snapshot.get('game') == _game_signature(game) and
                     status.get('phase') in {'ready', 'no_offers'} and status.get('game_id') == game.get('id') and
                     stamp and -5 <= (now-stamp).total_seconds() <= CACHE_SECONDS)
        offers = snapshot.get('offers', []) if fresh else []
        before_count = len(offers)
        # A cached market's own clock can expire before the retrieval TTL does.
        offers = [offer for offer in offers if isinstance(offer, dict) and
                  (clock := _timestamp(offer.get('source_market_updated_at'))) and
                  -5 <= (now-clock).total_seconds() <= MAX_MARKET_AGE_SECONDS]
        ids = {offer.get('projection_id') for offer in offers}
        legs = [leg for leg in snapshot.get('legs', []) if isinstance(leg, dict) and leg.get('projection_id') in ids] if fresh else []
        exclusions = list(snapshot.get('exclusions', [])) if fresh else []
        if before_count > len(offers):
            exclusions.append({'reason': 'stale_or_unverified_market_clock', 'count': before_count-len(offers)})
            status = {**status, 'matched_offers': len(offers), 'readable_selections': len(legs)}
            if not offers:
                fresh = False
                status = {**status, 'phase': 'stale', 'error_code': 'stale_market_cache'}
        return {'game': game, 'fresh': fresh, 'offers': offers, 'legs': legs,
                'exclusions': exclusions, 'status': status}

    async def _save(self, key, value):
        from game_schedule_check import _database_write, _metadata
        await _database_write(_metadata(self.store).set_checkpoint, key, value)

    async def _request(self, path, params=None):
        # Transport failure on the second request must not inherit the free
        # event lookup's HTTP 200 or quota headers.
        self._http_status, self._response_headers = None, {}
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_SECONDS, follow_redirects=False,
                headers={'Accept': 'application/json', 'User-Agent': 'KnicksResearchBot/1.0'})
        async with self._client.stream('GET', SOURCE + path,
                params={'apiKey': self._api_key, 'dateFormat': 'iso', **(params or {})}) as response:
            self._http_status = response.status_code
            self._response_headers = {key: response.headers.get(key) for key in (
                'x-requests-remaining', 'x-requests-used', 'x-requests-last')}
            if response.status_code != 200:
                code = ('provider_credits_exhausted' if _integer(response.headers.get('x-requests-remaining')) == 0
                        else {401: 'invalid_api_key', 403: 'access_refused', 429: 'rate_limited'}.get(response.status_code, 'http_error'))
                retry = None
                if response.status_code == 429:
                    raw = response.headers.get('retry-after', '')
                    try:
                        retry = float(raw)
                    except ValueError:
                        try:
                            retry = (email.utils.parsedate_to_datetime(raw)-_now(self._clock)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                raise FeedProblem(code, http_status=response.status_code, retry_seconds=retry)
            try:
                age = float(response.headers.get('age', '0'))
            except ValueError:
                raise FeedProblem('invalid_response_age') from None
            if not math.isfinite(age) or age < 0 or age > CACHE_SECONDS:
                raise FeedProblem('stale_source_response')
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise FeedProblem('response_size_limit')
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise FeedProblem('invalid_json_response') from None

    async def _record_quota(self, usage):
        usage = dict(usage)
        for header, key in (('x-requests-remaining', 'credits_remaining'),
                            ('x-requests-used', 'provider_credits_used'),
                            ('x-requests-last', 'provider_last_request_credits')):
            value = _integer(self._response_headers.get(header))
            if value is not None:
                usage[key] = value
        await self._save(USAGE_CHECKPOINT, usage)
        return usage

    async def ensure_for_game(self, game):
        return await self.refresh(game=game)

    async def refresh(self, force=False, game=None):
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                return await self._refresh(force=force, game=game)
        except TimeoutError:
            now = _now(self._clock)
            self._volatile_status = {'phase': 'unavailable', 'error_code': 'request_timeout',
                'checked_at': now.isoformat(), 'next_check_at': (now+dt.timedelta(seconds=300)).isoformat(),
                'http_status': None, 'matched_offers': 0, 'readable_selections': 0,
                'source_projection_count': 0, 'provider_event_id': None}
            # Do not wait for another database operation after the deadline.
            # The persisted pre-request reservation already accounts for a
            # possibly charged call; the local cooldown prevents retry storms.
            return {**self._volatile_status, 'provider': 'the_odds_api',
                    'provider_label': 'The Odds API', 'source': SOURCE,
                    'configured': bool(self._api_key), 'enabled': self.enabled}

    async def _refresh(self, force=False, game=None):
        if not self.enabled or not self._api_key or self._closed:
            return await asyncio.to_thread(self.status)
        generation = self._generation
        async with self._lock:
            now = _now(self._clock)
            old = await asyncio.to_thread(self.status)
            selected = await asyncio.to_thread(self._game, game, now)
            if self._generation != generation and selected and old.get('game_id') == selected.get('id'):
                return {**old, 'coalesced': True}
            until = _timestamp(old.get('next_check_at'))
            # Manual force cannot spend another five credits within the cache
            # window, and cannot override refusal/quota retry times.
            if until and until > now:
                return {**old, 'cooldown': True}
            self._volatile_status = None
            tip = _timestamp(selected.get('start_time')) if selected else None
            if not selected or not tip or tip <= now or selected.get('status') != 'scheduled':
                value = {'phase': 'no_saved_game', 'checked_at': now.isoformat(),
                         'last_success_at': old.get('last_success_at'), 'matched_offers': 0,
                         'readable_selections': 0, 'game_id': None, 'game_date': None,
                         'next_check_at': (now+dt.timedelta(seconds=CACHE_SECONDS)).isoformat()}
                await self._save(STATUS_CHECKPOINT, value)
                return await asyncio.to_thread(self.status)
            base = {'last_attempt': now.isoformat(), 'checked_at': now.isoformat(),
                    'last_success_at': old.get('last_success_at'), 'game_id': selected['id'],
                    'game_date': selected['date'], 'matched_offers': 0, 'readable_selections': 0}
            usage = await asyncio.to_thread(self._usage, now)
            next_month = (now.astimezone(dt.timezone.utc).replace(day=28)+dt.timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            self._generation += 1
            self._http_status, self._response_headers = None, {}
            try:
                if usage['credits_reserved'] + MAX_CALL_CREDITS > self.monthly_limit:
                    raise FeedProblem('credit_budget_exhausted')
                async with asyncio.timeout(REQUEST_SECONDS):
                    events = await self._request(EVENTS_PATH)
                    usage = await self._record_quota(usage)
                    if not isinstance(events, list) or len(events) > 500:
                        raise FeedProblem('invalid_provider_schema')
                    matches = [event for event in events if _event_matches(event, selected, now)]
                    if len(matches) != 1:
                        raise FeedProblem('event_identity_unverified' if not matches else 'event_identity_ambiguous')
                    event_id = matches[0].get('id')
                    if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', event_id):
                        raise FeedProblem('invalid_provider_event_id')
                    if usage.get('credits_remaining') is not None and usage['credits_remaining'] < MAX_CALL_CREDITS:
                        raise FeedProblem('provider_credits_exhausted')
                    # A reserved call remains charged locally even if the
                    # transport dies after the provider has consumed credits.
                    usage = {**usage, 'credits_reserved': usage['credits_reserved'] + MAX_CALL_CREDITS}
                    await self._save(USAGE_CHECKPOINT, usage)
                    document = await self._request(EVENTS_PATH + '/' + event_id + '/odds', {
                        'bookmakers': 'prizepicks', 'markets': ','.join(MARKETS),
                        'oddsFormat': 'american', 'includeMultipliers': 'true'})
                    usage = await self._record_quota(usage)
                    completed_at = _now(self._clock)
                    offers, legs, exclusions, count = await asyncio.to_thread(
                        parse_offers, document, selected, self.store, completed_at,
                        completed_at.isoformat(), event_id=event_id)
                snapshot = {'game_id': selected['id'], 'game': _game_signature(selected),
                            'provider_event_id': event_id, 'retrieved_at': completed_at.isoformat(),
                            'source': SOURCE, 'offers': offers, 'legs': legs, 'exclusions': exclusions}
                await self._save(SNAPSHOT_CHECKPOINT, snapshot)
                value = {**base, 'phase': 'ready' if offers else 'no_offers', 'http_status': 200,
                         'checked_at': completed_at.isoformat(),
                         'provider_event_id': event_id, 'last_success_at': completed_at.isoformat(),
                         'matched_offers': len(offers), 'readable_selections': len(legs),
                         'source_projection_count': count,
                         'next_check_at': (completed_at+dt.timedelta(seconds=CACHE_SECONDS)).isoformat()}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._record_quota(usage)
                code = error.code if isinstance(error, FeedProblem) else 'request_timeout' if isinstance(error, (TimeoutError, httpx.TimeoutException)) else 'connection_failed' if isinstance(error, httpx.HTTPError) else 'feed_validation_failed'
                status = error.http_status if isinstance(error, FeedProblem) and error.http_status is not None else self._http_status
                phase = {'invalid_api_key': 'not_authorized', 'access_refused': 'blocked',
                         'credit_budget_exhausted': 'budget_exhausted',
                         'provider_credits_exhausted': 'quota_exhausted',
                         'rate_limited': 'rate_limited'}.get(code, 'unavailable')
                delay = 900 if phase in {'blocked', 'not_authorized'} else 300
                retry_at = next_month if phase == 'budget_exhausted' else now+dt.timedelta(seconds=3600 if phase == 'quota_exhausted' else delay)
                retry = error.retry_seconds if isinstance(error, FeedProblem) else None
                if retry is not None and retry > delay:
                    try:
                        retry_at = now+dt.timedelta(seconds=retry) if math.isfinite(retry) else next_month
                    except OverflowError:
                        retry_at = dt.datetime.max.replace(tzinfo=now.tzinfo)
                value = {**base, 'phase': phase, 'http_status': status, 'error_code': code,
                         'next_check_at': retry_at.isoformat()}
            await self._save(STATUS_CHECKPOINT, value)
            return await asyncio.to_thread(self.status)

    async def run(self):
        # Deliberately do not poll a credit-metered API. The bot's /picks and
        # /pp_refresh commands trigger coalesced requests on demand.
        while not self._closed:
            await asyncio.sleep(60)

    async def aclose(self):
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
