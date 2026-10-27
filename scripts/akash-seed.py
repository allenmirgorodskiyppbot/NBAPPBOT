#!/usr/bin/env python3
"""Make a portable seed/backup without copying a live SQLite WAL database."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
import zipfile


def create_seed(config_path: Path, output: Path):
    config = json.loads(config_path.read_text())
    data = Path(config.get("data_dir", "data")).resolve()
    latest = data / "packages" / "latest"
    if not (latest / "manifest.json").is_file():
        raise ValueError("Export a verified data package before creating a seed")
    if output.exists():
        raise ValueError("Output exists; choose a new dated backup filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name("." + output.name + ".building")
    if temporary_output.exists():
        raise ValueError("Previous unfinished backup exists; inspect it before retrying")
    with (data / ".refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix="nbabot-seed-") as directory:
            staging = Path(directory)
            db = sqlite3.connect(data / "nba.sqlite3")
            copied = sqlite3.connect(staging / "nba.sqlite3")
            try:
                db.backup(copied)
                copied.execute("DELETE FROM locks")
                copied.execute("DELETE FROM service_status")
                copied.execute("UPDATE jobs SET status='retry',last_at=NULL WHERE status='running'")
                copied.commit()
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite backup failed integrity check")
            finally:
                db.close()
                copied.close()
            config["data_dir"] = "/data/state"
            config["deployment_kind"] = "akash"
            if config.get("provider_file"):
                raise ValueError("Local provider_file is not portable; configure a deployment-accessible feed first")
            (staging / "config.json").write_text(json.dumps(config, indent=2) + "\n")
            for name in ("originals", "reference"):
                if (data / name).exists():
                    shutil.copytree(data / name, staging / name, symlinks=False)
            package_version = latest.resolve().name
            destination = staging / "packages" / package_version
            destination.parent.mkdir(parents=True)
            manifest = json.loads((latest / "manifest.json").read_text())
            for name, expected in manifest.get("sha256", {}).items():
                relative=PurePosixPath(name)
                if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in name or any(":" in part for part in relative.parts):
                    raise ValueError("Invalid published package manifest path")
                if not (latest/name).resolve().is_relative_to(latest.resolve()):
                    raise ValueError("Published package path escapes package")
                with (latest / name).open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                        raise ValueError("Published package member checksum mismatch: " + name)
            if any(item.is_symlink() for item in latest.rglob("*")):
                raise ValueError("Published package member links are forbidden")
            shutil.copytree(latest,destination,symlinks=False)
            hashes = {}
            for item in sorted(staging.rglob("*")):
                if item.is_file():
                    with item.open("rb") as source:
                        hashes[item.relative_to(staging).as_posix()] = hashlib.file_digest(source, "sha256").hexdigest()
            seed_manifest = {"created_at":datetime.now(timezone.utc).isoformat(),
                             "latest_package":package_version,"sha256":hashes,
                             "scope":"Consistent canonical DB with source histories; originals, reference audit, latest verified package. Runtime locks and heartbeats cleared in backup only."}
            (staging / "seed_manifest.json").write_text(json.dumps(seed_manifest, indent=2) + "\n")
            try:
                with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
                    for item in sorted(staging.rglob("*")):
                        if item.is_file():
                            archive.write(item, item.relative_to(staging).as_posix())
                temporary_output.rename(output)
            finally:
                temporary_output.unlink(missing_ok=True)
    with output.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(checksum + "  " + output.name + "\n")
    return {"archive":str(output.resolve()),"bytes":output.stat().st_size,"sha256":checksum,
            "contains_credentials":"Config contains environment variable names, not environment token values. Review provider URL/config and private source content before hosting archive."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(create_seed(args.config, args.output), indent=2))
