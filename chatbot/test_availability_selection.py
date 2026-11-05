"""Saved official designations reach the same selection and conversation gate."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from board_shortlist import player_availability, enrich_shortlist
from independent_picks import build_independent_picks
from master import MasterStore
from season_calendar import seed_calendar
from structured_availability import normalize_official_pdf, build_availability_document, import_availability_document
from test_board_shortlist import candidate, report
import test_independent_picks as independent_fixtures

NOW=dt.datetime(2026,10,30,22,40,tzinfo=dt.timezone.utc)
DAY='2026-10-30'


class OfficialAvailabilitySelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MasterStore(Path(self.temp.name)/'test.sqlite')
        seed_calendar(self.store)

    def official(self,status,*,stamp='2026-10-30T18:30:00-04:00',player='Jalen Brunson',day=DAY):
        clock=dt.datetime.fromisoformat(stamp)
        url='https://ak-static.cms.nba.com/referee/injury/Injury-Report_'+clock.strftime('%Y-%m-%d_%I_%M%p')+'.pdf'
        rows=normalize_official_pdf([{'game_date':day,'away_team':'PHI','home_team':'NYK',
            'team':'NYK','player':player,'status':status,'reason':'Synthetic official-report fixture'}],
            source_url=url,report_at=stamp,text_sha256='a'*64,now=NOW)
        import_availability_document(self.store,build_availability_document(rows,now=NOW))

    def news(self,title,stamp='2026-10-30T18:35:00-04:00'):
        return {'title':'Jalen Brunson: '+title,'published_at':stamp,'source':'Test publisher',
                'url':'https://publisher.example/fixture','game_date':DAY}

    def check(self,news=(),*,now=NOW,day=DAY,player='Jalen Brunson'):
        return player_availability(news,player,day,now,store=self.store)

    def test_official_out_questionable_and_doubtful_hold_with_exact_source(self):
        for status in ('Out','Questionable','Doubtful'):
            with self.subTest(status=status):
                self.official(status)
                result=self.check()
                self.assertTrue(result['blocked'])
                self.assertTrue(result['official_designation_known'])
                self.assertIn('official NBA report',result['note'])
                self.assertTrue(result['reports'][0]['url'].startswith('https://ak-static.cms.nba.com/'))
                with self.store.connect() as db:db.execute('DELETE FROM structured_availability')

    def test_missing_store_preserves_news_only_api_and_absence_does_not_mean_healthy(self):
        news=[self.news('Questionable Friday')]
        legacy=player_availability(news,'Jalen Brunson',DAY,NOW)
        self.assertFalse(legacy['blocked'])
        self.assertNotIn('official_designation_known',legacy)
        unknown=self.check()
        self.assertFalse(unknown['official_designation_known'])
        self.assertIsNone(unknown['confirmed_starter'])
        self.assertIsNone(unknown['expected_minutes'])
        self.assertFalse(unknown['blocked'])

    def test_stale_future_wrong_game_and_other_player_never_clear_news(self):
        self.official('Available')
        news=[self.news('Ruled out Friday')]
        for kwargs in ({'now':NOW+dt.timedelta(hours=7)}, {'now':NOW-dt.timedelta(hours=1)},
                       {'day':'2026-10-31'},{'player':'Josh Hart'}):
            result=self.check(news,**kwargs)
            self.assertFalse(result['official_designation_known'])
        self.assertTrue(self.check(news,now=NOW+dt.timedelta(hours=7))['blocked'])

    def test_available_snapshot_cannot_override_later_out_or_questionable_or_minutes_limit(self):
        self.official('Available')
        for text,stamp in [('Ruled out Friday','2026-10-30T18:35:00-04:00'),
                           ('Questionable Friday','2026-10-30T18:35:00-04:00'),
                           ('Minutes restriction Friday','2026-10-30T17:00:00-04:00')]:
            result=self.check([self.news(text,stamp)])
            self.assertTrue(result['blocked'],text)
            self.assertTrue(result['official_superseded_or_limited'])

    def test_later_official_available_clears_older_timed_out_without_inventing_minutes(self):
        self.official('Available')
        result=self.check([self.news('Ruled out Friday','2026-10-30T17:00:00-04:00')])
        self.assertFalse(result['blocked'])
        self.assertIsNone(result['note'])
        self.assertIsNone(result['expected_minutes'])
        self.assertIsNone(result['minutes_restriction'])

    def test_probable_keeps_caution_and_never_erases_existing_absence(self):
        self.official('Probable')
        self.assertIn('Probable',self.check()['note'])
        self.assertTrue(self.check([self.news('Ruled out Friday')])['blocked'])

    def test_official_out_reaches_screenshot_shortlist_without_news(self):
        self.official('Out')
        with patch('board_shortlist.assess_pregame_freshness',return_value={'ready':True,'reasons':[]}):
            result=enrich_shortlist(self.store,report(candidate()),NOW)
        self.assertEqual(result['shortlist'],[])
        self.assertTrue(any('official NBA report' in row['reason'] for row in result['skipped']))

    def test_official_out_reaches_independent_pick_gate_without_news(self):
        # Reuse observation fixture construction; the test uses a separate store.
        independent_fixtures.IndependentPickTests.observations(self,'Jalen Brunson','NYK')
        self.official('Out')
        with patch('independent_picks.assess_pregame_freshness',return_value={
                'ready':True,'reasons':[],'current_player_stat_coverage':['pts','reb','ast','pra','fga']}):
            result=build_independent_picks(self.store,DAY,now=NOW)
        self.assertEqual(result['picks'],[])
        self.assertTrue(result['evaluated_candidates'])
        self.assertTrue(all('official NBA report' in row['reason'] for row in result['evaluated_candidates']))


if __name__=='__main__':unittest.main()
