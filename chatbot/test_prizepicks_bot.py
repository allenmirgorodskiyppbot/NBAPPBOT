"""Automatic bot-side public offers stay distinct from master targets."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import analysis_engine
import telegram_bot
from feeds import canonical_game
from telegram_bot import TelegramBot, RESPONSIVE_COMMANDS, prizepicks_status_reply
import test_next_matchup_master_lookup as fixtures

NOW, DATE = fixtures.NOW, fixtures.DATE
USER = 123
READINESS = {'ready': False, 'statistics_ready': True, 'timing_verified': False,
             'statistics_issues': [], 'timing_issues': ['Future game timing is unverified.'],
             'reasons': ['Future game timing is unverified.'],
             'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga']}


class FakeFeed:
    def __init__(self, game):
        self.game = dict(game)
        self.offers = []
        self.legs = []
        self.fresh = True
        self.saved_status = {'phase': 'ready', 'http_status': 200, 'checked_at': NOW.isoformat(),
                             'last_success_at': NOW.isoformat(), 'matched_offers': 4,
                             'readable_selections': 4}
        for number, (name, team) in enumerate([
                ('Jalen Brunson', 'NYK'), ('Karl-Anthony Towns', 'NYK'),
                ('AJ Green', 'MIL'), ('Giannis Antetokounmpo', 'MIL')], 1):
            row = {'projection_id': str(number), 'player': name, 'team': team,
                   'opponent': 'MIL' if team == 'NYK' else 'NYK', 'stat': 'pts',
                   'stat_type': 'Points', 'line': 10.5, 'offer_variant': 'standard',
                   'date': DATE, 'game_id': game['id'], 'status': 'active',
                   'retrieved_at': NOW.isoformat(), 'available_sides': ['more']}
            self.offers.append(row)
            self.legs.append({**row, 'side': 'more'})
        self.refresh = AsyncMock(side_effect=lambda **kwargs: copy.deepcopy(self.saved_status))
        self.status = Mock(side_effect=lambda: copy.deepcopy(self.saved_status))
        self.offers_for_game = Mock(side_effect=self.bundle)

    def bundle(self, game=None):
        return {'game': dict(self.game), 'fresh': self.fresh,
                'offers': copy.deepcopy(self.offers), 'legs': copy.deepcopy(self.legs),
                'exclusions': [], 'status': self.status()}


class PrizePicksBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = fixtures.NextMatchupMasterLookupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.game = self.store.games(DATE)[0]
        self.feed = FakeFeed(self.game)
        self.bot = TelegramBot(self.store, AsyncMock(), token='synthetic-token',
                               allowed_user_ids=[USER], prizepicks_feed=self.feed)
        self.bot._typing = AsyncMock()
        self.addAsyncCleanup(self.bot.shutdown)
        for name in ('independent_picks', 'board_shortlist', 'analysis_engine'):
            p = patch(name + '.assess_pregame_freshness', return_value=READINESS)
            p.start(); self.addCleanup(p.stop)
        for name in ('independent_picks', 'board_shortlist'):
            p = patch(name + '._now', return_value=NOW)
            p.start(); self.addCleanup(p.stop)
        original = analysis_engine.analyze_board
        p = patch('analysis_engine.analyze_board', side_effect=lambda store, board_id:
                  original(store, board_id, as_of=NOW))
        p.start(); self.addCleanup(p.stop)

    def message(self, text, user=USER):
        return {'text': text, 'from': {'id': user}, 'chat': {'id': user, 'type': 'private'}}

    def source_state(self):
        with self.store.connect() as db:
            return {table: [list(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ('games', 'player_games', 'checkpoints', 'entries', 'news')}

    async def picks(self):
        return await self.bot._handle_message(self.message('/picks'), 1)

    async def test_picks_automatically_combines_explicit_feed_choices_with_own_targets_without_http(self):
        self.feed.legs[0]['reference_projection'] = 999999
        self.feed.offers[0]['reference_projection'] = 999999
        before = self.source_state()
        answer = await self.picks()
        self.assertIn("From PrizePicks' public feed", answer)
        self.assertEqual(answer.count('My own calculated targets from your master'), 1)
        self.assertIn('Jalen Brunson — More 10.5 points', answer)
        self.assertIn('Giannis Antetokounmpo — More 10.5 points', answer)
        self.assertIn('mean 21.50', answer)
        self.assertNotIn('999999', answer)
        self.assertIn('My target lines, not quoted PrizePicks offers', answer)
        self.assertIn('Confirmed', prizepicks_status_reply(self.feed.status()))
        self.assertIn('one source offer per player/statistic', answer)
        self.feed.refresh.assert_not_awaited()
        self.bot.answer_callback.assert_not_awaited()
        self.assertEqual(before, self.source_state())
        self.assertEqual(len(self.store.boards()), 1)
        board = self.store.boards()[0]
        self.assertIn('PrizePicks public feed; telegram:123;', board['source'])
        self.assertEqual({leg['projection_id'] for leg in board['legs']}, {'1', '2', '3', '4'})
        self.assertEqual({leg['side'] for leg in board['legs']}, {'more'})

    async def test_unknown_sides_show_quoted_values_without_inventing_a_board_or_picks(self):
        self.feed.legs = []
        for offer in self.feed.offers:
            offer['available_sides'] = []
        answer = await self.picks()
        self.assertIn('• Jalen Brunson · 10.5 points', answer)
        self.assertIn('did not confirm usable More/Less', answer)
        self.assertNotIn('Jalen Brunson — More 10.5', answer)
        self.assertIn('My own calculated targets from your master', answer)
        self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])
        self.feed.refresh.assert_not_awaited()

    async def test_stale_or_wrong_game_feed_never_becomes_current_and_does_not_hide_master(self):
        self.feed.fresh = False
        self.feed.saved_status.update(phase='blocked', http_status=403,
                                      error_code='access_refused',
                                      detail='SECRET_SESSION_COOKIE', response_body='SECRET_SESSION_COOKIE')
        answer = await self.picks()
        self.assertIn('My target lines', answer)
        self.assertIn('HTTP 403', answer)
        self.assertIn('access_refused', answer)
        self.assertNotIn('SECRET_SESSION_COOKIE', answer)
        self.assertNotIn("From PrizePicks' public feed", answer)
        self.assertEqual(self.store.boards(), [])
        self.feed.fresh = True
        self.feed.game['id'] = canonical_game(DATE, 'NYK', 'CLE')
        answer = await self.picks()
        self.assertNotIn("From PrizePicks' public feed", answer)
        self.assertEqual(self.store.boards(), [])

    async def test_saved_status_explains_schema_failure_without_exposing_arbitrary_error_text(self):
        status = {'phase': 'schema_unavailable', 'http_status': 200,
                  'error_code': 'nba_league_identity_unverified', 'source_projection_count': 17,
                  'detail': 'SECRET_COOKIE', 'response_body': 'SECRET_COOKIE'}
        text = prizepicks_status_reply(status)
        self.assertIn('nba_league_identity_unverified', text)
        self.assertIn('Returned projection records: 17', text)
        self.assertNotIn('SECRET_COOKIE', text)
        status['error_code'] = 'secret_cookie'
        self.assertNotIn('secret_cookie', prizepicks_status_reply(status))

    async def test_explicit_invalid_date_does_not_attach_next_game_feed(self):
        answer = await self.bot._handle_message(self.message('/picks 2026-10-01'), 2)
        self.assertIn('no matching upcoming game', answer)
        self.feed.offers_for_game.assert_not_called()
        self.assertEqual(self.store.boards(), [])

    async def test_manual_refresh_checks_bot_service_but_status_never_fetches(self):
        status = await self.bot._handle_message(self.message('/pp_status'), 3)
        self.assertIn('HTTP 200', status)
        self.assertIn('deployed bot', status)
        self.feed.refresh.assert_not_awaited()
        self.feed.offers_for_game.assert_not_called()
        refreshed = await self.bot._handle_message(self.message('/pp_refresh'), 4)
        self.feed.refresh.assert_awaited_once_with(force=True)
        self.assertIn("From PrizePicks' public feed", refreshed)
        self.assertEqual(self.store.list_entries(), [])
        self.assertIn('/pp_status', RESPONSIVE_COMMANDS)

    async def test_status_stays_responsive_while_an_explicit_refresh_waits(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(**kwargs):
            entered.set()
            await release.wait()
            return self.feed.status()
        self.feed.refresh.side_effect = waiting
        refresh_task = asyncio.create_task(self.bot._handle_message(self.message('/pp_refresh'), 5))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            answer = await asyncio.wait_for(self.bot._handle_message(self.message('/pp_status'), 6), 1)
            self.assertIn('HTTP 200', answer)
        finally:
            release.set()
            await asyncio.gather(refresh_task, return_exceptions=True)

    async def test_feed_commands_remain_available_at_full_model_command_capacity(self):
        continued = asyncio.Event()
        polls = 0
        async def blocked_answer(*args, **kwargs):
            await asyncio.Event().wait()
        self.bot.answer_callback = AsyncMock(side_effect=blocked_answer)
        def update(number, text):
            return {'update_id': number, 'message': self.message(text)}
        async def api(method, payload):
            nonlocal polls
            if method != 'getUpdates':
                return {'message_id': 1}
            polls += 1
            if polls == 1:
                return [update(100, 'Slow model question')]
            if polls == 2:
                return [update(101, '/pp_refresh'), update(102, '/pp_status'),
                        update(103, '/entry {"legs":[],"stake":20}')]
            continued.set()
            await asyncio.Event().wait()
        self.bot._api = api
        with patch.object(telegram_bot, 'MAX_ACTIVE_COMMANDS', 1):
            task = asyncio.create_task(self.bot.run())
            try:
                await asyncio.wait_for(continued.wait(), 3)
                self.feed.refresh.assert_awaited_once_with(force=True)
                with self.bot._db() as db:
                    text = '\n'.join(row['text'] for row in db.execute('SELECT text FROM telegram_outbox'))
                self.assertIn('PrizePicks public feed: responding.', text)
                self.assertIn('This request was not started', text)
                self.assertEqual(self.store.list_entries(), [])
                self.assertTrue({'/pp', '/pp_refresh', '/pp_status'} <= RESPONSIVE_COMMANDS)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_cached_requests_reuse_owner_snapshot_and_keep_alternates_unmodified(self):
        alternate = {**self.feed.legs[0], 'projection_id': 'goblin-alternate',
                     'offer_variant': 'goblin', 'line': 5.5}
        self.feed.legs.append(alternate)
        self.feed.offers.append({k: v for k, v in alternate.items() if k != 'side'})
        saved = copy.deepcopy(self.feed.offers)
        await self.picks()
        await self.picks()
        self.assertEqual(len(self.store.boards()), 1)
        board = self.store.boards()[0]
        brunson = [leg for leg in board['legs'] if leg['player'] == 'Jalen Brunson']
        self.assertEqual([leg['line'] for leg in brunson], [10.5])
        self.assertEqual(saved, self.feed.offers)
        await self.bot._independent_picks_reply({'date': None, 'team': 'NYK', 'count': 4}, 456)
        self.assertEqual(len(self.store.boards()), 2)
        self.assertEqual(self.store.list_entries(), [])

    async def test_expired_cache_after_analysis_withholds_old_offers(self):
        calls = 0
        def cache(game=None):
            nonlocal calls
            calls += 1
            bundle = self.feed.bundle(game)
            if calls > 1:
                bundle['fresh'] = False
            return bundle
        self.feed.offers_for_game.side_effect = cache
        answer = await self.picks()
        self.assertIn('changed or expired during comparison', answer)
        self.assertIn('My target lines', answer)
        self.assertNotIn("From PrizePicks' public feed", answer)
        self.assertEqual(self.store.list_entries(), [])

    async def test_absent_feed_preserves_legacy_master_flow_and_diagnostics_are_clear(self):
        self.bot.prizepicks_feed = None
        answer = await self.picks()
        self.assertIn('My target lines', answer)
        self.assertNotIn('PrizePicks public feed:', answer)
        self.assertIn('not configured', await self.bot._handle_message(self.message('/pp_status'), 7))
        self.assertEqual(self.store.boards(), [])


if __name__ == '__main__':
    unittest.main()
