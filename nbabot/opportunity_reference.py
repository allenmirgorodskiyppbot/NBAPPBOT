"""Scoped, source-dated game opportunities and observed box-score roles.

Only the user's selected Knicks players and the workbook's named primary/alternate
matchups are exported. Published season team labels never replace historical teams.
No game records, injury facts, or historical pregame knowledge are invented.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlencode

from .core import ET, digest, dumps, now
from .public_sources import normalize_name

BOX_FIELDS = {"points":"pts", "reboundsTotal":"reb", "assists":"ast",
    "fieldGoalsMade":"fgm", "fieldGoalsAttempted":"fga", "threePointersMade":"fg3m",
    "threePointersAttempted":"fg3a", "freeThrowsMade":"ftm", "freeThrowsAttempted":"fta",
    "reboundsOffensive":"oreb", "reboundsDefensive":"dreb", "steals":"stl",
    "blocks":"blk", "turnovers":"tov", "foulsPersonal":"pf"}


def _root(store):
    return store.root / "reference" / "opportunities"


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp=path.parent/("."+path.name+"-"+uuid.uuid4().hex)
    try:
        temp.write_text(dumps(payload),encoding="utf-8")
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def _minutes(value):
    if value in (None, ""):
        return None
    text=str(value)
    if ":" in text:
        parts=text.split(":")
        if len(parts)!=2 or not re.fullmatch(r"\d+",parts[0]):
            raise ValueError("Invalid source minute clock")
        seconds=float(parts[1])
        # NBA's published integer-second formatter occasionally emits e.g.
        #27:60 for a duration close to28minutes. Preserve that source string,
        # interpret its stated units arithmetically, and retain a format flag
        # on the observation. Never replace the canonical precise minute value.
        if not 0<=seconds<=60:
            raise ValueError("Invalid source minute seconds")
        answer=int(parts[0])+seconds/60
    else:
        answer=float(value)
    if not 0<=answer<=90:
        raise ValueError("Invalid source playing minutes")
    return answer


def matchup_cohort(store):
    """Unique normalized full names only; no current roster or fuzzy surname join."""
    names={}
    for p in store.rows("SELECT id,name FROM players"):
        names.setdefault(normalize_name(p["name"]),[]).append(p)
    selected=set(map(str,store.config.get("organized_players",[])))
    selected_names={p["name"] for group in names.values() for p in group if p["id"] in selected}
    result={}; unresolved=[]
    for record in store.rows("SELECT payload FROM raw_rows WHERE sheet='Matchups'"):
        row=json.loads(record["payload"])
        if row.get("Knicks player") not in selected_names:
            continue
        team=row.get("Opponent")
        for field in ("Suspected defender","Alternate defender"):
            name=row.get(field)
            if not name:
                continue
            matches=names.get(normalize_name(name),[])
            if len(matches)!=1:
                unresolved.append({"team":team,"name":name,"role":field,"reason":"Unknown or ambiguous stable player identity"})
            else:
                result.setdefault(team,set()).add(str(matches[0]["id"]))
    return result,unresolved


def scoped_games(store):
    rows=store.rows("""SELECT DISTINCT game_id,date_et,season,opponent
        FROM player_games WHERE team='NYK' AND appeared=1
        AND season IN ('2024-25','2025-26','2026-27') ORDER BY date_et,game_id""")
    return [{**r,"season_type":"Playoffs" if r["game_id"].startswith("004") else "Regular Season"} for r in rows]


def _other_selected_role_games(store,selected):
    """Target only missing roles for the selected players on their actual old teams."""
    if not selected:
        return []
    placeholders=','.join('?' for _ in selected)
    rows=store.rows("""SELECT DISTINCT game_id,date_et,season,team,opponent FROM player_games
        WHERE player_id IN ("""+placeholders+""") AND appeared=1 AND started IS NULL
        AND team<>'NYK' AND opponent<>'NYK' ORDER BY date_et,game_id""",tuple(selected))
    games={}
    for row in rows:
        candidate={"game_id":row["game_id"],"date_et":row["date_et"],"season":row["season"],
            "season_type":"Playoffs" if row["game_id"].startswith("004") else "Regular Season",
            "opponent":row["opponent"],"expected_teams":sorted([row["team"],row["opponent"]]),"selected_roles_only":True}
        old=games.get(row["game_id"])
        if old and (old["expected_teams"]!=candidate["expected_teams"] or old["date_et"]!=candidate["date_et"]):
            raise ValueError("Selected historical role fixtures disagree")
        games[row["game_id"]]=candidate
    return list(games.values())


def _game(game):
    result=dict(game)
    result["game_id"]=str(result.get("game_id",result.get("id","")))
    if not re.fullmatch(r"\d{10}",result["game_id"]):
        raise ValueError("Game ID must preserve all ten digits")
    date=datetime.fromisoformat(str(result["date_et"])[:10]).date()
    if date>datetime.fromisoformat(now()).astimezone(ET).date():
        raise ValueError("Completed game is in the future")
    result.setdefault("season_type","Playoffs" if result["game_id"].startswith("004") else "Regular Season")
    if result["season_type"] not in ("Regular Season","Playoffs"):
        raise ValueError("Unsupported game phase")
    return result


def _compare(row, canonical, minute_tolerance=1/60):
    flags=[]
    old=canonical.get((row["game_id"],row["player_id"]))
    if not old:
        return "source_game_identity_only",flags
    if old.get("team")!=row["team"] or old.get("date_et")!=row["date_et"]:
        raise ValueError("Official game/player identity disagrees with canonical team or date")
    for field in ("pts","reb","ast","fgm","fga","fg3m","fg3a","ftm","fta"):
        if field in row and row[field] is not None and old.get(field) is not None and row[field]!=old[field]:
            flags.append("source_correction_or_conflict_"+field)
    if row.get("minutes") is not None and old.get("minutes") is not None and abs(row["minutes"]-old["minutes"])>minute_tolerance+1e-9:
        flags.append("source_minute_difference")
    if old.get("appeared")!=row.get("appeared"):
        flags.append("source_appearance_difference")
    return ("source_conflict" if flags else "verified_canonical_identity_and_available_fields"),flags


def _parse_box(obj, kind, game, selected, opponents, canonical, source_url, at):
    root_key="boxScoreTraditional" if kind=="starts" else "boxScorePlayerTrack"
    box=obj.get(root_key,{})
    if str(box.get("gameId"))!=game["game_id"]:
        raise ValueError("Official box score belongs to a different game")
    teams=[box.get(side,{}) for side in ("homeTeam","awayTeam")]
    team_names={t.get("teamTricode") for t in teams}
    expected=set(game.get("expected_teams") or ["NYK",game["opponent"]])
    if team_names!=expected:
        raise ValueError("Official game teams disagree with requested Knicks matchup")
    seen=set(); rows=[]; check={"kind":kind,"game_id":game["game_id"],"source_url":source_url,"retrieved_at":at,"starter_counts":{}}
    published=(obj.get("meta") or {}).get("time")
    for team in teams:
        actual=team["teamTricode"]
        people=team.get("players",[])
        if not isinstance(people,list) or not people:
            raise ValueError("Official game player list is unavailable")
        starters=sum(bool(str(p.get("position","")).strip()) for p in people)
        if kind=="starts" and starters!=5:
            raise ValueError("Official starting-position count is not five per team")
        check["starter_counts"][actual]=starters if kind=="starts" else None
        for p in people:
            pid=str(p.get("personId",""))
            if not re.fullmatch(r"\d+",pid) or pid in seen:
                raise ValueError("Missing or duplicate official player ID")
            seen.add(pid)
            if pid not in selected and not (actual==game["opponent"] and pid in opponents):
                continue
            stats=p.get("statistics",{})
            minutes=_minutes(stats.get("minutes"))
            minute_flags=[]
            if ":" in str(stats.get("minutes", "")) and float(str(stats["minutes"]).split(":")[1])==60:
                minute_flags.append("published_noncanonical_minute_clock_60_seconds")
            comment=str(p.get("comment") or "")
            appeared=int(minutes is not None and not comment)
            # DNP rows may contain published zeros; retain raw source separately,
            # while the analyzed observation remains an explicit nonappearance.
            row={"game_id":game["game_id"],"player_id":pid,
                "name":" ".join(x for x in (p.get("firstName"),p.get("familyName")) if x),
                "team":actual,"opponent":next(iter(team_names-{actual})),
                "date_et":game["date_et"],"season":game["season"],"season_type":game["season_type"],
                "appeared":appeared,"source_minutes":stats.get("minutes"),
                "minutes":minutes if appeared else None,"min_seconds":minutes*60 if appeared else None,
                "source_minute_precision":"Official game clock as published; source string retained",
                "source_url":source_url,"retrieved_at":at,"publisher_updated_at":published,
                "comment":comment,"scope":("Selected players' observed roles on actual historical teams; no opponent roster rows"
                    if game.get("selected_roles_only") else "Selected Knicks players and named primary/alternate opponent matchups only")}
            if kind=="starts":
                row.update({target:stats.get(source) if appeared else None for source,target in BOX_FIELDS.items()})
                row.update(position=p.get("position"),started=int(bool(str(p.get("position","")).strip())) if appeared else None)
                row["role_validation_status"]=("verified_official_game_identity_and_five_starters_per_team"
                    if appeared else "not_applicable_nonappearance")
            else:
                row.update({key:value if appeared else None for key,value in stats.items() if key!="minutes"})
                row["ast"]=stats.get("assists") if appeared else None
            row["validation_status"],row["flags"]=_compare(row,canonical)
            row["flags"].extend(minute_flags)
            rows.append(row)
    check["scoped_rows"]=len(rows)
    check["source_conflict_rows"]=sum(r["validation_status"]=="source_conflict" for r in rows)
    return rows,[check]


def _parse_passing(obj, games, selected, cohorts, canonical, source_url, at):
    params=obj.get("parameters") or {}
    for field,expected in (("Season",games[0]["season"]),("SeasonType",games[0]["season_type"]),("PtMeasureType","Passing")):
        if field in params and params[field]!=expected:
            raise ValueError("Official passing response has a different requested scope: "+field)
    for field in ("DateFrom","DateTo"):
        if field in params:
            value=params[field]
            try:
                returned=datetime.strptime(str(value),"%m/%d/%Y").date().isoformat()
            except ValueError as exc:
                raise ValueError("Official passing response did not honor the date filter") from exc
            if returned!=games[0]["date_et"]:
                raise ValueError("Official passing response has a different requested date")
    sets=obj.get("resultSets",[])
    if len(sets)!=1:
        raise ValueError("Expected one official passing result set")
    table=sets[0]
    headers=table.get("headers",[])
    if len(headers)!=len(set(headers)):
        raise ValueError("Duplicate official passing headers")
    rows=[]; seen=set(); checks=[]
    # Only scoped player/date/game candidates may be joined. Current source team
    # labels are retained as metadata, never used to infer historical affiliation.
    for values in table.get("rowSet",[]):
        raw=dict(zip(headers,values))
        pid=str(raw.get("PLAYER_ID",""))
        candidates=[]
        for game in games:
            old=canonical.get((game["game_id"],pid))
            if old and (pid in selected or (old["team"]==game["opponent"] and pid in cohorts.get(game["opponent"],set()))):
                candidates.append((game,old))
        if not candidates:
            continue
        if len(candidates)!=1:
            raise ValueError("Ambiguous player/date passing join")
        game,old=candidates[0]
        if raw.get("GP")!=1:
            raise ValueError("Date-filtered passing observation is not one game")
        key=(game["game_id"],pid)
        if key in seen:
            raise ValueError("Duplicate scoped passing observation")
        seen.add(key)
        if raw.get("AST") is None or old.get("ast") is None or raw["AST"]!=old["ast"]:
            raise ValueError("Single-date passing assists do not match canonical game")
        minutes=_minutes(raw.get("MIN"))
        if minutes is None or old.get("minutes") is None or abs(minutes-old["minutes"])>0.1+1e-9:
            raise ValueError("Single-date passing minutes do not match published precision")
        source_team=raw.get("TEAM_ABBREVIATION")
        row={"game_id":game["game_id"],"player_id":pid,"name":raw.get("PLAYER_NAME"),
            "team":old["team"],"opponent":old["opponent"],"date_et":game["date_et"],
            "season":game["season"],"season_type":game["season_type"],"appeared":old["appeared"],
            "source_minutes":raw.get("MIN"),"minutes":minutes,"min_seconds":minutes*60,
            "source_minute_precision":"Published tracking rounded decimal minutes; validated within0.1minute",
            "rawSource_TEAM_ID":str(raw["TEAM_ID"]) if raw.get("TEAM_ID") is not None else None,
            "rawSource_TEAM_ABBREVIATION":source_team,"SOURCE_TEAM_LABEL_DIFFERS":source_team!=old["team"],
            "source_url":source_url,"retrieved_at":at,"publisher_updated_at":None,
            "validation_status":"verified_unique_player_date_assists_and_published_minutes",
            "flags":["publisher_team_label_differs_from_historical_team"] if source_team!=old["team"] else [],
            "scope":"One-date GP=1 opportunity observation; not a season mean"}
        row.update({k:v for k,v in raw.items() if k not in ("PLAYER_ID","PLAYER_NAME","TEAM_ID","TEAM_ABBREVIATION","MIN")})
        rows.append(row)
    checks.append({"kind":"passing","date_et":games[0]["date_et"],"scoped_rows":len(rows),
        "source_team_label_differences":sum(r["SOURCE_TEAM_LABEL_DIFFERS"] for r in rows),
        "source_url":source_url,"retrieved_at":at})
    return rows,checks


def _read_capture(root, record):
    relative=Path(record["path"])
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts)!=2 or relative.parts[0]!="captures":
        raise ValueError("Unsafe opportunity capture path")
    path=root/relative
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Opportunity capture links are forbidden")
    raw=path.read_bytes()
    if digest(raw)!=record["sha256"]:
        raise ValueError("Opportunity capture checksum mismatch")
    table=json.loads(raw)
    if table["key"]!=record["key"]:
        raise ValueError("Opportunity capture key mismatch")
    return table


def load_opportunity_reference(store):
    root=_root(store)
    output={"tracking_games":[],"passing_games":[],"starts":[],"sources":[],"checks":[],"errors":[],"coverage":{},"status":"not_collected"}
    if not (root/"latest.json").exists():
        return output
    index=json.loads((root/"latest.json").read_text())
    output.update({key:index.get(key) for key in ("errors","coverage","status","attempted_at")})
    output["errors"]=list(output.get("errors") or [])
    for record in index.get("tables",[]):
        try:
            capture=_read_capture(root,record)
            kind=capture["kind"]
            name={"tracking":"tracking_games","passing":"passing_games","starts":"starts"}[kind]
            rows=[{**r,"refresh_status":record.get("refresh_status","success")} for r in capture["rows"]]
            if kind=="starts":
                for row in rows:
                    # Position/role is an independently observed field. A
                    # discrepancy in rebounds or minute precision does not erase
                    # the source's five-starter, game/player/team role evidence.
                    row["role_validation_status"]=("verified_official_game_identity_and_five_starters_per_team"
                        if row.get("appeared")==1 and row.get("started") in (0,1) else "not_applicable_nonappearance")
            output[name].extend(rows)
            output["checks"].extend(capture.get("checks",[]))
            output["sources"].append({k:capture.get(k) for k in ("key","kind","source_url","retrieved_at","season","season_type","date_et","game_id")} | {"rows":len(rows),"refresh_status":record.get("refresh_status","success")})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            output["errors"].append({"source":"cache_integrity","key":record.get("key"),"error":str(exc)})
    for name in ("tracking_games","passing_games","starts"):
        output[name].sort(key=lambda r:(r["date_et"],r["game_id"],r["player_id"]))
    return output


def collect_opportunity_reference(store, games=None, max_games=None, cohort_by_team=None, include_passing=True, force=False, include_other_team_starts=True):
    """Fetch and validate scoped observations; caller holds the maintenance lock.

    Repeated completed-game sources are cached. Passing uses only verified unique
    stable-player/date joins. Source failures retain older valid captures and are
    visible in the index; no canonical records or live package pointers change.
    """
    from .maintenance import fetch_json
    root=_root(store); root.mkdir(parents=True,exist_ok=True)
    games=[_game(g) for g in (scoped_games(store) if games is None else games)]
    games=sorted({g["game_id"]:g for g in games}.values(),key=lambda g:(g["date_et"],g["game_id"]),reverse=True)
    if max_games is not None:
        games=games[:max_games]
    selected=set(map(str,store.config.get("organized_players",[])))
    derived,unresolved=matchup_cohort(store)
    cohorts={team:set(map(str,ids)) for team,ids in (cohort_by_team or derived).items()}
    # Verified selected-player career observations add playoff identities that are
    # intentionally absent from the regular-season canonical DB. On overlap the
    # canonical rows remain the primary comparison record.
    from .career_reference import load_career_reference
    career=load_career_reference(store)
    canonical={(r["game_id"],r["player_id"]):r for r in career.get("rows",[])}
    canonical.update({(r["game_id"],r["player_id"]):r for r in store.rows("SELECT game_id,player_id,team,opponent,date_et,appeared,minutes,pts,reb,ast,fgm,fga,fg3m,fg3a,ftm,fta FROM player_games")})
    old=json.loads((root/"latest.json").read_text()) if (root/"latest.json").exists() else {"tables":[]}
    entries={r["key"]:dict(r) for r in old.get("tables",[])}
    specs=[]
    for game in games:
        gid=game["game_id"]
        specs.extend([{"key":"tracking:"+gid,"kind":"tracking","game":game,"source_url":"https://stats.nba.com/stats/boxscoreplayertrackv3?"+urlencode({"GameID":gid})},
            {"key":"starts:"+gid,"kind":"starts","game":game,"source_url":"https://stats.nba.com/stats/boxscoretraditionalv3?"+urlencode({"GameID":gid,"StartPeriod":0,"EndPeriod":0,"StartRange":0,"EndRange":0,"RangeType":0})}])
    role_games=_other_selected_role_games(store,selected) if include_other_team_starts else []
    existing={g["game_id"] for g in games}
    role_games=[_game(g) for g in role_games if g["game_id"] not in existing]
    for game in role_games:
        specs.append({"key":"starts:"+game["game_id"],"kind":"starts","game":game,
            "source_url":"https://stats.nba.com/stats/boxscoretraditionalv3?"+urlencode({"GameID":game["game_id"],"StartPeriod":0,"EndPeriod":0,"StartRange":0,"EndRange":0,"RangeType":0})})
    if include_passing:
        by_date={}
        for g in games:
            by_date.setdefault((g["season"],g["season_type"],g["date_et"]),[]).append(g)
        for (season,phase,date),group in by_date.items():
            md=datetime.fromisoformat(date).strftime("%m/%d/%Y")
            params={"LastNGames":0,"LeagueID":"00","Month":0,"OpponentTeamID":0,"PerMode":"PerGame","PlayerOrTeam":"Player","PtMeasureType":"Passing","Season":season,"SeasonType":phase,"TeamID":0,"DateFrom":md,"DateTo":md}
            specs.append({"key":"passing:"+season+":"+phase+":"+date,"kind":"passing","games":group,"source_url":"https://stats.nba.com/stats/leaguedashptstats?"+urlencode(params)})
    todo=[]; cached=0; errors=[]; captures=[]
    for spec in specs:
        if spec["key"] in entries and not force:
            try:
                prior=_read_capture(root,entries[spec["key"]])
                # A changed named matchup cohort needs a new scoped capture.
                if prior.get("cohort_hash")==digest([sorted(selected),{k:sorted(v) for k,v in cohorts.items()}]):
                    entries[spec["key"]]["refresh_status"]="cached_verified"
                    if spec["kind"]=="starts":
                        for row in prior.get("rows",[]):
                            canonical.setdefault((row["game_id"],row["player_id"]),row)
                    cached+=1; continue
            except (OSError,ValueError,KeyError):
                pass
        todo.append(spec)

    def request(spec):
        obj=fetch_json(spec["source_url"],store.config)
        at=now()
        if spec["kind"]=="passing":
            rows,checks=_parse_passing(obj,spec["games"],selected,cohorts,canonical,spec["source_url"],at)
            context=spec["games"][0]
        else:
            context=spec["game"]
            opponent_ids=set() if context.get("selected_roles_only") else cohorts.get(context["opponent"],set())
            rows,checks=_parse_box(obj,spec["kind"],context,selected,opponent_ids,canonical,spec["source_url"],at)
        return {"key":spec["key"],"kind":spec["kind"],"source_url":spec["source_url"],"retrieved_at":at,
            **{k:context.get(k) for k in ("season","season_type","date_et","game_id")},"rows":rows,"checks":checks,
            "cohort_hash":digest([sorted(selected),{k:sorted(v) for k,v in cohorts.items()}]),
            "raw_source":obj,"meaning":"Retrospectively retrieved official game record; not evidence of historical pregame knowledge"}

    # Fetch observed box controls before passing. This supplies independently
    # identified opponent playoff rows without applying a current roster or
    # treating the selected Knicks' career table as the opponent's production.
    for phase in ([s for s in todo if s["kind"]!="passing"],[s for s in todo if s["kind"]=="passing"]):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(request,spec):spec for spec in phase}
            for future in as_completed(futures):
                spec=futures[future]
                try:
                    table=future.result()
                    filename=digest([table["key"],table["retrieved_at"],table["rows"]])+".json"
                    target=root/"captures"/filename
                    _atomic(target,table)
                    entries[spec["key"]]={"key":spec["key"],"kind":table["kind"],"path":"captures/"+filename,
                        "sha256":digest(target.read_bytes()),"refresh_status":"success","retrieved_at":table["retrieved_at"],"rows":len(table["rows"])}
                    captures.append({"key":table["key"],"rows":len(table["rows"])})
                    if table["kind"]=="starts":
                        for row in table["rows"]:
                            canonical.setdefault((row["game_id"],row["player_id"]),row)
                except Exception as exc:
                    errors.append({"key":spec["key"],"source_url":spec["source_url"],"error_type":type(exc).__name__,"error":str(exc)[:250],"retained_previous":spec["key"] in entries})
                    if spec["key"] in entries:
                        entries[spec["key"]]["refresh_status"]="retained_after_failure"
    report={"status":"success" if not errors else "partial" if captures or cached else "failed","attempted_at":now(),
        "tables":sorted(entries.values(),key=lambda x:x["key"]),"errors":errors,
        "coverage":{"requested_games":len(games),"requested_sources":len(specs),"new_captures":len(captures),"cached_sources":cached,
            "requested_selected_role_only_games":len(role_games),"verified_career_comparison_rows":len(career.get("rows",[])),
            "selected_player_ids":sorted(selected),"named_matchup_players":{k:sorted(v) for k,v in cohorts.items()},"unresolved_matchup_names":unresolved,
            "first_game":min((g["date_et"] for g in games),default=None),"through_date":max((g["date_et"] for g in games),default=None),
            "scope":"Selected eight Knicks and only workbook-named primary/alternate opponent matchups; actual historical teams retained"}}
    _atomic(root/"runs"/(uuid.uuid4().hex+".json"),report)
    _atomic(root/"latest.json",report)
    return {k:v for k,v in report.items() if k!="tables"}
