import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nbabot.core import Store, load_config
from nbabot.career_reference import (_controls, _games, _reconcile,
    collect_career_reference, load_career_reference)


def games(*, phase="Regular Season", duplicate=False, clock="34:06"):
    row={"SEASON_YEAR":"2024-25","PLAYER_ID":"00123","PLAYER_NAME":"Fixture Guard",
         "TEAM_ID":"001","TEAM_ABBREVIATION":"NYK","GAME_ID":"0022400001" if phase=="Regular Season" else "0042400001",
         "GAME_DATE":"2024-10-23T00:00:00","MATCHUP":"NYK vs. BOS","MIN":34.098333333333336,
         "MIN_SEC":clock,"PTS":21,"REB":0,"OREB":0,"DREB":0,"AST":None,
         "FGM":8,"FGA":16,"FG3M":1,"FG3A":4,"FTM":4,"FTA":6,
         "STL":1,"BLK":0,"TOV":2,"PF":1,"PLUS_MINUS":0}
    return {"parameters":{"SeasonYear":"2024-25","SeasonType":phase},
            "resultSets":[{"name":"PlayerGameLogs","headers":list(row),"rowSet":[list(row.values())]*(2 if duplicate else 1)}]}


def controls():
    row={"PLAYER_ID":"00123","SEASON_ID":"2024-25","TEAM_ID":"001","TEAM_ABBREVIATION":"NYK",
         "GP":1,"GS":1,"MIN":34,"PTS":21,"REB":0,"OREB":0,"DREB":0,"AST":None,
         "FGM":8,"FGA":16,"FG3M":1,"FG3A":4,"FTM":4,"FTA":6,"STL":1,"BLK":0,"TOV":2,"PF":1}
    return {"resultSets":[{"name":"SeasonTotalsRegularSeason","headers":list(row),"rowSet":[list(row.values())]},
                          {"name":"SeasonTotalsPostSeason","headers":list(row),"rowSet":[]}]}


class CareerReference(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        c=load_config(Path(self.tmp.name)/"none")
        c["data_dir"]=str(Path(self.tmp.name)/"data")
        self.store=Store(c)
        self.player={"id":"00123","name":"Fixture Guard"}
        self.store.db.execute("INSERT INTO players VALUES(?,?)",tuple(self.player.values()))
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_exact_clock_original_numeric_null_zero_and_ids(self):
        rows=_games(games(),self.player,"2024-25","Regular Season","https://example.test/games","2026-10-25T00:00:00+00:00")
        r=rows[0]
        self.assertEqual(r["game_id"],"0022400001")
        self.assertEqual(r["player_id"],"00123")
        self.assertEqual(r["team_id"],"001")
        self.assertEqual(r["minutes"],34.1)
        self.assertEqual(r["min_seconds"],2046)
        self.assertEqual(r["source_minutes_exact_text"],"34.098333333333336")
        self.assertEqual(r["source_minute_clock"],"34:06")
        self.assertIsNone(r["ast"])
        self.assertEqual(r["reb"],0)
        self.assertIsNone(r["started"])

    def test_wrong_player_phase_duplicate_and_team_rejected(self):
        for mode in ("player","phase","duplicate","team"):
            obj=games(duplicate=mode=="duplicate")
            headers=obj["resultSets"][0]["headers"]
            if mode=="player":obj["resultSets"][0]["rowSet"][0][headers.index("PLAYER_ID")]="999"
            if mode=="phase":obj["parameters"]["SeasonType"]="Playoffs"
            if mode=="team":obj["resultSets"][0]["rowSet"][0][headers.index("TEAM_ABBREVIATION")]="BOS"
            with self.assertRaises(ValueError):
                _games(obj,self.player,"2024-25","Regular Season","https://example.test/games","2026-10-25T00:00:00+00:00")

    def test_postseason_stays_separate_and_missing_clock_not_invented(self):
        r=_games(games(phase="Playoffs",clock=None),self.player,"2024-25","Playoffs","https://example.test/games","2026-10-25T00:00:00+00:00")[0]
        self.assertEqual(r["season_type"],"Playoffs")
        self.assertEqual(r["game_id"],"0042400001")
        self.assertEqual(r["minutes"],34.098333333333336)
        self.assertIsNone(r["source_minute_clock"])
        self.assertIn("unavailable",r["source_minute_precision"])

    def test_control_totals_do_not_double_count_traded_team_subtotals(self):
        obj=controls();table=obj["resultSets"][0];h=table["headers"]
        a=table["rowSet"][0]
        b=list(a);b[h.index("TEAM_ID")]="002";b[h.index("TEAM_ABBREVIATION")]="BOS"
        total=list(a);total[h.index("TEAM_ID")]=0;total[h.index("TEAM_ABBREVIATION")]="TOT"
        for f in ("GP","GS","MIN","PTS","FGM","FGA","FG3M","FG3A","FTM","FTA","STL","TOV","PF"):
            total[h.index(f)]*=2
        table["rowSet"]=[a,b,total]
        c=_controls(obj,self.player,"https://example.test/controls","2026-10-25T00:00:00+00:00")[0]
        self.assertEqual(c["gp"],2)
        self.assertEqual(c["fga"],32)
        self.assertEqual(c["total_actual_teams"],["BOS","NYK"])

    def test_reconciliation_keeps_rounded_minute_difference_and_missing(self):
        rows=_games(games(),self.player,"2024-25","Regular Season","https://example.test/games","2026-10-25T00:00:00+00:00")
        control=_controls(controls(),self.player,"https://example.test/control","2026-10-25T00:00:00+00:00")[0]
        checked=_reconcile(rows,control)
        self.assertEqual(checked["status"],"passed_with_missing_fields")
        self.assertAlmostEqual(checked["minute_difference"],.1)
        self.assertIn("ast",checked["missing_count_controls"])
        self.assertEqual(rows[0]["minutes"],34.1)
        control["fga"]=17
        self.assertEqual(_reconcile(rows,control)["status"],"failed")

    def test_collect_reuses_history_and_failure_retains_immutable_captures(self):
        def fetch(url,config):return controls() if "playercareerstats" in url else games()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch) as call:
            report=collect_career_reference(self.store,["00123"])
        self.assertEqual(report["status"],"success")
        self.assertEqual(call.call_count,2)
        before=load_career_reference(self.store)
        self.assertEqual(len(before["rows"]),1)
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"],0)
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch) as call:
            collect_career_reference(self.store,["00123"])
        self.assertEqual(call.call_count,1)
        with patch("nbabot.maintenance.fetch_json",side_effect=TimeoutError("fixture")):
            failed=collect_career_reference(self.store,["00123"])
        self.assertEqual(failed["status"],"failed")
        after=load_career_reference(self.store)
        self.assertEqual(before["rows"],after["rows"])
        self.assertEqual(after["tables"][0]["refresh_status"],"retained_after_failure")

    def test_cache_path_and_hash_are_checked(self):
        with patch("nbabot.maintenance.fetch_json",side_effect=lambda url,c:controls() if "playercareerstats" in url else games()):
            collect_career_reference(self.store,["00123"])
        root=self.store.root/"reference/career"
        index=json.loads((root/"latest.json").read_text())
        index["entries"][1]["path"]="../nba.sqlite3"
        (root/"latest.json").write_text(json.dumps(index))
        loaded=load_career_reference(self.store)
        self.assertTrue(loaded["errors"])
        self.assertEqual(loaded["rows"],[])

    def test_conflicting_source_is_archived_but_not_promoted(self):
        def fetch(url,config):
            if "playercareerstats" not in url:return games()
            obj=controls();table=obj['resultSets'][0]
            table['rowSet'][0][table['headers'].index('FGA')]=17
            return obj
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch):
            report=collect_career_reference(self.store,["00123"])
        self.assertEqual(report["status"],"failed")
        self.assertEqual(load_career_reference(self.store)["rows"],[])
        self.assertEqual(len(report["quarantined_entries"]),1)
        path=self.store.root/'reference/career'/report['quarantined_entries'][0]['path']
        raw=json.loads(path.read_text())
        self.assertEqual(raw['checks']['status'],'failed')
        self.assertIn('raw_source_response',raw)


if __name__=="__main__":
    unittest.main()
