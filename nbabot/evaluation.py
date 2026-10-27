from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, time

import numpy as np
from scipy.optimize import minimize_scalar

from .core import ET, MARKETS, digest, dumps, now
from .model import MODEL_SPEC, history, model_id, probabilities, simulate


def crps(samples,y):
    a=np.sort(np.asarray(samples,dtype=float)); n=len(a)
    return float(np.abs(a-y).mean()-np.sum((2*np.arange(1,n+1)-n-1)*a)/(n*n))


def temperature(p,t):
    x=np.maximum(np.asarray(p),1e-6)**(1/t)
    return x/x.sum()


def metrics(rows,temp=1):
    if not rows:
        return {"n":0}
    preds=np.array([r["mean"] for r in rows]); truth=np.array([r["actual"] for r in rows])
    probs=np.array([temperature(r["probabilities"],temp) for r in rows]); outcomes=np.array([r["outcome"] for r in rows])
    onehot=np.eye(3)[outcomes]
    bins=[]
    for low in np.arange(0,1,.1):
        selected=(probs[:,0]>=low)&(probs[:,0]<low+.1)
        if selected.any():
            bins.append({"from":float(low),"to":float(low+.1),"n":int(selected.sum()),
                         "predicted_more":float(probs[selected,0].mean()),"observed_more":float((outcomes[selected]==0).mean())})
    # Date-block resampling keeps player outcomes in the same game date together.
    grouped=defaultdict(list)
    for r in rows:
        grouped[r["date"]].append(abs(r["mean"]-r["actual"]))
    blocks=list(grouped.values()); interval=None
    if len(blocks)>=10:
        sums=np.array([sum(b) for b in blocks]); ns=np.array([len(b) for b in blocks])
        ix=np.random.default_rng(32).integers(0,len(blocks),(1000,len(blocks)))
        interval=(np.quantile(sums[ix].sum(1)/ns[ix].sum(1),[.025,.975])).tolist()
    return {"n":len(rows),"date_blocks":len(blocks),"mae":float(np.abs(preds-truth).mean()),
        "rmse":float(np.sqrt(((preds-truth)**2).mean())),"mae_95_date_block_interval":interval,
        "crps":float(np.mean([r["crps"] for r in rows])),
        "brier_3_class":float(((probs-onehot)**2).sum(1).mean()),
        "log_loss":float(-np.log(probs[np.arange(len(rows)),outcomes]).mean()),
        "interval_80_coverage":float(np.mean([r["lo"]<=r["actual"]<=r["hi"] for r in rows])),
        "more_reliability_bins":bins}


def evaluate(store,mode="strict"):
    version=model_id(store)
    run_id=digest([version,mode,"chronological-60-20-20-v1"])[:32]
    prior=store.one("SELECT payload FROM evaluations WHERE id=?",(run_id,))
    if prior:
        return {**json.loads(prior["payload"]),"cached":True}
    targets=store.rows("SELECT * FROM player_games WHERE appeared=1 AND minutes>0 ORDER BY date_et,game_id,player_id")
    dates=sorted({r["date_et"] for r in targets})
    if len(dates)<20:
        raise ValueError("Need at least twenty distinct game dates for chronological partitions")
    train_end=dates[int(len(dates)*.6)]; test_start=dates[int(len(dates)*.8)]
    cal_start=dates[int(len(dates)*.7)]
    prior_test_overlap=False
    for row in store.rows("SELECT payload FROM evaluations"):
        earlier=json.loads(row["payload"])
        bounds=earlier.get("partitions",{})
        if earlier.get("mode")==mode and earlier.get("status")=="completed" and bounds.get("test_from", "9999")<=dates[-1]:
            prior_end=bounds.get("test_through",earlier.get("created_at","")[:10])
            prior_test_overlap |= prior_end>=test_start
    variants=("empirical","opportunity")
    predictions={v:defaultdict(list) for v in variants}
    skipped=0
    for target in targets:
        if target["date_et"]<train_end:
            continue
        cutoff=datetime.combine(datetime.fromisoformat(target["date_et"]).date(),time(),ET).isoformat()
        prior_games=history(store,target["player_id"],cutoff,mode)
        if len(prior_games)<20:
            skipped+=1; continue
        phase="test" if target["date_et"]>=test_start else "calibration" if target["date_et"]>=cal_start else "validation"
        for v in variants:
            sims=simulate(prior_games,n=500,seed=int(digest([target["game_id"],target["player_id"]])[:8],16),variant=v)
            for m,col in MARKETS.items():
                if m not in sims or target[col] is None:
                    continue
                # A pregame historical-median probe, not a real offer. Never report its returns as PrizePicks returns.
                prior_counts=[r[col] for r in prior_games[-82:] if r.get(col) is not None]
                line=float(np.median(prior_counts))
                p=probabilities(sims[m],line)
                counts=np.array([p["more"],p["less"],p["tie"]])*len(sims[m])
                probs=(counts+.5)/(len(sims[m])+1.5)
                actual=target[col]
                record={"date":target["date_et"],"game_id":target["game_id"],"player_id":target["player_id"],"cutoff":cutoff,
                    "market":m,"phase":phase,"mean":float(np.mean(sims[m])),"actual":actual,"line":line,
                    "probabilities":probs.tolist(),"outcome":0 if actual>line else 1 if actual<line else 2,
                    "lo":float(np.quantile(sims[m],.1)),"hi":float(np.quantile(sims[m],.9)),"crps":crps(sims[m],actual)}
                predictions[v][m].append(record)
    chosen={}; temperatures={}; reports={}; archive=[]
    for m in MARKETS:
        validation={v:[r for r in predictions[v][m] if r["phase"]=="validation"] for v in variants}
        if not any(validation.values()):
            reports[m]={"status":"unrun_no_asof_history","n":0}; continue
        chosen[m]=min(variants,key=lambda v:np.mean([r["crps"] for r in validation[v]]) if validation[v] else math.inf)
        selected=predictions[chosen[m]][m]
        calibration=[r for r in selected if r["phase"]=="calibration"]
        t=1.0
        if len(calibration)>=50:
            fit=minimize_scalar(lambda t: -np.mean([math.log(temperature(r["probabilities"],t)[r["outcome"]]) for r in calibration]),
                                bounds=(.5,3),method="bounded")
            t=float(fit.x)
        temperatures[m]=t
        reports[m]={"selected_variant":chosen[m],"calibration_temperature":t,"calibration_n":len(calibration),
            "validation":{v:metrics(validation[v]) for v in variants},
            "test_uncalibrated":metrics([r for r in selected if r["phase"]=="test"]),
            "test_temperature_calibrated":metrics([r for r in selected if r["phase"]=="test"],t),
            "test_baseline":metrics([r for r in predictions["empirical"][m] if r["phase"]=="test"])}
        archive.extend([{**r,"variant":chosen[m]} for r in selected])
    result={"id":run_id,"created_at":now(),"model_id":version,"mode":mode,"eligible_for_selection":False,
        "status":"completed" if archive else "unrun_insufficient_asof_history","skipped_player_games":skipped,
        "test_independence":"previously_viewed_dates" if prior_test_overlap else "first_opening" if archive else "unrun",
        "partitions":{"train_before":train_end,"model_selection_from":train_end,"calibration_from":cal_start,"test_from":test_start,"test_through":dates[-1],
                      "test_policy":"Frozen split; saved results reused for this dataset/model. No tuning on test."},
        "markets":reports,"selected_variants":chosen,"temperatures":temperatures,
        "limitations":["Historical median probes are hypothetical lines, not actual offers; no profitability or selection-threshold claim.",
            "Retrospective mode uses corrected outcomes available today and has revision/survivorship bias; not a strict as-of backtest."
                if mode=="retrospective" else "Strict mode requires historical record versions known before each game; import dates do not backfill knowledge.",
            "Calibration is experimental and applies to the probe-line distribution, not a verified current market board.",
            "No current roster, final season average, target-game minutes, or later injury snapshot is a historical feature."]}
    if prior_test_overlap:
        result["limitations"].append("Test dates overlap a completed evaluation: these results are not a newly independent untouched test.")
    folder=store.root/"evaluations"; folder.mkdir(exist_ok=True)
    (folder/(run_id+"_predictions.json")).write_text(dumps(archive))
    (folder/(run_id+".json")).write_text(json.dumps(result,indent=2))
    store.db.execute("INSERT INTO evaluations VALUES(?,?,?)",(run_id,result["created_at"],dumps(result)))
    store.db.commit()
    return result


def prospective_report(store):
    groups={}
    for row in store.rows("SELECT * FROM decisions ORDER BY created_at"):
        d=json.loads(row["payload"])
        if not d.get("probabilities") or not d.get("prediction_as_of"):
            continue
        game=store.one("SELECT * FROM games WHERE id=?",(d["game_id"],))
        if not game or not game["tipoff"] or d["as_of"]>=game["tipoff"] or d["prediction_as_of"]>=game["tipoff"]:
            continue
        # Use a single earliest recorded probability per actual offer/side, not repeated evaluations.
        k=(d["offer_id"],d["side"])
        if k in groups:
            continue
        obs=store.one("SELECT * FROM player_games WHERE game_id=? AND player_id=?",(d["game_id"],d["player_id"]))
        if not obs or obs["appeared"]!=1 or obs[MARKETS[d["market"]]] is None:
            continue
        y=obs[MARKETS[d["market"]]]; p=d["probabilities"]
        groups[k]={"offer_id":d["offer_id"],"side":d["side"],"market":d["market"],"line":d["line"],"actual":y,
            "outcome":"more" if y>d["line"] else "less" if y<d["line"] else "tie",
            "probabilities":p,"model_id":d["model_id"],"date":game["date_et"],"skip_reasons":d["skip_reasons"]}
    reports={}
    for m in MARKETS:
        rs=[r for r in groups.values() if r["market"]==m]
        reports[m]={"n_offer_sides":len(rs),"unique_offers":len({r['offer_id'] for r in rs}),
            "date_blocks":len({r['date'] for r in rs}),"brier_3_class":float(np.mean([
            sum((r["probabilities"][s]-(r["outcome"]==s))**2 for s in ("more","less","tie")) for r in rs])) if rs else None}
    return {"paper_only":True,"markets":reports,"settled_candidates":list(groups.values()),
        "profitability":"Withheld: requires verified contest rules, actual recorded entries and joint outcomes",
        "selection_policy":"No validated market threshold has been promoted. Skipped candidates remain archived."}
