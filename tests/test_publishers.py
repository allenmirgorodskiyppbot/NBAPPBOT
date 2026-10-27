import json
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from nbabot.core import now,stamp
from nbabot.maintenance import apply_sections,apply_control_updates
from nbabot.public_sources import (NBA_TEAMS,collect_context,exact_identity,nba_roster_rows,parse_injuries,
                                   parse_lineup,parse_news,roster_crosswalk)
import test_pipeline as fixtures


class PublisherParsers(unittest.TestCase):
    def setUp(self):
        self.at=now()
        self.tip=stamp(datetime.now(timezone.utc)+timedelta(days=1))
        self.game={"id":"0022600001","date_et":self.tip[:10],"tipoff":self.tip,"home":"BOS","away":"NYK","status":"scheduled"}
        self.rosters=[{"player_id":"00123","name":"Test Guard","team":"NYK","source_url":"https://stats.nba.com/roster"}]

    def test_ambiguous_identity_and_wrong_team_are_not_joined(self):
        self.assertEqual(exact_identity("Test Guard","BOS",self.rosters),None)
        self.assertEqual(exact_identity("Guard","NYK",self.rosters),None)
        self.assertEqual(exact_identity("Test Guard","NYK",self.rosters+self.rosters),None)
        mapping,missing=roster_crosswalk({"athletes":[{"id":"999","displayName":"Test Guard"}]},"NYK",self.rosters)
        self.assertEqual(mapping,{"999":"00123"}); self.assertFalse(missing)
        self.assertIn("identity_evidence",self.rosters[0])

    def test_injury_listing_never_proves_health_or_future_absence(self):
        data={"injuries":[{"id":"18","injuries":[{"athlete":{"id":"999","displayName":"Test Guard"},"status":"Out",
            "date":stamp(datetime.now(timezone.utc)-timedelta(hours=1))}]}]}
        records,missing=parse_injuries(data,[self.game],self.rosters,{"999":"00123"},self.at)
        self.assertFalse(missing); self.assertEqual(records[0]["status"],"reported_out")
        self.assertFalse(records[0]["game_specific_confirmed"])
        self.assertEqual(parse_injuries({"injuries":[]},[self.game],self.rosters,{},self.at)[0],[])
        data["injuries"][0]["injuries"][0]["status"]="Active"
        self.assertEqual(parse_injuries(data,[self.game],self.rosters,{},self.at)[0][0]["status"],"unknown")

    def test_news_provenance_and_speculation_are_preserved(self):
        article={"id":"news-1","published":stamp(datetime.now(timezone.utc)-timedelta(hours=1)),"headline":"Possible role change",
            "categories":[{"type":"athlete","athleteId":"999"},{"type":"athlete","athleteId":"888"}],
            "links":{"web":{"href":"https://www.espn.com/nba/story/test"}}}
        r=parse_news({"articles":[article]},"NYK",{"999":"00123"},self.at)[0]
        self.assertEqual(r["confirmation"],"unconfirmed"); self.assertEqual(r["players"],["00123"])
        self.assertEqual(r["unresolved_espn_player_ids"],["888"])
        self.assertTrue(r["retrieved_at"]); self.assertTrue(r["published_at"]); self.assertTrue(r["source_url"])

    def test_box_starters_not_backfilled_as_confirmed_pregame_lineup(self):
        obj={"header":{"id":"401000001","competitions":[{"date":self.tip,"competitors":[
            {"team":{"abbreviation":"NY"}},{"team":{"abbreviation":"BOS"}}]}]},
            "boxscore":{"players":[{"starter":True}]}}
        records=parse_lineup(obj,self.game,self.at)
        self.assertEqual(len(records),2); self.assertEqual(records[0]["confirmation"],"unknown")
        obj["header"]["competitions"][0]["competitors"][1]["team"]["abbreviation"]="LAL"
        with self.assertRaises(ValueError): parse_lineup(obj,self.game,self.at)

    def test_official_roster_team_id_and_leading_zero_player_id(self):
        obj={"resultSets":[{"name":"CommonTeamRoster","headers":["PLAYER_ID","PLAYER","TeamID"],
             "rowSet":[["00123","Test Guard",NBA_TEAMS["NYK"]]]}]}
        rows=nba_roster_rows(obj,"NYK",self.at,"https://stats.nba.com/roster")
        self.assertEqual(rows[0]["player_id"],"00123")
        with self.assertRaises(ValueError): nba_roster_rows(obj,"BOS",self.at,"https://stats.nba.com/roster")


class PublisherIntegration(unittest.TestCase):
    def test_independent_official_controls_supersede_but_preserve_old_snapshot(self):
        p=fixtures.Pipeline(); p.setUp()
        try:
            p.insert(p.row(PTS=23,FGM=7))
            original={"Season":"2026-27","NBA player ID":"00123","GP":1,"PTS":21,"REB":5,"AST":3,"3PM":1,
                      "Minutes (source precision)":34.098333333333336}
            p.store.db.execute("INSERT INTO raw_rows VALUES('fixture','NBA Season Totals',1,?)",(json.dumps(original),)); p.store.db.commit()
            from nbabot.ingest import validate
            self.assertEqual(validate(p.store)["status"],"failed")
            current={**original,"PTS":23,"Through":"2026-01-01"}
            apply_control_updates(p.store,{"season_controls":[current],"verified_at":now(),"season_controls_source":"https://stats.nba.com/stats/leaguedashplayerstats"})
            p.store.db.commit()
            self.assertNotEqual(validate(p.store)["status"],"failed")
            archived=json.loads(p.store.one("SELECT payload FROM raw_rows WHERE sheet='NBA Season Totals'")["payload"])
            self.assertEqual(archived["PTS"],21)
            self.assertEqual(p.store.one("SELECT count(*) n FROM audit WHERE kind='official_control_revision'")["n"],1)
        finally:
            p.tearDown()
    def test_complete_roster_removes_a_departed_player(self):
        p=fixtures.Pipeline(); p.setUp()
        try:
            p.insert()
            p.store.snapshot("rosters","00123",p.old,"https://example.test/roster",{"player_id":"00123","name":"Old Player","team":"NYK","active":True})
            p.store.db.commit(); at=now()
            records={"stats":[p.row()],"schedule":[{"id":"0022600099","date_et":p.tip[:10],"tipoff":p.tip,"home":"BOS","away":"NYK","status":"scheduled"}],
                "rosters":[{"player_id":"999","name":"New Player","team":"NYK","observed_at":at}],"injuries":[],"lineups":[],"news":[]}
            sections={k:{"complete":True,"verified_at":at,"coverage_cutoff":at,"source_url":"https://example.test/feed","records":v} for k,v in records.items()}
            with p.store.transaction(): apply_sections(p.store,sections)
            from nbabot.model import roster_for
            self.assertEqual([r["player_id"] for r in roster_for(p.store,"NYK",now())[0]],["999"])
        finally:
            p.tearDown()

    def test_publisher_collection_with_conservative_lineup_and_source_failure(self):
        p=fixtures.Pipeline(); p.setUp()
        try:
            p.schedule()
            comp={"date":p.tip,"competitors":[{"team":{"abbreviation":"NY"}},{"team":{"abbreviation":"BOS"}}]}
            def responses(requests,config):
                result={}
                for k in requests:
                    if k.startswith("nba_roster:"):
                        team=k.split(":")[1]; name="Test Guard" if team=="NYK" else "Test Center"
                        result[k]={"resultSets":[{"name":"CommonTeamRoster","headers":["PLAYER_ID","PLAYER","TeamID"],
                            "rowSet":[["00123" if team=="NYK" else "00456",name,NBA_TEAMS[team]]]}]}
                    elif k.startswith("espn_roster:"):
                        team=k.split(":")[1]
                        result[k]={"athletes":[{"id":"999" if team=="NYK" else "888","displayName":"Test Guard" if team=="NYK" else "Test Center"}]}
                    elif k.startswith("espn_news:"): result[k]={"articles":[]}
                    elif k=="espn_injuries": result[k]={"injuries":[]}
                    elif k.startswith("espn_scoreboard:"): result[k]={"events":[{"id":"401000001","competitions":[comp]}]}
                    else: result[k]={"header":{"id":"401000001","competitions":[comp]}}
                return result,[]
            with patch("nbabot.public_sources.request_many",responses):
                sections,errors=collect_context(p.store,{})
            self.assertEqual(set(sections),{"rosters","injuries","lineups","news"}); self.assertEqual(errors,[])
            self.assertEqual(len(sections["rosters"]["records"]),2)
            self.assertEqual(sections["injuries"]["records"],[])
            self.assertTrue(all(r["confirmation"]=="unknown" for r in sections["lineups"]["records"]))
            with patch("nbabot.public_sources.request_many",return_value=({},[{"source":"fixture","error":"NetworkFailure"}])):
                sections,errors=collect_context(p.store,{})
            self.assertTrue(errors); self.assertNotIn("rosters",sections)
        finally:
            p.tearDown()
