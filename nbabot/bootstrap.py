"""Explicit, repeatable league-history bootstrap from the official NBA endpoints."""
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode
import fcntl
import json
import re

from .core import digest,dumps,now
from .ingest import normalize_game,upsert_player_game,validate
from .maintenance import apply_control_updates,export_package,fetch_json,nba_parameters


def fetch_season(config,season):
    if not re.fullmatch(r"20\d{2}-\d{2}",season):
        raise ValueError("Season must have YYYY-YY format")
    stats_url="https://stats.nba.com/stats/playergamelogs?"+urlencode(nba_parameters(season))
    control_url="https://stats.nba.com/stats/leaguedashplayerstats?"+urlencode(nba_parameters(season))
    raw_stats=fetch_json(stats_url,config); stats_at=now()
    raw_controls=fetch_json(control_url,config); controls_at=now()
    result=raw_stats["resultSets"][0]
    records=[]
    for values in result["rowSet"]:
        r=dict(zip(result["headers"],values))
        matchup=r.get("MATCHUP","")
        r.update({"Opponent":matchup.split()[-1] if matchup else None,"Season":season,
                  "Source ID":stats_url,"known_at":stats_at,"Appeared":"Yes"})
        records.append(r)
    if not records:
        raise ValueError("No official history returned for "+season)
    through=max(r["GAME_DATE"][:10] for r in records)
    result=raw_controls["resultSets"][0]; controls=[]
    for values in result["rowSet"]:
        c=dict(zip(result["headers"],values))
        controls.append({"Season":season,"NBA player ID":str(c["PLAYER_ID"]),"Player":c["PLAYER_NAME"],"GP":c["GP"],
            "Minutes (source precision)":c.get("MIN"),"PTS":c.get("PTS"),"REB":c.get("REB"),"AST":c.get("AST"),
            "3PM":c.get("FG3M"),"Through":through,"source_url":control_url,"raw":c})
    if not controls:
        raise ValueError("Independent official controls unavailable for "+season)
    return {"season":season,"verified_at":controls_at,"source_url":stats_url,"records":records,
        "season_controls":controls,"season_controls_source":control_url,
        "source_capture":{"stats":raw_stats,"controls":raw_controls,"stats_url":stats_url,"controls_url":control_url,
                          "stats_retrieved_at":stats_at,"controls_retrieved_at":controls_at}}


def bootstrap_history(store,seasons):
    with (store.root/".refresh.lock").open("w") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {"status":"busy"}
        sections=[]
        for season in seasons:
            section=fetch_season(store.config,season)
            payload=dumps(section["source_capture"]).encode(); h=digest(payload)
            original=store.root/"originals"/(h[:16]+"_NBA_"+season+".json")
            original.parent.mkdir(exist_ok=True)
            if not original.exists(): original.write_bytes(payload)
            section["archive"]=(h,original)
            sections.append(section)
        counts=Counter()
        with store.transaction():
            for section in sections:
                h,path=section["archive"]
                store.db.execute("INSERT OR IGNORE INTO files VALUES(?,?,?,?,?)",(h,path.name,str(path),now(),
                    dumps({"official_history":{"season":section["season"],"rows":len(section["records"]),"controls":len(section["season_controls"])}})))
                apply_control_updates(store,section)
                for row in section["records"]:
                    counts[upsert_player_game(store,normalize_game(row,section["verified_at"]),corrections=True)]+=1
            report=validate(store)
            if report["status"]=="failed":
                errors=[r for r in report["checks"] if r["severity"]=="error"]
                raise ValueError("Official-history reconciliation failed: "+dumps(errors[:15]))
        from .model import project_upcoming
        from .offers import rank
        project_upcoming(store); rank(store)
        package=export_package(store,lock_held=True)
        return {"status":"success","seasons":seasons,"counts":dict(counts),"package":package,
            "note":"Official appearances supplement the original workbook. Missing DNP/injury/tracking histories were not invented. Historical knowledge times are retrieval times, not backfilled."}
