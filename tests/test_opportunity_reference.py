import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nbabot.core import Store,load_config
from nbabot.opportunity_reference import (_parse_box,_parse_passing,
    collect_opportunity_reference,load_opportunity_reference)


GAME={"game_id":"0022600001","date_et":"2026-10-20","season":"2026-27","season_type":"Regular Season","opponent":"ORL"}
URL="https://stats.nba.com/stats/fixture"
AT="2026-10-26T20:00:00+00:00"


def person(pid,position="G"):
    return {"personId":pid,"firstName":"Fixture","familyName":pid,"position":position,"comment":"",
        "statistics":{"minutes":"20:30","points":2,"reboundsTotal":0,"assists":0,
            "fieldGoalsMade":1,"fieldGoalsAttempted":1,"threePointersMade":0,"threePointersAttempted":0,
            "freeThrowsMade":0,"freeThrowsAttempted":0,"touches":0,"reboundChancesTotal":None}}


def box(kind="starts"):
    key="boxScoreTraditional" if kind=="starts" else "boxScorePlayerTrack"
    return {"meta":{"time":AT},key:{"gameId":GAME["game_id"],
        "homeTeam":{"teamTricode":"NYK","players":[person(p) for p in ("001","004","005","006","007")]+[person("008","")]},
        "awayTeam":{"teamTricode":"ORL","players":[person(p) for p in ("002","003","010","011","012")]}}}


def passing():
    return {"resultSets":[{"headers":["PLAYER_ID","PLAYER_NAME","TEAM_ID","TEAM_ABBREVIATION","GP","MIN","AST","POTENTIAL_AST","TOUCHES"],
        "rowSet":[["001","Fixture001",1610612738,"BOS",1,20.5,0,None,0],
                  ["002","Fixture002",1610612753,"ORL",1,20.5,0,0,0],
                  ["003","Unselected",1610612753,"ORL",1,20.5,0,10,4]]}]}


class OpportunityReference(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        config=load_config(Path(self.temp.name)/"none")
        config.update(data_dir=str(Path(self.temp.name)/"data"),organized_players=["001"])
        self.store=Store(config)
        for pid,team,opp in [("001","NYK","ORL"),("002","ORL","NYK")]:
            self.store.db.execute("INSERT INTO players VALUES(?,?)",(pid,"Fixture"+pid))
            self.store.db.execute("""INSERT INTO player_games(game_id,player_id,team,opponent,date_et,season,
                appeared,minutes,pts,reb,ast,fgm,fga,fg3m,fg3a,ftm,fta)
                VALUES(?,?,?,?,?,?,1,20.5,2,0,0,1,1,0,0,0,0)""",(GAME["game_id"],pid,team,opp,GAME["date_et"],GAME["season"]))
        self.store.db.commit()
        self.canonical={(r["game_id"],r["player_id"]):r for r in self.store.rows("SELECT * FROM player_games")}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_box_scope_starts_minutes_missing_zero_and_leading_ids(self):
        rows,checks=_parse_box(box(),"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)
        self.assertEqual({r["player_id"] for r in rows},{"001","002"})
        self.assertEqual(rows[0]["game_id"],"0022600001")
        self.assertEqual(rows[0]["source_minutes"],"20:30")
        self.assertEqual(rows[0]["minutes"],20.5)
        self.assertEqual(rows[0]["started"],1)
        self.assertEqual(rows[0]["role_validation_status"],"verified_official_game_identity_and_five_starters_per_team")
        self.assertEqual(checks[0]["starter_counts"],{"NYK":5,"ORL":5})
        tracking,_=_parse_box(box("tracking"),"tracking",GAME,{"001"},{"002"},self.canonical,URL,AT)
        self.assertEqual(tracking[0]["touches"],0)
        self.assertIsNone(tracking[0]["reboundChancesTotal"])

    def test_wrong_game_identity_and_invalid_start_counts_are_rejected(self):
        obj=box()
        obj["boxScoreTraditional"]["gameId"]="0022600099"
        with self.assertRaisesRegex(ValueError,"different game"):
            _parse_box(obj,"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)
        obj=box()
        obj["boxScoreTraditional"]["homeTeam"]["players"][0]["position"]=""
        with self.assertRaisesRegex(ValueError,"five"):
            _parse_box(obj,"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)

    def test_dnp_does_not_become_played_zero_and_corrections_remain_flags(self):
        obj=box()
        player=obj["boxScoreTraditional"]["homeTeam"]["players"][0]
        player["position"]=""
        obj["boxScoreTraditional"]["homeTeam"]["players"][-1]["position"]="G"
        player["comment"]="DNP - Coach's Decision"
        player["statistics"]["minutes"]=""
        rows,_=_parse_box(obj,"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)
        row=next(r for r in rows if r["player_id"]=="001")
        self.assertEqual(row["appeared"],0)
        self.assertIsNone(row["pts"])
        self.assertIsNone(row["started"])
        self.assertIn("source_appearance_difference",row["flags"])
        obj=box()
        obj["boxScoreTraditional"]["homeTeam"]["players"][0]["statistics"]["points"]=3
        rows,_=_parse_box(obj,"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)
        self.assertEqual(rows[0]["validation_status"],"source_conflict")
        self.assertEqual(self.canonical[(GAME["game_id"],"001")]["pts"],2)

    def test_noncanonical_published_second_clock_is_preserved_and_flagged(self):
        obj=box()
        obj["boxScoreTraditional"]["homeTeam"]["players"][0]["statistics"]["minutes"]="19:60"
        rows,_=_parse_box(obj,"starts",GAME,{"001"},{"002"},self.canonical,URL,AT)
        row=rows[0]
        self.assertEqual(row["source_minutes"],"19:60")
        self.assertEqual(row["minutes"],20)
        self.assertIn("published_noncanonical_minute_clock_60_seconds",row["flags"])
        self.assertIn("source_minute_difference",row["flags"])
        self.assertEqual(self.canonical[(GAME["game_id"],"001")]["minutes"],20.5)

    def test_passing_join_preserves_different_source_team_label(self):
        rows,_=_parse_passing(passing(),[GAME],{"001"},{"ORL":{"002"}},self.canonical,URL,AT)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["team"],"NYK")
        self.assertEqual(rows[0]["rawSource_TEAM_ABBREVIATION"],"BOS")
        self.assertTrue(rows[0]["SOURCE_TEAM_LABEL_DIFFERS"])
        self.assertIsNone(rows[0]["POTENTIAL_AST"])
        self.assertEqual(rows[1]["POTENTIAL_AST"],0)
        bad=passing()
        bad["resultSets"][0]["rowSet"][0][4]=2
        with self.assertRaisesRegex(ValueError,"one game"):
            _parse_passing(bad,[GAME],{"001"},{"ORL":{"002"}},self.canonical,URL,AT)
        bad=passing()
        bad["resultSets"][0]["rowSet"][0][6]=9
        with self.assertRaisesRegex(ValueError,"assists"):
            _parse_passing(bad,[GAME],{"001"},{"ORL":{"002"}},self.canonical,URL,AT)
        bad=passing()
        bad["parameters"]={"DateFrom":"10/21/2026","DateTo":"10/21/2026"}
        with self.assertRaisesRegex(ValueError,"different requested date"):
            _parse_passing(bad,[GAME],{"001"},{"ORL":{"002"}},self.canonical,URL,AT)
        bad=passing()
        bad["parameters"]={"SeasonType":"Playoffs"}
        with self.assertRaisesRegex(ValueError,"different requested scope"):
            _parse_passing(bad,[GAME],{"001"},{"ORL":{"002"}},self.canonical,URL,AT)

    def test_cache_reruns_failed_sources_and_unsafe_paths(self):
        def fetch(url,config):
            return box("tracking") if "playertrack" in url else box() if "traditional" in url else passing()
        with patch("nbabot.maintenance.fetch_json",side_effect=fetch) as request:
            first=collect_opportunity_reference(self.store,games=[GAME],cohort_by_team={"ORL":{"002"}})
            self.assertEqual(first["status"],"success")
            again=collect_opportunity_reference(self.store,games=[GAME],cohort_by_team={"ORL":{"002"}})
            self.assertEqual(again["coverage"]["cached_sources"],3)
            self.assertEqual(request.call_count,3)
        with patch("nbabot.maintenance.fetch_json",side_effect=TimeoutError("fixture")):
            failed=collect_opportunity_reference(self.store,games=[GAME],cohort_by_team={"ORL":{"002"}},force=True)
        self.assertEqual(failed["status"],"failed")
        loaded=load_opportunity_reference(self.store)
        self.assertEqual(len(loaded["tracking_games"]),2)
        self.assertEqual(len(loaded["starts"]),2)
        self.assertEqual(len(loaded["passing_games"]),2)
        self.assertEqual(loaded["starts"][0]["refresh_status"],"retained_after_failure")
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games")["n"],2)
        index_path=self.store.root/"reference/opportunities/latest.json"
        index=json.loads(index_path.read_text())
        index["tables"][0]["path"]="../nba.sqlite3"
        index_path.write_text(json.dumps(index))
        bad=load_opportunity_reference(self.store)
        self.assertTrue(any(r.get("source")=="cache_integrity" for r in bad["errors"]))

    def test_targeted_trade_history_starts_preserve_actual_old_team(self):
        self.store.db.execute("UPDATE player_games SET team='NOP',opponent='PHI',started=NULL WHERE player_id='001'")
        self.store.db.commit()
        obj=box()
        obj["boxScoreTraditional"]["homeTeam"]["teamTricode"]="NOP"
        obj["boxScoreTraditional"]["awayTeam"]["teamTricode"]="PHI"
        with patch("nbabot.maintenance.fetch_json",return_value=obj) as request:
            result=collect_opportunity_reference(self.store,games=[],include_passing=False)
        self.assertEqual(result["status"],"success")
        self.assertEqual(result["coverage"]["requested_selected_role_only_games"],1)
        self.assertEqual(request.call_count,1)
        rows=load_opportunity_reference(self.store)["starts"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["player_id"],"001")
        self.assertEqual(rows[0]["team"],"NOP")
        self.assertEqual(rows[0]["opponent"],"PHI")
        self.assertEqual(rows[0]["started"],1)
        self.assertIsNone(self.store.one("SELECT started FROM player_games WHERE player_id='001'")["started"])

    def test_verified_career_rows_support_postseason_passing_without_canonical_game(self):
        game={**GAME,"game_id":"0042600001","season_type":"Playoffs"}
        career={**self.canonical[(GAME["game_id"],"001")],"game_id":game["game_id"],"season_type":"Playoffs"}
        def fetch(url,config):
            if "leaguedashptstats" in url:
                return passing()
            obj=box("tracking" if "playertrack" in url else "starts")
            key="boxScorePlayerTrack" if "playertrack" in url else "boxScoreTraditional"
            obj[key]["gameId"]=game["game_id"]
            return obj
        with patch("nbabot.career_reference.load_career_reference",return_value={"rows":[career]}):
            with patch("nbabot.maintenance.fetch_json",side_effect=fetch):
                result=collect_opportunity_reference(self.store,games=[game],cohort_by_team={"ORL":{"002"}},include_other_team_starts=False)
        self.assertEqual(result["status"],"success")
        rows=load_opportunity_reference(self.store)["passing_games"]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["game_id"],"0042600001")
        self.assertEqual(rows[0]["season_type"],"Playoffs")
        opponent=next(r for r in rows if r["player_id"]=="002")
        self.assertEqual(opponent["team"],"ORL")
        self.assertEqual(self.store.one("SELECT count(*) n FROM player_games WHERE game_id='0042600001'")["n"],0)


if __name__=="__main__":
    unittest.main()
