"""Availability discussions reach real app routes before model/schedule work."""
import asyncio
import datetime as dt
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app as runtime
import bootstrap
from feeds import canonical_game, canonical_player
from master import MasterStore

NOW = dt.datetime(2026, 11, 11, 19, 30, tzinfo=dt.timezone.utc)
PLAYER = 'Karl-Anthony Towns'


class AvailabilityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.master = MasterStore(Path(folder.name) / 'master.sqlite')
        prior = canonical_game('2026-11-10', 'NYK', 'BOS')
        self.master.upsert_game({'id': prior, 'date': '2026-11-10',
            'home_team': 'NYK', 'away_team': 'BOS', 'status': 'final', 'source': 'Synthetic prior final'})
        self.master.upsert_player_game({'game_id': prior, 'player_id': canonical_player(PLAYER),
            'player': PLAYER, 'team': 'NYK', 'opponent': 'BOS', 'date': '2026-11-10',
            'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
            'pts': 24, 'reb': 10, 'ast': 5, 'minutes': 32, 'source': 'Synthetic runtime test'})
        self.master.upsert_game({'id': canonical_game('2026-11-11', 'NYK', 'MIA'),
            'date': '2026-11-11', 'home_team': 'NYK', 'away_team': 'MIA',
            'status': 'scheduled', 'start_time': '2026-11-12T00:30:00+00:00',
            'source': 'Synthetic saved schedule'})
        self.history = [{'role': 'user', 'content': 'kat 15.5 p+r'},
            {'role': 'assistant', 'content': 'Karl-Anthony Towns 15.5 points + rebounds — Hold for current checks.'}]
        self.message = 'kat is listed as questionable what should we do?'
        for target in ('conversation._now', 'pick_discussion._now'):
            p = patch(target, return_value=NOW)
            p.start()
            self.addCleanup(p.stop)

    def assert_focused(self, answer):
        self.assertIn(PLAYER, answer)
        self.assertIn('questionable', answer.casefold())
        self.assertRegex(answer.casefold(), r'wait|hold')
        self.assertNotIn('15 minutes is required', answer)
        self.assertNotIn('Baseline: 20', answer)

    async def test_telegram_status_question_answers_while_model_not_ready(self):
        state = SimpleNamespace(master=self.master, store=None, phase='workbook', chat_lock=asyncio.Lock())
        forbidden = AsyncMock(side_effect=AssertionError('Focused status reply must not fetch or infer'))
        with patch.object(runtime.app, 'state', state), \
                patch('app.refresh_pregame_schedule', new=forbidden), \
                patch('app.generated_evidence_reply', new=forbidden), \
                patch('httpx.AsyncClient.send', new=forbidden):
            answer = await runtime.telegram_answer(self.message, self.history)
        self.assert_focused(answer)
        forbidden.assert_not_awaited()

    async def test_telegram_status_question_answers_while_model_lock_is_busy(self):
        lock = asyncio.Lock()
        await lock.acquire()
        state = SimpleNamespace(master=self.master, store=None, phase='ready', chat_lock=lock)
        forbidden = AsyncMock(side_effect=AssertionError('Busy model must not block status discussion'))
        with patch.object(runtime.app, 'state', state), patch('app.generated_evidence_reply', new=forbidden):
            answer = await runtime.telegram_answer(self.message, self.history)
        self.assert_focused(answer)
        forbidden.assert_not_awaited()
        self.assertTrue(lock.locked())
        lock.release()

    async def test_web_answer_route_skips_semantic_model_and_schedule_refresh(self):
        request = runtime.ChatRequest(message=self.message, history=self.history)
        forbidden = AsyncMock(side_effect=AssertionError('No model or schedule request for this question'))
        with patch('app.refresh_pregame_schedule', new=forbidden), \
                patch('app.generated_evidence_reply', new=forbidden), \
                patch('semantic_dialogue.infer_dialogue_route', new=forbidden), \
                patch('httpx.AsyncClient.send', new=forbidden):
            result = await runtime.answer_chat_message(None, request, {}, self.master)
        self.assert_focused(result['answer'])
        self.assertFalse(result['ai_used'])
        forbidden.assert_not_awaited()

    def test_deploy_package_contains_focused_module_and_no_reintroduced_offer_api(self):
        self.assertEqual(bootstrap.APP_FILES.count('availability_discussion.py'), 1)
        for name in ('prizepicks_feed.py', 'prizepicks_source.py', 'the_odds_api_feed.py'):
            self.assertNotIn(name, bootstrap.APP_FILES)

    def test_historical_minutes_questions_remain_statistics_requests(self):
        from availability_discussion import can_handle_availability
        turn = {'slots': {'player': PLAYER, 'stat': 'pr', 'line': 15.5,
                          'game_date': '2026-11-11'}, 'entities': {'players': [PLAYER]}}
        for question in ('what were his minutes last 5 games?', 'what is his average minutes?',
                         'show his minutes game logs'):
            with self.subTest(question=question):
                self.assertFalse(can_handle_availability(question, turn))
        self.assertTrue(can_handle_availability('should we trust his average minutes if he is questionable?', turn))

    def test_blowout_rest_without_an_injury_question_keeps_its_basketball_risk_route(self):
        from availability_discussion import can_handle_availability
        turn = {'slots': {'player': PLAYER, 'stat': 'pr', 'line': 15.5,
                          'game_date': '2026-11-11'}, 'entities': {'players': [PLAYER]}}
        self.assertFalse(can_handle_availability('What if he sits in a blowout?', turn))
        self.assertTrue(can_handle_availability('What if he is questionable and sits in a blowout?', turn))


if __name__ == '__main__':
    unittest.main()
