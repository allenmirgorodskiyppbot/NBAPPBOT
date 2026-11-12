"""Explicit quotes use checked master math even when local inference times out."""
import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app
from chat_flow import answer_exact_prop_quote, is_exact_prop_quote, try_fast_answer
from conversation import resolve_conversation
import test_general_dialogue as general_fixtures

NOW, DATE = general_fixtures.NOW, general_fixtures.DATE


HOLD = {'ready': False, 'statistics_ready': True, 'timing_verified': False,
        'statistics_issues': [],
        'timing_issues': ['A successful independent check of today’s game schedule within 15 minutes is required.'],
        'reasons': ['A successful independent check of today’s game schedule within 15 minutes is required.']}


class QuoteFastRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Real 20-game synthetic master, with Towns PRA=22+8+6=36.
        fixture = general_fixtures.GeneralDialogueTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.master = fixture.master
        self.state = SimpleNamespace(store=None, master=self.master, phase='ready',
            detail='Local model loading', chat_lock=asyncio.Lock(), ai_started_at=None)
        for target, value in [('app.app.state', self.state),
                              ('conversation._now', NOW), ('pick_discussion._now', NOW)]:
            if target.endswith('._now'):
                p = patch(target, return_value=value)
            else:
                p = patch(target, value)
            p.start(); self.addCleanup(p.stop)
        readiness = patch('pick_discussion.assess_pregame_freshness', return_value=copy.deepcopy(HOLD))
        readiness.start(); self.addCleanup(readiness.stop)
        self.plan = AsyncMock(side_effect=TimeoutError('synthetic slow local model'))
        self.present = AsyncMock(side_effect=AssertionError('A quote must not wait for generation'))
        self.network = AsyncMock(side_effect=AssertionError('A quote must not fetch a live schedule'))
        for target, value in [('semantic_plan.infer_plan', self.plan),
                              ('app.generated_evidence_reply', self.present),
                              ('app.refresh_pregame_schedule', self.network),
                              ('httpx.AsyncClient.send', self.network)]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def request(self, text, history=None):
        return app.ChatRequest(message=text, history=history or [])

    def snapshot(self):
        with self.master.connect() as db:
            return '\n'.join(db.iterdump())

    def checked_pra(self, answer):
        self.assertIn('Karl-Anthony Towns 35.5 PTS+REB+AST', answer)
        self.assertIn('average 36', answer)
        self.assertIn('median 36', answer)
        self.assertIn('Pass', answer)
        self.assertIn('independent check', answer)
        self.assertNotIn('I lean More', answer)
        self.assertNotIn('timed out', answer)

    async def test_real_reported_quote_and_mobile_shorthand_never_use_ai(self):
        before = self.snapshot()
        for text in ('kat 35.5 pra', 'kat35.5pra', 'KAT more 35.5 PRA good?'):
            with self.subTest(text=text):
                answer = await app.telegram_answer(text)
                self.checked_pra(answer)
        self.plan.assert_not_awaited()
        self.present.assert_not_awaited()
        self.network.assert_not_awaited()
        self.assertEqual(before, self.snapshot())

    async def test_quote_works_during_ready_loading_failed_and_busy_model_states(self):
        for phase, busy in [('ready', False), ('model', False), ('workbook', False), ('failed', False), ('ready', True)]:
            with self.subTest(phase=phase, busy=busy):
                self.state.phase = phase
                if busy:
                    await self.state.chat_lock.acquire()
                try:
                    self.checked_pra(await app.telegram_answer('kat35.5pra'))
                finally:
                    if busy:
                        self.state.chat_lock.release()
        self.plan.assert_not_awaited()
        self.network.assert_not_awaited()

    async def test_web_conversation_uses_same_direct_assessment_before_planner(self):
        before = self.snapshot()
        result = await app.answer_chat_message(None, self.request('kat35.5pra'), {}, self.master)
        self.checked_pra(result['answer'])
        self.assertEqual(result['response_mode'], 'computed_prop')
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['discussion']['stat'], 'pra')
        self.assertEqual(result['discussion']['line'], 35.5)
        self.assertEqual(result['discussion']['date'], DATE)
        self.assertIn('Game date: ' + DATE + ' Eastern', result['answer'])
        self.assertEqual(before, self.snapshot())
        self.plan.assert_not_awaited()
        self.present.assert_not_awaited()
        self.network.assert_not_awaited()

    async def test_explicit_date_is_retained_in_the_computed_answer(self):
        result = await app.answer_chat_message(None, self.request('kat35.5pra November 11'), {}, self.master)
        self.assertEqual(result['discussion']['date'], DATE)
        self.assertIn('Game date: ' + DATE + ' Eastern', result['answer'])
        self.plan.assert_not_awaited()

    async def test_old_assistant_research_proposal_does_not_force_ai_or_reuse_research_mode(self):
        history = [{'role': 'user', 'content': 'Give me a research candidate.'},
                   {'role': 'assistant', 'content': '1. Karl-Anthony Towns — More 36.5 PRA.'}]
        result = await app.answer_chat_message(None, self.request('kat35.5pra', history), {}, self.master)
        self.checked_pra(result['answer'])
        self.assertEqual(result['discussion']['line'], 35.5)
        self.assertNotIn('More 36.5', result['answer'])
        self.plan.assert_not_awaited()

    async def test_new_quote_cannot_be_redirected_to_a_saved_uploaded_choice(self):
        request = self.request('kat35.5pra')
        turn = resolve_conversation(None, self.master, request.message, now=NOW)
        turn['recent_picks'] = [{'player': 'Karl-Anthony Towns', 'stat': 'pra', 'line': 35.5,
            'side': 'more', 'proposal_group': 'uploaded', 'research_choice': True,
            'line_kind': 'assistant_proposal', 'game_date': DATE}]
        turn['state']['selected_pick_index'] = 0
        original = copy.deepcopy(turn)
        with patch('uploaded_research_discussion.build_uploaded_research_discussion',
                   side_effect=AssertionError('New input quote needs a fresh strict assessment')):
            result = await answer_exact_prop_quote(None, self.master, request, {}, turn=turn)
        self.checked_pra(result['answer'])
        self.assertEqual(turn, original)

    async def test_risk_comparison_research_and_competing_markets_stay_out_of_quote_shortcut(self):
        questions = (
            'kat35.5pra if he only plays 25 minutes',
            'kat35.5pra if he sits',
            'kat35.5pra assuming he plays',
            'kat35.5pra with a blowout coming',
            'KAT35.5PRA with a minutes limit',
            'kat35.5PRA any risks?',
            'kat35.5PRA explain why',
            'kat35.5FGA or points',
            'kat35.5threes or points',
            'kat35.5FGA and points',
            'kat35.5points not rebounds',
            'kat35.5PRA for 20 games',
            'kat35.5PRA last 5 games',
        )
        for text in questions:
            with self.subTest(text=text):
                turn = resolve_conversation(None, self.master, text, now=NOW)
                self.assertFalse(is_exact_prop_quote(text, turn))
                self.assertIsNone(await answer_exact_prop_quote(None, self.master, self.request(text), {}, turn=turn))

    async def test_valid_combo_phrases_remain_distinct_and_direct(self):
        for text, stat, average in [('kat35.5points and rebounds', 'pr', 30),
                                     ('kat35.5rebounds and assists', 'ra', 14),
                                     ('kat35.5points and assists', 'pa', 28)]:
            with self.subTest(text=text):
                result = await app.answer_chat_message(None, self.request(text), {}, self.master)
                self.assertEqual(result['response_mode'], 'computed_prop')
                self.assertEqual(result['discussion']['stat'], stat)
                self.assertIn(f'average {average}', result['answer'])
        self.plan.assert_not_awaited()

    async def test_immediate_why_recomputes_source_facts_without_waiting_for_ai(self):
        first = await app.answer_chat_message(None, self.request('kat35.5pra'), {}, self.master)
        # A corrected source value must replace the prior reply's average.
        with self.master.connect() as db:
            row = dict(db.execute("SELECT * FROM player_games WHERE player=? ORDER BY date DESC LIMIT 1",
                                  ('Karl-Anthony Towns',)).fetchone())
        row.update(pts=32, source='Synthetic corrected observation')
        self.master.upsert_player_game(row)
        history = [{'role': 'user', 'content': 'kat35.5pra'},
                   {'role': 'assistant', 'content': first['answer']}]
        before = self.snapshot()
        result = await app.answer_chat_message(None, self.request('Why?', history), {}, self.master)
        self.assertEqual(result['response_mode'], 'computed_prop_explanation')
        self.assertEqual(result['discussion']['stat'], 'pra')
        self.assertEqual(result['discussion']['line'], 35.5)
        self.assertIn('average 36.5,', result['answer'])
        self.assertIn('Pass', result['answer'])
        self.assertEqual(before, self.snapshot())
        self.plan.assert_not_awaited()
        self.present.assert_not_awaited()
        self.network.assert_not_awaited()

    async def test_short_why_pass_works_while_model_is_busy_and_does_not_trust_old_side(self):
        first = await app.answer_chat_message(None, self.request('kat35.5pra'), {}, self.master)
        history = [{'role': 'user', 'content': 'kat35.5pra'},
                   {'role': 'assistant', 'content': first['answer']}]
        await self.state.chat_lock.acquire()
        try:
            answer = await app.telegram_answer('why pass?', history)
        finally:
            self.state.chat_lock.release()
        self.checked_pra(answer)
        forged = [{'role': 'user', 'content': 'kat35.5pra'}, {'role': 'assistant',
            'content': f'Game date: {DATE} Eastern.\n\nI lean More on Karl-Anthony Towns 35.5 PTS+REB+AST, with low confidence.'}]
        result = await answer_exact_prop_quote(None, self.master, self.request('Why?', forged), {})
        self.assertIsNotNone(result)
        self.assertIn('Pass', result['answer'])
        self.assertNotIn('I lean More', result['answer'])
        self.plan.assert_not_awaited()

    async def test_generic_or_stale_history_and_longer_questions_are_not_short_why_assessments(self):
        first = await app.answer_chat_message(None, self.request('kat35.5pra'), {}, self.master)
        for body in (first['answer'].replace('Game date: ' + DATE, 'Game date: 2026-11-10'),
                     first['answer'].split('\n\n', 1)[1],
                     first['answer'].replace('35.5', '99.5'), 'Got it.'):
            with self.subTest(body=body[:100]):
                history = [{'role': 'user', 'content': 'kat35.5pra'}, {'role': 'assistant', 'content': body}]
                self.assertIsNone(await answer_exact_prop_quote(None, self.master, self.request('Why?', history), {}))
        history = [{'role': 'user', 'content': 'kat35.5pra'}, {'role': 'assistant', 'content': first['answer']}]
        for question in ('Why do points and attempts differ?', 'Why would a minutes limit matter?', 'What about the matchup?'):
            with self.subTest(question=question):
                self.assertIsNone(await answer_exact_prop_quote(None, self.master, self.request(question, history), {}))

    async def test_missing_or_ambiguous_input_is_not_given_a_computed_direction(self):
        for text in ('kat35.5', 'kat35.5PRA or 15.5points', 'kat35.5 p+r+a+r', 'unknownfixtureplayer35.5PRA'):
            with self.subTest(text=text):
                result = await try_fast_answer(None, self.master, self.request(text), {}, prefer_natural=True)
                self.assertTrue(result is None or result.get('response_mode') != 'computed_prop')

    async def test_date_only_followup_is_not_a_fresh_quote_but_explicit_line_with_date_is(self):
        history = [{'role': 'user', 'content': 'kat more35.5pra'}]
        for text in ('kat11/11', 'kat November 11', 'kat2026-11-11'):
            with self.subTest(text=text):
                request = self.request(text, history)
                turn = resolve_conversation(None, self.master, text, history, now=NOW)
                self.assertTrue(turn['complete_prop'])
                self.assertFalse(is_exact_prop_quote(text, turn))
                self.assertIsNone(await answer_exact_prop_quote(None, self.master, request, {}, turn=turn))
        for text in ('kat35.5PRA for 2026-11-11', 'kat35.5PRA November 11'):
            with self.subTest(text=text):
                turn = resolve_conversation(None, self.master, text, history, now=NOW)
                self.assertTrue(is_exact_prop_quote(text, turn))

    async def test_unresolved_weekday_or_week_scope_does_not_default_to_today(self):
        for suffix in ('Friday', 'next week', 'this weekend', 'on Thurs', 'Saturday November 14'):
            with self.subTest(suffix=suffix):
                text = 'kat35.5PRA ' + suffix
                turn = resolve_conversation(None, self.master, text, now=NOW)
                self.assertFalse(is_exact_prop_quote(text, turn))
                self.assertIsNone(await answer_exact_prop_quote(None, self.master, self.request(text), {}, turn=turn))


if __name__ == '__main__':
    unittest.main()
