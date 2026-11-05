"""Next-game intent and schedule evidence stay separate from current verification."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from feeds import canonical_game
from master import MasterStore
from season_calendar import import_calendar
from structured_availability import (normalize_official_pdf,build_availability_document,
                                     import_availability_document)
from upcoming_game import wants_next_game_request,build_preview

NOW=dt.datetime(2026,11,5,16,tzinfo=dt.timezone.utc)
SOURCE='https://www.nba.com/team/1610612752/schedule'


class UpcomingGameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MasterStore(Path(self.tmp.name)/'master.sqlite')
        self.calendar([('2026-11-07','CLE','NYK','2026-11-08T01:00:00Z','scheduled'),
                       ('2026-11-10','NYK','MIL','2026-11-11T00:30:00Z','scheduled'),
                       ('2026-11-06','BOS','LAL','2026-11-07T00:00:00Z','scheduled')])

    def calendar(self,games):
        document={'season':'2026-27','covered_teams':['NYK','BOS','LAL'],
            'retrieved_at':'2026-10-30T02:50:00Z','source':SOURCE,
            'complete_for_published_games':False,'games':[{'date':day,'home_team':home,'away_team':away,
                'start_time':tip,'status':status,'source_asof':'2026-10-29'} for day,home,away,tip,status in games]}
        import_calendar(self.store,document)

    def preview(self,text="what's up next",**kwargs):
        return build_preview(self.store,text,now=NOW,**kwargs)

    def snapshot(self):
        with self.store.connect() as db:return '\n'.join(db.iterdump())

    def final(self,day,home,away,hs,aws,**changes):
        row={'id':canonical_game(day,home,away),'date':day,'home_team':home,'away_team':away,
             'home_score':hs,'away_score':aws,'status':'final','source':'https://example.test/validated-game',
             **changes}
        self.store.upsert_game(row)

    def test_default_knicks_mobile_typo_and_saved_calendar_label(self):
        for text in ["whats uo next","what's up next","who do we play","who we play next",
                     'next Knicks game','when do the Knicks play next?']:
            with self.subTest(text=text):
                self.assertTrue(wants_next_game_request(text))
                result=self.preview(text)
                self.assertEqual(result['upcoming_game_team'],'NYK')
                self.assertEqual(result['next_game_focus']['opponent'],'CLE')
                self.assertIn('NYK at CLE',result['answer'])
                self.assertIn('Saturday, Nov 7, 2026, 8:00 PM Eastern (away)',result['answer'])
                self.assertIn('saved calendar as of 2026-10-29',result['answer'])
                self.assertFalse(result['next_game_focus']['timing_verified'])
                self.assertFalse(result['ai_used'])

    def test_other_team_and_followup_keep_team_context_without_old_prop(self):
        first=self.preview('What is the next Lakers game?')
        self.assertEqual(first['upcoming_game_team'],'LAL')
        history=[{'role':'user','content':'Brunson more 25.5 points'},
                 {'role':'assistant','content':first['answer']}]
        self.assertTrue(wants_next_game_request('what to expect?',history))
        next_turn=self.preview('what to expect?',history=history)
        self.assertEqual(next_turn['upcoming_game_team'],'LAL')
        self.assertNotIn('25.5',next_turn['answer'])
        explicit=self.preview('What is the next Knicks game?',history=history,team='LAL')
        self.assertEqual(explicit['upcoming_game_team'],'NYK')
        direct=self.preview('what to expect?',team='LAL')
        self.assertEqual(direct['upcoming_game_team'],'LAL')

    def test_next_game_qualifier_does_not_steal_an_explicit_offered_prop(self):
        for text in ['Randle more1.5threes next game','Brunson more18points next game']:
            with self.subTest(text=text):
                self.assertFalse(wants_next_game_request(text))
                self.assertIsNone(self.preview(text))
        self.assertIsNone(self.preview('what to expect from Randle more1.5threes next game?',team='NYK'))
        history=[{'role':'user','content':'Randle more1.5threes'}]
        self.assertTrue(wants_next_game_request('what about the next game?',history))
        self.assertEqual(self.preview('what about the next game?',history=history)['upcoming_game_team'],'NYK')

    def test_nonbasketball_next_step_is_not_routed_and_old_preview_does_not_override_workflow(self):
        for text in ['what next after the download?','next deployment step','refresh is done, what next?',
                     'what does PRA mean?','show his last five games']:
            self.assertFalse(wants_next_game_request(text))
            self.assertIsNone(self.preview(text))
        history=[{'role':'assistant','content':self.preview()['answer']},
                 {'role':'user','content':'I downloaded the Excel file'},
                 {'role':'assistant','content':'The download is complete.'}]
        self.assertFalse(wants_next_game_request('what next?',history))
        self.assertIsNone(self.preview('what to expect after deployment?',team='NYK'))
        history=[{'role':'assistant','content':self.preview()['answer']},
                 {'role':'user','content':'I finished deploying the bot'}]
        self.assertFalse(wants_next_game_request('what next?',history))
        self.assertIsNone(self.preview('what to expect?',history=history,team='NYK'))
        self.assertTrue(wants_next_game_request('next Knicks game',history))
        history=[{'role':'assistant','content':self.preview()['answer']},
                 {'role':'user','content':'Brunson more25.5 points'},
                 {'role':'assistant','content':'Pass for now.'}]
        self.assertFalse(wants_next_game_request('what to expect?',history))
        self.assertIsNone(self.preview('what to expect?',history=history,team='NYK'))

    def test_final_live_postponed_and_passed_tipoffs_are_not_the_next_game(self):
        self.calendar([('2026-11-05','NYK','CHI','2026-11-05T15:00:00Z','scheduled'),
                       ('2026-11-06','NYK','CLE','2026-11-07T00:00:00Z','postponed'),
                       ('2026-11-07','CLE','NYK','2026-11-08T01:00:00Z','scheduled'),
                       ('2026-11-10','NYK','MIL','2026-11-11T00:30:00Z','scheduled')])
        self.final('2026-11-07','CLE','NYK',99,100)
        result=self.preview()
        self.assertEqual(result['next_game_focus']['date'],'2026-11-10')
        self.assertIn('(home)',result['answer'])

    def test_no_saved_game_and_future_unknown_time_are_explicit(self):
        missing=self.preview('Next Warriors game')
        self.assertIn('don’t have a saved upcoming GSW game',missing['answer'])
        self.calendar([('2026-11-07','NYK','CLE',None,'time_tbd')])
        result=self.preview()
        self.assertIn('time TBD',result['answer'])
        self.assertFalse(result['next_game_focus']['timing_verified'])

    def test_fresh_independent_timing_is_qualified_by_its_original_check_clock(self):
        now=dt.datetime(2026,11,7,20,tzinfo=dt.timezone.utc)
        game={'id':canonical_game('2026-11-07','CLE','NYK'),'date':'2026-11-07','home_team':'CLE','away_team':'NYK',
              'status':'scheduled','start_time':'2026-11-08T01:00:00Z','reported_status':'scheduled'}
        self.store.set_checkpoint('nba_schedule_today',{'status':'success','date':'2026-11-07',
            'checked_at':'2026-11-07T19:55:00Z','error':None,'source':'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json',
            'games':[game]})
        result=build_preview(self.store,'next Knicks game',now=now)
        self.assertTrue(result['next_game_focus']['timing_verified'])
        self.assertIn('independently checked at 2:55 PM Eastern',result['answer'])
        later=build_preview(self.store,'next Knicks game',now=now+dt.timedelta(minutes=11))
        self.assertFalse(later['next_game_focus']['timing_verified'])
        self.assertIn('unverified',later['answer'])

    def test_failed_future_and_conflicting_independent_checks_do_not_promote_timing(self):
        for changes in [{'status':'failed'},{'checked_at':'2026-11-05T16:01:00Z'},
                        {'games':[{'id':'different'}]}]:
            with self.subTest(changes=changes):
                self.store.set_checkpoint('nba_schedule_today',{'status':'success','date':'2026-11-07',
                    'checked_at':NOW.isoformat(),'source':'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json',
                    'games':[],**changes})
                self.assertFalse(self.preview()['next_game_focus']['timing_verified'])

    def test_saved_recent_form_uses_only_known_final_scores_without_claiming_season_record(self):
        self.final('2026-11-04','NYK','CHI',105,100)
        self.final('2026-11-02','BKN','NYK',110,100)
        self.final('2026-11-01','CLE','BOS',99,90)
        self.final('2026-10-31','NYK','MIA',None,None)
        self.final('2026-10-29','NYK','PHI',0,0)
        self.final('2026-04-01','NYK','MIL',130,90)
        result=self.preview()
        form={r['team']:r for r in result['recent_form']}
        self.assertEqual((form['NYK']['games'],form['NYK']['wins'],form['NYK']['losses']),(2,1,1))
        self.assertEqual(form['NYK']['data_through_date'],'2026-11-04')
        self.assertEqual(form['NYK']['points_for_average'],102.5)
        self.assertEqual(form['NYK']['points_against_average'],105)
        self.assertEqual(form['CLE']['points_for_average'],99)
        self.assertEqual(form['CLE']['points_against_average'],90)
        self.assertIn('stored finals through 2026-11-04',result['answer'])
        self.assertIn('NYK scored 102.5/game while CLE allowed 90',result['answer'])
        self.assertIn('different opponents',result['answer'])
        self.assertIn('not a forecast',result['answer'])
        self.assertNotIn('season record',result['answer'])

    def test_opponent_appearance_in_knicks_calendar_does_not_establish_other_teams_next_game(self):
        document={'season':'2026-27','covered_teams':['NYK'],'retrieved_at':'2026-11-05T15:00:00Z',
            'source':SOURCE,'complete_for_published_games':False,'games':[{
                'date':'2026-12-01','home_team':'NYK','away_team':'CHI','start_time':'2026-12-02T00:30:00Z'}]}
        import_calendar(self.store,document)
        result=self.preview('What is the next Bulls game?')
        self.assertIn('Next saved matchup for CHI',result['answer'])
        self.assertIn('Full CHI schedule coverage is not established',result['answer'])
        self.assertIn('may not be their next NBA game',result['answer'])
        self.assertFalse(result['schedule_coverage']['selected_team_coverage_established'])
        history=[{'role':'assistant','content':result['answer']}]
        self.assertEqual(self.preview('what to expect?',history=history)['upcoming_game_team'],'CHI')

    def test_date_only_availability_and_absent_players_do_not_become_health_claims(self):
        from structured_availability import normalize_nba_injuries
        rows=[{'season':2027,'report_datetime':dt.datetime(2026,11,5),'game_date':'2026-11-07',
               'game_time':'08:00 (ET)','away_team_abbr':'NYK','home_team_abbr':'CLE',
               'team':'New York Knicks','report_player_name':'Hart, Josh','current_status':'Out',
               'reason':'Injury','person_id':1628404,'game_id':'0022600199','match_method':'exact'}]
        records=normalize_nba_injuries(rows,'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
            'nba_stats_injury_report/injury_report_2027.parquet','2026-11-05T15:00:00Z','b'*64,NOW)
        import_availability_document(self.store,build_availability_document(records,now=NOW),now=NOW)
        result=self.preview()
        self.assertEqual(result['availability'],[])
        self.assertIn('not verified here yet',result['answer'])
        self.assertNotIn('healthy',result['answer'])

    def test_explicit_fresh_official_designation_retains_report_and_game_scope(self):
        row={'game_date':'2026-11-07','away_team':'NYK','home_team':'CLE','team':'NYK',
             'player':'Josh Hart','status':'Out','reason':'Injury'}
        records=normalize_official_pdf([row],source_url='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-05_10_45AM.pdf',
            report_at='2026-11-05T10:45:00-05:00',text_sha256='a'*64,now=NOW)
        import_availability_document(self.store,build_availability_document(records,now=NOW),now=NOW)
        result=self.preview()
        self.assertEqual(result['availability'][0]['status'],'Out')
        self.assertIn('Josh Hart — Out',result['answer'])
        self.assertIn('do not establish complete lineups',result['answer'])
        stale=build_preview(self.store,'next Knicks game',now=NOW+dt.timedelta(hours=7))
        self.assertEqual(stale['availability'],[])

    def test_preview_is_read_only_and_multiple_team_subject_is_not_guessed(self):
        before=self.snapshot()
        self.preview()
        self.assertEqual(self.snapshot(),before)
        ambiguous=self.preview('what is next for the Bulls and Lakers?')
        self.assertIn('Which team',ambiguous['answer'])
        self.assertNotIn('next_game_focus',ambiguous)


if __name__=='__main__':unittest.main()
