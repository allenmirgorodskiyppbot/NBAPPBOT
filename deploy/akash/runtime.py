"""One Akash service: shared-volume dashboard, scheduler, and verified first seed.

No Akash wallet, provider secret, or uploaded workbook is part of the image.
The persistent state root is installed with one rename, never over a live database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import zipfile

LOG = logging.getLogger("nbabot.akash")
MAX_ARCHIVE = 4 * 1024**3
MAX_UNPACKED = 16 * 1024**3


class HTTPSOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("Seed redirect must stay HTTPS")
        return super().redirect_request(request, response, code, message, headers, new_url)


def install_seed(archive: Path, state: Path, expected: str):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("NBABOT_SEED_SHA256 must be a SHA256 checksum")
    with archive.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if checksum != expected.lower():
        raise ValueError("Seed archive checksum mismatch; existing state was not changed")
    if state.exists():
        raise ValueError("Refusing to replace an existing persistent state directory")
    state.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".seed-", dir=state.parent))
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise ValueError("Seed exceeds unpacked size limit")
            names = set()
            for info in infos:
                p = PurePosixPath(info.filename)
                if (p.is_absolute() or ".." in p.parts or "\\" in info.filename
                        or info.filename in names or not p.parts
                        or any(":" in part for part in p.parts)):
                    raise ValueError("Unsafe or duplicate seed archive path")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("Seed archive links are forbidden")
                names.add(info.filename)
            z.extractall(staging)
        manifest = json.loads((staging / "seed_manifest.json").read_text())
        for relative, digest in manifest["sha256"].items():
            target = staging / relative
            if target.resolve().is_relative_to(staging.resolve()) is False or not target.is_file():
                raise ValueError("Invalid seed manifest file")
            with target.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != digest:
                    raise ValueError("Seed member checksum mismatch")
        db = sqlite3.connect(staging / "nba.sqlite3")
        try:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Seed SQLite integrity check failed")
        finally:
            db.close()
        config = json.loads((staging / "config.json").read_text())
        config["data_dir"] = str(state.resolve())
        config["deployment_kind"] = "akash"
        (staging / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        version = manifest.get("latest_package")
        if not version or PurePosixPath(version).name != version or not (staging / "packages" / version).is_dir():
            raise ValueError("Seed requires one verified package")
        (staging / "packages" / "latest").symlink_to(version, target_is_directory=True)
        os.replace(staging, state)
        LOG.info("Installed verified seed into persistent state")
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def bootstrap(config_path: Path):
    init_mode = os.environ.get("NBABOT_INIT_MODE", "seed")
    if init_mode not in ("seed", "spreadsheet"):
        raise ValueError("NBABOT_INIT_MODE must be seed or spreadsheet")
    if init_mode == "spreadsheet":
        from nbabot.first_boot import prepare_spreadsheet_state
        prepare_spreadsheet_state(config_path, os.environ.get("NBABOT_CONFIG_TEMPLATE", "/app/config.example.json"))
        return
    state = config_path.parent
    if state.exists():
        if not config_path.is_file() or not (state / "nba.sqlite3").is_file():
            raise ValueError("Existing state is incomplete; restore a verified backup explicitly")
        if json.loads(config_path.read_text()).get("data_source_mode") == "spreadsheet":
            from nbabot.first_boot import prepare_spreadsheet_state
            prepare_spreadsheet_state(config_path)
        return
    url = os.environ.get("NBABOT_SEED_URL", "")
    expected = os.environ.get("NBABOT_SEED_SHA256", "")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("First boot requires NBABOT_SEED_URL (HTTPS) and NBABOT_SEED_SHA256")
    # Signed URLs can contain credentials. Never print the URL, response body, or headers.
    state.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".seed-download-", dir=state.parent)
    os.close(descriptor)
    archive = Path(filename)
    try:
        request = Request(url, headers={"User-Agent": "NBAPPBOT/0.1"})
        with build_opener(HTTPSOnlyRedirect()).open(request, timeout=60) as response, archive.open("wb") as out:
            if urlsplit(response.url).scheme != "https":
                raise ValueError("Seed redirect must stay HTTPS")
            total = 0
            while chunk := response.read(1024**2):
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise ValueError("Seed exceeds download size limit")
                out.write(chunk)
        install_seed(archive, state, expected)
        if json.loads(config_path.read_text()).get("data_source_mode") == "spreadsheet":
            from nbabot.first_boot import prepare_spreadsheet_state
            prepare_spreadsheet_state(config_path)
    except Exception as exc:
        # urllib errors may include a credential-bearing URL, so log type only.
        raise RuntimeError("Seed initialization failed (" + type(exc).__name__ + ")") from None
    finally:
        archive.unlink(missing_ok=True)


def health(config_path: Path):
    with urlopen("http://127.0.0.1:8000/health", timeout=5) as response:
        if json.load(response).get("status") != "ok":
            raise ValueError("Dashboard health check failed")
    from datetime import datetime, timezone
    data_dir = Path(json.loads(config_path.read_text())["data_dir"])
    pulse = json.loads((data_dir / "scheduler-heartbeat.json").read_text())["heartbeat"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(pulse)).total_seconds()
    if not 0 <= age < 90:
        raise ValueError("Scheduler heartbeat is stale")


def run(config_path: Path):
    bootstrap(config_path)
    base = [sys.executable, "-m", "nbabot", "--config", str(config_path)]
    subprocess.run(base + ["recover"], check=True)
    children = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen(base + ["serve", "--host", "0.0.0.0", "--port", "8000"]))
        children.append(subprocess.Popen(base + ["scheduler", "run"]))
        LOG.info("Dashboard and daily scheduler started under one supervisor")
        while not stopping:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A required service exited; terminating container for provider restart")
            time.sleep(1)
    finally:
        stop(None, None)
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    configured = Path(os.environ.get("NBABOT_CONFIG", "/data/state/config.json"))
    try:
        {"run": run, "health": health}[sys.argv[1] if len(sys.argv) > 1 else "run"](configured)
    except Exception as exc:
        LOG.error("Akash runtime failed: %s", str(exc))
        raise SystemExit(1) from None
