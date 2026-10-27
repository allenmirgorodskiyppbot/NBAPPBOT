from __future__ import annotations

import csv
import itertools
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from .core import MARKETS, digest, dumps, freshness, latest_snapshots, now, stamp
from .model import availability, probabilities, roster_for


def import_rules(store,path):
    r=json.loads(Path(path).read_text())
    for k in ("id","verified_at","effective_from","source_url","contest","eligibility","treatment","payouts","verified"):
        if k not in r:
            raise ValueError(f"Rules missing {k}")
    stamp(r["verified_at"]); stamp(r["effective_from"])
    if not r["source_url"].startswith("https://"):
        raise ValueError("Rules need authoritative evidence URL")
    if any(k not in r["treatment"] for k in ("tie","dnp","void","reboot")):
        raise ValueError("Explicit tie/DNP/void/reboot treatment required")
    if any(v not in ("remove","loss","win","refund_entry") for v in r["treatment"].values()):
        raise ValueError("Unsupported settlement treatment")
    old=store.one("SELECT payload FROM rules WHERE id=?",(r["id"],))
    if old and old["payload"]!=dumps(r):
        raise ValueError("Rules versions are immutable; use a new ID")
    store.db.execute("INSERT OR IGNORE INTO rules VALUES(?,?,?,?,?,?)",
        (r["id"],stamp(r["verified_at"]),stamp(r["effective_from"]),
         stamp(r["effective_to"]) if r.get("effective_to") else None,r["source_url"],dumps(r)))
    store.db.commit()
    return {"id":r["id"],"verified":r["verified"],"note":"User-supplied rules; no claim of independent current-platform verification"}


def import_board(store,path):
    path=Path(path)
    if path.suffix.lower()=="csv":
        raise ValueError("Use .csv extension")
    if path.suffix.lower()==".csv":
        with path.open(newline="",encoding="utf-8-sig") as f:
            records=list(csv.DictReader(f))
        if not records:
            raise ValueError("Empty board")
        obj={"board_id":digest(path.read_bytes())[:20],"offers":records}
    else:
        obj=json.loads(path.read_text()); records=obj["offers"]
    if not records:
        raise ValueError("Empty board")
    board_id=str(obj.get("board_id") or digest(obj)[:20])
    existing=store.one("SELECT * FROM boards WHERE id=?",(board_id,))
    h=digest(obj)
    if existing:
        if existing["hash"]!=h:
            raise ValueError("Board IDs are immutable; capture changes as a new board")
        return {"board_id":board_id,"status":"already_imported"}
    normalized=[]
    for i,row in enumerate(records):
        r={**{k:v for k,v in obj.items() if k not in ("offers","board_id")},**row}
        for k in ("player_id","game_id","market","line","sides","offer_type","captured_at","source_url","contest"):
            if k not in r or r[k] in (None,""):
                raise ValueError(f"Offer {i+1} missing {k}")
        if r.get("evidence_type")=="screenshot" and r.get("reviewed") not in (True,"true"):
            raise ValueError("Screenshot extraction must be reviewed before import")
        pid=str(r["player_id"]); gid=str(r["game_id"])
        alias=store.one("SELECT game_id FROM game_aliases WHERE alias=?",(gid,))
        if alias:
            gid=alias["game_id"]
        if not store.one("SELECT 1 FROM players WHERE id=?",(pid,)) or not store.one("SELECT 1 FROM games WHERE id=?",(gid,)):
            raise ValueError("Unknown player or game ID; resolve names and identifiers before offer import")
        if r["market"] not in MARKETS:
            raise ValueError("Only points, rebounds, assists and threes supported; combos withheld")
        line=float(r["line"])
        if not math.isfinite(line) or line<0:
            raise ValueError("Invalid line")
        sides=r["sides"] if isinstance(r["sides"],list) else r["sides"].split("|")
        if not sides or any(s not in ("more","less") for s in sides) or len(sides)!=len(set(sides)):
            raise ValueError("Sides must be unique more/less values")
        captured=stamp(r["captured_at"])
        if captured>now():
            raise ValueError("Offer capture cannot be in the future")
        expires=stamp(r["expires_at"]) if r.get("expires_at") else None
        if expires and expires<=captured:
            raise ValueError("Offer expiry must follow capture")
        if not str(r["source_url"]).startswith("https://"):
            raise ValueError("Offer source evidence must be an HTTPS URL")
        offer_id=digest([board_id,str(r.get("offer_id",i))])[:32]
        normalized.append((offer_id,board_id,gid,pid,r["market"],line,dumps(sides),r["offer_type"],captured,
                           expires,r["source_url"],r["contest"],r.get("rules_id"),dumps(r)))
    with store.transaction():
        store.db.execute("INSERT INTO boards VALUES(?,?,?,?,?)",(board_id,now(),max(r[8] for r in normalized),
                         normalized[0][10],h))
        store.db.executemany("INSERT INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",normalized)
        store.db.execute("UPDATE decisions SET status='superseded' WHERE status='active'")
    return {"board_id":board_id,"offers":len(normalized),"status":"imported"}


def current_offers(store):
    # Newest snapshot per provider contest is authoritative; withdrawn offers cannot survive a newer board.
    boards=store.rows("SELECT * FROM boards ORDER BY captured_at,imported_at")
    latest={}
    for b in boards:
        for contest in store.rows("SELECT DISTINCT contest FROM offers WHERE board_id=?",(b["id"],)):
            latest[(b["source"],contest["contest"])]=b["id"]
    result=[]
    for (_,contest),bid in latest.items():
        result.extend(store.rows("SELECT * FROM offers WHERE board_id=? AND contest=?",(bid,contest)))
    return result


def rank(store,at=None,commit=True):
    at=stamp(at or now()); fresh=freshness(store,at)
    store.db.execute("UPDATE decisions SET status='superseded' WHERE status='active'")
    latest_run=store.one("SELECT status FROM update_runs ORDER BY started_at DESC LIMIT 1")
    candidates=[]
    projections={(r["game_id"],r["player_id"]):json.loads(r["payload"]) for r in store.rows("SELECT * FROM projections WHERE status='active'")}
    for offer in current_offers(store):
        p=projections.get((offer["game_id"],offer["player_id"]))
        game=store.one("SELECT * FROM games WHERE id=?",(offer["game_id"],))
        reasons=[]
        age=(datetime.fromisoformat(at)-datetime.fromisoformat(offer["captured_at"])).total_seconds()/3600
        if age<0 or age>store.config["freshness_hours"]["offers"] or (offer["expires_at"] and offer["expires_at"]<=at):
            reasons.append("stale_offer")
        if not game or game["status"]!="scheduled" or not game["tipoff"] or game["tipoff"]<=at:
            reasons.append("game_not_upcoming_or_postponed")
        if latest_run and latest_run["status"]!="success":
            reasons.append("last_refresh_failed_or_in_progress")
        reasons.extend("stale_or_missing_"+k for k in ("stats","schedule","rosters","injuries","lineups","news") if fresh[k]["stale"])
        if not p or offer["market"] not in p["markets"]:
            reasons.append("no_supported_projection")
        else:
            reasons.extend(p["flags"])
            age_p=(datetime.fromisoformat(at)-datetime.fromisoformat(p["as_of"])).total_seconds()/3600
            if age_p<0 or age_p>store.config["freshness_hours"]["injuries"]:
                reasons.append("stale_projection")
            if p["sample_games"]<store.config["min_games"]:
                reasons.append("insufficient_sample")
        rules_row=store.one("SELECT * FROM rules WHERE id=?",(offer["rules_id"],))
        rules=json.loads(rules_row["payload"]) if rules_row else None
        if not rules or not rules.get("verified"):
            reasons.append("unverified_contest_rules")
        elif rules["contest"]!=offer["contest"] or rules_row["effective_from"]>at or (rules_row["effective_to"] and rules_row["effective_to"]<=at):
            reasons.append("rules_not_applicable")
        if offer["offer_type"]!="standard" and (not rules or offer["offer_type"] not in rules.get("special_offers",{})):
            reasons.append("unsupported_special_offer")
        # Promotion must be backed by a stored actual-market chronological evaluation, never a made-up confidence score.
        policy=store.config.get("selection_policy")
        evidence=store.one("SELECT payload FROM evaluations WHERE id=?",(policy.get("evaluation_id"),)) if policy else None
        if not evidence or not json.loads(evidence["payload"]).get("eligible_for_selection"):
            reasons.append("no_validated_market_selection_policy")
        for side in json.loads(offer["sides"]):
            dist=p["markets"].get(offer["market"]) if p else None
            probs=probabilities(dist["samples"],offer["line"]) if dist else None
            local=list(dict.fromkeys(reasons))
            if probs and policy and probs[side]<policy.get("minimum_probability",1):
                local.append("below_validated_policy_threshold")
            candidate={"offer_id":offer["id"],"board_id":offer["board_id"],"player_id":offer["player_id"],
                "game_id":offer["game_id"],"player":p["player"] if p else offer["player_id"],"team":p["team"] if p else None,
                "market":offer["market"],"line":offer["line"],"side":side,"probabilities":probs,
                "selected_side_probability":probs[side] if probs else None,
                "distribution":{k:v for k,v in (dist or {}).items() if k!="samples"},"sample_games":p["sample_games"] if p else 0,
                "model_id":p["model_id"] if p else None,"prediction_as_of":p["as_of"] if p else None,
                "captured_at":offer["captured_at"],"source":offer["source"],"rules_id":offer["rules_id"],
                "contest":offer["contest"],"skip_reasons":local,"recommendation":not local,
                "explanation":p.get("explanation") if p else "No projection", "availability":p.get("availability") if p else None,
                "reasons_against":["Limited current-role history; rotation and availability may change.",
                    "Opponent and pace effects have not been validated; probabilities are experimental."],
                "as_of":at,"probability_basis":"conditional on playing; ties distinct; DNP not a zero outcome"}
            id=digest([offer["id"],side,at,candidate["model_id"]])[:32]
            store.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?)",(id,offer["id"],at,dumps(candidate),"active"))
            candidate["decision_id"]=id
            candidates.append(candidate)
    candidates.sort(key=lambda x:x["selected_side_probability"] or -1,reverse=True)
    if commit:
        store.db.commit()
    return {"as_of":at,"qualifying":[c for c in candidates if c["recommendation"]],"candidates":candidates,
        "status":"supported_selections" if any(c["recommendation"] for c in candidates) else "no_qualifying_picks",
        "missing_input":"Import a current actual board with contest rules" if not candidates else None}


def payout_multiplier(outcomes,rules,offer_types=None,teams=None):
    if not rules.get("verified"):
        raise ValueError("Unverified settlement rules")
    if offer_types and any(t!="standard" for t in offer_types):
        raise ValueError("Special-offer payout/settlement adapter not implemented")
    if teams is not None and len(teams)!=len(outcomes):
        raise ValueError("Team identities must align with outcomes")
    active=[]; active_teams=[]; removed=False
    for i,outcome in enumerate(outcomes):
        if outcome in ("win","loss"):
            active.append(outcome)
            if teams is not None: active_teams.append(teams[i])
            continue
        treatment=rules["treatment"].get(outcome)
        if treatment=="refund_entry":
            return 1.0
        if treatment=="remove":
            removed=True
            continue
        if treatment not in ("win","loss"):
            raise ValueError("Unsupported or missing settlement outcome: "+outcome)
        active.append(treatment)
        if teams is not None: active_teams.append(teams[i])
    minimum=int(rules["eligibility"].get("min_legs",2))
    if len(active)<minimum:
        if "below_minimum_multiplier" not in rules:
            raise ValueError("Missing reduced-entry settlement rule")
        return float(rules["below_minimum_multiplier"])
    if removed:
        minimum_teams=int(rules["eligibility"].get("min_teams",2))
        if minimum_teams>1 and teams is None:
            raise ValueError("Reduced entries require team identities to verify eligibility")
        if teams is not None and len(set(active_teams))<minimum_teams:
            if "ineligible_reduction_multiplier" not in rules:
                raise ValueError("Missing settlement rule for a reduction that violates team eligibility")
            return float(rules["ineligible_reduction_multiplier"])
    row=rules["payouts"].get(str(len(active)))
    if not row or str(active.count("win")) not in row:
        raise ValueError("Missing payout for reduced entry size and win count")
    v=float(row[str(active.count("win"))])
    if not math.isfinite(v) or v<0:
        raise ValueError("Invalid payout multiplier")
    return v


def expected_value(joint_outcomes,rules,stake=1.0,teams=None,offer_types=None):
    if not joint_outcomes or abs(sum(r["probability"] for r in joint_outcomes)-1)>1e-8:
        raise ValueError("Exhaustive validated joint outcome probabilities must sum to one")
    if any(not math.isfinite(r["probability"]) or r["probability"]<0 for r in joint_outcomes):
        raise ValueError("Invalid joint probabilities")
    if not math.isfinite(stake) or stake<=0 or len({len(r["outcomes"]) for r in joint_outcomes})!=1:
        raise ValueError("Positive stake and consistent outcome dimensions required")
    gross=sum(r["probability"]*payout_multiplier(r["outcomes"],rules,offer_types,teams) for r in joint_outcomes)*stake
    return {"stake":stake,"expected_gross_payout":gross,"expected_net":gross-stake}


def build_entries(store,ranking=None,size=2,experimental=False):
    ranking=ranking or rank(store)
    picks=ranking["candidates"] if experimental else ranking["qualifying"]
    picks=[p for p in picks if p["selected_side_probability"] is not None]
    out=[]
    for combo in itertools.combinations(picks[:30],size):
        if len({c["player_id"] for c in combo})!=size or len({c["game_id"] for c in combo})!=1:
            continue
        teams={p["team"] for p in combo}
        if "NYK" not in teams or len(teams)<2 or None in teams:
            continue
        if len({p["contest"] for p in combo})!=1 or len({p["rules_id"] for p in combo})!=1:
            continue
        rr=store.one("SELECT payload FROM rules WHERE id=?",(combo[0]["rules_id"],))
        rules=json.loads(rr["payload"]) if rr else None
        reasons=[]
        if not rules or not rules.get("verified"):
            reasons.append("rules_unverified")
        else:
            e=rules["eligibility"]
            if not e.get("min_legs",2)<=size<=e.get("max_legs",0):
                reasons.append("ineligible_entry_size")
            if not e.get("same_game_allowed",False):
                reasons.append("same_game_not_allowed")
            if len(teams)<e.get("min_teams",2):
                reasons.append("insufficient_teams")
            if any(sum(c["team"]==t for c in combo)>e.get("max_per_team",size) for t in teams):
                reasons.append("too_many_from_team")
        ps=[p["selected_side_probability"] for p in combo]
        # These bounds require no independence assumption; we do not fabricate a joint point estimate.
        reasons.append("joint_dependence_not_validated")
        if any(not p["recommendation"] for p in combo):
            reasons.append("contains_unqualified_candidates")
        entry={"selections":list(combo),"rules_id":combo[0]["rules_id"],"game_id":combo[0]["game_id"],
            "all_win_probability_bounds":[max(0,sum(ps)-(size-1)),min(ps)],"joint_probability":None,
            "expected_value":None,"reasons":reasons,"status":"experimental_withheld","created_at":now(),
            "stake":store.config["flat_stake"]}
        id=digest(entry)[:32]; entry["id"]=id
        store.db.execute("INSERT INTO entries VALUES(?,?,?,?)",(id,entry["created_at"],dumps(entry),entry["status"]))
        out.append(entry)
        if len(out)>=20:
            break
    store.db.commit()
    return {"entries":out,"note":"No entry EV or reliable joint probability until dependence and actual rules are validated. No entries are submitted."}


def settle_entry(store,entry_id,outcomes):
    row=store.one("SELECT * FROM entries WHERE id=?",(entry_id,))
    if not row:
        raise ValueError("Unknown paper entry")
    if store.one("SELECT 1 FROM settlements WHERE entry_id=?",(entry_id,)):
        raise ValueError("Entry already settled; immutable paper ledger")
    entry=json.loads(row["payload"])
    if len(outcomes)!=len(entry["selections"]):
        raise ValueError("One reviewed outcome per selection required")
    rr=store.one("SELECT payload FROM rules WHERE id=?",(entry["rules_id"],))
    if not rr:
        raise ValueError("Versioned rules required for settlement")
    offer_types=[store.one("SELECT offer_type FROM offers WHERE id=?",(s["offer_id"],))["offer_type"] for s in entry["selections"]]
    multiplier=payout_multiplier(outcomes,json.loads(rr["payload"]),offer_types,[s["team"] for s in entry["selections"]])
    result={"entry_id":entry_id,"outcomes":outcomes,"stake":entry["stake"],
        "gross_payout":multiplier*entry["stake"],"net":(multiplier-1)*entry["stake"],
        "game_id":entry["game_id"],"rules_id":entry["rules_id"],"settled_at":now(),"paper_only":True}
    store.db.execute("INSERT INTO settlements VALUES(?,?,?)",(entry_id,result["settled_at"],dumps(result)))
    store.db.commit()
    return result


def bankroll(store):
    rows=[json.loads(r["payload"]) for r in store.rows("SELECT * FROM settlements ORDER BY settled_at")]
    initial=store.config["paper_bankroll"]; balance=initial; peak=initial; drawdown=0
    staked=gross=0.0; clusters={}
    for r in rows:
        staked+=r["stake"]; gross+=r["gross_payout"]; balance+=r["net"]
        peak=max(peak,balance); drawdown=max(drawdown,peak-balance)
        game=store.one("SELECT date_et FROM games WHERE id=?",(r["game_id"],))
        day=game["date_et"] if game else r["game_id"]
        clusters.setdefault(day,[]).append(r)
    interval=None
    if len(clusters)>=20:
        totals=np.array([[sum(r["net"] for r in rs),sum(r["stake"] for r in rs)] for rs in clusters.values()])
        idx=np.random.default_rng(7).integers(0,len(totals),(2000,len(totals)))
        sampled=totals[idx].sum(axis=1)
        interval=np.quantile(sampled[:,0]/sampled[:,1],[.025,.975]).tolist()
    return {"paper_only":True,"initial_balance":initial,"flat_stake":store.config["flat_stake"],"entries":len(rows),
        "total_staked":staked,"gross_payouts":gross,"net":gross-staked,"ending_balance":balance,
        "roi":(gross-staked)/staked if staked else None,"roi_definition":"net / total staked (gross includes returned stake)",
        "max_drawdown":drawdown,"roi_95_interval":interval,"independent_date_blocks":len(clusters),
        "uncertainty_note":"Date-block bootstrap groups correlated entries; fewer than 20 blocks yields no interval."}
