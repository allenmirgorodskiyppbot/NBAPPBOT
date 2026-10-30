"""Population-safe source rendering and generated conversation, without canned replies."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import app
from grounded_reply import evidence_cards, render_reply, required_evidence
from master import MasterStore


def context(player='OG Anunoby',opponent='CHA'):
    return {'resolved_players':[player], 'resolved_teams':[opponent],
        'assignment_projections':[{'selected_player':player,'opponent_team':opponent,
        'primary':'Fixture Defender','alternate':'Fixture Alternate','assignment_asof':'2026-10-22','_row':227}],
        'current_player_statistics':[{'player':player,'ppg':17.6,'rpg':6.2,'apg':2.6,
            'games_played':5,'season':'2026-27','season_type':'Regular Season',
            'data_through_date':'2026-10-28','published_at':'2026-10-29T14:18:20Z','source':'https://example.com/player'}],
        'opponent_player_statistics':[{'player':player,'opponent':opponent,'season':'2026-27',
            'season_type':'Regular Season','n':0,'averages':{'pts':None}}],
        'current_tracking':[{'entity':opponent,'category':'Team defense','gp':5,'season':'2026-27',
            'fields':{'points_allowed':541,'defended_fgm':196,'defended_fga':422},
            'data_through_date':'2026-10-28','published_at':'2026-10-29T14:19:06Z','source':'https://example.com/defense'},
            {'entity':opponent,'category':'Team totals','gp':5,'season':'2026-27','fields':{'pts':505},
             'data_through_date':'2026-10-28','published_at':'2026-10-29T14:18:58Z','source':'https://example.com/offense'}]}


class EvidenceContractTests(unittest.TestCase):
    def test_verified_facts_keep_population_coverage_and_publication_for_arbitrary_entities(self):
        for player,opponent in [('OG Anunoby','CHA'),('Tyrese Maxey','CLE')]:
            cards=evidence_cards(context(player,opponent))
            average=next(c for c in cards if c['kind']=='player_all_opponent_averages')
            result=render_reply({'evidence_ids':[average['id']],
                'analysis':'The matchup alone does not justify a different scoring projection.','needs_more_data':False},cards)
            self.assertIn(player,result['answer'])
            for text in ('17.6 PPG','all opponents','Coverage through 2026-10-28','published 2026-10-29'):
                self.assertIn(text,result['answer'])
            self.assertNotIn('versus '+opponent,result['answer'])
            self.assertEqual(result['sources'][0]['population_kind'],'player_all_opponent_averages')

    def test_demonstrated_numeric_scope_misstatement_cannot_bypass_source_renderer(self):
        cards=evidence_cards(context())
        for prose in ("OG scored 17.6 PPG against Charlotte.",
                      'He averaged seventeen points against Charlotte.'):
            with self.subTest(prose=prose),self.assertRaises(ValueError):
                render_reply({'evidence_ids':['E1'],'analysis':prose,'needs_more_data':False},cards)

    def test_invalid_evidence_is_rejected(self):
        cards=evidence_cards(context())
        for ids in (['E999'],[1],['E1',None]):
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                render_reply({'evidence_ids':ids,'analysis':'The pairing is uncertain.','needs_more_data':False},cards,['E1'])

    def test_defense_and_offense_are_rendered_from_different_source_fields(self):
        cards=evidence_cards(context())
        defense=next(c for c in cards if c['kind']=='tracking_Team defense')
        offense=next(c for c in cards if c['kind']=='tracking_Team totals')
        self.assertIn('108.2 points allowed/game',defense['text'])
        self.assertIn('46.45% opponent FG',defense['text'])
        self.assertIn('101 points scored/game',offense['text'])
        self.assertNotIn('allowed',offense['text'])
        gap=next(c for c in cards if c['kind']=='player_opponent_sample')
        self.assertIn('no matching completed appearances in the stored sample',gap['text'])
        self.assertIn('coverage is incomplete',gap['text'])

    def test_retrieval_request_cannot_publish_an_unsupported_answer(self):
        cards=evidence_cards(context())
        self.assertIsNone(render_reply({'evidence_ids':[],'analysis':'','needs_more_data':True},cards))
        with self.assertRaises(ValueError):
            render_reply({'evidence_ids':[],'analysis':'He will score twenty points.','needs_more_data':True},cards)

    def test_social_reply_can_mention_topic_without_repeating_statistical_evidence(self):
        cards=evidence_cards(context())
        phrase='Glad that helped with your OG research.'
        result=render_reply({'evidence_ids':[],'analysis':phrase,'needs_more_data':False},cards)
        self.assertEqual(result['answer'],phrase)
        self.assertEqual(result['sources'],[])

    def test_requested_opponent_metric_uses_its_own_observed_denominator(self):
        data=context('Tyrese Maxey','CLE')
        data['opponent_player_statistics'][0].update(n=2,
            averages={'pts':20,'reb':7,'ast':4,'minutes':35,'tpm':3},
            observed_counts={'pts':2,'reb':1,'ast':2,'minutes':2,'tpm':1},
            first_date='2026-10-20',data_through_date='2026-10-28')
        cards=evidence_cards(data,'Explain Maxey rebounds and made threes against Cleveland')
        sample=next(c for c in cards if c['kind']=='player_opponent_sample')
        self.assertIn('7 RPG (1/2 observed)',sample['text'])
        self.assertIn('3 made threes/game (1/2 observed)',sample['text'])
        self.assertNotIn('PPG',sample['text'])
        self.assertIn('2 stored appearances',sample['text'])

    def test_all_opponent_metrics_disclose_missingness_and_preserve_unknown_counts(self):
        data=context()
        data['current_player_statistics'][0].update(points_observed=4,rebounds_observed=3,
            published_at=None,minutes_per_game=33)
        cards=evidence_cards(data,'Explain his points, rebounds and minutes')
        summary=next(c for c in cards if c['kind']=='player_all_opponent_averages')
        self.assertIn('17.6 PPG (4/5 observed)',summary['text'])
        self.assertIn('6.2 RPG (3/5 observed)',summary['text'])
        self.assertIn('33 minutes/game (observed count unavailable)',summary['text'])
        self.assertIn('Stored sample; coverage may be incomplete.', summary['text'])
        self.assertIn('all opponents', summary['text'])
        self.assertNotIn('APG',summary['text'])

    def test_unrelated_or_ambiguous_pairing_is_never_mandatory(self):
        data=context()
        cards=evidence_cards(data)
        self.assertEqual(required_evidence('Explain his Hornets matchup',cards),['E1','E3','E2'])
        data['resolved_players']=['OG Anunoby','Tyrese Maxey']
        data['resolved_teams']=[]
        cards=evidence_cards(data)
        self.assertEqual(required_evidence('Explain OG guarding Maxey',cards),[])
        data['resolved_players']=['OG Anunoby']
        data['resolved_teams']=['BOS']
        cards=evidence_cards(data)
        self.assertNotIn('E1', required_evidence('Explain his Celtics matchup',cards))
        self.assertNotIn('E2', required_evidence('Explain his Celtics matchup',cards))

    def test_percentage_fact_uses_fraction_as_shot_ratio(self):
        data=context()
        data['current_player_statistics'][0]['three_pct']=.4
        cards=evidence_cards(data,'Explain his three-point percentage')
        summary=next(c for c in cards if c['kind']=='player_all_opponent_averages')
        self.assertIn('40 3P% (shot ratio)',summary['text'])
        self.assertNotIn('PPG',summary['text'])


class GeneratedReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master=MasterStore(Path(self.directory.name)/'master.sqlite')

    def client(self,handler):
        original=httpx.AsyncClient
        return patch.object(app.httpx,'AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler),**kw))

    async def test_social_reply_is_model_generated_in_one_small_request(self):
        calls=[]
        phrase='Glad that helped. We can pick this up whenever you are ready.'
        def reply(request):
            payload=json.loads(request.content);calls.append(payload)
            return httpx.Response(200,json={'message':{'content':json.dumps({
                'evidence_ids':[],'analysis':phrase,'needs_more_data':False})},'done_reason':'stop'})
        with self.client(reply):
            result=await app.generated_evidence_reply(app.ChatRequest(message='thank you'),{},self.master,[])
        self.assertEqual(result['answer'],phrase)
        self.assertEqual(len(calls),1)
        self.assertNotIn('tools',calls[0])
        self.assertLessEqual(calls[0]['options']['num_ctx'],4096)

    async def test_bad_numeric_prose_is_not_delivered_as_a_verified_ai_answer(self):
        wrong='OG averaged 17.6 points against the Hornets.'
        def reply(request):
            return httpx.Response(200,json={'message':{'content':json.dumps({
                'evidence_ids':['E1','E3'],'analysis':wrong,'needs_more_data':False})},'done_reason':'stop'})
        with self.client(reply):
            result=await app.generated_evidence_reply(app.ChatRequest(message='Explain his stored matchup'),{},self.master,evidence_cards(context()))
        self.assertEqual(result['ai_failure_category'],'grounding_error')
        self.assertFalse(result['ai_used'])
        self.assertNotIn(wrong,result['answer'])
        self.assertIn('Tentative as of 2026-10-22',result['answer'])

    async def test_sufficient_evidence_is_explained_without_the_large_tool_catalog(self):
        cards=evidence_cards(context());calls=[]
        def reply(request):
            payload=json.loads(request.content);calls.append(payload)
            return httpx.Response(200,json={'message':{'content':json.dumps({
                'evidence_ids':['E1','E3'],
                'analysis':'The pairing is tentative. A confirmed assignment and workload would be needed to justify a scoring adjustment.',
                'needs_more_data':False})},'done_reason':'stop'})
        with self.client(reply):
            result=await app.generated_evidence_reply(app.ChatRequest(message='Explain his stored matchup'),{},self.master,cards)
        self.assertIn('all opponents',result['answer'])
        self.assertIn('confirmed assignment',result['answer'])
        self.assertEqual(len(calls),1)
        self.assertNotIn('tools',calls[0])
        self.assertNotIn('workbook_catalog',json.dumps(calls[0]))
        self.assertNotIn('ai_failure_category',result)


if __name__=='__main__':unittest.main()
