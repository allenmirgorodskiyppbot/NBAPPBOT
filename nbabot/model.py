"""Interpretable, conditional-on-playing empirical models. No unearned confidence claims."""
from __future__ import annotations

import json
import inspect
from datetime import datetime, timedelta

import numpy as np

from .core import ET, MARKETS, digest, dumps, freshness, latest_snapshots, now, stamp

MODEL_SPEC = {"version":"empirical-opportunity-v1","history_limit":82,"half_life_games":20,
              "recent_minutes_games":10,"minutes_recent_weight":.7,"markets":list(MARKETS),
              "calibration":"not_validated","seed":20261022}


def history(store, player_id, cutoff, mode="strict"):
    """No target-game minutes, future observations, or season-summary inputs."""
    cutoff=stamp(cutoff)
    date=datetime.fromisoformat(cutoff).astimezone(ET).date().isoformat()
    if mode=="strict":
        # Query versions, not the revised current table. Revisions learned later cannot leak backward.
        rows=store.rows("SELECT payload,known_at FROM stat_versions WHERE player_id=? AND known_at<? ORDER BY known_at",
                        (player_id,cutoff))
        versions={}
        for r in rows:
            v=json.loads(r["payload"])
            raw=json.loads(v.get("payload") or "{}")
            # Same-day data is usable only when explicitly observed final before this cutoff.
            # Date-only historical observations remain excluded on the target date.
            same_day_final=v["date_et"]==date and raw.get("game_status")=="completed"
            if v["date_et"]<date or same_day_final:
                versions[v["game_id"]]=v
        records=sorted(versions.values(),key=lambda r:(r["date_et"],r["game_id"]))
    elif mode=="retrospective":
        records=store.rows("SELECT * FROM player_games WHERE player_id=? AND date_et<? ORDER BY date_et,game_id",
                           (player_id,date))
    else:
        raise ValueError("Mode must be strict or retrospective")
    # Unknown appearance and DNPs must not be converted into zero-production played games.
    return [r for r in records if r["appeared"]==1 and r["minutes"] is not None and r["minutes"]>0]


def summarize(samples):
    a=np.asarray(samples)
    return {"mean":float(np.mean(a)),"median":float(np.median(a)),
            "interval_80":[float(v) for v in np.quantile(a,[.1,.9])],
            "interval_95":[float(v) for v in np.quantile(a,[.025,.975])]}


def probabilities(samples,line):
    line=float(line)
    if not np.isfinite(line) or line<0:
        raise ValueError("Finite nonnegative line required")
    a=np.asarray(samples)
    if not len(a):
        raise ValueError("No predictive samples")
    return {"more":float(np.mean(a>line)),"less":float(np.mean(a<line)),"tie":float(np.mean(a==line))}


def simulate(records,n=4000,seed=20261022,variant="opportunity",minute_cap=None):
    records=records[-82:]
    if len(records)<5:
        raise ValueError("Fewer than five prior played games with minutes")
    rng=np.random.default_rng(seed)
    weights=np.exp2(-(len(records)-1-np.arange(len(records)))/20)
    weights/=weights.sum()
    donor=rng.choice(len(records),n,p=weights)
    recent=records[-10:]
    min_pool=np.array([r["minutes"] for r in records])
    # Both minutes and rates carry historical variation; no realized target-game exposure.
    if variant=="empirical":
        minutes=min_pool[donor]
    else:
        minutes=np.where(rng.random(n)<.7,rng.choice([r["minutes"] for r in recent],n),
                         rng.choice(min_pool,n,p=weights))
    if minute_cap is not None:
        if not 0<float(minute_cap)<=65:
            raise ValueError("Minute cap outside supported range")
        minutes=np.minimum(minutes,float(minute_cap))
    ratio=minutes/min_pool[donor]
    result={"minutes":minutes}
    for market,col in MARKETS.items():
        if any(r.get(col) is None for r in records):
            continue
        counts=np.array([r[col] for r in records])[donor]
        raw=counts if variant=="empirical" else counts*ratio
        result[market]=np.floor(raw)+(rng.random(n)<(raw-np.floor(raw)))
    # Attempt opportunity and conversion are separate. Shared donor/minutes retain component dependence.
    if variant=="opportunity" and all(r.get("fg3a") is not None and r.get("fg3m") is not None for r in records):
        attempts=np.array([r["fg3a"] for r in records])[donor]*ratio
        attempts=(np.floor(attempts)+(rng.random(n)<attempts%1)).astype(int)
        makes=sum(r["fg3m"] for r in records)
        misses=sum(r["fg3a"]-r["fg3m"] for r in records)
        conversion=rng.beta(makes+1,misses+1,size=n)
        threes=rng.binomial(attempts,conversion)
        if "points" in result:
            old=np.array([r["fg3m"] for r in records])[donor]*ratio
            # Remaining points (twos + FT) keep the donor's joint production information.
            raw=np.maximum(0,np.array([r["pts"] for r in records])[donor]*ratio-3*old)
            rest=np.floor(raw)+(rng.random(n)<raw%1)
            result["points"]=rest+3*threes
        result["threes"]=threes
        result["three_attempts"]=attempts
    return result


def availability(store,game_id,player_id,at):
    snapshots=latest_snapshots(store,"injuries",at)
    aliases=[game_id]+[x["alias"] for x in store.rows("SELECT alias FROM game_aliases WHERE game_id=?",(game_id,))]
    found=[snapshots[a+"/"+player_id] for a in aliases if a+"/"+player_id in snapshots]
    if not found:
        return {"status":"unknown","note":"No explicit current availability observation; not assumed healthy"}
    return max(found,key=lambda r:r["observed_at"])


def upcoming_games(store,at=None,limit=3):
    at=stamp(at or now())
    return store.rows("SELECT * FROM games WHERE (home='NYK' OR away='NYK') AND tipoff>? "
                      "AND status='scheduled' ORDER BY tipoff LIMIT ?",(at,limit))


def roster_for(store,team,at):
    current=latest_snapshots(store,"rosters",at)
    if current:
        return [r for r in current.values() if r["team"]==team and r.get("active",True)],True
    # This fallback is explicitly historical and never certifies current roster eligibility.
    latest=store.rows("SELECT p.id player_id,p.name,g.team,max(g.date_et) date FROM players p JOIN player_games g "
                      "ON g.player_id=p.id WHERE g.date_et<? GROUP BY p.id,g.team",(datetime.fromisoformat(at).astimezone(ET).date().isoformat(),))
    by_player={}
    for r in latest:
        if r["player_id"] not in by_player or r["date"]>by_player[r["player_id"]]["date"]:
            by_player[r["player_id"]]=r
    return [r for r in by_player.values() if r["team"]==team],False


def model_id(store):
    data=store.rows("SELECT game_id,player_id,hash,known_at FROM player_games ORDER BY game_id,player_id")
    h=digest(data)
    specification={**MODEL_SPEC,"numpy_version":np.__version__,"simulations":store.config["simulations"],
        "implementation_sha256":digest([inspect.getsource(f) for f in (history,simulate,probabilities)])}
    id=digest([specification,h])[:24]
    store.db.execute("INSERT OR IGNORE INTO model_runs VALUES(?,?,?,?)",(id,now(),h,dumps(specification)))
    return id


def project_upcoming(store,at=None,limit=3,commit=True):
    at=stamp(at or now()); version=model_id(store); output=[]
    selected=None
    if store.config.get("organized_players"):
        from .organized import resolve_players
        selected={p["id"] for p in resolve_players(store)}
    store.db.execute("UPDATE decisions SET status='superseded' WHERE status='active'")
    fresh=freshness(store,at)
    lineups=latest_snapshots(store,"lineups",at)
    for game in upcoming_games(store,at,limit):
        for team in (game["home"],game["away"]):
            roster,verified=roster_for(store,team,at)
            for player in roster:
                pid=str(player["player_id"])
                if team=="NYK" and selected is not None and pid not in selected:
                    continue
                records=history(store,pid,at,"strict")
                a=availability(store,game["id"],pid,at)
                flags=[f"stale_or_missing_{k}" for k in ("stats","schedule","rosters","injuries","lineups","news") if fresh[k]["stale"]]
                if not verified:
                    flags.append("historical_roster_only")
                flags += ["uncalibrated_experimental","opponent_pace_and_role_effects_not_fitted",
                          "conditional_on_playing_not_DNP_probability"]
                if len(records)<store.config["min_games"]:
                    flags.append("small_sample")
                if a["status"] not in ("available","confirmed_active"):
                    flags.append("availability_"+a["status"])
                lineup=lineups.get(game["id"]+"/"+team,{})
                if lineup.get("confirmation")!="confirmed":
                    flags.append("starting_lineup_unconfirmed")
                result={"game_id":game["id"],"tipoff":game["tipoff"],"player_id":pid,"player":player["name"],
                    "team":team,"opponent":game["away"] if team==game["home"] else game["home"],
                    "as_of":at,"model_id":version,"sample_games":len(records[-82:]),"flags":flags,
                    "availability":a,"calibration":"not_validated","markets":{},"assumption":"Played game; no DNP/void mass estimated"}
                if len(records)>=5 and a["status"] not in ("out","inactive","confirmed_absent"):
                    seed=int(digest([pid,game["id"],version])[:8],16)
                    sims=simulate(records,n=store.config["simulations"],seed=seed,minute_cap=a.get("minutes_cap"))
                    result["minutes"]=summarize(sims["minutes"])
                    for market in MARKETS:
                        if market in sims:
                            result["markets"][market]={**summarize(sims[market]),"samples":[int(v) for v in sims[market]],
                                "sample_support":len(records[-82:])}
                    # Scenarios are sensitivity analyses, never arbitrary weighted mixtures.
                    if a["status"] not in ("available","confirmed_active"):
                        result["scenarios"]={"DNP":{"settlement":"void/rules-dependent, separate from zero"},
                                             "plays":{"minutes":result["minutes"],"weight":None}}
                    result["explanation"]=(f"{len(records[-82:])} prior games; last-ten minutes blended with season-window exposure. "
                        "Empirical production rates share simulated minutes; threes use attempts and beta-binomial conversion. "
                        "No causal injury, news, rest, or defender adjustment applied.")
                else:
                    result["withheld"]="Insufficient exact-minute history or confirmed absence"
                id=digest([game["id"],pid,at,version])[:32]
                store.db.execute("UPDATE projections SET status='superseded' WHERE game_id=? AND player_id=? AND status='active'",(game["id"],pid))
                store.db.execute("INSERT OR REPLACE INTO projections VALUES(?,?,?,?,?,?,?)",
                    (id,game["id"],pid,at,version,dumps(result),"active"))
                output.append(result)
    if commit:
        store.db.commit()
    return output
