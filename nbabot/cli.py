from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .core import Store, load_config


def parser():
    p=argparse.ArgumentParser(description="Knicks projections and prospective paper analysis")
    p.add_argument("--config",default="config.local.json")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("init")
    s=sub.add_parser("bootstrap"); s.add_argument("--seasons",nargs="+",default=["2024-25","2025-26"])
    s=sub.add_parser("context",help="Supplement official historical team-game and season opportunity references")
    s.add_argument("--seasons",nargs="+",help="Default: configured current season")
    s=sub.add_parser("backfill-matchups",help="Collect selected-player careers and only explicitly named matchup game references")
    s.add_argument("--seasons",nargs="+",help="Matchup seasons; default 2024-25,2025-26 and configured current season")
    s.add_argument("--force",action="store_true",help="Re-fetch cached sources; previous verified versions remain archived")
    s.add_argument("--without-career",action="store_true",help="Use existing career archive while collecting game-level references")
    s.add_argument("--rotation-batch",type=int,help="Limit slow rotation requests; automatic refresh defaults to eight eligible games")
    s=sub.add_parser("import"); s.add_argument("paths",nargs="+")
    sub.add_parser("validate"); sub.add_parser("export"); sub.add_parser("refresh"); sub.add_parser("recover"); sub.add_parser("source-check")
    s=sub.add_parser("reference",help="Preserve the audited historical reference workbook for repeatable organized exports")
    s.add_argument("path")
    s=sub.add_parser("organize",help="Generate the organized historical workbook; daily export uses configured organized_players")
    s.add_argument("--output",default="reports/organized")
    s.add_argument("--players",nargs="+",help="Exactly seven or eight NBA IDs or exact player names; configure organized_players to use them in daily exports")
    s.add_argument("--audit",help="Optional audited historical reference workbook")
    s.add_argument("--csv",action="store_true",help="Also export canonical/reference tables as CSV")
    s=sub.add_parser("train",help="Register reproducible baseline specification; no season aggregates are fitted")
    s=sub.add_parser("evaluate"); s.add_argument("--mode",choices=["strict","retrospective"],default="strict")
    s=sub.add_parser("project"); s.add_argument("--games",type=int,default=3)
    s=sub.add_parser("offers"); s.add_argument("path")
    s=sub.add_parser("rules"); s.add_argument("path")
    sub.add_parser("rank")
    s=sub.add_parser("entries"); s.add_argument("--size",type=int,default=2); s.add_argument("--experimental",action="store_true")
    s=sub.add_parser("settle"); s.add_argument("entry_id"); s.add_argument("outcomes",nargs="+",choices=["win","loss","tie","dnp","void","reboot"])
    sub.add_parser("bankroll"); sub.add_parser("prospective")
    s=sub.add_parser("report"); s.add_argument("--output",default="reports")
    s=sub.add_parser("serve"); s.add_argument("--host",default="127.0.0.1"); s.add_argument("--port",type=int,default=8000)
    s=sub.add_parser("scheduler"); s.add_argument("action",choices=["status","run","tick","retry","install"])
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s",stream=sys.stderr)
    config=load_config(args.config)
    store=Store(config)
    try:
        c=args.command
        if config.get("data_source_mode")=="spreadsheet" and c in ("bootstrap","context","backfill-matchups","offers","rules") and not config.get("external_sources_approved"):
            raise ValueError("Focus remains on the master workbook; using another source requires owner approval first")
        if c=="init":
            result={"database":str(store.path),"config":args.config,"note":"No credentials or data invented"}
        elif c=="bootstrap":
            from .bootstrap import bootstrap_history
            result=bootstrap_history(store,args.seasons)
        elif c=="context":
            from .historical_context import collect_context_reference
            import fcntl
            with (store.root/".refresh.lock").open("w") as lock:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Another maintenance command is in progress")
                result=collect_context_reference(store,args.seasons)
        elif c=="backfill-matchups":
            from .scoped_history import collect_scoped_history
            import fcntl
            with (store.root/".refresh.lock").open("w") as lock:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Another maintenance command is in progress")
                result=collect_scoped_history(store,seasons=args.seasons,force=args.force,include_career=not args.without_career,rotation_batch_size=args.rotation_batch)
        elif c=="import":
            if config.get("data_source_mode")=="spreadsheet":
                from .master_source import import_master,refresh_master
                if len(args.paths)!=1:
                    raise ValueError("Supply exactly one master XLSX workbook")
                result=import_master(store,args.paths[0])
                result["processing"]=refresh_master(store)
            else:
                from .ingest import import_files
                result=import_files(store,args.paths)
        elif c=="validate":
            from .ingest import validate
            result=validate(store)
        elif c=="export":
            from .maintenance import export_package
            result={"package":export_package(store)}
        elif c=="reference":
            from .organized import archive_audit
            result=archive_audit(store,args.path)
        elif c=="organize":
            from .organized import build_organized_workbook
            import fcntl
            with (store.root/".refresh.lock").open("w") as lock:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("A refresh/bootstrap is in progress; organize after it completes")
                with store.transaction(write=False):
                    result=build_organized_workbook(store,args.output,audit_dir=args.audit,selected_players=args.players,
                        csv_dir=Path(args.output).with_suffix("")/"csv" if args.csv else None)
        elif c=="refresh":
            from .maintenance import refresh
            result=refresh(store)
        elif c=="recover":
            from .maintenance import recover
            result=recover(store)
        elif c=="source-check":
            from .maintenance import source_check
            result=source_check(store)
        elif c=="train":
            from .model import MODEL_SPEC,model_id
            result={"model_id":model_id(store),"specification":MODEL_SPEC}; store.db.commit()
        elif c=="evaluate":
            from .evaluation import evaluate
            result=evaluate(store,args.mode)
        elif c=="project":
            from .model import project_upcoming
            from .report import public_projection
            result=[public_projection(p) for p in project_upcoming(store,limit=args.games)]
        elif c=="offers":
            from .offers import import_board,rank
            from .model import project_upcoming
            result=import_board(store,args.path)
            project_upcoming(store)
            ranking=rank(store)
            result["analysis_status"]=ranking["status"]
        elif c=="rules":
            from .offers import import_rules
            result=import_rules(store,args.path)
        elif c=="rank":
            from .offers import rank
            result=rank(store)
        elif c=="entries":
            from .offers import build_entries
            if not 2<=args.size<=6:
                raise ValueError("Entry size must be 2–6")
            result=build_entries(store,size=args.size,experimental=args.experimental)
        elif c=="settle":
            from .offers import settle_entry
            result=settle_entry(store,args.entry_id,args.outcomes)
        elif c=="bankroll":
            from .offers import bankroll
            result=bankroll(store)
        elif c=="prospective":
            from .evaluation import prospective_report
            result=prospective_report(store)
        elif c=="report":
            from .report import write_report
            result=write_report(store,args.output)
        elif c=="serve":
            import uvicorn
            from .web import create_app
            store.close()
            uvicorn.run(create_app(config),host=args.host,port=args.port)
            return
        elif c=="scheduler":
            from .scheduler import install_systemd,retry_failed,run,status,tick
            if args.action=="run":
                run(store); return
            result={"status":status,"tick":tick,"retry":retry_failed}.get(args.action,lambda s:install_systemd(args.config))(store)
        print(json.dumps(result,indent=2,default=str,allow_nan=False))
        if isinstance(result,dict) and result.get("status")=="failed":
            return 2
    except (ValueError,KeyError,FileNotFoundError,json.JSONDecodeError) as e:
        print(json.dumps({"error":str(e)}),file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
