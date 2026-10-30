"""Conversational entity resolution, scoped retrieval and source boundaries."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl
import httpx

import app
from basketball_research import (resolve_question, answer_assignment_question, canonical_history,
                                  research_context, ScopedNewsStore)
from master import MasterStore
from release_checks import verify_release, model_startup_completed
from workbook import WorkbookStore


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Matchup_Assignments'
        for _ in range(6):
            sheet.append([None])
        sheet.append(['assignment_id','selected_player','opponent_team','primary','alternate',
                      'assignment_asof','assignment_status'])
        sheet.append(['A1','OG Anunoby','CHA',"Royce O’Neale",'Grant Williams',
                      '2026-10-22T12:00:00','Tentative starter matchup'])
        sheet.append(['A2','OG Anunoby','BOS','Jayson Tatum','Jaylen Brown',
                      '2026-10-22T12:00:00','Tentative starter matchup'])
        sheet.append(['A3','Jalen Brunson','CHA','LaMelo Ball','Tre Mann',
                      '2026-10-22T12:00:00','Tentative starter matchup'])
        profile = book.create_sheet('OG Anunoby')
        for _ in range(6):
            profile.append([None])
        profile.append(['season', 'games', 'ppg'])
        profile.append(['2026-27', 4, 16.25])
        self.book_path = root / 'fixture.xlsx'
        book.save(self.book_path)
        book.close()
        self.workbook = WorkbookStore(str(self.book_path), str(root / 'workbook.sqlite'))
        self.master = MasterStore(root / 'master.sqlite')
        for name, team in [('OG Anunoby','NYK'),('Jalen Brunson','NYK'),('Jalen Smith','CHI'),
                           ('Karl-Anthony Towns','NYK'),('Miles McBride','NYK')]:
            self.master.upsert_player_game({'game_id':'g1', 'player':name, 'team':team,
                'date':'2026-10-27','season':'2026-27','season_type':'Regular Season',
                'appearance_state':'Played','pts':20})

    def resolve(self, question, history=None):
        return resolve_question(self.workbook, self.master, question, history)

    def answer(self, question, history=None):
        return answer_assignment_question(self.workbook, self.master, question, history)

    def test_spoken_name_nickname_and_team_variations_retrieve_same_pairing(self):
        for question in ["Who's OG and Abby’s matchup on the Hornets?", 'Who guards Anunoby against Charlotte?',
                         "Who is OG Anunoby's matchup vs CHA?", 'Who is Anunobyy matched up against on the Hornets?']:
            with self.subTest(question=question):
                result = self.answer(question)
                self.assertIsNotNone(result)
                self.assertIn('Royce O’Neale', result['answer'])
                self.assertIn('Grant Williams', result['answer'])
                self.assertIn('2026-10-22', result['answer'])
                self.assertIn('not a confirmed', result['answer'])
                self.assertIn('does not specify the direction', result['answer'])
                self.assertEqual(result['sources'][0]['excel_rows'], [8])
                self.assertFalse(result['ai_used'])

    def test_general_player_team_catalog_and_safe_nicknames(self):
        self.assertEqual(self.resolve("Who is Brunson's matchup with Charlotte?")['players'], ['Jalen Brunson'])
        self.assertEqual(self.resolve('What is KAT averaging?')['players'], ['Karl-Anthony Towns'])
        self.assertEqual(self.resolve('What about Deuce?')['players'], ['Miles McBride'])
        self.assertEqual(self.resolve('Who does OG face on Boston?')['teams'], ['BOS'])
        self.assertEqual(self.resolve('Tell me about Jalen')['players'], [])  # Ambiguous first name.
        self.assertEqual(self.resolve('Is his field goal percentage in PHI higher?')['teams'], ['PHI'])

    def test_team_followup_retains_player_and_new_team(self):
        history = [{'role':'user','content':"Who is OG's matchup against Charlotte?"}]
        answer = self.answer('And against Boston?', history)
        self.assertIn('Jayson Tatum', answer['answer'])
        self.assertNotIn('Royce', answer['answer'])
        self.assertEqual(self.resolve('Why is that?', history)['teams'], ['CHA'])
        self.assertEqual(self.resolve('Why is that?', history)['players'], ['OG Anunoby'])
        changed = self.resolve('What about Brunson?', history)
        self.assertEqual(changed['players'], ['Jalen Brunson'])
        self.assertEqual(changed['teams'], ['CHA'])

    def test_new_general_news_topic_does_not_inherit_old_player(self):
        history = [{'role':'user','content':'Tell me about Jalen Brunson'}]
        resolved = self.resolve('Show latest Knicks injury news', history)
        self.assertEqual(resolved['players'], [])
        self.assertEqual(resolved['teams'], ['NYK'])
        self.assertEqual(self.resolve('Hello', history)['players'], [])

    def test_missing_opponent_is_a_normal_question_and_missing_pair_is_disclosed(self):
        self.assertIn('Which opponent', self.answer("Who is OG's matchup?")['answer'])
        self.assertIn('don’t have a saved', self.answer("Who is OG's matchup against Phoenix?")['answer'])
        self.assertIsNone(self.answer('What FG% does Maxey shoot when Brunson guards him?'))
        self.assertIsNone(self.answer('Why does OG match up with the Hornets forward?'))
        self.assertIsNone(self.answer('Who is OG’s matchup against Charlotte in 2024-25?'))

    def test_canonical_history_preserves_numbers_filters_and_assistant_words(self):
        history = [{'role':'user','content':'OG last five games, points in 2026-27'},
                   {'role':'assistant','content':'Source: OG; do not alter this quotation.'}]
        result = canonical_history(self.workbook, self.master, history)
        self.assertEqual(result[0]['content'], 'OG Anunoby last five games, points in 2026-27')
        self.assertEqual(result[1], history[1])

    def add_news(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for key, title in [('og','OG Anunoby: Questionable with injury'),
                           ('other','Jalen Smith: Ruled out with injury'),
                           ('promo','OG Anunoby Promo Code: Get $50')]:
            self.master.record_news({'id':key,'title':title,'body':title,'source':'fixture',
                'published_at':now,'url':'https://example.com/' + key})
        self.master.feed_success('rss_1')

    def test_player_news_scope_excludes_unrelated_player_and_grounding_is_bounded(self):
        self.add_news()
        resolved = self.resolve('Show latest injury news about OG')
        scoped = ScopedNewsStore(self.master, resolved)
        self.assertNotIn('Jalen Smith', str(scoped.recent_news()))
        context = research_context(self.workbook, self.master, resolved)
        self.assertEqual(context['resolved_players'], ['OG Anunoby'])
        self.assertNotIn('Promo Code', str(context))
        self.assertIn('player_sheets', context)
        self.assertLessEqual(len(json.dumps(context)), 5500)

    def test_direct_matchup_works_during_model_download_without_a_model_call(self):
        state = app.app.state
        state.store, state.master = self.workbook, self.master
        state.phase, state.detail = 'model', 'Loading'
        state.chat_lock = asyncio.Lock()
        with patch.object(app.httpx, 'AsyncClient', side_effect=AssertionError('No model request expected')):
            answer = asyncio.run(app.telegram_answer("Who's OG and Abby's matchup against the Hornets?"))
        self.assertIn('Royce', answer)

    def test_generic_explanation_uses_scoped_source_facts_and_generated_interpretation(self):
        seen = []
        def reply(request):
            payload = json.loads(request.content)
            seen.append(payload)
            context = json.loads(payload['messages'][-1]['content'])
            card = next(c for c in context['evidence'] if c['kind']=='tentative_assignment')
            self.assertIn('Royce O', card['text'])
            self.assertIn('2026-10-22', card['text'])
            self.assertIn('direction unconfirmed', card['text'])
            self.assertNotIn('tools',payload)
            return httpx.Response(200,json={'message':{'content':json.dumps({
                'evidence_ids':[card['id']], 'analysis':'The assignment alone cannot justify changing his scoring projection.',
                'needs_more_data':False})}})
        real = httpx.AsyncClient
        with patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(reply), **kw)):
            answer = asyncio.run(app.answer_question(self.workbook,
                app.ChatRequest(message='Explain OG’s projected matchup against the Hornets'), {}, self.master))
        self.assertIn('tentative', answer['answer'].lower())
        self.assertIn('cannot justify', answer['answer'])
        self.assertNotIn('ai_failure_category',answer)
        self.assertEqual(len(seen), 1)

    def test_player_specific_news_request_passes_only_matching_evidence_to_model(self):
        self.add_news()
        def reply(request):
            payload = json.loads(request.content)
            evidence = payload['messages'][-1]['content']
            self.assertIn('OG Anunoby', evidence)
            self.assertNotIn('Jalen Smith', evidence)
            self.assertNotIn('Promo Code', evidence)
            return httpx.Response(200,json={'message':{'content':'{"reports":[1]}'}})
        real = httpx.AsyncClient
        with patch.object(app.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(reply), **kw)):
            result = asyncio.run(app.answer_question(self.workbook,
                app.ChatRequest(message='Summarize latest injury news about OG'), {}, self.master))
        self.assertIn('OG Anunoby', result['answer'])
        self.assertNotIn('Jalen Smith', result['answer'])

    def test_automatic_release_report_keeps_live_data_untouched_and_schedule_blocker_visible(self):
        before = self.master.status()['counts']
        self.master.set_checkpoint('knicks_digest_status', {'state':'schedule_unavailable'})
        report = verify_release(self.master,self.workbook,Path(self.directory.name),model_phase='model',
                                telegram={'enabled':True,'connected':True})
        self.assertEqual(report['failed'],0)
        self.assertGreater(report['warnings'],0)
        self.assertEqual(self.master.status()['counts'],before)
        checks = {check['name']:check['status'] for check in report['checks']}
        self.assertEqual(checks['Current Knicks schedule available for digest timing'],'WARN')
        self.assertEqual(checks['Sourced conversational workbook matchup lookup'],'PASS')
        self.master.set_checkpoint('release_checks', report)
        model_startup_completed(self.master)
        self.assertEqual(self.master.get_checkpoint('release_checks')['model_phase'],'ready')
        self.assertEqual(self.master.status()['counts'],before)


if __name__ == '__main__':
    unittest.main()
