"""Verified NBA measured rotations for the selected Knicks and named matchups.

The raw rotation response is validated using all participants so a five-player
lineup really covers every game interval. Exported participant rows remain scoped
and are never interpreted as individual teammate-absence production.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlencode

from .core import digest, dumps, now, stamp


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    try:
        temp.write_text(dumps(value))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _scoped_ids(store, cohort_by_team=None):
    selected = {str(v) for v in store.config.get("organized_players", [])}
    if not selected:
        raise ValueError("Explicit selected Knicks players are required")
    if cohort_by_team is not None:
        return selected, {str(t): {str(p) for p in ids} for t, ids in cohort_by_team.items()}
    names = defaultdict(list)
    for player in store.rows("SELECT id,name FROM players"):
        names[player["name"].casefold()].append(player["id"])
    selected_names = {p["name"] for p in store.rows("SELECT id,name FROM players") if p["id"] in selected}
    opponents = defaultdict(set)
    for record in store.rows("SELECT payload FROM raw_rows WHERE sheet='Matchups'"):
        r = json.loads(record["payload"])
        if r.get("Knicks player") not in selected_names:
            continue
        team = r.get("Opponent")
        if not team:
            continue
        for field in ("Suspected defender", "Alternate defender"):
            name = r.get(field)
            matches = names.get(str(name).casefold(), [])
            if len(matches) == 1:
                opponents[team].add(str(matches[0]))
    return selected, dict(opponents)


def _games(store, games=None):
    if games is None:
        games = store.rows("""SELECT DISTINCT game_id,date_et,season,opponent FROM player_games
            WHERE team='NYK' AND appeared=1 AND season>='2024-25' ORDER BY date_et DESC,game_id""")
    output = []
    seen = set()
    for source in games:
        g = dict(source)
        game_id = str(g.get("game_id", g.get("id", "")))
        if not re.fullmatch(r"\d{10}", game_id) or not game_id.startswith(("002", "004")):
            raise ValueError("Verified regular-season or playoff game ID is required")
        if game_id in seen:
            continue
        seen.add(game_id)
        g["game_id"] = game_id
        g["phase"] = "Playoffs" if game_id.startswith("004") else "Regular Season"
        if g.get("opponent") is None:
            g["opponent"] = g.get("away") if g.get("home") == "NYK" else g.get("home")
        if not g.get("opponent") or g["opponent"] == "NYK":
            raise ValueError("Knicks fixture needs an opposing team")
        output.append(g)
    # Begin with the user's initial Magic matchup, then newest other fixtures.
    return sorted(output, key=lambda g: (g.get("opponent") != "ORL", -int(str(g.get("date_et", "0000-00-00")).replace("-", "")), g["game_id"]))


def _ticks(value):
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid rotation timestamp") from exc
    if not d.is_finite() or d != d.to_integral_value() or d < 0:
        raise ValueError("Rotation timestamps must be nonnegative tenths-of-seconds")
    return int(d)


def _minutes_seconds(value):
    """Clock-format source precision is preserved, not invented from rounded MIN."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+:\d{2}(\.\d+)?", value):
        raise ValueError("Box score needs an explicit source MM:SS minute value")
    minutes, seconds = value.split(":")
    if Decimal(seconds) >= 60:
        raise ValueError("Invalid source box-score minute clock")
    resolution = Decimal(1) if "." not in seconds else Decimal(10) ** -len(seconds.split(".")[1])
    return Decimal(minutes) * 60 + Decimal(seconds), resolution


def validate_rotation(rotation, box, game, scoped_ids):
    """Return scoped rows only when full five-player and box-minute checks pass.

    Rotation clocks are tenths of elapsed playing seconds. Official MM:SS box
    minutes are rounded to whole seconds, so agreement is assessed against half
    the published precision (0.5 seconds), without modifying either source.
    """
    game_id = str(game["game_id"])
    data = box.get("boxScoreTraditional", {})
    if str(data.get("gameId")) != game_id:
        raise ValueError("Box-score game ID disagrees with requested game")
    teams = {}
    for side in ("homeTeam", "awayTeam"):
        team = data.get(side, {})
        tid = str(team.get("teamId", ""))
        abbr = team.get("teamTricode")
        if not re.fullmatch(r"\d+", tid) or not abbr or tid in teams:
            raise ValueError("Invalid or duplicate box-score team identity")
        teams[tid] = team
    if {t["teamTricode"] for t in teams.values()} != {"NYK", game["opponent"]}:
        raise ValueError("Actual box-score teams disagree with Knicks fixture")
    tables = rotation.get("resultSets")
    if not isinstance(tables, list) or len(tables) != 2:
        raise ValueError("Expected both official rotation teams")
    all_rows, by_team, seen = [], defaultdict(list), set()
    for table in tables:
        headers = table.get("headers", [])
        required = {"GAME_ID", "TEAM_ID", "PERSON_ID", "IN_TIME_REAL", "OUT_TIME_REAL"}
        if not required.issubset(headers) or len(headers) != len(set(headers)):
            raise ValueError("Rotation source lacks required stable keys/clocks")
        for values in table.get("rowSet", []):
            if len(values) != len(headers):
                raise ValueError("Rotation source row width differs from headers")
            raw = dict(zip(headers, values))
            if str(raw["GAME_ID"]) != game_id:
                raise ValueError("Rotation source game ID disagrees with request")
            tid, pid = str(raw["TEAM_ID"]), str(raw["PERSON_ID"])
            if tid not in teams or not re.fullmatch(r"\d+", pid):
                raise ValueError("Rotation player/team identifier is invalid")
            a, b = _ticks(raw["IN_TIME_REAL"]), _ticks(raw["OUT_TIME_REAL"])
            if b < a:
                raise ValueError("Rotation stint has negative duration")
            key = (tid, pid, a, b)
            if key in seen:
                raise ValueError("Duplicate rotation segment")
            seen.add(key)
            row = {"raw": raw, "team_id": tid, "player_id": pid, "start_tick": a, "end_tick": b}
            all_rows.append(row)
            by_team[tid].append(row)
    if set(by_team) != set(teams) or not all_rows:
        raise ValueError("Both rotation teams require observations")
    ends = {max(r["end_tick"] for r in rows) for rows in by_team.values()}
    if len(ends) != 1:
        raise ValueError("Rotation teams disagree on game duration")
    end = ends.pop()
    if end < 28800 or (end - 28800) % 3000:
        raise ValueError("Rotation duration does not match regulation plus complete overtime periods")
    checks, intervals, starts = [], [], []
    sums = defaultdict(int)
    for tid, rows in by_team.items():
        team = teams[tid]
        positive = [r for r in rows if r["end_tick"] > r["start_tick"]]
        boundaries = sorted({0, end, *[v for r in positive for v in (r["start_tick"], r["end_tick"])]})
        for a, b in zip(boundaries, boundaries[1:]):
            active = [r["player_id"] for r in positive if r["start_tick"] <= a and r["end_tick"] >= b]
            if len(active) != 5 or len(set(active)) != 5:
                raise ValueError("Five-player rotation coverage failed at elapsed tick " + str(a))
            selected = sorted(set(active) & scoped_ids)
            if selected:
                intervals.append({"game_id": game_id, "team_id": tid, "team": team["teamTricode"],
                    "opponent": next(t["teamTricode"] for t_id, t in teams.items() if t_id != tid),
                    "season": game.get("season"), "date_et": game.get("date_et"), "phase": game.get("phase"),
                    "in_seconds": str(Decimal(a) / 10), "out_seconds": str(Decimal(b) / 10),
                    "duration_seconds": str(Decimal(b-a) / 10), "scoped_player_ids": selected,
                    "full_lineup_size": 5, "lineup_key": digest([tid, sorted(active)])[:24],
                    "scope": "Measured five-player team context; not individual teammate-absence production"})
        box_players = {str(p["personId"]): p for p in team.get("players", [])}
        if len(box_players) != len(team.get("players", [])):
            raise ValueError("Duplicate box-score player identity")
        for r in rows:
            if r["player_id"] not in box_players:
                raise ValueError("Rotation player absent from actual game box score")
            sums[(tid, r["player_id"])] += r["end_tick"] - r["start_tick"]
        for pid, player in box_players.items():
            value = player.get("statistics", {}).get("minutes")
            if not value:
                if sums[(tid,pid)]:
                    raise ValueError("Rotation minutes exist without a played box-score minute clock")
                continue
            seconds, precision = _minutes_seconds(value)
            actual = Decimal(sums[(tid,pid)]) / 10
            difference = actual - seconds
            if abs(difference) > precision / 2:
                raise ValueError("Rotation minutes disagree with source box precision for player " + pid)
            initial = any(r["player_id"] == pid and r["start_tick"] == 0 and r["end_tick"] > 0 for r in positive)
            starter = bool(str(player.get("position", "")))
            if initial != starter:
                raise ValueError("Rotation opening lineup disagrees with observed box-score starters")
            checks.append({"game_id": game_id, "team_id": tid, "player_id": pid,
                "rotation_seconds": str(actual), "box_minutes_source": value,
                "difference_seconds": str(difference), "source_resolution_seconds": str(precision),
                "status": "matched_within_published_box_precision", "scoped": pid in scoped_ids})
            # An explicit zero clock without observed elapsed rotation time is
            # not evidence of a played bench appearance (DNP/zero-time cases).
            if pid in scoped_ids and (actual>0 or seconds>0):
                starts.append({"game_id": game_id, "player_id": pid, "team": team["teamTricode"],
                    "opponent": next(t["teamTricode"] for t_id,t in teams.items() if t_id != tid),
                    "season": game.get("season"), "date_et": game.get("date_et"), "phase": game.get("phase"),
                    "started": int(initial), "basis": "Measured opening rotation and observed box starter position agree"})
    stints = []
    indices = defaultdict(int)
    for r in sorted(all_rows, key=lambda r:(r["team_id"],r["player_id"],r["start_tick"],r["end_tick"])):
        pid, tid = r["player_id"], r["team_id"]
        if pid not in scoped_ids:
            continue
        indices[(tid,pid)] += 1
        raw = r["raw"]
        stints.append({"game_id": game_id, "player_id": pid, "name": " ".join(str(raw.get(k) or "") for k in ("PLAYER_FIRST","PLAYER_LAST")).strip(),
            "team_id": tid, "team": teams[tid]["teamTricode"],
            "opponent": next(t["teamTricode"] for t_id,t in teams.items() if t_id != tid),
            "season": game.get("season"), "date_et": game.get("date_et"), "phase": game.get("phase"),
            "stint_index": indices[(tid,pid)], "in_time_source": str(raw["IN_TIME_REAL"]),
            "out_time_source": str(raw["OUT_TIME_REAL"]), "in_seconds": str(Decimal(r["start_tick"]) / 10),
            "out_seconds": str(Decimal(r["end_tick"]) / 10), "duration_seconds": str(Decimal(r["end_tick"]-r["start_tick"]) / 10),
            "rotation_units": "IN_TIME_REAL/OUT_TIME_REAL are tenths of elapsed playing seconds",
            "validation_status": "verified_five_player_coverage_and_box_minute_precision"})
    return {"stints": stints, "lineup_intervals": intervals, "starts": starts,
        "checks": [r for r in checks if r["scoped"]], "validation": {"status":"passed", "elapsed_game_seconds":str(Decimal(end)/10),
            "overtime_periods":(end-28800)//3000, "all_participant_minute_checks":len(checks),
            "meaning":"All participants validated internally; only selected players and named matchup participants exported."}}


def load_rotation_reference(store):
    root = store.root / "reference" / "rotations"
    target = root / "latest.json"
    result = {"stints":[], "lineup_intervals":[], "starts":[], "sources":[], "checks":[], "errors":[], "coverage":{}, "status":"not_collected"}
    if not target.exists():
        return result
    try:
        index = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        result["errors"].append({"source":"rotation_cache", "error":str(exc)})
        result["status"] = "failed"
        return result
    result.update({k:index.get(k) for k in ("coverage","status","attempted_at")})
    result["errors"] = list(index.get("errors", []))
    for entry in index.get("games", []):
        try:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts)!=2 or relative.parts[0]!="captures":
                raise ValueError("Unsafe rotation capture path")
            path=root/relative
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Rotation captures must be regular files")
            raw=path.read_bytes()
            if digest(raw)!=entry["sha256"]:
                raise ValueError("Rotation capture checksum mismatch")
            capture=json.loads(raw)
            if capture["game_id"]!=entry["game_id"]:
                raise ValueError("Rotation capture game ID mismatch")
            for key in ("stints","lineup_intervals","starts","checks"):
                result[key].extend(capture["parsed"][key])
            result["sources"].extend(capture["sources"])
        except (OSError,ValueError,KeyError,TypeError) as exc:
            result["errors"].append({"game_id":entry.get("game_id"),"source":"rotation_cache","error":str(exc)})
    for key in ("stints","lineup_intervals","starts"):
        result[key].sort(key=lambda r:(str(r.get("date_et","")),r["game_id"],r.get("player_id",""),int(Decimal(r.get("in_seconds","0"))*10)))
    return result


def collect_rotation_reference(store, games=None, cohort_by_team=None, max_games=None, force=False):
    """Collect verified rotations without mutating canonical game observations."""
    from .maintenance import fetch_json
    selected, opponents = _scoped_ids(store,cohort_by_team)
    fixtures = _games(store,games)
    if max_games is not None:
        fixtures=fixtures[:max_games]
    root=store.root/"reference"/"rotations"
    root.mkdir(parents=True,exist_ok=True)
    latest=root/"latest.json"
    old=json.loads(latest.read_text()) if latest.exists() else {"games":[]}
    entries={r["game_id"]:dict(r) for r in old.get("games",[])}
    errors=[]; captured=[]; reused=[]; cooldown_skipped=[]
    at=now()
    prior_errors={r["game_id"]:dict(r) for r in old.get("errors",[]) if r.get("game_id")}
    for error in prior_errors.values():
        error.setdefault("attempted_at",old.get("attempted_at"))
        error.setdefault("failure_kind","validation" if error.get("error_type") in ("ValueError","JSONDecodeError") else "source")
    retry_hours=store.config.get("historical_retry_hours",24)
    if isinstance(retry_hours,bool) or not isinstance(retry_hours,(int,float)) or not math.isfinite(retry_hours) or retry_hours<0:
        raise ValueError("historical_retry_hours must be nonnegative")
    pending=[]
    for game in fixtures:
        error=prior_errors.get(game["game_id"])
        captured_at=error.get("attempted_at",old.get("attempted_at")) if error else None
        age=None
        try:
            age=(datetime.fromisoformat(at)-datetime.fromisoformat(stamp(captured_at))).total_seconds()/3600 if captured_at else None
        except (TypeError,ValueError):
            pass
        if error and not force and retry_hours and age is not None and 0<=age<retry_hours:
            cooldown_skipped.append(game["game_id"])
        else:
            pending.append(game)
    config={**store.config,"http_timeout":max(5,store.config.get("http_timeout",15))}

    def collect(game):
        gid=game["game_id"]
        scope=selected|opponents.get(game["opponent"],set())
        scope_hash=digest(sorted(scope))
        prior=entries.get(gid)
        if prior and prior.get("scope_sha256")==scope_hash and not force:
            path=root/prior["path"]
            if path.is_file() and not path.is_symlink() and digest(path.read_bytes())==prior["sha256"]:
                return None,prior
        rotation_url="https://stats.nba.com/stats/gamerotation?"+urlencode({"GameID":gid,"LeagueID":"00"})
        box_url="https://stats.nba.com/stats/boxscoretraditionalv3?"+urlencode({"GameID":gid,"StartPeriod":0,"EndPeriod":0,"StartRange":0,"EndRange":0,"RangeType":0})
        rotation=fetch_json(rotation_url,config); rotation_at=now()
        box=fetch_json(box_url,config); box_at=now()
        try:
            parsed=validate_rotation(rotation,box,game,scope)
        except ValueError as exc:
            rejected={"game_id":gid,"game":game,"rotation_source_url":rotation_url,"rotation_retrieved_at":rotation_at,
                "box_source_url":box_url,"box_retrieved_at":box_at,"raw":{"rotation":rotation,"box":box},
                "validation_status":"withheld","validation_error":str(exc)}
            name=gid+"_"+digest(rejected)[:24]+".json"
            _atomic(root/"quarantine"/name,rejected)
            raise ValueError(str(exc)+"; raw response preserved in quarantine/"+name) from exc
        source={"dataset":"measured_player_rotations","game_id":gid,"source_url":rotation_url,"retrieved_at":rotation_at,
            "box_source_url":box_url,"box_retrieved_at":box_at,"phase":game["phase"],"units":"Tenths of elapsed playing seconds; source box minutes retain their own clock precision",
            "scope":"Eight selected Knicks and named primary/alternate matchup participants only; all game participants used internally for validation"}
        for key in ("stints","lineup_intervals","starts","checks"):
            parsed[key]=[{**r,"source_url":rotation_url,"retrieved_at":rotation_at,"box_source_url":box_url,"box_retrieved_at":box_at} for r in parsed[key]]
        capture={"game_id":gid,"game":game,"scope_sha256":scope_hash,"raw":{"rotation":rotation,"box":box},"parsed":parsed,"sources":[source]}
        raw=dumps(capture).encode(); name=gid+"_"+digest(raw)[:24]+".json"
        path=root/"captures"/name
        if not path.exists():_atomic(path,capture)
        entry={"game_id":gid,"path":"captures/"+name,"sha256":digest(path.read_bytes()),"scope_sha256":scope_hash,"retrieved_at":rotation_at,"status":"verified"}
        return capture,entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(collect,g):g for g in pending}
        for future in as_completed(futures):
            game=futures[future]
            try:
                capture,entry=future.result()
                entries[game["game_id"]]=entry
                (captured if capture else reused).append(game["game_id"])
            except Exception as exc:
                errors.append({"game_id":game["game_id"],"opponent":game["opponent"],"date_et":game.get("date_et"),
                    "source":"official_measured_rotations","error_type":type(exc).__name__,"failure_kind":"validation" if isinstance(exc,ValueError) else "source", "attempted_at":now(),"error":str(exc)[:300],"retained_previous":game["game_id"] in entries})
    # Appending a new fixture batch must not erase unresolved source failures
    # from older batches. A fresh successful capture clears that game's failure.
    merged_errors={gid:e for gid,e in prior_errors.items() if gid not in captured}
    merged_errors.update({e["game_id"]:e for e in errors})
    all_errors=sorted(merged_errors.values(),key=lambda r:r["game_id"])
    report={"status":"success" if not all_errors else "partial" if entries else "failed","attempted_at":at,
        "coverage":{"requested_games":len(fixtures),"verified_cached_games":len(entries),"new_games":len(captured),"reused_games":len(reused),
            "scope":"Selected Knicks and named primary/alternate matchup participants; no full roster export","failed_this_run":len(errors),
            "unresolved_cached_failures":len(all_errors),"cooldown_skipped_games":len(cooldown_skipped),"retry_hours":retry_hours},
        "games":sorted(entries.values(),key=lambda r:r["game_id"]),"errors":all_errors,"run_errors":errors}
    _atomic(root/"runs"/(uuid.uuid4().hex+".json"),report)
    _atomic(latest,report)
    return {k:v for k,v in report.items() if k!="games"}
