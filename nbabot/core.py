from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
MARKETS = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m"}
DEFAULT = {
    "data_source_mode":"online",
    "external_sources_approved":False,"external_source_approvals":[],
    "data_dir": "data", "daily_time": "09:00", "timezone": "America/New_York",
    "pregame_minutes": [120, 60, 15], "freshness_hours": {
        "stats": 30, "schedule": 30, "rosters": 24, "injuries": 3,
        "lineups": 3, "news": 6, "offers": 1},
    "nba_enabled": True, "public_context_enabled": True, "season": "2026-27", "provider_url": None,
    "provider_file": None, "provider_token_env": "NBA_PROVIDER_TOKEN",
    "provider_auth_header": "Authorization", "provider_auth_scheme": "Bearer",
    "provider_allowed_hosts": [], "http_timeout": 15, "simulations": 4000,
    "min_games": 20, "paper_bankroll": 100.0, "flat_stake": 1.0,
    "selection_policy": None,
    "organized_players": [], "historical_context_enabled":True,
    "scoped_history_enabled":True, "historical_retry_hours":24, "historical_rotation_batch_size":8,
    "package_retention_days":14, "package_min_versions":7,
}


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def stamp(value):
    """Unambiguous UTC timestamp. Never silently interpret a naive timestamp as ET."""
    if isinstance(value, datetime):
        d = value
    else:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"Timezone required: {value}")
    return d.astimezone(UTC).isoformat(timespec="microseconds")


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def digest(obj):
    return hashlib.sha256((obj if isinstance(obj, bytes) else dumps(obj).encode())).hexdigest()


def load_config(path=None):
    c = json.loads(json.dumps(DEFAULT))
    path = Path(path or os.environ.get("NBABOT_CONFIG", "config.local.json"))
    if path.exists():
        supplied = json.loads(path.read_text())
        for k, v in supplied.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    c["data_dir"] = str(Path(c["data_dir"]).resolve())
    if c.get("data_source_mode") not in ("online","spreadsheet"):
        raise ValueError("data_source_mode must be online or spreadsheet")
    if not isinstance(c.get("external_sources_approved"),bool) or not isinstance(c.get("external_source_approvals"),list):
        raise ValueError("External source approval must be a boolean with an explicit list of approved hostnames")
    if c["data_source_mode"]=="spreadsheet" and not (c.get("external_sources_approved") is True and c.get("external_source_approvals")):
        c.update(nba_enabled=False,public_context_enabled=False,provider_url=None,provider_file=None,
            provider_allowed_hosts=[],historical_context_enabled=False,scoped_history_enabled=False)
    datetime.strptime(c["daily_time"], "%H:%M")
    ZoneInfo(c["timezone"])
    if c["simulations"] < 100:
        raise ValueError("At least 100 simulations required")
    if c["flat_stake"] <= 0:
        raise ValueError("Stake must be positive")
    if not isinstance(c["package_retention_days"],(int,float)) or c["package_retention_days"]<0:
        raise ValueError("package_retention_days must be nonnegative (zero disables pruning)")
    if not isinstance(c["package_min_versions"],int) or c["package_min_versions"]<2:
        raise ValueError("package_min_versions must be at least two")
    return c


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS files(hash TEXT PRIMARY KEY, name TEXT, original_path TEXT, imported_at TEXT, inventory TEXT);
CREATE TABLE IF NOT EXISTS raw_rows(file_hash TEXT, sheet TEXT, row_number INTEGER, payload TEXT,
 PRIMARY KEY(file_hash,sheet,row_number));
CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, abbr TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, date_et TEXT NOT NULL, tipoff TEXT,
 home TEXT, away TEXT, status TEXT, season TEXT, known_at TEXT NOT NULL, source TEXT, metadata TEXT);
CREATE TABLE IF NOT EXISTS game_versions(id TEXT, known_at TEXT, payload TEXT, hash TEXT,
 PRIMARY KEY(id,hash));
CREATE TABLE IF NOT EXISTS game_aliases(alias TEXT PRIMARY KEY, game_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS player_games(game_id TEXT, player_id TEXT, team TEXT, opponent TEXT,
 season TEXT, date_et TEXT, appeared INTEGER, started INTEGER, minutes REAL, min_seconds REAL,
 pts INTEGER, reb INTEGER, ast INTEGER, fg3m INTEGER, fg3a INTEGER, fgm INTEGER, fga INTEGER,
 ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
 pf INTEGER, plus_minus INTEGER, known_at TEXT, source TEXT, payload TEXT, hash TEXT,
 PRIMARY KEY(game_id,player_id), FOREIGN KEY(player_id) REFERENCES players(id));
CREATE INDEX IF NOT EXISTS pg_history ON player_games(player_id,date_et);
CREATE TABLE IF NOT EXISTS stat_versions(game_id TEXT,player_id TEXT,known_at TEXT,hash TEXT,payload TEXT,
 PRIMARY KEY(game_id,player_id,known_at,hash));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, kind TEXT, record_key TEXT, old TEXT, new TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS snapshots(kind TEXT, key TEXT, observed_at TEXT, retrieved_at TEXT,
 source TEXT, payload TEXT, hash TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS snap_kind ON snapshots(kind,observed_at);
CREATE TABLE IF NOT EXISTS freshness(kind TEXT PRIMARY KEY, verified_at TEXT, coverage_cutoff TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS update_runs(id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
 status TEXT, sources TEXT, errors TEXT, package TEXT);
CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY, at TEXT, scope TEXT, severity TEXT, code TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS offers(id TEXT PRIMARY KEY, board_id TEXT, game_id TEXT,player_id TEXT,
 market TEXT,line REAL,sides TEXT,offer_type TEXT,captured_at TEXT,expires_at TEXT,
 source TEXT,contest TEXT,rules_id TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS boards(id TEXT PRIMARY KEY, imported_at TEXT, captured_at TEXT, source TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, verified_at TEXT, effective_from TEXT, effective_to TEXT, source TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS model_runs(id TEXT PRIMARY KEY, created_at TEXT, data_hash TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS projections(id TEXT PRIMARY KEY,game_id TEXT,player_id TEXT,as_of TEXT,
 model_id TEXT,payload TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,offer_id TEXT,created_at TEXT,payload TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS settlements(entry_id TEXT PRIMARY KEY,settled_at TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,due_at TEXT,status TEXT,attempts INTEGER,last_at TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS service_status(name TEXT PRIMARY KEY,heartbeat TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY,owner TEXT,expires_at TEXT);
"""


class Store:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["data_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "nba.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Preserve repeated values at different knowledge times (A -> B -> A corrections).
        version_pk=[r["name"] for r in self.db.execute("PRAGMA table_info(stat_versions)") if r["pk"]]
        if "known_at" not in version_pk:
            self.db.executescript("""BEGIN IMMEDIATE;
                ALTER TABLE stat_versions RENAME TO stat_versions_v1;
                CREATE TABLE stat_versions(game_id TEXT,player_id TEXT,known_at TEXT,hash TEXT,payload TEXT,
                  PRIMARY KEY(game_id,player_id,known_at,hash));
                INSERT INTO stat_versions SELECT * FROM stat_versions_v1;
                DROP TABLE stat_versions_v1;
                COMMIT;""")
        self.db.execute("CREATE INDEX IF NOT EXISTS stat_versions_history ON stat_versions(player_id,known_at)")
        self.db.commit()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def check(self, scope, severity, code, detail):
        self.db.execute("INSERT INTO checks(at,scope,severity,code,detail) VALUES(?,?,?,?,?)",
                        (now(), scope, severity, code, dumps(detail)))

    def snapshot(self, kind, key, observed_at, source, payload, retrieved_at=None):
        observed_at = stamp(observed_at)
        row = (kind, str(key), observed_at, stamp(retrieved_at or now()), source, dumps(payload))
        h = digest([kind, str(key), observed_at, source, payload])
        self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?,?)", (*row, h))

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self,write=True):
        try:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise


def freshness(store, at=None):
    t = datetime.fromisoformat(stamp(at or now()))
    rows = {r["kind"]: r for r in store.rows("SELECT * FROM freshness")}
    result = {}
    for kind, hours in store.config["freshness_hours"].items():
        r = rows.get(kind)
        age = (t - datetime.fromisoformat(r["verified_at"])).total_seconds() / 3600 if r else None
        result[kind] = {**(r or {}), "age_hours": age, "stale": age is None or age < 0 or age > hours}
    return result


def latest_snapshots(store, kind, at=None):
    at = stamp(at or now())
    rows = store.rows("SELECT * FROM snapshots WHERE kind=? AND observed_at<=? AND retrieved_at<=? "
                      "ORDER BY observed_at,retrieved_at", (kind, at, at))
    latest = {}
    for r in rows:
        latest[r["key"]] = {**json.loads(r["payload"]), "observed_at": r["observed_at"],
                             "retrieved_at": r["retrieved_at"], "source": r["source"]}
    return latest
