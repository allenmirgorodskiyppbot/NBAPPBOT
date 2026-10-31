"""Readable, regenerated research tables over the existing saved evidence.

Exporting does not fetch data, mutate statistics, or claim that formatting in the
original workbook has been synchronized. Every derivative keeps its population
and source coverage; unknown and historical inputs remain visible.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import statistics
import uuid
from zoneinfo import ZoneInfo

from master import STAT_COMPONENTS

RAW_TABLES = ('games', 'player_games', 'news', 'feed_status', 'boards', 'entries',
              'grade_events', 'season_schedule', 'season_averages', 'refreshed_tracking')
BASE_FIELDS = ('pts', 'reb', 'ast', 'fgm', 'fga', 'tpm', 'tpa', 'ftm', 'fta',
               'minutes', 'stl', 'blk', 'tov', 'oreb', 'dreb', 'fouls', 'plus_minus')
METRICS = {**{key: (key,) for key in BASE_FIELDS}, **STAT_COMPONENTS}
WARNING = 'Observed saved sample; incomplete history can differ from published totals. Descriptive, not causal or predictive.'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def _value(row, metric):
    keys = METRICS.get(metric)
    return sum(row[key] for key in keys) if keys and all(_number(row.get(key)) for key in keys) else None


def _quantile(values, fraction):
    if not values:
        return None
    data = sorted(values)
    position = (len(data) - 1) * fraction
    left, right = int(position), math.ceil(position)
    return data[left] + (data[right] - data[left]) * (position - left)


def _beta_fraction(a, b, x):
    # Continued fraction for the regularized incomplete beta function, used
    # only for descriptive Student-t intervals already present in the workbook.
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    value = d
    for m in range(1, 201):
        for coefficient in (m * (b - m) * x / ((a + 2*m - 1) * (a + 2*m)),
                            -(a + m) * (a + b + m) * x / ((a + 2*m) * (a + 2*m + 1))):
            d = 1 + coefficient * d
            c = 1 + coefficient / c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            change = d * c
            value *= change
        if abs(change - 1) < 3e-14:
            break
    return value


def _regularized_beta(x, a, b):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    scale = math.exp(math.lgamma(a+b) - math.lgamma(a) - math.lgamma(b)
                     + a*math.log(x) + b*math.log1p(-x))
    if x < (a+1)/(a+b+2):
        return scale * _beta_fraction(a,b,x) / a
    return 1 - scale * _beta_fraction(b,a,1-x) / b


@lru_cache(maxsize=2048)
def _t95(df):
    low, high = 0.0, 16.0
    for _ in range(55):
        mid = (low + high) / 2
        cdf = 1 - .5 * _regularized_beta(df/(df+mid*mid), df/2, .5)
        if cdf < .975:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def summarize(rows, metric):
    observed = [(r, _value(r, metric)) for r in rows]
    observed = [(r, value) for r, value in observed if value is not None]
    values = [value for _, value in observed]
    n = len(values)
    mean = statistics.fmean(values) if n else None
    sd = statistics.stdev(values) if n > 1 else None
    delta = _t95(n-1)*sd/math.sqrt(n) if n > 1 else None
    dates = [_day(row.get('date')) for row, _ in observed if _day(row.get('date'))]
    return {'n': n, 'missing': len(rows)-n, 'total': sum(values) if n else None,
            'mean': mean, 'median': statistics.median(values) if n else None,
            'sd': sd, 'iqr': _quantile(values,.75)-_quantile(values,.25) if n else None,
            'ci_low': mean-delta if delta is not None else None,
            'ci_high': mean+delta if delta is not None else None,
            'ci_method': 'Descriptive 95% Student-t interval; independence is not established.',
            'first_game': min(dates) if dates else None, 'last_game': max(dates) if dates else None,
            'distinct_games': len({r['game_id'] for r,_ in observed}),
            'source_count': len({str(r.get('source') or '') for r,_ in observed}),
            'status': 'recomputed_from_saved_observations' if n else 'unavailable_no_observed_values',
            'warning': WARNING}


def _window(rows, window, phase):
    selected = [r for r in rows if r.get('season_type') == phase]
    window = str(window or '')
    if re.fullmatch(r'20\d{2}-\d{2}',window):
        return [r for r in selected if r.get('season') == window], None
    if window == 'NYK tenure':
        return [r for r in selected if r.get('team') == 'NYK'], None
    if window in {'Available career','All observed'}:
        return selected, None
    match = re.fullmatch(r'Recent (5|10|20) available',window)
    if match:
        return sorted(selected,key=lambda r:(r['date'],r['game_id']),reverse=True)[:int(match[1])], None
    return [], 'Unknown original window; no population was guessed.'


def _rest_match(row, wanted, rest_lookup):
    info = rest_lookup.get((row.get('team'), _day(row.get('date'))))
    if info is None:
        return None
    if wanted == 'Offseason gap':
        return bool(info.get('offseason_gap')) or info.get('rest_bucket') == wanted
    days = info.get('rest_days')
    if not _number(days):
        return None
    if info.get('offseason_gap') or info.get('rest_bucket') == 'Offseason gap':
        return False
    return {'0 (B2B)':days==0,'1':days==1,'2':days==2,'3-6':3<=days<=6,'7+':days>=7}.get(wanted)


def calculated_splits(definitions, players, rest_lookup):
    output = []
    for original in definitions:
        row = {key: original.get(key) for key in ('split_id','player','season_type','window','definition',
                                                  'filter','opponent_team','named_matchup_player','statistic')}
        group, problem = _window(players.get(row['player'],[]),row['window'],row['season_type'])
        kind, metric = row['definition'], row['statistic']
        if metric not in METRICS:
            problem = 'Unsupported original statistic; prior value remains in Original_Evidence.'
        baseline = group
        if kind in {'A: Opponent team','A: Opponent team by Rest'}:
            group = [r for r in group if r.get('opponent') == row['opponent_team']]
            if kind.endswith('by Rest'):
                baseline = group
        elif kind not in {'Baseline','Rest','Home/Away'}:
            problem = 'Original population is not reconstructable from whole-game observations.'
        unknown_rest = 0
        if kind in {'Rest','A: Opponent team by Rest'}:
            matched = [(r,_rest_match(r,str(row['filter']),rest_lookup)) for r in group]
            unknown_rest = sum(flag is None for _,flag in matched)
            group = [r for r,flag in matched if flag is True]
            if str(row['filter']) not in {'0 (B2B)','1','2','3-6','7+','Offseason gap'}:
                problem = 'Unknown original rest filter.'
        elif kind == 'Home/Away':
            if row['filter'] not in {'Home','Away'}:
                problem = 'Unknown original venue filter.'
            group = [r for r in group if r.get('venue') == row['filter']]
        elif row['filter'] not in {'All',None,''}:
            problem = 'Unknown original filter; no unfiltered replacement used.'
        if problem:
            row.update(status='unavailable',reason=problem)
        else:
            result = summarize(group,metric)
            comparison = summarize(baseline,metric)
            row.update(result,baseline_n=comparison['n'],baseline_mean=comparison['mean'],
                       difference=result['mean']-comparison['mean'] if result['n'] and comparison['n'] else None,
                       unknown_rest_games_excluded=unknown_rest)
        row['original_last_game'] = _day(original.get('last_game'))
        row['original_n'] = original.get('n')
        if not problem and _number(original.get('n')) and row['n'] < original['n']:
            row['warning'] += ' Saved master has fewer observations than original snapshot; original result is retained separately.'
        output.append(row)
    return output


def correlations(definitions, players):
    output = []
    for original in definitions:
        keys = ('player','other_player','x','y','window','season_type','relation')
        row = {key:original.get(key) for key in keys}
        first,problem = _window(players.get(row['player'],[]),row['window'],row['season_type'])
        # Recent-N is defined on the first player's window; the other player is
        # matched by exact game identity, not by independently chosen recent-N.
        second = {r['game_id']:r for r in players.get(row['other_player'],[])
                  if r.get('season_type')==row['season_type']}
        if row['relation'] not in {'Within player','Same-game opponents','Same-game teammates'}:
            problem = 'Unknown original relationship.'
        pairs = []
        if row['x'] not in METRICS or row['y'] not in METRICS:
            problem = 'Unsupported original statistic.'
        for one in first if not problem else []:
            two = second.get(one['game_id'])
            if not two or two.get('season') != one.get('season'):
                continue
            if row['relation']=='Within player' and row['player']!=row['other_player']:
                continue
            if row['relation']=='Same-game opponents' and one.get('team')==two.get('team'):
                continue
            if row['relation']=='Same-game teammates' and one.get('team')!=two.get('team'):
                continue
            x,y = _value(one,row['x']),_value(two,row['y'])
            if x is not None and y is not None:
                pairs.append((one,x,y))
        n=len(pairs)
        try:
            value=statistics.correlation([x for _,x,_ in pairs],[y for _,_,y in pairs]) if n>=2 else None
        except statistics.StatisticsError:
            value=None
        row.update(matched_games=n,pearson_r=value,
                   first_game=min(r['date'] for r,_,_ in pairs) if pairs else None,
                   last_game=max(r['date'] for r,_,_ in pairs) if pairs else None,
                   original_matched_games=original.get('matched_games'),original_last_game=_day(original.get('last_game')),
                   status='unavailable' if problem else 'recomputed_from_saved_observations' if value is not None else 'insufficient_or_constant_sample',
                   reason=problem,warning='Exact same-game observations only. '+WARNING)
        output.append(row)
    return output


def pair_summaries(definitions, players, assignments):
    assignment_index={r.get('assignment_id'):r for r in assignments}
    output=[]
    for original in definitions:
        row={key:original.get(key) for key in ('assignment_id','direction','player','named_player','role','window','season_type')}
        first,problem=_window(players.get(row['player'],[]),row['window'],row['season_type'])
        assignment=assignment_index.get(row['assignment_id'],{})
        opponent=assignment.get('opponent_team') if row['direction']=='Knicks offense' else 'NYK' if row['direction'] in {'Knicks defense','Opponent offense'} else None
        if row['direction'] in {'Knicks defense','Opponent offense'} and row['window']=='NYK tenure':
            problem='Opponent-offense NYK-tenure eligibility needs the named Knicks player tenure; no all-career substitute used.'
        if opponent:
            first=[r for r in first if r.get('opponent')==opponent]
        else:
            problem='Assignment direction/opponent unavailable; no matchup population guessed.'
        second={r['game_id']:r for r in players.get(row['named_player'],[]) if r.get('season_type')==row['season_type']}
        matched=[r for r in first if r['game_id'] in second and r.get('team')!=second[r['game_id']].get('team')
                 and r.get('season')==second[r['game_id']].get('season')]
        if problem:
            matched=[]
        pts,fga=summarize(matched,'pts'),summarize(matched,'fga')
        row.update(eligible_observed_games=len(first),confirmed_coappearances=len(matched),
                   unknown_or_not_observed_other=len(first)-len(matched),observed_pts=pts['n'],
                   missing_pts=pts['missing'],mean_pts=pts['mean'],median_pts=pts['median'],sd_pts=pts['sd'],
                   mean_fga=fga['mean'],observed_fga=fga['n'],first_game=pts['first_game'],last_game=pts['last_game'],
                   baseline_pts=summarize(first,'pts')['mean'],status='unavailable' if problem else pts['status'],reason=problem,
                   warning='Whole-game opponent coappearance, not measured guarding or a defender effect. '+WARNING)
        output.append(row)
    return output


def player_metrics(players):
    output=[]
    for player,rows in sorted(players.items()):
        groups=defaultdict(list)
        for row in rows:
            groups[(row.get('season'),row.get('season_type'))].append(row)
        for (season,phase),group in sorted(groups.items(),key=lambda item:str(item[0])):
            for metric in METRICS:
                output.append({'player':player,'season':season,'season_type':phase,'statistic':metric,
                               'actual_teams':','.join(sorted({r.get('team') or '' for r in group})),
                               'population':'All saved final played games in this season/phase',**summarize(group,metric)})
            ratios=(('fg_pct',('fgm','fga'),lambda t:t['fgm'],lambda t:t['fga']),
                    ('three_pct',('tpm','tpa'),lambda t:t['tpm'],lambda t:t['tpa']),
                    ('ft_pct',('ftm','fta'),lambda t:t['ftm'],lambda t:t['fta']),
                    ('efg_pct',('fgm','tpm','fga'),lambda t:t['fgm']+.5*t['tpm'],lambda t:t['fga']),
                    ('estimated_ts_pct',('pts','fga','fta'),lambda t:t['pts'],lambda t:2*(t['fga']+.44*t['fta'])),
                    ('three_attempt_rate',('tpa','fga'),lambda t:t['tpa'],lambda t:t['fga']),
                    ('free_throw_rate',('fta','fga'),lambda t:t['fta'],lambda t:t['fga']),
                    ('fga_per_36',('fga','minutes'),lambda t:36*t['fga'],lambda t:t['minutes']))
            for metric,fields,numerator,denominator in ratios:
                matched=[r for r in group if all(_number(r.get(k)) for k in fields)]
                totals={key:sum(r[key] for r in matched) for key in fields}
                top,bottom=numerator(totals),denominator(totals)
                output.append({'player':player,'season':season,'season_type':phase,'statistic':metric,
                               'population':'Ratio of totals on matching observed games; not average of percentages',
                               'actual_teams':','.join(sorted({r.get('team') or '' for r in matched})),
                               'n':len(matched),'missing':len(group)-len(matched),
                               'value':top/bottom if bottom>0 else None,'numerator':top,'denominator':bottom,
                               'first_game':min(r['date'] for r in matched) if matched else None,
                               'last_game':max(r['date'] for r in matched) if matched else None,
                               'status':'recomputed_from_saved_observations' if matched and bottom>0 else 'unavailable_no_denominator',
                               'warning':WARNING})
    return output


def tracking_metrics(records):
    def publication(row):
        # Field-specific provenance can override the category's dates, so this
        # normalization must run after merging both current and retained fields.
        if row.get('publication_time_kind')=='retrieval_observed':
            row['source_published_at']=None
            row['published_at']=None
        elif row.get('source_published_at') is None:
            row['source_published_at']=row.get('published_at')
        if row.get('status')!='retained_prior_value_not_refreshed':
            row['status']='source_value_coverage_known' if row.get('data_through_date') else 'source_value_covered_date_unknown'
        return row
    output=[]
    for record in records:
        common={key:record.get(key) for key in ('entity','category','season','season_type','gp','population','source',
                                               'published_at','source_published_at','retrieved_at','publication_time_kind',
                                               'data_through_date','coverage_basis')}
        if record.get('publication_time_kind')=='retrieval_observed':
            common['source_published_at']=record.get('source_published_at')
            common['published_at']=None
        elif common['source_published_at'] is None:
            common['source_published_at']=record.get('published_at')
        for metric,value in record.get('fields',{}).items():
            provenance=record.get('field_provenance',{}).get(metric,{})
            if not isinstance(provenance,dict):
                provenance={'provenance_note':provenance}
            output.append(publication({**common,**provenance,'metric':metric,'value':value,
                           'units':record.get('field_units',{}).get(metric),
                           'status':'source_value_coverage_known' if record.get('data_through_date') else 'source_value_covered_date_unknown'}))
        for metric,retained in record.get('retained_fields',{}).items():
            if not isinstance(retained,dict):
                continue
            output.append(publication({'entity':record.get('entity'),'category':record.get('category'),
                           'season':record.get('season'),'season_type':record.get('season_type'),
                           **retained,'metric':metric,'status':'retained_prior_value_not_refreshed'}))
    return output


def _profile_view(tables, names, opponent=None, opponent_names=None, current_season=None):
    """Uniform readable columns for existing player/opponent page definitions."""
    output=[]
    for row in tables['Player_Metrics']:
        if row['player'] not in names or opponent and row['season']!=current_season:
            continue
        output.append({'section':'Player production / efficiency','player':row['player'],
                       'season':row['season'],'season_type':row['season_type'],'metric':row['statistic'],
                       'actual_teams':row.get('actual_teams'),
                       'value':row.get('value'),'mean':row.get('mean'),'total':row.get('total'),
                       'median':row.get('median'),'sd':row.get('sd'),'n':row.get('n'),'missing':row.get('missing'),
                       'first_game':row.get('first_game'),'last_game':row.get('last_game'),
                       'status':row['status'],'notes':row['population']+'; '+row['warning']})
    for row in tables['Calculated_Splits']:
        if row.get('player') not in names or opponent and row.get('opponent_team') not in {None,'',opponent}:
            continue
        output.append({'section':'Existing split: '+str(row['definition']),'player':row['player'],
                       'opponent':row.get('opponent_team'),'window':row['window'],'season_type':row['season_type'],
                       'metric':row['statistic'],'filter':row['filter'],'mean':row.get('mean'),'n':row.get('n'),
                       'missing':row.get('missing'),'first_game':row.get('first_game'),'last_game':row.get('last_game'),
                       'status':row['status'],'notes':row.get('reason') or row.get('warning')})
    for row in tables['Tracking_Metrics']:
        entity=str(row.get('entity') or '')
        if entity not in names and not any(n in entity.split(' | ') for n in names):
            continue
        if opponent and (row.get('season')!=current_season or
                         ' | ' in entity and not set(entity.split(' | ')) & set(opponent_names or [])):
            continue
        output.append({'section':'Tracking: '+str(row.get('category')),'player':entity,
                       'season':row.get('season'),'season_type':row.get('season_type'),'metric':row['metric'],
                       'value':row.get('value'),'n':row.get('gp'),'units':row.get('units'),
                       'source':row.get('source'),'source_published_at':row.get('source_published_at'),
                       'retrieved_at':row.get('retrieved_at'),'last_game':row.get('data_through_date'),
                       'status':row['status'],'notes':row.get('population')})
    for row in tables['Matchup_Assignments']:
        if row.get('selected_player') not in names or opponent and row.get('opponent_team')!=opponent:
            continue
        output.append({'section':'Tentative matchup','player':row.get('selected_player'),'opponent':row.get('opponent_team'),
                       'primary':row.get('primary'),'alternate':row.get('alternate'),
                       'assignment_asof':row.get('assignment_asof'),'checked_at':row.get('last_checked'),
                       'status':row.get('refresh_state') or row.get('assignment_status'),
                       'notes':row.get('refresh_note') or 'Original tentative assignment; direction and game-day guarding unconfirmed.'})
    for row in tables['Measured_Pair_Metrics']:
        if not ({row['offense_player'],row['defense_player']} & names):
            continue
        if opponent and (row['season']!=current_season or
                         opponent not in (row.get('offense_teams','').split(',')+row.get('defense_teams','').split(','))):
            continue
        output.append({'section':'Dated measured matchup','player':row['offense_player'],'defender':row['defense_player'],
                       'season':row['season'],'season_type':row['season_type'],'metric':row['metric'],'value':row['value'],
                       'n':row.get('covered_games'),'first_game':row['first_game'],'last_game':row['last_game'],
                       'source':row.get('source_ids'),'status':'dated_measured_observations','notes':row['population']})
    for label in ('Shot locations and distance bands','Rotation stints and entry/exit times',
                  'On/off possessions and ratings','Play types; contests and deflections'):
        output.append({'section':'Profile coverage','metric':label,'status':'requires_own_source',
                       'notes':'Only current rows explicitly present in Tracking_Metrics establish a refresh. Original profile-only values remain in the dated reference workbook; no replacement is inferred from totals.'})
    return output


def measured_pair_metrics(rows):
    groups=defaultdict(list)
    for row in rows:
        if row.get('offense_player') and row.get('defense_player') and _day(row.get('date')):
            groups[(row['offense_player'],row['defense_player'],row.get('season'),row.get('season_type'),
                    row.get('offense_team'),row.get('defense_team'))].append(row)
    output=[]
    for (offense,defense,season,phase,offense_team,defense_team),group in sorted(groups.items(),key=lambda item:str(item[0])):
        common={'offense_player':offense,'defense_player':defense,'season':season,'season_type':phase,
                'offense_teams':','.join(sorted({str(r.get('offense_team')) for r in group if r.get('offense_team')})),
                'defense_teams':','.join(sorted({str(r.get('defense_team')) for r in group if r.get('defense_team')})),
                'covered_games':len({r['game_id'] for r in group}),
                'first_game':min(_day(r['date']) for r in group),'last_game':max(_day(r['date']) for r in group),
                'population':'Dated measured pair observations only; not whole-game coappearance',
                'source_ids':','.join(sorted({str(r.get('source_id') or '') for r in group}))}
        for metric in ('matchup_seconds','partial_possessions','player_points','team_points','fgm','fga','tpm','tpa','ftm','fta',
                       'matchup_assists','matchup_turnovers','shooting_fouls'):
            values=[r[metric] for r in group if _number(r.get(metric))]
            output.append({**common,'metric':metric,'value':sum(values) if values else None,
                           'observed_records':len(values),'missing_records':len(group)-len(values)})
        for label,made,attempts in (('fg_pct','fgm','fga'),('three_pct','tpm','tpa'),('ft_pct','ftm','fta')):
            matched=[r for r in group if _number(r.get(made)) and _number(r.get(attempts))]
            numerator=sum(r[made] for r in matched);denominator=sum(r[attempts] for r in matched)
            output.append({**common,'metric':label,'value':numerator/denominator if denominator>0 else None,
                           'numerator':numerator,'denominator':denominator,'observed_records':len(matched),
                           'missing_records':len(group)-len(matched)})
    return output


def _checkpoint(db,name,default):
    row=db.execute('SELECT value_json FROM checkpoints WHERE name=?',(name,)).fetchone()
    return json.loads(row[0]) if row else default


def build_export_tables(master, now=None):
    """Read one database snapshot and derive tables without database writes."""
    now=now or dt.datetime.now(dt.timezone.utc)
    today=now.astimezone(ZoneInfo('America/New_York')).date()
    stamp=now.isoformat()
    with master.connect() as db:
        db.execute('BEGIN')
        raw={name:[dict(r) for r in db.execute('SELECT * FROM '+name)] for name in RAW_TABLES}
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_evidence'").fetchone()
        evidence=[dict(r) for r in db.execute('SELECT * FROM workbook_evidence')] if exists else []
        inventory=_checkpoint(db,'workbook_sheet_inventory',[])
        tracking_status=_checkpoint(db,'tracking_refresh_status',{})
        refresh_status=_checkpoint(db,'nightly_nba_refresh',{})
    evidence_groups=defaultdict(list)
    for row in evidence:
        evidence_groups[row['kind']].append(json.loads(row['payload_json']))
    games={r['id']:r for r in raw['games']}
    players=defaultdict(list)
    for row in raw['player_games']:
        game=games.get(row['game_id'],{})
        if game.get('status')!='final' or str(row.get('appearance_state') or '').lower()!='played' or not _day(row.get('date')):
            continue
        if _day(row['date'])>today.isoformat():
            continue
        row={**row,'venue':'Home' if row.get('team')==game.get('home_team') else 'Away' if row.get('team')==game.get('away_team') else None}
        players[row['player']].append(row)
    rest={(r.get('team'),_day(r.get('date'))):r for r in evidence_groups['rest_schedule']}
    try:
        from season_calendar import refreshed_rest_rows
    except ImportError:
        pass
    else:
        for row in refreshed_rest_rows(master,now=now):
            rest[(row.get('team'),_day(row.get('date')))]=row
    assignments=evidence_groups['assignments']
    try:
        from assignment_refresh import assignment_rows_for
    except ImportError:
        pass
    else:
        assignments=assignment_rows_for(master,fallback_rows=assignments,now=now)
    records=[json.loads(r['payload_json']) for r in raw['refreshed_tracking']]
    tables={**raw,
            'Player_Metrics':player_metrics(players),
            'Calculated_Splits':calculated_splits(evidence_groups['splits'],players,rest),
            'Correlations':correlations(evidence_groups['correlations'],players),
            'Pair_Summaries':pair_summaries(evidence_groups['pair_summaries'],players,evidence_groups['assignments']),
            'Measured_Pair_Metrics':measured_pair_metrics([r for r in evidence_groups['measured_pairs']
                if _day(r.get('date')) and _day(r['date'])<=today.isoformat()]),
            'Tracking_Metrics':tracking_metrics(records),
            'Matchup_Assignments':assignments,
            'Team_Schedule':list(rest.values()),
            'Original_Evidence':[{**r,'status':'Original dated workbook snapshot; not relabeled as current'} for r in evidence],
            'Original_Missing_Data':[{**r,'export_status':'Original dated assessment; use Refresh_Coverage for current availability'}
                                     for r in evidence_groups['missing_data']],
            'Original_Sources':evidence_groups['sources']}
    profile_names={r['sheet'] for r in inventory if r['sheet'] in players}
    season_year=today.year-(today.month<7)
    current_season=f'{season_year}-{str(season_year+1)[-2:]}'
    if any(r['sheet']=='Overview' for r in inventory):
        tables['Overview']=[{**r,'view_note':'Current-season saved observations; see player tabs for historical and source-specific detail.'}
                            for r in tables['Player_Metrics'] if r['player'] in profile_names and
                            r['season']==current_season and r['statistic'] in {'pts','reb','ast','pra','fga','minutes'}]
    for sheet in inventory:
        name=sheet['sheet']
        if name in profile_names:
            tables[name]=_profile_view(tables,{name})
        elif re.fullmatch(r'vs [A-Z]{3}',name):
            opponent=name[3:]
            names=set(profile_names)
            # Existing stored latest team evidence only; no inferred current
            # roster from an old career appearance or remembered public roster.
            opponent_names=set()
            for player,rows in players.items():
                latest=max(rows,key=lambda r:r['date'])
                if (latest.get('team')==opponent and latest.get('season')==current_season and
                        0<=(today-dt.date.fromisoformat(latest['date'])).days<=7):
                    opponent_names.add(player)
            names.update(opponent_names)
            tables[name]=_profile_view(tables,names,opponent,opponent_names,current_season)
    latest=max((r['date'] for rows in players.values() for r in rows),default=None)
    refreshed={'Player_Games':'player_games; Player_Metrics','Season_Totals':'Player_Metrics; season_averages; Tracking_Metrics',
               'Calculated_Splits':'Calculated_Splits','Correlations':'Correlations','Pair_Summaries':'Pair_Summaries',
               'Pair_Tracking':'Measured_Pair_Metrics; Tracking_Metrics','Matchup_Assignments':'Matchup_Assignments',
               'Team_Schedule':'Team_Schedule; season_schedule; Original_Evidence'}
    coverage=[]
    for sheet in inventory:
        name=sheet['sheet'];target=refreshed.get(name)
        profile=name in players or name.startswith('vs ') or name=='Overview'
        coverage.append({'original_sheet':name,'original_rows':sheet.get('rows'),
                         'current_export_tables':name if profile else target,
                         'status':'current_derivatives_and_dated_sources' if target or profile else 'original_reference_snapshot',
                         'original_sheet_rewritten':False,'readable_current_view_generated':name in tables and profile,
                         'note':'Original workbook remains preserved. Current readable views use saved derivatives and their per-row coverage, not copied original formatting. Missing sources retain original dates.',
                         'export_generated_at':stamp})
    coverage.extend([
        {'group':'Game-derived statistics','status':'recomputed_from_saved_observations','latest_observed_game':latest,
         'target_game_date':refresh_status.get('target_game_date'),'source_data_through_date':refresh_status.get('source_data_through_date'),
         'note':'Every statistic uses its own observed population/date. Latest game is not proof of complete history.'},
        {'group':'Tentative assignments','status':'revalidated_dated_evidence' if any(r.get('last_checked') for r in assignments) else 'original_snapshot',
         'note':'Last checked is separate from assignment_asof. Revalidation does not confirm a defender or invent a replacement.'},
        {'group':'Rest-filtered statistics','status':'verified_rest_rows_only','note':'Missing team schedule/rest evidence is excluded; player appearance gaps do not establish team rest.'},
        {'group':'Original 53-sheet presentation','status':'preserved_reference','note':'Current exports regenerate supported tables; original formatted player/opponent sheets remain the original dated file.'}])
    for item in tracking_status.get('coverage',[]):
        coverage.append({'group':'Tracking: '+str(item.get('category')),**item,
                         'note':'Source publication time, covered-game date and export time are separate.'})
    tables['Refresh_Coverage']=coverage
    tables['Export_ReadMe']=[{'topic':'Generated','value':stamp},
        {'topic':'Source policy','value':'Saved observations and original definitions only. Exporting performs no network request or statistical writes.'},
        {'topic':'Original workbook','value':'The original formatted workbook remains a dated reference. Current derivatives are in the named research tables.'},
        {'topic':'Missing values','value':'Missing or DNP observations are never substituted with zeros. Empty samples/rate denominators remain blank.'},
        {'topic':'Populations','value':'Historical seasons and phases stay separate except explicitly named Available career/Recent windows. Coappearance does not establish guarding.'},
        {'topic':'Freshness','value':'Export generation time is not data coverage. Read each source/last-game date and Refresh_Coverage.'}]
    return tables


def _cell(value):
    if isinstance(value,(dict,list,tuple)):
        value=json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)
    if isinstance(value,str):
        # XLSX cannot store controls or arbitrarily long strings. Formula-like
        # imported text stays plain text on every generated sheet.
        value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',value)
        if value[:1] in {'=','+','-','@'}:
            value="'"+value
        return value[:32767]
    return value


def export_master(master, directory, now=None):
    import openpyxl
    tables=build_export_tables(master,now)
    workbook=openpyxl.Workbook(write_only=True)
    for name,rows in tables.items():
        sheet=workbook.create_sheet(name)
        columns=list(dict.fromkeys(key for row in rows for key in row))
        if not columns:
            # Preserve meaningful raw table headers for empty fresh databases.
            if name in RAW_TABLES:
                with master.connect() as db:
                    columns=[r[0] for r in db.execute('SELECT * FROM '+name+' LIMIT 0').description]
            else:
                columns=['status']
        sheet.append([_cell(column) for column in columns])
        for row in rows:
            sheet.append([_cell(row.get(key)) for key in columns])
        sheet.close()
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/'NBA_Master.xlsx'
    temporary=directory/('NBA_Master-'+uuid.uuid4().hex+'.partial.xlsx')
    try:
        workbook.save(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        for sheet in workbook.worksheets:
            writer=getattr(sheet,'_writer',None)
            if writer is not None and isinstance(writer.out,str) and Path(writer.out).exists():
                writer.cleanup()
        workbook.close()
    return target
