from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .core import ET, freshness, now
from .ingest import validate
from .model import upcoming_games
from .offers import bankroll, current_offers
from .scheduler import status as scheduler_status


def public_projection(p):
    return {**p,"markets":{m:{k:v for k,v in d.items() if k!="samples"} for m,d in p.get("markets",{}).items()}}


def overview(store):
    spreadsheet=store.config.get("data_source_mode")=="spreadsheet"
    from .first_boot import read_status
    master=None
    if spreadsheet:
        from .master_source import master_status
        master=master_status(store)
    projections=[public_projection(json.loads(r["payload"])) for r in store.rows("SELECT * FROM projections WHERE status='active' ORDER BY game_id,player_id")]
    decisions=[json.loads(r["payload"]) for r in store.rows("SELECT * FROM decisions WHERE status='active' ORDER BY created_at")]
    games=upcoming_games(store,limit=10)
    for g in games:
        g["tipoff_et"]=datetime.fromisoformat(g["tipoff"]).astimezone(ET).strftime("%a %b %d, %Y · %I:%M %p %Z")
    latest=store.root/"packages/latest"
    manifest=json.loads((latest/"manifest.json").read_text()) if (latest/"manifest.json").exists() else None
    return {"generated_at":now(),"games":games,"projections":projections,"offers":current_offers(store),
        "data_source_mode":store.config.get("data_source_mode","online"),"master_source":master,
        "external_sources":{"status":"approved" if store.config.get("external_sources_approved") is True and store.config.get("external_source_approvals") else "awaiting_approval",
            "approved_hosts":store.config.get("external_source_approvals",[]),"integrations_available":True},
        "initialization":read_status(store.root),
        "decisions":decisions,"freshness":freshness(store),"scheduler":scheduler_status(store),"package":manifest,
        "last_update":store.one("SELECT * FROM update_runs ORDER BY started_at DESC LIMIT 1"),
        "last_successful_update":store.one("SELECT * FROM update_runs WHERE status='success' ORDER BY started_at DESC LIMIT 1"),
        "validation":validate(store),"bankroll":bankroll(store),
        "news":[json.loads(r["payload"]) for r in store.rows("SELECT payload FROM snapshots WHERE kind='news' ORDER BY observed_at DESC LIMIT 30")],
        "evaluations":[json.loads(r["payload"]) for r in store.rows("SELECT payload FROM evaluations ORDER BY created_at DESC LIMIT 4")],
        "status":"experimental_projections_only" if not any(d.get("recommendation") for d in decisions) else "supported_selections",
        "limitations":(["Only the owner-uploaded master workbook supplies data; daily processing does not add new games or verify online updates."] if spreadsheet else [])+["No picks without current imported offers, verified sources and validated market policy.",
            "DNP, unknown availability, and missing statistics are distinct from a played zero.",
            "No reliable joint-entry probability or profitability claim before dependence and settlement validation."]}


def write_report(store,directory="reports"):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    data=overview(store)
    (directory/"upcoming.json").write_text(json.dumps(data,indent=2,default=str))
    lines=["# Knicks projection report",f"Generated {data['generated_at']}","", "Status: experimental; no validated picks.",""]
    if data["package"]:
        lines += ["Imported coverage: "+str(data["package"]["coverage"]),""]
    lines += ["## Upcoming games",""]
    for g in data["games"]:
        lines.append(f"- {g['away']} at {g['home']} — {g['tipoff_et']} ({g['id']}); schedule snapshot requires current verification.")
    lines += ["","## Freshness",""]
    for k,v in data["freshness"].items():
        lines.append(f"- {k}: {'STALE / MISSING' if v['stale'] else 'fresh'}; verified at {v.get('verified_at','never')}")
    lines += ["","## Conditional projections", "", "| Player | Team | Market | Mean | Median | 80% interval | Prior games |", "|---|---|---|---:|---:|---|---:|"]
    for p in data["projections"]:
        for m,d in p["markets"].items():
            lines.append(f"| {p['player']} | {p['team']} | {m} | {d['mean']:.2f} | {d['median']:.1f} | {d['interval_80']} | {p['sample_games']} |")
    lines += ["","All projections are uncalibrated sensitivity estimates conditional on playing. Historical roster candidates are not verified current rosters.",
        "","## Selections", "", "No qualifying picks." if not any(d["recommendation"] for d in data["decisions"]) else "See JSON for candidates."]
    for d in data["decisions"]:
        lines.append(f"- {d['player']} {d['side']} {d['line']} {d['market']}: {', '.join(d['skip_reasons']) or 'qualifying'}")
    if not data["offers"]:
        lines.append("Actual PrizePicks board and applicable contest rules have not been supplied; no lines have been invented.")
    lines += ["","## Operations", "",data["scheduler"]["note"], "", "Last successful live refresh: "+str(data["last_successful_update"]),
              "", "## Limitations",""]+data["limitations"]
    if data["last_update"]:
        lines += ["","Last attempted refresh: "+data["last_update"]["status"],"Failed source details: "+data["last_update"]["errors"]]
    (directory/"upcoming.md").write_text("\n".join(lines)+"\n")
    headers=["game_id","player_id","player","team","market","mean","median","interval_low","interval_high","sample_games","as_of","flags"]
    with (directory/"projections.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=headers); writer.writeheader()
        for p in data["projections"]:
            for m,d in p["markets"].items():
                row={k:p[k] for k in ("game_id","player_id","player","team","sample_games","as_of")}
                row.update({"market":m,"mean":d["mean"],"median":d["median"],"interval_low":d["interval_80"][0],
                            "interval_high":d["interval_80"][1],"flags":"|".join(p["flags"])})
                writer.writerow(row)
    return {"report":str((directory/"upcoming.md").resolve()),"json":str((directory/"upcoming.json").resolve()),
            "csv":str((directory/"projections.csv").resolve()),"projected_players":len(data["projections"]),"offers":len(data["offers"])}
