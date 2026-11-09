"""Documented provider transport and free-plan controls, using synthetic data."""
import asyncio
import copy
import datetime as dt
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from feeds import canonical_game
from master import MasterStore
import the_odds_api_feed as provider

NOW = dt.datetime(2026, 11, 9, 17, tzinfo=dt.timezone.utc)
DATE = '2026-11-10'
TIP = '2026-11-11T00:30:00+00:00'
KEY = 'synthetic-private-provider-key'


def event(**values):
    return {'id': 'synthetic_event_1', 'sport_key': 'basketball_nba', 'commence_time': TIP,
            'home_team': 'New York Knicks', 'away_team': 'Milwaukee Bucks', **values}


def document(**values):
    result = event(bookmakers=[{'key': 'prizepicks', 'markets': [
        {'key': 'player_points', 'last_update': NOW.isoformat(), 'outcomes': [
            {'name': 'Over', 'description': 'Jalen Brunson', 'point': 25.5, 'price': -119},
            {'name': 'Under', 'description': 'Jalen Brunson', 'point': 25.5, 'price': -119}]},
        {'key': 'player_rebounds', 'last_update': NOW.isoformat(), 'outcomes': [
            {'name': 'Over', 'description': 'Giannis Antetokounmpo', 'point': 9.5, 'price': -119}]}]}])
    return {**result, **values}


class TheOddsAPIFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'master.sqlite')
        self.clock = [NOW]
        self.game = {'id': canonical_game(DATE, 'NYK', 'MIL'), 'date': DATE,
            'home_team': 'NYK', 'away_team': 'MIL', 'status': 'scheduled', 'start_time': TIP,
            'source': 'SYNTHETIC matchup'}
        self.store.upsert_game(self.game)
        for player, team, opponent in [('Jalen Brunson', 'NYK', 'CLE'),
                                       ('Giannis Antetokounmpo', 'MIL', 'CHI')]:
            prior = canonical_game('2026-11-07', team, opponent)
            self.store.upsert_game({'id': prior, 'date': '2026-11-07', 'home_team': team,
                'away_team': opponent, 'status': 'final', 'source': 'SYNTHETIC player control'})
            self.store.upsert_player_game({'game_id': prior, 'date': '2026-11-07',
                'player': player, 'team': team, 'opponent': opponent,
                'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
                'minutes': 33, 'pts': 25, 'reb': 10, 'source': 'SYNTHETIC player control'})

    def feed(self, handler=None, **kwargs):
        calls = []
        def respond(request):
            calls.append(request)
            if handler:
                return handler(request)
            payload = [event()] if request.url.path == provider.EVENTS_PATH else document()
            return httpx.Response(200, json=payload,
                headers={'x-requests-remaining': '495', 'x-requests-used': '5', 'x-requests-last': '5'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(client.aclose)
        feed = provider.TheOddsAPIFeed(self.store, client=client, now=lambda: self.clock[0],
                                      api_key=kwargs.pop('api_key', KEY), **kwargs)
        self.addAsyncCleanup(feed.aclose)
        return feed, calls

    def protected(self):
        with self.store.connect() as db:
            return {table: [list(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ('games', 'player_games', 'entries', 'boards', 'news', 'grade_events')}

    async def test_documented_two_request_flow_matches_sides_and_preserves_master(self):
        feed, calls = self.feed()
        before = self.protected()
        status = await feed.refresh()
        self.assertEqual(status['phase'], 'ready')
        self.assertEqual(status['matched_offers'], 2)
        self.assertEqual(status['readable_selections'], 3)
        self.assertEqual(status['local_credits_reserved'], 5)
        self.assertEqual(status['credits_remaining'], 495)
        self.assertEqual([request.url.host for request in calls], ['api.the-odds-api.com'] * 2)
        self.assertEqual(calls[0].url.path, provider.EVENTS_PATH)
        self.assertEqual(calls[1].url.params['bookmakers'], 'prizepicks')
        self.assertEqual(set(calls[1].url.params['markets'].split(',')), set(provider.MARKETS))
        report = feed.offers_for_game()
        self.assertTrue(report['fresh'])
        self.assertEqual({(leg['player'], leg['side']) for leg in report['legs']}, {
            ('Jalen Brunson', 'more'), ('Jalen Brunson', 'less'), ('Giannis Antetokounmpo', 'more')})
        self.assertEqual(self.protected(), before)
        self.assertNotIn(KEY, json.dumps(status))
        for offer in report['offers']:
            self.assertNotIn('price', offer)
            self.assertFalse(offer['offer_verified'])
            self.assertEqual(offer['source_market_updated_at'], NOW.isoformat())
            self.assertEqual(offer['source_kind'], 'PrizePicks via The Odds API')

    async def test_missing_key_and_disabled_reader_make_no_requests(self):
        for kwargs in ({'api_key': ''}, {'enabled': False}):
            with self.subTest(kwargs=kwargs):
                feed, calls = self.feed(**kwargs)
                status = await feed.refresh(force=True)
                self.assertIn(status['phase'], {'not_configured', 'disabled'})
                self.assertEqual(calls, [])
                self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_concurrent_picks_and_manual_refresh_share_one_charged_snapshot(self):
        feed, calls = self.feed()
        statuses = await asyncio.gather(*(feed.refresh(force=True) for _ in range(8)))
        self.assertTrue(all(status['phase'] == 'ready' for status in statuses))
        self.assertEqual(len(calls), 2)
        await feed.refresh(force=True)
        await feed.ensure_for_game(self.game)
        self.assertEqual(len(calls), 2)
        self.assertEqual(feed.status()['local_credits_reserved'], 5)

    async def test_budget_reservation_survives_reader_restart_and_never_overruns(self):
        first, calls = self.feed(monthly_limit=5)
        await first.refresh()
        await first.aclose()
        self.clock[0] += dt.timedelta(seconds=91)
        second, new_calls = self.feed(monthly_limit=5)
        status = await second.refresh(force=True)
        self.assertEqual(status['phase'], 'budget_exhausted')
        self.assertEqual(status['local_credits_reserved'], 5)
        self.assertEqual(status['next_check_at'], '2026-12-01T00:00:00+00:00')
        self.assertEqual(new_calls, [])
        self.assertEqual(len(calls), 2)

    async def test_provider_quota_uses_free_check_then_stops_before_charged_request(self):
        remaining = ['0']
        def handler(request):
            payload = [event()] if request.url.path == provider.EVENTS_PATH else document()
            return httpx.Response(200, json=payload, headers={'x-requests-remaining': remaining[0]})
        feed, calls = self.feed(handler)
        status = await feed.refresh()
        self.assertEqual(status['phase'], 'quota_exhausted')
        self.assertEqual(len(calls), 1)
        self.assertEqual(status['local_credits_reserved'], 0)
        remaining[0] = '500'
        self.clock[0] += dt.timedelta(hours=1, seconds=1)
        updated = document()
        for market in updated['bookmakers'][0]['markets']:
            market['last_update'] = self.clock[0].isoformat()
        def reset_handler(request):
            payload = [event()] if request.url.path == provider.EVENTS_PATH else updated
            return httpx.Response(200, json=payload, headers={'x-requests-remaining': '500'})
        feed._client = httpx.AsyncClient(transport=httpx.MockTransport(reset_handler))
        self.addAsyncCleanup(feed._client.aclose)
        self.assertEqual((await feed.refresh())['phase'], 'ready')

    async def test_refusal_invalid_key_and_rate_limit_are_distinct_and_cool_down(self):
        for code, phase, error in ((401, 'not_authorized', 'invalid_api_key'),
                                    (403, 'blocked', 'access_refused'),
                                    (429, 'rate_limited', 'rate_limited')):
            with self.subTest(code=code):
                self.store.set_checkpoint(provider.STATUS_CHECKPOINT, {})
                feed, calls = self.feed(lambda request: httpx.Response(code, text=KEY,
                    headers={'Retry-After': '1800'}))
                status = await feed.refresh()
                self.assertEqual((status['phase'], status['error_code']), (phase, error))
                self.assertNotIn(KEY, json.dumps(status))
                await feed.refresh(force=True)
                self.assertEqual(len(calls), 1)
                self.assertEqual(status['local_credits_reserved'], 0)
                if code == 429:
                    self.assertEqual(status['next_check_at'], (NOW+dt.timedelta(minutes=30)).isoformat())

    async def test_only_exact_fixture_and_unique_provider_event_can_spend_credits(self):
        for events in ([event(home_team='Milwaukee Bucks', away_team='New York Knicks')],
                       [event(commence_time='2026-11-11T01:30:00Z')],
                       [event(sport_key='basketball_wnba')], [event(), event(id='other')],
                       [event(id='../other')]):
            with self.subTest(events=events):
                self.store.set_checkpoint(provider.STATUS_CHECKPOINT, {})
                feed, calls = self.feed(lambda request: httpx.Response(200, json=events))
                self.assertEqual((await feed.refresh())['phase'], 'unavailable')
                self.assertEqual(len(calls), 1)
                self.assertEqual(feed.status()['local_credits_reserved'], 0)

    async def test_stale_source_market_is_excluded_and_cache_rechecks_source_clock(self):
        data = document()
        for market in data['bookmakers'][0]['markets']:
            market['last_update'] = (NOW-dt.timedelta(seconds=299)).isoformat()
        feed, _ = self.feed(lambda request: httpx.Response(200, json=[event()] if request.url.path == provider.EVENTS_PATH else data))
        await feed.refresh()
        self.assertEqual(len(feed.offers_for_game()['offers']), 2)
        self.clock[0] += dt.timedelta(seconds=2)
        report = feed.offers_for_game()
        self.assertFalse(report['fresh'])
        self.assertEqual(report['legs'], [])
        self.assertEqual(report['status']['error_code'], 'stale_market_cache')

    async def test_changed_saved_event_invalidates_quote_without_redating_it(self):
        feed, _ = self.feed()
        await feed.refresh()
        saved = copy.deepcopy(self.store.get_checkpoint(provider.SNAPSHOT_CHECKPOINT))
        self.store.upsert_game({**self.game, 'start_time': '2026-11-11T01:00:00+00:00'})
        self.assertFalse(feed.offers_for_game()['fresh'])
        self.assertEqual(self.store.get_checkpoint(provider.SNAPSHOT_CHECKPOINT), saved)

    async def test_unsupported_markets_and_unknown_sides_are_not_fabricated(self):
        data = document()
        data['bookmakers'][0]['markets'][0]['outcomes'] = [
            {'name': 'Maybe', 'description': 'Jalen Brunson', 'point': 25.5}]
        data['bookmakers'][0]['markets'].append({
            'key': 'player_points_alternate', 'last_update': NOW.isoformat(), 'outcomes': [
                {'name': 'Over', 'description': 'Jalen Brunson', 'point': 19.5, 'price': 100}]})
        feed, _ = self.feed(lambda request: httpx.Response(200, json=[event()] if request.url.path == provider.EVENTS_PATH else data))
        await feed.refresh()
        report = feed.offers_for_game()
        self.assertEqual([offer['player'] for offer in report['offers']], ['Giannis Antetokounmpo'])
        self.assertEqual([leg['side'] for leg in report['legs']], ['more'])

    async def test_wrong_book_or_changed_event_reply_is_not_a_prizepicks_offer(self):
        data = document(bookmakers=[{'key': 'draftkings', 'markets': []}])
        feed, _ = self.feed(lambda request: httpx.Response(200, json=[event()] if request.url.path == provider.EVENTS_PATH else data))
        status = await feed.refresh()
        self.assertEqual(status['phase'], 'no_offers')
        self.assertEqual(feed.offers_for_game()['legs'], [])

    async def test_key_is_redacted_even_when_httpx_request_logging_is_enabled(self):
        feed, _ = self.feed()
        logger = logging.getLogger('httpx')
        with self.assertLogs(logger, level=logging.INFO) as captured:
            await feed.refresh()
        rendered = '\n'.join(captured.output)
        self.assertNotIn(KEY, rendered)
        self.assertIn('apiKey=[REDACTED]', rendered)
        with self.store.connect() as db:
            saved = '\n'.join(row[0] for row in db.execute('SELECT value_json FROM checkpoints'))
        self.assertNotIn(KEY, saved)

    async def test_interrupted_charged_call_retains_durable_reservation_and_bounded_cooldown(self):
        async def delayed(request):
            if request.url.path == provider.EVENTS_PATH:
                return httpx.Response(200, json=[event()])
            raise httpx.ReadTimeout('Synthetic interruption after the charged request began')
        client = httpx.AsyncClient(transport=httpx.MockTransport(delayed))
        self.addAsyncCleanup(client.aclose)
        feed = provider.TheOddsAPIFeed(self.store, client=client, api_key=KEY, now=lambda: self.clock[0])
        self.addAsyncCleanup(feed.aclose)
        status = await feed.refresh()
        self.assertEqual(status['error_code'], 'request_timeout')
        self.assertEqual(self.store.get_checkpoint(provider.USAGE_CHECKPOINT)['credits_reserved'], 5)
        self.assertFalse(feed.offers_for_game()['fresh'])
        self.assertTrue((await feed.refresh())['cooldown'])

    async def test_background_supervisor_does_not_spend_any_free_plan_credits(self):
        feed, calls = self.feed()
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, [])
        self.assertEqual(feed.status()['local_credits_reserved'], 0)


if __name__ == '__main__':
    unittest.main()
