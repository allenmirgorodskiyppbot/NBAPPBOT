import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nbabot.core import Store, load_config
from nbabot.historical_context import (_normalize_rows, _requests,
    collect_context_reference, load_context_reference)


def official(spec, *, bad=False):
    if spec["kind"] == "team_games":
        headers = ["SEASON_YEAR", "GAME_ID", "TEAM_ID", "GAME_DATE", "MATCHUP", "MIN", "PTS"]
        rows = [[spec["season"], "0022600001", 1610612752, "2026-10-20T00:00:00", "NYK vs. BOS", 240.0167, 0],
                [spec["season"], "0022600001", 1610612738, "2026-10-20T00:00:00", "BOS @ NYK", 240.0167, None]]
    else:
        headers = ["PLAYER_ID", "TEAM_ID", "GP", "POTENTIAL_AST", "AST"]
        rows = [["00123", 1610612752, 2, None, 0]]
    if bad:
        rows.append(rows[0])
    return {"parameters": {"Season": spec["season"]}, "resultSets": [{"headers": headers, "rowSet": rows}]}


class HistoricalContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        c = load_config(Path(self.tmp.name) / "missing.json")
        c.update(data_dir=str(Path(self.tmp.name) / "data"), season="2026-27")
        self.store = Store(c)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_official_ids_minutes_and_null_zero_are_preserved(self):
        spec = _requests("2026-27")[0]
        rows = _normalize_rows(official(spec), spec)
        self.assertEqual(rows[0]["GAME_ID"], "0022600001")
        self.assertEqual(rows[0]["TEAM_ID"], "1610612738")
        self.assertEqual(rows[0]["MIN"], 240.0167)
        self.assertIsNone(rows[0]["PTS"])
        self.assertEqual(rows[1]["PTS"], 0)

    def test_duplicate_or_missing_team_and_wrong_season_fail(self):
        spec = _requests("2026-27")[0]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            _normalize_rows(official(spec, bad=True), spec)
        data = official(spec)
        data["resultSets"][0]["rowSet"].pop()
        with self.assertRaisesRegex(ValueError, "two distinct"):
            _normalize_rows(data, spec)
        data = official(spec)
        data["parameters"]["Season"] = "2025-26"
        with self.assertRaisesRegex(ValueError, "season"):
            _normalize_rows(data, spec)

    def test_empty_tracking_is_not_zero_and_season_means_remain_separate(self):
        spec = _requests("2026-27")[2]
        data = official(spec)
        rows = _normalize_rows(data, spec)
        self.assertEqual(rows[0]["PLAYER_ID"], "00123")
        self.assertIsNone(rows[0]["POTENTIAL_AST"])
        self.assertEqual(rows[0]["AST"], 0)
        data["resultSets"][0]["rowSet"] = []
        with self.assertRaisesRegex(ValueError, "No observations"):
            _normalize_rows(data, spec)

    def test_repeated_capture_failure_retains_last_good_tables(self):
        by_url = {r["source_url"]: r for r in _requests("2026-27")}
        def fetch(url, config):
            return official(by_url[url])
        with patch("nbabot.maintenance.fetch_json", side_effect=fetch):
            report = collect_context_reference(self.store)
        self.assertEqual(report["status"], "success")
        before = load_context_reference(self.store)
        self.assertEqual(len(before["tables"]), 5)
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"], 0)
        with patch("nbabot.maintenance.fetch_json", side_effect=TimeoutError("fixture timeout")):
            report = collect_context_reference(self.store)
        self.assertEqual(report["status"], "failed")
        after = load_context_reference(self.store)
        self.assertEqual([r["rows_sha256"] for r in before["tables"]], [r["rows_sha256"] for r in after["tables"]])
        self.assertEqual({r["refresh_status"] for r in after["tables"]}, {"retained_after_failure"})
        self.assertEqual(len(after["errors"]), 5)
        self.assertTrue(all(r["retained_previous"] for r in after["errors"]))
        tracking = next(r for r in after["tables"] if r["kind"] == "tracking_season")
        self.assertIsNone(tracking["coverage_cutoff"])
        self.assertIn("season means", tracking["unit"])

    def test_cache_integrity_and_paths_are_checked(self):
        by_url = {r["source_url"]: r for r in _requests("2026-27")}
        with patch("nbabot.maintenance.fetch_json", side_effect=lambda url, config: official(by_url[url])):
            collect_context_reference(self.store)
        root = self.store.root / "reference/context"
        index = json.loads((root / "latest.json").read_text())
        index["tables"][0]["path"] = "../nba.sqlite3"
        (root / "latest.json").write_text(json.dumps(index))
        loaded = load_context_reference(self.store)
        self.assertEqual(len(loaded["tables"]), 4)
        self.assertEqual(loaded["errors"][0]["source"], "cache_integrity")

    def test_base_advanced_coverage_conflict_retains_both_previous_views(self):
        by_url = {r["source_url"]: r for r in _requests("2026-27")}
        with patch("nbabot.maintenance.fetch_json", side_effect=lambda url, config: official(by_url[url])):
            collect_context_reference(self.store)
        before = {r["key"]: r for r in load_context_reference(self.store)["tables"]}
        def conflict(url, config):
            spec = by_url[url]
            obj = official(spec)
            if spec["measure"] == "Advanced":
                for row in obj["resultSets"][0]["rowSet"]:
                    row[1] = "0022600002"
            return obj
        with patch("nbabot.maintenance.fetch_json", side_effect=conflict):
            report = collect_context_reference(self.store)
        self.assertEqual(report["status"], "partial")
        after = {r["key"]: r for r in load_context_reference(self.store)["tables"]}
        for measure in ("Base", "Advanced"):
            key = "team_games:2026-27:" + measure
            self.assertEqual(after[key]["rows_sha256"], before[key]["rows_sha256"])
            self.assertEqual(after[key]["refresh_status"], "retained_after_failure")

    def test_canonical_difference_is_reported_without_forcing_totals(self):
        self.store.db.execute("INSERT INTO players VALUES('00123','Test Player')")
        self.store.db.execute("""INSERT INTO player_games(game_id,player_id,team,season,appeared,pts,fgm,fg3m)
            VALUES('0022600001','00123','NYK','2026-27',1,21,8,1)""")
        self.store.db.commit()
        by_url = {r["source_url"]: r for r in _requests("2026-27")}
        def fetch(url, config):
            spec = by_url[url]
            obj = official(spec)
            if spec["kind"] == "team_games":
                obj["resultSets"][0]["headers"].append("TEAM_ABBREVIATION")
                obj["resultSets"][0]["rowSet"][0].append("NYK")
                obj["resultSets"][0]["rowSet"][1].append("BOS")
            return obj
        with patch("nbabot.maintenance.fetch_json", side_effect=fetch):
            report = collect_context_reference(self.store)
        self.assertEqual(report["status"], "success_with_warnings")
        self.assertEqual(report["warnings"][0]["code"], "canonical_team_total_difference")
        self.assertEqual(self.store.one("SELECT pts FROM player_games")["pts"], 21)


if __name__ == "__main__":
    unittest.main()
