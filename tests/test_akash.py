import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from nbabot.core import Store, load_config
from nbabot.ingest import normalize_game, upsert_player_game
from nbabot.maintenance import export_package


def module(relative, name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/relative)
    loaded=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


seed=module('scripts/akash-seed.py','akash_seed')
runtime=module('deploy/akash/runtime.py','akash_runtime')
configure=module('scripts/akash-configure.py','akash_configure')


class AkashRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=load_config(self.root/'missing')
        self.config.update(data_dir=str(self.root/'data'),nba_enabled=False,public_context_enabled=False)
        self.path=self.root/'config.json'
        self.path.write_text(json.dumps(self.config))
        self.store=Store(self.config)
        row={'GAME_ID':'0022600001','PLAYER_ID':'00123','PLAYER_NAME':'Test Guard',
             'TEAM_ABBREVIATION':'NYK','Opponent':'BOS','Season':'2026-27','Game date':'2026-01-01',
             'Appeared':'Yes','MIN':34.098333333333336,'PTS':21,'REB':0,'AST':None,
             'FG3M':1,'FG3A':3,'FGM':6,'FGA':19,'FTM':8,'FTA':8,'OREB':0,'DREB':0}
        upsert_player_game(self.store,normalize_game(row,'2026-01-02T01:00:00+00:00'))
        self.store.db.execute("INSERT INTO locks VALUES('refresh','old','2026-01-02T01:00:00+00:00')")
        self.store.db.commit()
        export_package(self.store)
        latest=self.store.root/'packages/latest'
        nested=latest/'organized_csv/Data_PlayerGames.csv'
        nested.parent.mkdir(exist_ok=True)
        nested.write_text('game_id,player_id\n0022600001,00123\n')
        manifest=json.loads((latest/'manifest.json').read_text())
        manifest['sha256']['organized_csv/Data_PlayerGames.csv']=hashlib.sha256(nested.read_bytes()).hexdigest()
        (latest/'manifest.json').write_text(json.dumps(manifest))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_seed_roundtrip_preserves_canonical_values_and_clears_only_backup_runtime(self):
        output=self.root/'seed.zip'
        result=seed.create_seed(self.path,output)
        state=self.root/'restored'
        runtime.install_seed(output,state,result['sha256'])
        restored_config=load_config(state/'config.json')
        restored=Store(restored_config)
        try:
            row=restored.one('SELECT * FROM player_games')
            self.assertEqual(row['game_id'],'0022600001')
            self.assertEqual(row['player_id'],'00123')
            self.assertIsNone(row['ast'])
            self.assertEqual(row['reb'],0)
            self.assertEqual(row['minutes'],34.098333333333336)
            self.assertEqual(restored.rows('SELECT * FROM locks'),[])
            self.assertEqual(len(self.store.rows('SELECT * FROM locks')),1)
            self.assertTrue((state/'packages/latest/Master_Data.xlsx').is_file())
            self.assertEqual((state/'packages/latest/organized_csv/Data_PlayerGames.csv').read_text(),
                             'game_id,player_id\n0022600001,00123\n')
        finally:
            restored.close()

    def test_seed_bad_hash_leaves_destination_absent(self):
        output=self.root/'seed.zip'
        seed.create_seed(self.path,output)
        state=self.root/'restored'
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):
            runtime.install_seed(output,state,'0'*64)
        self.assertFalse(state.exists())

    def test_seed_traversal_does_not_escape_staging(self):
        archive=self.root/'bad.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('../escape','data')
        with self.assertRaisesRegex(ValueError,'Unsafe'):
            runtime.install_seed(archive,self.root/'restored',hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertFalse((self.root/'escape').exists())
        self.assertFalse((self.root/'restored').exists())

    def test_seed_does_not_overwrite_existing_state(self):
        output=self.root/'seed.zip'
        result=seed.create_seed(self.path,output)
        state=self.root/'restored'
        state.mkdir()
        (state/'keep').write_text('existing')
        with self.assertRaisesRegex(ValueError,'existing'):
            runtime.install_seed(output,state,result['sha256'])
        self.assertEqual((state/'keep').read_text(),'existing')

    def test_sdl_renderer_requires_digest_and_quotes_url(self):
        target=self.root/'deploy.yaml'
        with self.assertRaises(ValueError):
            configure.render('nbappbot:latest','https://example.test/seed.zip','a'*64,'uact','10000',target)
        configure.render('ghcr.io/example/nba@sha256:'+'b'*64,'https://example.test/seed.zip?x=1&y=a#frag','a'*64,'uact','10000',target)
        text=target.read_text()
        self.assertIn('"NBABOT_SEED_URL=https://example.test/seed.zip?x=1&y=a#frag"',text)
        self.assertEqual(target.stat().st_mode&0o777,0o600)


class SpreadsheetRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=self.root/'state/config.json'
        self.template=self.root/'template.json'
        self.template.write_text(json.dumps({'organized_players':[str(i) for i in range(1,9)]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_spreadsheet_mode_initializes_without_seed_or_network(self):
        settings={'NBABOT_INIT_MODE':'spreadsheet','NBABOT_CONFIG_TEMPLATE':str(self.template),
                  'NBABOT_DASHBOARD_USER':'owner','NBABOT_DASHBOARD_PASSWORD':'fixture-private-password'}
        with patch.dict(os.environ,settings), patch.object(runtime,'build_opener',side_effect=AssertionError('No network allowed')):
            runtime.bootstrap(self.config)
        loaded=json.loads(self.config.read_text())
        self.assertEqual(loaded['data_source_mode'],'spreadsheet')
        self.assertFalse(loaded['nba_enabled'])
        self.assertFalse(loaded['public_context_enabled'])
        self.assertEqual(json.loads((self.config.parent/'first_boot_status.json').read_text())['status'],'waiting_for_master')

    def test_private_auth_is_required_before_starting_children(self):
        with patch.dict(os.environ,{'NBABOT_INIT_MODE':'spreadsheet','NBABOT_DASHBOARD_USER':'owner','NBABOT_DASHBOARD_PASSWORD':''}), \
                patch.object(runtime.subprocess,'run',side_effect=AssertionError('Child must not start')):
            with self.assertRaisesRegex(ValueError,'non-placeholder'):
                runtime.run(self.config)
        self.assertFalse(self.config.parent.exists())

    def test_removed_public_boot_mode_cannot_start_online_initialization(self):
        with patch.dict(os.environ,{'NBABOT_INIT_MODE':'public'}):
            with self.assertRaisesRegex(ValueError,'seed or spreadsheet'):
                runtime.bootstrap(self.config)
        self.assertFalse(self.config.parent.exists())


if __name__=='__main__':
    unittest.main()
