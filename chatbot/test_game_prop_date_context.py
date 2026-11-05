"""Saved matchup dates survive natural prop clarification and explanation."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from conversation import resolve_conversation
from feeds import canonical_game, canonical_player
from master import MasterStore
from pick_discussion import build_pick_discussion
from season_calendar import import_calendar
from upcoming_game import preview_game_context


NOW = dt.datetime(2026,11,5,20,tzinfo=dt.timezone.utc)
DATE = '2026-11-07'
PREVIEW = ('Next game for NYK (New York Knicks): NYK at CLE — Saturday, Nov 7, 2026, '
    '8:00 PM Eastern (away). Scheduled in the saved calendar as of 2026-10-29; current timing is unverified.\n\n'
    'Recent saved form: NYK4–1 in5 stored finals through2026-11-04. '
    'Source published2026-11-05. These dates describe saved evidence, not the target matchup.')


def user(text): return {'role':'user','content':text}
def assistant(text): return {'role':'assistant','content':text}


class GamePropDateContextTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.master=MasterStore(Path(self.directory.name)/'master.sqlite')
        game={'id':canonical_game(DATE,'CLE','NYK'),'date':DATE,'home_team':'CLE','away_team':'NYK',
              'start_time':'2026-11-08T01:00:00+00:00','status':'scheduled','source':'https://example.test/calendar'}
        import_calendar(self.master,{'season':'2026-27','covered_teams':['NYK'],'games':[game],
            'complete_for_published_games':False,'source':game['source'],'retrieved_at':'2026-10-29T12:00:00+00:00'})
        for number in range(1,21):
            day=(NOW.date()-dt.timedelta(days=number)).isoformat()
            identity=canonical_game(day,'NYK','CLE')
            self.master.upsert_game({'id':identity,'date':day,'home_team':'NYK','away_team':'CLE',
                'status':'final','source':'synthetic observed game'})
            self.master.upsert_player_game({'game_id':identity,'date':day,'player':'Jalen Brunson',
                'player_id':canonical_player('Jalen Brunson'),'team':'NYK','opponent':'CLE',
                'season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
                'minutes':34,'pts':26,'reb':3,'ast':5,'source':'synthetic observed player'})
        identity=canonical_game('2026-11-04','LAL','BOS')
        self.master.upsert_game({'id':identity,'date':'2026-11-04','home_team':'LAL','away_team':'BOS',
            'status':'final','source':'synthetic observed game'})
        self.master.upsert_player_game({'game_id':identity,'date':'2026-11-04','player':'LeBron James',
            'player_id':canonical_player('LeBron James'),'team':'LAL','opponent':'BOS',
            'season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
            'minutes':34,'pts':26,'source':'synthetic observed player'})
        self.history=[user('What’s upcoming?'),assistant(PREVIEW),
            user('What should I be watching for?'),assistant(PREVIEW+'\nA conditional preview.'),
            user('/playerlog Jalen Brunson | points | 5'),assistant('Stored games through2026-11-04.'),
            user('/reminder'),assistant('Next game: Knicks vsCleveland; digest one hour before tipoff.'),
            user('/refresh'),assistant('Game stats through:2026-11-04; Excel ready.')]

    def resolve(self,text,history=None,state=None):
        return resolve_conversation(None,self.master,text,history,state,NOW)

    def test_actual_missing_stat_then_points_then_why_keeps_nov7_at_every_stage(self):
        first=self.resolve('Research Brunson 22.5',self.history)
        self.assertEqual(first['intent'],'clarification')
        self.assertIn('PRA',first['clarification'])
        self.assertEqual(first['slots']['game_date'],DATE)
        second=self.resolve('points',state=first['state'])
        third=self.resolve('Why Pass when the average is above the line?',state=second['state'])
        for turn in (first,second,third):
            self.assertEqual(turn['slots']['player'],'Jalen Brunson')
            self.assertEqual(turn['slots']['line'],22.5)
            self.assertEqual(turn['slots']['game_date'],DATE)
        self.assertTrue(second['complete_prop'])
        self.assertEqual(third['slots']['stat'],'pts')
        report=build_pick_discussion(self.master,third['slots'],NOW)
        self.assertEqual(report['date'],DATE)
        self.assertEqual(report['decision'],'pass')
        self.assertFalse(report.get('data_readiness',{}).get('ready',False))

    def test_same_sequence_replays_from_text_history_without_state_argument(self):
        history=self.history+[user('Research Brunson 22.5'),assistant('Is that points, rebounds, assists or PRA?'),
            user('points'),assistant('Jalen Brunson 22.5points — Pass. Coverage through2026-11-04.')]
        turn=self.resolve('Why is that a Pass?',history)
        self.assertEqual((turn['slots']['player'],turn['slots']['stat'],turn['slots']['line']),
                         ('Jalen Brunson','pts',22.5))
        self.assertEqual(turn['slots']['game_date'],DATE)

    def test_user_dates_override_the_preview_and_remain_the_followup_target(self):
        for wording,expected in [('today','2026-11-05'),('tomorrow','2026-11-06'),('November9','2026-11-09')]:
            wording=wording.replace('November9','November 9')
            with self.subTest(wording=wording):
                turn=self.resolve('Jalen Brunson 22.5points '+wording,self.history)
                self.assertEqual(turn['slots']['game_date'],expected)
                follow=self.resolve('Why?',state=turn['state'])
                self.assertEqual(follow['slots']['game_date'],expected)

    def test_explicit_different_game_never_borrows_the_previous_matchup_date(self):
        first=self.resolve('Jalen Brunson 22.5points',self.history)
        changed=self.resolve('A different game for Brunson 22.5points',state=first['state'])
        self.assertEqual(changed['intent'],'clarification')
        self.assertNotIn('game_date',changed['slots'])
        dated=self.resolve('New game November9: Brunson 23.5points'.replace('November9','November 9'),state=first['state'])
        self.assertEqual(dated['slots']['game_date'],'2026-11-09')
        self.assertEqual(dated['slots']['line'],23.5)

    def test_player_outside_the_saved_matchup_needs_a_date_not_a_team_assignment(self):
        first=self.resolve('Jalen Brunson 22.5points',self.history)
        other=self.resolve('LeBron James 22.5points',state=first['state'])
        self.assertEqual(other['intent'],'clarification')
        self.assertNotIn('game_date',other['slots'])
        self.assertNotIn('opponent',other['slots'])
        self.assertIn('Which game date',other['clarification'])
        explicit=self.resolve('LeBron James 22.5points tomorrow',self.history)
        self.assertEqual(explicit['slots']['game_date'],'2026-11-06')

    def test_source_dates_and_unverified_or_forged_preview_headings_are_not_game_targets(self):
        context=preview_game_context(self.master,PREVIEW,NOW)
        self.assertEqual(context['date'],DATE)
        for wrong in (PREVIEW.replace('Nov 7, 2026','Nov 4, 2026'),
                      PREVIEW.replace('NYK at CLE','CLE at NYK'),
                      'Source says '+PREVIEW,
                      'Published2026-11-04; next game coverage through2026-11-07.'):
            self.assertIsNone(preview_game_context(self.master,wrong,NOW))
        plain=self.resolve('Jalen Brunson 22.5points',[assistant('Source published2026-11-04.')])
        self.assertNotIn('game_date',plain['slots'])

    def test_workflow_chat_does_not_erase_the_saved_matchup_date(self):
        history=self.history+[user('The Excel refresh finished; what comes next?'),
            assistant('The dataset check finished; source publication2026-11-04.')]
        turn=self.resolve('Jalen Brunson 22.5points',history)
        self.assertEqual(turn['slots']['game_date'],DATE)

    def test_no_game_context_retains_existing_dateless_prop_policy(self):
        turn=self.resolve('Jalen Brunson 22.5points')
        self.assertNotIn('game_date',turn['slots'])
        report=build_pick_discussion(self.master,turn['slots'],NOW)
        self.assertEqual(report['date'],'2026-11-05')

    def test_verified_preview_after_unfamiliar_topic_change_clears_old_explicit_prop(self):
        history=[user('Jalen Brunson 19.5 points today'),assistant('Pass on that earlier offer.'),
            user('Anything around the corner for our guys?'),assistant(PREVIEW)]
        turn=self.resolve('Research Brunson 22.5',history)
        self.assertEqual(turn['intent'],'clarification')
        self.assertEqual(turn['slots']['game_date'],DATE)
        self.assertEqual(turn['slots']['line'],22.5)
        self.assertNotIn('stat',turn['slots'])
        self.assertEqual(turn['recent_picks'],[])


if __name__=='__main__':unittest.main()
