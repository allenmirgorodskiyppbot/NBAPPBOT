"""Empty, authenticated master-first state for an Akash deployment.

No public source bootstrap or provider access occurs here. Outside integrations
remain available for later explicit owner approval; the master is primary now.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .core import DEFAULT, Store, dumps, now

LOG = logging.getLogger(__name__)
STATUS_FILE = "first_boot_status.json"
STATE_MARKER = "spreadsheet_state.json"
PENDING_SOURCE_SETTINGS = {
    "nba_enabled": False,
    "public_context_enabled": False,
    "provider_url": None,
    "provider_file": None,
    "provider_allowed_hosts": [],
    "historical_context_enabled": False,
    "scoped_history_enabled": False,
}


def _approved(config):
    return config.get("external_sources_approved") is True and bool(config.get("external_source_approvals"))


def _source_policy(data_dir):
    """Read approval/configuration only; never contact an external source."""
    root = Path(data_dir)
    name = "config.json"
    marker = root / STATE_MARKER
    try:
        if marker.is_file():
            name = json.loads(marker.read_text()).get("config_file", name)
        if Path(name).name != name:
            raise ValueError("Invalid configuration filename")
        path = root / name
        config = json.loads(path.read_text()) if path.is_file() else {}
        approved = _approved(config)
        configured = any(config.get(key) for key in
                         ("nba_enabled", "public_context_enabled", "historical_context_enabled", "scoped_history_enabled", "provider_url", "provider_file"))
        note = ("The master workbook remains primary. Outside access is approved only for the recorded hosts and explicitly configured integrations."
                if approved else "The master workbook is primary for now. Outside sources await owner approval; scheduled processing uses existing uploaded data.")
        return {"source_policy": "master_primary", "outside_access": "approved" if approved else "awaiting_owner_approval",
                "external_refresh_enabled": bool(approved and configured),
                "approved_external_hosts": config.get("external_source_approvals", []) if approved else [],
                "source_note": note}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"source_policy": "master_primary", "outside_access": "awaiting_owner_approval", "external_refresh_enabled": False,
                "approved_external_hosts": [], "source_note": "The master remains primary; outside approval metadata is unavailable and no outside access is assumed."}


def _atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            output.write(dumps(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_private_auth():
    """Validate configured values without recording credentials anywhere."""
    values = [os.environ.get(name, "") for name in
              ("NBABOT_DASHBOARD_USER", "NBABOT_DASHBOARD_PASSWORD")]
    for value in values:
        normalized = value.strip().casefold()
        if (not normalized or normalized in {"password", "changeme", "change-me", "change_me", "todo"}
                or normalized.startswith(("replace", "your_", "your-", "<"))):
            raise ValueError("Spreadsheet mode requires both NBABOT_DASHBOARD_USER and NBABOT_DASHBOARD_PASSWORD with non-placeholder values")
    return True


def write_status(data_dir, status, **details):
    """Write source/readiness metadata; this is not a live scheduler heartbeat."""
    if status not in {"waiting_for_master", "validating_master", "ready", "error", "refreshing"}:
        raise ValueError("Unsupported spreadsheet initialization status")
    policy = _source_policy(data_dir)
    payload = {
        "mode": "spreadsheet", "status": status, "updated_at": now(),
        "note": policy["source_note"], "note_is_source_policy": "note" not in details,
        **details,
    }
    # Callers cannot override the source policy through display metadata.
    payload.update(mode="spreadsheet", status=status, **policy)
    _atomic(Path(data_dir) / STATUS_FILE, payload)
    return payload


def read_status(data_dir):
    path = Path(data_dir) / STATUS_FILE
    if not path.is_file():
        return {"mode": None, "status": "not_requested"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("mode") != "spreadsheet":
            raise ValueError("Unknown first-boot state")
        policy = _source_policy(data_dir)
        if value.get("note_is_source_policy") or str(value.get("note", "")).startswith("Only an owner-uploaded master workbook"):
            value["note"] = policy["source_note"]
        value.update(policy)
        return value
    except (OSError, ValueError, TypeError) as exc:
        return {"mode": "spreadsheet", "status": "error", "error_type": type(exc).__name__,
                **_source_policy(data_dir),
                "note": "Initialization status metadata is unreadable; the database was not changed."}


def _existing_state(config_path):
    state = config_path.parent
    if not config_path.is_file() or not (state / "nba.sqlite3").is_file():
        raise ValueError("Existing state is incomplete; restore a verified backup explicitly")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("data_source_mode") != "spreadsheet":
        raise ValueError("Refusing to reuse a database from another data mode for spreadsheet-only operation")
    marker_path = state / STATE_MARKER
    if not marker_path.is_file():
        raise ValueError("Spreadsheet-only state marker is missing; restore a verified spreadsheet-only backup")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("mode") != "spreadsheet" or marker.get("created_empty") is not True:
        raise ValueError("Unrecognized spreadsheet-only state marker")
    db = sqlite3.connect(state / "nba.sqlite3")
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Existing spreadsheet database failed its integrity check")
    finally:
        db.close()
    updated = {**config, "data_dir": str(state.resolve()), "deployment_kind": "akash"}
    updated.setdefault("external_sources_approved", False)
    updated.setdefault("external_source_approvals", [])
    if not _approved(updated):
        updated.update(PENDING_SOURCE_SETTINGS)
    if updated != config:
        _atomic(config_path, updated)
    if marker.get("config_file") != config_path.name:
        _atomic(marker_path, {**marker, "config_file": config_path.name})
    if not (state / STATUS_FILE).exists():
        # Metadata loss must not claim a new empty database or erase old records.
        write_status(state, "error", error_type="MissingStatusMetadata",
                     note="Spreadsheet state exists, but readiness metadata is missing. Review or re-upload the master; existing records were preserved.")
    return {"created": False, "mode": "spreadsheet", "state": str(state.resolve()), "status": read_status(state)}


def prepare_spreadsheet_state(config_path, template_path="/app/config.example.json"):
    """Initialize once, keep records on restart, and reject mixed-source state."""
    require_private_auth()
    config_path = Path(config_path)
    state = config_path.parent
    state.parent.mkdir(parents=True, exist_ok=True)
    with (state.parent / ".spreadsheet-state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if state.exists():
            return _existing_state(config_path)
        template = json.loads(Path(template_path).read_text(encoding="utf-8"))
        if not isinstance(template, dict):
            raise ValueError("Spreadsheet template must be a JSON configuration object")
        # This config starts with public application defaults, never a user's
        # local config, database, credentials or uploaded original workbook.
        allowed = {**DEFAULT, **{key: value for key, value in template.items() if key in DEFAULT}}
        config = {**allowed, **PENDING_SOURCE_SETTINGS, "data_source_mode": "spreadsheet",
                  "external_sources_approved": False, "external_source_approvals": [],
                  "data_dir": str(state.resolve()), "deployment_kind": "akash"}
        staging = Path(tempfile.mkdtemp(prefix=".spreadsheet-state-", dir=state.parent))
        try:
            _atomic(staging / config_path.name, config)
            _atomic(staging / STATE_MARKER, {"schema_version": 1, "mode": "spreadsheet", "created_empty": True,
                    "created_at": now(), "source_policy": "master_primary", "config_file": config_path.name})
            store = Store({**config, "data_dir": str(staging)})
            store.close()
            write_status(staging, "waiting_for_master", uploaded_master_present=False)
            os.replace(staging, state)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    LOG.info("Created empty spreadsheet-only state; waiting for private master upload")
    return {"created": True, "mode": "spreadsheet", "state": str(state.resolve()), "status": read_status(state)}
