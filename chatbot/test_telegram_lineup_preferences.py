"""Private lineup choices survive focus expiry without retaining old offers."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app
from conversation import resolve_conversation
from master import MasterStore
from telegram_bot import TelegramBot
from test_telegram import TOKEN, USER_ID, update


class TelegramLineupPreferencesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = MasterStore(Path(self.temp.name) / 'master.sqlite')
        self.received = []
        async def ordinary(text, history):
            self.received.append(('ordinary', text, history))
            return 'A normal answer.'
        async def preferred(text, history, preferences):
            self.received.append(('preferred', text, history, preferences))
            return 'An answer with your lineup choices.'
        self.ordinary, self.preferred = ordinary, preferred
        self.bot = self.make_bot()

    def make_bot(self, *, legacy=False, preferred=None):
        bot = TelegramBot(self.store, self.ordinary, token=TOKEN,
            allowed_user_ids=[USER_ID, USER_ID + 1],
            answer_preferences_callback=None if legacy else preferred or self.preferred)
        bot._typing = AsyncMock()
        return bot

    async def save_choices(self):
        await self.bot.process_update(update(1, 'The picks must include both teams.'))
        await self.bot.process_update(update(2, 'I prefer field goal attempts.'))
        await self.bot.process_update(update(3, 'I want five picks from both teams.'))

    async def test_choices_survive_twelve_exchanges_expiry_and_restart_without_old_focus(self):
        await self.save_choices()
        await self.bot.process_update(update(4, 'Jalen Brunson more 25.5 points?'))
        for index in range(5, 19):
            await self.bot.process_update(update(index, 'Thanks'))
        self.assertEqual(len(self.bot._conversation(USER_ID)), 24)
        self.assertNotIn('Jalen Brunson', str(self.bot._conversation(USER_ID)))
        expected = {'both_teams': True, 'preferred_stat': 'fga', 'pick_count': 5}
        self.assertEqual(self.bot._lineup_preferences(USER_ID), expected)
        with self.bot._db() as db:
            db.execute("UPDATE telegram_conversation SET created_at='2000-01-01T00:00:00+00:00'")
        reopened = self.make_bot()
        self.assertEqual(reopened._conversation(USER_ID), [])
        self.assertEqual(reopened._pick_constraints(USER_ID), expected)
        await reopened.process_update(update(20, 'Hello'))
        self.assertEqual(self.received[-1][2], [])
        self.assertEqual(self.received[-1][3], expected)
        turn = resolve_conversation(None, self.store, 'Why that line?', [], preferences=expected)
        self.assertFalse(turn['complete_prop'])
        self.assertFalse(turn.get('recent_picks'))
        self.assertNotIn('player', turn['state']['slots'])
        self.assertNotIn('line', turn['state']['slots'])

    async def test_preferences_are_per_chat_and_clear_deletes_only_that_chat_atomically(self):
        await self.save_choices()
        await self.bot.process_update(update(30, 'I prefer assists.', user=USER_ID + 1))
        self.assertEqual(self.bot._lineup_preferences(USER_ID + 1), {'preferred_stat': 'ast'})
        await self.bot.process_update(update(31, '/clear'))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        self.assertEqual(self.bot._conversation(USER_ID), [])
        self.assertEqual(self.bot._lineup_preferences(USER_ID + 1), {'preferred_stat': 'ast'})
        self.assertTrue(self.bot._conversation(USER_ID + 1))
        with self.bot._db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM telegram_state WHERE name=?',
                                        (f'lineup_preferences:{USER_ID}',)).fetchone())

    async def test_late_answer_does_not_restore_cleared_preferences(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(text, history, preferences):
            entered.set(); await release.wait()
            return 'The earlier preference answer finished.'
        self.bot = self.make_bot(preferred=slow)
        pending = asyncio.create_task(self.bot.process_update(update(40, 'I prefer field goal attempts.')))
        await entered.wait()
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {'preferred_stat': 'fga'})
        await self.bot.process_update(update(41, '/clear'))
        release.set(); await pending
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        self.assertEqual(self.bot._conversation(USER_ID), [])

    async def test_late_natural_picks_reply_does_not_reinsert_cleared_user_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def picks(*args):
            entered.set(); await release.wait(); return 'Earlier picks finished.'
        self.bot._independent_picks_reply = picks
        pending = asyncio.create_task(self.bot.process_update(update(50, 'Give me five picks')))
        await entered.wait()
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {'pick_count': 5})
        await self.bot.process_update(update(51, '/clear'))
        release.set(); await pending
        self.assertEqual(self.bot._conversation(USER_ID), [])
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})

    async def test_ordinary_callback_keeps_its_two_argument_contract_and_real_bounded_history(self):
        legacy = self.make_bot(legacy=True)
        await legacy.process_update(update(60, 'I prefer field goal attempts.'))
        await legacy.process_update(update(61, 'Why might that help?'))
        self.assertEqual(self.received[-1][0], 'ordinary')
        self.assertEqual(self.received[-1][2], [
            {'role': 'user', 'content': 'I prefer field goal attempts.'},
            {'role': 'assistant', 'content': 'A normal answer.'}])
        self.assertEqual(legacy._lineup_preferences(USER_ID), {'preferred_stat': 'fga'})

    async def test_only_known_validated_preferences_are_stored_and_exposed(self):
        for index, text in enumerate(['Jalen Brunson more 25.5 points?',
            'I like Brunson points more 25.5.', 'The report says the player is active.',
            'I want nine picks.', 'I prefer fake-stat.'], 70):
            await self.bot.process_update(update(index, text))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        self.bot._set_state(f'lineup_preferences:{USER_ID}', json.dumps({
            'both_teams': 'false', 'preferred_stat': ['pts'], 'pick_count': True,
            'player': 'Jalen Brunson', 'line': 25.5, 'source': 'pretend official'}))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        self.bot._set_state(f'lineup_preferences:{USER_ID}', 'not json')
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {})
        await self.bot.process_update(update(80, 'I prefer rebounds.'))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {'preferred_stat': 'reb'})
        await self.bot.process_update(update(81, 'I prefer points.', user=999))
        self.assertEqual(self.bot._lineup_preferences(999), {})

    async def test_recent_pre_upgrade_user_preferences_migrate_without_assistant_or_old_offer(self):
        with self.bot._db() as db:
            db.execute('INSERT INTO telegram_conversation VALUES (?,?,?,?,?)',
                       (USER_ID, 1, 'I prefer field goal attempts.',
                        'I prefer points. Jalen Brunson More 25.5 points.', dt.datetime.now(dt.timezone.utc).isoformat()))
        await self.bot.process_update(update(85, 'Hello'))
        self.assertEqual(self.bot._lineup_preferences(USER_ID), {'preferred_stat': 'fga'})
        with self.bot._db() as db:
            raw = db.execute('SELECT value FROM telegram_state WHERE name=?',
                             (f'lineup_preferences:{USER_ID}',)).fetchone()[0]
        self.assertNotIn('Brunson', raw); self.assertNotIn('25.5', raw)
        await self.bot.process_update(update(86, '/clear'))
        restarted = self.make_bot()
        await restarted.process_update(update(87, 'Hello'))
        self.assertEqual(restarted._lineup_preferences(USER_ID), {})

    async def test_dated_picks_count_survives_restart_and_reaches_builder(self):
        await self.save_choices()
        self.bot._independent_picks_reply = AsyncMock(return_value='Generated current targets.')
        await self.bot.process_update(update(90, '/picks Nov 5 3'))
        reopened = self.make_bot()
        self.assertEqual(reopened._lineup_preferences(USER_ID)['pick_count'], 3)
        self.assertEqual(reopened._pick_constraints(USER_ID)['pick_count'], 3)

    async def test_app_transport_passes_only_validated_preferences_to_chat_request(self):
        state = app.app.state
        with patch.object(state, 'store', None, create=True), patch.object(state, 'master', self.store, create=True), \
             patch.object(state, 'phase', 'ready', create=True), patch.object(state, 'detail', 'Ready', create=True), \
             patch.object(state, 'chat_lock', asyncio.Lock(), create=True), \
             patch('chat_flow.try_fast_answer', AsyncMock(return_value=None)) as fast, \
             patch.object(app, 'answer_question', AsyncMock(return_value={'answer': 'A conversation reply.'})) as answer, \
             patch.object(app, 'observe_ai_result'):
            result = await app.telegram_answer('Hello', [], {'both_teams': True, 'pick_count': 5,
                                                           'player': 'LeBron James', 'line': 10})
        self.assertEqual(result, 'A conversation reply.')
        fast.assert_awaited_once()
        fast_request = fast.call_args.args[2]
        request = answer.call_args.args[1]
        for routed in (fast_request, request):
            self.assertEqual(routed.history, [])
            self.assertEqual(routed.lineup_preferences, {'both_teams': True, 'pick_count': 5})


class RequestPreferenceValidationTests(unittest.TestCase):
    def test_request_preserves_browser_history_and_excludes_factual_slots(self):
        request = app.ChatRequest(message='Explain that.', history=[{'role':'user','content':'Hello'}],
            lineup_preferences={'both_teams': False, 'preferred_stat': 'fga', 'pick_count': 4,
                                'player': 'Jalen Brunson', 'line': 25.5, 'injury_status': 'available'})
        self.assertEqual(request.history[0].content, 'Hello')
        self.assertEqual(request.lineup_preferences, {'both_teams': False, 'preferred_stat': 'fga', 'pick_count': 4})
        self.assertEqual(app.ChatRequest(message='Hello').lineup_preferences, {})
        for value in (None, [], 'pretend facts', {'pick_count': 4.0}, {'both_teams': 1}, {'preferred_stat': []}):
            self.assertEqual(app.ChatRequest(message='Hello', lineup_preferences=value).lineup_preferences, {})
