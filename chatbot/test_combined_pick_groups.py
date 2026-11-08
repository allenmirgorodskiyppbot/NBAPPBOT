"""Uploaded offers and calculated targets retain separate follow-up identities."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation import _assistant_cards, resolve_conversation
from feeds import canonical_game
from master import MasterStore
from pregame_freshness import assess_pregame_freshness


NOW=dt.datetime(2026,11,7,23,tzinfo=dt.timezone.utc)
NEXT_DAY=dt.datetime(2026,11,8,6,tzinfo=dt.timezone.utc)
NAMES=['Jalen Brunson','Josh Hart','Karl-Anthony Towns','Donovan Mitchell','Evan Mobley','Darius Garland']


def combined(count=4, *, date_header='Assumed game date: 2026-11-07 Eastern; matched saved schedule.', same_market=False):
    lines=['Read 12 distinct lines across 4 recent screenshot(s).',date_header,
           'From your uploaded lines','My shortlist']
    for number,name in enumerate(NAMES[:count],1):
        lines.extend([f'{number}. {name} — More 19.5 points (standard offer)','Why: Saved comparison.'])
    lines.extend(['My own calculated targets from your master',
                  'My projections — CLE vs NYK · Nov 07, 8:00 PM Eastern',
                  'My target lines, not quoted PrizePicks offers.'])
    for number,name in enumerate(NAMES[:count],1):
        lines.extend([f'{number}. {name} · {"points" if same_market else "rebounds"}: 7',
                      'More if line ≤5.5; Less if line ≥8.5.'])
    return '\n'.join(lines)


class CombinedPickGroupsTests(unittest.TestCase):
    def setUp(self):
        names=patch('conversation._players',return_value=NAMES)
        names.start();self.addCleanup(names.stop)

    def resolve(self,text, *, answer=None, state=None, now=NOW):
        history=[{'role':'user','content':'Analyze these uploaded player lines.'},
                 {'role':'assistant','content':answer or combined()}]
        return resolve_conversation(None,None,text,history if state is None else None,state,now)

    def test_both_groups_keep_source_labels_without_promoting_calculated_targets(self):
        cards=_assistant_cards(combined(),NAMES,NOW)
        self.assertEqual(len(cards),8)
        self.assertTrue(all(card['proposal_group']=='uploaded' and card['line']==19.5
                            and card['side']=='more' for card in cards[:4]))
        self.assertTrue(all(card['proposal_group']=='independent' and card['line_kind']=='research_target'
                            and 'line' not in card and 'side' not in card for card in cards[4:]))
        self.assertTrue(all(card['game_date']=='2026-11-07' for card in cards))

    def test_own_first_projection_selects_a_target_without_an_offered_line(self):
        for question in ('Why your own first pick?','Why the first projection?','Explain target number 1 from the master.'):
            with self.subTest(question=question):
                turn=self.resolve(question)
                self.assertEqual(turn['intent'],'explanation')
                self.assertIsNone(turn['clarification'])
                self.assertEqual(turn['slots']['player'],'Jalen Brunson')
                self.assertEqual(turn['slots']['stat'],'reb')
                self.assertNotIn('line',turn['slots']);self.assertNotIn('side',turn['slots'])
                self.assertFalse(turn['complete_prop'])
                self.assertEqual(turn['recent_picks'][0]['proposal_group'],'independent')

    def test_unqualified_first_pick_or_player_across_both_groups_requires_clarification(self):
        for question in ('Why the first pick?','Explain #1','Why Brunson?'):
            with self.subTest(question=question):
                turn=self.resolve(question)
                self.assertEqual(turn['intent'],'clarification')
                self.assertIn('ambiguous_pick',turn['errors'])
                self.assertIn('uploaded lines',turn['clarification'])
                self.assertIn('master targets',turn['clarification'])
                self.assertFalse(turn['complete_prop'])

    def test_first_uploaded_pick_retains_the_actual_displayed_line(self):
        for question in ('Why the first pick from my screenshots?','Explain uploaded pick 1.',
                         'Why Brunson points from the images?'):
            with self.subTest(question=question):
                turn=self.resolve(question)
                self.assertEqual(turn['slots']['player'],'Jalen Brunson')
                self.assertEqual(turn['slots']['stat'],'pts')
                self.assertEqual(turn['slots']['line'],19.5)
                self.assertEqual(turn['recent_picks'][0]['proposal_group'],'uploaded')
                self.assertTrue(turn['complete_prop'])

    def test_explicit_group_resolves_same_player_and_same_market_without_borrowing_price(self):
        answer=combined(same_market=True)
        ambiguous=self.resolve('Why Brunson points?',answer=answer)
        self.assertIn('ambiguous_pick',ambiguous['errors'])
        own=self.resolve('Why Brunson points from the master?',answer=answer)
        self.assertEqual(own['slots']['stat'],'pts');self.assertNotIn('line',own['slots'])
        self.assertEqual(own['recent_picks'][0]['proposal_group'],'independent')
        uploaded=self.resolve('Why Brunson points from my uploads?',answer=answer)
        self.assertEqual(uploaded['slots']['line'],19.5)

    def test_uncaptioned_upload_date_survives_eastern_midnight_for_both_sources(self):
        for header in ('Game date: 2026-11-07 Eastern; matched saved schedule.',
                       'Assumed game date: 2026-11-07 Eastern; matched saved schedule.'):
            with self.subTest(header=header):
                answer=combined(date_header=header)
                uploaded=self.resolve('Why first pick from screenshots?',answer=answer,now=NEXT_DAY)
                own=self.resolve('Why first projection?',answer=answer,now=NEXT_DAY)
                self.assertEqual(uploaded['slots']['game_date'],'2026-11-07')
                self.assertEqual(own['slots']['game_date'],'2026-11-07')
                self.assertNotIn('line',own['slots'])

    def test_source_dates_invalid_or_nonleading_labels_cannot_become_the_target_game(self):
        for header in ('Published source through 2026-11-06.',
                       'Source: Game date: 2026-11-06.',
                       'Game date: 2026-99-07 Eastern.',
                       'Game date: today Eastern.'):
            with self.subTest(header=header):
                cards=_assistant_cards(combined(date_header=header),NAMES,NOW)
                self.assertTrue(cards)
                self.assertTrue(all('game_date' not in card for card in cards))

    def test_twelve_cards_and_their_groups_survive_state_cleanup_and_long_history(self):
        answer=combined(count=6)
        # Presentation prose may be longer than the old 5,000-character limit;
        # source cards at the end must still reach the reference parser.
        answer=answer.replace('My own calculated targets from your master',
            'Context: '+('saved ' * 650)+'\nMy own calculated targets from your master')
        self.assertGreater(len(answer),5000);self.assertLess(len(answer),6500)
        turn=self.resolve('Why sixth pick from the master?',answer=answer)
        self.assertEqual(len(turn['state']['recent_picks']),12)
        self.assertEqual(turn['slots']['player'],'Darius Garland')
        self.assertEqual(turn['recent_picks'][0]['proposal_group'],'independent')
        self.assertNotIn('line',turn['slots'])
        again=self.resolve('Why last pick from the screenshots?',state=turn['state'])
        self.assertEqual(len(again['state']['recent_picks']),12)
        self.assertEqual(again['recent_picks'][0]['proposal_group'],'uploaded')
        self.assertEqual(again['slots']['line'],19.5)

    def test_zero_or_nonexistent_group_ordinals_do_not_select_another_card(self):
        for question in ('Why uploaded pick 0?','Why projection 7?'):
            with self.subTest(question=question):
                turn=self.resolve(question)
                self.assertIn('missing_pick',turn['errors'])
                self.assertFalse(turn['complete_prop'])

    def test_independent_heading_cannot_turn_an_assistant_number_into_a_current_offer(self):
        answer=('Game date: 2026-11-07 Eastern.\nMy own calculated targets from your master\n'
                '1. Jalen Brunson — More 99.5 points')
        turn=self.resolve('Why first projection?',answer=answer)
        self.assertFalse(turn['complete_prop']);self.assertNotIn('line',turn['slots'])
        self.assertEqual(turn['recent_picks'][0]['line_kind'],'research_target')

    def test_remembered_game_date_is_not_an_independent_live_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'master.sqlite')
            store.upsert_game({'id':canonical_game('2026-11-07','CLE','NYK'),'date':'2026-11-07',
                'home_team':'CLE','away_team':'NYK','status':'scheduled',
                'start_time':'2026-11-08T01:00:00+00:00','source':'saved calendar fixture'})
            turn=self.resolve('Why first pick from my screenshots?')
            report=assess_pregame_freshness(store,turn['slots']['game_date'],'NYK',NOW)
            self.assertFalse(report['ready'])
            self.assertTrue(any('schedule within 15 minutes' in reason for reason in report['reasons']))
            self.assertFalse(store.get_checkpoint('nba_schedule_today'))


if __name__=='__main__':unittest.main()
