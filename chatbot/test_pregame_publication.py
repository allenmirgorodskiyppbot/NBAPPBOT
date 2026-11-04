"""Daily freshness distinguishes verified offdays from incomplete publications."""
import copy
import datetime as dt
import unittest

from nba_daily_export import verified_day_inventory, reconcile_day_inventory


NOW = dt.datetime(2026,11,4,22,40,tzinfo=dt.timezone.utc)


def calendar():
    return {'leagues':[{'abbreviation':'NBA','calendarStartDate':'2026-09-30T07:00Z',
            'calendarEndDate':'2027-06-26T06:59Z',
            'calendar':['2026-11-02T08:00Z','2026-11-04T08:00Z']}], 'events':[]}


class PregamePublicationTests(unittest.TestCase):
    def test_explicit_league_calendar_offday_creates_scoped_empty_snapshot(self):
        inventory = verified_day_inventory(calendar(), '2026-11-03', NOW)
        result = reconcile_day_inventory(None, inventory, '2026-11-03', {'revision':'fixture'})
        self.assertTrue(result['complete'])
        self.assertTrue(result['inventory_verified'])
        self.assertTrue(result['inventory_off_day_verified'])
        self.assertEqual(result['expected_games'], 0)
        self.assertEqual(result['player_games'], [])
        self.assertEqual(result['scope_revision'], 'fixture')
        self.assertIn('scoreboard?dates=20261103',result['inventory_verification_source'])

    def test_empty_scoreboard_on_published_game_date_is_not_an_offday(self):
        with self.assertRaises(ValueError):
            verified_day_inventory(calendar(), '2026-11-02', NOW)

    def test_empty_or_out_of_season_calendar_cannot_prove_offday(self):
        for change in ('no_league','empty_calendar','other_league','wrong_season','naive_date'):
            with self.subTest(change=change):
                doc = calendar()
                if change=='no_league': doc['leagues']=[]
                elif change=='empty_calendar': doc['leagues'][0]['calendar']=[]
                elif change=='other_league': doc['leagues'][0]['abbreviation']='WNBA'
                elif change=='wrong_season': doc['leagues'][0]['calendarEndDate']='2026-06-26T06:59Z'
                else: doc['leagues'][0]['calendarStartDate']='2026-09-30'
                with self.assertRaises(ValueError):
                    verified_day_inventory(doc,'2026-11-03',NOW)

    def test_today_or_future_date_cannot_be_complete_daily_offday(self):
        for day in ('2026-11-04','2026-11-05'):
            with self.subTest(day=day), self.assertRaises(ValueError):
                verified_day_inventory(calendar(),day,NOW)

    def test_missing_boxscore_publication_for_nonempty_inventory_stays_missing(self):
        inventory = {'inventory_verified':True,'inventory_off_day_verified':False,
                     'games':[{'id':'game','status':'final'}]}
        self.assertIsNone(reconcile_day_inventory(None,inventory,'2026-11-02',{'revision':'fixture'}))

    def test_inventory_mismatch_does_not_upgrade_partial_publication(self):
        snapshot = {'complete':True,'expected_games':1,'games':[{'id':'one','status':'final'}]}
        inventory = {'inventory_verified':True,'games':[{'id':'one','status':'final'},
                                                       {'id':'two','status':'final'}]}
        before = copy.deepcopy(snapshot)
        result = reconcile_day_inventory(snapshot,inventory,'2026-11-02',{'revision':'fixture'})
        self.assertFalse(result['inventory_verified'])
        self.assertEqual(result['games'],snapshot['games'])
        self.assertEqual(snapshot,before)

    def test_status_disagreement_is_not_verified(self):
        snapshot = {'complete':True,'expected_games':1,'games':[{'id':'one','status':'final'}]}
        inventory = {'inventory_verified':True,'games':[{'id':'one','status':'postponed'}]}
        self.assertFalse(reconcile_day_inventory(snapshot,inventory,'2026-11-02',{})['inventory_verified'])

    def test_matching_inventory_keeps_original_statistics_publication_timestamp(self):
        snapshot = {'complete':True,'expected_games':1,'games':[{'id':'one','status':'final'}],
                    'published_at':'2026-11-03T15:00:00+00:00','player_games':[{'pts':17}]}
        inventory = {'inventory_verified':True,'inventory_verified_at':NOW.isoformat(),
                     'inventory_game_ids':['one'],'games':[{'id':'one','status':'final'}]}
        result = reconcile_day_inventory(snapshot,inventory,'2026-11-02',{})
        self.assertTrue(result['inventory_verified'])
        self.assertEqual(result['published_at'],snapshot['published_at'])
        self.assertEqual(result['player_games'],snapshot['player_games'])


if __name__ == '__main__':
    unittest.main()
