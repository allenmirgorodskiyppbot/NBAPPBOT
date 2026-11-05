"""Explicit refresh orchestration and honest, reusable dataset status messages."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import datetime as dt
import hashlib
import inspect
import json
import math
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN=ZoneInfo('America/New_York')
VOLATILE={'updated_at','retrieved_at','checked_at','last_checked','last_checked_at','source_checked_at',
          'published_at','source_published_at','source_content_sha256','source','source_id',
          'field_provenance','date_mapping_sources','coverage_note','publication_time_kind',
          'publication_source','last_attempt','generated_at'}
DATA_TABLES=('games','player_games','season_schedule','refreshed_tracking',
             'measured_pair_history','structured_availability','refreshed_assignments')
CONTROLLER_BUSY_TIMEOUT_MS=100


def _sqlite_contention(error):
    code=getattr(error,'sqlite_errorcode',None)
    return (isinstance(error,sqlite3.OperationalError) and type(code) is int
            and code & 255 in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED})


class _ControllerStore:
    """Use fresh short transactions without changing the shared store's connection policy."""
    def __init__(self,store):self.store=store

    @contextmanager
    def connect(self):
        with self.store.connect() as db:
            db.execute('PRAGMA busy_timeout='+str(CONTROLLER_BUSY_TIMEOUT_MS))
            yield db

    def get_checkpoint(self,name,default=None):
        with self.connect() as db:
            row=db.execute('SELECT value_json FROM checkpoints WHERE name=?',(name,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_checkpoint(self,name,value):
        from master import encoded
        with self.connect() as db:
            db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                       (name,encoded(value)))

    def feed_success(self,name,detail):
        from master import now
        stamp=now()
        with self.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_success,last_attempt,detail,error) VALUES (?,?,?,?,NULL) '
                'ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success,last_attempt=excluded.last_attempt,'
                'detail=excluded.detail,error=NULL',(name,stamp,stamp,str(detail)[:1000]))

    def feed_failure(self,name,error):
        from master import now
        stamp=now()
        with self.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_error,last_attempt,error) VALUES (?,?,?,?) '
                'ON CONFLICT(name) DO UPDATE SET last_error=excluded.last_error,last_attempt=excluded.last_attempt,'
                'error=excluded.error',(name,stamp,stamp,str(error)[:1000]))


def _record_interrupted(store,progress,stamp):
    """One short atomic cleanup attempt; cancellation never retries forever."""
    from master import encoded
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT value_json FROM checkpoints WHERE name='nightly_nba_refresh'").fetchone()
        state=json.loads(row[0]) if row else {}
        state.update(master_export_error='ManualUpdateInterrupted',last_export_attempt=stamp)
        for name,value in (('nightly_nba_refresh',state),('dataset_update_status',progress)):
            db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                       (name,encoded(value)))


def _instant(value):
    try:
        moment=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return moment if moment.tzinfo else None
    except (ValueError,TypeError):
        return None


def _semantic(value):
    if isinstance(value,dict):
        return {k:_semantic(v) for k,v in value.items() if k not in VOLATILE}
    if isinstance(value,(list,tuple)):
        return [_semantic(v) for v in value]
    if type(value) is float:
        return int(value) if math.isfinite(value) and value.is_integer() else value if math.isfinite(value) else str(value)
    return value


def data_fingerprint(store):
    """Stream actual values in stable primary-key order, without a table-sized list."""
    digest=hashlib.sha256()
    with store.connect() as db:
        db.execute('BEGIN')
        available={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in DATA_TABLES:
            if table not in available:
                continue
            columns=list(db.execute('PRAGMA table_info('+table+')'))
            keys=[row[1] for row in sorted(columns,key=lambda row:row[5]) if row[5]]
            # Known tables have primary keys. The fallback also keeps a future
            # keyless table deterministic without collecting its rows in Python.
            fields=[row[1] for row in columns if row[1] not in VOLATILE]
            order=keys or fields
            quote=lambda field:'"'+field.replace('"','""')+'"'
            query='SELECT '+','.join(map(quote,fields))+' FROM '+table
            if order:query+=' ORDER BY '+','.join(map(quote,order))
            digest.update(table.encode())
            for raw in db.execute(query):
                row=dict(raw)
                if isinstance(row.get('payload_json'),str):
                    try:row['payload_json']=json.loads(row['payload_json'])
                    except ValueError:pass
                encoded=json.dumps(_semantic(row),sort_keys=True,separators=(',',':'),default=str)
                digest.update(encoded.encode());digest.update(b'\n')
        scope=store.get_checkpoint('dataset_refresh_scope',{}) or {}
        digest.update(json.dumps(_semantic(scope),sort_keys=True,separators=(',',':'),default=str).encode())
    return digest.hexdigest()


def _clear_history_export_error(store,captured):
    """Clear only the exact failed export covered by this successful rebuild."""
    if not captured.get('export_error'):return
    signature=(captured.get('export_error'),captured.get('export_error_at'))
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT value_json FROM checkpoints WHERE name='history_upgrade_status'").fetchone()
        current=json.loads(row[0]) if row else {}
        if (current.get('export_error'),current.get('export_error_at'))==signature:
            current.pop('export_error',None);current.pop('export_error_at',None)
            db.execute("UPDATE checkpoints SET value_json=? WHERE name='history_upgrade_status'",(json.dumps(current),))


def build_saved_report(store, *, nightly=None, supplemental=None, now=None, started_at=None,
                       changed=False, export=None, errors=()):
    now=now or dt.datetime.now(dt.timezone.utc)
    nightly=nightly if nightly is not None else store.get_checkpoint('nightly_nba_refresh',{}) or {}
    supplemental=supplemental if supplemental is not None else store.get_checkpoint('supplemental_data_status',{}) or {}
    tracking=store.get_checkpoint('tracking_refresh_status',{}) or {}
    schedule=store.get_checkpoint('nba_schedule_today',{}) or {}
    scope=store.get_checkpoint('dataset_scope_refresh',{}) or {}
    history=store.get_checkpoint('history_upgrade_status',{}) or {}
    pending=list(nightly.get('pending_dates') or [])
    gaps=[]
    if nightly.get('game_refresh_phase')!='updated' or not nightly.get('source_data_through_date'):
        gaps.append('Game imports are incomplete or unavailable.')
    if pending:
        gaps.append(f'{len(pending)} game date(s) remain pending.')
    if nightly.get('target_inventory_verified') is not True:
        gaps.append('Game inventory has not been independently verified.')
    if nightly.get('tracking_refresh_phase')!='collected':
        gaps.append('Tracking is '+str(nightly.get('tracking_refresh_phase') or 'unverified')+'.')
    unknown=[r.get('category') for r in tracking.get('coverage',[])
             if r.get('status')=='collected' and not r.get('source_data_through_date')]
    if unknown or not nightly.get('tracking_source_data_through_date'):
        gaps.append('Tracking covered-game dates are unknown'+(f' in {len(unknown)} categories.' if unknown else '.'))
    unavailable=[r.get('category') for r in tracking.get('coverage',[]) if r.get('status')!='collected']
    if unavailable:
        gaps.append('Unavailable tracking: '+', '.join(str(v) for v in unavailable)+'.')
    if supplemental.get('phase') not in {'updated','unchanged'} or supplemental.get('throttled'):
        gaps.append('Historical/availability sources are '+str(supplemental.get('phase') or 'unverified')+'.')
    checked=_instant(schedule.get('checked_at'))
    if (schedule.get('status')!='success' or schedule.get('error') or not schedule.get('source')
            or schedule.get('date')!=now.astimezone(EASTERN).date().isoformat() or not checked
            or not -300<=(now-checked).total_seconds()<=900):
        gaps.append('The current game schedule is unverified or its check has expired.')
    if scope.get('phase')=='retained_prior_scope':
        gaps.append('Current roster scope could not be reverified; prior scope was retained.')
    export=export if export is not None else {
        'phase':'failed' if nightly.get('master_export_error') else 'ready' if nightly.get('master_export_updated_at') else 'unverified',
        'updated_at':nightly.get('master_export_updated_at'),'error_type':nightly.get('master_export_error')}
    if history.get('export_error'):
        export={**export,'phase':'failed','error_type':history['export_error'],
                'history_export_error_at':history.get('export_error_at')}
    if export.get('phase')!='ready':
        gaps.append('Excel export is '+str(export.get('phase') or 'unverified')+'.')
    gaps.extend(str(value) for value in errors)
    return {'schema_version':1,'phase':'partial' if gaps else 'updated' if changed else 'unchanged',
        'changed':bool(changed),'started_at':(started_at or now).isoformat(),'finished_at':now.isoformat(),
        'run_date':(started_at or now).astimezone(EASTERN).date().isoformat(),
        'nightly':nightly,'supplemental':supplemental,'schedule':schedule,'export':export,
        'remaining_gaps':list(dict.fromkeys(gaps)),
        'scope':'Saved published records only; current picks retain their independent freshness gates.'}


def format_update_report(report, daily=False):
    if report.get('phase')=='busy':
        return 'A dataset refresh is already running. I’ll send its result when it finishes.'
    gaps=report.get('remaining_gaps') or []
    if daily:
        stamp=_instant(report.get('finished_at'))
        when=stamp.astimezone(EASTERN).strftime('%-I:%M %p') if stamp else 'saved check time'
        heading='Daily refresh finished at '+when+' Eastern.'
    elif report.get('changed'):
        heading='Updated the saved dataset.' if not gaps else 'Updated some data; some checks still need attention.'
    else:
        heading='Already up to date for the saved sources checked.' if not gaps else 'No new changes; some data couldn’t be verified.'
    nightly=report.get('nightly') or {}
    lines=[heading,'Game stats through: '+str(nightly.get('source_data_through_date') or 'unverified')+'.',
        'Tracking: '+str(nightly.get('tracking_refresh_phase') or 'unverified')+'.',
        'Excel: '+str((report.get('export') or {}).get('phase') or 'unverified')+'.']
    if gaps:
        lines.append('Remaining gaps: '+' '.join(gaps[:6]))
        if len(gaps)>6:lines.append(f'{len(gaps)-6} additional checks remain unverified; use /freshness for game readiness.')
    return '\n'.join(lines)


async def _complete_callback(callback):
    """Keep a real thread-backed operation guarded even if its caller cancels."""
    value=callback()
    if not inspect.isawaitable(value):
        return value
    task=asyncio.ensure_future(value)
    interrupted=False
    while True:
        try:
            result=await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            interrupted=True
            if task.done():
                raise
        except BaseException:
            if interrupted:
                raise asyncio.CancelledError() from None
            raise
    if interrupted:
        raise asyncio.CancelledError()
    return result


class DatasetUpdateController:
    def __init__(self,nightly,*,supplemental_refresh=None,schedule_refresh=None,export_refresh=None):
        self.nightly=nightly
        self.supplemental_refresh=supplemental_refresh
        self.schedule_refresh=schedule_refresh
        self.export_refresh=export_refresh
        self.lock=asyncio.Lock()
        self._progress=None
        self._database_waiters=0

    async def _database(self,operation,*args,**kwargs):
        """Never block the loop; retry only real BUSY/LOCKED with a fresh operation."""
        delay=.05
        waiting=False
        try:
            while True:
                try:
                    return await _complete_callback(lambda:asyncio.to_thread(operation,*args,**kwargs))
                except sqlite3.Error as error:
                    if not _sqlite_contention(error):raise
                    if not waiting:
                        waiting=True;self._database_waiters+=1
                    await asyncio.sleep(delay)
                    delay=min(1,delay*2)
        finally:
            if waiting:self._database_waiters-=1

    def status(self):
        """Read progress and real elapsed time without launching another refresh."""
        active=self.lock.locked() or getattr(self.nightly,'manual_update_active',False)
        if self._progress and (active or self._progress.get('phase')=='interrupted'):
            report=dict(self._progress)
        else:
            report=dict(_ControllerStore(self.nightly.store).get_checkpoint('dataset_update_status',{}) or {})
        started=_instant(report.get('started_at'))
        stage_started=_instant(report.get('stage_started_at'))
        now=self.nightly.now()
        finished=_instant(report.get('finished_at'))
        endpoint=now if report.get('phase')=='checking' else finished or now
        if started:report['elapsed_seconds']=max(0,(endpoint-started).total_seconds())
        if stage_started:report['stage_elapsed_seconds']=max(0,(endpoint-stage_started).total_seconds())
        report['active']=bool(active and report.get('phase')=='checking')
        report['database_waiting']=self._database_waiters>0
        if report.get('phase')=='checking' and not active:
            report.update(phase='interrupted',detail='The previous refresh stopped before recording a result. Use /refresh to retry.')
        return report

    async def refresh(self,progress=None):
        if self.lock.locked() or getattr(self.nightly,'manual_update_active',False):
            return {'phase':'busy','changed':False}
        async with self.lock:
            self.nightly.manual_update_active=True
            store=_ControllerStore(self.nightly.store)
            started=self.nightly.now()
            self._progress={'phase':'checking','stage':'prepare','detail':'Preparing the dataset refresh',
                'started_at':started.isoformat(),'stage_started_at':started.isoformat()}
            interrupted_export=False
            errors=[]
            async def stage(detail,name):
                current=self.nightly.now()
                self._progress={'phase':'checking','started_at':started.isoformat(),
                    'detail':detail,'stage':name,'stage_started_at':current.isoformat(),
                    'updated_at':current.isoformat(),'elapsed_seconds':max(0,(current-started).total_seconds())}
                await self._database(store.set_checkpoint,'dataset_update_status',self._progress)
                if progress:
                    result=progress(detail)
                    if inspect.isawaitable(result):await result
            try:
                previous_update=await self._database(store.get_checkpoint,'dataset_update_status',{}) or {}
                interrupted_export=previous_update.get('phase') in {'checking','interrupted','failed'}
                await stage('Comparing the saved dataset','snapshot_before')
                before=await self._database(data_fingerprint,store)
                await stage('Checking game statistics and tracking','games_tracking')
                try:
                    await _complete_callback(lambda:self.nightly.refresh_now(export=False))
                except Exception as error:
                    errors.append('Game refresh failed: '+type(error).__name__+'.')
                supplemental=None
                if self.supplemental_refresh:
                    await stage('Checking historical statistics and availability','history_availability')
                    try:supplemental=await _complete_callback(self.supplemental_refresh)
                    except Exception as error:errors.append('Supplemental refresh failed: '+type(error).__name__+'.')
                if self.schedule_refresh:
                    await stage('Checking the current game schedule','schedule')
                    try:await _complete_callback(self.schedule_refresh)
                    except Exception as error:errors.append('Schedule check failed: '+type(error).__name__+'.')
                await stage('Comparing refreshed values','snapshot_after')
                after=await self._database(data_fingerprint,store)
                changed=before!=after
                export=None
                saved=await self._database(store.get_checkpoint,'nightly_nba_refresh',{}) or {}
                history_export=await self._database(store.get_checkpoint,'history_upgrade_status',{}) or {}
                directory=getattr(self.nightly,'data_dir',None)
                good_export=bool(not interrupted_export and directory and (Path(directory)/'exports'/'NBA_Master.xlsx').is_file()
                    and saved.get('master_export_updated_at') and not saved.get('master_export_error')
                    and not history_export.get('export_error'))
                if not changed and good_export:
                    export={'phase':'ready','updated_at':saved['master_export_updated_at'],'reused':True}
                elif self.export_refresh:
                    await stage('Rebuilding the master Excel file','excel')
                    state=await self._database(store.get_checkpoint,'nightly_nba_refresh',{}) or {}
                    state['last_export_attempt']=self.nightly.now().isoformat()
                    state['master_export_error']='RebuildInProgress'
                    await self._database(store.set_checkpoint,'nightly_nba_refresh',state)
                    try:
                        await _complete_callback(self.export_refresh)
                        await self._database(_clear_history_export_error,store,history_export)
                        state.update(master_export_updated_at=self.nightly.now().isoformat(),master_export_error=None)
                        export={'phase':'ready','updated_at':state['master_export_updated_at']}
                        await self._database(store.feed_success,'master_export','Master Excel rebuilt after all requested imports; source freshness remains separate.')
                    except Exception as error:
                        state['master_export_error']=type(error).__name__
                        export={'phase':'failed','error_type':type(error).__name__}
                        await self._database(store.feed_failure,'master_export',type(error).__name__+'; previous Excel export retained.')
                    await self._database(store.set_checkpoint,'nightly_nba_refresh',state)
                report=await self._database(build_saved_report,store,supplemental=supplemental,now=self.nightly.now(),
                    started_at=started,changed=changed,export=export,errors=errors)
                report.update(stage='finished',detail='Refresh completed with remaining source gaps.' if report['remaining_gaps']
                    else 'Refresh completed.',elapsed_seconds=max(0,(self.nightly.now()-started).total_seconds()))
                self._progress=report
                await self._database(store.set_checkpoint,'dataset_update_status',report)
                return report
            except asyncio.CancelledError:
                self._progress={'phase':'interrupted','started_at':started.isoformat(),
                    'finished_at':self.nightly.now().isoformat(),'stage':'interrupted',
                    'elapsed_seconds':max(0,(self.nightly.now()-started).total_seconds()),
                    'detail':'Refresh was interrupted after active work settled; a complete recheck remains available.'}
                try:
                    await _complete_callback(lambda:asyncio.to_thread(_record_interrupted,store,self._progress,self.nightly.now().isoformat()))
                except sqlite3.Error as error:
                    # A previous checking checkpoint and the durable Telegram
                    # job retain retryability if an unrelated writer remains.
                    if not _sqlite_contention(error):raise
                raise
            finally:
                self.nightly.manual_update_active=False
