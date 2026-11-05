"""Human dialogue routes by meaning, then reads actual isolated saved evidence."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app
import test_chat_flow as fixtures
from conversation import resolve_conversation
from feeds import canonical_game
from grounded_reply import render_natural_reply
from season_calendar import import_calendar


class SemanticChatFlowTests(unittest.IsolatedAsyncioTestCase):
    add_player = fixtures.ChatFlowTests.add_player
    snapshot = fixtures.ChatFlowTests.snapshot
    explain = fixtures.ChatFlowTests.explain

    def setUp(self):
        fixtures.ChatFlowTests.setUp(self)
        self.events=[]
        self.generation_records=[]
        self.route=AsyncMock(return_value={'route':'next_game','team':'NYK'})
        self.natural=AsyncMock(side_effect=self.checked_natural)
        self.lookup=AsyncMock(side_effect=self.source_check)
        for name, value in [('semantic_dialogue.infer_dialogue_route',self.route),
                            ('app.generated_evidence_reply',self.natural),
                            ('app.refresh_pregame_schedule',self.lookup)]:
            patcher=patch(name,value);patcher.start();self.addCleanup(patcher.stop)
        import_calendar(self.master,{'season':'2026-27','covered_teams':['NYK','CLE'],
            'complete_for_published_games':False,'retrieved_at':fixtures.NOW.isoformat(),
            'source':'https://example.test/dated-calendar','games':[
                {'date':'2026-11-07','home_team':'CLE','away_team':'NYK',
                 'status':'scheduled','start_time':'2026-11-08T01:00:00Z'}]})
        for day,home,away,hs,aws in [('2026-11-01','NYK','BOS',121,110),
                                   ('2026-10-31','CLE','MIA',108,100)]:
            self.master.upsert_game({'id':canonical_game(day,home,away),'date':day,
                'home_team':home,'away_team':away,'home_score':hs,'away_score':aws,
                'status':'final','source':'https://example.test/validated-final'})

    async def source_check(self, master):
        self.assertIs(master,self.master)
        self.events.append('source_check')

    async def checked_natural(self, request, progress, master, cards, **kwargs):
        self.events.append('generation')
        self.generation_records.append({'message':request.message,'cards':cards,**kwargs})
        if kwargs.get('dialogue',{}).get('intent')=='upcoming_game':
            team_card=next((card for card in cards if card.get('entity')=='NYK'),None)
            self.assertIsNotNone(team_card,'Reader must supply actual team evidence before synthesis')
            average=team_card['approved_facts'][0]['values'][0]
            text=(f'NYK averaged {average:g} points per game in its saved sample. '
                  'If rotation minutes change, counting stats can change too.')
            result=render_natural_reply({'evidence_ids':[team_card['id']],
                'reply':text,'needs_more_data':False},cards)
            result['ai_used']=True
            return result
        return {'answer':kwargs.get('fallback') or 'The saved evidence needs a clearer question.',
                'sources':[],'ai_used':True}

    async def ask(self, text, history=None):
        return await app.answer_chat_message(None,app.ChatRequest(message=text,history=history or []),
                                             {},self.master)

    def prop_history(self, *, multiple=False):
        proposal='Julius Randle — More 1.5 threes.'
        if multiple:
            proposal='1. Julius Randle — More 1.5 threes.\n2. Josh Hart — Less 22.5 PRA.'
        return [{'role':'user','content':'Suggest a research candidate.'},
                {'role':'assistant','content':proposal}]

    def assert_installed_help_evidence(self,card):
        from telegram_bot import HELP
        lines=card['text'].splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertIn(line,HELP.splitlines(),f'Help evidence must quote an installed command verbatim: {line}')
        commands={line.split()[0] for line in lines}
        self.assertTrue({'/freshness','/picks'} <= commands)
        self.assertEqual(card['source'],'Installed bot command help')
        self.assertEqual(card['approved_facts'][0]['text'],card['text'])

    async def test_novel_upcoming_wording_selects_real_reader_before_grounded_generation(self):
        before=self.snapshot()
        result=await self.ask('whats upcoming')
        self.route.assert_awaited_once()
        self.assertEqual(self.route.call_args.args[0].message,'whats upcoming')
        self.assertEqual(result['next_game_focus']['date'],'2026-11-07')
        self.assertEqual(result['next_game_focus']['opponent'],'CLE')
        self.assertIn('NYK at CLE',result['answer'])
        self.assertIn('8:00 PM Eastern',result['answer'])
        self.assertIn('current timing is unverified',result['answer'])
        self.assertIn('NYK averaged 121 points',result['answer'])
        self.assertTrue(result['ai_used'])
        self.assertEqual(result['response_mode'],'generated')
        self.assertEqual(self.events,['source_check','generation'])
        self.assertEqual(self.snapshot(),before,'Conversation must not create offers, entries or coverage')

    async def test_new_game_topic_discards_old_prop_focus_without_rewriting_the_question(self):
        history=self.prop_history()
        result=await self.ask('anything on the horizon?',history)
        self.assertEqual(result['next_game_focus']['team'],'NYK')
        self.assertNotIn('1.5',result['answer'])
        self.assertNotIn('discussion',result)
        self.assertEqual(self.generation_records[0]['message'],'anything on the horizon?')

    async def test_unfamiliar_preview_followup_retains_the_selected_game(self):
        first=await self.ask('whats upcoming')
        history=[{'role':'user','content':'whats upcoming'},
                 {'role':'assistant','content':first['answer']}]
        result=await self.ask('anything there you would keep an eye on?',history)
        self.assertEqual(result['next_game_focus'],first['next_game_focus'])
        self.assertEqual(self.route.call_args.kwargs['resolved_context']['focus_kind'],'game')
        self.assertEqual(self.route.call_args.kwargs['resolved_context']['team'],'NYK')
        self.assertEqual(self.natural.await_count,2)

    async def test_unfamiliar_reason_after_single_proposal_keeps_its_exact_line(self):
        self.route.return_value={'route':'discuss_prop','team':None}
        result=await self.ask('Help me see the thinking here.',self.prop_history())
        self.route.assert_awaited_once()
        self.assertEqual(self.route.call_args.kwargs['resolved_context']['focus_kind'],'prop')
        self.natural.assert_awaited_once()
        self.assertIn('Julius Randle',result['answer'])
        self.assertIn('1.5',result['answer'])
        self.assertNotIn('25.5',result['answer'])

    async def test_ambiguous_multi_pick_question_clarifies_without_choosing_a_leg(self):
        self.route.return_value={'route':'discuss_prop','team':None}
        result=await self.ask('I am curious what sold you on it.',self.prop_history(multiple=True))
        self.assertIn('Which pick',result['answer'])
        self.assertNotIn('discussion',result)
        self.natural.assert_not_awaited()

    async def test_colloquial_market_switch_clears_the_previous_threshold_before_inference(self):
        history=[{'role':'user','content':'Julius Randle more1.5threes'},
                 {'role':'assistant','content':'Julius Randle — More 1.5 threes.'}]
        for question,stat in [('What about his boards instead?','reb'),('Could we use dimes instead?','ast')]:
            with self.subTest(question=question):
                self.route.reset_mock();self.natural.reset_mock()
                turn=resolve_conversation(None,self.master,question,history)
                self.assertEqual(turn['slots']['stat'],stat)
                self.assertNotIn('line',turn['slots'])
                result=await self.ask(question,history)
                self.assertNotIn('1.5',result['answer'])
                self.assertFalse(result.get('discussion',{}).get('line'))
                self.natural.assert_not_awaited()
                self.route.assert_not_awaited()

    async def test_router_failure_asks_for_context_instead_of_inventing_no_events(self):
        self.route.return_value=None
        result=await self.ask('anything on the horizon?')
        self.assertEqual(result['response_mode'],'clarification')
        self.assertIn('next Knicks game',result['answer'])
        self.assertNotIn('no upcoming',result['answer'].lower())
        self.assertFalse(result['ai_used'])
        self.lookup.assert_not_awaited();self.natural.assert_not_awaited()

    async def test_software_workflow_meaning_does_not_reuse_an_old_game_preview(self):
        first=await self.ask('whats upcoming')
        self.route.reset_mock();self.natural.reset_mock();self.lookup.reset_mock()
        self.route.return_value={'route':'chat','team':None}
        history=[{'role':'user','content':'whats upcoming'},
                 {'role':'assistant','content':first['answer']}]
        result=await self.ask('The refresh finished; where do I go from here?',history)
        self.assertNotIn('next_game_focus',result)
        self.lookup.assert_not_awaited()
        self.natural.assert_awaited_once()
        cards=self.generation_records[-1]['cards']
        self.assertEqual([card['id'] for card in cards],['B1'])
        self.assertEqual(cards[0]['kind'],'bot_help')
        self.assert_installed_help_evidence(cards[0])
        self.assertEqual(self.generation_records[-1]['message'],
                         'The refresh finished; where do I go from here?')

    async def test_wrong_next_game_plan_cannot_turn_excel_workflow_into_a_matchup(self):
        self.route.return_value={'route':'next_game','team':'NYK'}
        before=self.snapshot()
        result=await self.ask('The Excel refresh finished; what comes after that?',self.prop_history())
        self.route.assert_awaited_once()
        self.assertNotIn('next_game_focus',result)
        self.assertNotIn('discussion',result)
        self.assertNotIn('1.5',result['answer'])
        self.lookup.assert_not_awaited()
        self.natural.assert_awaited_once()
        cards=self.generation_records[-1]['cards']
        self.assertEqual([card['id'] for card in cards],['B1'])
        self.assert_installed_help_evidence(cards[0])
        self.assertEqual(self.snapshot(),before)

    async def test_wrong_model_team_cannot_silently_switch_a_focused_preview(self):
        first=await self.ask('whats upcoming')
        history=[{'role':'user','content':'whats upcoming'},
                 {'role':'assistant','content':first['answer']}]
        self.route.return_value={'route':'next_game','team':'CLE'}
        second=await self.ask('anything there you would keep an eye on?',history)
        self.assertEqual(self.route.call_args.kwargs['resolved_context']['team'],'NYK')
        self.assertEqual(second['next_game_focus']['team'],'NYK')
        self.assertEqual(second['next_game_focus'],first['next_game_focus'])
        self.assertTrue(second['ai_used'])
        self.assertEqual(second['response_mode'],'generated')
        explicit=await self.ask('What is coming up for Cleveland?',history)
        self.assertEqual(explicit['next_game_focus']['team'],'CLE')
        self.assertTrue(explicit['ai_used'])
        self.assertEqual(explicit['response_mode'],'generated')

    async def test_wrong_next_game_plan_cannot_discard_an_exact_existing_prop(self):
        self.route.return_value={'route':'next_game','team':None}
        history=[{'role':'user','content':'Jalen Brunson more25.5points'},
                 {'role':'assistant','content':'Jalen Brunson — More 25.5 points.'}]
        before=self.snapshot()
        result=await self.ask('Help me see the thinking here.',history)
        self.route.assert_awaited_once()
        self.assertEqual(self.route.call_args.kwargs['resolved_context']['focus_kind'],'prop')
        self.assertNotIn('next_game_focus',result)
        self.assertIn('Jalen Brunson',result['answer'])
        self.assertIn('25.5',result['answer'])
        self.assertTrue(result['ai_used'])
        self.natural.assert_awaited_once()
        cards=self.generation_records[-1]['cards']
        self.assertTrue(cards)
        self.assertTrue(all(not card.get('kind','').startswith('upcoming_') for card in cards))
        line_facts=[fact for card in cards for fact in card.get('approved_facts',[])
                    if fact.get('fact_type')=='line']
        self.assertTrue(line_facts)
        self.assertEqual({(fact['entity'],fact['metric'],tuple(fact['values'])) for fact in line_facts},
                         {('Jalen Brunson','points',(25.5,))})
        assessments=self.generation_records[-1]['dialogue']['assessments']
        self.assertEqual({(item['player'],item['stat']) for item in assessments},{('Jalen Brunson','pts')})
        self.assertEqual(self.snapshot(),before)

    async def test_a_complete_line_question_never_creates_a_board_or_entry(self):
        before=self.snapshot()
        result=await self.ask('Julius Randle more1.5threes good?')
        self.assertEqual(result['discussion']['player'],'Julius Randle')
        self.assertEqual(result['discussion']['stat'],'tpm')
        self.assertEqual(result['discussion']['line'],1.5)
        self.natural.assert_awaited_once()
        self.assertEqual(self.snapshot(),before)

    async def test_natural_preview_rejects_an_invented_scoring_fact(self):
        self.natural.side_effect=None
        self.natural.return_value={'answer':'The Knicks averaged 999 points per game.',
                                  'sources':[],'ai_used':True}
        result=await self.ask('whats upcoming')
        self.assertNotIn('999',result['answer'])
        self.assertIn('current timing is unverified',result['answer'])
        self.assertFalse(result['ai_used'])

    async def test_not_ready_and_busy_models_keep_checked_fallbacks_without_inference(self):
        state=SimpleNamespace(store=None,master=self.master,phase='workbook',detail='Loading',
                              chat_lock=asyncio.Lock(),ai_started_at=None)
        with patch.object(app.app,'state',state):
            first=await app.telegram_answer('next Knicks game')
            self.assertIn('Next saved matchup',first)
            state.phase='ready';await state.chat_lock.acquire()
            try:
                second=await app.telegram_answer('next Knicks game')
            finally:
                state.chat_lock.release()
        self.assertEqual(first,second)
        self.route.assert_not_awaited();self.natural.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
