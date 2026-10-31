"""Refresh existing opportunity/defense/pair metrics from their own tracking source."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dataset_refresh import load_scope
from master import normalized_name,clear_unverified_tracking_coverage

BASE='https://stats.nba.com/stats/'
MAPS={
 'Player totals':{'PTS':'pts','REB':'reb','AST':'ast','MIN':'minutes','FGM':'fgm','FGA':'fga','FG3M':'tpm','FG3A':'tpa',
                  'FTM':'ftm','FTA':'fta','STL':'stl','BLK':'blk','TOV':'tov','OREB':'oreb','DREB':'dreb','PF':'fouls'},
 'Team totals':{'PTS':'pts','REB':'reb','AST':'ast','FGM':'fgm','FGA':'fga','FG3M':'tpm','FG3A':'tpa','FTM':'ftm','FTA':'fta','TOV':'tov',
                'OREB':'oreb','DREB':'dreb','STL':'stl','BLK':'blk','PF':'fouls','MIN':'minutes'},
 'Team defense':{'OPP_FGM':'defended_fgm','OPP_FGA':'defended_fga','OPP_PTS':'points_allowed',
                 'OPP_REB':'opponent_rebounds','OPP_AST':'opponent_assists','OPP_FG3M':'opponent_tpm','OPP_FG3A':'opponent_tpa'},
 'Passing':{'POTENTIAL_AST':'potential_assists','PASSES_MADE':'passes','AST':'assists'},
 'Rebounding':{'REB_CHANCES':'rebound_chances','REB':'rebounds'},
 'Possessions':{'TOUCHES':'touches','TIME_OF_POSS':'possession_minutes'},
 'Overall':{'D_FGM':'defended_fgm','D_FGA':'defended_fga'},
 'Less Than 6Ft':{'D_FGM':'rim_defended_fgm','D_FGA':'rim_defended_fga'},
 'Rim':{'DEF_RIM_FGM':'rim_defended_fgm','DEF_RIM_FGA':'rim_defended_fga'},
 'Hustle':{'CONTESTED_SHOTS':'contested_shots','DEFLECTIONS':'deflections'},
 'Named pair':{'MATCHUP_MIN':'matchup_minutes','PARTIAL_POSS':'partial_possessions','PLAYER_PTS':'player_points',
               'TEAM_PTS':'team_points','MATCHUP_FGM':'fgm','MATCHUP_FGA':'fga','MATCHUP_FG3M':'tpm','MATCHUP_FG3A':'tpa',
               'MATCHUP_FTM':'ftm','MATCHUP_FTA':'fta','MATCHUP_AST':'ast','MATCHUP_TOV':'tov','SFL':'shooting_fouls'}}


def result_rows(document):
    groups=document.get('resultSets') or ([document['resultSet']] if document.get('resultSet') else [])
    rows=[]
    for group in groups:
        headers=group.get('headers');values=group.get('rowSet')
        if not isinstance(headers,list) or not all(isinstance(h,str) for h in headers) or not isinstance(values,list):
            raise ValueError('Tracking response schema is unavailable.')
        for row in values:
            if len(row)!=len(headers):raise ValueError('Tracking columns are inconsistent.')
            rows.append(dict(zip(headers,row)))
    if not groups:raise ValueError('Tracking response is missing its result sets.')
    return rows


def normalize_records(rows,category,scope,season,pairs=None,team_ids=None,per_mode='Totals'):
    # Every counting field below is cumulative. Never interpret a PerGame or
    # Per36 publication as a total and divide it by GP again downstream.
    if per_mode != 'Totals':
        raise ValueError('Tracking normalization requires cumulative Totals.')
    names={normalized_name(p['player']):p['player'] for p in scope['players']}
    ids={str(p['nba_player_id']):p['player'] for p in scope['players'] if p.get('nba_player_id')}
    allowed={(p['offense_player'],p['defense_player']) for p in pairs or []}
    result=[]
    for row in rows:
        if row.get('PER_MODE', row.get('per_mode', 'Totals')) != 'Totals':
            continue
        if category in {'Team totals','Team defense'}:
            entity=(team_ids or {}).get(str(row.get('TEAM_ID')))
            if entity not in scope['teams']:continue
        elif category=='Named pair':
            offense=ids.get(str(row.get('OFF_PLAYER_ID'))) or names.get(normalized_name(row.get('OFF_PLAYER_NAME','')))
            defense=ids.get(str(row.get('DEF_PLAYER_ID'))) or names.get(normalized_name(row.get('DEF_PLAYER_NAME','')))
            if (offense,defense) not in allowed:continue
            entity=offense+' | '+defense
        else:
            entity=ids.get(str(row.get('PLAYER_ID') or row.get('CLOSE_DEF_PERSON_ID'))) or names.get(normalized_name(row.get('PLAYER_NAME','')))
            if not entity:continue
        gp=row.get('G') if category=='Hustle' else row.get('GP');fields={}
        if type(gp) not in (int,float) or not math.isfinite(gp) or gp<=0 or not float(gp).is_integer():continue
        for source,target in MAPS[category].items():
            value=row.get(source)
            if source=='MATCHUP_MIN' and isinstance(value,str) and re.fullmatch(r'\d+:[0-5]\d(?:\.\d+)?',value):
                minutes,seconds=value.split(':')
                value=int(minutes)+float(seconds)/60
            if type(value) in (int,float) and math.isfinite(value) and value>=0:fields[target]=value
        if category=='Player totals' and not {'pts','reb','ast','minutes'}<=set(fields):
            continue
        made,attempts=('fgm','fga') if category=='Named pair' else ('defended_fgm','defended_fga') if category in {'Overall','Team defense'} else ('rim_defended_fgm','rim_defended_fga')
        if not fields:continue
        if category in {'Named pair','Overall','Less Than 6Ft','Rim','Team defense'}:
            if made in fields and attempts in fields:
                if fields[made]>fields[attempts]:raise ValueError('Tracking makes exceed attempts.')
                fields['fg_pct_allowed' if category!='Named pair' else 'fg_pct']=fields[made]/fields[attempts] if fields[attempts]>0 else None
        from feeds import canonical_player
        result.append({'entity':entity,'player_id':canonical_player(entity) if category=='Player totals' else None,
                       'category':category,'season':season,'season_type':'Regular Season',
                       'team':row.get('TEAM_ABBREVIATION') or row.get('PLAYER_LAST_TEAM_ABBREVIATION'),
                       'gp':gp,'fields':fields,'per_mode':'Totals',
                       'field_units':{key:('ratio' if key in {'fg_pct','fg_pct_allowed'} else 'cumulative_total') for key in fields},
                       'population':'Actual measured named pair' if category=='Named pair' else 'All actual opponents'})
    return result


def preserve_sparse_fields(record, previous):
    """Keep old values with their own denominators/dates, outside fresh fields.

    A missing new field is not zero. It must also not inherit a newer row's GP,
    publication date or coverage. Current research uses only ``fields``;
    ``retained_fields`` is an explicit archive for export/coverage inspection.
    """
    record = dict(record)
    fields = dict(record.get('fields', {}))
    record['fields'] = fields
    # The row already carries shared source metadata. Repeating it for every
    # active field inflated a normal extract beyond downstream download limits.
    # Keep an explicit field override only when its provenance actually differs.
    common = {name: record.get(name) for name in
        ('source', 'published_at', 'source_published_at', 'publication_time_kind',
         'retrieved_at', 'data_through_date', 'coverage_basis', 'gp', 'per_mode')}
    supplied = record.get('field_provenance', {})
    provenance = {key: {**common, **supplied[key]} for key in fields
                  if isinstance(supplied.get(key), dict)
                  and any(common.get(name) != value for name, value in supplied[key].items())}
    if provenance:
        record['field_provenance'] = provenance
    else:
        record.pop('field_provenance', None)
    retained = dict(record.get('retained_fields', {}))
    if isinstance(previous, dict):
        retained = {**previous.get('retained_fields', {}), **retained}
    if isinstance(previous, dict):
        for key, value in previous.get('fields', {}).items():
            if key not in fields or fields[key] is None:
                if value is not None:
                    meta = previous.get('field_provenance', {}).get(key) or {
                        name: previous.get(name) for name in
                        ('source', 'published_at', 'source_published_at', 'publication_time_kind',
                         'retrieved_at', 'data_through_date', 'coverage_basis', 'gp', 'per_mode')}
                    retained[key] = {'value': value, **meta,
                                     'reason': 'Missing from latest source record; prior value retained separately.'}
    for key, value in fields.items():
        if value is not None:
            retained.pop(key, None)
    if retained:
        record['retained_fields'] = retained
    else:
        record.pop('retained_fields', None)
    return record


async def collect_tracking(collector,output,categories=None):
    """Collect only requested original metric populations; preserve other sources."""
    now=collector.now;scope=load_scope();local=now.astimezone(ZoneInfo('America/New_York'))
    year=local.year-(local.month<7);season=f'{year}-{str(year+1)[-2:]}'
    target=Path(output)/'existing_tracking.json'
    old=json.loads(target.read_text()) if target.exists() else {}
    from news_digest import _aware
    checked=_aware(old.get('direct_tracking_attempted_at'))
    if checked and 0 <= (now-checked).total_seconds()<6*3600:
        return any(c.get('status')=='collected' for c in old.get('coverage',[]))
    pair_scope=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
    pairs=pair_scope['pairs'];team_ids=pair_scope.get('team_ids',{})
    defaults={key:'' for key in ('College','Conference','Country','DateFrom','DateTo','Division','DraftPick','DraftYear',
        'GameScope','GameSegment','Height','Location','Outcome','PORound','PlayerExperience','PlayerPosition',
        'SeasonSegment','StarterBench','VsConference','VsDivision','Weight','PlayerID')}
    defaults.update(LeagueID='00',Season=season,SeasonType='Regular Season',PerMode='Totals',
                    LastNGames=0,Month=0,OpponentTeamID=0,Period=0,TeamID=0,PaceAdjust='N',Rank='N',PlusMinus='N')
    specs=[('Player totals','leaguedashplayerstats',{'MeasureType':'Base'}),
           ('Team totals','leaguedashteamstats',{'MeasureType':'Base'}),
           ('Team defense','leaguedashteamstats',{'MeasureType':'Opponent'}),
           ('Passing','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Passing'}),
           ('Rebounding','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Rebounding'}),
           ('Possessions','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Possessions'}),
           ('Overall','leaguedashptdefend',{'DefenseCategory':'Overall'}),
           ('Less Than 6Ft','leaguedashptdefend',{'DefenseCategory':'Less Than 6Ft'}),
           ('Hustle','leaguehustlestatsplayer',{}),
           ('Named pair','leagueseasonmatchups',{'OffTeamID':'1610612752','DefTeamID':'','OffPlayerID':'','DefPlayerID':''}),
           ('Named pair','leagueseasonmatchups',{'OffTeamID':'','DefTeamID':'1610612752','OffPlayerID':'','DefPlayerID':''})]
    selected=set(categories) if categories is not None else {s[0] for s in specs}
    if not selected <= {s[0] for s in specs}:
        raise ValueError('Unknown direct tracking category.')
    if not selected:
        return any(c.get('status')=='collected' for c in old.get('coverage',[]))
    previous={(r['season'],r['entity'],r['category']):r for r in old.get('records',[]) if r.get('season')==season}
    records=dict(previous);attempts={}
    for category,endpoint,extra in specs:
        if category not in selected:continue
        params=({k:defaults[k] for k in ('LeagueID','Season','SeasonType','PerMode')} if category=='Named pair' else dict(defaults))
        if category=='Hustle':
            # Published endpoint schema uses G rather than GP. These are the
            # only original hustle concepts retained; no new screen/boxout fields.
            params={k:v for k,v in params.items() if k not in {'GameScope','GameSegment','StarterBench','PlayerID',
                'LastNGames','Period','PaceAdjust','Rank','PlusMinus'}}
        params.update(extra)
        result={'category':category,'endpoint':endpoint,'request_scope':dict(extra)}
        try:
            data=await collector.request(BASE+endpoint+'?'+urlencode(params))
            normalized=normalize_records(result_rows(data),category,scope,season,pairs,team_ids)
            if not normalized:raise ValueError('No existing scoped metrics were returned.')
            for row in normalized:
                row.update(source=BASE+endpoint,published_at=now.isoformat(),
                           source_published_at=None,publication_time_kind='retrieval_observed',
                           retrieved_at=now.isoformat(),data_through_date=None,
                           coverage_basis='not_reported_by_tracking_source')
                key=(row['season'],row['entity'],row['category'])
                records[key]=preserve_sparse_fields(row,previous.get(key))
            result.update(status='collected',records=len(normalized))
        except Exception as error:
            result.update(status='unavailable',error_type=type(error).__name__)
            code=getattr(getattr(error,'response',None),'status_code',None)
            if code:result['http_status']=code
            print('Existing metric source:',category,type(error).__name__,code,flush=True)
        attempts.setdefault(category,[]).append(result)
    coverage={c['category']:dict(c) for c in old.get('coverage',[])}
    for category,details in attempts.items():
        successes=[d for d in details if d['status']=='collected']
        state='collected' if len(successes)==len(details) else 'partial' if successes else 'unavailable'
        current={'category':category,'status':state,'records':sum(d.get('records',0) for d in successes),
                 'last_attempt':now.isoformat(),'source_published_at':None,'retrieved_at':now.isoformat(),
                 'source_data_through_date':None,'attempts':details}
        if not successes and coverage.get(category,{}).get('status')=='collected':
            # A failed optional direct source must not erase a working mirror.
            coverage[category]['direct_attempt']=current
        else:coverage[category]=current
    document={**old,'schema_version':1,'scope_revision':scope['revision'],'season':season,
              'last_attempt':now.isoformat(),'direct_tracking_attempted_at':now.isoformat(),
              'coverage':list(coverage.values()),'records':list(records.values()),
              'policy':'Existing workbook metrics and named pair definitions only; unsupported fields are never inferred.'}
    dates=[c.get('source_data_through_date') for c in coverage.values() if c.get('status')=='collected']
    document['source_data_through_date']=min(dates) if dates and all(dates) else None
    target.write_text(json.dumps(document,indent=2,sort_keys=True)+'\n')
    return any(c.get('status')=='collected' for c in coverage.values())


async def supplement_tracking(collector,output,categories=None):
    """Try missing original NBA populations after the independently published feed.

    PublicCollector stops actual network requests after a refusal/timeout for a
    host. A separate six-hour checkpoint prevents this optional path from
    hammering NBA Stats, without a fresh mirror timestamp suppressing it forever.
    """
    target=Path(output)/'existing_tracking.json'
    old=json.loads(target.read_text()) if target.exists() else {}
    # A prior direct success remains due again after its own cooldown. Only a
    # category actually supplied by the current mirror is excluded here.
    available={c['category'] for c in old.get('coverage',[])
               if c.get('status')=='collected' and c.get('source_content_sha256')}
    requested=set(categories) if categories is not None else {'Overall','Less Than 6Ft','Named pair','Hustle'}
    return await collect_tracking(collector,output,sorted(requested-available))


def import_tracking(store,document,now):
    from news_digest import _aware
    scope=store.get_checkpoint('dataset_refresh_scope',{})
    if document.get('scope_revision')!=scope.get('revision') or document.get('schema_version')!=1:
        raise ValueError('Tracking snapshot is outside the existing dataset scope.')
    names={p['player'] for p in scope['players']}
    pair_doc=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
    pairs={(p['offense_player'],p['defense_player']) for p in pair_doc['pairs']}
    rows=[]
    for original in document.get('records',[]):
        record=clear_unverified_tracking_coverage(dict(original))
        category=record.get('category');entity=record.get('entity');stamp=_aware(record.get('published_at'))
        if category not in MAPS or not stamp or stamp>now+dt.timedelta(minutes=5):raise ValueError('Invalid tracking record.')
        if record.get('per_mode','Totals') != 'Totals':
            raise ValueError('Tracking fields must use cumulative Totals, not per-game values.')
        covered=record.get('data_through_date')
        if covered is not None:
            try:
                if not isinstance(covered,str) or dt.date.fromisoformat(covered).isoformat()!=covered or covered>stamp.date().isoformat():
                    raise ValueError
            except (ValueError,TypeError):
                raise ValueError('Tracking coverage must be a valid covered-game date or unknown.') from None
        if ((category=='Named pair' and tuple(entity.split(' | ')) not in pairs) or
            (category in {'Team totals','Team defense'} and entity not in scope.get('teams',[])) or
            (category not in {'Named pair','Team totals','Team defense'} and entity not in names)):
            raise ValueError('Unrequested tracking entity.')
        fields=record.get('fields',{})
        gp=record.get('gp')
        if type(gp) not in (int,float) or not math.isfinite(gp) or gp<0 or not float(gp).is_integer():
            raise ValueError('Games played must be finite, nonnegative and integral.')
        allowed=set(MAPS[category].values())|{'fg_pct','fg_pct_allowed'}
        if not set(fields)<=allowed:raise ValueError('Unrequested tracking metric.')
        if any(value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0)
               for value in fields.values()):raise ValueError('Invalid tracking numeric value.')
        retained=record.get('retained_fields',{})
        if not isinstance(retained,dict) or not set(retained)<=allowed:
            raise ValueError('Unrequested retained tracking metric.')
        for item in retained.values():
            if not isinstance(item,dict) or type(item.get('value')) not in (int,float) or not math.isfinite(item['value']) or item['value']<0:
                raise ValueError('Invalid retained tracking value.')
            prior_stamp=_aware(item.get('published_at'))
            if not prior_stamp or prior_stamp>stamp:
                raise ValueError('Invalid retained tracking source date.')
        if record.get('season')!=document.get('season') or record.get('season_type')!='Regular Season':
            raise ValueError('Tracking season/phase does not match its declared population.')
        for made,attempts in [('fgm','fga'),('tpm','tpa'),('ftm','fta'),('defended_fgm','defended_fga'),('rim_defended_fgm','rim_defended_fga')]:
            if fields.get(made) is not None and fields.get(attempts) is not None and fields[made]>fields[attempts]:
                raise ValueError('Tracking made shots exceed their attempts.')
        rows.append(record)
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS refreshed_tracking (season TEXT,entity TEXT,category TEXT,payload_json TEXT,'
                   'PRIMARY KEY(season,entity,category))')
        for record in rows:
            prior=db.execute('SELECT payload_json FROM refreshed_tracking WHERE season=? AND entity=? AND category=?',
                (record['season'],record['entity'],record['category'])).fetchone()
            if prior:
                previous=json.loads(prior[0])
                if _aware(previous.get('published_at'))>_aware(record['published_at']):
                    continue
                if (previous.get('data_through_date') and record.get('data_through_date') and
                    record['data_through_date']<previous['data_through_date']):
                    continue
                record=preserve_sparse_fields(record,previous)
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?) ON CONFLICT(season,entity,category) '
                       'DO UPDATE SET payload_json=excluded.payload_json',
                       (record['season'],record['entity'],record['category'],json.dumps(record)))
    coverage=[dict(item) for item in document.get('coverage',[])]
    for item in coverage:
        if item.get('status')=='collected':
            dates=[r.get('data_through_date') for r in rows if r.get('category')==item.get('category')]
            item['source_data_through_date']=min(dates) if dates and all(dates) else None
    dates=[item.get('source_data_through_date') for item in coverage if item.get('status')=='collected']
    store.set_checkpoint('tracking_refresh_status',{'last_attempt':document.get('last_attempt'),
                         'coverage':coverage,'existing_records':len(rows),
                         'source_data_through_date':min(dates) if dates and all(dates) else None,
                         'note':'Prior values keep original timestamps when a tracking source is unavailable.'})
    if any(item.get('status')=='collected' for item in document.get('coverage',[])):
        store.feed_success('existing_tracking_refresh','Existing scoped metrics imported; source publication and covered-date fields retain upstream lag.')
    return len(rows)
