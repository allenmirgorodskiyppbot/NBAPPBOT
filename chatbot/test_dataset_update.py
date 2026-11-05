"""Manual updates compare real values and keep slow jobs guarded until complete."""
import asyncio
from contextlib import contextmanager
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import time
import tracemalloc
import unittest
from unittest.mock import AsyncMock,patch

from dataset_refresh import initialize_scope
from dataset_update import DatasetUpdateController,data_fingerprint,format_update_report,_ControllerStore
from master import MasterStore
from nightly_data import NightlyNBARefresh

NOW=dt.datetime(2026,11,5,16,30,tzinfo=dt.timezone.utc)
DAY='2026-11-04'


class DatasetUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=MasterStore(self.root/'master.sqlite')
        initialize_scope(self.store)
        self.now=[NOW]
        owner=self
        class Nightly:
            store=owner.store
            data_dir=owner.root
            manual_update_active=False
            def now(self):return owner.now[0]
            async def refresh_now(self,*,export=True):
                owner.calls.append(('nightly',export))
                return owner.store.get_checkpoint('nightly_nba_refresh')
        self.nightly=Nightly();self.calls=[]
        self.store.set_checkpoint('nightly_nba_refresh',{
            'phase':'updated','game_refresh_phase':'updated','target_game_date':DAY,
            'source_data_through_date':DAY,'target_inventory_verified':True,'pending_dates':[],
            'tracking_refresh_phase':'collected','tracking_source_data_through_date':DAY,
            'master_export_updated_at':NOW.isoformat(),'master_export_error':None})
        self.store.set_checkpoint('tracking_refresh_status',{'coverage':[
            {'category':'Player totals','status':'collected','source_data_through_date':DAY}]})
        self.store.set_checkpoint('supplemental_data_status',{'phase':'updated'})
        self.store.set_checkpoint('nba_schedule_today',{'status':'success','error':None,
            'checked_at':NOW.isoformat(),'date':'2026-11-05','source':'independent source fixture'})
        self.file=self.root/'exports'/'NBA_Master.xlsx';self.file.parent.mkdir()
        self.file.write_bytes(b'prior complete export fixture')

    def controller(self,**kwargs):
        return DatasetUpdateController(self.nightly,**kwargs)

    def player(self,points=20):
        self.store.upsert_game({'id':'prior-game','date':DAY,'status':'final','home_team':'NYK','away_team':'BOS'})
        self.store.upsert_player_game({'game_id':'prior-game','player':'Jalen Brunson','player_id':'name:jalenbrunson',
            'team':'NYK','opponent':'BOS','date':DAY,'season':'2026-27','season_type':'Regular Season',
            'appearance_state':'Played','pts':points,'minutes':30})

    async def test_noop_checks_reuse_existing_good_excel_and_qualify_currentness(self):
        export=AsyncMock()
        report=await self.controller(export_refresh=export).refresh()
        self.assertEqual(self.calls,[('nightly',False)])
        export.assert_not_awaited()
        self.assertEqual(report['phase'],'unchanged')
        self.assertTrue(report['export']['reused'])
        self.assertIn('for the saved sources checked',format_update_report(report))

    async def test_count_stable_player_correction_is_a_real_update_and_exports_once_after_import(self):
        self.player()
        async def supplemental():
            with self.store.connect() as db:db.execute('UPDATE player_games SET pts=27 WHERE game_id=?',('prior-game',))
            self.calls.append(('supplemental',True));return {'phase':'updated'}
        async def export():
            with self.store.connect() as db:self.assertEqual(db.execute('SELECT pts FROM player_games').fetchone()[0],27)
            self.calls.append(('export',True))
        report=await self.controller(supplemental_refresh=supplemental,export_refresh=export).refresh()
        self.assertTrue(report['changed'])
        self.assertEqual(self.calls,[('nightly',False),('supplemental',True),('export',True)])
        self.assertEqual(self.store.status()['counts']['player_games'],1)

    async def test_timestamp_only_assignment_recheck_does_not_change_dataset_or_rebuild_excel(self):
        payload={'selected_player':'Jalen Brunson','primary':'Alex Caruso','assignment_asof':DAY,
                 'last_checked':'2026-11-05T15:30:00Z'}
        with self.store.connect() as db:
            db.execute('CREATE TABLE refreshed_assignments(source_key TEXT PRIMARY KEY,payload_json TEXT)')
            db.execute('INSERT INTO refreshed_assignments VALUES (?,?)',('a',json.dumps(payload)))
        before=data_fingerprint(self.store)
        async def supplemental():
            payload['last_checked']=NOW.isoformat()
            with self.store.connect() as db:db.execute('UPDATE refreshed_assignments SET payload_json=?',(json.dumps(payload),))
            return {'phase':'updated'}
        export=AsyncMock()
        report=await self.controller(supplemental_refresh=supplemental,export_refresh=export).refresh()
        self.assertEqual(data_fingerprint(self.store),before)
        self.assertFalse(report['changed']);export.assert_not_awaited()

    async def test_tracking_value_change_with_same_rows_and_gp_is_detected(self):
        payload={'entity':'Jalen Brunson','category':'Passing','season':'2026-27','gp':8,
                 'fields':{'potential_assists':75},'retrieved_at':NOW.isoformat()}
        with self.store.connect() as db:db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
            ('2026-27','Jalen Brunson','Passing',json.dumps(payload)))
        original=data_fingerprint(self.store)
        payload['retrieved_at']='2026-11-05T16:31:00Z'
        with self.store.connect() as db:db.execute('UPDATE refreshed_tracking SET payload_json=?',(json.dumps(payload),))
        self.assertEqual(data_fingerprint(self.store),original)
        payload['fields']['potential_assists']=76
        with self.store.connect() as db:db.execute('UPDATE refreshed_tracking SET payload_json=?',(json.dumps(payload),))
        self.assertNotEqual(data_fingerprint(self.store),original)

    async def test_missing_or_failed_export_rebuilds_once_even_without_new_data(self):
        for missing in (True,False):
            with self.subTest(missing=missing):
                state=self.store.get_checkpoint('nightly_nba_refresh')
                state['master_export_error']=None if missing else 'OSError'
                self.store.set_checkpoint('nightly_nba_refresh',state)
                if missing:self.file.unlink(missing_ok=True)
                else:self.file.write_bytes(b'old file')
                async def rebuild():self.file.write_bytes(b'complete export')
                export=AsyncMock(side_effect=rebuild)
                report=await self.controller(export_refresh=export).refresh()
                export.assert_awaited_once();self.assertFalse(report['changed'])
                self.assertEqual(report['export']['phase'],'ready')

    async def test_partial_source_and_unknown_coverage_never_claim_already_current(self):
        state=self.store.get_checkpoint('nightly_nba_refresh')
        state.update(tracking_refresh_phase='partial',tracking_source_data_through_date=None,
                     pending_dates=['2026-11-02'],older_pending_dates=['2026-11-02'])
        self.store.set_checkpoint('nightly_nba_refresh',state)
        self.store.set_checkpoint('tracking_refresh_status',{'coverage':[
            {'category':'Hustle','status':'collected','source_data_through_date':None}]})
        report=await self.controller(supplemental_refresh=AsyncMock(return_value={'phase':'unavailable'})).refresh()
        text=format_update_report(report)
        self.assertEqual(report['phase'],'partial');self.assertFalse(report['changed'])
        self.assertIn('No new changes; some data couldn’t be verified',text)
        self.assertNotIn('Already up to date',text)
        self.assertTrue(any('covered-game dates' in gap for gap in report['remaining_gaps']))

    async def test_none_schedule_callback_reads_failed_persisted_check(self):
        async def schedule():
            self.store.set_checkpoint('nba_schedule_today',{'status':'failed','error':'HTTP 403'})
        report=await self.controller(schedule_refresh=schedule).refresh()
        self.assertEqual(report['phase'],'partial')
        self.assertTrue(any('schedule' in gap for gap in report['remaining_gaps']))

    async def test_background_job_is_busy_until_cancelled_thread_backed_export_really_finishes(self):
        self.file.unlink()
        entered=asyncio.Event();release=asyncio.Event()
        async def export():
            entered.set();await release.wait();self.file.write_bytes(b'complete')
        controller=self.controller(export_refresh=export)
        task=asyncio.create_task(controller.refresh())
        await entered.wait();task.cancel();await asyncio.sleep(0)
        busy=await controller.refresh()
        self.assertEqual(busy['phase'],'busy');self.assertTrue(self.nightly.manual_update_active)
        release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.file.exists());self.assertFalse(self.nightly.manual_update_active)

    async def test_cancellation_survives_later_callback_failure_without_running_more_stages(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def supplemental():
            entered.set();await release.wait();raise ValueError('source rejected after interruption')
        schedule=AsyncMock();export=AsyncMock()
        controller=self.controller(supplemental_refresh=supplemental,schedule_refresh=schedule,export_refresh=export)
        task=asyncio.create_task(controller.refresh())
        await entered.wait();task.cancel();await asyncio.sleep(0)
        self.assertEqual((await controller.refresh())['phase'],'busy')
        release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        schedule.assert_not_awaited();export.assert_not_awaited()
        self.assertEqual(self.store.get_checkpoint('dataset_update_status')['phase'],'interrupted')
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh')['master_export_error'],'ManualUpdateInterrupted')

    async def test_failed_final_export_keeps_previous_file_and_retries_on_next_noop_refresh(self):
        self.player()
        async def correction():
            with self.store.connect() as db:db.execute('UPDATE player_games SET pts=28')
            return {'phase':'updated'}
        original=self.file.read_bytes()
        first=await self.controller(supplemental_refresh=correction,
            export_refresh=AsyncMock(side_effect=OSError('disk temporarily unavailable'))).refresh()
        self.assertTrue(first['changed']);self.assertEqual(first['export']['phase'],'failed')
        self.assertEqual(self.file.read_bytes(),original)
        async def rebuild():self.file.write_bytes(b'current verified dataset export')
        export=AsyncMock(side_effect=rebuild)
        second=await self.controller(export_refresh=export).refresh()
        export.assert_awaited_once();self.assertFalse(second['changed'])
        self.assertEqual(second['export']['phase'],'ready')

    async def test_prior_history_export_failure_forces_one_noop_rebuild_then_clears_matching_error(self):
        self.store.set_checkpoint('history_upgrade_status',{'phase':'complete','export_error':'OSError',
            'export_error_at':'2026-11-05T15:30:00Z','other_saved_value':'preserved'})
        export=AsyncMock()
        report=await self.controller(export_refresh=export).refresh()
        export.assert_awaited_once();self.assertFalse(report['changed'])
        self.assertEqual(report['export']['phase'],'ready')
        history=self.store.get_checkpoint('history_upgrade_status')
        self.assertNotIn('export_error',history);self.assertNotIn('export_error_at',history)
        self.assertEqual(history['other_saved_value'],'preserved')
        again=AsyncMock();await self.controller(export_refresh=again).refresh();again.assert_not_awaited()

    async def test_failed_manual_export_retains_history_error_and_missing_exporter_reports_failure(self):
        failed={'phase':'complete','export_error':'OSError'} # Legacy failure without a clock.
        self.store.set_checkpoint('history_upgrade_status',failed)
        report=await self.controller(export_refresh=AsyncMock(side_effect=OSError('still unwritable'))).refresh()
        self.assertEqual(self.store.get_checkpoint('history_upgrade_status'),failed)
        self.assertEqual(report['export']['phase'],'failed')
        report=await self.controller().refresh()
        self.assertEqual(report['export']['phase'],'failed')
        self.assertNotIn('Already up to date',format_update_report(report))

    async def test_newer_history_export_failure_is_not_cleared_by_finishing_manual_export(self):
        self.store.set_checkpoint('history_upgrade_status',{'phase':'complete','export_error':'OSError',
            'export_error_at':'2026-11-05T15:30:00Z'})
        newer={'phase':'partial','export_error':'OSError','export_error_at':'2026-11-05T16:31:00Z'}
        async def export():self.store.set_checkpoint('history_upgrade_status',newer)
        report=await self.controller(export_refresh=export).refresh()
        self.assertEqual(self.store.get_checkpoint('history_upgrade_status'),newer)
        self.assertEqual(report['export']['phase'],'failed')

    async def test_fingerprint_is_independent_of_insertion_order_and_keeps_real_changes(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE refreshed_assignments(source_key TEXT PRIMARY KEY,payload_json TEXT)')
            db.executemany('INSERT INTO refreshed_assignments VALUES (?,?)',[
                ('b',json.dumps({'primary':'Defender B'})),('a',json.dumps({'primary':'Defender A'}))])
        before=data_fingerprint(self.store)
        with self.store.connect() as db:
            db.execute('DELETE FROM refreshed_assignments')
            db.executemany('INSERT INTO refreshed_assignments VALUES (?,?)',[
                ('a',json.dumps({'primary':'Defender A'})),('b',json.dumps({'primary':'Defender B'}))])
        self.assertEqual(data_fingerprint(self.store),before)
        with self.store.connect() as db:
            db.execute('UPDATE refreshed_assignments SET payload_json=? WHERE source_key=?',
                       (json.dumps({'primary':'Different defender'}),'a'))
        self.assertNotEqual(data_fingerprint(self.store),before)

    async def test_fingerprint_large_text_population_has_bounded_python_memory(self):
        payload=json.dumps({'selected_player':'Jalen Brunson','primary':'Defender',
                            'reason':'Published contextual evidence. '*150})
        with self.store.connect() as db:
            db.execute('CREATE TABLE refreshed_assignments(source_key TEXT PRIMARY KEY,payload_json TEXT)')
            db.executemany('INSERT INTO refreshed_assignments VALUES (?,?)',
                           ((f'row-{index:05d}',payload) for index in range(5000)))
        tracemalloc.start()
        try:
            digest=data_fingerprint(self.store)
            _,peak=tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(digest),64)
        self.assertLess(peak,8_000_000,'Fingerprinting must not retain the whole text population in Python.')

    async def test_status_reports_active_stage_and_elapsed_without_starting_another_update(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def supplemental():
            entered.set();await release.wait();return {'phase':'updated'}
        controller=self.controller(supplemental_refresh=supplemental)
        task=asyncio.create_task(controller.refresh())
        await entered.wait()
        self.now[0]+=dt.timedelta(seconds=90)
        before=self.store.get_checkpoint('nightly_nba_refresh')
        status=controller.status()
        self.assertEqual(status['phase'],'checking');self.assertTrue(status['active'])
        self.assertEqual(status['stage'],'history_availability')
        self.assertEqual((status['elapsed_seconds'],status['stage_elapsed_seconds']),(90,90))
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh'),before)
        self.assertEqual(self.calls,[('nightly',False)])
        release.set();await task
        self.now[0]+=dt.timedelta(minutes=5)
        final=controller.status()
        self.assertFalse(final['active']);self.assertEqual(final['stage'],'finished')
        self.assertEqual(final['elapsed_seconds'],90)

    async def test_stale_saved_checking_state_does_not_claim_an_active_refresh(self):
        self.store.set_checkpoint('dataset_update_status',{'phase':'checking','stage':'excel',
            'detail':'Rebuilding the master Excel file','started_at':(NOW-dt.timedelta(minutes=8)).isoformat()})
        report=self.controller().status()
        self.assertEqual(report['phase'],'interrupted');self.assertFalse(report['active'])
        self.assertIn('retry',report['detail'])

    async def hold_writer(self):
        entered=threading.Event();release=threading.Event()
        def writer():
            db=sqlite3.connect(self.store.path,timeout=1)
            try:
                db.execute('BEGIN IMMEDIATE');entered.set();release.wait(3)
                db.rollback()
            finally:db.close()
        thread=threading.Thread(target=writer,daemon=True);thread.start()
        self.addCleanup(thread.join,2);self.addCleanup(release.set)
        self.assertTrue(await asyncio.to_thread(entered.wait,1))
        return release

    async def test_real_writer_contention_keeps_heartbeat_running_and_saves_one_completion(self):
        release=await self.hold_writer()
        stamps=[];done=False
        async def heartbeat():
            while not done:
                stamps.append(time.monotonic());await asyncio.sleep(.01)
        pulse=asyncio.create_task(heartbeat())
        async def unlock():await asyncio.sleep(.4);release.set()
        unlocker=asyncio.create_task(unlock())
        try:
            report=await self.controller().refresh()
        finally:
            done=True;release.set();await pulse;await unlocker
        self.assertGreater(len(stamps),15)
        self.assertLess(max(b-a for a,b in zip(stamps,stamps[1:])),.2)
        self.assertEqual(self.calls,[('nightly',False)])
        self.assertEqual(self.store.get_checkpoint('dataset_update_status')['phase'],report['phase'])

    async def test_cancel_during_writer_wait_drains_short_attempt_without_orphan_or_endless_cleanup(self):
        release=await self.hold_writer();controller=self.controller()
        task=asyncio.create_task(controller.refresh())
        await asyncio.sleep(.18);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,.7)
        self.assertFalse(self.nightly.manual_update_active);self.assertFalse(controller.lock.locked())
        self.assertEqual(self.calls,[])
        self.assertFalse(release.is_set())
        release.set();await asyncio.sleep(.2)
        self.assertEqual(self.calls,[])
        self.assertEqual(controller.status()['phase'],'interrupted')

    async def test_readonly_database_failure_is_visible_and_never_retried_as_writer_contention(self):
        attempts=[]
        @contextmanager
        def readonly():
            db=sqlite3.connect(Path(self.store.path).as_uri()+'?mode=ro',uri=True)
            db.row_factory=sqlite3.Row
            try:
                attempts.append(True)
                with db:yield db
            finally:db.close()
        with patch.object(self.store,'connect',readonly):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                await asyncio.wait_for(self.controller().refresh(),1)
        self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_READONLY)
        self.assertEqual(len(attempts),2) # One prior-state read, then one failed stage write.
        self.assertFalse(self.nightly.manual_update_active);self.assertEqual(self.calls,[])

    async def test_full_and_uncoded_operational_errors_are_not_hidden_or_retried(self):
        for code in (sqlite3.SQLITE_FULL,None):
            calls=[]
            error=sqlite3.OperationalError('database is locked')
            if code is not None:error.sqlite_errorcode=code
            def failed():calls.append(True);raise error
            with self.subTest(code=code),self.assertRaises(sqlite3.OperationalError):
                await self.controller()._database(failed)
            self.assertEqual(len(calls),1)

    async def test_initial_database_wait_reports_this_active_job_instead_of_old_interruption(self):
        entered=threading.Event();release=threading.Event();original=_ControllerStore.get_checkpoint
        controller=self.controller();controller._progress={'phase':'interrupted','detail':'Previous job'}
        def first_read(store,name,default=None):
            if name=='dataset_update_status' and not entered.is_set():
                entered.set();release.wait(2)
            return original(store,name,default)
        with patch.object(_ControllerStore,'get_checkpoint',first_read):
            task=asyncio.create_task(controller.refresh())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,1))
                report=controller.status()
                self.assertEqual(report['phase'],'checking');self.assertTrue(report['active'])
                self.assertEqual(report['stage'],'prepare')
            finally:release.set()
            await task


class NightlyManualNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=MasterStore(self.root/'master.sqlite')
        self.scope=initialize_scope(self.store);self.now=[NOW];self.calls=[]
        owner=self
        class Worker:
            async def request(self,url):
                name=url.rsplit('/',1)[-1];owner.calls.append(name)
                if name=='existing_tracking.json':
                    return {'schema_version':1,'scope_revision':owner.scope['revision'],'season':'2026-27',
                        'last_attempt':owner.now[0].isoformat(),'records':[],
                        'coverage':[{'category':'Overall','status':'unavailable'}]}
                day=name[:-5]
                return {'schema_version':1,'scope_revision':owner.scope['revision'],'upstream_full_game_validated':True,
                    'source':'espn','date':day,'published_at':owner.now[0].isoformat(),'expected_games':0,
                    'games':[],'player_games':[],'complete':True,'pending_game_ids':[],'errors':[]}
        self.worker=Worker()
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True,'handled_target_date':'2026-11-03','pending_dates':[],
            'last_attempt':NOW.isoformat(),'last_tracking_attempt':NOW.isoformat()})

    def service(self,notifier=None):
        return NightlyNBARefresh(self.store,self.worker,now=lambda:self.now[0],notifier=notifier)

    async def tick(self,service,*,force=False):
        with patch('dataset_refresh.refresh_published_scope',new=AsyncMock(return_value=self.scope)):
            return await service.tick(force=force,export=False)

    async def test_force_checks_yesterday_before_deadline_without_consuming_daily_notice(self):
        self.now[0]=dt.datetime(2026,11,5,15,0,tzinfo=dt.timezone.utc) # 10 AM Eastern
        notifier=AsyncMock(return_value=True)
        await self.tick(self.service(notifier),force=True)
        self.assertIn(DAY+'.json',self.calls)
        self.assertFalse(self.store.get_checkpoint('dataset_daily_notification:2026-11-05'))
        notifier.assert_not_awaited()
        self.calls.clear();self.now[0]=NOW
        await self.tick(self.service(notifier))
        self.assertIn(DAY+'.json',self.calls);notifier.assert_awaited_once()

    async def test_confirmed_daily_notice_is_once_across_hourly_retries_and_restart(self):
        notifier=AsyncMock(return_value=True)
        await self.tick(self.service(notifier))
        self.now[0]+=dt.timedelta(hours=1)
        await self.tick(self.service(notifier))
        notifier.assert_awaited_once()
        message,report=notifier.await_args.args
        self.assertEqual(report['notification_day'],'2026-11-05')
        self.assertEqual(report['notification_event_key'],'dataset-daily:2026-11-05')
        self.assertIn('Tracking: partial',message)
        self.assertEqual(self.store.get_checkpoint('dataset_daily_notification:2026-11-05')['phase'],'queued')

    async def test_notifier_missing_false_or_failed_never_permanently_suppresses_notice(self):
        await self.tick(self.service())
        self.assertFalse(self.store.get_checkpoint('dataset_daily_notification:2026-11-05'))
        notifier=AsyncMock(side_effect=[False,RuntimeError('outbox unavailable'),True])
        for expected in ('not_queued','failed','queued'):
            await self.tick(self.service(notifier))
            self.assertEqual(self.store.get_checkpoint('dataset_daily_notification:2026-11-05')['phase'],expected)
            self.now[0]+=dt.timedelta(minutes=1)
        self.assertEqual(notifier.await_count,3)

    async def test_daily_event_day_is_captured_before_a_long_refresh_crosses_midnight(self):
        self.now[0]=dt.datetime(2026,11,6,4,59,tzinfo=dt.timezone.utc)
        notifier=AsyncMock(return_value=True)
        original=self.worker.request
        async def slow(url):
            value=await original(url)
            if url.endswith('existing_tracking.json'):
                self.now[0]=dt.datetime(2026,11,6,5,1,tzinfo=dt.timezone.utc)
            return value
        self.worker.request=slow
        await self.tick(self.service(notifier))
        report=notifier.await_args.args[1]
        self.assertEqual(report['notification_day'],'2026-11-05')
        self.assertFalse(self.store.get_checkpoint('dataset_daily_notification:2026-11-06'))


if __name__=='__main__':unittest.main()
