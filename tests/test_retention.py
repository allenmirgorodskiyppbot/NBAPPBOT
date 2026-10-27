import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nbabot.core import Store, load_config
from nbabot.retention import prune_packages


class PackageRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = load_config(self.root / "none")
        config.update(data_dir=str(self.root / "data"), package_retention_days=14, package_min_versions=2)
        self.store = Store(config)
        self.packages = self.store.root / "packages"
        self.packages.mkdir()
        self.at = "2026-10-26T12:00:00+00:00"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def package(self, name, created, run=None):
        path = self.packages / name
        path.mkdir()
        manifest = {"version": name, "created_at": created, "reason": "manual canonical export",
                    "validation": {"status": "passed_with_limitations"}, "sha256": {"example.csv": "test"},
                    "last_successful_refresh": {"id": run} if run else None}
        (path / "manifest.json").write_text(json.dumps(manifest))
        (path / "example.csv").write_text("unchanged\n")
        return path

    def success(self, run, package, finished="2026-10-25T12:00:00+00:00"):
        self.store.db.execute("INSERT INTO update_runs VALUES(?,?,?,?,?,?,?)",
                              (run, finished, finished, "success", "{}", "[]", str(package)))
        self.store.db.commit()

    def test_prunes_only_old_verified_packages_and_protects_latest_and_committed(self):
        old = self.package("old", "2026-09-01T12:00:00+00:00")
        latest = self.package("latest_target", "2026-09-02T12:00:00+00:00")
        committed = self.package("committed", "2026-09-03T12:00:00+00:00", "r1")
        self.success("r1", committed)
        self.package("recent1", "2026-10-25T12:00:00+00:00")
        self.package("recent2", "2026-10-24T12:00:00+00:00")
        (self.packages / "latest").symlink_to(latest.name)
        result = prune_packages(self.store, self.at)
        self.assertEqual(result["deleted"], [old.name])
        self.assertTrue(latest.is_dir())
        self.assertTrue(committed.is_dir())
        self.assertEqual(result["status"], "success")
        self.assertGreater(result["disk"]["free_bytes"], 0)

    def test_minimum_versions_retained_even_when_all_are_old(self):
        for day in range(1, 5):
            self.package(str(day), f"2026-09-0{day}T12:00:00+00:00")
        result = prune_packages(self.store, self.at)
        self.assertEqual(set(result["deleted"]), {"1", "2"})
        self.assertTrue((self.packages / "3").exists())
        self.assertTrue((self.packages / "4").exists())

    def test_unrecognized_uncommitted_and_symlinks_are_untouched(self):
        self.package("current1", "2026-10-25T12:00:00+00:00")
        self.package("current2", "2026-10-24T12:00:00+00:00")
        uncommitted = self.package("uncommitted", "2026-09-01T12:00:00+00:00", "unknown")
        incomplete = self.packages / "incomplete"
        incomplete.mkdir()
        external = self.root / "outside"
        external.mkdir()
        (external / "keep").write_text("keep")
        (self.packages / "link").symlink_to(external)
        result = prune_packages(self.store, self.at)
        self.assertEqual(result["deleted"], [])
        self.assertTrue(uncommitted.exists())
        self.assertTrue(incomplete.exists())
        self.assertEqual((external / "keep").read_text(), "keep")
        self.assertEqual(len(result["skipped"]), 3)

    def test_escaping_latest_pointer_blocks_all_cleanup(self):
        self.package("old", "2026-09-01T12:00:00+00:00")
        (self.packages / "latest").symlink_to("../outside")
        result = prune_packages(self.store, self.at)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue((self.packages / "old").is_dir())

    def test_disabled_and_failure_do_not_delete_or_raise(self):
        old = self.package("old", "2026-09-01T12:00:00+00:00")
        self.package("current1", "2026-10-25T12:00:00+00:00")
        self.package("current2", "2026-10-24T12:00:00+00:00")
        self.store.config["package_retention_days"] = 0
        self.assertEqual(prune_packages(self.store, self.at)["status"], "disabled")
        self.assertTrue(old.exists())
        self.store.config["package_retention_days"] = 14
        with patch("nbabot.retention.shutil.rmtree", side_effect=PermissionError("denied")):
            result = prune_packages(self.store, self.at)
        self.assertEqual(result["status"], "warning")
        self.assertTrue(old.exists())

    def test_migrated_committed_package_name_is_protected(self):
        protected = self.package("recovery", "2026-09-01T12:00:00+00:00", "r1")
        self.success("r1", Path("/old_host/data/packages/recovery"))
        self.package("current1", "2026-10-25T12:00:00+00:00")
        self.package("current2", "2026-10-24T12:00:00+00:00")
        result = prune_packages(self.store, self.at)
        self.assertIn("recovery", result["protected"])
        self.assertTrue(protected.exists())

    def test_invalid_configuration_fails_before_deletion(self):
        for key, value in [("package_retention_days", -1), ("package_min_versions", 1),
                           ("package_retention_days", True), ("package_retention_days", float("nan"))]:
            config = self.store.config.copy()
            self.store.config[key] = value
            with self.assertRaises(ValueError):
                prune_packages(self.store, self.at)
            self.store.config = config


if __name__ == "__main__":
    unittest.main()
