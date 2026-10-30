"""Experimental evidence-based projection scenarios over existing workbook metrics.

No calibrated probability, causal defender effect, expected return or validated
betting edge is claimed. The model can decline to offer an aggressive candidate.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from zoneinfo import ZoneInfo

from feeds import canonical_player
from master import STAT_COMPONENTS
from news_digest import _aware, material_categories
from season_calendar import calendar_context


def _evidence(store,kind,entity):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_evidence'").fetchone():
            return []
        return [json.loads(row[0]) for row in db.execute('SELECT payload_json FROM workbook_evidence WHERE kind=? AND entity=?',(kind,entity))]


def _quantile(values,p):
    ordered=sorted(values);position=(len(ordered)-1)*p;lo=int(position);hi=math.ceil(position)
    return ordered[lo]+(ordered[hi]-ordered[lo])*(position-lo)


def _weighted(values):
    weights=[.94**i for i in range(len(values))]
    return sum(v*w for v,w in zip(values,weights))/sum(weights)


def projection_for_leg(store,leg,now):
    components=STAT_COMPONENTS.get(leg.get('stat'))
    date=str(leg.get('date') or '')[:10]
    if not components or not date:
        return {'available':False,'reason':'A supported statistic and event date are required.'}
    today=now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    cutoff=min(date,today)
    player=leg['player'];team=leg.get('team');player_id=leg.get('player_id') or canonical_player(player)
    filters=['p.player_id=?','p.date<?',"g.status='final'","LOWER(p.appearance_state)='played'",
             "p.season_type='Regular Season'"]
    args=[player_id,cutoff]
    if team:
        filters.append('p.team=?');args.append(team)
    with store.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT p.*,g.home_team,g.away_team FROM player_games p '
            'JOIN games g ON g.id=p.game_id WHERE '+' AND '.join(filters)+' ORDER BY p.date DESC LIMIT 100',args)]
    valid=[r for r in rows if r.get('minutes') is not None and r['minutes']>0
           and all(r.get(key) is not None for key in components)]
    valid=valid[:40]
    if len(valid)<8:
        return {'available':False,'reason':'Fewer than eight comparable regular-season observations have both minutes and the requested statistic.',
                'observations':len(valid),'missing_policy':'No missing minutes/statistic is imputed.'}
    recent=valid[:20];last5=valid[:5]
    rates=[sum(float(r[k]) for k in components)/r['minutes'] for r in recent]
    minutes=_weighted([r['minutes'] for r in last5])
    baseline_rate=_weighted(rates)
    context=calendar_context(store,date,team) if team else {}
    opponent=leg.get('opponent') or context.get('opponent')
    reasons=[f'Recent same-team regular-season workload: {minutes:.1f} minutes, from {len(last5)} observed appearances.',
             f'Recency-weighted production per minute uses {len(recent)} observations; other seasons/phases are not silently combined as one current-season average.']
    limitations=['Experimental scenario model; no calibrated win probability or validated betting advantage.',
                 'Overall team pace, current coaching assignments and unreported availability are not inferred from a partial player subset.']
    factor=1.0;signals=[];cohorts=[]
    if opponent:
        matched=[r for r in valid if r.get('opponent')==opponent]
        cohort=[sum(r[k] for k in components)/r['minutes'] for r in matched]
        adjustment=0.0
        if len(cohort)>=3 and baseline_rate>0:
            adjustment=max(-.08,min(.08,(statistics.fmean(cohort)/baseline_rate-1)*min(.25,len(cohort)/(len(cohort)+12))))
            factor*=1+adjustment
            reasons.append(f'Observed production against {opponent}: {len(cohort)} comparable games; shrunken reference adjustment {adjustment:+.1%}. This is a team-opponent association, not a named-defender effect.')
            if adjustment>.01:signals.append('positive opponent cohort')
        else:
            limitations.append(f'Only {len(cohort)} comparable observations against {opponent}; no quantitative opponent adjustment applied.')
        cohorts.append({'type':'opponent_team','team':opponent,'n':len(cohort),'adjustment':adjustment})
    rest=context.get('scheduled_rest_days')
    rest_rows=_evidence(store,'rest_schedule',team) if team else []
    rest_lookup={str(r.get('date',''))[:10]:r.get('rest_days') for r in rest_rows}
    if rest is not None:
        same_rest=[r for r in valid if rest_lookup.get(r['date'])==rest]
        adjustment=0.0
        if len(same_rest)>=5 and baseline_rate>0:
            rate=statistics.fmean(sum(r[k] for k in components)/r['minutes'] for r in same_rest)
            adjustment=max(-.05,min(.05,(rate/baseline_rate-1)*min(.20,len(same_rest)/(len(same_rest)+20))))
            factor*=1+adjustment
            reasons.append(f'Team calendar shows {rest} full rest day(s); {len(same_rest)} comparable historical workload observations support a shrunken {adjustment:+.1%} scenario adjustment.')
            if adjustment>.01:signals.append('positive rest cohort')
        else:
            reasons.append(f'Team calendar shows {rest} full rest day(s); the matching sample is too small for an automatic fatigue adjustment.')
        cohorts.append({'type':'team_rest','days':rest,'n':len(same_rest),'adjustment':adjustment})
    else:
        limitations.append('Complete scheduled team rest context is unavailable; player appearance gaps are not substituted for team rest.')
    expected=baseline_rate*minutes*factor
    scenario_values=[rate*minutes*factor for rate in rates]
    sd=statistics.stdev(scenario_values) if len(scenario_values)>1 else 0
    low_minutes=_quantile([r['minutes'] for r in recent],.25)
    high_minutes=_quantile([r['minutes'] for r in recent],.75)
    low,high=baseline_rate*low_minutes*factor,baseline_rate*high_minutes*factor
    source_news=[]
    for item in store.recent_news(100):
        if player.casefold() not in (str(item.get('title',''))+' '+str(item.get('body',''))).casefold():continue
        stamp=_aware(item.get('published_at'))
        if stamp and not 0<=(now-stamp).total_seconds()<=48*3600:continue
        if not material_categories(item):continue
        source_news.append({k:item.get(k) for k in ('title','source','url','published_at')})
        if len(source_news)==3:break
    if source_news:
        limitations.append('Recent collected reports may affect this player; no minutes/role change is invented from an unverified headline.')
    assignments=[r for r in _evidence(store,'assignments',player) if r.get('opponent_team')==opponent
                 and str(r.get('assignment_asof',''))[:10]<=today]
    assignments=sorted(assignments,key=lambda r:str(r.get('assignment_asof','')),reverse=True)[:1]
    pairing=None
    if assignments:
        assignment=assignments[0]
        pairing={'primary':assignment.get('primary'),'alternate':assignment.get('alternate'),
                 'asof':assignment.get('assignment_asof'),'excel_row':assignment.get('_row'),
                 'status':'Tentative; direction and actual game-day defensive assignment are not confirmed.'}
        reasons.append(f"Dated tentative pairing: {pairing['primary']} / {pairing['alternate']} ({str(pairing['asof'])[:10]}). It informs a conditional research scenario, not a made-up defender adjustment.")
    # Unpriced research uses the same workload/context model without fabricating
    # a sportsbook line. Margins only exist after an actual offered side/line.
    priced=leg.get('line') is not None and leg.get('side') in {'more','less'}
    edge=(expected-float(leg['line']))*(1 if leg['side']=='more' else -1) if priced else None
    standardized=edge/sd if sd and edge is not None else None
    volume=[r.get('fga') for r in last5 if r.get('fga') is not None]
    if len(volume)>=3 and leg['stat'] in {'pts','tpm'}:
        reasons.append(f'Recent shot volume: {statistics.fmean(volume):.1f} FGA per observed game ({len(volume)} observations).')
    sample_lag=(dt.date.fromisoformat(cutoff)-dt.date.fromisoformat(recent[0]['date'])).days
    confidence='moderate experimental' if len(recent)>=15 and sample_lag<=7 and not source_news else 'low'
    aggressive=bool(edge is not None and edge>0 and standardized is not None and standardized>=.15 and signals
                    and high-low>max(1,.15*expected) and not source_news and sample_lag<=7)
    return {'available':True,'method':'contextual-scenarios-v2','reference_projection':round(expected,2),
            'projected_minutes':round(minutes,2),'per_minute_baseline':round(baseline_rate,4),
            'minutes_scenarios':{'lower_workload':round(low,2),'reference':round(expected,2),'higher_workload':round(high,2)},
            'empirical_scenario_range':{'p10':round(_quantile(scenario_values,.1),2),'p90':round(_quantile(scenario_values,.9),2),
                'meaning':'Spread of rescaled historical observations, not a calibrated forecast interval.'},
            'selected_side_margin':round(edge,2) if edge is not None else None,'standardized_margin':round(standardized,3) if standardized is not None else None,
            'priced_offer':priced,
            'confidence':confidence,'n':len(recent),'first_date':min(r['date'] for r in recent),'last_date':recent[0]['date'],
            'actual_teams':sorted({r['team'] for r in recent}),'seasons':sorted({r['season'] for r in recent}),
            'opponent':opponent,'calendar':context,'cohorts':cohorts,'tentative_pairing':pairing,
            'supporting_reasons':reasons,'counterarguments':limitations,'player_reports':source_news,
            'aggressive_research_candidate':aggressive,'aggressive_signals':signals,
            'risk_note':'Higher-variance scenario needs favorable workload/matchup conditions. Riskier does not mean more likely to win.',
            'source_games':[{'game_id':r['game_id'],'date':r['date'],'source':r['source']} for r in recent]}
