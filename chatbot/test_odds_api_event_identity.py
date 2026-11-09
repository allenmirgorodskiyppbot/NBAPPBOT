"""Synthetic identity boundaries; these are not observations of a live book."""
import datetime as dt
import json
import unittest

from feeds import TEAM_WORDS
import the_odds_api_feed as source
import test_the_odds_api_contract as fixtures
from test_the_odds_api_contract import (
    NOW, TIP, SECRET, event, document, market,
)


class OddsAPIEventIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TheOddsAPIIndependentContractTests.asyncSetUp
    feed = fixtures.TheOddsAPIIndependentContractTests.feed

    def payload(self, observed, *, at=None):
        return {**document(market(at=at or self.clock[0])), **observed}

    async def test_unique_same_fixture_with_different_provider_clock_retains_both_times(self):
        observed = event(commence_time='2026-11-11T00:40:00+00:00')
        before = self.store.games(self.game['date'])
        feed = self.feed(self.payload(observed), events=[observed])
        result = await feed.refresh()
        self.assertEqual(result['phase'], 'ready')
        self.assertTrue(result['provider_timing_differs'])
        self.assertEqual(result['matched_provider_start_time'], observed['commence_time'])
        self.assertEqual(result['saved_start_time'], TIP)
        saved = feed.offers_for_game(self.game)
        self.assertTrue(saved['fresh'])
        self.assertEqual(saved['offers'][0]['provider_start_time'], observed['commence_time'])
        self.assertEqual(saved['offers'][0]['saved_start_time'], TIP)
        snapshot = self.store.get_checkpoint(source.SNAPSHOT_CHECKPOINT)
        self.assertEqual(snapshot['game']['start_time'], TIP)
        self.assertEqual(snapshot['provider_event_signature']['start_time'], observed['commence_time'])
        self.assertEqual(self.store.games(self.game['date']), before)
        self.assertEqual(len(self.calls), 2)

    async def test_paid_response_must_keep_the_selected_provider_clock(self):
        selected = event(commence_time='2026-11-11T00:40:00+00:00')
        changed = event(commence_time='2026-11-11T00:45:00+00:00')
        feed = self.feed(self.payload(changed), events=[selected])
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertFalse(feed.offers_for_game(self.game)['fresh'])
        self.assertEqual(len(self.calls), 2)

    async def test_paid_response_must_keep_the_selected_provider_id(self):
        selected = event(commence_time='2026-11-11T00:40:00+00:00')
        changed = {**selected, 'id': 'a_different_provider_event'}
        feed = self.feed(self.payload(changed), events=[selected])
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertFalse(feed.offers_for_game(self.game)['fresh'])
        self.assertEqual(len(self.calls), 2)

    async def test_cache_stops_at_provider_tip_even_while_saved_tip_is_future(self):
        saved_tip = dt.datetime.fromisoformat(TIP)
        self.clock[0] = saved_tip - dt.timedelta(seconds=60)
        observed = event(commence_time=(saved_tip - dt.timedelta(seconds=30)).isoformat())
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertEqual((await feed.refresh())['phase'], 'ready')
        self.clock[0] += dt.timedelta(seconds=31)
        self.assertLess(self.clock[0], saved_tip)
        self.assertFalse(feed.offers_for_game(self.game)['fresh'])
        self.assertEqual(feed.offers_for_game(self.game)['offers'], [])

    async def test_cache_stops_at_saved_tip_even_while_provider_tip_is_future(self):
        saved_tip = dt.datetime.fromisoformat(TIP)
        self.clock[0] = saved_tip - dt.timedelta(seconds=30)
        observed = event(commence_time=(saved_tip + dt.timedelta(seconds=60)).isoformat())
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertEqual((await feed.refresh())['phase'], 'ready')
        self.clock[0] += dt.timedelta(seconds=31)
        self.assertFalse(feed.offers_for_game(self.game)['fresh'])
        self.assertEqual(feed.offers_for_game(self.game)['offers'], [])

    async def test_missing_listing_is_waiting_and_never_reserves_paid_credits(self):
        feed = self.feed(document(market()), events=[])
        result = await feed.refresh()
        self.assertEqual(result['phase'], 'waiting_for_event')
        self.assertEqual(result['error_code'], 'provider_event_not_listed')
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(result['credits_reserved'], 0)
        self.assertEqual(len(self.calls), 1)

    async def test_two_same_pair_date_events_with_different_times_remain_ambiguous(self):
        events = [event(), event(id='second_event', commence_time='2026-11-11T01:00:00+00:00')]
        feed = self.feed(document(market()), events=events)
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertEqual(result['error_code'], 'event_identity_ambiguous')
        self.assertEqual(result['credits_reserved'], 0)
        self.assertEqual(len(self.calls), 1)

    async def test_reversed_home_away_never_uses_the_other_venue(self):
        observed = event(home_team='Milwaukee Bucks', away_team='New York Knicks')
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertNotEqual((await feed.refresh())['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(feed.status()['credits_reserved'], 0)

    async def test_different_eastern_date_never_uses_another_meeting(self):
        observed = event(commence_time='2026-11-12T00:30:00+00:00')
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertNotEqual((await feed.refresh())['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(feed.status()['credits_reserved'], 0)

    async def test_started_provider_event_is_held_before_paid_lookup(self):
        self.clock[0] = dt.datetime.fromisoformat(TIP) - dt.timedelta(minutes=1)
        observed = event(commence_time=(self.clock[0] - dt.timedelta(seconds=1)).isoformat())
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertNotEqual((await feed.refresh())['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(feed.status()['credits_reserved'], 0)

    async def test_explicit_cancellation_is_not_converted_to_a_scheduled_offer(self):
        observed = event(status='cancelled')
        feed = self.feed(self.payload(observed), events=[observed])
        self.assertNotEqual((await feed.refresh())['phase'], 'ready')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(feed.status()['credits_reserved'], 0)

    async def test_diagnostics_are_bounded_canonical_and_never_echo_raw_fields(self):
        observed = [event(id='raw_source_id_' + str(i),
                    away_team='Cleveland Cavaliers',
                    commence_time=(dt.datetime.fromisoformat(TIP) + dt.timedelta(days=i+1)).isoformat(),
                    private_note=SECRET, raw_url='https://example.invalid/?apiKey=' + SECRET)
                    for i in range(5)]
        observed.append(event(id='raw_source_id_invalid', away_team=SECRET,
                              commence_time=SECRET, raw_url=SECRET))
        feed = self.feed(document(market()), events=observed)
        result = await feed.refresh()
        self.assertNotEqual(result['phase'], 'ready')
        self.assertEqual(result['provider_event_count'], len(observed))
        rows = result['provider_knicks_events']
        self.assertLessEqual(len(rows), 3)
        for row in rows:
            self.assertLessEqual(set(row), {'home_team', 'away_team', 'date', 'start_time'})
            self.assertIn(row.get('home_team'), {*TEAM_WORDS, None})
            self.assertIn(row.get('away_team'), {*TEAM_WORDS, None})
            if row.get('start_time'):
                self.assertIsNotNone(dt.datetime.fromisoformat(row['start_time']).tzinfo)
        expected = result['expected_game']
        self.assertEqual(expected, {key: self.game[key] for key in ('home_team', 'away_team', 'date', 'start_time')})
        encoded = json.dumps(result)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn('raw_source_id', encoded)
        self.assertNotIn('raw_url', encoded)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
