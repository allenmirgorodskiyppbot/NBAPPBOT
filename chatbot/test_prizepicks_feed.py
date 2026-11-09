"""Public-feed lifecycle and safety controls without any actual sportsbook calls."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from feeds import canonical_game
from master import MasterStore
import prizepicks_feed as pp

NOW = dt.datetime(2026, 11, 9, 17, tzinfo=dt.timezone.utc)
DATE = '2026-11-10'
TIP = '2026-11-11T00:30:00+00:00'


def document(*, projection='p1', sides=('more', 'less'), pages=1, page=1, **attributes):
    attrs = {'line_score': 25.5, 'stat_type': 'Points', 'stat_display_name': 'Points',
             'status': 'pre_game', 'odds_type': 'standard', 'projection_type': 'Single Stat',
             'is_promo': False, 'start_time': TIP, 'updated_at': '2026-11-09T16:00:00Z'}
    if sides is not None:
        attrs['available_sides'] = list(sides)
    attrs.update(attributes)
    return {'data': [{'id': projection, 'type': 'projection', 'attributes': attrs,
                     'relationships': {'new_player': {'data': {'type': 'new_player', 'id': 'nba-player-1'}},
                                       'league': {'data': {'type': 'league', 'id': '7'}}}}],
            'included': [{'type': 'league', 'id': '7', 'attributes': {'name': 'NBA'}},
                         {'type': 'new_player', 'id': 'nba-player-1',
                          'attributes': {'name': 'Jalen Brunson', 'team': 'NYK'}}],
            'meta': {'current_page': page, 'total_pages': pages}}


class PrizePicksFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'master.sqlite')
        self.clock = [NOW]
        self.store.upsert_game({'id': canonical_game(DATE, 'NYK', 'MIL'), 'date': DATE,
            'home_team': 'NYK', 'away_team': 'MIL', 'status': 'scheduled', 'start_time': TIP,
            'source': 'SYNTHETIC feed matchup control'})
        prior = canonical_game('2026-11-07', 'NYK', 'CLE')
        self.store.upsert_game({'id': prior, 'date': '2026-11-07', 'home_team': 'NYK',
                               'away_team': 'CLE', 'status': 'final', 'source': 'SYNTHETIC control'})
        self.store.upsert_player_game({'game_id': prior, 'date': '2026-11-07', 'player': 'Jalen Brunson',
            'team': 'NYK', 'opponent': 'CLE', 'season': '2026-27', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 33, 'pts': 25, 'source': 'SYNTHETIC control'})

    def client(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return client

    def feed(self, handler, **kwargs):
        feed = pp.PrizePicksFeed(self.store, client=self.client(handler), now=lambda: self.clock[0], **kwargs)
        self.addAsyncCleanup(feed.aclose)
        return feed

    def protected(self):
        with self.store.connect() as db:
            return {table: [list(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ('games', 'player_games', 'news', 'boards', 'entries', 'grade_events')}

    async def test_explicit_feed_offers_match_exact_game_and_do_not_touch_master_or_entries(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=document())
        feed = self.feed(handler)
        before = self.protected()
        status = await feed.refresh()
        self.assertEqual(status['phase'], 'ready')
        self.assertEqual(status['matched_offers'], 1)
        self.assertEqual(status['readable_selections'], 2)
        report = feed.offers_for_game()
        self.assertTrue(report['fresh'])
        self.assertEqual({leg['side'] for leg in report['legs']}, {'more', 'less'})
        self.assertEqual(report['offers'][0]['projection_id'], 'p1')
        self.assertEqual(report['offers'][0]['provider_updated_at'], '2026-11-09T16:00:00+00:00')
        self.assertFalse(report['offers'][0]['offer_verified'])
        self.assertEqual(calls[0].url.host, 'api.prizepicks.com')
        self.assertEqual(calls[0].method, 'GET')
        self.assertNotIn('authorization', calls[0].headers)
        self.assertNotIn('cookie', calls[0].headers)
        self.assertEqual(self.protected(), before)

    async def test_unknown_sides_and_adjusted_types_remain_preview_only(self):
        for variant in ('standard', 'goblin', 'demon'):
            with self.subTest(variant=variant):
                feed = self.feed(lambda request: httpx.Response(200, json=document(sides=None, odds_type=variant)))
                await feed.refresh(force=True)
                report = feed.offers_for_game()
                self.assertEqual(len(report['offers']), 1)
                self.assertEqual(report['offers'][0]['offer_variant'], variant)
                self.assertEqual(report['legs'], [])
                self.assertTrue(report['offers'][0]['requires_offer_confirmation'])

    async def test_goblin_explicit_more_only_never_invents_less_or_payout(self):
        feed = self.feed(lambda request: httpx.Response(200, json=document(sides=('more',), odds_type='goblin')))
        await feed.refresh()
        report = feed.offers_for_game()
        self.assertEqual([row['side'] for row in report['legs']], ['more'])
        self.assertNotIn('payout', json.dumps(report))

    async def test_missing_promotion_flag_is_preview_only(self):
        doc = document()
        del doc['data'][0]['attributes']['is_promo']
        feed = self.feed(lambda request: httpx.Response(200, json=doc))
        await feed.refresh()
        report = feed.offers_for_game()
        self.assertEqual(len(report['offers']), 1)
        self.assertIsNone(report['offers'][0]['is_promo'])
        self.assertTrue(report['offers'][0]['requires_offer_confirmation'])
        self.assertEqual(report['legs'], [])

    async def test_unsupported_scope_promos_and_unknown_variants_never_become_legs(self):
        for changes in ({'projection_type': 'First Half'}, {'projection_type': 'Combo'},
                        {'projection_type': None}, {'period': '1H'}, {'stat_display_name': '1Q Points'},
                        {'is_promo': True}, {'is_promo': 'false'}, {'odds_type': None}):
            with self.subTest(changes=changes):
                feed = self.feed(lambda request: httpx.Response(200, json=document(**changes)))
                await feed.refresh(force=True)
                self.assertEqual(feed.offers_for_game()['legs'], [])

    async def test_jsonapi_join_uses_resource_id_not_included_order(self):
        doc = document()
        doc['included'].insert(0, {'type': 'new_player', 'id': 'unrelated',
                                  'attributes': {'name': 'Wrong Person', 'team': 'MIL'}})
        doc['included'].reverse()
        feed = self.feed(lambda request: httpx.Response(200, json=doc))
        await feed.refresh()
        self.assertEqual(feed.offers_for_game()['offers'][0]['player'], 'Jalen Brunson')

    async def test_403_preserves_last_success_clock_and_blocks_old_cache_without_retry_storm(self):
        responses = [httpx.Response(200, json=document()), httpx.Response(403, text='private provider body')]
        calls = []
        def handler(request):
            calls.append(request)
            return responses.pop(0)
        feed = self.feed(handler)
        await feed.refresh()
        old = self.store.get_checkpoint(pp.SNAPSHOT_CHECKPOINT)
        self.clock[0] += dt.timedelta(seconds=10)
        status = await feed.refresh(force=True)
        self.assertEqual(status['phase'], 'blocked')
        self.assertEqual(status['http_status'], 403)
        self.assertEqual(status['last_success_at'], NOW.isoformat())
        self.assertEqual(self.store.get_checkpoint(pp.SNAPSHOT_CHECKPOINT), old)
        self.assertFalse(feed.offers_for_game()['fresh'])
        self.assertEqual(feed.offers_for_game()['offers'], [])
        await feed.refresh(force=True)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('private provider body', json.dumps(status))

    async def test_429_honors_retry_after_even_for_manual_force(self):
        calls = []
        feed = self.feed(lambda request: calls.append(request) or httpx.Response(429, headers={'Retry-After': '3600'}))
        status = await feed.refresh(force=True)
        self.assertEqual(status['phase'], 'rate_limited')
        self.assertEqual(status['next_check_at'], (NOW+dt.timedelta(hours=1)).isoformat())
        await feed.refresh(force=True)
        self.assertEqual(len(calls), 1)

    async def test_long_retry_after_is_not_shortened_and_overflow_never_retries_early(self):
        for raw, expected in [('172800', NOW+dt.timedelta(days=2)),
                              ('1e300', dt.datetime.max.replace(tzinfo=dt.timezone.utc)),
                              ('inf', NOW+dt.timedelta(days=1))]:
            with self.subTest(raw=raw):
                # Each independent provider response starts without a refusal
                # from a previous case; force must honor an existing cooldown.
                self.store.set_checkpoint(pp.STATUS_CHECKPOINT, {})
                feed = self.feed(lambda request: httpx.Response(429, headers={'Retry-After': raw}))
                status = await feed.refresh(force=True)
                self.assertEqual(status['next_check_at'], expected.isoformat())

    async def test_concurrent_force_requests_coalesce_even_with_frozen_clock(self):
        entered, release, calls = asyncio.Event(), asyncio.Event(), []
        async def handler(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=document())
        feed = self.feed(handler)
        first = asyncio.create_task(feed.refresh(force=True))
        await entered.wait()
        second = asyncio.create_task(feed.refresh(force=True))
        await asyncio.sleep(.01)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(len(calls), 1)
        self.assertTrue(results[1]['coalesced'])

    async def test_pages_and_total_counts_are_validated_as_one_complete_read(self):
        calls = []
        def handler(request):
            page = int(request.url.params['page'])
            calls.append(page)
            doc = document(projection='p'+str(page), pages=2, page=page)
            doc['meta']['total_count'] = 2
            return httpx.Response(200, json=doc)
        feed = self.feed(handler)
        self.assertEqual((await feed.refresh())['matched_offers'], 2)
        self.assertEqual(calls, [1, 2])
        for count in (3,):
            feed = self.feed(lambda request: httpx.Response(200, json={**document(), 'meta': {
                'current_page': 1, 'total_pages': 1, 'total_count': count}}))
            status = await feed.refresh(force=True)
            self.assertEqual(status['phase'], 'partial')
            self.assertEqual(status['http_status'], 200)
            self.assertEqual(feed.offers_for_game()['offers'], [])

    async def test_incomplete_or_duplicate_pages_never_replace_previous_snapshot(self):
        feed = self.feed(lambda request: httpx.Response(200, json=document()))
        await feed.refresh()
        original = self.store.get_checkpoint(pp.SNAPSHOT_CHECKPOINT)
        doc = document(pages=2)
        bad = self.feed(lambda request: httpx.Response(200, json={**doc, 'meta': {
            'current_page': int(request.url.params['page']), 'total_pages': 2}}))
        status = await bad.refresh(force=True)
        self.assertEqual(status['error_code'], 'duplicate_projection_across_pages')
        self.assertEqual(status['http_status'], 200)
        self.assertEqual(self.store.get_checkpoint(pp.SNAPSHOT_CHECKPOINT), original)
        self.assertFalse(bad.offers_for_game()['fresh'])

    async def test_changed_cancelled_started_or_other_game_invalidates_cached_offers(self):
        feed = self.feed(lambda request: httpx.Response(200, json=document()))
        await feed.refresh()
        game = feed.offers_for_game()['game']
        self.store.upsert_game({**game, 'start_time': '2026-11-11T01:00:00+00:00'})
        self.assertFalse(feed.offers_for_game(game)['fresh'])
        self.store.upsert_game({**game, 'status': 'canceled'})
        self.assertFalse(feed.offers_for_game(game)['fresh'])
        self.store.upsert_game(game)
        self.clock[0] = dt.datetime(2026, 11, 11, 1, tzinfo=dt.timezone.utc)
        self.assertFalse(feed.offers_for_game(game)['fresh'])
        self.assertFalse(feed.offers_for_game({**game, 'id': 'other'})['fresh'])

    async def test_cache_expires_and_regular_refresh_is_due_before_expiry(self):
        calls = []
        feed = self.feed(lambda request: calls.append(request) or httpx.Response(200, json=document()))
        await feed.refresh()
        self.clock[0] += dt.timedelta(seconds=61)
        await feed.refresh()
        self.assertEqual(len(calls), 2)
        self.clock[0] += dt.timedelta(seconds=pp.CACHE_SECONDS+1)
        self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_bad_league_clock_player_status_or_number_are_not_usable_offers(self):
        for changes in ({'start_time': '2026-11-11T00:30:00'}, {'status': 'suspended'},
                        {'line_score': float('nan')}, {'updated_at': 'not a date'},
                        {'start_time': '2026-11-11T01:30:00Z'}):
            doc = document(**changes)
            feed = self.feed(lambda request: httpx.Response(200, json=doc))
            await feed.refresh(force=True)
            self.assertEqual(feed.offers_for_game()['offers'], [])
        doc = document(); doc['included'][0]['attributes']['name'] = 'NBA1H'
        feed = self.feed(lambda request: httpx.Response(200, json=doc))
        status = await feed.refresh(force=True)
        self.assertEqual(status['error_code'], 'nba_league_identity_unverified')
        self.assertEqual(status['http_status'], 200)

    async def test_unknown_pagination_stale_http_cache_and_large_pages_fail_closed(self):
        for doc, headers in (({**document(), 'meta': {}}, {}),
                             (document(pages=pp.MAX_PAGES+1), {}),
                             (document(), {'Age': '180'})):
            feed = self.feed(lambda request: httpx.Response(200, json=doc, headers=headers))
            status = await feed.refresh(force=True)
            self.assertNotIn(status['phase'], {'ready', 'no_offers'})
            self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_total_request_deadline_is_bounded(self):
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=document())
        feed = self.feed(slow)
        start = time.monotonic()
        with patch.object(pp, 'REQUEST_SECONDS', .03):
            status = await feed.refresh(force=True)
        self.assertLess(time.monotonic()-start, .5)
        self.assertEqual(status['error_code'], 'request_timeout')
        self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_response_byte_limit_fails_closed(self):
        feed = self.feed(lambda request: httpx.Response(200, json=document()))
        with patch.object(pp, 'MAX_RESPONSE_BYTES', 100):
            status = await feed.refresh(force=True)
        self.assertEqual(status['error_code'], 'response_size_limit')
        self.assertEqual(status['http_status'], 200)
        self.assertFalse(feed.offers_for_game()['fresh'])

    async def test_complete_empty_nba_response_is_distinct_from_access_failure(self):
        doc = document()
        doc['data'] = []
        doc['meta'] = {'current_page': 1, 'total_pages': 0, 'total_count': 0}
        feed = self.feed(lambda request: httpx.Response(200, json=doc))
        status = await feed.refresh()
        self.assertEqual(status['phase'], 'no_offers')
        self.assertEqual(status['http_status'], 200)
        self.assertTrue(feed.offers_for_game()['fresh'])
        self.assertEqual(feed.offers_for_game()['offers'], [])

    async def test_player_transfer_does_not_reuse_previous_team_offer(self):
        game_id = canonical_game('2026-11-08', 'MIL', 'BOS')
        self.store.upsert_game({'id': game_id, 'date': '2026-11-08', 'home_team': 'MIL',
                               'away_team': 'BOS', 'status': 'final', 'source': 'SYNTHETIC transfer'})
        self.store.upsert_player_game({'game_id': game_id, 'date': '2026-11-08', 'player': 'Jalen Brunson',
            'team': 'MIL', 'opponent': 'BOS', 'season': '2026-27', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 30, 'pts': 20, 'source': 'SYNTHETIC transfer'})
        feed = self.feed(lambda request: httpx.Response(200, json=document()))
        await feed.refresh()
        report = feed.offers_for_game()
        self.assertEqual(report['offers'], [])
        self.assertTrue(any(row['reason'] == 'current_player_team_unverified' for row in report['exclusions']))

    async def test_disabled_constructor_and_status_never_create_network_clients(self):
        before = self.protected()
        with patch('prizepicks_feed.httpx.AsyncClient') as constructor:
            feed = pp.PrizePicksFeed(self.store, now=lambda: NOW, enabled=False)
            self.assertEqual(feed.status()['phase'], 'disabled')
            await feed.refresh(force=True)
            feed.offers_for_game()
            await feed.aclose()
            constructor.assert_not_called()
        self.assertEqual(self.protected(), before)


if __name__ == '__main__':
    unittest.main()
