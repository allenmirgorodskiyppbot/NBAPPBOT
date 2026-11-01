"""Previous-day existing-dataset refresh at 11:30 AM Eastern with durable retries."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
from zoneinfo import ZoneInfo

from feeds import canonical_game, canonical_team, TEAM_WORDS
from nba_daily_export import validate_final
from news_digest import _aware

EASTERN=ZoneInfo('America/New_York')
DEFAULT_BASE='https://raw.githubusercontent.com/allenmirgorodskiyppbot/NBAPPBOT/main/data/live'


def import_daily_snapshot(store, document, day, now):
    """Validate a full document first; atomically publish the available complete games."""
    if (not isinstance(document,dict) or document.get('schema_version')!=1 or document.get('date')!=day
            or document.get('source') not in {'nba','espn'}):
        raise ValueError('Daily NBA snapshot has an invalid schema, date or publisher.')
    published=_aware(document.get('published_at'))
    scope=store.get_checkpoint('dataset_refresh_scope',{})
    if not scope or document.get('scope_revision')!=scope.get('revision') or document.get('upstream_full_game_validated') is not True:
        raise ValueError('Daily refresh is outside the existing authorized dataset scope.')
    if not published or published>now+dt.timedelta(minutes=5):
        raise ValueError('Daily NBA snapshot has an invalid retrieval timestamp.')
    expected=document.get('expected_games')
    games,rows=document.get('games'),document.get('player_games')
    if type(expected) is not int or not 0<=expected<=30 or not isinstance(games,list) or not isinstance(rows,list):
        raise ValueError('Daily NBA snapshot is missing its game inventory.')
    if len(games)>expected or len(rows)>1500:
        raise ValueError('Daily NBA snapshot exceeds its game inventory.')
    seen=set();groups={}
    for game in games:
        home,away=canonical_team(game.get('home_team')),canonical_team(game.get('away_team'))
        if (home not in TEAM_WORDS or away not in TEAM_WORDS or home==away or game.get('date')!=day
                or game.get('id')!=canonical_game(day,home,away) or game['id'] in seen
                or game.get('status') not in {'scheduled','live','final','postponed','canceled'}):
            raise ValueError('Daily NBA snapshot has an invalid/duplicate game.')
        seen.add(game['id']);groups[game['id']]=[]
    for row in rows:
        if row.get('game_id') not in groups or row.get('date')!=day:
            raise ValueError('Player row is outside the requested game day.')
        groups[row['game_id']].append(row)
    from dataset_refresh import select_existing_rows
    selected=select_existing_rows(rows,scope)
    if len(selected)!=len(rows):
        raise ValueError('Daily refresh contains a player outside the existing dataset.')
    by_game={game['id']:game for game in games}
    year=int(day[:4])-(int(day[5:7])<7)
    for row,canonical in zip(rows,selected):
        game=by_game[row['game_id']]
        if row.get('player_id')!=canonical['player_id'] or row.get('player')!=canonical['player']:
            raise ValueError('Daily observation does not use its registered canonical identity.')
        if {canonical_team(row.get('team')),canonical_team(row.get('opponent'))}!={game['home_team'],game['away_team']}:
            raise ValueError('Player team/opponent do not match the actual source game.')
        if row.get('season')!=f'{year}-{str(year+1)[-2:]}' or row.get('season_type')!='Regular Season':
            raise ValueError('Daily observation has a different season or statistical phase.')
    # The publisher validates complete source games before extracting the existing
    # tracked subset. A subset must never be mistaken for complete team totals.
    seen_players=set()
    for row in rows:
        key=(row['game_id'],row.get('player_id'))
        if not row.get('player_id') or key in seen_players or row.get('appearance_state') not in {'Played','DNP','Unknown'}:
            raise ValueError('Daily refresh has an invalid or duplicate tracked observation.')
        seen_players.add(key)
        if row['appearance_state']=='Played' and (row.get('pts') is None or row.get('minutes') is None or row['minutes']<=0):
            raise ValueError('Tracked played observation is missing points/minutes.')
    for game in games:
        if game['status']!='final' and groups[game['id']]:
            raise ValueError('Partial/live player rows are not imported as final daily data.')
    complete=(len(games)==expected and all(g['status'] in {'final','postponed','canceled'} for g in games)
              and not document.get('pending_game_ids') and not document.get('errors'))
    if bool(document.get('complete'))!=complete:
        raise ValueError('Daily NBA completeness claim does not match its records.')
    digest=hashlib.sha256(json.dumps(document,sort_keys=True,default=str).encode()).hexdigest()
    prior=store.get_checkpoint('daily_nba:'+day,{})
    if prior.get('sha256')==digest:
        return {**prior,'unchanged':True}
    prior_publication=_aware(prior.get('publisher_retrieved_at'))
    if prior_publication and published<prior_publication:
        return {**prior,'unchanged':True,'ignored_older_publication':True}
    count=0
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for game in games:
            if not groups[game['id']]:
                continue  # No unrelated game archive when no existing player is represented.
            old=db.execute('SELECT status FROM games WHERE id=?',(game['id'],)).fetchone()
            if old and old['status']=='final' and game['status']!='final':
                continue
            for row in groups[game['id']]:
                old_player=db.execute('SELECT appearance_state FROM player_games WHERE game_id=? AND player_id=?',
                                      (row['game_id'],row['player_id'])).fetchone()
                if old_player and old_player['appearance_state']=='Played' and row['appearance_state']=='Unknown':
                    continue
                count+=bool(store.upsert_player_game(row,db=db))
            store.upsert_game(game,db=db)
        record={'date':day,'complete':complete,'games':len(games),'expected_games':expected,
                'player_rows':len(rows),'changed_player_rows':count,'source':document['source'],
                'inventory_scope':document.get('inventory_scope','published snapshot inventory; full league schedule not independently verified'),
                'publisher_retrieved_at':published.isoformat(),'imported_at':now.isoformat(),'sha256':digest}
        db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                   ('daily_nba:'+day,json.dumps(record)))
    return record


class NightlyNBARefresh:
    def __init__(self,store,worker,now=None,exporter=None,data_dir=None):
        self.store,self.worker=store,worker
        self.now=now or (lambda:dt.datetime.now(dt.timezone.utc))
        self.base=os.getenv('NBA_DAILY_DATA_URL',DEFAULT_BASE).rstrip('/')
        if not self.base.startswith('https://'):
            raise ValueError('NBA_DAILY_DATA_URL must be HTTPS.')
        self.enabled=os.getenv('DAILY_NBA_REFRESH','1')=='1'
        self.exporter,self.data_dir=exporter,data_dir
        self.lock=asyncio.Lock()

    async def tick(self):
        async with self.lock:
            now=self.now();local=now.astimezone(EASTERN);today=local.date()
            deadline=dt.datetime.combine(today,dt.time(11,30),EASTERN)
            target_day=today-dt.timedelta(days=1 if local>=deadline else 2)
            target=target_day.isoformat()
            status=self.store.get_checkpoint('nightly_nba_refresh',{})
            if not self.enabled:
                self.store.set_checkpoint('nightly_nba_refresh',{'enabled':False});return
            if not self.store.get_checkpoint('dataset_refresh_scope'):
                self.store.set_checkpoint('nightly_nba_refresh',{'enabled':True,'phase':'waiting_for_existing_dataset_scope'});return
            due=str(status.get('handled_target_date',''))<target
            pending=list(status.get('pending_dates',[]))
            if not status.get('initialized'):
                pending.extend((target_day-dt.timedelta(days=n)).isoformat() for n in range(7))
                status['initialized']=True
            if due:
                # Today's required previous-day update comes first, before older
                # startup/backfill dates. Restarting after the deadline catches up.
                # A restart can miss several daily deadlines. Queue every skipped
                # date, not just the two newest dates; the three-date batch below
                # still bounds each collection pass and persists the remainder.
                missed=[]
                try:
                    handled=dt.date.fromisoformat(str(status.get('handled_target_date','')))
                except ValueError:
                    handled=None
                if handled and handled<target_day:
                    missed=[(target_day-dt.timedelta(days=n)).isoformat()
                            for n in range((target_day-handled).days)]
                pending=[target,(target_day-dt.timedelta(days=1)).isoformat(),*missed,*pending]
                status['handled_target_date']=target
            elif local>=deadline and not self.store.get_checkpoint('daily_nba:'+target,{}).get('complete'):
                pending=[target,*pending]
            last=_aware(status.get('last_attempt'))
            # Repair legacy status that may have borrowed the tracking/source
            # file's newest date even though that game day was never imported.
            with self.store.connect() as db:
                imported=[json.loads(row[0]) for row in db.execute(
                    "SELECT value_json FROM checkpoints WHERE name LIKE 'daily_nba:%'")]
            through=max((r['date'] for r in imported if r.get('complete') is True
                         and isinstance(r.get('date'),str) and r['date']<=target),default=None)
            game_attempt=bool(pending and (due or not last or (now-last).total_seconds()>=3600))
            tracking_last=_aware(status.get('last_tracking_attempt'))
            tracking_attempt=bool((local>=deadline or game_attempt) and
                                  (due or not tracking_last or (now-tracking_last).total_seconds()>=3600))
            if game_attempt:
                pending=list(dict.fromkeys(pending))
                if target in pending:
                    pending=[target,*[day for day in pending if day!=target]]
                batch,pending=pending[:3],pending[3:]
                for day in batch:
                    try:
                        document=await self.worker.request(self.base+'/'+day+'.json')
                        report=await asyncio.to_thread(import_daily_snapshot,self.store,document,day,now)
                        if report['complete']:
                            # Only this validated date was imported. The upstream
                            # file may also contain later games or tracking data.
                            through=max(str(through or ''),day)
                            self.store.feed_success('nba_daily_refresh',f"Existing scoped statistics for {day}: {report['games']} published games, {report['player_rows']} existing-player observations.")
                        else:
                            pending.append(day)
                    except asyncio.CancelledError:raise
                    except Exception as error:
                        pending.append(day)
                        code=getattr(getattr(error,'response',None),'status_code',None)
                        self.store.feed_failure('nba_daily_refresh',type(error).__name__+(f': HTTP {code}' if code else '')+
                            '; previous-day data not available for '+day+'. Prior verified values retained; hourly retry is automatic.')
                status['last_attempt']=now.isoformat()
            if tracking_attempt:
                try:
                    tracking=await self.worker.request(self.base+'/existing_tracking.json')
                    from advanced_refresh import import_tracking
                    await asyncio.to_thread(import_tracking,self.store,tracking,now)
                    coverage=self.store.get_checkpoint('tracking_refresh_status',{}).get('coverage',[])
                    status['tracking_refresh_phase']=('partial' if any(c.get('status')!='collected' for c in coverage)
                        else 'collected' if coverage else 'unavailable')
                    status['tracking_source_data_through_date']=self.store.get_checkpoint('tracking_refresh_status',{}).get('source_data_through_date')
                except asyncio.CancelledError:raise
                except Exception as error:
                    status['tracking_refresh_phase']='unavailable'
                    self.store.feed_failure('existing_tracking_refresh',type(error).__name__+
                        '; missing tracking fields keep their original source dates and are not inferred from game totals.')
                status['last_tracking_attempt']=now.isoformat()
            if game_attempt or tracking_attempt:
                try:
                    from assignment_refresh import refresh_assignments
                    await asyncio.to_thread(refresh_assignments,self.store,now)
                except asyncio.CancelledError:raise
                except Exception as error:
                    self.store.feed_failure('assignment_refresh',type(error).__name__+
                        '; prior tentative source dates retained; no new defender assumed.')
            pending=list(dict.fromkeys(pending))
            waiting=target in pending or not through or through<target
            if waiting and status.get('last_attempt'):
                self.store.feed_failure('nba_daily_refresh','Waiting for publication of '+target+
                    '; latest published covered game date is '+str(through or 'unknown')+'. Hourly catch-up remains enabled.')
            elif game_attempt:
                older_pending=[day for day in pending if day<target]
                detail='Required published game inventory for '+target+' imported.'
                if older_pending:
                    detail+=f' {len(older_pending)} older dates remain pending; hourly catch-up is automatic. See pending_dates.'
                self.store.feed_success('nba_daily_refresh',detail)
            next_run=deadline if local<deadline else dt.datetime.combine(today+dt.timedelta(days=1),dt.time(11,30),EASTERN)
            status.update(enabled=True,phase='waiting_for_publication' if waiting else 'updated',timezone='America/New_York',
                          daily_time='11:30',target_game_date=target,next_daily_run=next_run.isoformat(),
                          pending_dates=pending,source_data_through_date=through,retry_seconds=3600,
                          older_pending_dates=[day for day in pending if day<target],
                          game_refresh_phase='waiting_for_publication' if waiting else 'updated',
                          completeness_scope='Published game inventory processed; all NBA games and every tracking metric are not independently verified by this status.',
                          tracking_retry_seconds=3600,
                          source='Independently published public NBA extracts; existing workbook entities only',
                          note='Game refresh status covers imported game observations only. Tracking metrics and tentative assignments have separate coverage and source dates; rebuilding Excel does not make every field current.')
            self.store.set_checkpoint('nightly_nba_refresh',status)
            # Persist current coverage before rebuilding, including failed or
            # unchanged source checks. A new file time is never a source date.
            export_last=_aware(status.get('last_export_attempt'))
            export_retry=bool(status.get('master_export_error') and
                              (not export_last or (now-export_last).total_seconds()>=3600))
            if (game_attempt or tracking_attempt or export_retry) and self.exporter and self.data_dir:
                from pathlib import Path
                status['last_export_attempt']=now.isoformat()
                try:
                    await asyncio.to_thread(self.exporter,self.store,Path(self.data_dir)/'exports')
                    status['master_export_updated_at']=now.isoformat()
                    status['master_export_error']=None
                    self.store.feed_success('master_export',
                        'Master Excel rebuilt from saved records; source freshness remains separate in Refresh_Coverage.')
                except asyncio.CancelledError:raise
                except Exception as error:
                    status['master_export_error']=type(error).__name__
                    self.store.feed_failure('master_export',type(error).__name__+'; previous Excel export retained.')
                self.store.set_checkpoint('nightly_nba_refresh',status)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.store.feed_failure('nba_daily_refresh',type(error).__name__)
            await asyncio.sleep(60 if self.store.get_checkpoint('dataset_refresh_scope') else 5)
