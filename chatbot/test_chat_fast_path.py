"""Computed routine replies avoid inference while deeper discussion stays bounded."""
import asyncio
import threading
import unittest
from unittest.mock import patch

import chat_flow
import app
import test_chat_flow as fixtures
from conversation import resolve_conversation
from feeds import canonical_game
from test_conversation import NAMES,NOW


class FastConversationTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.ChatFlowTests.setUp
    add_player=fixtures.ChatFlowTests.add_player
    explain=fixtures.ChatFlowTests.explain
    ask=fixtures.ChatFlowTests.ask
    snapshot=fixtures.ChatFlowTests.snapshot

    async def test_full_correction_reason_risk_confidence_and_definition_sequence_never_uses_model(self):
        history=[]
        for text in ['Josh Hart line22.5 choose up or down','22.5 I mean points rebounds assists',
                     'Up or down choose','Why?','What could go wrong?','How confident are you?',
                     'What does PRA mean?']:
            result=await self.ask(text,history)
            self.assertFalse(result['ai_used'])
            if 'discussion' in result:
                self.assertEqual((result['discussion']['player'],result['discussion']['stat'],result['discussion']['line']),
                                 ('Josh Hart','pra',22.5))
            history.extend([{'role':'user','content':text},{'role':'assistant','content':result['answer']}])
        self.generate.assert_not_awaited()
        self.assertIn('points plus rebounds plus assists',result['answer'])

    async def test_identical_prop_and_correction_have_deterministic_checked_decisions(self):
        text='Julius Randal more1.5threes a good line?'
        a=await self.ask(text);b=await self.ask(text)
        self.assertEqual(a['answer'],b['answer']);self.assertEqual(a['discussion'],b['discussion'])
        self.assertTrue(a['sources']);self.assertEqual(a['discussion']['line'],1.5)
        history=[{'role':'user','content':text},{'role':'assistant','content':a['answer']}]
        changed=await self.ask('Actually2.5 not1.5',history)
        self.assertEqual(changed['discussion']['line'],2.5)
        self.generate.assert_not_awaited()

    async def test_unverified_current_data_remains_explicit_pass_without_inference(self):
        self.master.set_checkpoint('daily_nba:2026-11-01',{})
        result=await self.ask('Julius Randle more1.5threes good?')
        self.assertEqual(result['discussion']['decision'],'pass')
        self.assertIn('Pass for now',result['answer'])
        self.assertIn('unverified',result['answer'])
        self.generate.assert_not_awaited()

    async def test_clear_preferences_and_thanks_do_not_retrieve_stats_or_use_model(self):
        with patch.object(chat_flow,'_preference_evidence',side_effect=AssertionError('unneeded lookup')):
            preference=await self.ask('I prefer field goal attempts')
            both=await self.ask('Picks must be from both teams')
            thanks=await self.ask('Thanks!')
        self.assertEqual(preference['constraints']['preferred_stat'],'fga')
        self.assertTrue(both['constraints']['both_teams'])
        self.assertEqual(thanks['answer'],'You’re welcome.')
        self.generate.assert_not_awaited()

    async def test_routine_shortlist_why_keeps_every_pick_and_rechecks_readiness(self):
        history=[{'role':'assistant','content':'My shortlist\n1. Josh Hart — Less 22.5 PRA\n2. Julius Randle — More 1.5 threes'}]
        result=await self.ask('Why those picks?',history)
        self.assertIn('Josh Hart',result['answer']);self.assertIn('Julius Randle',result['answer'])
        self.assertFalse(result['ai_used']);self.generate.assert_not_awaited()
        with patch.object(chat_flow,'_changed_readiness',return_value={'answer':'Hold: current readiness changed','ai_used':False,'sources':[]}):
            held=await self.ask('Why those picks?',history)
        self.assertTrue(held['answer'].startswith('Hold'))

    async def test_deeper_discussion_keeps_generator_but_has_small_history_and_total_deadline(self):
        history=[]
        for i in range(8):
            history.extend([{'role':'user','content':'Earlier explanation '+('x'*600)},
                            {'role':'assistant','content':'Earlier response '+('y'*600)}])
        result=await self.ask('Explain Julius Randle more1.5threes in detail',history)
        self.generate.assert_awaited_once()
        request=self.generated[-1]['request']
        self.assertLessEqual(len(request.history),4)
        self.assertLessEqual(sum(len(item.content) for item in request.history),900)
        self.assertEqual(result['discussion']['line'],1.5)
        async def pending(*args,**kwargs):
            await asyncio.Event().wait()
        self.generate.side_effect=pending
        with patch.object(chat_flow,'MODEL_DISCUSSION_SECONDS',.01):
            fallback=await self.ask('Explain Julius Randle more1.5threes in detail')
        self.assertFalse(fallback['ai_used']);self.assertEqual(fallback['discussion']['line'],1.5)

    async def test_fast_computation_cancellation_is_propagated(self):
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        original=chat_flow.build_pick_discussion
        def blocking(*args,**kwargs):
            entered.set();release.wait(3)
            try:
                return original(*args,**kwargs)
            finally:
                finished.set()
        with patch.object(chat_flow,'build_pick_discussion',side_effect=blocking):
            task=asyncio.create_task(self.ask('Julius Randle more1.5threes'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait,3))
        self.generate.assert_not_awaited()

    async def test_public_fast_preflight_never_calls_generation_and_declines_open_ended_research(self):
        with patch.object(chat_flow,'_generate_reply',side_effect=AssertionError('preflight called model')):
            direct=await chat_flow.try_fast_answer(None,self.master,app.ChatRequest(message='Randle more1.5threes good?'))
            deep=await chat_flow.try_fast_answer(None,self.master,app.ChatRequest(message='Explain Randle more1.5threes in detail'))
            research=await chat_flow.try_fast_answer(None,self.master,app.ChatRequest(message='Compare the Knicks and Nets defenses'))
        self.assertEqual(direct['discussion']['line'],1.5)
        self.assertFalse(direct['ai_used'])
        self.assertIsNone(deep);self.assertIsNone(research)

    async def test_next_game_preflight_overrides_old_prop_and_reuses_team_for_expectations(self):
        self.master.upsert_game({'id':canonical_game('2026-11-05','NYK','BKN'),'date':'2026-11-05',
            'home_team':'NYK','away_team':'BKN','status':'scheduled',
            'start_time':'2026-11-06T00:30:00+00:00','source':'saved fixture calendar'})
        history=[{'role':'user','content':'Julius Randle more1.5threes'}]
        preview=await chat_flow.try_fast_answer(None,self.master,app.ChatRequest(message='whats uo next',history=history))
        self.assertEqual(preview['upcoming_game_team'],'NYK')
        self.assertIn('for NYK',preview['answer'])
        self.assertIn('current timing is unverified',preview['answer'])
        self.assertNotIn('1.5',preview['answer'])
        history.extend([{'role':'user','content':'whats uo next'},{'role':'assistant','content':preview['answer']}])
        followup=await chat_flow.try_fast_answer(None,self.master,app.ChatRequest(message='What should we expect?',history=history))
        self.assertEqual(followup['upcoming_game_team'],'NYK')
        self.assertEqual(followup['next_game_focus']['id'],preview['next_game_focus']['id'])
        self.generate.assert_not_awaited()


class WorkloadMarketBoundaryTests(unittest.TestCase):
    def test_second_half_blowout_discussion_is_not_a_new_partial_game_offer(self):
        with patch('conversation._players',return_value=NAMES):
            first=resolve_conversation(None,None,'Brunson more25.5points',now=NOW)
            discussion=resolve_conversation(None,None,'What if he sits most of the second half in a blowout?',state=first['state'],now=NOW)
            offer=resolve_conversation(None,None,'Actually second half points line12.5',state=first['state'],now=NOW)
        self.assertTrue(discussion['complete_prop']);self.assertEqual(discussion['slots']['line'],25.5)
        self.assertEqual(discussion['slots']['stat'],'pts');self.assertIsNone(discussion['clarification'])
        self.assertFalse(offer['complete_prop']);self.assertIn('special or partial-game',offer['clarification'])

    def test_explicit_next_game_clears_old_prop_and_keeps_only_safe_team_focus(self):
        with patch('conversation._players',return_value=NAMES):
            old=resolve_conversation(None,None,'Brunson more25.5points',now=NOW)
            next_game=resolve_conversation(None,None,'whats uo next',state=old['state'],now=NOW)
            followup=resolve_conversation(None,None,'what to expect',state=next_game['state'],now=NOW)
        self.assertEqual(next_game['intent'],'upcoming_game')
        self.assertFalse(next_game['complete_prop'])
        self.assertNotIn('player',next_game['slots']);self.assertNotIn('line',next_game['slots'])
        self.assertEqual(followup['intent'],'upcoming_game')
        self.assertEqual(followup['state']['upcoming_game_team'],'NYK')


if __name__=='__main__':unittest.main()
