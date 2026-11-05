"""Coverage-only calendar changes must update the exported workbook."""
import tempfile
import unittest
from pathlib import Path

from master import MasterStore
from dataset_update import data_fingerprint


class CalendarFingerprintTests(unittest.TestCase):
    def test_tbd_coverage_changes_are_semantic_but_polling_metadata_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MasterStore(Path(directory)/'fixture.sqlite')
            value={'season':'2026-27','assigned_games':1200,'unassigned_slots':[{'date':'2026-12-08','home_team':None}],
                   'team_coverage':[{'team':'NYK','assigned_games':80,'unassigned_team_games':2}],
                   'phase':'updated','changed':True,'revision':'first','source_checked_at':'2026-11-05T18:00:00Z'}
            store.set_checkpoint('season_schedule_refresh',value)
            first=data_fingerprint(store)
            value.update(phase='unchanged',changed=False,revision='metadata-only',source_checked_at='2026-11-05T19:00:00Z')
            store.set_checkpoint('season_schedule_refresh',value)
            self.assertEqual(first,data_fingerprint(store))
            value['unassigned_slots'][0]['date']='2026-12-09'
            store.set_checkpoint('season_schedule_refresh',value)
            self.assertNotEqual(first,data_fingerprint(store))
