import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from nbabot.core import DEFAULT
from nbabot.first_boot import prepare_spreadsheet_state, read_status, require_private_auth, write_status


class SpreadsheetFirstBoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "state/config.json"
        self.template = self.root / "template.json"
        self.template.write_text(json.dumps({**DEFAULT, "organized_players": [str(i) for i in range(1, 9)],
            "provider_url": "https://example.test/private", "provider_file": "/not/a/public/source"}))
        self.auth = patch.dict(os.environ, {"NBABOT_DASHBOARD_USER": "owner", "NBABOT_DASHBOARD_PASSWORD": "fixture-private-password"})
        self.auth.start()

    def tearDown(self):
        self.auth.stop()
        self.tmp.cleanup()

    def test_empty_state_disables_every_external_source(self):
        created = prepare_spreadsheet_state(self.path, self.template)
        self.assertTrue(created['created'])
        config = json.loads(self.path.read_text())
        self.assertEqual(config['data_source_mode'], 'spreadsheet')
        self.assertEqual(config['data_dir'], str(self.path.parent))
        for key in ('nba_enabled', 'public_context_enabled', 'historical_context_enabled', 'scoped_history_enabled'):
            self.assertFalse(config[key])
        self.assertIsNone(config['provider_url'])
        self.assertIsNone(config['provider_file'])
        self.assertEqual(config['provider_allowed_hosts'], [])
        self.assertFalse(config['external_sources_approved'])
        self.assertEqual(config['external_source_approvals'], [])
        db = sqlite3.connect(self.path.parent / 'nba.sqlite3')
        self.assertEqual(db.execute('SELECT count(*) FROM player_games').fetchone()[0], 0)
        db.close()
        self.assertFalse((self.path.parent / 'originals').exists())
        self.assertEqual(read_status(self.path.parent)['status'], 'waiting_for_master')
        self.assertEqual(read_status(self.path.parent)['outside_access'], 'awaiting_owner_approval')

    def test_missing_and_placeholder_auth_refuse_before_creating_state(self):
        for user, password in [('', ''), ('owner', ''), ('owner', 'REPLACE_WITH_A_PRIVATE_PASSWORD'), ('owner', 'changeme')]:
            with self.subTest(user=user, password_placeholder=not password or 'REPLACE' in password or password=='changeme'):
                with patch.dict(os.environ, {'NBABOT_DASHBOARD_USER':user, 'NBABOT_DASHBOARD_PASSWORD':password}):
                    with self.assertRaisesRegex(ValueError, 'non-placeholder'):
                        prepare_spreadsheet_state(self.path, self.template)
                self.assertFalse(self.path.parent.exists())

    def test_existing_other_mode_is_not_reused(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'data_source_mode':'public'}))
        sqlite3.connect(self.path.parent / 'nba.sqlite3').close()
        with self.assertRaisesRegex(ValueError, 'another data mode'):
            prepare_spreadsheet_state(self.path, self.template)
        self.assertEqual(json.loads(self.path.read_text())['data_source_mode'], 'public')

    def test_restart_preserves_data_status_and_local_settings(self):
        prepare_spreadsheet_state(self.path, self.template)
        db = sqlite3.connect(self.path.parent / 'nba.sqlite3')
        db.execute("INSERT INTO players VALUES('00123','Private Workbook Player')")
        db.commit(); db.close()
        write_status(self.path.parent, 'ready', uploaded_master_present=True, master_name='master.xlsx')
        config = json.loads(self.path.read_text())
        config.update(daily_time='06:42', nba_enabled=True, provider_url='https://example.test/accidental')
        self.path.write_text(json.dumps(config))
        result = prepare_spreadsheet_state(self.path, self.template)
        self.assertFalse(result['created'])
        after = json.loads(self.path.read_text())
        self.assertEqual(after['daily_time'], '06:42')
        self.assertFalse(after['nba_enabled'])
        self.assertIsNone(after['provider_url'])
        self.assertEqual(read_status(self.path.parent)['status'], 'ready')
        db = sqlite3.connect(self.path.parent / 'nba.sqlite3')
        self.assertEqual(db.execute('SELECT id FROM players').fetchone()[0], '00123')
        db.close()

    def test_unmarked_or_incomplete_state_is_not_replaced(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'data_source_mode':'spreadsheet'}))
        sqlite3.connect(self.path.parent / 'nba.sqlite3').close()
        with self.assertRaisesRegex(ValueError, 'marker is missing'):
            prepare_spreadsheet_state(self.path, self.template)

    def test_metadata_cannot_override_the_source_policy(self):
        prepare_spreadsheet_state(self.path, self.template)
        result = write_status(self.path.parent, 'ready', source_policy='online', external_refresh_enabled=True)
        self.assertEqual(result['source_policy'], 'master_primary')
        self.assertEqual(result['outside_access'], 'awaiting_owner_approval')
        self.assertFalse(result['external_refresh_enabled'])
        (self.path.parent / 'first_boot_status.json').write_text('not-json')
        self.assertEqual(read_status(self.path.parent)['status'], 'error')

    def test_later_owner_approval_survives_restart_without_enabling_flags(self):
        prepare_spreadsheet_state(self.path, self.template)
        config = json.loads(self.path.read_text())
        config.update(external_sources_approved=True, external_source_approvals=['stats.nba.com'])
        self.path.write_text(json.dumps(config))
        prepare_spreadsheet_state(self.path, self.template)
        after = json.loads(self.path.read_text())
        self.assertTrue(after['external_sources_approved'])
        self.assertEqual(after['external_source_approvals'], ['stats.nba.com'])
        self.assertFalse(after['nba_enabled'])
        self.assertFalse(after['public_context_enabled'])
        status = read_status(self.path.parent)
        self.assertEqual(status['outside_access'], 'approved')
        self.assertFalse(status['external_refresh_enabled'])

    def test_approved_explicit_integration_configuration_is_preserved(self):
        prepare_spreadsheet_state(self.path, self.template)
        config = json.loads(self.path.read_text())
        config.update(external_sources_approved=True, external_source_approvals=['authorized.example.test'],
            provider_url='https://authorized.example.test/feed', provider_allowed_hosts=['authorized.example.test'])
        self.path.write_text(json.dumps(config))
        prepare_spreadsheet_state(self.path, self.template)
        after = json.loads(self.path.read_text())
        self.assertEqual(after['provider_url'], 'https://authorized.example.test/feed')
        self.assertEqual(after['provider_allowed_hosts'], ['authorized.example.test'])
        self.assertFalse(after['nba_enabled'])
        self.assertTrue(read_status(self.path.parent)['external_refresh_enabled'])

    def test_approval_without_explicit_hosts_remains_pending(self):
        prepare_spreadsheet_state(self.path, self.template)
        config = json.loads(self.path.read_text())
        config.update(external_sources_approved=True, external_source_approvals=[], nba_enabled=True)
        self.path.write_text(json.dumps(config))
        prepare_spreadsheet_state(self.path, self.template)
        after = json.loads(self.path.read_text())
        self.assertTrue(after['external_sources_approved'])
        self.assertFalse(after['nba_enabled'])
        self.assertEqual(read_status(self.path.parent)['outside_access'], 'awaiting_owner_approval')


if __name__ == '__main__':
    unittest.main()
