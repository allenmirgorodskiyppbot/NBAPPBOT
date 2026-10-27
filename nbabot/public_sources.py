"""Best-effort publisher JSON adapters. Missing reports are explicitly unknown.

NBA is the authority for player IDs and rosters. ESPN identities are accepted only
through an unambiguous exact full-name + current-team crosswalk, with evidence retained.
No private endpoints, offer scraping, or inferred healthy-player reports.
"""
from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlencode

from .core import ET, dumps, now, stamp
from .maintenance import fetch_json

NBA_TEAMS={name:str(1610612737+i) for i,name in enumerate([
    "ATL","BOS","CLE","NOP","CHI","DAL","DEN","GSW","HOU","LAC","LAL","MIA","MIL","MIN","BKN",
    "NYK","ORL","IND","PHI","PHX","POR","SAC","SAS","OKC","TOR","UTA","MEM","WAS","DET","CHA"])}
ESPN_TEAMS={name:str(i+1) for i,name in enumerate([
    "ATL","BOS","NOP","CHI","CLE","DAL","DEN","DET","GSW","HOU","IND","LAC","LAL","MIA","MIL",
    "MIN","BKN","NYK","ORL","PHI","PHX","POR","SAC","SAS","OKC","UTA","WAS","TOR","MEM","CHA"])}
ALIASES={"NY":"NYK","GS":"GSW","NO":"NOP","SA":"SAS","WSH":"WAS","UTAH":"UTA","PHO":"PHX"}
ESPN="https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


def normalize_name(value):
    text=unicodedata.normalize("NFKD",str(value))
    return re.sub(r"[^a-z0-9]","","".join(c for c in text if not unicodedata.combining(c)).lower())


def exact_identity(name,team,rosters):
    matches=[r for r in rosters if r["team"]==team and normalize_name(r["name"])==normalize_name(name)]
    # Do not fuzzy-match surnames, jersey numbers or a workbook's obsolete team.
    return matches[0] if len(matches)==1 else None


def envelope(records,source,at,coverage=None):
    return {"complete":True,"verified_at":at,"coverage_cutoff":coverage or at,"source_url":source,"records":records}


def request_many(requests,config):
    results={}; errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        running={pool.submit(fetch_json,url,config):key for key,url in requests.items()}
        for future in as_completed(running):
            key=running[future]
            try:
                results[key]=future.result()
            except Exception as e:
                # No response URL/query/header values in diagnostics.
                errors.append({"source":key,"error":type(e).__name__,
                    "http_status":getattr(getattr(e,"response",None),"status_code",None)})
    return results,errors


def relevant_games(store,sections,at):
    games=(sections.get("schedule") or {}).get("records",[])
    if not games:
        games=store.rows("SELECT * FROM games")
    return sorted([g for g in games if (g.get("home")=="NYK" or g.get("away")=="NYK") and
        g.get("status")=="scheduled" and g.get("tipoff") and stamp(g["tipoff"])>at],key=lambda g:stamp(g["tipoff"]))[:3]


def nba_roster_rows(obj,team,at,url):
    result_sets=obj.get("resultSets",[])
    result=next((r for r in result_sets if r.get("name")=="CommonTeamRoster"),None)
    if result is None:
        raise ValueError("NBA roster response has no CommonTeamRoster")
    rows=[]
    for values in result["rowSet"]:
        r=dict(zip(result["headers"],values))
        if r.get("TeamID") and str(r["TeamID"])!=NBA_TEAMS[team]:
            raise ValueError("NBA roster response belongs to another team")
        if not r.get("PLAYER_ID") or not r.get("PLAYER"):
            raise ValueError("NBA roster missing stable ID/name")
        rows.append({"player_id":str(r["PLAYER_ID"]),"name":r["PLAYER"],"team":team,
            "observed_at":at,"retrieved_at":at,"source_url":url,"active":True,
            "position":r.get("POSITION"),"birth_date":r.get("BIRTH_DATE"),"raw":r,
            "availability_note":"Roster membership is not proof of game availability"})
    if not rows:
        raise ValueError("Empty current NBA roster")
    return rows


def roster_crosswalk(obj,team,nba_rosters):
    athletes=obj.get("athletes",[])
    if not isinstance(athletes,list):
        raise ValueError("Unexpected ESPN roster contract")
    found={}; unresolved=[]
    for a in athletes:
        identity=exact_identity(a.get("displayName",a.get("fullName","")),team,nba_rosters)
        if identity and a.get("id"):
            found[str(a["id"])]=identity["player_id"]
            identity["espn_id"]=str(a["id"])
            identity["identity_evidence"]={"method":"unique_exact_full_name_and_current_NBA_team",
                "espn_source":ESPN+"/teams/"+ESPN_TEAMS[team]+"/roster","nba_source":identity["source_url"]}
        else:
            unresolved.append({"espn_id":a.get("id"),"name":a.get("displayName"),"team":team})
    return found,unresolved


def parse_injuries(obj,games,rosters,crosswalk,at):
    if not isinstance(obj.get("injuries"),list):
        raise ValueError("Unexpected ESPN injuries contract")
    teams={g[t] for g in games for t in ("home","away")}
    records=[]; unresolved=[]
    for group in obj["injuries"]:
        raw_team=group.get("abbreviation",(group.get("team") or {}).get("abbreviation"))
        if not raw_team:
            raw_team=next((t for t,i in ESPN_TEAMS.items() if i==str(group.get("id"))),None)
        team=ALIASES.get(raw_team,raw_team)
        if team not in teams:
            continue
        for injury in group.get("injuries",[]):
            athlete=injury.get("athlete",{})
            espn_id=str(athlete.get("id",""))
            pid=crosswalk.get(espn_id)
            if not pid:
                identity=exact_identity(athlete.get("displayName",athlete.get("fullName","")),team,rosters)
                pid=identity["player_id"] if identity else None
            if not pid:
                unresolved.append({"team":team,"espn_id":espn_id,"name":athlete.get("displayName")})
                continue
            status=str(injury.get("status", "unknown")).lower()
            normalized={"out":"reported_out","inactive":"reported_inactive","questionable":"questionable","doubtful":"doubtful",
                "day-to-day":"questionable","day to day":"questionable","probable":"probable"}.get(status,"unknown")
            # Report publication is preserved separately from when this response was observed.
            published=injury.get("date")
            if published:
                published=stamp(published)
                if published>at:
                    raise ValueError("Future injury report publication")
            for game in games:
                if team not in (game["home"],game["away"]):
                    continue
                records.append({"player_id":pid,"game_id":game["id"],"team":team,"status":normalized,
                    "observed_at":at,"retrieved_at":at,"published_at":published,"source_url":ESPN+"/injuries",
                    "espn_id":espn_id,"raw_status":injury.get("status"),"raw":injury,
                    "game_specific_confirmed":False,
                    "note":"Published injury listing; not an official game-specific clearance. Recheck before tipoff.",
                    "expected_return":(injury.get("details") or {}).get("returnDate")})
    return records,unresolved


def parse_news(obj,team,crosswalk,at):
    if not isinstance(obj.get("articles"),list):
        raise ValueError("Unexpected ESPN news contract")
    records=[]
    for article in obj["articles"]:
        published=stamp(article["published"])
        if published>at:
            raise ValueError("Future article publication")
        ids=[str(c["athleteId"]) for c in article.get("categories",[]) if c.get("type")=="athlete" and c.get("athleteId")]
        url=((article.get("links") or {}).get("web") or {}).get("href")
        if not url or not article.get("headline"):
            raise ValueError("Article lacks source evidence/headline")
        records.append({"id":"espn:"+str(article.get("id",article.get("dataSourceIdentifier",url))),
            "headline":article["headline"],"published_at":published,"retrieved_at":at,"source_url":url,
            "players":sorted({crosswalk[i] for i in ids if i in crosswalk}),"teams":[team],
            "unresolved_espn_player_ids":[i for i in ids if i not in crosswalk],
            "confirmation":"unconfirmed","confirmation_basis":"Publisher article; factual assertions require review",
            "raw":article})
    return records


def parse_lineup(obj,game,at):
    header=obj.get("header")
    if not header or not header.get("id"):
        raise ValueError("ESPN game summary missing header identity")
    summary_teams={ALIASES.get((c.get("team") or {}).get("abbreviation"),(c.get("team") or {}).get("abbreviation"))
                   for comp in header.get("competitions",[]) for c in comp.get("competitors",[])}
    if summary_teams!={game["home"],game["away"]}:
        raise ValueError("Wrong team join in game summary")
    event_date=header.get("competitions",[{}])[0].get("date")
    if event_date and abs((datetime.fromisoformat(stamp(event_date))-datetime.fromisoformat(stamp(game["tipoff"]))).total_seconds())>3600:
        raise ValueError("ESPN game time conflicts with NBA schedule")
    # A retrospective box-score starter flag is not a pregame confirmed lineup.
    return [{"game_id":game["id"],"team":team,"observed_at":at,"retrieved_at":at,
        "confirmation":"unknown","starters":[],"source_url":ESPN+"/summary?event="+str(header["id"]),
        "note":"Pregame confirmed starting lineup unavailable from this adapter; box-score starters are not backfilled."}
        for team in (game["home"],game["away"])]


def collect_context(store,sections):
    at=now(); games=relevant_games(store,sections,at)
    teams=sorted({g[t] for g in games for t in ("home","away")})
    if not games:
        return {},[{"source":"public_context","error":"No upcoming Knicks fixtures to verify"}]
    requests={}; roster_urls={}
    for team in teams:
        if team not in NBA_TEAMS or team not in ESPN_TEAMS:
            return {},[{"source":"public_context","error":"Unrecognized team identity"}]
        url="https://stats.nba.com/stats/commonteamroster?"+urlencode({"LeagueID":"00","Season":store.config["season"],"TeamID":NBA_TEAMS[team]})
        requests["nba_roster:"+team]=url; roster_urls[team]=url
        requests["espn_roster:"+team]=ESPN+"/teams/"+ESPN_TEAMS[team]+"/roster"
        requests["espn_news:"+team]=ESPN+"/news?team="+ESPN_TEAMS[team]+"&limit=20"
    requests["espn_injuries"]=ESPN+"/injuries"
    # Resolve summary event identities through the publisher's dated scoreboard, not guessed NBA IDs.
    for game in games:
        requests["espn_scoreboard:"+game["date_et"]]=ESPN+"/scoreboard?dates="+game["date_et"].replace("-","")
    results,errors=request_many(requests,store.config)
    at=now()
    output={}; rosters=[]; crosswalk={}; unresolved=[]
    for team in teams:
        try:
            rosters.extend(nba_roster_rows(results["nba_roster:"+team],team,at,roster_urls[team]))
        except Exception as e:
            errors.append({"source":"rosters","error":type(e).__name__+": current NBA roster unavailable for "+team})
    if len({r["team"] for r in rosters})==len(teams):
        for team in teams:
            try:
                mapping,missing=roster_crosswalk(results["espn_roster:"+team],team,rosters)
                crosswalk.update(mapping); unresolved.extend(missing)
            except Exception:
                errors.append({"source":"identity_crosswalk","error":"ESPN roster unavailable for "+team})
        output["rosters"]=envelope(rosters,"https://stats.nba.com/stats/commonteamroster",at)
    try:
        injuries,missing=parse_injuries(results["espn_injuries"],games,rosters,crosswalk,at)
        if missing:
            raise ValueError("Unresolved current injury identities; source data retained for review")
        output["injuries"]=envelope(injuries,ESPN+"/injuries",at)
    except Exception as e:
        errors.append({"source":"injuries","error":type(e).__name__})
    news=[]; news_ok=True
    for team in teams:
        try:
            news.extend(parse_news(results["espn_news:"+team],team,crosswalk,at))
        except Exception as e:
            news_ok=False; errors.append({"source":"news","error":type(e).__name__+": "+team})
    if news_ok:
        unique={}
        for r in news:
            if r["id"] in unique:
                previous=unique[r["id"]]
                for field in ("teams","players","unresolved_espn_player_ids"):
                    r[field]=sorted(set(r[field])|set(previous[field]))
            unique[r["id"]]=r
        output["news"]=envelope(list(unique.values()),ESPN+"/news",at)
    summary_requests={}
    for game in games:
        matches=[]
        for event in results.get("espn_scoreboard:"+game["date_et"],{}).get("events",[]):
            for comp in event.get("competitions",[]):
                competitors=comp.get("competitors",[])
                event_teams={ALIASES.get(c["team"]["abbreviation"],c["team"]["abbreviation"]) for c in competitors}
                if event_teams=={game["home"],game["away"]}:
                    matches.append(event)
        if len(matches)==1:
            summary_requests[game["id"]]=ESPN+"/summary?event="+str(matches[0]["id"])
        else:
            errors.append({"source":"lineups","error":"No unique publisher event mapping for "+game["id"]})
    summaries,summary_errors=request_many(summary_requests,store.config)
    at=now()
    errors.extend(summary_errors); lineups=[]
    for game in games:
        try:
            lineups.extend(parse_lineup(summaries[game["id"]],game,at))
        except Exception as e:
            errors.append({"source":"lineups","error":type(e).__name__+": "+game["id"]})
    if len(lineups)==len(games)*2:
        output["lineups"]=envelope(lineups,ESPN+"/summary",at)
    # Archive the exact fetched publisher data, including unresolved identities, even on failure.
    archive=store.root/"publisher_raw"; archive.mkdir(exist_ok=True)
    (archive/(at.replace(":","-")+".json")).write_text(dumps({"retrieved_at":at,"responses":results,
        "summaries":summaries,"unresolved_roster_aliases":unresolved,"errors":errors}))
    return output,errors
