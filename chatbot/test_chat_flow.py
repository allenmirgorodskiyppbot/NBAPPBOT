"""The real dialogue/context/assessment path, with only language generation mocked."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app
from chat_flow import answer_conversation
from feeds import canonical_game, canonical_player
from master import MasterStore


NOW = dt.datetime(2026, 11, 2, 21, tzinfo=dt.timezone.utc)


class ChatFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.master = MasterStore(Path(self.folder.name) / 'conversation.sqlite')
        self.add_player('Josh Hart', 'NYK', [17, 19, 23, 21, 18, 19], 'pra')
        self.add_player('Julius Randle', 'BKN', [4, 3, 0, 0, 2, 3], 'tpm')
        self.add_player('Jalen Brunson', 'NYK', [24, 38, 23, 28, 19, 21], 'pts')
        self.master.upsert_game({'id': 'tonight', 'date': '2026-11-02',
            'home_team': 'NYK', 'away_team': 'BKN', 'status': 'scheduled',
            'start_time': '2026-11-03T00:30:00+00:00', 'source': 'synthetic calendar'})
        for name in ('conversation._now', 'pick_discussion._now', 'research_discussion._now'):
            patcher = patch(name, return_value=NOW)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.generated = []
        self.generate = AsyncMock(side_effect=self.explain)

    def add_player(self, player, team, values, stat):
        for index, value in enumerate(values):
            date = (dt.date(2026, 11, 1) - dt.timedelta(days=index)).isoformat()
            game_id = canonical_game(date, team, 'BOS')
            self.master.upsert_game({'id': game_id, 'date': date, 'home_team': team,
                'away_team': 'BOS', 'status': 'final', 'source': 'synthetic completed games'})
            row = {'game_id': game_id, 'player_id': canonical_player(player), 'player': player,
                'team': team, 'opponent': 'BOS', 'date': date, 'season': '2026-27',
                'season_type': 'Regular Season', 'appearance_state': 'Played',
                'source': 'synthetic player observations', 'minutes': 34,
                'pts': 20, 'reb': 5, 'ast': 4, 'tpm': 2, 'tpa': 7, 'fga': 18}
            if stat == 'pra':
                row['pts'] = value - row['reb'] - row['ast']
            else:
                row[stat] = value
            self.master.upsert_player_game(row)

    async def explain(self, request, progress, master, cards, **kwargs):
        self.generated.append({'request': request, 'cards': cards, **kwargs})
        return {'answer': kwargs.get('fallback') or 'Here is a conversational explanation.',
                'sources': [], 'ai_used': True}

    def snapshot(self):
        with self.master.connect() as db:
            return '\n'.join(db.iterdump())

    async def ask(self, text, history=None, *, through_app=False):
        request = app.ChatRequest(message=text, history=history or [])
        if through_app:
            with patch.object(app, 'generated_evidence_reply', self.generate):
                return await app.answer_question(None, request, {}, self.master)
        return await answer_conversation(None, self.master, request, {}, self.generate)

    @staticmethod
    def remember(history, question, result):
        history.extend([{'role': 'user', 'content': question},
                        {'role': 'assistant', 'content': result['answer']}])

    async def test_hart_screenshot_exchange_clarifies_then_assesses_then_explains(self):
        before = self.snapshot()
        history = []
        question = 'The Josh hart line is at 22.5 u choose up or down'
        first = await self.ask(question, history, through_app=True)
        self.assertFalse(first['ai_used'])
        self.assertIn('22.5', first['answer'])
        self.assertIn('PRA', first['answer'])
        self.generate.assert_not_awaited()
        self.remember(history, question, first)

        question = '22.5 I mean for points rebounds assists line'
        second = await self.ask(question, history, through_app=True)
        self.assertEqual(second['discussion']['player'], 'Josh Hart')
        self.assertEqual(second['discussion']['stat'], 'pra')
        self.assertEqual(second['discussion']['line'], 22.5)
        self.assertEqual(second['discussion']['decision'], 'less')
        self.assertTrue(self.generated[-1]['natural'])
        self.assertTrue(self.generated[-1]['cards'])
        self.remember(history, question, second)

        question = 'Up or down choose'
        third = await self.ask(question, history, through_app=True)
        self.assertEqual(third['discussion']['decision'], 'less')
        self.assertEqual(third['discussion']['stat'], 'pra')
        self.remember(history, question, third)

        last = await self.ask('Why do you think that is a good pick?', history, through_app=True)
        self.assertEqual(last['discussion']['player'], 'Josh Hart')
        self.assertEqual(last['discussion']['line'], 22.5)
        self.assertEqual(self.generated[-1]['dialogue']['intent'], 'explanation')
        facts = [fact for card in self.generated[-1]['cards'] for fact in card.get('approved_facts', [])]
        self.assertTrue(any(fact.get('entity') == 'Josh Hart' for fact in facts))
        self.assertTrue(all(fact.get('entity') != 'Julius Randle' for fact in facts))
        self.assertEqual(before, self.snapshot())

    async def test_randal_typo_reaches_three_point_assessment(self):
        result = await self.ask('Does Julius Randal more1.5threes a good line', through_app=True)
        self.assertEqual(result['discussion']['player'], 'Julius Randle')
        self.assertEqual(result['discussion']['stat'], 'tpm')
        self.assertEqual(result['discussion']['line'], 1.5)
        self.assertEqual(result['discussion']['decision'], 'more')
        self.assertTrue(self.generated[-1]['cards'])
        self.assertIn('shooting accuracy', self.generated[-1]['fallback'])

    async def test_both_team_rule_acknowledges_constraint_without_team_stat_dump(self):
        text = 'The picks we choose we must have at least one from each team. Can’t be all Knicks'
        before = self.snapshot()
        result = await self.ask(text, through_app=True)
        self.assertIn('each team', result['answer'])
        self.assertEqual(self.generated[-1]['cards'], [])
        self.assertEqual(self.generated[-1]['dialogue']['intent'], 'lineup_constraint')
        self.assertTrue(self.generated[-1]['dialogue']['constraints']['both_teams'])
        self.assertEqual(before, self.snapshot())

    async def test_unrelated_data_requests_are_left_to_legacy_retrieval(self):
        history = [{'role': 'user', 'content': 'Josh Hart 22.5 PRA more or less'}]
        for text in ['Read the record', 'Show Jalen Brunson last 5 games points',
                     'Show current tracking data', 'Read the stored passing record']:
            with self.subTest(text=text):
                self.assertIsNone(await self.ask(text, history))
        self.generate.assert_not_awaited()

    async def test_ordinary_player_log_still_uses_actual_legacy_calculation(self):
        result = await self.ask('Show Josh Hart last 5 games points', through_app=True)
        self.assertFalse(result['ai_used'])
        self.assertIn('stored games played', result['answer'])
        self.assertIn('pts=', result['answer'])
        self.generate.assert_not_awaited()

    async def test_followup_market_change_does_not_reuse_points_threshold(self):
        history = [{'role': 'user', 'content': 'Brunson more 19.5 points'}]
        result = await self.ask('What about his field goal attempts?', history, through_app=True)
        self.assertFalse(result['ai_used'])
        self.assertIn('line', result['answer'])
        self.assertNotIn('19.5', result['answer'])
        self.generate.assert_not_awaited()

    async def test_preference_does_not_invent_a_new_line_or_force_a_recommendation(self):
        history = [{'role': 'user', 'content': 'Brunson more 19.5 points'}]
        result = await self.ask('I liked the attempts more', history, through_app=True)
        self.assertNotIn('discussion', result)
        self.assertTrue(self.generated[-1]['cards'])
        self.assertIn('Jalen Brunson', str(self.generated[-1]['cards']))
        self.assertNotIn('19.5', self.generated[-1]['request'].message)

    async def test_named_matchup_with_brevity_request_uses_real_scoped_evidence(self):
        base = 'How does Jalen Brunson match up against Brooklyn, and what could affect his scoring?'
        before = self.snapshot()
        for text in [base + ' Keep it brief.', base + ' Keep the answer short.',
                     'Briefly, ' + base, 'I want to know how Jalen Brunson matches up against Brooklyn.',
                     'Tell me why Jalen Brunson might score differently against Brooklyn.',
                     'Tell me why Jalen Brunson is a good scorer against Brooklyn.']:
            with self.subTest(text=text):
                self.generated.clear()
                result = await self.ask(text, through_app=True)
                self.assertTrue(result['ai_used'])
                generated = self.generated[-1]
                self.assertTrue(generated['cards'], 'A style instruction must not bypass basketball evidence.')
                self.assertTrue(generated.get('natural'))
                evidence = str(generated['cards'])
                self.assertIn('Jalen Brunson', evidence)
                self.assertNotIn('Julius Randle', evidence)
                self.assertNotIn('Josh Hart', evidence)
                self.assertNotIn('discussion', result)
        self.assertEqual(before, self.snapshot())

    async def test_social_thanks_is_generated_without_old_statistics(self):
        history = [{'role': 'user', 'content': 'Julius Randle more 1.5 threes'}]
        await self.ask('thank you', history, through_app=True)
        self.assertEqual(self.generated[-1]['dialogue']['intent'], 'chat')
        self.assertEqual(self.generated[-1]['cards'], [])

    async def test_supported_shortlist_followup_recomputes_facts_from_database(self):
        history = [{'role': 'assistant', 'content': 'My shortlist\n'
            '1. Josh Hart — Less 22.5 PTS+REB+AST (standard offer)\n'
            'Why: A previous explanation may be stale.\n'
            '2. Julius Randle — More 1.5 3-pointers made (Demon offer)'}]
        before = self.snapshot()
        await self.ask('Why did you choose those picks?', history, through_app=True)
        generated = self.generated[-1]
        self.assertTrue(generated['cards'])
        entities = {fact['entity'] for card in generated['cards'] for fact in card.get('approved_facts', [])}
        self.assertEqual(entities, {'Josh Hart', 'Julius Randle'})
        self.assertEqual({row['player'] for row in generated['dialogue']['assessments']}, entities)
        self.assertEqual(before, self.snapshot())

    async def test_research_target_followup_has_evidence_without_claiming_actual_offer(self):
        self.add_player('Josh Hart', 'NYK', [17, 19, 23, 21, 18, 19, 20, 22], 'pra')
        self.add_player('Jalen Brunson', 'NYK', [24, 38, 23, 28, 19, 21, 27, 26], 'pts')
        history = [{'role': 'user', 'content': '/picks Nov 2'},
                   {'role': 'assistant', 'content': 'My projections — NYK vs BKN · Nov 02\n'
                    'My target lines, not quoted PrizePicks offers.\n'
                    '1. Josh Hart · PTS+REB+AST: 20\n'
                    'More if line ≤15.5; Less if line ≥24.5.\n'
                    'Why: Comparable workload.\n'
                    '2. Jalen Brunson · points: 25.5\n'
                    'More if line ≤19.5; Less if line ≥31.5.'}]
        before = self.snapshot()
        await self.ask('Why did you choose those picks?', history, through_app=True)
        generated = self.generated[-1]
        self.assertTrue(generated['cards'], 'Research-target explanations still need freshly retrieved evidence.')
        self.assertNotIn('line', generated['dialogue'].get('slots', {}),
                         'An independent target is not a quoted PrizePicks offer.')
        text = str(generated)
        self.assertIn('Josh Hart', text)
        self.assertIn('Jalen Brunson', text)
        self.assertEqual(before, self.snapshot())

    async def test_research_target_explanation_uses_header_event_not_today(self):
        self.add_player('Josh Hart', 'NYK', [17, 19, 23, 21, 18, 19, 20, 22], 'pra')
        self.master.upsert_game({'id': 'future', 'date': '2026-11-05',
            'home_team': 'NYK', 'away_team': 'BKN', 'status': 'scheduled',
            'start_time': '2026-11-06T00:30:00+00:00', 'source': 'synthetic calendar'})
        history = [{'role': 'assistant', 'content': 'My projections — NYK vs BKN · Nov 05, 7:30 PM Eastern\n'
                   '1. Josh Hart · PTS+REB+AST: 20\n'
                   'More if line ≤15.5; Less if line ≥24.5.'}]
        before = self.snapshot()
        await self.ask('Why this pick?', history, through_app=True)
        generated = self.generated[-1]
        self.assertTrue(generated['cards'])
        self.assertEqual(generated['dialogue']['date'], '2026-11-05')
        self.assertTrue(all(card['line_kind'] == 'research_target' and not card['offer_verified']
                            for card in generated['cards']))
        self.assertEqual(before, self.snapshot())

    async def test_research_target_with_insufficient_history_is_explained_without_model_invention(self):
        history = [{'role': 'assistant', 'content': 'My projections — NYK vs BKN · Nov 02\n'
                   '1. Josh Hart · PTS+REB+AST: 20\n'
                   'More if line ≤15.5; Less if line ≥24.5.'}]
        before = self.snapshot()
        result = await self.ask('Why this pick?', history, through_app=True)
        self.assertFalse(result['ai_used'])
        self.assertIn('recompute', result['answer'])
        self.assertIn('Josh Hart', result['answer'])
        self.generate.assert_not_awaited()
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
