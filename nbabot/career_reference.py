"""Targeted NBA career box scores and independent season controls.

These are retrospective references, not pregame knowledge vintages. They never
change the canonical league table or silently mix postseason and regular games.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
import uuid

from .core import ET, digest, dumps, now
from .ingest import normalize_game

PHASES = {"SeasonTotalsRegularSeason": "Regular Season", "SeasonTotalsPostSeason": "Playoffs"}
FIELDS = {"PTS": "pts", "REB": "reb", "AST": "ast", "FGM": "fgm", "FGA": "fga",
          "FG3M": "fg3m", "FG3A": "fg3a", "FTM": "ftm", "FTA": "fta",
          "OREB": "oreb", "DREB": "dreb", "STL": "stl", "BLK": "blk", "TOV": "tov", "PF": "pf"}


def _root(store):
    return store.root / "reference" / "career"


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    try:
        temp.write_text(dumps(payload), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 2 or path.parts[0] != "captures":
        raise ValueError("Invalid career capture path")
    full = root / path
    if full.is_symlink() or full.parent.is_symlink():
        raise ValueError("Career captures must be regular files")
    return full


def _sets(obj):
    result = {}
    for table in obj.get("resultSets", []):
        headers, rows = table.get("headers"), table.get("rowSet")
        if not isinstance(headers, list) or len(headers) != len(set(headers)) or not isinstance(rows, list):
            raise ValueError("Invalid official table")
        if any(len(row) != len(headers) for row in rows):
            raise ValueError("Official row width does not match headers")
        result[table["name"]] = [dict(zip(headers, row)) for row in rows]
    if not result:
        raise ValueError("No official result sets")
    return result


def _controls(obj, player, source, retrieved):
    """Prefer the publisher's all-team row over its overlapping team subtotals."""
    sets = _sets(obj)
    result = []
    for table_name, phase in PHASES.items():
        if table_name not in sets:
            raise ValueError("Missing independent career season controls")
        groups = {}
        for row in sets[table_name]:
            if str(row.get("PLAYER_ID")) != player["id"]:
                raise ValueError("Career controls returned a different player")
            season = row.get("SEASON_ID")
            if not re.fullmatch(r"\d{4}-\d{2}", str(season)):
                raise ValueError("Invalid career control season")
            groups.setdefault(season, []).append(row)
        for season, raw in sorted(groups.items()):
            total = [r for r in raw if str(r.get("TEAM_ID")) == "0"]
            if len(total) > 1:
                raise ValueError("Duplicate all-team career control")
            selected = total or raw
            if len({str(r.get("TEAM_ID")) for r in selected}) != len(selected):
                raise ValueError("Duplicate team career control")
            def summed(field):
                values = [r.get(field) for r in selected]
                return sum(values) if all(isinstance(v, (int, float)) for v in values) else None
            row = {"player_id": player["id"], "name": player["name"], "season": season,
                   "season_type": phase, "gp": summed("GP"), "gs": summed("GS"),
                   "minutes_reported": summed("MIN"),
                   "total_actual_teams": sorted({r.get("TEAM_ABBREVIATION") for r in raw if str(r.get("TEAM_ID")) != "0" and r.get("TEAM_ABBREVIATION")}),
                   "source_url": source, "retrieved_at": retrieved,
                   "scope": "Independent published season totals; all-team totals and team subtotals are not added together",
                   "source_rows": raw}
            row.update({field: summed(publisher) for publisher, field in FIELDS.items()})
            if row["gp"] is None or row["gp"] < 0 or int(row["gp"]) != row["gp"]:
                raise ValueError("Invalid control games played")
            result.append(row)
    return result


def _games(obj, player, season, phase, source, retrieved):
    sets = _sets(obj)
    if len(sets) != 1:
        raise ValueError("Expected one official player game-log table")
    values = next(iter(sets.values()))
    returned = obj.get("parameters", {})
    if str(returned.get("SeasonYear", returned.get("Season", season))) != season:
        raise ValueError("Returned season does not match request")
    if returned.get("SeasonType", phase) != phase:
        raise ValueError("Returned season type does not match request")
    result, seen = [], set()
    for raw in values:
        if str(raw.get("PLAYER_ID")) != player["id"]:
            raise ValueError("Game log returned a different player")
        matchup = str(raw.get("MATCHUP", "")).split()
        if len(matchup) != 3 or matchup[1] not in ("vs.", "@"):
            raise ValueError("Unrecognized historical matchup")
        if raw.get("TEAM_ABBREVIATION") != matchup[0]:
            raise ValueError("Historical team disagrees with matchup")
        clock, original_min = raw.get("MIN_SEC"), raw.get("MIN")
        copy = {**raw, "MIN": clock if clock not in (None, "") else original_min,
                "Opponent": matchup[-1], "Season": season, "Appeared": "Yes", "Source ID": source}
        normalized = normalize_game(copy, retrieved)
        game_id = normalized["game_id"]
        prefix = ("002" if phase == "Regular Season" else "004") + season[2:4]
        if len(game_id) != 10 or not game_id.startswith(prefix):
            raise ValueError("Game ID does not match requested season and phase")
        if normalized["date_et"] > datetime.fromisoformat(now()).astimezone(ET).date().isoformat():
            raise ValueError("Completed career game is in the future")
        if normalized["date_et"][:4] not in (season[:4], str(int(season[:4]) + 1)):
            raise ValueError("Career game date is outside season years")
        if game_id in seen:
            raise ValueError("Duplicate career player-game key")
        seen.add(game_id)
        normalized.pop("payload", None)
        normalized["name"] = player["name"]
        normalized["season_type"] = phase
        normalized["team_id"] = str(raw["TEAM_ID"]) if raw.get("TEAM_ID") is not None else None
        normalized["source_minutes"] = original_min
        normalized["source_minute_clock"] = clock
        normalized["source_minute_precision"] = ("Published MIN_SEC clock retained; numeric MIN also preserved"
            if clock not in (None, "") else "Published numeric MIN only; clock precision unavailable")
        normalized["source_minute_decimal_difference"] = normalized["minutes"] - original_min if original_min is not None and normalized["minutes"] is not None else None
        normalized["opponent_current_franchise"] = {"NOH": "NOP", "NJN": "BKN", "SEA": "OKC"}.get(matchup[-1], matchup[-1])
        normalized["known_at"] = retrieved
        normalized["retrieved_at"] = retrieved
        normalized["source_url"] = source
        normalized["scope"] = "Retrospective completed career game; not historical pregame knowledge"
        for field in ("minutes", "min_seconds", "source_minutes"):
            normalized[field + "_exact_text"] = str(normalized[field]) if normalized[field] is not None else None
        result.append(normalized)
    return sorted(result, key=lambda r: (r["date_et"], r["game_id"], r["player_id"]))


def _reconcile(rows, control):
    differences, missing = {}, []
    if len(rows) != control["gp"]:
        differences["gp"] = {"games": len(rows), "control": control["gp"]}
    for stat in FIELDS.values():
        observations = [r.get(stat) for r in rows]
        if control.get(stat) is None or any(v is None for v in observations):
            missing.append(stat)
        elif sum(observations) != control[stat]:
            differences[stat] = {"games": sum(observations), "control": control[stat]}
    minute_total = sum(r["minutes"] for r in rows) if all(r.get("minutes") is not None for r in rows) else None
    minute_difference = minute_total - control["minutes_reported"] if minute_total is not None and control.get("minutes_reported") is not None else None
    return {"status": "failed" if differences else "passed_with_missing_fields" if missing else "passed",
            "count_differences": differences, "missing_count_controls": missing,
            "game_minute_total": minute_total, "reported_control_minutes": control.get("minutes_reported"),
            "minute_difference": minute_difference,
            "minute_note": "Published control minutes can be rounded; differences remain reported, source clocks and controls are never adjusted to force agreement.",
            "games_missing_source_clock": sum(r.get("source_minute_clock") in (None, "") for r in rows),
            "unknown_start_or_bench": sum(r.get("started") is None for r in rows)}


def _capture(root, payload):
    filename = payload["retrieved_at"].replace(":", "").replace("+", "_") + "_" + digest(payload["key"])[:12] + ".json"
    path = root / "captures" / filename
    _atomic(path, payload)
    return {k: payload.get(k) for k in ("key", "kind", "player_id", "season", "season_type", "source_url", "retrieved_at", "coverage_cutoff", "checks")} | {
        "path": "captures/" + filename, "sha256": digest(path.read_bytes()), "row_count": len(payload["rows"]), "refresh_status": "success"}


def load_career_reference(store):
    root = _root(store)
    if not (root / "latest.json").exists():
        return {"status": "not_collected", "tables": [], "rows": [], "controls": [], "errors": []}
    index = json.loads((root / "latest.json").read_text())
    result = {k: v for k, v in index.items() if k != "entries"}
    result.update(tables=[], rows=[], controls=[])
    result["errors"] = list(result.get("errors", []))
    selected = set(index.get("scope", {}).get("selected_player_ids", []))
    seen = set()
    for entry in index.get("entries", []):
        if selected and entry.get("player_id") not in selected:
            continue
        try:
            path = _safe_path(root, entry["path"])
            raw = path.read_bytes()
            if digest(raw) != entry["sha256"]:
                raise ValueError("Career capture checksum mismatch")
            table = json.loads(raw)
            if table["key"] != entry["key"]:
                raise ValueError("Career capture key mismatch")
            table.pop("raw_source_response", None)
            table["refresh_status"] = entry.get("refresh_status", "success")
            if table["kind"] == "career_controls":
                result["controls"].extend(table["rows"])
                continue
            for row in table["rows"]:
                key = (row["player_id"], row["game_id"])
                if key in seen:
                    raise ValueError("Duplicate loaded career game")
                seen.add(key)
            result["tables"].append(table)
            result["rows"].extend(table["rows"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append({"key": entry.get("key"), "source": "career_cache_integrity", "error": str(exc)})
    result["rows"].sort(key=lambda r: (r["date_et"], r["game_id"], r["player_id"]))
    return result


def collect_career_reference(store, player_ids=None, refresh=False):
    """Collect only explicitly selected players. Caller holds maintenance lock.

    Completed historical seasons are reused if their independent count controls
    still agree. Current-season tables refresh on every call. No canonical table
    is changed, and previous immutable captures remain available for audit.
    """
    from .maintenance import fetch_json, nba_parameters
    ids = [str(p) for p in (player_ids if player_ids is not None else store.config.get("organized_players", []))]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Supply unique selected career player IDs")
    players = []
    for pid in ids:
        player = store.one("SELECT id,name FROM players WHERE id=?", (pid,))
        if not player:
            raise ValueError("Unknown selected career player: " + pid)
        players.append(player)
    root = _root(store)
    root.mkdir(parents=True, exist_ok=True)
    old = json.loads((root / "latest.json").read_text()) if (root / "latest.json").exists() else {"entries": []}
    entries = {r["key"]: dict(r) for r in old.get("entries", []) if r.get("player_id") in ids}
    errors, all_controls, specs, captured, quarantined = [], [], [], [], []
    attempted = now()
    def get_controls(player):
        source = "https://stats.nba.com/stats/playercareerstats?" + urlencode({"PlayerID": player["id"], "PerMode": "Totals", "LeagueID": "00"})
        obj = fetch_json(source, store.config)
        at = now()
        controls = _controls(obj, player, source, at)
        return {"key": "controls:" + player["id"], "kind": "career_controls", "player_id": player["id"],
                "source_url": source, "retrieved_at": at, "rows": controls, "raw_source_response": obj}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(get_controls, player): player for player in players}
        for future in as_completed(futures):
            player = futures[future]
            try:
                table = future.result()
                entries[table["key"]] = _capture(root, table)
                all_controls.extend(table["rows"])
            except Exception as exc:
                errors.append({"key": "controls:" + player["id"], "error_type": type(exc).__name__, "error": str(exc)[:300]})
                for entry in entries.values():
                    if entry.get("player_id") == player["id"]:
                        entry["refresh_status"] = "retained_after_failure"
    player_map = {p["id"]: p for p in players}
    for control in all_controls:
        if control["gp"] == 0:
            continue
        key = ":".join(("games", control["player_id"], control["season"], control["season_type"]))
        prior = entries.get(key)
        if prior and not refresh and control["season"] != store.config["season"]:
            try:
                raw = _safe_path(root, prior["path"]).read_bytes()
                if digest(raw) != prior["sha256"]:
                    raise ValueError("Career capture checksum mismatch")
                previous = json.loads(raw)
                if _reconcile(previous["rows"], control)["status"] != "failed":
                    prior["refresh_status"] = "reconciled_cached_history"
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
        params = nba_parameters(control["season"])
        params.update(PlayerID=control["player_id"], SeasonType=control["season_type"], SeasonTypeNullable=control["season_type"])
        specs.append({"key": key, "control": control, "source_url": "https://stats.nba.com/stats/playergamelogs?" + urlencode(params)})
    def get_games(spec):
        control = spec["control"]
        obj = fetch_json(spec["source_url"], store.config)
        at = now()
        rows = _games(obj, player_map[control["player_id"]], control["season"], control["season_type"], spec["source_url"], at)
        checks = _reconcile(rows, control)
        return {"key": spec["key"], "kind": "career_games", "player_id": control["player_id"],
                "season": control["season"], "season_type": control["season_type"], "source_url": spec["source_url"],
                "retrieved_at": at, "coverage_cutoff": max((r["date_et"] for r in rows), default=None),
                "unit": "One played player-game; raw minute clock and numeric minutes retained separately",
                "scope": "Selected player retrospective career box scores, separated by regular season/playoffs",
                "rows": rows, "checks": checks, "raw_source_response": obj}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(get_games, spec): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                table = future.result()
                if table["checks"]["status"] == "failed":
                    # Preserve the response for review without publishing its
                    # conflicted rows as verified career observations.
                    quarantined.append(_capture(root, table))
                    raise ValueError("Independent career counts do not reconcile: " + dumps(table["checks"]["count_differences"]))
                entries[spec["key"]] = _capture(root, table)
                captured.append({"key": spec["key"], "rows": len(table["rows"]), "checks": table["checks"]})
            except Exception as exc:
                errors.append({"key": spec["key"], "source_url": spec["source_url"], "error_type": type(exc).__name__, "error": str(exc)[:400], "retained_previous": spec["key"] in entries})
                if spec["key"] in entries:
                    entries[spec["key"]]["refresh_status"] = "retained_after_failure"
    report = {"schema_version": 1, "status": "success" if not errors else "partial" if captured else "failed",
              "attempted_at": attempted, "scope": {"selected_player_ids": ids, "canonical_mutations": False,
              "excludes": "Entire opponent rosters, All-Star/college games, invented absences and historical pregame availability"},
              "entries": sorted(entries.values(), key=lambda r: r["key"]), "errors": errors,
              "captured": captured, "quarantined_entries": quarantined,
              "limitations": ["Retrieval times are current knowledge, not historical pregame publication times.",
                  "Source minute clocks and original numeric minutes remain distinct; unavailable clocks remain flagged.",
                  "Career season GS is not an individual-game starting indicator; unknown starts remain missing."]}
    _atomic(root / "runs" / (uuid.uuid4().hex + ".json"), report)
    _atomic(root / "latest.json", report)
    return {k: v for k, v in report.items() if k != "entries"}
