from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import digest, dumps, now, stamp
from .maintenance import refresh

LOG=logging.getLogger(__name__)


def heartbeat_file(store):
    root=store.root
    target=root/"scheduler-heartbeat.json"
    temp=root/(".scheduler-heartbeat-"+str(os.getpid())+"-"+str(threading.get_ident()))
    temp.write_text(dumps({"heartbeat":now(),"pid":os.getpid(),"deployment":os.environ.get("NBABOT_DEPLOYMENT_KIND","current_instance_only")}))
    os.replace(temp,target)


def plan_jobs(store,at=None):
    at=stamp(at or now()); current=datetime.fromisoformat(at)
    local=current.astimezone(ZoneInfo(store.config["timezone"]))
    hour,minute=map(int,store.config["daily_time"].split(":"))
    due=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    jobs=[("daily:"+local.date().isoformat(),stamp(due))]
    for game in store.rows("SELECT * FROM games WHERE (home='NYK' OR away='NYK') AND status='scheduled' AND tipoff>?",(at,)):
        tip=datetime.fromisoformat(game["tipoff"])
        if tip-current>timedelta(days=2):
            continue
        for offset in store.config["pregame_minutes"]:
            t=tip-timedelta(minutes=float(offset))
            jobs.append(("pregame:"+game["id"]+":"+digest(game["tipoff"])[:8]+":"+str(offset),stamp(t)))
    return jobs


def tick(store,at=None,runner=refresh):
    at=stamp(at or now())
    service={"pid":os.getpid(),"daily_time":store.config["daily_time"],"timezone":store.config["timezone"],
             "pregame_minutes":store.config["pregame_minutes"],"deployment":os.environ.get("NBABOT_DEPLOYMENT_KIND","current_instance_only")}
    store.db.execute("INSERT OR REPLACE INTO service_status VALUES('scheduler',?,?)",(now(),dumps(service)))
    heartbeat_file(store)
    planned=dict(plan_jobs(store,at))
    for key,due in planned.items():
        store.db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,'pending',0,NULL,NULL)",(key,due))
    for job in store.rows("SELECT key FROM jobs WHERE status IN ('pending','retry') AND key LIKE 'pregame:%'"):
        if job["key"] not in planned:
            store.db.execute("UPDATE jobs SET status='obsolete' WHERE key=?",(job["key"],))
    store.db.commit()
    pending=store.rows("SELECT * FROM jobs WHERE due_at<=? AND status IN ('pending','retry','running') ORDER BY due_at",(at,))
    ran=[]
    for job in pending:
        # Postponed/rescheduled pregame jobs are discarded; do not act on obsolete tipoffs.
        if job["key"].startswith("pregame:") and job["key"] not in {j[0] for j in plan_jobs(store,at)}:
            store.db.execute("UPDATE jobs SET status='obsolete' WHERE key=?",(job["key"],)); store.db.commit(); continue
        last=datetime.fromisoformat(job["last_at"]) if job["last_at"] else None
        delay=[60,300,900][min(max(job["attempts"]-1,0),2)]
        if last and (datetime.fromisoformat(at)-last).total_seconds()<delay:
            continue
        if job["attempts"]>=4:
            store.db.execute("UPDATE jobs SET status='exhausted' WHERE key=?",(job["key"],)); store.db.commit(); continue
        # Cross-process lease prevents concurrent refresh runs against the same SQLite database.
        owner=str(os.getpid())+":"+digest(at)[:8]
        with store.transaction():
            lock=store.one("SELECT * FROM locks WHERE name='refresh'")
            if lock and lock["expires_at"]>now():
                return ran
            expires=stamp(datetime.fromisoformat(now())+timedelta(minutes=20))
            store.db.execute("INSERT OR REPLACE INTO locks VALUES('refresh',?,?)",(owner,expires))
            store.db.execute("UPDATE jobs SET status='running',attempts=attempts+1,last_at=? WHERE key=?",(at,job["key"]))
        try:
            result=runner(store)
            if result["status"]=="busy":
                store.db.execute("UPDATE jobs SET status='pending',attempts=attempts-1,last_at=NULL WHERE key=?",(job["key"],))
                continue
            status=("success" if result["status"] in ("success","unchanged") else
                "waiting_for_input" if result["status"]=="waiting_for_master" else "retry")
            store.db.execute("UPDATE jobs SET status=?,error=? WHERE key=?",(status,dumps(result.get("errors",[])),job["key"]))
            ran.append({"job":job["key"],"status":status})
        except Exception as e:
            LOG.exception("Scheduled refresh failed")
            store.db.rollback()
            store.db.execute("UPDATE jobs SET status='retry',error=? WHERE key=?",(type(e).__name__,job["key"]))
        finally:
            store.db.execute("DELETE FROM locks WHERE name='refresh' AND owner=?",(owner,))
            store.db.commit()
    return ran


def run(store):
    LOG.info("Scheduler running in this instance: daily %s %s",store.config["daily_time"],store.config["timezone"])
    def pulse():
        while True:
            heartbeat_file(store)
            time.sleep(15)
    threading.Thread(target=pulse,daemon=True,name="scheduler-heartbeat").start()
    while True:
        try:
            tick(store)
        except sqlite3.OperationalError as e:
            store.db.rollback()
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            LOG.warning("Database busy during another maintenance command; polling again in 15 seconds")
        time.sleep(15)


def status(store):
    row=store.one("SELECT * FROM service_status WHERE name='scheduler'")
    heartbeat=row["heartbeat"] if row else None
    deployment="current_instance_only"
    try:
        captured=json.loads((store.root/"scheduler-heartbeat.json").read_text())
        heartbeat=captured["heartbeat"]
        deployment=captured.get("deployment",deployment)
    except (OSError,ValueError,KeyError):
        pass
    age=(datetime.fromisoformat(now())-datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
    heartbeat_current=age is not None and 0<=age<90
    installed=False
    if shutil.which("systemctl"):
        result=subprocess.run(["systemctl","--user","is-active","nbabot-scheduler.service"],capture_output=True,timeout=5)
        installed=result.returncode==0
    return {"heartbeat":heartbeat,"operating_in_this_instance":heartbeat_current,
        "persistent_systemd_service_active":installed,"automatic_updates_persistently_active":installed and heartbeat_current,
        "deployment_kind":deployment,"external_lease_verification":"not_checked" if deployment=="akash" else "not_applicable",
        "daily_time":store.config["daily_time"],"timezone":store.config["timezone"],"pregame_minutes":store.config["pregame_minutes"],
        "jobs":store.rows("SELECT * FROM jobs ORDER BY due_at DESC LIMIT 30"),
        "next_planned":plan_jobs(store),"note":("Scheduler heartbeat is runtime evidence only. Verify the funded, active Akash lease and its persistent storage in Akash Console; the app does not query the lease."
            if deployment=="akash" else "A cloud chat process is temporary. Deploy the supplied Akash/Compose stack or install the systemd service on an always-on host.")}


def retry_failed(store):
    cursor=store.db.execute("UPDATE jobs SET status='pending',attempts=0,last_at=NULL,error=NULL WHERE status IN ('retry','exhausted')")
    store.db.commit()
    return {"requeued_jobs":cursor.rowcount,"note":"The running scheduler will retry these jobs; check actual outcomes in scheduler status"}


def install_systemd(config_path):
    if not shutil.which("systemctl"):
        raise ValueError("systemd unavailable; deploy with Docker Compose on an always-on host")
    probe=subprocess.run(["systemctl","--user","show-environment"],capture_output=True,timeout=5)
    if probe.returncode:
        raise ValueError("No user systemd manager. Use the supplied Compose deployment on a persistent host.")
    working=str(Path.cwd()); config=str(Path(config_path).resolve())
    if any(c in working+config+sys.executable for c in ('\n','\r','"','%')):
        raise ValueError("Unsupported systemd path characters")
    unit=f'''[Unit]
Description=NBAPPBOT daily and pregame maintenance
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working}
ExecStart="{sys.executable}" -m nbabot --config "{config}" scheduler run
Restart=on-failure
RestartSec=15
UMask=0077

[Install]
WantedBy=default.target
'''
    dest=Path.home()/".config/systemd/user/nbabot-scheduler.service"
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() and dest.read_text()!=unit:
        raise ValueError("Existing service differs; preserve it and review the deployment documentation")
    dest.write_text(unit)
    subprocess.run(["systemctl","--user","daemon-reload"],check=True)
    subprocess.run(["systemctl","--user","enable","--now","nbabot-scheduler.service"],check=True)
    return {"installed":str(dest),"note":"Enable user lingering if this host stops user services at logout; verify with scheduler status."}
