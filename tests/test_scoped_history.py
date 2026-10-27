import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from nbabot.core import Store,load_config,now
from nbabot.scoped_history import archive_feature_snapshot,load_feature_snapshot,collect_scoped_history,rotation_batch
from nbabot.scope import matchup_scope


class ScopedHistory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        config=load_config(Path(self.tmp.name)/"none")
        config.update(data_dir=self.tmp.name,organized_players=[f"00{i}" for i in range(1,9)])
        self.store=Store(config)
        for i in range(1,9):
            self.store.db.execute('INSERT INTO players VALUES(?,?)',(f'00{i}',f'Player {i}'))
            self.store.db.execute("""INSERT INTO player_games(game_id,player_id,team,opponent,season,date_et)
                VALUES('0022400001',?,'NYK','BOS','2024-25','2025-01-01')""",(f'00{i}',))
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_snapshot_is_now_and_only_prospective_games(self):
        old=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        for game_id,tip in [('past',old),('future',future)]:
            self.store.db.execute("INSERT INTO games(id,date_et,tipoff,home,away,status,known_at) VALUES(?,?,?,'NYK','BOS','scheduled',?)",(game_id,tip[:10],tip,old))
        self.store.db.commit()
        root=self.store.root/'reference/career'
        root.mkdir(parents=True)
        (root/'latest.json').write_text(json.dumps({'status':'retrospective','retrieved_at':now()}))
        metadata=archive_feature_snapshot(self.store)
        loaded=load_feature_snapshot(self.store)
        self.assertEqual(metadata['upcoming_games'],1)
        self.assertEqual(loaded['snapshot']['upcoming_games'][0]['id'],'future')
        self.assertEqual(loaded['snapshot']['prediction_cutoff'],metadata['captured_at'])
        self.assertEqual(loaded['snapshot']['reference_indices']['career']['index']['status'],'retrospective')
        self.assertEqual(loaded['status'],'prospective_only')

    def test_snapshot_tampering_fails(self):
        meta=archive_feature_snapshot(self.store)
        (self.store.root/'reference/pregame_snapshots'/meta['file']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'checksum'):
            load_feature_snapshot(self.store)

    def test_source_failure_retains_other_results_and_no_fake_success(self):
        patches=[patch('nbabot.career_reference.collect_career_reference',return_value={'status':'success'}),
            patch('nbabot.career_reference.load_career_reference',return_value={'rows':[]}),
            patch('nbabot.opportunity_reference.collect_opportunity_reference',side_effect=TimeoutError),
            patch('nbabot.matchup_reference.collect_matchup_reference',return_value={'status':'success','shots':12}),
            patch('nbabot.rotation_reference.collect_rotation_reference',return_value={'status':'partial','errors':[{'source':'fixture'}]})]
        for p in patches:p.start()
        try:
            result=collect_scoped_history(self.store)
        finally:
            for p in reversed(patches):p.stop()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['errors'][0]['source'],'opportunities')
        self.assertEqual(result['results']['matchups']['shots'],12)
        self.assertTrue((self.store.root/'reference/scoped_updates/latest.json').is_file())

    def test_rotation_retries_are_bounded_and_prioritize_new_games(self):
        root=self.store.root/'reference/rotations'
        root.mkdir(parents=True)
        past=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        (root/'latest.json').write_text(json.dumps({'errors':[{'game_id':'0022400001','attempted_at':past}]}))
        games=[{'game_id':'0022400001','date_et':'2025-01-01','opponent':'BOS'},
            {'game_id':'0022600001','date_et':'2026-10-20','opponent':'BOS'}]
        scope=matchup_scope(self.store)
        chosen=rotation_batch(self.store,games,scope,1)
        self.assertEqual([g['game_id'] for g in chosen],['0022600001'])
        (root/'latest.json').write_text(json.dumps({'errors':[{'game_id':'0022400001','attempted_at':now()}]}))
        self.assertEqual(rotation_batch(self.store,games[:1],scope,8),[])
        self.assertEqual(len(rotation_batch(self.store,games[:1],scope,8,force=True)),1)


if __name__=='__main__':
    unittest.main()
