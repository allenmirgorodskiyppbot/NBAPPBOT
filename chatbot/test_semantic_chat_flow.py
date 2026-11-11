"""App integration with semantic plans, real saved readers and mocked transport.

Plan injection checks integration contracts. It does not measure the installed
model's planning accuracy; held-out model trials live outside this suite.
"""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app
import test_chat_flow as fixtures
from conversation_readers import execute_read
from feeds import canonical_game
from season_calendar import import_calendar
from semantic_plan import validate_plan


class SemanticChatFlowTests(unittest.IsolatedAsyncioTestCase):
    add_player = fixtures.ChatFlowTests.add_player
    snapshot = fixtures.ChatFlowTests.snapshot
    explain = fixtures.ChatFlowTests.explain

    def setUp(self):
        fixtures.ChatFlowTests.setUp(self)
        self.plan = AsyncMock()
        self.tasks = []
        self.synthesis = AsyncMock(side_effect=self.model_response)
        self.lookup = AsyncMock(side_effect=AssertionError('General saved reads must not refresh the schedule'))
        self.legacy = AsyncMock(side_effect=AssertionError('Do not start a second model routing chain'))
        for name, value in [('semantic_plan.infer_plan', self.plan),
                            ('general_dialogue._synthesize', self.synthesis),
                            ('conversation_readers.execute_read', self.read),
                            ('conversation_readers._clock', lambda value: fixtures.NOW),
                            ('app.refresh_pregame_schedule', self.lookup),
                            ('semantic_dialogue.infer_dialogue_route', self.legacy)]:
            patcher = patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.choose('research', [{'tool': 'next_game', 'args': {'team': 'NYK'}}])
        import_calendar(self.master, {'season': '2026-27', 'covered_teams': ['NYK', 'CLE'],
            'complete_for_published_games': False, 'retrieved_at': fixtures.NOW.isoformat(),
            'source': 'https://example.test/dated-calendar', 'games': [
                {'date': '2026-11-07', 'home_team': 'CLE', 'away_team': 'NYK',
                 'status': 'scheduled', 'start_time': '2026-11-08T01:00:00Z'}]})
        for day, home, away, hs, aws in [('2026-11-01', 'NYK', 'BOS', 121, 110),
                                       ('2026-10-31', 'CLE', 'MIA', 108, 100)]:
            self.master.upsert_game({'id': canonical_game(day, home, away), 'date': day,
                'home_team': home, 'away_team': away, 'home_score': hs, 'away_score': aws,
                'status': 'final', 'source': 'https://example.test/validated-final'})

    def choose(self, goal, reads=(), *, reply='', clarification=None):
        plan = validate_plan({'goal': goal, 'reads': list(reads), 'reply': reply,
                              'clarification': clarification})
        self.assertIsNotNone(plan)
        self.plan.return_value = plan

    def read(self, master, task, **kwargs):
        self.assertIs(master, self.master)
        self.tasks.append(task)
        return execute_read(master, task, **kwargs)

    async def model_response(self, payload):
        return {'message': {'content': json.dumps({
            'evidence_ids': ['E1'],
            'analysis': ('If workload changes, opportunities could change too. '
                         'The saved sample is context, not a reliable forecast.'),
            'needs_more_data': False,
        })}, 'done_reason': 'stop'}

    async def ask(self, text, history=None):
        return await app.answer_chat_message(None, app.ChatRequest(message=text, history=history or []),
                                            {}, self.master)

    def prop_history(self, *, multiple=False):
        proposal = 'Julius Randle — More 1.5 threes.'
        if multiple:
            proposal = '1. Julius Randle — More 1.5 threes.\n2. Josh Hart — Less 22.5 PRA.'
        return [{'role': 'user', 'content': 'Suggest a research candidate.'},
                {'role': 'assistant', 'content': proposal}]

    async def test_novel_upcoming_wording_selects_real_reader_without_live_refresh(self):
        before = self.snapshot()
        result = await self.ask('whats upcoming')
        self.plan.assert_awaited_once()
        self.assertEqual(self.plan.await_args.args[0].message, 'whats upcoming')
        self.assertEqual(self.tasks, [{'tool': 'next_game', 'args': {'team': 'NYK'}}])
        self.assertIn('NYK at CLE', result['answer'])
        self.assertIn('8:00 PM Eastern', result['answer'])
        self.assertIn('current timing is unverified', result['answer'])
        self.assertEqual(result['response_mode'], 'general_facts')
        self.lookup.assert_not_awaited()
        self.synthesis.assert_not_awaited()
        self.legacy.assert_not_awaited()
        self.assertEqual(self.snapshot(), before)

    async def test_new_game_topic_discards_old_prop_without_rewriting_question(self):
        result = await self.ask('anything on the horizon?', self.prop_history())
        self.assertEqual(self.plan.await_args.args[0].message, 'anything on the horizon?')
        self.assertIn('NYK at CLE', result['answer'])
        self.assertNotIn('1.5', result['answer'])
        self.assertNotIn('discussion', result)
        self.lookup.assert_not_awaited()

    async def test_preview_followup_can_select_game_context_and_team_evidence(self):
        first = await self.ask('whats upcoming')
        self.choose('explain', [{'tool': 'next_game', 'args': {'team': 'NYK'}},
                                {'tool': 'team_games', 'args': {'team': 'NYK', 'window': 5}}])
        result = await self.ask('anything there you would keep an eye on?', [
            {'role': 'user', 'content': 'whats upcoming'},
            {'role': 'assistant', 'content': first['answer']}])
        self.assertEqual([task['tool'] for task in self.tasks], ['next_game', 'next_game', 'team_games'])
        self.assertIn('opportunities could change', result['answer'])
        self.assertEqual(result['response_mode'], 'general_explanation')
        self.synthesis.assert_awaited_once()
        self.lookup.assert_not_awaited()

    async def test_unfamiliar_reason_keeps_exact_focused_prop_for_reader(self):
        self.choose('explain', [{'tool': 'prop_assessment', 'args': {
            'player': 'Julius Randle', 'stat': 'tpm', 'line': 1.5, 'date': '2026-11-02'}}])
        result = await self.ask('Help me see the thinking here.', self.prop_history())
        focus = self.plan.await_args.kwargs['focus']['slots']
        self.assertEqual((focus.get('player'), focus.get('stat'), focus.get('line')),
                         ('Julius Randle', 'tpm', 1.5))
        self.assertEqual(self.tasks[0]['args']['line'], 1.5)
        self.assertNotIn('25.5', result['answer'])
        self.synthesis.assert_awaited_once()
        self.lookup.assert_not_awaited()

    async def test_multi_pick_question_can_clarify_without_selecting_a_leg(self):
        self.choose('clarify', clarification='Which pick do you mean: Randle’s threes or Hart’s PRA?')
        result = await self.ask('I am curious what sold you on it.', self.prop_history(multiple=True))
        self.assertIn('Which pick', result['answer'])
        self.plan.assert_awaited_once()
        self.assertEqual(self.tasks, [])
        self.synthesis.assert_not_awaited()

    async def test_new_market_clarifies_after_semantic_plan_without_old_threshold(self):
        history = [{'role': 'user', 'content': 'Julius Randle more1.5threes'},
                   {'role': 'assistant', 'content': 'Julius Randle — More 1.5 threes.'}]
        self.choose('clarify', clarification='What is Randle’s rebound line?')
        result = await self.ask('What about his boards instead?', history)
        self.plan.assert_awaited_once()
        self.assertNotIn('line', self.plan.await_args.kwargs['focus']['slots'])
        self.assertNotIn('1.5', result['answer'])
        self.assertIn('rebound line', result['answer'])
        self.assertEqual(self.tasks, [])

    async def test_invalid_plan_does_not_launch_old_inference_or_invent_no_events(self):
        self.plan.return_value = None
        result = await self.ask('anything on the horizon?')
        self.assertEqual(result['response_mode'], 'general_clarification')
        self.assertNotIn('no upcoming', result['answer'].lower())
        self.assertFalse(result['ai_used'])
        self.assertEqual(self.tasks, [])
        self.lookup.assert_not_awaited()
        self.synthesis.assert_not_awaited()
        self.legacy.assert_not_awaited()

    async def test_software_subject_can_leave_prior_game_context(self):
        first = await self.ask('whats upcoming')
        self.choose('chat', reply='Review the refresh result and any unresolved data gaps before evaluating an offer.')
        result = await self.ask('The refresh finished; where do I go from here?', [
            {'role': 'user', 'content': 'whats upcoming'},
            {'role': 'assistant', 'content': first['answer']}])
        self.assertIn('unresolved data gaps', result['answer'])
        self.assertNotIn('CLE', result['answer'])
        self.assertEqual(len(self.tasks), 1)
        self.lookup.assert_not_awaited()

    async def test_open_concept_after_old_prop_reaches_plan_before_legacy_clarification(self):
        self.choose('explain', reply=('Assists describe passes leading to made baskets. '
            'Points describe scoring; these statistics measure different contributions.'))
        result = await self.ask('What makes assists different from points?', self.prop_history())
        self.plan.assert_awaited_once()
        self.assertEqual(self.plan.await_args.args[0].message, 'What makes assists different from points?')
        self.assertIn('different contributions', result['answer'])
        self.assertNotIn('Which', result['answer'])
        self.assertNotIn('1.5', result['answer'])
        self.assertEqual(self.tasks, [])
        self.lookup.assert_not_awaited()

    async def test_complete_prop_computes_decision_without_creating_board_or_entry(self):
        self.choose('evaluate', [{'tool': 'prop_assessment', 'args': {
            'player': 'Julius Randle', 'stat': 'tpm', 'line': 1.5, 'date': '2026-11-02'}}])
        before = self.snapshot()
        result = await self.ask('Julius Randle more1.5threes good?')
        self.assertEqual(self.tasks[0]['args']['line'], 1.5)
        self.assertIn('Julius Randle', result['answer'])
        self.assertEqual(result['response_mode'], 'general_facts')
        self.synthesis.assert_not_awaited()
        self.assertEqual(self.snapshot(), before)

    async def test_wrong_generated_scoring_fact_falls_back_to_reader_data(self):
        self.choose('explain', [{'tool': 'team_games', 'args': {'team': 'NYK', 'window': 1}}])
        self.synthesis.side_effect = None
        self.synthesis.return_value = {'message': {'content': json.dumps({
            'evidence_ids': ['E1'], 'analysis': 'The Knicks averaged 999 points per game.',
            'needs_more_data': False})}}
        result = await self.ask('How should I read that scoring sample?')
        self.assertNotIn('999', result['answer'])
        self.assertIn('121', result['answer'])
        self.assertEqual(result['response_mode'], 'general_facts')
        self.assertFalse(result['ai_used'])

    async def test_explicit_team_change_selects_that_saved_schedule(self):
        self.choose('research', [{'tool': 'next_game', 'args': {'team': 'CLE'}}])
        result = await self.ask('What is coming up for Cleveland?', self.prop_history())
        self.assertEqual(self.tasks[0]['args']['team'], 'CLE')
        self.assertIn('CLE', result['answer'])
        self.assertNotIn('1.5', result['answer'])

    async def test_not_ready_and_busy_keep_exact_saved_calendar_fallback(self):
        state = SimpleNamespace(store=None, master=self.master, phase='workbook', detail='Loading',
                                chat_lock=asyncio.Lock(), ai_started_at=None)
        with patch.object(app.app, 'state', state):
            first = await app.telegram_answer('next Knicks game')
            self.assertIn('Next saved matchup', first)
            state.phase = 'ready'
            await state.chat_lock.acquire()
            try:
                second = await app.telegram_answer('next Knicks game')
            finally:
                state.chat_lock.release()
        self.assertEqual(first, second)
        self.plan.assert_not_awaited()
        self.synthesis.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
