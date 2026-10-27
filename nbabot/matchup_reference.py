"""Scoped measured matchups and shot events from official NBA game endpoints.

The whitelist is the uploaded primary/alternate matchup table for the selected
Knicks players. It is a dated research scope, never a measured future assignment.
No missing pair is replaced with zeros and partial possessions stay separate
from full-game production.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import time
import unicodedata
import uuid
from urllib.parse import urlencode

from .core import digest, dumps, now


def _name(value):
    return " ".join(unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().casefold().split())


def _id(value):
    if value is None or isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError("Expected an integer or text NBA identifier")
    return str(value)


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.parent/("."+path.name+"-"+uuid.uuid4().hex)
    try:
        temp.write_text(dumps(value), encoding="utf-8")
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def matchup_scope(store):
    """Resolve only explicit selected-player primary/alternate pairs by stable ID."""
    players=store.rows("SELECT id,name FROM players")
    by_name={}
    for player in players:
        by_name.setdefault(_name(player["name"]),[]).append(player)
    selected=set()
    for supplied in store.config["organized_players"]:
        found=[p for p in players if p["id"]==str(supplied) or _name(p["name"])==_name(supplied)]
        if len(found)!=1:
            raise ValueError("Selected Knicks player is unknown or ambiguous")
        selected.add(found[0]["id"])
    if not selected:
        raise ValueError("An explicit selected Knicks cohort is required")
    selected_names={_name(p["name"]):p["id"] for p in players if p["id"] in selected}
    pairs=set(); roles={}; unresolved=[]
    for raw in store.rows("SELECT DISTINCT payload FROM raw_rows WHERE sheet='Matchups'"):
        row=json.loads(raw["payload"])
        knicks=selected_names.get(_name(row.get("Knicks player","")))
        if not knicks:
            continue
        team=row.get("Opponent")
        for field in ("Suspected defender","Alternate defender"):
            name=row.get(field)
            if not name:
                continue
            found=by_name.get(_name(name),[])
            if len(found)!=1:
                unresolved.append({"selected_knicks_player_id":knicks,"opponent_team":team,
                    "name":name,"field":field,"status":"unresolved_or_ambiguous_name"})
                continue
            identity=(knicks,str(team),found[0]["id"])
            pairs.add(identity)
            roles.setdefault(identity,set()).add(field)
    return {"selected":selected,"pairs":pairs,"roles":roles,"unresolved":unresolved,
        "players":{p["id"]:p["name"] for p in players}}


def _games(store,seasons=None,games=None):
    if games is None:
        rows=store.rows("""SELECT game_id,date_et,season,
            CASE WHEN team='NYK' THEN opponent ELSE team END opponent_team
            FROM player_games WHERE team='NYK' OR opponent='NYK'
            GROUP BY game_id,date_et,season,opponent_team ORDER BY date_et,game_id""")
    else:
        rows=games
    result={}
    for row in rows:
        game=dict(row)
        game["game_id"]=_id(game.get("game_id",game.get("id")))
        if len(game["game_id"])!=10 or not game["game_id"].startswith(("002","004")):
            continue
        if seasons and game["season"] not in seasons:
            continue
        game["season_type"]="Playoffs" if game["game_id"].startswith("004") else "Regular Season"
        game["opponent_team"]=game.get("opponent_team",game.get("opponent"))
        if not game["opponent_team"] or game["opponent_team"]=="NYK":
            raise ValueError("A scoped Knicks fixture requires the actual opponent")
        if game["game_id"] in result and result[game["game_id"]]!=game:
            raise ValueError("Conflicting Knicks game identities")
        result[game["game_id"]]=game
    return sorted(result.values(),key=lambda r:(r["date_et"],r["game_id"]))


def _metadata(game,url,at,source_timestamp=None):
    return {k:game[k] for k in ("game_id","date_et","season","season_type","opponent_team")} | {
        "source_url":url,"retrieved_at":at,"source_timestamp":source_timestamp}


def normalize_matchups(obj,game,scope,url,at):
    source=obj.get("boxScoreMatchups")
    if not isinstance(source,dict) or _id(source.get("gameId"))!=game["game_id"]:
        raise ValueError("Matchup response has a different game ID")
    teams=[source.get("homeTeam",{}),source.get("awayTeam",{})]
    codes={t.get("teamTricode") for t in teams}
    if codes!={"NYK",game["opponent_team"]}:
        raise ValueError("Matchup response historical teams differ from fixture")
    rows=[];seen=set()
    meta=_metadata(game,url,at,obj.get("meta",{}).get("time"))
    for team in teams:
        offense_team=team["teamTricode"]
        defense_team=game["opponent_team"] if offense_team=="NYK" else "NYK"
        for player in team.get("players",[]):
            offensive_id=_id(player["personId"])
            for matchup in player.get("matchups",[]):
                defender_id=_id(matchup["personId"])
                knicks,opponent=(offensive_id,defender_id) if offense_team=="NYK" else (defender_id,offensive_id)
                identity=(knicks,game["opponent_team"],opponent)
                if identity not in scope["pairs"]:
                    continue
                key=(game["game_id"],offensive_id,defender_id)
                if key in seen:
                    raise ValueError("Duplicate measured offensive/defensive pair")
                seen.add(key)
                stats=matchup.get("statistics",{})
                for field in ("partialPossessions","matchupFieldGoalsMade","matchupFieldGoalsAttempted",
                              "matchupThreePointersMade","matchupThreePointersAttempted"):
                    value=stats.get(field)
                    if value is not None and (not isinstance(value,(int,float)) or value<0):
                        raise ValueError("Invalid measured matchup statistic")
                for made,attempts in (("matchupFieldGoalsMade","matchupFieldGoalsAttempted"),
                                      ("matchupThreePointersMade","matchupThreePointersAttempted")):
                    if stats.get(made) is not None and stats.get(attempts) is not None and stats[made]>stats[attempts]:
                        raise ValueError("Measured matchup makes exceed attempts")
                rows.append({**meta,"offensive_player_id":offensive_id,
                    "offensive_player_name":(player.get("firstName","")+" "+player.get("familyName","")).strip(),
                    "offensive_team":offense_team,"defender_player_id":defender_id,
                    "defender_player_name":(matchup.get("firstName","")+" "+matchup.get("familyName","")).strip(),
                    "defender_team":defense_team,"selected_knicks_player_id":knicks,"matchup_player_id":opponent,
                    "knicks_side":"offense" if offense_team=="NYK" else "defense",
                    "scope_basis":sorted(scope["roles"].get(identity,[])),
                    "assignment_status":"Measured past pair; uploaded primary/alternate scope is a dated estimate, not a future assignment",
                    "units":"Partial-possession defender tracking; not whole-game production",
                    **stats})
    return rows


def normalize_shots(obj,game,scope,url,at,team_ids):
    result=next((x for x in obj.get("resultSets",[]) if x.get("name")=="Shot_Chart_Detail"),None)
    if result is None or not isinstance(result.get("headers"),list):
        raise ValueError("Shot response lacks its shot-event table")
    headers=result["headers"]
    if len(set(headers))!=len(headers):
        raise ValueError("Duplicate shot headers")
    opponents={p[2] for p in scope["pairs"] if p[1]==game["opponent_team"]}
    rows=[];seen=set()
    meta=_metadata(game,url,at)
    for values in result.get("rowSet",[]):
        if len(values)!=len(headers):
            raise ValueError("Invalid shot table width")
        raw=dict(zip(headers,values))
        game_id=_id(raw.get("GAME_ID"));player_id=_id(raw.get("PLAYER_ID"))
        event_id=_id(raw.get("GAME_EVENT_ID"));team_id=_id(raw.get("TEAM_ID"))
        if game_id!=game["game_id"]:
            raise ValueError("Shot event belongs to a different game")
        team=team_ids.get(team_id)
        if team not in ("NYK",game["opponent_team"]):
            raise ValueError("Shot historical team differs from scoped fixture")
        keep=(team=="NYK" and player_id in scope["selected"]) or (team==game["opponent_team"] and player_id in opponents)
        if not keep:
            continue
        if str(raw.get("GAME_DATE","")).replace("-","")[:8]!=game["date_et"].replace("-","")[:8]:
            raise ValueError("Shot event date differs from actual fixture date")
        key=(game_id,event_id,player_id)
        if key in seen:
            raise ValueError("Duplicate shot event")
        seen.add(key)
        for field in ("SHOT_ATTEMPTED_FLAG","SHOT_MADE_FLAG"):
            if raw.get(field) not in (0,1,None):
                raise ValueError("Invalid shot result flag")
        for field in ("GAME_ID","GAME_EVENT_ID","PLAYER_ID","TEAM_ID"):
            raw[field]=_id(raw[field])
        rows.append({**meta,"game_event_id":event_id,"player_id":player_id,"player_name":raw.get("PLAYER_NAME"),
            "team":team,"side":"Selected Knicks" if team=="NYK" else "Named matchup opponent",
            "units":"One published field-goal attempt; source coordinates/zone labels retained; defender distance unavailable",
            **raw})
    return rows


def _url(kind,game):
    from .maintenance import nba_parameters
    if kind=="matchups":
        return "https://stats.nba.com/stats/boxscorematchupsv3?"+urlencode({"GameID":game["game_id"]})
    params=nba_parameters(game["season"])
    params.update(GameID=game["game_id"],SeasonType=game["season_type"],SeasonTypeNullable=game["season_type"],
        ContextMeasure="FGA",ContextFilter="",ClutchTime="",AheadBehind="",PointDiff="",StartPeriod=0,EndPeriod=0,RangeType=0)
    return "https://stats.nba.com/stats/shotchartdetail?"+urlencode(params)


def _capture_path(root,relative):
    p=Path(relative)
    if p.is_absolute() or ".." in p.parts or len(p.parts)!=2 or p.parts[0]!="captures":
        raise ValueError("Invalid matchup capture path")
    path=root/p
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Matchup captures must be regular files")
    return path


def reconcile_shots(rows,game,scope,canonical):
    """Check scoped event totals against independently stored whole-game boxes."""
    named={p[2] for p in scope["pairs"] if p[1]==game["opponent_team"]}
    observed={}
    for row in rows:
        value=observed.setdefault(row["player_id"],{"fga":0,"fgm":0,"fg3a":0,"fg3m":0,"three_type_complete":True})
        value["fga"]+=row.get("SHOT_ATTEMPTED_FLAG")==1
        value["fgm"]+=row.get("SHOT_MADE_FLAG")==1
        value["fg3a"]+=row.get("SHOT_TYPE")=="3PT Field Goal" and row.get("SHOT_ATTEMPTED_FLAG")==1
        value["fg3m"]+=row.get("SHOT_TYPE")=="3PT Field Goal" and row.get("SHOT_MADE_FLAG")==1
        value["three_type_complete"]&=row.get("SHOT_TYPE") is not None
    checks={"matched_players":0,"unavailable_players":0,"differences":[]}
    for (game_id,pid),box in canonical.items():
        if game_id!=game["game_id"] or box.get("appeared")!=1:
            continue
        if not ((box.get("team")=="NYK" and pid in scope["selected"]) or
                (box.get("team")==game["opponent_team"] and pid in named)):
            continue
        shot=observed.get(pid,{"fga":0,"fgm":0,"fg3a":0,"fg3m":0,"three_type_complete":True})
        fields=("fga","fgm","fg3a","fg3m") if shot["three_type_complete"] else ("fga","fgm")
        diff={field:{"canonical":box[field],"shot_events":shot[field]} for field in fields
              if box.get(field) is not None and box[field]!=shot[field]}
        if diff:
            checks["differences"].append({"game_id":game_id,"player_id":pid,"differences":diff})
        elif all(box.get(field) is not None for field in fields):
            checks["matched_players"]+=1
        else:
            checks["unavailable_players"]+=1
    for pid in observed:
        if (game["game_id"],pid) not in canonical:
            checks["unavailable_players"]+=1
    return checks


def load_matchup_reference(store):
    root=store.root/"reference/matchups"
    if not (root/"latest.json").exists():
        return {"matchups":[],"shots":[],"coverage":[],"sources":[],"errors":[],"summary":{"status":"not_collected"}}
    index=json.loads((root/"latest.json").read_text())
    output={"matchups":[],"shots":[],"coverage":[],"sources":[],"errors":list(index.get("errors",[])),
        "summary":{k:v for k,v in index.items() if k!="captures"}}
    for item in index.get("captures",[]):
        try:
            path=_capture_path(root,item["path"])
            raw=path.read_bytes()
            if digest(raw)!=item["sha256"]:
                raise ValueError("Matchup capture checksum mismatch")
            capture=json.loads(raw)
            if capture["key"]!=item["key"]:
                raise ValueError("Matchup capture key mismatch")
            output[capture["kind"]].extend(capture["rows"])
            output["coverage"].append(capture["coverage"]|{"refresh_status":item.get("refresh_status","cached"),
                "shot_validation":item.get("shot_validation",capture["coverage"].get("shot_validation"))})
            output["sources"].append({k:capture[k] for k in ("key","kind","game","source_url","retrieved_at")})
        except (OSError,ValueError,TypeError,KeyError) as exc:
            output["errors"].append({"key":item.get("key"),"error":str(exc),"source":"cache_integrity"})
    for kind,fields in (("matchups",("game_id","offensive_player_id","defender_player_id")),
                        ("shots",("game_id","game_event_id","player_id"))):
        seen=set()
        for row in output[kind]:
            key=tuple(row[f] for f in fields)
            if key in seen:
                raise ValueError("Duplicate scoped observation across cached captures")
            seen.add(key)
        output[kind].sort(key=lambda r:(r["date_et"],r["game_id"],tuple(str(r[f]) for f in fields[1:])))
    return output


def collect_matchup_reference(store,seasons=None,game_limit=None,force=False,games=None,max_workers=4):
    """Collect scoped shots/pairs; caller should hold the maintenance lock.

    Historical observations are retrieved now. This never fabricates historical
    pregame publication snapshots, substitutes zeros, or mutates canonical games.
    """
    from .maintenance import fetch_json
    scope=matchup_scope(store)
    fixtures=_games(store,seasons,games)
    if game_limit is not None:
        fixtures=fixtures[-int(game_limit):]
    root=store.root/"reference/matchups"
    root.mkdir(parents=True,exist_ok=True)
    old=json.loads((root/"latest.json").read_text()) if (root/"latest.json").exists() else {"captures":[]}
    captures={r["key"]:dict(r) for r in old.get("captures",[])}
    # A changed whitelist must refilter old source captures, never retain an old
    # all-roster view as if it matched the newly selected scope.
    scope_hash=digest({"pairs":sorted(scope["pairs"]),"selected":sorted(scope["selected"])})
    requested=[];errors=[];new=0;cached=0
    team_ids={r["id"]:r["abbr"] for r in store.rows("SELECT * FROM teams")}
    canonical={(r["game_id"],r["player_id"]):r for r in store.rows(
        "SELECT game_id,player_id,team,appeared,fga,fgm,fg3a,fg3m FROM player_games WHERE team='NYK' OR opponent='NYK'")}
    # The career archive supplies independently controlled selected-player
    # playoff boxes absent from the regular-season canonical tables.
    career_errors=[]
    try:
        from .career_reference import load_career_reference
        career=load_career_reference(store)
        career_errors=career.get("errors",[])
        for row in career.get("rows",[]):
            if row["player_id"] in scope["selected"]:
                canonical.setdefault((row["game_id"],row["player_id"]),row)
    except ImportError:
        pass
    opportunity_errors=[];opportunity_conflicts=[]
    try:
        from .opportunity_reference import load_opportunity_reference
        opportunity=load_opportunity_reference(store)
        opportunity_errors=opportunity.get("errors",[])
        for row in opportunity.get("starts",[]):
            if row.get("validation_status")=="source_conflict":
                opportunity_conflicts.append({"game_id":row["game_id"],"player_id":row["player_id"],
                    "flags":row.get("flags",[]),"source_url":row.get("source_url")})
                continue
            if row.get("validation_status") in ("verified_canonical_identity_and_available_fields","source_game_identity_only"):
                canonical.setdefault((row["game_id"],row["player_id"]),row)
    except ImportError:
        pass
    for game in fixtures:
        for kind in ("matchups","shots"):
            key=kind+":"+game["game_id"]
            prior=captures.get(key)
            valid=False
            if prior and prior.get("scope_hash")==scope_hash:
                try:
                    raw=_capture_path(root,prior["path"]).read_bytes()
                    valid=digest(raw)==prior["sha256"]
                    if valid and kind=="shots":
                        check=reconcile_shots(json.loads(raw)["rows"],game,scope,canonical)
                        valid=not check["differences"]
                        if valid:
                            captures[key]["shot_validation"]=check
                except (OSError,ValueError):
                    pass
            if valid and not force:
                captures[key]["refresh_status"]="cached"
                cached+=1
            else:
                if prior and not valid:
                    captures.pop(key,None)
                requested.append((key,kind,game))

    def collect(spec):
        key,kind,game=spec
        url=_url(kind,game)
        try:
            obj=fetch_json(url,store.config)
        except Exception:
            time.sleep(0.2)
            obj=fetch_json(url,store.config)
        at=now()
        rows=(normalize_matchups(obj,game,scope,url,at) if kind=="matchups" else
              normalize_shots(obj,game,scope,url,at,team_ids))
        validation=reconcile_shots(rows,game,scope,canonical) if kind=="shots" else None
        if validation and validation["differences"]:
            _atomic(root/"quarantine"/(game["game_id"]+"_shots_"+uuid.uuid4().hex+".json"),
                {"source_url":url,"retrieved_at":at,"raw_source":obj,"validation":validation})
            raise ValueError("Scoped shot totals differ from canonical box scores: "+dumps(validation["differences"][:3]))
        coverage={**_metadata(game,url,at),"kind":kind,"scoped_rows":len(rows),
            "source_available":True,"scope_hash":scope_hash,
            "shot_validation":validation,
            "zero_rows_meaning":"No scoped published observation in this response; not a zero-valued missing pair/player statistic",
            "scope":"Only selected Knicks and their listed primary/alternate matchup players; actual historical teams required"}
        capture={"key":key,"kind":kind,"game":game,"scope_hash":scope_hash,"source_url":url,"retrieved_at":at,
            "coverage":coverage,"rows":rows,"raw_source":obj}
        path=root/"captures"/(game["game_id"]+"_"+kind+"_"+uuid.uuid4().hex+".json")
        _atomic(path,capture)
        return {"key":key,"kind":kind,"game_id":game["game_id"],"scope_hash":scope_hash,
            "path":"captures/"+path.name,"sha256":digest(path.read_bytes()),"rows":len(rows),
            "retrieved_at":at,"refresh_status":"success","shot_validation":validation}

    with ThreadPoolExecutor(max_workers=max(1,min(int(max_workers),6))) as pool:
        futures={pool.submit(collect,spec):spec for spec in requested}
        for future in as_completed(futures):
            key,kind,game=futures[future]
            try:
                captures[key]=future.result();new+=1
            except Exception as exc:
                same_scope=key in captures and captures[key].get("scope_hash")==scope_hash
                if key in captures and not same_scope:
                    captures.pop(key)
                if same_scope:
                    captures[key]["refresh_status"]="retained_after_failure"
                errors.append({"key":key,"source_url":_url(kind,game),"game_id":game["game_id"],
                    "error_type":type(exc).__name__,"error":str(exc)[:300],"retained_previous":same_scope})
    active=[r for r in captures.values() if r.get("scope_hash")==scope_hash]
    report={"status":"success" if not errors else "partial" if active else "failed","attempted_at":now(),
        "scope_hash":scope_hash,"selected_player_ids":sorted(scope["selected"]),"whitelisted_directed_identity_pairs":len(scope["pairs"]),
        "unresolved_scope_names":scope["unresolved"],"requested_games":len(fixtures),"new_captures":new,"cached_captures":cached,
        "matchup_rows":sum(r["rows"] for r in active if r["kind"]=="matchups"),
        "shot_rows":sum(r["rows"] for r in active if r["kind"]=="shots"),"errors":errors,
        "shot_reconciliation":{"matched_players":sum((r.get("shot_validation") or {}).get("matched_players",0) for r in active),
            "unavailable_players":sum((r.get("shot_validation") or {}).get("unavailable_players",0) for r in active),
            "differences":sum(len((r.get("shot_validation") or {}).get("differences",[])) for r in active)},
        "career_validation_errors":career_errors,
        "opportunity_validation_errors":opportunity_errors,
        "opportunity_validation_conflicts":opportunity_conflicts,
        "meaning":"Measured historical partial-possession pairs and shot attempts only. No per-defender rebounds or shot defender-distance field; no historical pregame vintages inferred.",
        "captures":sorted(active,key=lambda r:r["key"])}
    _atomic(root/"runs"/(uuid.uuid4().hex+".json"),report)
    _atomic(root/"latest.json",report)
    return {k:v for k,v in report.items() if k!="captures"}
