"""Conservative retention of reproducible published data packages.

The caller must hold ``.refresh.lock`` and call this only after a successful
publication transaction has committed. Original files and the historical
database are outside this module's deletion scope.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .core import now, stamp

LOG = logging.getLogger(__name__)


def _disk(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError as exc:
        return {"error": type(exc).__name__}


def prune_packages(store, at=None):
    """Remove only expired, recognized packages; return cleanup warnings.

    A retention of zero days disables pruning. At least the newest
    ``package_min_versions`` recognized versions, ``latest``, and the most
    recent committed refresh package always remain. Unknown directories,
    incomplete builds, and symlinks are never deletion candidates. A cleanup
    failure is reported here and must not roll back an already committed
    successful refresh.
    """
    days = store.config.get("package_retention_days", 14)
    minimum = store.config.get("package_min_versions", 7)
    if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or days < 0:
        raise ValueError("package_retention_days must be a nonnegative number")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 2:
        raise ValueError("package_min_versions must be an integer of at least two")
    current = datetime.fromisoformat(stamp(at or now()))
    cutoff = current - timedelta(days=days)
    root = store.root / "packages"
    result = {"status": "disabled" if days == 0 else "success", "at": stamp(current),
              "retention_days": days, "minimum_versions": minimum,
              "deleted": [], "protected": [], "skipped": [], "warnings": [],
              "disk": _disk(store.root)}
    if days == 0 or not root.exists():
        return result
    if root.is_symlink():
        result["status"] = "blocked"
        result["warnings"].append("Package root is a symlink; no packages were removed.")
        return result

    protected = set()
    latest = root / "latest"
    if latest.is_symlink():
        target = os.readlink(latest)
        # Publication uses a single relative directory name. Refuse to make
        # retention decisions from an unrecognized or escaping pointer.
        if not target or Path(target).is_absolute() or Path(target).name != target or target in (".", ".."):
            result["status"] = "blocked"
            result["warnings"].append("Unrecognized latest pointer; no packages were removed.")
            return result
        protected.add(target)
    elif latest.exists():
        result["status"] = "blocked"
        result["warnings"].append("latest is a user-created file or directory; no packages were removed.")
        return result

    committed = store.one("SELECT package FROM update_runs WHERE status='success' AND package IS NOT NULL "
                          "ORDER BY finished_at DESC LIMIT 1")
    if committed:
        # Absolute package paths can originate on the host that made a seed.
        # The immutable version name survives migration to another data root.
        protected.add(Path(committed["package"]).name)

    valid = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        result["status"] = "warning"
        result["warnings"].append(f"Cannot enumerate packages: {type(exc).__name__}")
        return result
    for path in children:
        if path.name == "latest":
            continue
        if path.is_symlink() or not path.is_dir():
            result["skipped"].append({"version": path.name, "reason": "symlink_or_non_directory"})
            continue
        manifest = path / "manifest.json"
        if manifest.is_symlink():
            result["skipped"].append({"version": path.name, "reason": "symlink_manifest"})
            continue
        try:
            payload = json.loads(manifest.read_text())
            created = datetime.fromisoformat(stamp(payload["created_at"]))
            if payload["version"] != path.name or not isinstance(payload.get("sha256"), dict):
                raise ValueError("Unrecognized package manifest")
            if payload.get("validation", {}).get("status") not in ("passed", "passed_with_limitations"):
                raise ValueError("Unverified package")
            if not isinstance(payload.get("reason"), str) or not payload["reason"]:
                raise ValueError("Missing package provenance")
            run = payload.get("last_successful_refresh")
            if run is not None and (not isinstance(run, dict) or not run.get("id") or
                    not store.one("SELECT 1 FROM update_runs WHERE id=? AND status='success'", (run["id"],))):
                raise ValueError("Refresh package does not have a committed successful run")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["skipped"].append({"version": path.name, "reason": "incomplete_or_unrecognized_manifest",
                                      "error_type": type(exc).__name__})
            continue
        valid.append((created, path))

    valid.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    protected.update(path.name for _, path in valid[:minimum])
    result["protected"] = sorted(protected)
    for created, path in valid:
        if path.name in protected or created >= cutoff:
            continue
        try:
            # Never follow a directory that was replaced with a link while
            # retention was running. CPython's fd-based rmtree also protects
            # traversal through symlinks inside a package on Linux.
            if path.is_symlink() or not path.is_dir():
                raise OSError("Package changed during pruning")
            shutil.rmtree(path)
            result["deleted"].append(path.name)
            LOG.info("Removed expired data package %s", path.name)
        except OSError as exc:
            result["warnings"].append(f"Could not remove {path.name}: {type(exc).__name__}")
            LOG.warning("Could not remove expired data package %s: %s", path.name, type(exc).__name__)
    if result["warnings"]:
        result["status"] = "warning"
    result["disk"] = _disk(store.root)
    return result
