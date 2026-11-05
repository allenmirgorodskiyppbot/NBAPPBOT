"""Published Knicks season fixtures, separate from live timing and results.

Unknown NBA Cup participants stay explicit unresolved slots. A publication with
80 assigned Knicks games is not relabeled as a complete 82-game calendar.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from feeds import TEAM_WORDS, canonical_game, canonical_team
from news_digest import _aware
from season_calendar import current_season, FIELDS

EASTERN = ZoneInfo('America/New_York')
TAG = 'nba_stats_schedules'
ROOT = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
MAX_DOCUMENT_BYTES = 2_000_000
CHECKPOINT = 'season_schedule_refresh'
TEAM_IDS = {team:str(1610612737+i) for i,team in enumerate([
    'ATL','BOS','CLE','NOP','CHI','DAL','DEN','GSW','HOU','LAC','LAL','MIA','MIL','MIN',
    'BKN','NYK','ORL','IND','PHI','PHX','POR','SAC','SAS','OKC','TOR','UTA','MEM','WAS','DET','CHA'])}


def _clock(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise ValueError('Schedule checks require a timezone-aware clock.')
    return now


def _source(source_meta, season, now):
    end = int(season[:4])+1
    expected = ROOT+TAG+f'/nba_schedule_{end}.parquet'
    if not isinstance(source_meta,dict):
        raise ValueError('Published schedule source metadata is missing.')
    published = _aware(source_meta.get('source_published_at'))
    if (source_meta.get('source') != expected or not published or published > now
            or not re.fullmatch(r'[0-9a-f]{64}',str(source_meta.get('source_content_sha256') or ''))):
        raise ValueError('Published schedule source identity, checksum or publication is invalid.')
    return {'source':expected,'source_published_at':published.isoformat(),
            'source_content_sha256':source_meta['source_content_sha256']}


def build_published_schedule(rows, source_meta, now=None):
    """Validate the complete source population before constructing a relay."""
    now = _clock(now); season = current_season(now); year = int(season[:4]); end = year+1
    meta = _source(source_meta,season,now)
    if not isinstance(rows,list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Published schedule source exceeds its bounded population.')
    games,slots,seen,seen_games = [],[],set(),set()
    ignored = Counter()
    for raw in rows:
        if not isinstance(raw,dict) or str(raw.get('season')) != str(end):
            raise ValueError('Published schedule contains an invalid season row.')
        phase = raw.get('season_type')
        if phase == 'preseason':
            ignored[phase] += 1
            continue
        if phase not in {'regular-season','nba-cup'}:
            raise ValueError('Published schedule contains an unsupported statistical phase.')
        provider = str(raw.get('game_id') or '')
        prefix = '002' if phase == 'regular-season' else '006'
        if not re.fullmatch(prefix+str(year)[-2:]+r'\d{5}',provider) or provider in seen:
            raise ValueError('Published schedule has a duplicate or invalid provider event.')
        seen.add(provider)
        day = dt.date.fromisoformat(str(raw.get('game_date')))
        if not dt.date(year,7,1) <= day < dt.date(end,7,1):
            raise ValueError('Published fixture date is outside its season.')
        home,away = canonical_team(raw.get('home_team_abbreviation')),canonical_team(raw.get('away_team_abbreviation'))
        missing = not raw.get('home_team_abbreviation') and not raw.get('away_team_abbreviation')
        if missing:
            if (str(raw.get('home_team_id')) != '0' or str(raw.get('away_team_id')) != '0'
                    or any(raw.get(key) is not None for key in ('home_pts','away_pts','home_wl','away_wl','matchup'))):
                raise ValueError('Unassigned fixture has conflicting participants or results.')
            slots.append({'provider_game_id':provider,'date':day.isoformat(),
                'season_type':'Regular Season' if phase=='regular-season' else 'NBA Cup Final',
                'home_team':None,'away_team':None,'start_time':None,'status':'participants_tbd',**meta})
            continue
        if phase != 'regular-season':
            raise ValueError('Assigned NBA Cup Final must not enter regular-season fixture inventory.')
        if (home not in TEAM_IDS or away not in TEAM_IDS or home==away
                or str(raw.get('home_team_id')) != TEAM_IDS[home] or str(raw.get('away_team_id')) != TEAM_IDS[away]
                or raw.get('matchup') != f'{home} vs. {away}'):
            raise ValueError('Published schedule team IDs or home/away header are inconsistent.')
        identity = canonical_game(day.isoformat(),home,away)
        if identity in seen_games:
            raise ValueError('Published schedule repeats a canonical game.')
        seen_games.add(identity)
        games.append({'id':identity,'date':day.isoformat(),'season':season,'season_type':'Regular Season',
            'home_team':home,'away_team':away,'provider_game_id':provider,'start_time':None,'status':'time_tbd',
            'source':meta['source'],'source_asof':meta['source_published_at'][:10]})
    games=[game for game in games if 'NYK' in (game['home_team'],game['away_team'])]
    if not 70 <= len(games) <= 82:
        raise ValueError('A published Knicks season calendar requires 70–82 assigned Knicks games.')
    # Global Cup slots have unknown participants; none can be labeled a Knicks
    # fixture. Their opponents/dates stay unknown until the publisher assigns NYK.
    slots=[];regular_slots=0;unassigned=82-len(games)
    coverage = [{'season':season,'team':'NYK','assigned_games':len(games),
        'regular_season_target':82,'unassigned_team_games':unassigned,
        'tipoffs_reported_by_source':0,'complete_for_published_games':True,
        'complete_82_game_schedule':unassigned==0}]
    games.sort(key=lambda row:(row['date'],row['id']));slots.sort(key=lambda row:row['provider_game_id'])
    identity = {'season':season,**meta,'games':games,'unassigned_slots':slots,'team_coverage':coverage}
    fixture_keys=('id','date','season','season_type','home_team','away_team','provider_game_id','start_time','status')
    semantic={'season':season,'games':[{key:r.get(key) for key in fixture_keys} for r in games],
              'unassigned_slots':[{key:r.get(key) for key in fixture_keys} for r in slots],
              'team_coverage':coverage}
    revision = hashlib.sha256(json.dumps(semantic,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'schema_version':1,**identity,'retrieved_at':now.isoformat(),'revision':revision,
        'team':'NYK','covered_teams':['NYK'],'assigned_games':len(games),'published_regular_slots':len(games),
        'regular_season_total':82,'regular_season_games_per_team':82,'unassigned_games':unassigned,
        'published_unassigned_regular_slots':regular_slots,'unpublished_regular_games':unassigned-regular_slots,
        'complete_for_published_games':True,'complete_season_schedule':unassigned==0,
        'unassigned_windows':[],'unassigned_coverage_unknown':bool(unassigned),
        'ignored_source_phases':dict(ignored),'upstream_published_schedule_validated':True,
        'time_coverage':'Source provides dates and participants only; exact tipoff times are not reported.',
        'unassigned_note':'Remaining Knicks regular-season games depend on NBA Cup scheduling; their exact dates, opponents and times remain unknown until assigned.',
        'scope':'Published NYK regular-season fixture inventory only. Other NBA teams appear as Knicks opponents. It never verifies live event timing, final results, player participation or current availability.'}


def validate_published_schedule(document, now=None):
    now = _clock(now)
    if (not isinstance(document,dict) or document.get('schema_version') != 1
            or document.get('season') != current_season(now)
            or document.get('team') != 'NYK'
            or document.get('upstream_published_schedule_validated') is not True
            or len(json.dumps(document,default=str).encode()) > MAX_DOCUMENT_BYTES):
        raise ValueError('Invalid published season schedule document.')
    checked = _aware(document.get('retrieved_at'))
    if not checked or checked > now+dt.timedelta(minutes=5):
        raise ValueError('Published schedule retrieval timestamp is invalid.')
    meta = _source(document,document['season'],checked)
    if (not isinstance(document.get('games'),list) or not isinstance(document.get('unassigned_slots'),list)
            or len(document['games'])>2500 or len(document['unassigned_slots'])>100):
        raise ValueError('Published calendar fixture lists exceed their bounds.')
    # Recreate the source-shaped identity rows and all coverage assertions. No
    # client-supplied total, team identity or missing-game count is trusted.
    rows=[]
    for row in document.get('games',[]):
        if not isinstance(row,dict) or row.get('start_time') is not None or row.get('status') != 'time_tbd':
            raise ValueError('Untimed source cannot provide a verified tipoff or game state.')
        home,away=row.get('home_team'),row.get('away_team')
        rows.append({'game_id':row.get('provider_game_id'),'game_date':row.get('date'),
            'season':int(document['season'][:4])+1,'season_type':'regular-season',
            'home_team_abbreviation':home,'away_team_abbreviation':away,'home_team_id':TEAM_IDS.get(home),
            'away_team_id':TEAM_IDS.get(away),'matchup':f'{home} vs. {away}'})
    for row in document.get('unassigned_slots',[]):
        if (not isinstance(row,dict) or row.get('status')!='participants_tbd'
                or any(row.get(key) is not None for key in ('home_team','away_team','start_time'))
                or row.get('season_type') not in {'Regular Season','NBA Cup Final'}):
            raise ValueError('Invalid unresolved season slot.')
        rows.append({'game_id':row.get('provider_game_id'),'game_date':row.get('date'),
            'season':int(document['season'][:4])+1,'season_type':'regular-season' if row['season_type']=='Regular Season' else 'nba-cup',
            'home_team_id':0,'away_team_id':0,'home_team_abbreviation':None,'away_team_abbreviation':None,
            'matchup':None,'home_pts':None,'away_pts':None,'home_wl':None,'away_wl':None})
    rebuilt=build_published_schedule(rows,meta,checked)
    for key in ('games','unassigned_slots','team_coverage','revision','covered_teams','assigned_games',
                'published_regular_slots','regular_season_total','regular_season_games_per_team','unassigned_games',
                'published_unassigned_regular_slots','unpublished_regular_games','complete_for_published_games',
                'complete_season_schedule','unassigned_windows','unassigned_coverage_unknown'):
        if document.get(key) != rebuilt[key]:
            raise ValueError('Published schedule identity or coverage assertions do not reconcile: '+key)
    return rebuilt


def import_published_schedule(store, document, now=None):
    """Merge fixtures atomically; retain compatible saved clocks and actual facts."""
    now=_clock(now);doc=validate_published_schedule(document,now);season=doc['season']
    with store.connect() as db:
        db.execute('PRAGMA busy_timeout=100')
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT value_json FROM checkpoints WHERE name=?',(CHECKPOINT,)).fetchone()
        old=json.loads(old[0]) if old else {}
        prior_pub=_aware(old.get('source_published_at'))
        if prior_pub and prior_pub>_aware(doc['source_published_at']):
            return {**old,'phase':'unchanged','changed':False,'retained_newer_publication':True}
        prior={row['id']:dict(row) for row in db.execute("SELECT * FROM season_schedule WHERE season=? AND (home_team='NYK' OR away_team='NYK')",(season,))}
        calendar_row=db.execute('SELECT value_json FROM checkpoints WHERE name=?',('season_calendar:'+season,)).fetchone()
        previous_calendar=json.loads(calendar_row[0]) if calendar_row else {}
        prepared=[];conflicts=[];preserved=0
        for row in doc['games']:
            row=dict(row);existing=prior.get(row['id'])
            raw={**row,'fixture_provenance':{key:doc[key] for key in ('source','source_published_at','source_content_sha256')}}
            row.update(source_kind='published_league_calendar',checked_at=doc['retrieved_at'])
            if existing:
                try:
                    old_payload=json.loads(existing.get('payload_json') or '{}')
                except (TypeError,ValueError):
                    old_payload={}
                original=(old_payload.get('original_calendar_payload') if isinstance(old_payload,dict)
                          and 'fixture_provenance' in old_payload else old_payload)
                if original:raw['original_calendar_payload']=original
                if any(existing.get(key)!=row[key] for key in ('date','home_team','away_team')):
                    conflicts.append({'game_id':row['id'],'reason':'Existing saved venue/identity conflicts; prior row retained.'})
                    continue
                tip=_aware(existing.get('start_time'))
                if tip and tip.astimezone(EASTERN).date().isoformat()==row['date']:
                    for key in ('start_time','source','source_asof','source_kind','checked_at'):
                        row[key]=existing.get(key)
                    row['status']=existing.get('status') or 'scheduled';preserved+=1
                    raw['preserved_tipoff_provenance']={key:existing.get(key) for key in ('source','source_asof','checked_at','start_time')}
                elif existing.get('status') in {'final','live','past_schedule_only','postponed','canceled'}:
                    row['status']=existing['status']
            row['payload_json']=json.dumps(raw,sort_keys=True,separators=(',',':'))
            prepared.append(row)
        # Retire this relay's superseded rows and older saved future fixtures.
        # More recent independent calendars and actual live/final games win.
        keep={row['id'] for row in doc['games']}
        actual_states={row['id']:row['status'] for row in db.execute('SELECT id,status FROM games WHERE date>=?',
            (now.astimezone(EASTERN).date().isoformat(),))}
        obsolete=[]
        for identity,row in prior.items():
            if identity in keep:continue
            if actual_states.get(identity) in {'final','live','suspended'}:continue
            older_saved=(row.get('date','')>=now.astimezone(EASTERN).date().isoformat()
                and isinstance(row.get('source_asof'),str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',row['source_asof'])
                and row['source_asof']<=doc['source_published_at'][:10])
            if row.get('source_kind')=='published_league_calendar' or older_saved:obsolete.append(identity)
        for identity in obsolete:
            db.execute('DELETE FROM season_schedule WHERE id=?',(identity,))
        for row in prepared:
            db.execute('INSERT INTO season_schedule ('+','.join(FIELDS)+') VALUES ('+','.join('?' for _ in FIELDS)+') '
                'ON CONFLICT(id) DO UPDATE SET '+','.join(key+'=excluded.'+key for key in FIELDS if key!='id'),
                [row.get(key) for key in FIELDS])
        semantic_fields=[key for key in FIELDS if key not in {'checked_at','payload_json','source','source_asof','source_kind'}]
        changed=(old.get('revision')!=doc['revision'] or bool(obsolete)
                 or any(row['id'] not in prior or any(prior[row['id']].get(key)!=row.get(key)
                    for key in semantic_fields) for row in prepared))
        report={key:doc[key] for key in ('season','source','source_published_at','source_content_sha256','revision',
            'covered_teams','assigned_games','published_regular_slots','regular_season_total','regular_season_games_per_team',
            'unassigned_games','published_unassigned_regular_slots','unpublished_regular_games','team_coverage',
            'complete_for_published_games','complete_season_schedule','unassigned_windows','unassigned_coverage_unknown',
            'unassigned_note','unassigned_slots','time_coverage','scope')}
        report.update(phase='partial' if conflicts else 'updated' if changed else 'unchanged',changed=changed,team='NYK',
            published_games=doc['assigned_games'],source_kind='published_league_calendar',source_checked_at=doc['retrieved_at'],
            preserved_tipoffs=preserved,conflicts=conflicts,retained_extra_saved_rows=len(set(prior)-keep)-len(obsolete),
            upstream_published_schedule_validated=True)
        saved_tips=Counter(team for row in db.execute('SELECT home_team,away_team FROM season_schedule '
            'WHERE season=? AND start_time IS NOT NULL',(season,)) for team in row)
        report['team_coverage']=[{**row,'available_saved_tipoffs':saved_tips[row['team']]}
                                 for row in doc['team_coverage']]
        old_coverage={row.get('team'):row for row in previous_calendar.get('team_coverage',[])
                      if isinstance(row,dict)}
        slot_dates=[row['date'] for row in doc['unassigned_slots'] if row['season_type']=='Regular Season']
        for row in report['team_coverage']:
            team=row['team'];old_team=old_coverage.get(team,{})
            singleton=set(previous_calendar.get('covered_teams',[]))=={team}
            windows=old_team.get('unassigned_windows') or (previous_calendar.get('unassigned_windows') if singleton else []) or []
            old_missing=old_team.get('unassigned_team_games',previous_calendar.get('unassigned_games') if singleton else None)
            old_ids={key for key,value in prior.items() if team in (value['home_team'],value['away_team'])}
            new_ids={value['id'] for value in doc['games'] if team in (value['home_team'],value['away_team'])}
            valid=(bool(windows) and old_missing==row['unassigned_team_games'] and old_ids==new_ids
                and all(isinstance(w,dict) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(w.get('start','')))
                    and re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(w.get('end',''))) and w['start']<=w['end'] for w in windows)
                and all(any(w['start']<=day<=w['end'] for w in windows) for day in slot_dates))
            row['unassigned_windows']=windows if valid and row['unassigned_team_games'] else []
            row['unassigned_coverage_unknown']=bool(row['unassigned_team_games'] and not row['unassigned_windows'])
            if row['unassigned_windows']:
                row['unassigned_window_provenance']=old_team.get('unassigned_window_provenance') or {
                    key:previous_calendar.get(key) for key in ('source','source_kind','source_checked_at','revision')}
        for key,value in ((CHECKPOINT,report),('season_calendar:'+season,report)):
            db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                (key,json.dumps(value,sort_keys=True)))
    return report


async def collect_season_schedule(mirror, output, now=None):
    """Collector-only Parquet decoding; Akash consumes the bounded JSON relay."""
    now=_clock(now or mirror.now);name=f'nba_schedule_{mirror.end_year}.parquet';url=ROOT+TAG+'/'+name
    cache=getattr(mirror,'_published_inventory_assets',{})
    if name in cache:
        rows,meta=cache[name]
    else:
        asset=(await mirror.release(TAG)).get(name)
        if (not isinstance(asset,dict) or asset.get('browser_download_url')!=url
                or type(asset.get('size')) is not int or not 0<asset['size']<=8_000_000
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(asset.get('digest') or ''))):
            raise ValueError('Published schedule release asset is invalid.')
        content,http_meta=await mirror.asset(TAG,name)
        digest=hashlib.sha256(content).hexdigest()
        if len(content)!=asset['size'] or digest!=asset['digest'][7:] or http_meta.get('browser_download_url')!=url:
            raise ValueError('Published schedule bytes do not match their release identity.')
        import pyarrow.parquet as pq
        table=pq.read_table(io.BytesIO(content))
        if table.num_rows>5000:raise ValueError('Published schedule exceeds row bound.')
        rows=table.to_pylist();meta={'source':url,'source_published_at':asset['updated_at'],'source_content_sha256':digest}
        cache[name]=(rows,meta);mirror._published_inventory_assets=cache
    document=build_published_schedule(rows,meta,now)
    validate_published_schedule(document,now)
    data=(json.dumps(document,indent=2,sort_keys=True)+'\n').encode()
    if len(data)>MAX_DOCUMENT_BYTES:raise ValueError('Published schedule relay exceeds byte bound.')
    path=Path(output)/'season_schedule.json';path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.json.tmp');temporary.write_bytes(data);temporary.replace(path)
    return {'phase':'updated','file':path.name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
            **{key:document[key] for key in ('season','assigned_games','unassigned_games','published_regular_slots','unpublished_regular_games','revision')}}


def schedule_coverage_rows(store,season=None):
    report=store.get_checkpoint(CHECKPOINT,{}) or {}
    if season and report.get('season')!=season:return []
    return [{**row,**{key:report.get(key) for key in ('source','source_published_at','source_checked_at','source_content_sha256')},
        'live_timing_verified':False,'source_time_coverage':report.get('time_coverage')} for row in report.get('team_coverage',[])]


def unassigned_schedule_rows(store,season=None):
    report=store.get_checkpoint(CHECKPOINT,{}) or {}
    if season and report.get('season')!=season:return []
    rows=[dict(row) for row in report.get('unassigned_slots',[])]
    if report.get('unpublished_regular_games'):
        rows.append({'provider_game_id':None,'team':'NYK','date':None,'home_team':None,'away_team':None,'start_time':None,
            'season_type':'Regular Season','status':'not_yet_published','aggregate_game_count':report['unpublished_regular_games'],
            'source':report.get('source'),'source_published_at':report.get('source_published_at'),
            'note':'Count of remaining unannounced Knicks games, not invented individual fixture rows.'})
    return rows


def team_calendar_metadata(store, team, season=None):
    """A team view of league coverage; never turn league totals into team totals."""
    team=canonical_team(team);season=season or current_season(_clock())
    if team not in TEAM_WORDS:return {}
    meta=store.get_checkpoint('season_calendar:'+season,{}) or {}
    if team not in meta.get('covered_teams',[]):
        full=store.get_checkpoint(CHECKPOINT,{}) or {}
        if full.get('season')!=season or team not in full.get('covered_teams',[]):return meta
        meta=full
    coverage=next((row for row in meta.get('team_coverage',[]) if isinstance(row,dict) and row.get('team')==team),None)
    if coverage is None:return meta
    return {**meta,'covered_teams':[team],'published_games':coverage['assigned_games'],
        'regular_season_total':coverage['regular_season_target'],'unassigned_games':coverage['unassigned_team_games'],
        'unassigned_windows':coverage.get('unassigned_windows',[]),
        'unassigned_coverage_unknown':coverage.get('unassigned_coverage_unknown',bool(coverage['unassigned_team_games'])),
        'unassigned_window_provenance':coverage.get('unassigned_window_provenance')}
