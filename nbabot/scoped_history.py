"""Targeted historical enrichment and honest prospective feature cutoffs."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from .core import digest,dumps,freshness,now,stamp
from .scope import matchup_games,matchup_scope


def _atomic(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.parent/("."+path.name+"-"+uuid.uuid4().hex)
    try:
        temp.write_text(dumps(payload))
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def archive_feature_snapshot(store):
    """Archive what exists now; never backdate a newly fetched historical feature."""
    captured=now()
    root=store.root/"reference"/"pregame_snapshots"
    indices={}
    for name in ("career","opportunities","matchups","rotations","context"):
        path=store.root/"reference"/name/"latest.json"
        if path.is_file():
            raw=path.read_bytes()
            indices[name]={"sha256":digest(raw),"index":json.loads(raw)}
    audit=store.root/"reference/latest.json"
    if audit.is_file():
        raw=audit.read_bytes()
        indices["historical_audit"]={"sha256":digest(raw),"index":json.loads(raw)}
    scope=matchup_scope(store)
    upcoming=store.rows("""SELECT id,date_et,tipoff,home,away,season,known_at,source FROM games
        WHERE status='scheduled' AND (home='NYK' OR away='NYK') AND tipoff>?
        AND known_at<=? ORDER BY tipoff LIMIT 10""",(captured,captured))
    payload={"schema_version":1,"captured_at":captured,"prediction_cutoff":captured,
        "scope":scope,"upcoming_games":upcoming,"freshness":freshness(store,captured),
        "canonical_record_fingerprint":digest(store.rows("SELECT game_id,player_id,hash,known_at FROM player_games ORDER BY game_id,player_id")),
        "canonical_coverage":store.one("SELECT min(date_et) first,max(date_et) last,count(*) player_games FROM player_games"),
        "latest_model":store.one("SELECT id,created_at,data_hash FROM model_runs ORDER BY created_at DESC LIMIT 1"),
        "archived_predictions":store.rows("SELECT id,game_id,player_id,as_of,model_id,status FROM projections WHERE as_of<=? ORDER BY as_of DESC LIMIT 500",(captured,)),
        "reference_indices":indices,
        "meaning":"Prospective cutoff captured now. Older retrieved game statistics remain retrospective; this does not reconstruct historical pregame knowledge or validate a model."}
    filename=captured.replace(":","").replace("+","_")+"_"+uuid.uuid4().hex[:8]+".json"
    path=root/filename
    _atomic(path,payload)
    meta={"file":filename,"sha256":digest(path.read_bytes()),"captured_at":captured,
        "upcoming_games":len(upcoming),"reference_names":sorted(indices),
        "meaning":payload["meaning"]}
    _atomic(root/"latest.json",meta)
    return meta


def load_feature_snapshot(store):
    root=store.root/"reference/pregame_snapshots"
    latest=root/"latest.json"
    if not latest.is_file():
        return {"status":"not_collected","snapshots":[]}
    metadata=json.loads(latest.read_text())
    name=metadata["file"]
    if Path(name).name!=name:
        raise ValueError("Invalid prospective snapshot path")
    path=root/name
    if path.is_symlink() or digest(path.read_bytes())!=metadata["sha256"]:
        raise ValueError("Prospective snapshot checksum mismatch")
    return {"status":"prospective_only","latest":metadata,
        "snapshot":json.loads(path.read_text()),
        "historical_pregame_vintages":"Not reconstructed; earlier games remain unavailable for strict as-of testing"}


def rotation_batch(store,games,scope,batch_size,force=False):
    """Prioritize new fixtures and bound slow optional historical retries."""
    if batch_size is None:
        return games
    if isinstance(batch_size,bool) or not isinstance(batch_size,int) or batch_size<1:
        raise ValueError("historical_rotation_batch_size must be a positive integer")
    root=store.root/"reference/rotations"
    path=root/"latest.json"
    index=json.loads(path.read_text()) if path.is_file() else {}
    entries={r["game_id"]:r for r in index.get("games",[])}
    errors={r["game_id"]:r for r in index.get("errors",[]) if r.get("game_id")}
    current=datetime.fromisoformat(now())
    candidates=[]
    for game in games:
        gid=game["game_id"]; entry=entries.get(gid)
        if entry and not force:
            relative=Path(entry.get("path",""))
            expected=digest(sorted(set(scope["selected_ids"])|set(scope["by_opponent"].get(game["opponent"],[]))))
            capture=root/relative
            if (not relative.is_absolute() and ".." not in relative.parts and capture.is_file()
                and not capture.is_symlink() and entry.get("scope_sha256")==expected
                and digest(capture.read_bytes())==entry.get("sha256")):
                continue
        error=errors.get(gid)
        if error and not force:
            try:
                age=(current-datetime.fromisoformat(stamp(error.get("attempted_at") or index.get("attempted_at")))).total_seconds()/3600
                if 0<=age<float(store.config.get("historical_retry_hours",24)):
                    continue
            except (TypeError,ValueError):
                pass
        candidates.append((bool(error),error.get("attempted_at","") if error else "",game))
    # Unseen new games first; historical failures rotate by oldest attempt time.
    candidates.sort(key=lambda r:(r[0],r[1],-int(r[2]["date_et"].replace("-",""))))
    return [r[2] for r in candidates[:batch_size]]


def collect_scoped_history(store,seasons=None,force=False,include_career=True,rotation_batch_size=None):
    """Caller holds the maintenance lock; only named matchups are collected."""
    scope=matchup_scope(store)
    seasons=seasons or ["2024-25","2025-26",store.config["season"]]
    results={}; errors=[]
    if include_career:
        try:
            from .career_reference import collect_career_reference
            results["career"]=collect_career_reference(store,refresh=force)
        except Exception as exc:
            errors.append({"source":"career","error_type":type(exc).__name__})
    try:
        from .career_reference import load_career_reference
        career_rows=load_career_reference(store).get("rows",[])
    except (OSError,ValueError,KeyError):
        career_rows=[]
    games=matchup_games(store,seasons,career_rows)
    from .opportunity_reference import collect_opportunity_reference
    from .matchup_reference import collect_matchup_reference
    from .rotation_reference import collect_rotation_reference
    rotation_games=rotation_batch(store,games,scope,rotation_batch_size,force)
    calls={
        "opportunities":lambda:collect_opportunity_reference(store,games=games,cohort_by_team=scope["by_opponent"],force=force),
        "matchups":lambda:collect_matchup_reference(store,seasons=seasons,games=games,force=force),
        "rotations":lambda:collect_rotation_reference(store,games=rotation_games,cohort_by_team=scope["by_opponent"],force=force),
    }
    for name,call in calls.items():
        try:
            results[name]=call()
        except Exception as exc:
            errors.append({"source":name,"error_type":type(exc).__name__})
    partial=bool(errors) or any(r.get("status") not in ("success","passed") for r in results.values())
    report={"status":"partial" if partial else "success","attempted_at":now(),
        "selected_player_count":len(scope["selected_ids"]),"pair_rules":len(scope["pairs"]),
        "scope_unresolved":scope["unresolved"],"target_games":len(games),"seasons":seasons,
        "rotation_batch_requested":len(rotation_games),"rotation_batch_limit":rotation_batch_size,
        "results":results,"errors":errors,
        "meaning":"Scoped historical references only. Preserved source failures and conflicts do not become zeros or validated projection effects."}
    root=store.root/"reference/scoped_updates"
    _atomic(root/(uuid.uuid4().hex+".json"),report)
    _atomic(root/"latest.json",report)
    report["prospective_snapshot"]=archive_feature_snapshot(store)
    return report
