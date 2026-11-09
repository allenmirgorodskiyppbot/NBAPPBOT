"""Independent provider-boundary checks using invented records and transport."""
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
import the_odds_api_feed as source


NOW = dt.datetime(2026, 11, 9, 17, tzinfo=dt.timezone.utc)
TIP = '2026-11-11T00:30:00+00:00'
SECRET = 'synthetic-private-provider-key'


def event(**changes):
    value = {'id': 'synthetic_event', 'sport_key': 'basketball_nba',
             'home_team': 'New York Knicks', 'away_team': 'Milwaukee Bucks',
             'commence_time': TIP}
    value.update(changes)
    return value


def document(*markets):
    return {**event(), 'bookmakers': [{'key': 'prizepicks', 'markets': list(markets)}]}


def market(player='Jalen Brunson', line=25.5, *, at=NOW, key='player_points'):
    return {'key': key, 'last_update': at.isoformat(), 'outcomes': [
        {'name': 'Over', 'description': player, 'point': line, 'price': -999999},
        {'name': 'Under', 'description': player, 'point': line, 'price': 999999}]}


class TheOddsAPIIndependentContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MasterStore(Path(folder.name) / 'master.sqlite')
        self.clock = [NOW]
        self.game = {'id': canonical_game('2026-11-10', 'NYK', 'MIL'),
                     'date': '2026-11-10', 'home_team': 'NYK', 'away_team': 'MIL',
                     'status': 'scheduled', 'start_time': TIP, 'source': 'SYNTHETIC'}
        self.store.upsert_game(self.game)
        for name, team in [('Jalen Brunson', 'NYK'), ('Giannis Antetokounmpo', 'MIL')]:
            identity = canonical_game('2026-11-07', team, 'CLE')
            self.store.upsert_game({'id': identity, 'date': '2026-11-07',
                'home_team': team, 'away_team': 'CLE', 'status': 'final', 'source': 'SYNTHETIC'})
            self.store.upsert_player_game({'game_id': identity, 'date': '2026-11-07',
                'player': name, 'team': team, 'opponent': 'CLE', 'pts': 25,
                'minutes': 33, 'appearance_state': 'Played', 'season': '2026-27',
                'season_type': 'Regular Season', 'source': 'SYNTHETIC'})

    def feed(self, payload, *, events=None, headers=None, advance_on_odds=0):
        self.calls = []
        events = [event()] if events is None else events
        def handler(request):
            self.calls.append(request.url.path)
            self.assertEqual(request.url.host, 'api.the-odds-api.com')
            self.assertEqual(request.url.params.get('apiKey'), SECRET)
            if request.url.path.endswith('/odds'):
                self.clock[0] += dt.timedelta(seconds=advance_on_odds)
            body = events if request.url.path.endswith('/events') else payload
            return httpx.Response(200, json=body, headers=headers or {})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        feed = source.TheOddsAPIFeed(self.store, client=client, api_key=SECRET,
                                     now=lambda: self.clock[0], enabled=True)
        self.addAsyncCleanup(feed.aclose)
        return feed

    async def test_request_logs_and_durable_metadata_never_contain_provider_key(self):
        feed = self.feed(document(market()))
        logger = logging.getLogger('httpx')
        old_level = logger.level
        self.addCleanup(logger.setLevel, old_level)
        with self.assertLogs('httpx', level='INFO') as captured:
            result = await feed.refresh()
        self.assertEqual(result['phase'], 'ready')
        self.assertNotIn(SECRET, '\n'.join(captured.output))
        self.assertIn('[REDACTED]', '\n'.join(captured.output))
        with self.store.connect() as db:
            saved = [list(row) for row in db.execute('SELECT * FROM checkpoints')]
        self.assertNotIn(SECRET, json.dumps([saved, result, feed.offers_for_game()]))

    async def test_market_clock_expiration_is_checked_again_inside_retrieval_cache(self):
        feed = self.feed(document(market(at=NOW - dt.timedelta(seconds=290)),
                                  market('Giannis Antetokounmpo', 10.5, key='player_rebounds')))
        result = await feed.refresh()
        self.assertEqual(result['matched_offers'], 2)
        self.clock[0] += dt.timedelta(seconds=20)
        saved = feed.offers_for_game(self.game)
        self.assertEqual({item['player'] for item in saved['offers']}, {'Giannis Antetokounmpo'})
        self.assertEqual({item['player'] for item in saved['legs']}, {'Giannis Antetokounmpo'})
        self.assertEqual(len(self.calls), 2)

    async def test_provider_update_during_request_is_compared_with_completion_clock(self):
        completed = NOW + dt.timedelta(seconds=7)
        feed = self.feed(document(market(at=completed)), advance_on_odds=7)
        result = await feed.refresh()
        self.assertEqual(result['phase'], 'ready')
        saved = feed.offers_for_game(self.game)
        self.assertEqual(len(saved['offers']), 1)
        self.assertEqual(saved['offers'][0]['provider_updated_at'], completed.isoformat())

    async def test_duplicate_event_matches_do_not_spend_a_priced_request(self):
        feed = self.feed(document(market()), events=[event(), event(id='second_synthetic_event')])
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['credits_reserved'], 0)
        self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_unverified_team_suffix_never_selects_or_spends_on_a_game(self):
        feed = self.feed(document(market()), events=[event(home_team='Fictional New York Knicks')])
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['credits_reserved'], 0)

    async def test_other_book_rows_and_alternate_or_period_markets_do_not_become_standard(self):
        value = document(market(), market(key='player_points_alternate'), market(key='player_points_q1'))
        other = copy.deepcopy(value['bookmakers'][0])
        other['key'] = 'fanduel'
        other['markets'][0]['outcomes'][0]['point'] = 0.5
        value['bookmakers'].append(other)
        feed = self.feed(value)
        result = await feed.refresh()
        self.assertEqual(result['phase'], 'ready')
        saved = feed.offers_for_game(self.game)
        self.assertEqual(len(saved['offers']), 1)
        self.assertEqual(saved['offers'][0]['line'], 25.5)
        self.assertEqual(saved['offers'][0]['stat'], 'pts')
        self.assertTrue(all('price' not in item for item in saved['legs']))

    async def test_missing_under_is_not_added_from_the_other_book(self):
        value = document(market())
        value['bookmakers'][0]['markets'][0]['outcomes'].pop()
        value['bookmakers'].append({'key': 'fanduel', 'markets': [market()]})
        feed = self.feed(value)
        await feed.refresh()
        self.assertEqual({item['side'] for item in feed.offers_for_game(self.game)['legs']}, {'more'})

    async def test_saved_fixture_clock_change_invalidates_prior_quote_immediately(self):
        feed = self.feed(document(market()))
        await feed.refresh()
        changed = {**self.game, 'start_time': '2026-11-11T01:00:00+00:00'}
        self.store.upsert_game(changed)
        self.assertFalse(feed.offers_for_game(changed)['fresh'])
        self.assertEqual(feed.offers_for_game(changed)['offers'], [])

    async def test_provider_reservation_survives_recreating_the_reader(self):
        feed = self.feed(document(market()), headers={'x-requests-remaining': '495'})
        await feed.refresh()
        recreated = source.TheOddsAPIFeed(self.store, api_key=SECRET,
            now=lambda: self.clock[0], enabled=True)
        self.addAsyncCleanup(recreated.aclose)
        self.assertEqual(recreated.status()['credits_reserved'], source.MAX_CALL_CREDITS)
        self.assertEqual(recreated.status()['credits_remaining'], 495)

    async def test_exhausted_provider_credits_are_not_reported_as_an_invalid_key(self):
        def handler(request):
            if request.url.path.endswith('/events'):
                return httpx.Response(200, json=[event()], headers={'x-requests-remaining': '500'})
            return httpx.Response(401, json={'error_code': 'OUT_OF_USAGE_CREDITS',
                'message': SECRET}, headers={'x-requests-remaining': '0'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        feed = source.TheOddsAPIFeed(self.store, client=client, api_key=SECRET,
            now=lambda: self.clock[0], enabled=True)
        self.addAsyncCleanup(feed.aclose)
        result = await feed.refresh()
        self.assertEqual(result['phase'], 'quota_exhausted')
        self.assertEqual(result['credits_remaining'], 0)
        self.assertNotIn('invalid_api_key', json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))

    async def test_timeout_after_success_does_not_relabel_old_http_200_as_current(self):
        feed = self.feed(document(market()))
        await feed.refresh()
        self.assertEqual(feed.status()['http_status'], 200)
        self.clock[0] += dt.timedelta(seconds=source.CACHE_SECONDS + 1)
        async def stalled(_request):
            await asyncio.Future()
        client = httpx.AsyncClient(transport=httpx.MockTransport(stalled))
        self.addAsyncCleanup(client.aclose)
        feed._client = client
        with patch.object(source, 'REQUEST_SECONDS', .1):
            result = await feed.refresh()
        self.assertEqual(result['error_code'], 'request_timeout')
        self.assertIsNone(feed.status().get('http_status'))
        self.assertFalse(feed.offers_for_game(self.game)['fresh'])


if __name__ == '__main__':
    unittest.main()
