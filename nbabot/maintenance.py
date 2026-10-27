from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from .core import ET, digest, dumps, freshness, latest_snapshots, now, stamp
from .ingest import normalize_game, upsert_game, upsert_player_game, validate

REQUIRED = ("stats", "schedule", "rosters", "injuries", "lineups", "news")


def retain_packages(store):
    """Cleanup is best effort after publication; it cannot invalidate a good refresh."""
    try:
        from .retention import prune_packages
        return prune_packages(store)
    except Exception as e:
        logging.getLogger(__name__).warning("Package retention failed: %s",type(e).__name__)
        return {"status":"cleanup_failed", "error_type":type(e).__name__}


def nba_parameters(season,start="",end=""):
    # Explicit published-source filters: these endpoints do not share safe defaults.
    return {"Conference":"","DateFrom":start,"DateTo":end,"Division":"","GameScope":"","GameSegment":"",
        "LastNGames":0,"LeagueID":"00","Location":"","Month":0,"OpponentTeamID":0,"Outcome":"","PORound":0,
        "PaceAdjust":"N","PerMode":"Totals","Period":0,"PlayerExperience":"","PlayerPosition":"","PlusMinus":"N",
        "Rank":"N","SeasonSegment":"","SeasonType":"Regular Season","ShotClockRange":"","StarterBench":"",
        "TeamID":0,"VsConference":"","VsDivision":"","Weight":"","MeasureType":"Base","PlayerID":0,
        "College":"","Country":"","DraftPick":"","DraftYear":"","Height":"","DistanceRange":"",
        "ShotDistRange":"","ShotLocation":"","Counter":0,"Season":season,"SeasonNullable":season,
        "SeasonTypeNullable":"Regular Season"}


def safe_cell(value):
    # CSV spreadsheet formula injection protection; numerics and IDs retain their types.
    if isinstance(value,str) and value.startswith(("=","+","-","@","\t","\r")):
        return "'"+value
    return value


def prepare_package(store, label="manual"):
    """Build a complete immutable version before changing the stable latest pointer."""
    version = datetime.now().strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex[:8]
    root = store.root / "packages"
    root.mkdir(exist_ok=True)
    dest = root / version
    dest.mkdir()
    tables = {
        "NBA_Player_Games":store.rows("SELECT * FROM player_games ORDER BY date_et,game_id,player_id"),
        "Games":store.rows("SELECT * FROM games ORDER BY date_et,id"),
        "Players":store.rows("SELECT * FROM players ORDER BY id"),
        "Teams":store.rows("SELECT * FROM teams ORDER BY id"),
        "Snapshots":store.rows("SELECT * FROM snapshots ORDER BY observed_at"),
        "Correction_Audit":store.rows("SELECT * FROM audit ORDER BY id"),
        "Update_Log":store.rows("SELECT * FROM update_runs ORDER BY started_at"),
        "Data_Files":store.rows("SELECT * FROM files ORDER BY imported_at"),
    }
    registry=[json.loads(r["payload"]) for r in store.rows("SELECT payload FROM raw_rows WHERE sheet='Source Registry'")]
    seen=set()
    for r in store.rows("SELECT DISTINCT source,known_at FROM player_games UNION SELECT DISTINCT source,retrieved_at FROM snapshots"):
        url,at=r["source"],r["known_at"]
        if not url or not url.startswith("https://") or (url,at) in seen:
            continue
        seen.add((url,at))
        registry.append({"Source ID":"fetched:"+digest([url,at])[:20],"Publisher":urlparse(url).hostname,
            "Dataset":"Verified observation or publisher snapshot","Source URL":url,"Retrieved at (UTC)":at,
            "Publisher update time":None,"Verification":"See canonical controls, update log and snapshot scope"})
    tables["Source_Registry"]=registry
    from .report import public_projection
    tables["Upcoming_Projections"]=[public_projection(json.loads(r["payload"])) for r in store.rows(
        "SELECT payload FROM projections WHERE status='active' ORDER BY game_id,player_id")]
    # Recompute current summaries; imported season/tracking summaries remain dated Raw_ tables.
    columns=["minutes","pts","reb","ast","fg3m","fg3a","fgm","fga","ftm","fta"]
    expressions=",".join(f"count({c}) {c}_observed,avg({c}) {c}_observed_mean,"
        f"CASE WHEN count({c})=count(*) THEN sum({c}) ELSE NULL END {c}_complete_total" for c in columns)
    tables["Player_Season_Derived"]=store.rows("SELECT player_id,season,count(*) played_games,min(date_et) first_game,max(date_et) through_date,"+
        expressions+" FROM player_games WHERE appeared=1 GROUP BY player_id,season ORDER BY player_id,season")
    tables["Dataset_Freshness"]=store.rows("SELECT * FROM freshness ORDER BY kind")
    for kind in ("injuries","rosters","lineups","news"):
        tables[kind.title()+"_History"]=store.rows("SELECT * FROM snapshots WHERE kind=? ORDER BY observed_at",(kind,))
    for sheet in store.rows("SELECT DISTINCT sheet FROM raw_rows ORDER BY sheet"):
        name = sheet["sheet"].replace(" ","_")
        # Raw tables are clearly marked and never appended to canonical observations.
        tables["Raw_"+name] = [json.loads(r["payload"]) for r in store.rows(
            "SELECT payload FROM raw_rows WHERE sheet=? ORDER BY file_hash,row_number",(sheet["sheet"],))]
    wb = Workbook(write_only=True)
    used = set()
    for name, rows in tables.items():
        headers = list(dict.fromkeys(k for r in rows for k in r)) or ["no_records"]
        with (dest / (name+".csv")).open("w",newline="",encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for r in rows:
                w.writerow([safe_cell(dumps(r.get(k)) if isinstance(r.get(k),(dict,list)) else r.get(k)) for k in headers])
        title = name[:31]
        n = 1
        while title in used:
            title = name[:27]+"_"+str(n); n+=1
        used.add(title)
        ws = wb.create_sheet(title)
        ws.append(headers)
        for r in rows:
            cells=[]
            for h in headers:
                v=r.get(h)
                if isinstance(v,(dict,list)):
                    v=dumps(v)
                cell=WriteOnlyCell(ws,value=v)
                if isinstance(v,str):
                    cell.data_type="s"  # preserve text IDs and never execute raw formulas
                cells.append(cell)
            ws.append(cells)
    wb.save(dest / "Master_Data.xlsx")
    organized = {"status":"not_configured", "reason":"Exactly seven or eight selected Knicks players must be configured in organized_players."}
    if store.config.get("organized_players"):
        from .organized import build_organized_workbook
        organized = build_organized_workbook(store, dest, csv_dir=dest/"organized_csv")
    # iterdump sees this connection's current transaction. A backup from another connection
    # would omit pending changes; deserializing a WAL image is not portable across hosts.
    snapshot = sqlite3.connect(dest / "database.sqlite3")
    snapshot.executescript("\n".join(store.db.iterdump()))
    snapshot.close()
    manifest = {
        "version":version,"created_at":now(),"reason":label,
        "last_successful_refresh":store.one("SELECT * FROM update_runs WHERE status='success' ORDER BY finished_at DESC LIMIT 1"),
        "coverage":store.one("SELECT min(date_et) first,max(date_et) last,count(*) player_games FROM player_games"),
        "freshness":freshness(store),"validation":validate(store),
        "tables":{k:len(v) for k,v in tables.items()},
        "organized_workbook":organized,
        "limitations":["Imported source checks are archived claims, not fresh independent verification.",
                        "Raw season means, team on/off and estimated defenders are not player-game features.",
                        "Missing values export as blank, not zero; all IDs are text in the workbook."],
    }
    from .historical_context import load_context_reference
    context=load_context_reference(store)
    manifest["historical_context"]={k:v for k,v in context.items() if k!="tables"}
    manifest["historical_context"]["tables"]=[{k:v for k,v in table.items() if k!="rows"} | {"row_count":len(table["rows"])} for table in context["tables"]]
    scoped_index=store.root/"reference/scoped_updates/latest.json"
    if scoped_index.is_file():
        scoped=json.loads(scoped_index.read_text())
        manifest["scoped_history"]={k:scoped.get(k) for k in ("status","attempted_at","selected_player_count","pair_rules","target_games","errors","meaning")}
    from .scoped_history import load_feature_snapshot
    prospective=load_feature_snapshot(store)
    manifest["prospective_feature_snapshot"]={k:prospective.get(k) for k in ("status","latest","historical_pregame_vintages")}
    manifest["sha256"]={str(p.relative_to(dest)):digest(p.read_bytes()) for p in dest.rglob("*") if p.is_file()}
    (dest/"manifest.json").write_text(json.dumps(manifest,indent=2,default=str))
    return dest


def publish_package(store, dest):
    latest=store.root/"packages"/"latest"
    if latest.exists() and not latest.is_symlink():
        raise ValueError("Refusing to replace a user-created latest directory")
    temp=latest.parent/(".latest-"+uuid.uuid4().hex)
    temp.symlink_to(dest.name,target_is_directory=True)
    os.replace(temp,latest)
    return str(dest)


def export_package(store,lock_held=False):
    def export_snapshot():
        with store.transaction(write=False):
            report=validate(store)
            if report["status"]=="failed":
                raise ValueError("Resolve quarantined records or validation conflicts before publishing")
            result=publish_package(store,prepare_package(store,"manual canonical export; source freshness unchanged"))
        retain_packages(store)
        return result
    if lock_held:
        return export_snapshot()
    with (store.root/".refresh.lock").open("w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A refresh/bootstrap is in progress; export after it completes")
        return export_snapshot()


def fetch_json(url, config, token=None):
    parsed=urlparse(url)
    if config.get("data_source_mode")=="spreadsheet" and not (config.get("external_sources_approved") is True
        and parsed.hostname in set(config.get("external_source_approvals",[]))):
        raise ValueError("Outside data access is awaiting owner approval; the master spreadsheet remains the active source")
    if parsed.scheme!="https" or parsed.username or parsed.password:
        raise ValueError("Provider URL must be HTTPS without embedded credentials")
    allowed=set(config.get("provider_allowed_hosts",[])) | {"cdn.nba.com","stats.nba.com","site.api.espn.com"}
    if parsed.hostname not in allowed:
        raise ValueError("Declare the authorized provider hostname in provider_allowed_hosts")
    headers={"User-Agent":"NBAPPBOT/0.1 (+local research)","Accept":"application/json"}
    if parsed.hostname in ("stats.nba.com","cdn.nba.com"):
        headers.update({"Referer":"https://www.nba.com/","Origin":"https://www.nba.com"})
    if token:
        header=config.get("provider_auth_header","Authorization")
        scheme=config.get("provider_auth_scheme","Bearer")
        if not re.fullmatch(r"[A-Za-z0-9-]+",header) or header.lower() in ("host","proxy-authorization","connection"):
            raise ValueError("Unsupported authentication header name")
        if scheme and not re.fullmatch(r"[A-Za-z0-9_-]+",scheme):
            raise ValueError("Unsupported authentication scheme")
        headers[header]=(scheme+" " if scheme else "")+token
    # Preserve certificate verification, proxy bindings, and credential destination scope.
    with httpx.Client(timeout=config["http_timeout"],follow_redirects=False) as client:
        response=client.get(url,headers=headers)
        response.raise_for_status()
        if len(response.content)>80_000_000:
            raise ValueError("Feed exceeds 80MB limit")
        return response.json()


def source_check(store):
    """Read-only diagnostics; never print credential values or raw provider payloads."""
    config=store.config
    if config.get("data_source_mode")=="spreadsheet":
        return {"status":"awaiting_approval" if not config.get("external_sources_approved") else "not_configured",
            "source_policy":"master_primary","note":"Outside integrations remain available; requests require explicit approval for the source and a configured operation"}
    if not config.get("provider_url"):
        if config.get("public_context_enabled"):
            from .public_sources import request_many,ESPN
            sources={"nba_schedule":"https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json",
                "nba_roster":"https://stats.nba.com/stats/commonteamroster?LeagueID=00&Season="+config["season"]+"&TeamID=1610612752",
                "espn_injuries":ESPN+"/injuries","espn_news":ESPN+"/news?team=18&limit=5"}
            results,errors=request_many(sources,config)
            return {"mode":"public_publishers","status":"reachable" if len(results)==len(sources) else "failed",
                "reachable_sources":list(results),"errors":errors,"credentials_required":False,
                "note":"Read-only access check; live coverage and response parsing still require a successful full refresh"}
        return {"status":"not_configured","missing":"Authorized HTTPS endpoint and response mapping"}
    token=os.environ.get(config["provider_token_env"])
    try:
        obj=fetch_json(config["provider_url"],config,token)
        sections=obj.get("sections",{}) if isinstance(obj,dict) else {}
        checks={}
        for kind in REQUIRED:
            if kind not in sections:
                checks[kind]={"status":"missing_mapping_or_section"}
                continue
            try:
                validate_section(kind,sections[kind])
                checks[kind]={"status":"valid_contract","records":len(sections[kind]["records"])}
            except (ValueError,KeyError,TypeError) as e:
                checks[kind]={"status":"invalid_contract","error_type":type(e).__name__}
        return {"status":"reachable","token_binding_present":bool(token),"sections":checks,
                "top_level_fields":list(obj) if isinstance(obj,dict) else None,
                "note":"Read-only contract check; no freshness, recommendations or data package changed"}
    except Exception as e:
        return {"status":"failed","token_binding_present":bool(token),"error_type":type(e).__name__,
                "http_status":e.response.status_code if isinstance(e,httpx.HTTPStatusError) else None,
                "note":"No credential values logged; diagnose network, authorization and response contract separately"}


def nba_sections(store):
    config=store.config
    sections={}; errors=[]
    at=now()
    schedule_url="https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"
    try:
        obj=fetch_json(schedule_url,config)
        at=now()
        league=obj["leagueSchedule"]
        records=[]; unassigned=[]
        for date in league["gameDates"]:
            for g in date["games"]:
                if not g.get("gameId") or not g.get("gameDateTimeUTC") or not g.get("homeTeam",{}).get("teamTricode") or not g.get("awayTeam",{}).get("teamTricode"):
                    unassigned.append(g)
                    continue
                tip=stamp(g["gameDateTimeUTC"])
                records.append({"id":str(g["gameId"]),"tipoff":tip,
                    "date_et":datetime.fromisoformat(tip).astimezone(ET).date().isoformat(),
                    "home":g["homeTeam"]["teamTricode"],"away":g["awayTeam"]["teamTricode"],
                    "status":("postponed" if "postpon" in str(g.get("gameStatusText","")).lower() or str(g.get("gameStatusText","")).upper() in ("PPD","SUSPENDED","CANCELED","CANCELLED") else
                              {1:"scheduled",2:"live",3:"completed"}.get(g.get("gameStatus"),"unknown")),
                    "season":str(league.get("seasonYear",config["season"])),"known_at":at,
                    "source":schedule_url,"metadata":dumps(g)})
        if not records:
            raise ValueError("Empty official schedule")
        sections["schedule"]={"complete":True,"verified_at":at,"coverage_cutoff":at,"source_url":schedule_url,"records":records}
        sections["schedule"]["unassigned_slots"]=unassigned
    except Exception as e:
        errors.append({"source":"nba_schedule","error":type(e).__name__+": "+str(e)[:180]})
    from urllib.parse import urlencode
    last=store.one("SELECT max(date_et) cutoff FROM player_games")
    cutoff=last["cutoff"] if last else None
    start=(datetime.fromisoformat(cutoff)-timedelta(days=7)).strftime("%m/%d/%Y") if cutoff else ""
    params=nba_parameters(config["season"],start)
    stats_url="https://stats.nba.com/stats/playergamelogs?"+urlencode(params)
    try:
        obj=fetch_json(stats_url,config)
        at=now()
        rs=obj["resultSets"][0]
        records=[]
        for values in rs["rowSet"]:
            r=dict(zip(rs["headers"],values))
            matchup=r.get("MATCHUP","")
            r.update({"Opponent":matchup.split()[-1] if matchup else None,"Season":config["season"],
                      "Source ID":stats_url,"known_at":at,"Appeared":"Yes"})
            scheduled=next((g for g in sections.get("schedule",{}).get("records",[]) if g["id"]==str(r.get("GAME_ID"))),None)
            if scheduled and scheduled["status"]=="completed":
                r["game_status"]="completed"
            records.append(r)
        if not records:
            raise ValueError("No completed player-game rows returned; not marking historical data fresh")
        through=max(r["GAME_DATE"][:10] for r in records)
        controls_url="https://stats.nba.com/stats/leaguedashplayerstats?"+urlencode(nba_parameters(config["season"],end=datetime.fromisoformat(through).strftime("%m/%d/%Y")))
        controls_obj=fetch_json(controls_url,config)
        control_set=controls_obj["resultSets"][0]
        controls=[]
        for values in control_set["rowSet"]:
            c=dict(zip(control_set["headers"],values))
            controls.append({"Season":config["season"],"NBA player ID":str(c["PLAYER_ID"]),"Player":c["PLAYER_NAME"],
                "GP":c["GP"],"Minutes (source precision)":c.get("MIN"),"PTS":c.get("PTS"),"REB":c.get("REB"),
                "AST":c.get("AST"),"3PM":c.get("FG3M"),"Through":through,"source_url":controls_url,"raw":c})
        if not controls:
            raise ValueError("Empty independent season controls")
        sections["stats"]={"complete":True,"verified_at":now(),"coverage_cutoff":through,
                            "source_url":stats_url,"records":records,"season_controls":controls,
                            "season_controls_source":controls_url}
    except Exception as e:
        errors.append({"source":"nba_stats","error":type(e).__name__+": "+str(e)[:180]})
    return sections,errors


def validate_section(kind, section):
    if not section.get("complete") or not isinstance(section.get("records"),list):
        raise ValueError(f"{kind}: complete=true and records array required")
    verified=stamp(section["verified_at"])
    if verified>now():
        raise ValueError(f"{kind}: verification is in the future")
    if not section.get("coverage_cutoff"):
        raise ValueError(f"{kind}: coverage cutoff required")
    source=section.get("source_url","")
    if not source.startswith("https://"):
        raise ValueError(f"{kind}: HTTPS evidence URL required")
    if kind in ("stats","schedule","rosters") and not section["records"]:
        raise ValueError(f"{kind}: empty mandatory table")
    for r in section["records"]:
        if kind=="news":
            for field in ("id","source_url","published_at","retrieved_at","players","confirmation","headline"):
                if field not in r:
                    raise ValueError(f"News missing {field}")
            if r["confirmation"] not in ("confirmed","unconfirmed"):
                raise ValueError("News confirmation must be explicit")
            if stamp(r["published_at"])>stamp(r["retrieved_at"]):
                raise ValueError("News publication after retrieval")
        if kind=="rosters" and not all(r.get(f) for f in ("player_id","name","team","observed_at")):
            raise ValueError("Roster needs NBA ID, name, team, observed_at")
        if kind=="injuries" and not all(r.get(f) for f in ("player_id","game_id","status","observed_at")):
            raise ValueError("Availability needs player_id,game_id,status,observed_at; omissions are unknown")
        if kind=="lineups" and not all(r.get(f) for f in ("game_id","team","observed_at","confirmation")):
            raise ValueError("Lineup needs game, team, timestamp and confirmation")
        if r.get("observed_at") and stamp(r["observed_at"])>verified:
            raise ValueError(f"{kind}: observation after verification")
    return verified


def apply_control_updates(store,section):
    old_controls={}
    for row in store.rows("SELECT payload FROM raw_rows WHERE sheet='NBA Season Totals'"):
        c=json.loads(row["payload"])
        old_controls[str(c.get("Season"))+"/"+str(c.get("NBA player ID"))]=c
    old_controls.update({r["key"]:json.loads(r["payload"]) for r in store.rows(
        "SELECT key,payload FROM snapshots WHERE kind='season_controls' ORDER BY observed_at")})
    for c in section.get("season_controls",[]):
        identity=c["Season"]+"/"+str(c["NBA player ID"])
        previous=old_controls.get(identity)
        if previous and any(previous.get(k)!=c.get(k) for k in ("GP","Minutes (source precision)","PTS","REB","AST","3PM")):
            store.db.execute("INSERT INTO audit(at,kind,record_key,old,new,source) VALUES(?,?,?,?,?,?)",
                (now(),"official_control_revision",identity,dumps(previous),dumps(c),section["season_controls_source"]))
        verified=section["verified_at"]
        store.snapshot("season_controls",identity,verified,section["season_controls_source"],c,verified)


def apply_sections(store, sections):
    for kind in REQUIRED:
        section=sections[kind]
        verified=validate_section(kind,section)
        source=section["source_url"]
        if kind=="stats" and section.get("season_controls"):
            apply_control_updates(store,section)
        if kind=="rosters":
            covered_teams={r["team"] for r in section["records"]}
            present={str(r["player_id"]) for r in section["records"]}
            for pid,previous in latest_snapshots(store,"rosters").items():
                if previous["team"] in covered_teams and pid not in present and previous.get("active",True):
                    store.snapshot("rosters",pid,verified,source,{"player_id":pid,"name":previous["name"],
                        "team":previous["team"],"active":False,"observed_at":verified,
                        "note":"No longer on a complete current roster; new destination and availability unknown"},verified)
        for r in section["records"]:
            if kind=="stats":
                p=normalize_game(r,verified)
                if p["date_et"]>=datetime.fromisoformat(verified).astimezone(ET).date().isoformat():
                    # Same-day finalized games need explicit final evidence.
                    if r.get("game_status")!="completed":
                        raise ValueError("Same-day stats require completed game_status")
                upsert_player_game(store,p,corrections=True)
            elif kind=="schedule":
                r={**r,"known_at":verified,"source":source}
                alias=store.one("SELECT game_id FROM game_aliases WHERE alias=?",(r["id"],))
                if alias:
                    r["id"]=alias["game_id"]
                # Merge dated ESPN snapshot IDs into official game IDs without duplicate fixtures.
                previous=store.rows("SELECT * FROM games WHERE date_et=? AND home=? AND away=? AND id<>?",
                                    (r["date_et"],r["home"],r["away"],r["id"]))
                for old in previous:
                    if old["id"].startswith("espn:"):
                        store.db.execute("INSERT OR REPLACE INTO game_aliases VALUES(?,?)",(old["id"],r["id"]))
                        store.db.execute("UPDATE offers SET game_id=? WHERE game_id=?",(r["id"],old["id"]))
                        store.db.execute("DELETE FROM games WHERE id=?",(old["id"],))
                upsert_game(store,r)
            else:
                identity = (str(r["player_id"]) if kind=="rosters" else
                            str(r["game_id"])+"/"+str(r["player_id"]) if kind=="injuries" else
                            str(r["game_id"])+"/"+r["team"] if kind=="lineups" else str(r["id"]))
                observed=r.get("observed_at",r.get("published_at",verified))
                if kind=="rosters":
                    store.db.execute("INSERT INTO players VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                                     (str(r["player_id"]),r["name"]))
                store.snapshot(kind,identity,observed,r.get("source_url",source),r,r.get("retrieved_at",verified))
        store.db.execute("INSERT OR REPLACE INTO freshness VALUES(?,?,?,?)",
                         (kind,verified,section["coverage_cutoff"],source))


def refresh(store):
    if store.config.get("data_source_mode")=="spreadsheet":
        from .master_source import refresh_master
        return refresh_master(store)
    with (store.root/".refresh.lock").open("w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status":"busy","note":"Another refresh owns the maintenance lock"}
        return _refresh(store)


def _refresh(store):
    """A failed mandatory source cannot replace the last verified master package."""
    recover_publication(store)
    run=uuid.uuid4().hex; at=now(); sections={}; errors=[]
    store.db.execute("INSERT INTO update_runs VALUES(?,?,?,?,?,?,?)",(run,at,None,"running","{}","[]",None))
    store.db.commit()
    if store.config["nba_enabled"]:
        sections,errors=nba_sections(store)
    if store.config.get("public_context_enabled") and not (store.config.get("provider_file") or store.config.get("provider_url")):
        from .public_sources import collect_context
        context,context_errors=collect_context(store,sections)
        sections.update(context); errors.extend(context_errors)
    try:
        if store.config.get("provider_file"):
            obj=json.loads(Path(store.config["provider_file"]).read_text())
            sections.update(obj["sections"])
        elif store.config.get("provider_url"):
            token=os.environ.get(store.config["provider_token_env"])
            obj=fetch_json(store.config["provider_url"],store.config,token)
            sections.update(obj["sections"])
    except Exception as e:
        errors.append({"source":"authorized_provider","error":type(e).__name__+": "+str(e)[:200]})
    # Retain every fetched snapshot, including incomplete runs, separately from the published tables.
    archive=store.root/"refresh_raw"; archive.mkdir(exist_ok=True)
    (archive/(run+".json")).write_text(dumps({"retrieved_at":now(),"sections":sections,"errors":errors}))
    for kind in REQUIRED:
        if kind not in sections:
            errors.append({"source":kind,"error":"Missing authorized source/section"})
        else:
            try:
                verified=validate_section(kind,sections[kind])
                age=(datetime.fromisoformat(now())-datetime.fromisoformat(verified)).total_seconds()/3600
                if age>store.config["freshness_hours"][kind]:
                    raise ValueError("Source verification is stale")
            except Exception as e:
                errors.append({"source":kind,"error":str(e)})
    # An official-source failure is nonblocking if an authorized replacement covered that section.
    errors=[e for e in errors if not (e["source"]=="nba_stats" and "stats" in sections)
            and not (e["source"]=="nba_schedule" and "schedule" in sections)]
    dest=None
    context_report=None
    scoped_report=None
    if not errors:
        if store.config.get("nba_enabled") and store.config.get("historical_context_enabled",True):
            try:
                from .historical_context import collect_context_reference
                context_report=collect_context_reference(store)
            except Exception as e:
                context_report={"status":"failed","error_type":type(e).__name__,"meaning":"Optional historical references were retained; these are not projection features"}
                logging.getLogger(__name__).warning("Optional historical-context refresh failed: %s",type(e).__name__)
        latest=store.root/"packages"/"latest"
        previous=os.readlink(latest) if latest.is_symlink() else None
        try:
            with store.transaction():
                apply_sections(store,sections)
                report=validate(store)
                if report["status"]=="failed":
                    raise ValueError("Canonical validation failed: "+dumps(report))
                if store.config.get("nba_enabled") and store.config.get("organized_players") and store.config.get("scoped_history_enabled",True):
                    try:
                        from .scoped_history import collect_scoped_history
                        scoped_report=collect_scoped_history(store,rotation_batch_size=store.config.get("historical_rotation_batch_size",8))
                    except Exception as e:
                        scoped_report={"status":"failed","error_type":type(e).__name__,"meaning":"Optional scoped references retained; no model effects assumed"}
                        logging.getLogger(__name__).warning("Optional scoped historical refresh failed: %s",type(e).__name__)
                store.db.execute("UPDATE update_runs SET finished_at=?,status='success',sources=?,errors='[]' WHERE id=?",
                                 (now(),dumps({k:v["source_url"] for k,v in sections.items()}),run))
                store.db.execute("UPDATE decisions SET status='superseded' WHERE status='active'")
                store.db.execute("UPDATE projections SET status='superseded' WHERE status='active'")
                from .model import project_upcoming
                from .offers import rank
                projections=project_upcoming(store,commit=False)
                ranking=rank(store,commit=False)
                if scoped_report is not None:
                    from .scoped_history import archive_feature_snapshot
                    scoped_report["prospective_snapshot"]=archive_feature_snapshot(store)
                dest=prepare_package(store,"verified daily/pregame refresh")
                store.db.execute("UPDATE update_runs SET package=? WHERE id=?",(str(dest),run))
                publish_package(store,dest)
            retention=retain_packages(store)
            return {"run":run,"status":"success","package":str(dest),"projections":len(projections),
                    "qualifying_picks":len(ranking["qualifying"]),"retention":retention,"historical_context":context_report,"scoped_history":scoped_report}
        except Exception as e:
            # Restore publication pointer if a filesystem/commit fault follows replacement.
            # A crash between pointer replacement and COMMIT is recovered by recover_publication().
            if dest and latest.is_symlink() and os.readlink(latest)==dest.name:
                if previous is None:
                    latest.unlink()
                else:
                    recovery=latest.parent/(".recover-"+uuid.uuid4().hex)
                    recovery.symlink_to(previous,target_is_directory=True)
                    os.replace(recovery,latest)
            errors.append({"source":"validation_or_publication","error":str(e)[:1000]})
    store.db.rollback()
    store.db.execute("UPDATE update_runs SET finished_at=?,status='failed',sources=?,errors=? WHERE id=?",
                     (now(),dumps(list(sections)),dumps(errors),run))
    store.db.execute("UPDATE decisions SET status='superseded' WHERE status='active'")
    store.db.commit()
    return {"run":run,"status":"failed","errors":errors,"retained_latest":str(store.root/"packages"/"latest"),
            "recommendations":"withheld until required sources recover"}


def recover_publication(store):
    """Reconcile a crash in the tiny pointer/SQLite commit window, without network calls."""
    latest=store.root/"packages/latest"
    committed=store.one("SELECT package FROM update_runs WHERE status='success' AND package IS NOT NULL ORDER BY finished_at DESC LIMIT 1")
    if not latest.is_symlink() or not (latest/"manifest.json").exists():
        return
    manifest=json.loads((latest/"manifest.json").read_text())
    run=manifest.get("last_successful_refresh")
    if run and not store.one("SELECT 1 FROM update_runs WHERE id=? AND status='success'",(run["id"],)):
        if committed and Path(committed["package"]).exists():
            publish_package(store,Path(committed["package"]))
        else:
            # Find the newest published manual/historical version, not a partial build.
            choices=[]
            for p in latest.parent.glob("*/manifest.json"):
                if p.parent.is_symlink():
                    continue
                m=json.loads(p.read_text())
                if not m.get("last_successful_refresh"):
                    choices.append(p.parent)
            if choices:
                publish_package(store,sorted(choices)[-1])
            else:
                latest.unlink()


def recover(store):
    with (store.root/".refresh.lock").open("w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status":"busy"}
        recover_publication(store)
        return {"status":"checked","latest":str(store.root/"packages/latest")}
