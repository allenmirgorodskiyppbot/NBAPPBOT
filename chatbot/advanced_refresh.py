"""Refresh existing opportunity/defense/pair metrics from their own tracking source."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dataset_refresh import load_scope
from master import normalized_name

BASE='https://stats.nba.com/stats/'
MAPS={
 'Player totals':{'PTS':'pts','REB':'reb','AST':'ast','MIN':'minutes','FGM':'fgm','FGA':'fga','FG3M':'tpm','FG3A':'tpa',
                  'FTM':'ftm','FTA':'fta','STL':'stl','BLK':'blk','TOV':'tov','OREB':'oreb','DREB':'dreb','PF':'fouls'},
 'Team totals':{'PTS':'pts','REB':'reb','AST':'ast','FGM':'fgm','FGA':'fga','FG3M':'tpm','FG3A':'tpa','FTM':'ftm','FTA':'fta','TOV':'tov'},
 'Passing':{'POTENTIAL_AST':'potential_assists','PASSES_MADE':'passes','AST':'assists'},
 'Rebounding':{'REB_CHANCES':'rebound_chances','REB':'rebounds'},
 'Possessions':{'TOUCHES':'touches','TIME_OF_POSS':'possession_minutes'},
 'Overall':{'D_FGM':'defended_fgm','D_FGA':'defended_fga'},
 'Less Than 6Ft':{'D_FGM':'rim_defended_fgm','D_FGA':'rim_defended_fga'},
 'Rim':{'DEF_RIM_FGM':'rim_defended_fgm','DEF_RIM_FGA':'rim_defended_fga'},
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


def normalize_records(rows,category,scope,season,pairs=None,team_ids=None):
    names={normalized_name(p['player']):p['player'] for p in scope['players']}
    ids={str(p['nba_player_id']):p['player'] for p in scope['players'] if p.get('nba_player_id')}
    allowed={(p['offense_player'],p['defense_player']) for p in pairs or []}
    result=[]
    for row in rows:
        if category=='Team totals':
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
        gp=row.get('GP');fields={}
        if type(gp) not in (int,float) or not math.isfinite(gp) or gp<=0 or not float(gp).is_integer():continue
        for source,target in MAPS[category].items():
            value=row.get(source)
            if type(value) in (int,float) and math.isfinite(value) and value>=0:fields[target]=value
        if category=='Player totals' and not {'pts','reb','ast','minutes'}<=set(fields):
            continue
        made,attempts=('fgm','fga') if category=='Named pair' else ('defended_fgm','defended_fga') if category=='Overall' else ('rim_defended_fgm','rim_defended_fga')
        if not fields:continue
        if category in {'Named pair','Overall','Less Than 6Ft','Rim'}:
            if made in fields and attempts in fields:
                if fields[made]>fields[attempts]:raise ValueError('Tracking makes exceed attempts.')
                fields['fg_pct_allowed' if category!='Named pair' else 'fg_pct']=fields[made]/fields[attempts] if fields[attempts]>0 else None
        from feeds import canonical_player
        result.append({'entity':entity,'player_id':canonical_player(entity) if category=='Player totals' else None,
                       'category':category,'season':season,'season_type':'Regular Season',
                       'gp':gp,'fields':fields,'population':'Actual measured named pair' if category=='Named pair' else 'All actual opponents'})
    return result


async def collect_tracking(collector,output):
    now=collector.now;scope=load_scope();year=now.astimezone(ZoneInfo('America/New_York')).year
    month=now.astimezone(ZoneInfo('America/New_York')).month
    year-=month<7;season=f'{year}-{str(year+1)[-2:]}'
    target=Path(output)/'existing_tracking.json'
    old=json.loads(target.read_text()) if target.exists() else {}
    from news_digest import _aware
    checked=_aware(old.get('last_attempt'))
    if checked and (now-checked).total_seconds()<6*3600:
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
           ('Passing','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Passing'}),
           ('Rebounding','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Rebounding'}),
           ('Possessions','leaguedashptstats',{'PlayerOrTeam':'Player','PtMeasureType':'Possessions'}),
           ('Overall','leaguedashptdefend',{'DefenseCategory':'Overall'}),
           ('Less Than 6Ft','leaguedashptdefend',{'DefenseCategory':'Less Than 6Ft'}),
           ('Named pair','leagueseasonmatchups',{'OffTeamID':'1610612752','DefTeamID':'','OffPlayerID':'','DefPlayerID':''}),
           ('Named pair','leagueseasonmatchups',{'OffTeamID':'','DefTeamID':'1610612752','OffPlayerID':'','DefPlayerID':''})]
    records=[];coverage=[]
    for category,endpoint,extra in specs:
        params=({k:defaults[k] for k in ('LeagueID','Season','SeasonType','PerMode')} if category=='Named pair' else dict(defaults))
        params.update(extra)
        try:
            data=await collector.request(BASE+endpoint+'?'+urlencode(params))
            normalized=normalize_records(result_rows(data),category,scope,season,pairs,team_ids)
            if not normalized:raise ValueError('No existing scoped metrics were returned.')
            for row in normalized:row.update(source=BASE+endpoint,published_at=now.isoformat())
            records.extend(normalized);coverage.append({'category':category,'status':'collected','records':len(normalized)})
        except Exception as error:
            coverage.append({'category':category,'status':'unavailable','error_type':type(error).__name__})
            print('Existing metric source:',category,type(error).__name__,
                  getattr(getattr(error,'response',None),'status_code',None),flush=True)
    # Preserve prior successful records for failed categories, with their original
    # dates. They are never relabeled fresh just because a retry was attempted.
    success={c['category'] for c in coverage if c['status']=='collected'}
    records.extend(r for r in old.get('records',[]) if r.get('category') not in success)
    distinct={(r['season'],r['entity'],r['category']):r for r in records}
    document={'schema_version':1,'scope_revision':scope['revision'],'season':season,'last_attempt':now.isoformat(),
              'coverage':coverage,'records':list(distinct.values()),'policy':'Existing workbook metrics and named pair definitions only; unsupported fields are never inferred.'}
    target.write_text(json.dumps(document,indent=2,sort_keys=True)+'\n')
    return any(c['status']=='collected' for c in coverage)


def import_tracking(store,document,now):
    from news_digest import _aware
    scope=store.get_checkpoint('dataset_refresh_scope',{})
    if document.get('scope_revision')!=scope.get('revision') or document.get('schema_version')!=1:
        raise ValueError('Tracking snapshot is outside the existing dataset scope.')
    names={p['player'] for p in scope['players']}
    pair_doc=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
    pairs={(p['offense_player'],p['defense_player']) for p in pair_doc['pairs']}
    rows=[]
    for record in document.get('records',[]):
        category=record.get('category');entity=record.get('entity');stamp=_aware(record.get('published_at'))
        if category not in MAPS or not stamp or stamp>now+dt.timedelta(minutes=5):raise ValueError('Invalid tracking record.')
        if ((category=='Named pair' and tuple(entity.split(' | ')) not in pairs) or
            (category=='Team totals' and entity not in scope.get('teams',[])) or
            (category not in {'Named pair','Team totals'} and entity not in names)):
            raise ValueError('Unrequested tracking entity.')
        fields=record.get('fields',{})
        gp=record.get('gp')
        if type(gp) not in (int,float) or not math.isfinite(gp) or gp<0 or not float(gp).is_integer():
            raise ValueError('Games played must be finite, nonnegative and integral.')
        allowed=set(MAPS[category].values())|{'fg_pct','fg_pct_allowed'}
        if not set(fields)<=allowed:raise ValueError('Unrequested tracking metric.')
        if any(value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0)
               for value in fields.values()):raise ValueError('Invalid tracking numeric value.')
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
            if prior and _aware(json.loads(prior[0]).get('published_at'))>_aware(record['published_at']):
                continue
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?) ON CONFLICT(season,entity,category) '
                       'DO UPDATE SET payload_json=excluded.payload_json',
                       (record['season'],record['entity'],record['category'],json.dumps(record)))
    store.set_checkpoint('tracking_refresh_status',{'last_attempt':document.get('last_attempt'),
                         'coverage':document.get('coverage',[]),'existing_records':len(rows),
                         'note':'Prior values keep original timestamps when a tracking source is unavailable.'})
    if any(item.get('status')=='collected' for item in document.get('coverage',[])):
        store.feed_success('existing_tracking_refresh','Existing scoped metrics imported; source publication and covered-date fields retain upstream lag.')
    return len(rows)
