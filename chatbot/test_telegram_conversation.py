"""Telegram conversation memory reaches real recommendation and upload paths."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from conversation import resolve_conversation
from feeds import canonical_game, canonical_player
from independent_picks import build_independent_picks
from master import MasterStore
from season_calendar import seed_calendar
from telegram_bot import TelegramBot
from test_board_shortlist import candidate, report
from test_screenshot_intake import card, extraction
from test_telegram import TOKEN, USER_ID, update


NOW = dt.datetime(2026, 10, 30, 22, 40, tzinfo=dt.timezone.utc)
DAY = '2026-10-30'


class TelegramConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This suite verifies transport/history/preferences. Real publication
        # readiness is separately covered with actual daily source checkpoints.
        for name in ('board_shortlist', 'independent_picks'):
            readiness = patch(name + '.assess_pregame_freshness', return_value={
                'ready': True, 'reasons': [], 'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga']})
            readiness.start()
            self.addCleanup(readiness.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)
        for player, team in [('Jalen Brunson', 'NYK'), ('Josh Hart', 'NYK'),
                             ('Tyrese Maxey', 'PHI'), ('Joel Embiid', 'PHI')]:
            samples = [(offset, (dt.date(2026, 10, 29) - dt.timedelta(days=offset)).isoformat(), '2026-27')
                       for offset in range(12)]
            samples += [(offset, (dt.date(2026, 4, 10) - dt.timedelta(days=offset)).isoformat(), '2025-26')
                        for offset in range(8)]
            for offset, day, season in samples:
                game = canonical_game(day, team, 'BOS')
                self.store.upsert_game({'id': game, 'date': day, 'home_team': team,
                    'away_team': 'BOS', 'status': 'final', 'source': 'conversation test fixture'})
                self.store.upsert_player_game({'game_id': game, 'date': day,
                    'player': player, 'player_id': canonical_player(player), 'team': team,
                    'opponent': 'BOS', 'season': season, 'season_type': 'Regular Season',
                    'appearance_state': 'Played', 'minutes': 33, 'pts': 24 + offset % 3,
                    'reb': 6, 'ast': 4, 'fga': 18, 'source': 'conversation test fixture'})
        self.received = []

        async def answer(text, history):
            self.received.append((text, history))
            return 'I can explain the saved recommendation.'

        self.bot = TelegramBot(self.store, answer, token=TOKEN, allowed_user_ids=[USER_ID])
        self.bot._typing = AsyncMock()
        self.bot._refresh_board_news = AsyncMock()
        self.bot._download = AsyncMock(return_value=b'isolated image fixture')

    def financial_counts(self):
        with self.store.connect() as db:
            return {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    for name in ('entries', 'grade_events')}

    def last_output(self):
        with self.store.connect() as db:
            return db.execute('SELECT text FROM telegram_outbox ORDER BY id DESC LIMIT 1').fetchone()[0]

    def analysis_fixture(self, store, board_id):
        """Transport fixture retains the real immutable board and every offered side."""
        board = store.get_board(board_id)
        items = []
        for leg in board['legs']:
            item = candidate(leg['player'], side=leg['side'], line=leg['line'],
                             team=leg.get('team', 'NYK'), stat=leg['stat'])
            item['leg'] = dict(leg)
            items.append(item)
        return {**report(*items), 'board_id': board_id}

    async def test_history_preferences_survive_reopen_and_clear_explicitly(self):
        before = self.financial_counts()
        await self.bot.process_update(update(1, 'I need four picks from both teams.'))
        await self.bot.process_update(update(2, 'I prefer field goal attempts.'))
        constraints = self.bot._pick_constraints(USER_ID)
        self.assertEqual(constraints, {'both_teams': True, 'preferred_stat': 'fga', 'pick_count': 4})
        await self.bot.process_update(update(3, "I don't need both teams for my picks."))
        self.assertFalse(self.bot._pick_constraints(USER_ID)['both_teams'])
        reopened = TelegramBot(self.store, self.bot.answer_callback, token=TOKEN,
                               allowed_user_ids=[USER_ID])
        self.assertEqual(reopened._pick_constraints(USER_ID), self.bot._pick_constraints(USER_ID))
        await self.bot.process_update(update(4, 'The picks must include both teams.'))
        self.assertTrue(self.bot._pick_constraints(USER_ID)['both_teams'])
        await self.bot.process_update(update(5, '/clear'))
        self.assertEqual(self.bot._pick_constraints(USER_ID), {})
        self.assertEqual(self.financial_counts(), before)

    async def test_picks_command_uses_preferences_and_preserves_targets_for_why(self):
        await self.bot.process_update(update(10, 'I prefer field goal attempts.'))
        await self.bot.process_update(update(11, 'The picks must include both teams.'))
        built = []

        def build(store, **kwargs):
            value = build_independent_picks(store, now=NOW, **kwargs)
            built.append(value)
            return value

        before = self.financial_counts()
        with patch('independent_picks.build_independent_picks', side_effect=build):
            await self.bot.process_update(update(12, '/picks ' + DAY))
        self.assertEqual(len(built), 1)
        self.assertTrue(built[0]['lineup_complete'])
        self.assertEqual({p['stat'] for p in built[0]['picks']}, {'fga'})
        self.assertEqual(built[0]['selected_teams'], ['NYK', 'PHI'])
        reply = self.last_output()
        self.assertIn('My target lines, not quoted PrizePicks offers.', reply)
        self.assertEqual(self.bot._conversation(USER_ID)[-1]['content'], reply)
        await self.bot.process_update(update(13, 'Why did you pick those?'))
        text, history = self.received[-1]
        turn = resolve_conversation(None, self.store, text, history, now=NOW)
        self.assertEqual(turn['intent'], 'explanation')
        self.assertEqual(len(turn['recent_picks']), 4)
        self.assertTrue(all(p['line_kind'] == 'research_target' and 'line' not in p
                            for p in turn['recent_picks']))
        self.assertFalse(turn['complete_prop'])
        self.assertEqual(self.financial_counts(), before)

    async def test_shortlist_receives_persisted_count_and_team_rule(self):
        await self.bot.process_update(update(20, 'I need four picks from both teams.'))
        items = [candidate('Jalen Brunson'), candidate('Josh Hart'),
                 candidate('Tyrese Maxey', team='PHI'), candidate('Joel Embiid', team='PHI')]
        board = self.store.create_board([item['leg'] for item in items], source='conversation fixture')
        analysis = {**report(*items), 'board_id': board['id']}
        from board_shortlist import enrich_shortlist
        with patch('board_shortlist.enrich_shortlist', wraps=enrich_shortlist) as enrich, \
             patch('board_shortlist._now', return_value=NOW):
            text = await self.bot._shortlist_reply(analysis, USER_ID)
        self.assertEqual(enrich.call_args.kwargs['count'], 4)
        self.assertTrue(enrich.call_args.kwargs['constraints']['both_teams'])
        self.assertEqual(text.count('More 19.5 points'), 4)

    async def test_picks_reuses_count_unless_current_request_changes_it(self):
        await self.bot.process_update(update(60, 'I need three picks from both teams.'))
        counts = []

        def build(store, **kwargs):
            counts.append(kwargs['count'])
            return build_independent_picks(store, now=NOW, **kwargs)

        with patch('independent_picks.build_independent_picks', side_effect=build):
            await self.bot.process_update(update(61, '/picks ' + DAY))
            await self.bot.process_update(update(62, '/picks ' + DAY + ' 4'))
        self.assertEqual(counts, [3, 4])

    async def test_board_and_analysis_commands_save_quoted_lines_for_followup(self):
        before = self.financial_counts()
        command = '/board ' + DAY + '\nJalen Brunson | points | more | 19.5'
        with patch('analysis_engine.analyze_board', side_effect=self.analysis_fixture), \
             patch('board_shortlist._now', return_value=NOW):
            await self.bot.process_update(update(30, command))
            board = self.store.boards()[0]
            await self.bot.process_update(update(31, '/analyze ' + board['id']))
        history = self.bot._conversation(USER_ID)
        self.assertIn('PrizePicks board saved:', history[1]['content'])
        self.assertIn('More 19.5 points', history[-1]['content'])
        turn = resolve_conversation(None, self.store, 'Why Brunson?', history, now=NOW)
        self.assertTrue(turn['complete_prop'])
        self.assertEqual((turn['slots']['player'], turn['slots']['stat'], turn['slots']['line']),
                         ('Jalen Brunson', 'pts', 19.5))
        self.assertEqual(self.financial_counts(), before)

    async def test_screenshot_reply_is_remembered_without_recording_a_placed_entry(self):
        before = self.financial_counts()
        photo = update(40, '')
        photo['message'].pop('text')
        photo['message'].update(caption=DAY, date=NOW.timestamp(),
            photo=[{'file_id': 'isolated-photo', 'width': 1000, 'height': 1500, 'file_size': 100}])
        with patch('screenshot_board.extract_screenshot', return_value=extraction(card(line=19.5))), \
             patch('analysis_engine.analyze_board', side_effect=self.analysis_fixture), \
             patch('board_shortlist._now', return_value=NOW):
            await self.bot.process_update(photo)
        self.assertEqual(len(self.store.boards()), 1)
        history = self.bot._conversation(USER_ID)
        self.assertIn('Read 1 distinct lines', history[-1]['content'])
        self.assertNotIn('SYNTHETIC RAW TEXT', history[-1]['content'])
        await self.bot.process_update(update(41, 'Why Brunson?'))
        text, passed_history = self.received[-1]
        turn = resolve_conversation(None, self.store, text, passed_history, now=NOW)
        self.assertTrue(turn['complete_prop'])
        self.assertEqual(turn['slots']['line'], 19.5)
        self.assertEqual(turn['slots']['stat'], 'pts')
        self.assertEqual(self.financial_counts(), before)


if __name__ == '__main__':
    unittest.main()
