"""Direct season averages retain useful snapshots without inventing freshness."""
import json
from pathlib import Path
import tempfile
import unittest

from master import MasterStore
from player_stats import answer_player_question


class DirectPublishedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'master.sqlite')
        self.store.upsert_game({'id':'fixture:oct20','date':'2026-10-20','home_team':'NYK',
                                'away_team':'PHI','status':'final'})
        self.store.upsert_player_game({'game_id':'fixture:oct20','date':'2026-10-20',
            'player':'Jalen Brunson','team':'NYK','opponent':'PHI','season':'2026-27',
            'season_type':'Regular Season','appearance_state':'Played','pts':21,'ast':8,
            'source':'Fixture: one actual stored appearance'})
        self.record={'entity':'Jalen Brunson','season':'2026-27','season_type':'Regular Season',
            'category':'Player totals','gp':5,'fields':{'pts':129,'ast':35},
            'published_at':'2026-10-29T14:18:20+00:00','data_through_date':None,
            'source':'https://example.com/published-season-snapshot'}

    def save(self):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                ('2026-27','Jalen Brunson','Player totals',json.dumps(self.record)))

    def ask(self,text='What is Jalen Brunson PPG in 2026-27?'):
        self.save()
        return answer_player_question(self.store,text)

    def test_broader_undated_snapshot_is_separate_from_dated_partial_observations(self):
        answer=self.ask()
        self.assertFalse(answer['ai_used'])
        self.assertEqual(answer['data']['averages']['pts'],21)
        self.assertEqual(answer['data']['published_snapshot']['averages']['pts'],25.8)
        self.assertEqual(answer['data']['published_snapshot']['games_played'],5)
        self.assertIsNone(answer['data']['published_snapshot']['through_date'])
        for text in ('1 stored games','Average pts: 21.00','pts: 25.80 per game',
                     'Published games played: 5','Covered game date: unknown',
                     '2026-10-29T14:18:20+00:00','The two samples are not combined.'):
            self.assertIn(text,answer['answer'])
        with self.store.connect() as db:
            row=db.execute("SELECT games_played,ppg,through_date FROM season_averages WHERE player='Jalen Brunson'").fetchone()
        self.assertEqual(tuple(row),(1,21.0,'2026-10-20'))

    def test_known_stale_or_earlier_published_snapshots_stay_excluded(self):
        for covered,published in [('2026-10-19','2026-10-29T14:18:20+00:00'),
                                  (None,'2026-10-20T01:00:00+00:00')]:
            with self.subTest(covered=covered,published=published):
                self.record.update(data_through_date=covered,published_at=published)
                answer=self.ask()
                self.assertNotIn('published_snapshot',answer['data'])
                self.assertNotIn('25.80',answer['answer'])

    def test_known_coverage_can_still_supply_primary_season_average(self):
        self.record['data_through_date']='2026-10-28'
        answer=self.ask()
        self.assertEqual(answer['data']['averages']['pts'],25.8)
        self.assertNotIn('published_snapshot',answer['data'])

    def test_game_logs_other_phases_and_advanced_metrics_do_not_use_this_snapshot(self):
        self.assertNotIn('published_snapshot',self.ask('Jalen Brunson last one game points in 2026-27')['data'])
        self.assertNotIn('25.80',self.ask('Jalen Brunson playoff PPG in 2026-27')['answer'])
        self.assertIsNone(self.ask('Jalen Brunson average potential assists in 2026-27'))

    def test_multiple_requested_stats_keep_each_population_separate(self):
        answer=self.ask('Jalen Brunson average points and assists in 2026-27')
        self.assertEqual(answer['data']['averages'],{'pts':21,'ast':8})
        self.assertEqual(answer['data']['published_snapshot']['averages'],{'pts':25.8,'ast':7})


if __name__=='__main__':unittest.main()
