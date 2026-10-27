"""Optional official historical reference captures, separate from model features.

Season tracking rows are season means, never player-game observations. Retrieval
today does not establish what was known before a historical game.
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


TRACKING_MEASURES = ("Passing", "Rebounding", "Possessions")


def _root(store):
    return store.root / "reference" / "context"


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    try:
        temp.write_text(dumps(payload), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_capture(root, relative):
    # Source metadata is data, and must not allow path traversal on restore.
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 2 or p.parts[0] != "captures":
        raise ValueError("Invalid historical-context capture path")
    full = root / p
    if full.is_symlink() or full.parent.is_symlink():
        raise ValueError("Historical-context captures must be regular files")
    return full


def load_context_reference(store):
    """Read verified cache tables; unavailable captures produce explicit errors."""
    root = _root(store)
    if not (root / "latest.json").exists():
        return {"tables": [], "errors": [], "status": "not_collected"}
    index = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    result = {"tables": [], "errors": list(index.get("errors", [])), "warnings":list(index.get("warnings", [])),
              "status": index.get("status", "unknown"), "attempted_at": index.get("attempted_at")}
    for record in index.get("tables", []):
        try:
            p = _safe_capture(root, record["path"])
            raw = p.read_bytes()
            if digest(raw) != record["sha256"]:
                raise ValueError("Capture checksum mismatch")
            table = json.loads(raw)
            if table["key"] != record["key"]:
                raise ValueError("Capture key mismatch")
            # Cache status belongs to this refresh attempt, not the older source capture.
            table["refresh_status"] = record.get("refresh_status", "success")
            result["tables"].append(table)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append({"key": record.get("key"), "error": str(exc), "source": "cache_integrity"})
    return result


def _requests(season):
    from .maintenance import nba_parameters
    specs = []
    for measure in ("Base", "Advanced"):
        params = nba_parameters(season)
        params.update(MeasureType=measure)
        specs.append({"key": f"team_games:{season}:{measure}", "kind": "team_games", "measure": measure,
            "season": season, "scope": "team_game_regular_season", "unit": "one team in one completed game; published minutes",
            "source_url": "https://stats.nba.com/stats/teamgamelogs?" + urlencode(params)})
    for measure in TRACKING_MEASURES:
        params = {"LastNGames": 0, "LeagueID": "00", "Month": 0, "OpponentTeamID": 0,
            "PerMode": "PerGame", "PlayerOrTeam": "Player", "PtMeasureType": measure,
            "Season": season, "SeasonType": "Regular Season", "TeamID": 0}
        specs.append({"key": f"tracking_season:{season}:{measure}", "kind": "tracking_season", "measure": measure,
            "season": season, "scope": "player_season_tracking_mean_regular_season", "unit": "published per-game season means; GP is tracking sample support",
            "source_url": "https://stats.nba.com/stats/leaguedashptstats?" + urlencode(params)})
    return specs


def _normalize_rows(obj, spec):
    sets = obj.get("resultSets")
    if not isinstance(sets, list) or len(sets) != 1:
        raise ValueError("Expected one official result set")
    headers, values = sets[0].get("headers"), sets[0].get("rowSet")
    if not isinstance(headers, list) or len(headers) != len(set(headers)) or not isinstance(values, list):
        raise ValueError("Invalid official table headers or rows")
    if not values:
        raise ValueError("No observations returned; retaining previous verified capture")
    params = obj.get("parameters", {})
    returned_season = params.get("SeasonYear", params.get("Season"))
    if returned_season is not None and str(returned_season) != spec["season"]:
        raise ValueError("Returned season differs from requested season")
    rows, seen = [], set()
    for values_row in values:
        if not isinstance(values_row, (list, tuple)) or len(values_row) != len(headers):
            raise ValueError("Official row width differs from headers")
        r = dict(zip(headers, values_row))
        for name, value in list(r.items()):
            if name.endswith("_ID") and value is not None:
                # API identifiers are integers or digit strings. Never stringify a float.
                if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
                    raise ValueError("Invalid identifier in official table")
                r[name] = str(value)
        if spec["kind"] == "team_games":
            for field in ("GAME_ID", "TEAM_ID", "GAME_DATE", "MATCHUP"):
                if r.get(field) in (None, ""):
                    raise ValueError("Team game lacks a stable key or game context")
            if len(r["GAME_ID"]) != 10:
                raise ValueError("Official game ID must preserve all ten digits")
            if not r["GAME_ID"].startswith("002" + spec["season"][2:4]):
                raise ValueError("Game ID does not match the requested regular season")
            game_date = str(r["GAME_DATE"])[:10]
            try:
                date = datetime.fromisoformat(game_date).date()
            except ValueError as exc:
                raise ValueError("Invalid official game date") from exc
            start = int(spec["season"][:4])
            if date.year not in (start, start + 1):
                raise ValueError("Game date falls outside the requested season years")
            if date > datetime.fromisoformat(now()).astimezone(ET).date():
                raise ValueError("Completed team-game date is in the future")
            if r.get("SEASON_YEAR") not in (None, spec["season"]):
                raise ValueError("Row season differs from requested season")
            key = (r["GAME_ID"], r["TEAM_ID"])
        else:
            if not r.get("PLAYER_ID") or r.get("GP") is None or r["GP"] <= 0:
                raise ValueError("Season tracking requires player ID and positive GP")
            key = (r["PLAYER_ID"], r.get("TEAM_ID"))
        if key in seen:
            raise ValueError("Duplicate official observation key")
        seen.add(key)
        rows.append(r)
    if spec["kind"] == "team_games":
        by_game = {}
        for r in rows:
            game = by_game.setdefault(r["GAME_ID"], [])
            game.append(r)
        for group in by_game.values():
            if len(group) != 2 or len({r["TEAM_ID"] for r in group}) != 2:
                raise ValueError("Expected exactly two distinct team observations per completed game")
            if len({str(r["GAME_DATE"])[:10] for r in group}) != 1:
                raise ValueError("Team observations disagree on game date")
        rows.sort(key=lambda r: (str(r["GAME_DATE"]), r["GAME_ID"], r["TEAM_ID"]))
    else:
        rows.sort(key=lambda r: (r["PLAYER_ID"], r.get("TEAM_ID") or ""))
    return rows


def _canonical_checks(store, table):
    """Report independently available player-box sums, without assuming completeness."""
    if table["kind"] != "team_games" or table["measure"] != "Base":
        return {}
    sums = store.rows("""SELECT game_id,team,count(*) players,
        count(pts) pts_n,sum(pts) pts,count(fgm) fgm_n,sum(fgm) fgm,
        count(fg3m) fg3m_n,sum(fg3m) fg3m
        FROM player_games WHERE season=? AND appeared=1 GROUP BY game_id,team""", (table["season"],))
    canonical = {(r["game_id"], r["team"]): r for r in sums}
    matches, missing, differences = 0, 0, []
    for row in table["rows"]:
        raw = canonical.get((row["GAME_ID"], row.get("TEAM_ABBREVIATION")))
        if raw is None:
            missing += 1
            continue
        diff = {}
        for source, column in (("PTS", "pts"), ("FGM", "fgm"), ("FG3M", "fg3m")):
            if row.get(source) is not None and raw[column + "_n"] == raw["players"] and raw[column] != row[source]:
                diff[source] = {"official_team": row[source], "canonical_player_sum": raw[column]}
        if diff:
            differences.append({"game_id": row["GAME_ID"], "team": row.get("TEAM_ABBREVIATION"),
                "canonical_players": raw["players"], "differences": diff})
        else:
            matches += 1
    return {"canonical_box_sum_matches": matches, "canonical_box_sum_unavailable": missing,
        "canonical_box_sum_differences": differences,
        "meaning": "Differences are source conflicts or partial canonical team coverage; no team total is forced to match."}


def collect_context_reference(store, seasons=None):
    """Capture optional references; individual source failures retain prior tables.

    Call while holding the app's maintenance lock. It never changes canonical
    player games, availability, prediction features, or the live package pointer.
    """
    from .maintenance import fetch_json
    seasons = list(dict.fromkeys(seasons or [store.config["season"]]))
    for season in seasons:
        if not re.fullmatch(r"\d{4}-\d{2}", season) or int(season[-2:]) != (int(season[:4]) + 1) % 100:
            raise ValueError("Expected consecutive NBA season labels such as 2026-27")
    root = _root(store)
    root.mkdir(parents=True, exist_ok=True)
    latest = root / "latest.json"
    old = json.loads(latest.read_text()) if latest.exists() else {"tables": []}
    entries = {r["key"]: dict(r) for r in old.get("tables", [])}
    specs = [spec for season in seasons for spec in _requests(season)]
    attempted = now()
    errors, captured, candidates, warnings = [], [], {}, []

    def request(spec):
        obj = fetch_json(spec["source_url"], store.config)
        rows = _normalize_rows(obj, spec)
        at = now()
        cutoff = max(str(r["GAME_DATE"])[:10] for r in rows) if spec["kind"] == "team_games" else None
        return {**spec, "retrieved_at": at, "coverage_cutoff": cutoff,
            "coverage_note": ("Season means through publisher's currently returned sample; exact final game date is not supplied by this endpoint."
                if spec["kind"] == "tracking_season" else "Completed official regular-season team games in requested season."),
            "publisher_updated_at": None, "rows": rows,
            "warnings": (["Team labels are publisher season/roster metadata; do not apply them retrospectively to individual games.",
                "Season means are not individual game observations or pregame historical features."] if spec["kind"] == "tracking_season" else []),
            "rows_sha256": digest(rows)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(request, spec): spec for spec in specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                table = future.result()
                table["checks"] = _canonical_checks(store, table)
                if table["checks"].get("canonical_box_sum_differences"):
                    warning = {"key": spec["key"], "code": "canonical_team_total_difference",
                        "count": len(table["checks"]["canonical_box_sum_differences"]),
                        "meaning": "Source conflict or incomplete canonical coverage; inspect per-game differences in checks."}
                    table["warnings"].append(warning)
                    warnings.append(warning)
                filename = table["retrieved_at"].replace(":", "").replace("+", "_") + "_" + digest(spec["key"])[:12] + ".json"
                path = root / "captures" / filename
                _atomic_json(path, table)
                entries[spec["key"]] = {k: table[k] for k in ("key", "kind", "measure", "season", "retrieved_at", "coverage_cutoff")}
                entries[spec["key"]].update(path="captures/" + filename, sha256=digest(path.read_bytes()),
                    refresh_status="success", row_count=len(table["rows"]))
                captured.append({"key": spec["key"], "rows": len(table["rows"]), "coverage_cutoff": table["coverage_cutoff"]})
                candidates[spec["key"]] = table
            except Exception as exc:
                errors.append({"key": spec["key"], "source_url": spec["source_url"],
                    "attempted_at": attempted, "error_type": type(exc).__name__, "error": str(exc)[:300],
                    "retained_previous": spec["key"] in entries})
                if spec["key"] in entries:
                    entries[spec["key"]]["refresh_status"] = "retained_after_failure"
    # Separate endpoints should describe the same set of completed team-games.
    # If they do not, archive the responses but retain both earlier verified views.
    prior = {r["key"]: r for r in old.get("tables", [])}
    for season in seasons:
        pair = [f"team_games:{season}:{measure}" for measure in ("Base", "Advanced")]
        if all(key in candidates for key in pair):
            keysets = [{(r["GAME_ID"], r["TEAM_ID"]) for r in candidates[key]["rows"]} for key in pair]
            if keysets[0] != keysets[1]:
                for key in pair:
                    errors.append({"key": key, "error_type": "CoverageConflict", "attempted_at": attempted,
                        "error": "Base and Advanced completed team-game keys differ; both new views withheld.",
                        "retained_previous": key in prior})
                    if key in prior:
                        entries[key] = {**prior[key], "refresh_status": "retained_after_failure"}
                    else:
                        entries.pop(key, None)
                captured = [r for r in captured if r["key"] not in pair]
    report = {"status": ("success_with_warnings" if warnings else "success") if not errors else "partial" if captured else "failed",
        "attempted_at": attempted, "tables": sorted(entries.values(), key=lambda r: r["key"]), "errors": errors,
        "warnings": warnings,
        "captured": sorted(captured, key=lambda r: r["key"]),
        "meaning": "Optional reference tables only; missing advanced context does not invent statistics or validate projection effects."}
    # A single pointer file switches all new references atomically; old capture files remain.
    _atomic_json(root / "runs" / (uuid.uuid4().hex + ".json"), report)
    _atomic_json(latest, report)
    return {k: v for k, v in report.items() if k != "tables"}
