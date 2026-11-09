"""On-demand PrizePicks comparisons use master arithmetic and retain provenance."""
import asyncio
import copy
import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch

import analysis_engine
import test_next_matchup_master_lookup as fixtures
import test_prizepicks_bot as public_fixtures
from conversation import _assistant_cards, resolve_conversation
from telegram_bot import TelegramBot, prizepicks_status_reply


NOW, DATE, USER = fixtures.NOW, fixtures.DATE, 123
READINESS = public_fixtures.READINESS
HEADING = 'From PrizePicks via The Odds API'


class FakeOddsFeed(public_fixtures.FakeFeed):
    def __init__(self, game):
        super().__init__(game)
        self.saved_status.update(provider='the_odds_api', provider_label='The Odds API',
                                 credits_remaining=495, local_credits_reserved=5,
                                 monthly_credit_limit=500)
        for row in self.offers + self.legs:
            row['source_kind'] = 'PrizePicks via The Odds API'
            row['source'] = 'https://api.the-odds-api.com'
        self.ensure_for_game = AsyncMock(side_effect=lambda game: self.status())


class TheOddsAPIBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = fixtures.NextMatchupMasterLookupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store = fixture.store
        self.game = self.store.games(DATE)[0]
        self.feed = FakeOddsFeed(self.game)
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

    def message(self, text):
        return {'text': text, 'from': {'id': USER}, 'chat': {'id': USER, 'type': 'private'}}

    async def command(self, text='/picks', number=1):
        return await self.bot._handle_message(self.message(text), number)

    def source_state(self):
        with self.store.connect() as db:
            return {table: [list(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ('games', 'player_games', 'entries', 'news')}

    async def test_picks_checks_exact_game_on_demand_and_compares_both_teams_using_master(self):
        for row in self.feed.offers + self.feed.legs:
            row.update(reference_projection=999999, price=999999, win_probability=1)
        before = self.source_state()
        answer = await self.command()
        self.feed.ensure_for_game.assert_awaited_once()
        checked_game = self.feed.ensure_for_game.await_args.args[0]
        for field in ('id', 'date', 'home_team', 'away_team', 'start_time', 'status'):
            self.assertEqual(checked_game[field], self.game[field])
        self.feed.refresh.assert_not_awaited()
        self.assertIn(HEADING, answer)
        self.assertNotIn("From PrizePicks' public feed", answer)
        self.assertIn('Jalen Brunson — More 10.5 points', answer)
        self.assertIn('Giannis Antetokounmpo — More 10.5 points', answer)
        self.assertIn('mean 21.50', answer)
        self.assertNotIn('999999', answer)
        self.assertEqual(answer.count('My own calculated targets from your master'), 1)
        self.assertIn('My target lines, not quoted PrizePicks offers', answer)
        self.assertIn('Five main market types', answer)
        self.assertIn('Provider prices are excluded', answer)
        self.bot.answer_callback.assert_not_awaited()
        self.assertEqual(self.source_state(), before)
        self.assertEqual(len(self.store.boards()), 1)
        self.assertIn('PrizePicks via The Odds API; telegram:123;', self.store.boards()[0]['source'])
        self.assertEqual(self.store.list_entries(), [])

    async def test_status_is_read_only_and_reports_only_safe_provider_counters(self):
        self.feed.saved_status.update(detail='SECRET_API_KEY', response_body='SECRET_API_KEY',
                                      api_key='SECRET_API_KEY', provider_label='SECRET_API_KEY')
        answer = await self.command('/pp_status')
        self.assertIn('PrizePicks via The Odds API: responding', answer)
        self.assertIn('Provider credits remaining: 495', answer)
        self.assertIn('Saved credit reservations this month: 5', answer)
        self.assertIn('Bot monthly credit limit: 500', answer)
        self.assertNotIn('SECRET_API_KEY', answer)
        self.assertNotIn('Automatic checks continue', answer)
        self.feed.ensure_for_game.assert_not_awaited()
        self.feed.refresh.assert_not_awaited()
        self.feed.offers_for_game.assert_not_called()
        self.assertEqual(self.store.boards(), [])

    async def test_explicit_refresh_remains_a_separate_manual_source_check(self):
        answer = await self.command('/pp_refresh')
        self.feed.refresh.assert_awaited_once_with(force=True)
        self.feed.ensure_for_game.assert_not_awaited()
        self.assertIn(HEADING, answer)
        self.assertIn('Provider credits remaining: 495', answer)
        self.assertEqual(self.store.list_entries(), [])

    async def test_missing_key_quota_and_refused_key_preserve_master_without_source_choices(self):
        for phase, code, http_status in (
                ('not_configured', 'api_key_missing', None),
                ('budget_exhausted', 'budget_exhausted', None),
                ('quota_exhausted', 'quota_exhausted', 401),
                ('invalid_key', 'invalid_api_key', 401),
                ('blocked', 'access_refused', 403)):
            with self.subTest(phase=phase):
                self.feed.fresh = False
                self.feed.saved_status.update(phase=phase, error_code=code, http_status=http_status,
                                              detail='SECRET_KEY_IN_SERVER_ERROR')
                answer = await self.command()
                self.assertIn('My target lines', answer)
                self.assertIn('PrizePicks via The Odds API:', answer)
                self.assertNotIn(HEADING, answer)
                self.assertNotIn('SECRET_KEY_IN_SERVER_ERROR', answer)
                self.assertNotIn('Automatic checks continue', answer)
                self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])

    async def test_source_timeout_keeps_master_output_and_does_not_use_model(self):
        self.feed.fresh = False
        self.feed.saved_status.update(phase='unavailable', error_code='request_timeout')
        self.feed.ensure_for_game.side_effect = TimeoutError('SECRET_KEY_FROM_REQUEST_URL')
        answer = await self.command()
        self.assertIn('My target lines', answer)
        self.assertIn('request_timeout', answer)
        self.assertNotIn('SECRET_KEY_FROM_REQUEST_URL', answer)
        self.assertNotIn(HEADING, answer)
        self.bot.answer_callback.assert_not_awaited()
        self.assertEqual(self.store.list_entries(), [])

    async def test_missing_game_does_not_spend_a_provider_request_for_another_game(self):
        answer = await self.command('/picks 2026-10-01')
        self.assertIn('no matching upcoming game', answer)
        self.feed.ensure_for_game.assert_not_awaited()
        self.feed.offers_for_game.assert_not_called()
        self.assertEqual(self.store.boards(), [])

    async def test_status_can_respond_while_on_demand_check_is_waiting(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(game):
            entered.set()
            await release.wait()
            return self.feed.status()
        self.feed.ensure_for_game.side_effect = waiting
        task = asyncio.create_task(self.command())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            answer = await asyncio.wait_for(self.command('/pp_status', number=2), 1)
            self.assertIn('Provider credits remaining: 495', answer)
            self.feed.refresh.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_directions_remain_quote_previews_and_do_not_make_betting_choices(self):
        self.feed.legs = []
        for row in self.feed.offers:
            row['available_sides'] = []
        answer = await self.command()
        self.assertIn(HEADING, answer)
        self.assertIn('did not confirm usable More/Less', answer)
        self.assertNotIn('Jalen Brunson — More 10.5', answer)
        self.assertIn('My own calculated targets from your master', answer)
        self.assertEqual(self.store.boards(), [])

    async def test_exact_player_line_stat_game_question_recomputes_reason_from_master(self):
        import app
        from chat_flow import answer_conversation
        answer = await self.command()
        request = app.ChatRequest(
            message='Why Brunson over 10.5 points against MIL this game?',
            history=[{'role': 'assistant', 'content': answer}])
        model = AsyncMock()
        with patch('conversation._now', return_value=NOW), \
                patch('uploaded_research_discussion._now', return_value=NOW), \
                patch('uploaded_research_discussion.assess_pregame_freshness', return_value=READINESS):
            result = await answer_conversation(None, self.store, request, {}, model)
        self.assertIn(HEADING, result['answer'])
        self.assertIn('Jalen Brunson — More 10.5 points', result['answer'])
        self.assertIn('20/20 above', result['answer'])
        self.assertIn('mean 21.50', result['answer'])
        self.assertIn('NYK vs MIL on 2026-11-10', result['answer'])
        self.assertIn('No numerical matchup bonus', result['answer'])
        self.assertFalse(result['approved_for_entry'])
        self.assertFalse(result['offer_verified'])
        model.assert_not_called()
        # The source connection is checked by /picks, never by a why question.
        self.feed.ensure_for_game.assert_awaited_once()


NAMES = ['Karl-Anthony Towns', 'Jalen Brunson']
FOLLOWUP_NOW = dt.datetime(2026, 11, 9, 18, tzinfo=dt.timezone.utc)
ANSWER = (
    'Game date: 2026-11-10 Eastern.\n\n' + HEADING + '\n\n'
    '1. Karl-Anthony Towns — More 9.5 rebounds (NYK; standard offer)\n'
    'RESEARCH PICK: Research buffer supported.\n\n'
    '2. Jalen Brunson — Less 0.5 offensive rebounds (NYK; standard offer)\n'
    'RESEARCH PICK: Mixed historical support.\n\n'
    'My own calculated targets from your master\n\n'
    '1. Karl-Anthony Towns · PTS+REB+AST: 36.6\n'
    'More if line ≤30.5; Less if line ≥42.5.\n\n'
    '2. Jalen Brunson · FG attempts: 19.95\n'
    'More if line ≤15.5; Less if line ≥23.5.'
)


class TheOddsAPIFollowupTests(unittest.TestCase):
    def setUp(self):
        p = patch('conversation._players', return_value=NAMES)
        p.start(); self.addCleanup(p.stop)

    def resolve(self, question, *, answer=ANSWER, state=None):
        return resolve_conversation(None, None, question,
            [{'role': 'assistant', 'content': answer}] if state is None else None,
            state, now=FOLLOWUP_NOW)

    def test_source_heading_distinguishes_provider_choices_from_unpriced_master_targets(self):
        cards = _assistant_cards(ANSWER, NAMES, FOLLOWUP_NOW)
        self.assertEqual(len(cards), 4)
        for card in cards[:2]:
            self.assertEqual(card['source_kind'], 'prizepicks_the_odds_api')
            self.assertEqual(card['proposal_group'], 'uploaded')
            self.assertEqual(card['game_date'], DATE)
            self.assertTrue(card['research_choice'])
            self.assertNotIn('offer_verified', card)
        for card in cards[2:]:
            self.assertEqual(card['proposal_group'], 'independent')
            self.assertNotIn('line', card)
            self.assertNotIn('source_kind', card)

    def test_natural_source_ordinal_and_saved_followup_retain_exact_line_and_provider(self):
        for question in ('Why first from The Odds API?', 'Explain first from odds API.',
                         'Why first from PrizePicks?'):
            with self.subTest(question=question):
                turn = self.resolve(question)
                self.assertIsNone(turn['clarification'])
                self.assertEqual(turn['slots']['player'], 'Karl-Anthony Towns')
                self.assertEqual(turn['slots']['line'], 9.5)
                self.assertEqual(turn['slots']['stat'], 'reb')
                self.assertEqual(turn['recent_picks'][0]['source_kind'], 'prizepicks_the_odds_api')
                state = copy.deepcopy(turn['state'])
                for card in state['recent_picks']:
                    card['offer_verified'] = True
                again = self.resolve('Why that one?', state=state)
                self.assertEqual(again['recent_picks'][0]['source_kind'], 'prizepicks_the_odds_api')
                self.assertNotIn('offer_verified', again['recent_picks'][0])

    def test_ambiguous_ordinal_asks_between_provider_lines_and_master_targets(self):
        turn = self.resolve('Why the first pick?')
        self.assertIn('PrizePicks feed lines', turn['clarification'])
        self.assertIn('master targets', turn['clarification'])

    def test_recomputed_master_explanation_keeps_odds_provider_heading(self):
        import test_uploaded_research_picks as research_fixtures
        from uploaded_research_discussion import build_uploaded_research_discussion
        fixture = research_fixtures.UploadedResearchPickTests()
        fixture.setUp()
        try:
            data = fixture.build(fixture.user_fixture())
            first = data['research_picks'][0]
            pick = {key: first[key] for key in ('player', 'stat', 'line')}
            pick.update(side=first['lean_side'], game_date=research_fixtures.DATE, research_choice=True,
                        line_kind='assistant_proposal', proposal_group='uploaded',
                        source_kind='prizepicks_the_odds_api')
            with patch('uploaded_research_discussion.assess_pregame_freshness',
                       return_value=research_fixtures.TIMING_ONLY):
                result = build_uploaded_research_discussion(fixture.store, [pick], now=research_fixtures.NOW)
            self.assertIn(HEADING, result['answer'])
            self.assertNotIn("From PrizePicks' public feed", result['answer'])
            self.assertNotIn('From your uploaded lines', result['answer'])
            turn = resolve_conversation(None, None, 'Why first from The Odds API?',
                [{'role': 'assistant', 'content': result['answer']}], now=research_fixtures.NOW)
            self.assertIsNone(turn['clarification'])
            self.assertEqual(turn['recent_picks'][0]['source_kind'], 'prizepicks_the_odds_api')
        finally:
            fixture.doCleanups()

    def test_changed_line_does_not_inherit_previous_quoted_line_or_research_choice(self):
        reply = ('Game date: 2026-11-10 Eastern.\n' + HEADING + '\n'
                 '1. Jalen Brunson — More 25.5 points (NYK; standard offer)\n'
                 'RESEARCH PICK: Research buffer supported.\n'
                 'My own calculated targets from your master\n'
                 '1. Jalen Brunson · points: 31.5\n'
                 'More if line ≤25.5; Less if line ≥37.5.')
        turn = self.resolve('What about Brunson more 27.5 points?', answer=reply)
        self.assertIsNone(turn['clarification'])
        self.assertEqual(turn['slots']['line'], 27.5)
        self.assertIsNone(turn['state']['selected_pick_index'])
        self.assertEqual(turn['state']['recent_picks'][0]['line'], 25.5)
        self.assertNotIn('line', turn['state']['recent_picks'][1])


if __name__ == '__main__':
    unittest.main()
