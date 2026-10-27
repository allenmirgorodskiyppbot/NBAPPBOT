"""Lossless raw archive plus conservative canonical player-game ingestion."""
from __future__ import annotations

import csv
import json
import math
import re
import shutil
from collections import Counter
from datetime import datetime, time
from pathlib import Path

from openpyxl import load_workbook

from .core import ET, UTC, digest, dumps, now, stamp


def key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


ALIASES = {
    "game_id": ["NBA game ID", "GAME_ID"], "player_id": ["NBA player ID", "PLAYER_ID", "PERSON_ID"],
    "name": ["Player", "PLAYER_NAME"], "team": ["Team", "TEAM_ABBREVIATION", "TEAM_ABBR"],
    "team_id": ["NBA team ID", "TEAM_ID"], "opponent_id": ["Opponent team ID"],
    "opponent": ["Opponent", "OPPONENT_ABBREVIATION"], "date_et": ["Game date", "GAME_DATE", "date_et"],
    "season": ["Season", "SEASON_YEAR"], "appeared": ["Appeared", "PLAYED"],
    "started": ["Started", "STARTED"], "minutes": ["Minutes (source precision)", "MIN", "MINUTES"],
    "min_seconds": ["Minutes in seconds", "MIN_SECONDS", "min_seconds"],
    "pts": ["PTS", "Points"], "reb": ["REB", "Rebounds"], "ast": ["AST", "Assists"],
    "fg3m": ["3PM", "FG3M"], "fg3a": ["3PA", "FG3A"], "fgm": ["FGM"], "fga": ["FGA"],
    "ftm": ["FTM"], "fta": ["FTA"], "oreb": ["OREB"], "dreb": ["DREB"],
    "stl": ["STL"], "blk": ["BLK"], "tov": ["TO", "TOV"], "pf": ["Fouls", "PF"],
    "plus_minus": ["Plus/minus", "PLUS_MINUS"], "source": ["Source ID", "SOURCE_ID", "source"],
    "known_at": ["known_at", "Retrieved at (UTC)", "retrieved_at"],
}
COUNTS = ["pts", "reb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "oreb", "dreb",
          "stl", "blk", "tov", "pf", "plus_minus"]
PG_FIELDS = ["game_id", "player_id", "team", "opponent", "season", "date_et", "appeared", "started",
             "minutes", "min_seconds", *COUNTS, "known_at", "source", "payload", "hash"]


def mapped(row):
    normalized = {key(k): v for k, v in row.items() if k is not None}
    out = {}
    for field, names in ALIASES.items():
        values = [normalized[key(n)] for n in names if key(n) in normalized and normalized[key(n)] not in (None, "")]
        if values:
            if field in COUNTS and len({str(v) for v in values}) > 1:
                raise ValueError(f"Conflicting aliases for {field}")
            out[field] = values[0]
        else:
            out[field] = None
    return out


def identifier(v, width=None):
    if v is None or str(v).strip() == "":
        raise ValueError("Missing stable identifier")
    if isinstance(v, (float, int)):
        if int(v) != v:
            raise ValueError("Nonintegral identifier")
        return str(int(v)).zfill(width or 1)
    return str(v).strip()


def date_only(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    text = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized date: {v}")


def boolean(v):
    if v is None or str(v).lower() in ("", "unknown", "unavailable", "none"):
        return None
    if str(v).lower() in ("yes", "true", "1", "1.0", "played"):
        return 1
    if str(v).lower() in ("no", "false", "0", "0.0", "dnp", "inactive"):
        return 0
    raise ValueError(f"Invalid boolean: {v}")


def duration(v):
    if v is None or v == "":
        return None
    if isinstance(v, str) and v.startswith("PT"):
        m = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", v)
        if not m:
            raise ValueError("Invalid ISO minutes")
        return float(m[1] or 0) + float(m[2] or 0) / 60
    if isinstance(v, str) and ":" in v:
        m, s = v.split(":")
        return float(m) + float(s) / 60
    f = float(v)
    if not math.isfinite(f):
        raise ValueError("Nonfinite minutes")
    return f


def normalize_game(row, known_at):
    p = mapped(row)
    p["game_id"] = identifier(p["game_id"], 10)
    p["player_id"] = identifier(p["player_id"])
    p["date_et"] = date_only(p["date_et"])
    p["season"] = str(p["season"] or "unknown").replace("–", "-")
    p["minutes"] = duration(p["minutes"])
    p["min_seconds"] = float(p["min_seconds"]) if p["min_seconds"] is not None else (
        p["minutes"] * 60 if p["minutes"] is not None else None)
    p["appeared"], p["started"] = boolean(p["appeared"]), boolean(p["started"])
    if p["appeared"] is None and p["minutes"] is not None and p["minutes"] > 0:
        p["appeared"] = 1
    for k in COUNTS:
        v = p[k]
        if v is not None:
            n = float(v)
            if not math.isfinite(n) or n != int(n) or (n < 0 and k != "plus_minus"):
                raise ValueError(f"Invalid count {k}={v}")
            p[k] = int(n)
    if p["minutes"] is not None:
        if not 0 <= p["minutes"] <= 85:
            raise ValueError("Minutes outside plausible multi-overtime bounds")
        if abs(p["minutes"] * 60 - p["min_seconds"]) > .001:
            raise ValueError("Minute precision conflict")
    for a, b in [("fg3m", "fg3a"), ("fgm", "fga"), ("ftm", "fta"), ("fg3m", "fgm"), ("fg3a", "fga")]:
        if p[a] is not None and p[b] is not None and p[a] > p[b]:
            raise ValueError(f"Inconsistent shooting: {a}>{b}")
    if all(p[k] is not None for k in ("pts", "fgm", "fg3m", "ftm")):
        if p["pts"] != 2*p["fgm"] + p["fg3m"] + p["ftm"]:
            raise ValueError("Point total does not reconcile to shooting")
    if all(p[k] is not None for k in ("reb", "oreb", "dreb")) and p["reb"] != p["oreb"] + p["dreb"]:
        raise ValueError("Rebound components do not reconcile")
    if not p["team"]:
        raise ValueError("Missing actual historical team (comparison team is not sufficient)")
    p["known_at"] = stamp(p["known_at"] or known_at)
    p["source"] = str(p["source"] or "uploaded")
    p["payload"] = dumps(row)
    p["hash"] = digest({k: v for k, v in p.items() if k not in ("known_at", "source", "payload", "hash", "name", "team_id", "opponent_id")})
    return p


def upsert_game(store, g):
    required = ("id", "date_et", "known_at")
    if any(not g.get(k) for k in required):
        raise ValueError("Schedule requires id, date_et, known_at")
    g = {"tipoff": None, "home": None, "away": None, "status": "unknown", "season": None,
         "source": None, "metadata": "{}", **g}
    g["known_at"] = stamp(g["known_at"])
    if g["tipoff"]:
        g["tipoff"] = stamp(g["tipoff"])
    old = store.one("SELECT * FROM games WHERE id=?", (g["id"],))
    # A sparse box observation must not erase a known schedule.
    if old and not g["tipoff"]:
        for k in ("tipoff", "home", "away"):
            g[k] = old[k] or g[k]
    if old and dumps(old) != dumps(g):
        store.db.execute("INSERT INTO audit(at,kind,record_key,old,new,source) VALUES(?,?,?,?,?,?)",
                         (now(), "schedule", g["id"], dumps(old), dumps(g), g["source"]))
    fields = ["id", "date_et", "tipoff", "home", "away", "status", "season", "known_at", "source", "metadata"]
    store.db.execute(f"INSERT OR REPLACE INTO games({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                     [g[k] for k in fields])
    store.db.execute("INSERT OR IGNORE INTO game_versions VALUES(?,?,?,?)",
                     (g["id"], g["known_at"], dumps(g), digest(g)))


def upsert_player_game(store, p, corrections=False):
    old = store.one("SELECT * FROM player_games WHERE game_id=? AND player_id=?", (p["game_id"], p["player_id"]))
    if old and old["appeared"]==p["appeared"]:
        # A lean game-log response must not erase known detailed-box fields such as starts.
        for k in ["started","minutes","min_seconds",*COUNTS]:
            if p[k] is None and old[k] is not None:
                p[k]=old[k]
        p["hash"]=digest({k:v for k,v in p.items() if k not in ("known_at","source","payload","hash","name","team_id","opponent_id")})
    # XLSX numeric serialization may differ from CSV by floating-point roundoff.
    # This tolerance is a millionth of a second, not box-display rounding.
    equivalent = old and all(
        (math.isclose(old[k],p[k],rel_tol=0,abs_tol=1e-6) if k in ("minutes","min_seconds") and old[k] is not None and p[k] is not None
         else old[k]==p[k]) for k in PG_FIELDS if k not in ("known_at","source","payload","hash"))
    if old and (old["hash"] == p["hash"] or equivalent):
        return "duplicate"
    if old and not corrections:
        raise ValueError(f"Conflicting source for {p['game_id']}/{p['player_id']}; review or import verified correction")
    # Prevent a wrong game ID joining different dates or opponents.
    existing = store.one("SELECT * FROM games WHERE id=?", (p["game_id"],))
    if existing and existing["date_et"] != p["date_et"]:
        raise ValueError("Game ID joins to a different date")
    if existing and existing["home"] and existing["away"] and p["team"] not in (existing["home"], existing["away"]):
        raise ValueError("Game ID joins to a different team")
    store.db.execute("INSERT INTO players VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                     (p["player_id"], p["name"] or p["player_id"]))
    for id_key, abbr in (("team_id", "team"), ("opponent_id", "opponent")):
        if p.get(id_key) and p.get(abbr):
            store.db.execute("INSERT OR IGNORE INTO teams VALUES(?,?)", (str(p[id_key]), p[abbr]))
    if not existing:
        upsert_game(store, {"id": p["game_id"], "date_et": p["date_et"], "status": "completed",
                            "known_at": p["known_at"], "season": p["season"], "source": p["source"]})
    if old:
        store.db.execute("INSERT INTO audit(at,kind,record_key,old,new,source) VALUES(?,?,?,?,?,?)",
                         (now(), "stat_correction", p["game_id"]+"/"+p["player_id"], dumps(old), dumps(p), p["source"]))
    store.db.execute(f"INSERT OR REPLACE INTO player_games({','.join(PG_FIELDS)}) VALUES({','.join('?' for _ in PG_FIELDS)})",
                     [p[k] for k in PG_FIELDS])
    store.db.execute("INSERT OR IGNORE INTO stat_versions VALUES(?,?,?,?,?)",
                     (p["game_id"], p["player_id"], p["known_at"], p["hash"], dumps(p)))
    return "corrected" if old else "inserted"


def workbook_tables(path):
    book = load_workbook(path, read_only=True, data_only=True)
    tables = {}
    try:
        for sheet in book:
            rows = list(sheet.values)
            header = next((i for i, r in enumerate(rows) if len(r) > 2 or (r and r[0] == "Topic")), None)
            if header is None:
                tables[sheet.title] = ([], rows)
                continue
            names = [str(x) if x is not None else f"unnamed_{j}" for j, x in enumerate(rows[header])]
            tables[sheet.title] = (names, [dict(zip(names, r)) for r in rows[header+1:] if any(v is not None for v in r)])
    finally:
        book.close()
    return tables


def source_time(v):
    if not v:
        return None
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=UTC).isoformat(timespec="microseconds")
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        # Called only for columns explicitly labeled UTC in Source Registry.
        return stamp(d.replace(tzinfo=UTC) if d.tzinfo is None else d)
    except ValueError:
        return None


def import_files(store, paths):
    results = []
    files = []
    for p in paths:
        p = Path(p)
        files.extend(sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p])
    # Read registry metadata before observations when supplied separately.
    files.sort(key=lambda p: (0 if "registry" in p.stem.lower() or p.suffix.lower()==".xlsx" else 1, str(p)))
    for path in files:
        if path.suffix.lower() not in (".xlsx", ".csv", ".json", ".md", ".txt"):
            results.append({"file": path.name, "status": "unsupported_preserved", "bytes": path.stat().st_size})
            raw = path.read_bytes()
            target = store.root / "originals" / (digest(raw)[:16]+"_"+path.name)
            target.parent.mkdir(exist_ok=True)
            if path.resolve() != target.resolve() and not target.exists():
                shutil.copy2(path,target)
            continue
        raw = path.read_bytes()
        h = digest(raw)
        if store.one("SELECT 1 FROM files WHERE hash=?", (h,)):
            results.append({"file": path.name, "status": "already_imported"})
            continue
        target = store.root / "originals" / (h[:16]+"_"+path.name)
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
        if path.suffix.lower() == ".xlsx":
            tables = workbook_tables(path)
        elif path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                tables = {path.stem.replace("_", " "): (reader.fieldnames or [], list(reader))}
        elif path.suffix.lower() == ".json":
            obj = json.loads(raw)
            records = obj if isinstance(obj,list) else [obj]
            tables = {path.stem: ([], records)}
        else:
            tables = {path.stem: ([], [{"text": raw.decode("utf-8-sig")}])}
        stats = Counter()
        timestamps = {}
        for r in store.rows("SELECT payload FROM raw_rows WHERE sheet='Source Registry'"):
            x = json.loads(r["payload"])
            timestamps[x.get("Source ID")] = source_time(x.get("Retrieved at (UTC)")) or source_time(x.get("Extracted at (UTC)"))
        for name, (_, records) in tables.items():
            if key(name)=="sourceregistry":
                for r in records:
                    timestamps[r.get("Source ID")] = source_time(r.get("Retrieved at (UTC)")) or source_time(r.get("Extracted at (UTC)"))
        with store.transaction():
            store.db.execute("INSERT INTO files VALUES(?,?,?,?,?)", (h, path.name, str(target), now(),
                dumps({n:{"rows":len(rs),"headers":hs} for n,(hs,rs) in tables.items()})))
            for name, (_, records) in tables.items():
                for i, row in enumerate(records):
                    store.db.execute("INSERT INTO raw_rows VALUES(?,?,?,?)", (h, name, i+1, dumps(row)))
                    canonical = key(name) in ("cleanstats", "nbaplayergames", "playergames")
                    if canonical:
                        m = mapped(row)
                        try:
                            p = normalize_game(row, timestamps.get(m["source"]) or now())
                            stats[upsert_player_game(store, p)] += 1
                        except (ValueError, TypeError) as e:
                            stats["quarantined"] += 1
                            store.check(path.name, "error", "quarantined_player_game", {"row":i+1,"error":str(e)})
                    elif key(name)=="schedule":
                        try:
                            d = date_only(row["Date (ET)"])
                            clock = datetime.strptime(str(row["Tipoff (ET)"]), "%I:%M %p").time()
                            tip = datetime.combine(datetime.fromisoformat(d).date(), clock, ET)
                            opponent = row["Opponent"]
                            home, away = ("NYK",opponent) if row["Home / away"]=="Home" else (opponent,"NYK")
                            g_id = "espn:"+str(row["ESPN game ID"])
                            # Keep the actual NBA identity when already known for that fixture.
                            match = store.one("SELECT DISTINCT game_id FROM player_games WHERE date_et=? AND team='NYK' AND opponent=?", (d,opponent))
                            if match:
                                store.db.execute("INSERT OR REPLACE INTO game_aliases VALUES(?,?)", (g_id,match["game_id"]))
                                g_id = match["game_id"]
                            upsert_game(store, {"id":g_id,"date_et":d,"tipoff":tip.isoformat(),"home":home,
                                "away":away,"status":str(row["Status"]).lower(),"season":"2026-27",
                                "known_at":now(),"source":row["Game source"],"metadata":dumps(row)})
                            stats["schedule"] += 1
                        except (KeyError,ValueError) as e:
                            stats["quarantined"] += 1
                            store.check(path.name,"error","quarantined_schedule",str(e))
            store.check(path.name,"info","raw_import",dict(stats))
        results.append({"file":path.name,"sha256":h,"tables":{n:len(r) for n,(_,r) in tables.items()},**stats})
    return results


def validate(store):
    checks = []
    def add(level, code, detail):
        checks.append({"severity":level,"code":code,"detail":detail})
    total = store.one("SELECT count(*) n FROM player_games")["n"]
    players = store.one("SELECT count(*) n FROM players")["n"]
    coverage = store.one("SELECT min(date_et) first,max(date_et) last,count(DISTINCT game_id) games FROM player_games")
    add("info","coverage",{"player_games":total,"players":players,**coverage})
    # Reconcile only complete player-season coverage; selected subsets must not masquerade as league coverage.
    controls = store.rows("SELECT payload FROM raw_rows WHERE sheet IN ('NBA Season Totals','NBA_Season_Totals')")
    controls_by_key={}
    for row in controls:
        c=json.loads(row["payload"])
        controls_by_key[str(c.get("Season"))+"/"+str(c.get("NBA player ID"))]=(c,False)
    for row in store.rows("SELECT key,payload FROM snapshots WHERE kind='season_controls' ORDER BY observed_at"):
        controls_by_key[row["key"]]=(json.loads(row["payload"]),True)
    complete, partial, conflicts = 0,0,0
    for c,fresh_control in controls_by_key.values():
        try:
            pid = str(c["NBA player ID"])
            season = c["Season"].replace("–","-")
            obs = store.one("SELECT count(*) gp,sum(pts) pts,sum(reb) reb,sum(ast) ast,sum(fg3m) fg3m,sum(minutes) minutes "
                            "FROM player_games WHERE player_id=? AND season=? AND appeared=1 AND date_et<=?",(pid,season,str(c.get("Through") or "9999-12-31")[:10]))
            if obs["gp"] != int(c["GP"]):
                partial += 1
                if fresh_control:
                    add("error","fresh_season_coverage_mismatch",{"player_id":pid,"season":season,"observed_gp":obs["gp"],"expected_gp":c["GP"]})
                continue
            complete += 1
            for col, source in [("pts","PTS"),("reb","REB"),("ast","AST"),("fg3m","3PM"),("minutes","Minutes (source precision)")]:
                if c.get(source) is not None and obs[col] is not None and abs(float(c[source])-obs[col]) > .005:
                    conflicts += 1
                    add("error","season_total_conflict",{"player_id":pid,"season":season,"field":col,"observed":obs[col],"expected":c[source]})
        except (KeyError,TypeError,ValueError) as e:
            add("warning","unreadable_control",str(e))
    add("info","season_controls",{"complete":complete,"partial_or_absent":partial,"conflicts":conflicts})
    if players < 100:
        add("warning","league_package_missing","Only selected player history is present. Upload NBA_Player_Games.csv and companion tables; workbook claims are not independent checks.")
    if not store.one("SELECT 1 FROM snapshots WHERE kind='injuries'"):
        add("warning","injury_history_unavailable","Injury notes and missing-report entries do not establish health.")
    add("warning","historical_vintages","Retrospective corrected box scores are not an intraday as-of archive. Strict historical tests require known_at before each cutoff.")
    missing = store.one("SELECT sum(minutes IS NULL) minutes,sum(pts IS NULL) pts,sum(fg3a IS NULL) fg3a FROM player_games")
    add("info","missing_values",missing)
    joined=store.one("SELECT count(*) n FROM player_games p JOIN games g ON g.id=p.game_id WHERE p.date_et<>g.date_et")["n"]
    if joined:
        add("error","incorrect_game_join",joined)
    future=store.one("SELECT count(*) n FROM player_games WHERE date_et>?",(datetime.now(ET).date().isoformat(),))["n"]
    if future:
        add("error","future_observations",future)
    missing_games=store.rows("SELECT g.id,g.date_et FROM games g LEFT JOIN player_games p ON p.game_id=g.id "
                            "WHERE g.status='completed' GROUP BY g.id HAVING count(p.player_id)=0")
    if missing_games:
        add("warning","completed_games_missing_stats",missing_games[:50])
    imported_issues=store.rows("SELECT payload FROM raw_rows WHERE sheet='Data Checks'")
    statuses=Counter(json.loads(r["payload"]).get("Status") for r in imported_issues)
    add("info","archived_source_check_statuses",dict(statuses))
    # A full team box has >=5 played players; selected subsets cannot validate team minutes or scores.
    partial=store.one("SELECT count(*) n FROM (SELECT game_id,team FROM player_games WHERE appeared=1 "
                      "GROUP BY game_id,team HAVING count(*)<5)")["n"]
    add("warning" if partial else "info","team_box_coverage",{"partial_team_games":partial,
        "meaning":"Team totals require a complete box; missing participants are not zeros."})
    errors = store.rows("SELECT scope,code,detail FROM checks WHERE severity='error'")
    for error in errors:
        add("error","import_error",error)
    return {"at":now(),"status":"failed" if any(c["severity"]=="error" for c in checks) else "passed_with_limitations", "checks":checks}
