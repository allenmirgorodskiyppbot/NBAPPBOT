import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlparse

from nbabot.core import Store,load_config
from nbabot.matchup_reference import (collect_matchup_reference,load_matchup_reference,
    matchup_scope,normalize_matchups,normalize_shots)


class MatchupReference(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        config=load_config(Path(self.tmp.name)/"none")
        config.update(data_dir=str(Path(self.tmp.name)/"data"),organized_players=["001","002"])
        self.store=Store(config)
        for pid,name in [("001","Selected One"),("002","Selected Two"),("003","Extra Knicks"),
                         ("101","Named One"),("102","Named Two"),("199","Whole Roster Extra")]:
            self.store.db.execute("INSERT INTO players VALUES(?,?)",(pid,name))
        self.store.db.execute("INSERT INTO teams VALUES('1610612752','NYK')")
        self.store.db.execute("INSERT INTO teams VALUES('1610612738','BOS')")
        for i,(knicks,opponent) in enumerate([("Selected One","Named One"),("Selected Two","Named Two")]):
            self.store.db.execute("INSERT INTO raw_rows VALUES('fixture','Matchups',?,?)",(i,json.dumps({
                "Knicks player":knicks,"Opponent":"BOS","Suspected defender":opponent})))
        self.store.db.commit()
        self.scope=matchup_scope(self.store)
        self.game={"game_id":"0022500001","date_et":"2025-10-22","season":"2025-26",
                   "season_type":"Regular Season","opponent_team":"BOS"}
        self.teams={"1610612752":"NYK","1610612738":"BOS"}
        self.url="https://stats.nba.com/stats/test"
        self.at="2026-10-26T12:00:00+00:00"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def matchups(self,points=0):
        def person(pid,matchups):
            return {"personId":pid,"firstName":"Name","familyName":pid,"matchups":[
                {"personId":other,"firstName":"Other","familyName":other,"statistics":{
                    "partialPossessions":0.4,"matchupMinutes":"0:03.5","playerPoints":points,
                    "matchupPotentialAssists":None,"matchupFieldGoalsMade":0,"matchupFieldGoalsAttempted":0}}
                for other in matchups]}
        return {"meta":{"time":self.at},"boxScoreMatchups":{"gameId":self.game["game_id"],
            "homeTeam":{"teamTricode":"NYK","players":[person("001",["101","102","199"]),person("002",["102"])]},
            "awayTeam":{"teamTricode":"BOS","players":[person("101",["001"]),person("102",["001","002"])]}}}

    def shots(self):
        headers=["GAME_ID","GAME_EVENT_ID","PLAYER_ID","PLAYER_NAME","TEAM_ID","GAME_DATE","SHOT_ATTEMPTED_FLAG","SHOT_MADE_FLAG","SHOT_DISTANCE"]
        rows=[]
        for event,pid,team in [("00007","001",1610612752),(8,"002",1610612752),(9,"003",1610612752),
                               (10,"101",1610612738),(11,"102",1610612738),(12,"199",1610612738)]:
            rows.append([self.game["game_id"],event,pid,pid,team,"20251022",1,0,None])
        return {"resultSets":[{"name":"Shot_Chart_Detail","headers":headers,"rowSet":rows}]}

    def test_only_exact_primary_alternate_pairs_in_both_directions(self):
        rows=normalize_matchups(self.matchups(),self.game,self.scope,self.url,self.at)
        self.assertEqual(len(rows),4)
        self.assertEqual({(r["offensive_player_id"],r["defender_player_id"]) for r in rows},
                         {("001","101"),("002","102"),("101","001"),("102","002")})
        self.assertEqual({r["knicks_side"] for r in rows},{"offense","defense"})
        self.assertTrue(all(r["partialPossessions"]==0.4 for r in rows))
        self.assertIsNone(rows[0]["matchupPotentialAssists"])
        self.assertEqual(rows[0]["playerPoints"],0)
        self.assertEqual(rows[0]["matchupMinutes"],"0:03.5")
        self.assertIn("not whole-game",rows[0]["units"])
        self.assertNotIn("rebounds",rows[0])

    def test_duplicate_pairs_bad_game_and_bad_totals_are_rejected(self):
        obj=self.matchups()
        obj["boxScoreMatchups"]["homeTeam"]["players"][0]["matchups"].append(copy.deepcopy(obj["boxScoreMatchups"]["homeTeam"]["players"][0]["matchups"][0]))
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            normalize_matchups(obj,self.game,self.scope,self.url,self.at)
        obj=self.matchups()
        obj["boxScoreMatchups"]["gameId"]="0022509999"
        with self.assertRaisesRegex(ValueError,"different game"):
            normalize_matchups(obj,self.game,self.scope,self.url,self.at)
        obj=self.matchups()
        obj["boxScoreMatchups"]["homeTeam"]["players"][0]["matchups"][0]["statistics"]["matchupFieldGoalsMade"]=2
        with self.assertRaisesRegex(ValueError,"exceed"):
            normalize_matchups(obj,self.game,self.scope,self.url,self.at)

    def test_shots_scope_text_keys_and_missing_are_preserved(self):
        rows=normalize_shots(self.shots(),self.game,self.scope,self.url,self.at,self.teams)
        self.assertEqual({r["player_id"] for r in rows},{"001","002","101","102"})
        self.assertEqual(rows[0]["game_event_id"],"00007")
        self.assertEqual(rows[0]["game_id"],"0022500001")
        self.assertIsNone(rows[0]["SHOT_DISTANCE"])
        self.assertEqual(rows[0]["SHOT_MADE_FLAG"],0)
        self.assertNotIn("defender_distance",rows[0])
        bad=self.shots()
        bad["resultSets"][0]["rowSet"].append(bad["resultSets"][0]["rowSet"][0])
        with self.assertRaisesRegex(ValueError,"Duplicate"):
            normalize_shots(bad,self.game,self.scope,self.url,self.at,self.teams)

    def test_cached_rerun_failure_retention_and_correction_versions(self):
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch) as call:
            first=collect_matchup_reference(self.store,games=[self.game])
            self.assertEqual(call.call_count,2)
            second=collect_matchup_reference(self.store,games=[self.game])
            self.assertEqual(call.call_count,2)
        self.assertEqual(first["status"],"success")
        self.assertEqual(second["cached_captures"],2)
        before=load_matchup_reference(self.store)
        self.assertEqual(len(before["matchups"]),4)
        self.assertEqual(len(before["shots"]),4)
        with patch("nbabot.maintenance.fetch_json",side_effect=TimeoutError("fixture timeout")):
            failed=collect_matchup_reference(self.store,games=[self.game],force=True)
        after=load_matchup_reference(self.store)
        self.assertEqual(failed["status"],"partial")
        self.assertEqual(after["matchups"],before["matchups"])
        self.assertEqual({r["refresh_status"] for r in after["coverage"]},{"retained_after_failure"})
        def corrected(url,config):
            return self.matchups(points=2) if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=corrected):
            collect_matchup_reference(self.store,games=[self.game],force=True)
        after=load_matchup_reference(self.store)
        self.assertTrue(all(r["playerPoints"]==2 for r in after["matchups"]))
        self.assertEqual(len(list((self.store.root/"reference/matchups/captures").glob("*.json"))),4)
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"],0)

    def test_changed_scope_cannot_retain_old_all_roster_rows(self):
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch):
            collect_matchup_reference(self.store,games=[self.game])
        self.store.config["organized_players"].append("003")
        with patch("nbabot.maintenance.fetch_json",side_effect=TimeoutError("fixture timeout")):
            failed=collect_matchup_reference(self.store,games=[self.game])
        self.assertEqual(failed["status"],"failed")
        self.assertEqual(load_matchup_reference(self.store)["shots"],[])

    def test_corrupt_capture_cannot_be_loaded(self):
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch):
            collect_matchup_reference(self.store,games=[self.game])
        root=self.store.root/"reference/matchups"
        manifest=json.loads((root/"latest.json").read_text())
        (root/manifest["captures"][0]["path"]).write_text("{}")
        loaded=load_matchup_reference(self.store)
        self.assertTrue(any(e.get("source")=="cache_integrity" for e in loaded["errors"]))

    def test_missing_shot_events_cannot_replace_a_reconciled_capture(self):
        self.store.db.execute("""INSERT INTO player_games(game_id,player_id,team,opponent,appeared,fga,fgm,fg3a,fg3m)
            VALUES(?,?,'NYK','BOS',1,1,0,0,0)""",(self.game["game_id"],"001"))
        self.store.db.commit()
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch):
            first=collect_matchup_reference(self.store,games=[self.game])
        self.assertEqual(first["shot_reconciliation"]["matched_players"],1)
        def missing(url,config):
            obj=fetch(url,config)
            if "shotchart" in url:
                obj["resultSets"][0]["rowSet"]=[r for r in obj["resultSets"][0]["rowSet"] if r[2]!="001"]
            return obj
        with patch("nbabot.maintenance.fetch_json",side_effect=missing):
            result=collect_matchup_reference(self.store,games=[self.game],force=True)
        self.assertEqual(result["status"],"partial")
        self.assertTrue(any("differ from canonical" in x["error"] for x in result["errors"]))
        self.assertIn("001",{r["player_id"] for r in load_matchup_reference(self.store)["shots"]})
        self.assertEqual(len(list((self.store.root/"reference/matchups/quarantine").glob("*.json"))),1)

    def test_controlled_career_boxes_also_validate_missing_canonical_games(self):
        controls={"rows":[{"game_id":self.game["game_id"],"player_id":"001","team":"NYK",
            "appeared":1,"fga":2,"fgm":0,"fg3a":0,"fg3m":0}],"errors":[]}
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch), \
                patch("nbabot.career_reference.load_career_reference",return_value=controls):
            result=collect_matchup_reference(self.store,games=[self.game])
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["shot_rows"],0)
        self.assertTrue(any("differ from canonical" in e["error"] for e in result["errors"]))
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"],0)

    def test_verified_official_boxes_validate_opponents_and_conflicts_remain_flagged(self):
        box={"game_id":self.game["game_id"],"player_id":"101","team":"BOS","appeared":1,
            "fga":1,"fgm":0,"fg3a":0,"fg3m":0,"validation_status":"source_game_identity_only"}
        def fetch(url,config):
            return self.matchups() if "boxscorematchups" in url else self.shots()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch), \
                patch("nbabot.opportunity_reference.load_opportunity_reference",return_value={"starts":[box],"errors":[]}):
            result=collect_matchup_reference(self.store,games=[self.game])
        self.assertEqual(result["shot_reconciliation"]["matched_players"],1)
        conflict={**box,"fga":99,"validation_status":"source_conflict","flags":["source_correction_or_conflict_fga"]}
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch), \
                patch("nbabot.opportunity_reference.load_opportunity_reference",return_value={"starts":[conflict],"errors":[]}):
            result=collect_matchup_reference(self.store,games=[self.game],force=True)
        self.assertEqual(result["status"],"success")
        self.assertEqual(result["shot_reconciliation"]["matched_players"],0)
        self.assertEqual(len(result["opportunity_validation_conflicts"]),1)
        self.assertEqual(result["shot_rows"],4)


if __name__=="__main__":
    unittest.main()
