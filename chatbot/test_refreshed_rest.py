import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from master import MasterStore
from season_calendar import SEED_PATH, import_calendar, refreshed_rest_rows


class RefreshedRestTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'master.sqlite')
        self.publication=json.loads(SEED_PATH.read_text())
        import_calendar(self.store,self.publication)
        self.game=next(g for g in self.publication['games'] if g['date']=='2026-10-30')
        self.store.upsert_game({**self.game,'status':'final'})
        self.now=dt.datetime(2026,10,31,17,tzinfo=dt.timezone.utc)

    def test_completed_matching_game_updates_knicks_rest_only(self):
        rows=refreshed_rest_rows(self.store,self.now)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['team'],'NYK')
        self.assertEqual(rows[0]['rest_days'],1)
        self.assertEqual(rows[0]['previous_team_game'],'2026-10-28')
        self.assertEqual(rows[0]['source_asof'],self.game['source_asof'])

    def test_no_rest_from_partial_calendar_or_player_appearance_gap(self):
        meta=self.store.get_checkpoint('season_calendar:2026-27')
        meta['complete_for_published_games']=False
        self.store.set_checkpoint('season_calendar:2026-27',meta)
        self.assertEqual(refreshed_rest_rows(self.store,self.now),[])

    def test_unassigned_window_prevents_false_rest(self):
        meta=self.store.get_checkpoint('season_calendar:2026-27')
        meta['unassigned_windows']=[{'start':'2026-10-29','end':'2026-10-29'}]
        self.store.set_checkpoint('season_calendar:2026-27',meta)
        self.assertEqual(refreshed_rest_rows(self.store,self.now),[])

    def test_inconsistent_or_future_game_is_not_used(self):
        self.assertEqual(refreshed_rest_rows(self.store,dt.datetime(2026,10,29,17,tzinfo=dt.timezone.utc)),[])
        with self.store.connect() as db:
            db.execute("UPDATE games SET away_team='BOS' WHERE id=?",(self.game['id'],))
        self.assertEqual(refreshed_rest_rows(self.store,self.now),[])


if __name__=='__main__':
    unittest.main()
