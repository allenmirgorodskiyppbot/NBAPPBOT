import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from nbabot.core import Store, load_config, now
from nbabot.maintenance import export_package
from nbabot.maintenance import refresh
from nbabot.scheduler import heartbeat_file, status
from nbabot.web import create_app


class DeploymentIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config=load_config(Path(self.tmp.name)/"none")
        self.config["data_dir"]=str(Path(self.tmp.name)/"data")
        self.store=Store(self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_optional_dashboard_auth_covers_data_and_downloads(self):
        export_package(self.store)
        with patch.dict(os.environ,{"NBABOT_DASHBOARD_USER":"owner","NBABOT_DASHBOARD_PASSWORD":"fixture-password"}):
            with TestClient(create_app(self.config)) as client:
                self.assertEqual(client.get("/health").status_code,200)
                for path in ("/","/api/overview","/api/export/Master_Data.xlsx","/static/app.js"):
                    denied=client.get(path)
                    self.assertEqual(denied.status_code,401)
                    self.assertIn("Basic",denied.headers["www-authenticate"])
                    self.assertEqual(client.get(path,auth=("owner","fixture-password")).status_code,200)
                self.assertEqual(client.get("/",headers={"Authorization":"Basic !!!"}).status_code,401)
                self.assertEqual(client.get("/",auth=("owner","wrong")).status_code,401)

    def test_incomplete_auth_settings_fail_closed(self):
        with patch.dict(os.environ,{"NBABOT_DASHBOARD_USER":"owner","NBABOT_DASHBOARD_PASSWORD":""}):
            with self.assertRaises(ValueError):
                create_app(self.config)

    def test_akash_marker_does_not_claim_verified_lease(self):
        with patch.dict(os.environ,{"NBABOT_DEPLOYMENT_KIND":"akash"}):
            heartbeat_file(self.store)
        with patch("nbabot.scheduler.shutil.which",return_value=None):
            result=status(self.store)
        self.assertTrue(result["operating_in_this_instance"])
        self.assertEqual(result["deployment_kind"],"akash")
        self.assertEqual(result["external_lease_verification"],"not_checked")
        self.assertFalse(result["automatic_updates_persistently_active"])

    def test_exporter_failure_keeps_previous_verified_package(self):
        first=Path(export_package(self.store))
        latest=self.store.root/"packages/latest"
        self.config["organized_players"]=["not-a-player"]
        with patch("nbabot.organized.build_organized_workbook",side_effect=ValueError("Invalid selected cohort")):
            with self.assertRaises(ValueError):
                export_package(self.store)
        self.assertEqual(latest.resolve(),first)
        self.assertEqual(json.loads((first/"manifest.json").read_text())["organized_workbook"]["status"],"not_configured")

    def test_daily_refresh_builds_organized_package_and_preserves_fixed_cohort(self):
        at=now()
        played=(datetime.now(timezone.utc)-timedelta(days=2)).date().isoformat()
        tip=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        rows=[]
        rosters=[]
        for i in range(1,10):
            pid=f"00{i}"
            team,opponent=("NYK","BOS") if i<9 else ("BOS","NYK")
            rows.append({"GAME_ID":"0022600001","PLAYER_ID":pid,"PLAYER_NAME":f"Fixture {i}",
                "TEAM_ABBREVIATION":team,"Opponent":opponent,"Season":"2026-27","Game date":played,
                "Appeared":"Yes","MIN":34.098333333333336,"PTS":21,"REB":0,"AST":None,
                "FG3M":1,"FG3A":3,"FGM":6,"FGA":19,"FTM":8,"FTA":8})
            rosters.append({"player_id":pid,"name":f"Fixture {i}","team":team,"observed_at":at})
        records={"stats":rows,"rosters":rosters,"injuries":[],"lineups":[],"news":[],
            "schedule":[{"id":"0022600099","date_et":tip[:10],"tipoff":tip,"home":"NYK","away":"BOS","status":"scheduled"}]}
        sections={k:{"complete":True,"verified_at":at,"coverage_cutoff":at,"source_url":"https://example.test/fixture","records":v} for k,v in records.items()}
        feed=Path(self.tmp.name)/"fixture.json"
        feed.write_text(json.dumps({"sections":sections}))
        self.config.update(nba_enabled=False,public_context_enabled=False,provider_file=str(feed),simulations=100,
            organized_players=[f"00{i}" for i in range(1,8)])
        first=refresh(self.store)
        self.assertEqual(first["status"],"success",first)
        latest=self.store.root/"packages/latest"
        self.assertTrue((latest/"Knicks_Historical_Organized.xlsx").is_file())
        manifest=json.loads((latest/"manifest.json").read_text())
        self.assertEqual(len(manifest["organized_workbook"]["sheets"]),52)
        self.assertTrue(any(p.startswith("organized_csv/") for p in manifest["sha256"]))
        projected={r["player_id"] for r in self.store.rows("SELECT player_id FROM projections WHERE status='active'")}
        self.assertNotIn("008",projected)
        self.assertIn("009",projected)
        original=latest.resolve()
        sections["stats"]["records"][0]["PTS"]=999
        feed.write_text(json.dumps({"sections":sections}))
        failed=refresh(self.store)
        self.assertEqual(failed["status"],"failed")
        self.assertEqual(latest.resolve(),original)
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"],9)


if __name__=="__main__":
    unittest.main()
