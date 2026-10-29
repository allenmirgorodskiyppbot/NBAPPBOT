"""Verify that owner self-tests exercise real logic without polluting the live master."""
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import unittest

import app
from master import MasterStore
from self_check import format_self_check, run_self_checks


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.live = MasterStore(self.root / 'live.sqlite')
        self.live.record_news({'id': 'owner-news', 'title': 'Owner injury note', 'source': 'Fixture'})
        self.live.feed_success('rss_1', 'Fixture feed')

    def tearDown(self):
        self.directory.cleanup()

    def dump(self):
        with sqlite3.connect(self.root / 'live.sqlite') as db:
            return '\n'.join(db.iterdump())

    def test_self_tests_cover_imports_grading_exports_restore_without_live_writes(self):
        backups = self.root / 'backups'
        backups.mkdir()
        self.live.backup(str(backups / 'master.sqlite'))
        before = self.dump()
        report = run_self_checks(self.live, self.root, app.export_master)
        self.assertEqual(report['failed'], 0, report)
        self.assertEqual(report['warnings'], 0, report)
        self.assertGreaterEqual(report['passed'], 12)
        self.assertEqual(self.dump(), before)
        self.assertEqual(report['counts']['entries'], 0)
        self.assertIn('temporary databases', format_self_check(report))
        self.assertFalse(list(self.root.glob('knicks-selftest-*')))

    def test_corrupt_live_backup_is_reported_without_leaking_contents(self):
        backups = self.root / 'backups'
        backups.mkdir()
        (backups / 'bad.sqlite').write_text('PRIVATE_BACKUP_CONTENT')
        report = run_self_checks(self.live, self.root)
        failures = [check for check in report['checks'] if check['status'] == 'FAIL']
        self.assertEqual(len(failures), 1)
        self.assertIn('backup integrity', failures[0]['name'])
        self.assertNotIn('PRIVATE_BACKUP_CONTENT', str(report))

    def test_missing_backup_and_stale_source_are_warnings_not_fake_success(self):
        self.live.feed_failure('rss_1', 'Blocked fixture feed')
        report = run_self_checks(self.live, self.root)
        warnings = [check for check in report['checks'] if check['status'] == 'WARN']
        self.assertGreaterEqual(len(warnings), 2)
        self.assertEqual(report['failed'], 0)


if __name__ == '__main__':
    unittest.main()
