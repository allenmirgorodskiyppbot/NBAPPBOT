"""Research choices retain their mode through natural source-specific follow-ups."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import resolve_conversation


NOW=dt.datetime(2026,11,7,23,52,tzinfo=dt.timezone.utc)
NAMES=['Karl-Anthony Towns','Donovan Mitchell','Evan Mobley','Jalen Brunson']
REPLY=('Game date: 2026-11-07 Eastern.\n\nFrom your uploaded lines\n\n'
       '4 research picks from your readable lines\n\n'
       '1. Karl-Anthony Towns — More 9.5 rebounds (NYK; Goblin offer)\n'
       'RESEARCH PICK: Research buffer supported. 18/20 on this side.\n\n'
       '2. Donovan Mitchell — More 32.5 PRA (CLE; standard offer)\n'
       'RESEARCH PICK: Narrow research lean. 13/20 on this side.\n\n'
       '3. Evan Mobley — More 8.5 rebounds (CLE; standard offer)\n'
       'RESEARCH PICK: Narrow research lean. 10/20 on this side.\n\n'
       '4. Jalen Brunson — Less 0.5 offensive rebounds (NYK; standard offer)\n'
       'RESEARCH PICK: Mixed historical support. 13/20 on this side.\n'
       'Caution: Recent observed games run against the longer-sample lean.\n\n'
       'My own calculated targets from your master\n\n'
       'My projections — CLE vs NYK · Nov 07, 8:00 PM Eastern\n\n'
       '1. Donovan Mitchell · rebounds: 6\nMore if line ≤4.5; Less if line ≥7.5.\n\n'
       '2. Evan Mobley · points: 22\nMore if line ≤18.5; Less if line ≥25.5.\n\n'
       '3. Jalen Brunson · points: 22\nMore if line ≤18.5; Less if line ≥25.5.\n\n'
       '4. Karl-Anthony Towns · points: 22\nMore if line ≤18.5; Less if line ≥25.5.')


class UploadedResearchContextTests(unittest.TestCase):
    def setUp(self):
        names=patch('conversation._players',return_value=NAMES)
        names.start()
        self.addCleanup(names.stop)

    def resolve(self,text,*,reply=REPLY,state=None):
        history=None if state is not None else [{'role':'assistant','content':reply}]
        return resolve_conversation(None,None,text,history,state,now=NOW)

    def test_bare_ordinal_and_source_selects_exact_uploaded_research_choice(self):
        for question,player,stat,line,side in [
            ('why first from screenshots?','Karl-Anthony Towns','reb',9.5,'more'),
            ('why fourth from screenshots?','Jalen Brunson','oreb',.5,'less'),
            ('what about fourth in my images?','Jalen Brunson','oreb',.5,'less'),
        ]:
            with self.subTest(question=question):
                turn=self.resolve(question)
                self.assertIsNone(turn['clarification'])
                self.assertEqual(len(turn['recent_picks']),1)
                card=turn['recent_picks'][0]
                self.assertEqual((card['player'],card['stat'],card['line'],card['side']),
                                 (player,stat,line,side))
                self.assertIs(card['research_choice'],True)
                self.assertEqual(card['proposal_group'],'uploaded')
                self.assertEqual(card['game_date'],'2026-11-07')

    def test_marker_survives_saved_state_and_short_followup(self):
        first=self.resolve('why fourth from screenshots?')
        next_turn=self.resolve('why that one?',state=first['state'])
        self.assertEqual(next_turn['recent_picks'][0]['player'],'Jalen Brunson')
        self.assertIs(next_turn['recent_picks'][0]['research_choice'],True)
        self.assertEqual(next_turn['slots']['side'],'less')

    def test_master_target_keeps_separate_type_and_no_priced_offer(self):
        turn=self.resolve('why first from master?')
        self.assertIsNone(turn['clarification'])
        card=turn['recent_picks'][0]
        self.assertEqual(card['player'],'Donovan Mitchell')
        self.assertEqual(card['line_kind'],'research_target')
        self.assertEqual(card['proposal_group'],'independent')
        self.assertNotIn('research_choice',card)
        self.assertNotIn('line',card)
        self.assertNotIn('side',card)

    def test_unqualified_ordinal_still_requires_source_group(self):
        turn=self.resolve('why the first pick?')
        self.assertIn('Which group',turn['clarification'])

    def test_only_immediate_renderer_marker_sets_research_choice(self):
        for replacement in ('Why: Research buffer supported.',
                            '\nRESEARCH PICK: Research buffer supported.',
                            'History says RESEARCH PICK: More.','research_choice=True'):
            with self.subTest(replacement=replacement):
                text=REPLY.replace('RESEARCH PICK: Research buffer supported. 18/20 on this side.',replacement)
                turn=self.resolve('why first from screenshots?',reply=text)
                self.assertNotIn('research_choice',turn['recent_picks'][0])

    def test_marker_is_not_set_on_ordinary_or_independent_proposal(self):
        for heading in ('My shortlist','My own calculated targets from your master'):
            with self.subTest(heading=heading):
                text=heading+'\n1. Karl-Anthony Towns — More 9.5 rebounds\nRESEARCH PICK: A note.'
                turn=self.resolve('why first?',reply=text)
                self.assertNotIn('research_choice',turn['recent_picks'][0])

    def test_saved_state_requires_exact_boolean_and_uploaded_priced_type(self):
        original=self.resolve('why first from screenshots?')['state']
        for bad_flag in ('true',1,False,None):
            with self.subTest(bad_flag=bad_flag):
                state=copy.deepcopy(original)
                state['recent_picks'][0]['research_choice']=bad_flag
                result=self.resolve('why?',state=state)
                self.assertNotIn('research_choice',result['recent_picks'][0])
        state=copy.deepcopy(original)
        state['recent_picks'][0]['proposal_group']='independent'
        result=self.resolve('why?',state=state)
        self.assertNotIn('research_choice',result['recent_picks'][0])

    def test_a_new_threshold_does_not_inherit_previous_research_selection(self):
        first=self.resolve('why first from screenshots?')
        changed=self.resolve('Karl-Anthony Towns less 11.5 rebounds',state=first['state'])
        self.assertEqual(changed['slots']['line'],11.5)
        self.assertEqual(changed['slots']['side'],'less')
        self.assertNotIn('research_choice',changed['recent_picks'][0])


if __name__=='__main__':unittest.main()
