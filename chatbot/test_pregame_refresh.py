"""Pregame game-source rechecks keep freshness separate from saved data."""
import asyncio
import copy
import datetime as dt
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from dataset_refresh import initialize_scope
from feeds import canonical_game, canonical_player
from master import MasterStore
from nightly_data import NightlyNBARefresh, import_daily_snapshot
from test_nightly_data import snapshot, NOW, DAY


async def _inline_executor(function, *args, **kwargs):
    return function(*args, **kwargs)


class PregameRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=MasterStore(Path(self.temp.name)/'master.sqlite')
        self.scope=initialize_scope(self.store)
        self.now=[NOW]
        inline=patch('nightly_data.asyncio.to_thread',_inline_executor)
        inline.start();self.addCleanup(inline.stop)

    def tracking(self):
        return {'schema_version':1,'scope_revision':self.scope['revision'],'season':'2026-27',
                'last_attempt':self.now[0].isoformat(),'records':[],'coverage':[]}

    def service(self, documents):
        owner=self
        class Worker:
            calls=[]
            async def request(inner,url):
                inner.calls.append(url)
                if url.endswith('/existing_tracking.json'):return owner.tracking()
                day=url.rsplit('/',1)[-1][:-5]
                data=documents.get(day)
                if isinstance(data,Exception):raise data
                if data is None:
                    httpx.Response(404,request=httpx.Request('GET',url)).raise_for_status()
                return copy.deepcopy(data)
        worker=Worker();worker.calls=[]
        service=NightlyNBARefresh(self.store,worker,now=lambda:self.now[0])
        return service,worker

    def status(self, target=DAY):
        self.store.set_checkpoint('nightly_nba_refresh',{'initialized':True,'handled_target_date':target,
            'pending_dates':[],'last_attempt':NOW.isoformat(),'last_tracking_attempt':NOW.isoformat()})

    def verified(self, document):
        document=copy.deepcopy(document)
        document.update(inventory_verified=True,inventory_verified_at=NOW.isoformat(),
            inventory_verification_source='https://example.com/independent-nba-inventory',
            inventory_game_ids=sorted(g['id'] for g in document['games']),
            inventory_off_day_verified=not document['games'])
        return document

    def empty(self, day=DAY):
        document=snapshot()
        document.update(date=day,games=[],player_games=[],expected_games=0)
        return document

    def final(self, day, home):
        document=self.empty(day)
        document.update(games=[{'id':canonical_game(day,home,'CLE'),'date':day,
                               'home_team':home,'away_team':'CLE','status':'final'}],expected_games=1)
        return document

    def upcoming(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO season_schedule (id,season,date,home_team,away_team,start_time,status) '
                       'VALUES (?,?,?,?,?,?,?)',('fixture-upcoming','2026-27','2026-10-30','NYK','PHI',
                                               '2026-10-30T23:30:00+00:00','scheduled'))

    def test_complete_day_is_rechecked_hourly_and_correction_updates_without_duplicate(self):
        import_daily_snapshot(self.store,snapshot(20),DAY,NOW);self.status()
        changed=snapshot(26);changed['published_at']=(NOW+dt.timedelta(hours=1)).isoformat()
        service,worker=self.service({DAY:changed})
        self.now[0]=NOW+dt.timedelta(minutes=59)
        asyncio.run(service.tick());self.assertEqual(worker.calls,[])
        self.now[0]=NOW+dt.timedelta(hours=1)
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/'+DAY+'.json'))
        with self.store.connect() as db:
            row=db.execute('SELECT pts FROM player_games WHERE player_id=?',(canonical_player('Tyrese Maxey'),)).fetchone()
        self.assertEqual(row['pts'],26)
        self.assertEqual(self.store.status()['counts']['player_games'],10)
        checked=self.store.get_checkpoint('daily_nba:'+DAY)
        self.assertEqual(checked['last_refresh_status'],'success')
        self.assertEqual(checked['last_checked_at'],self.now[0].isoformat())

    def test_unchanged_document_refreshes_check_time_not_source_publication_or_import_time(self):
        original=import_daily_snapshot(self.store,snapshot(),DAY,NOW)
        now=NOW+dt.timedelta(hours=1)
        again=import_daily_snapshot(self.store,snapshot(),DAY,now)
        self.assertTrue(again['unchanged'])
        self.assertEqual(again['publisher_retrieved_at'],original['publisher_retrieved_at'])
        self.assertEqual(again['imported_at'],original['imported_at'])
        self.assertEqual(again['last_checked_at'],now.isoformat())

    def test_latest_check_failure_preserves_imported_game_records_but_is_not_fresh_success(self):
        previous=import_daily_snapshot(self.store,snapshot(),DAY,NOW);self.status()
        service,worker=self.service({})
        self.now[0]=NOW+dt.timedelta(hours=1)
        asyncio.run(service.tick())
        checked=self.store.get_checkpoint('daily_nba:'+DAY)
        self.assertTrue(checked['complete'])
        self.assertEqual(checked['sha256'],previous['sha256'])
        self.assertEqual(checked['last_refresh_status'],'failed')
        self.assertEqual(checked['last_refresh_error'],'HTTPStatusError: HTTP 404')
        self.assertEqual(self.store.status()['counts']['player_games'],10)
        self.assertIn(DAY,self.store.get_checkpoint('nightly_nba_refresh')['pending_dates'])

    def test_final_inventory_records_teams_and_ids_without_claiming_full_league_coverage(self):
        report=import_daily_snapshot(self.store,snapshot(),DAY,NOW)
        game_id=snapshot()['games'][0]['id']
        self.assertEqual(report['final_game_ids'],[game_id])
        self.assertEqual(report['team_final_game_ids'],{'CLE':[game_id],'PHI':[game_id]})
        self.assertIn('not independently verified',report['inventory_scope'])
        self.assertFalse(report['inventory_verified'])

    def test_missing_target_also_rechecks_last_complete_day_without_fabricating_offday(self):
        older='2026-10-28'
        document=snapshot();document.update(date=older,games=[],player_games=[],expected_games=0)
        import_daily_snapshot(self.store,document,older,NOW)
        self.status();self.now[0]=NOW+dt.timedelta(hours=1)
        service,worker=self.service({older:document})
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/'+DAY+'.json'))
        self.assertTrue(worker.calls[1].endswith('/'+older+'.json'))
        old_check=self.store.get_checkpoint('daily_nba:'+older)
        self.assertEqual(old_check['last_checked_at'],self.now[0].isoformat())
        self.assertEqual(old_check['last_refresh_status'],'success')
        missing=self.store.get_checkpoint('daily_nba:'+DAY)
        self.assertFalse(missing['complete'])
        self.assertNotIn('expected_games',missing)
        self.assertIn(DAY,self.store.get_checkpoint('nightly_nba_refresh')['pending_dates'])

    def test_independent_inventory_matches_final_game_ids_and_preserves_its_source_time(self):
        document=self.verified(snapshot())
        report=import_daily_snapshot(self.store,document,DAY,NOW)
        self.assertTrue(report['inventory_verified'])
        self.assertEqual(report['inventory_game_ids'],report['final_game_ids'])
        self.assertEqual(report['inventory_verified_at'],NOW.isoformat())
        self.assertFalse(report['inventory_off_day_verified'])
        again=import_daily_snapshot(self.store,document,DAY,NOW+dt.timedelta(hours=1))
        self.assertEqual(again['inventory_verified_at'],NOW.isoformat())
        self.assertNotEqual(again['inventory_verified_at'],again['last_checked_at'])

    def test_inconsistent_inventory_and_bad_verification_dates_are_rejected_atomically(self):
        variants=[{'inventory_game_ids':[]}, {'inventory_game_ids':['invented-game']},
                  {'inventory_verified_at':'2026-10-30T15:30:00'},
                  {'inventory_verified_at':(NOW+dt.timedelta(hours=1)).isoformat()},
                  {'inventory_verification_source':'http://example.com/inventory'}]
        for change in variants:
            with self.subTest(change=change):
                document=self.verified(snapshot());document.update(change)
                with self.assertRaises(ValueError):import_daily_snapshot(self.store,document,DAY,NOW)
                self.assertEqual(self.store.status()['counts']['player_games'],0)
                self.assertFalse(self.store.get_checkpoint('daily_nba:'+DAY))

    def test_verified_zero_game_day_requires_explicit_offday_proof(self):
        document=self.verified(self.empty());document.pop('inventory_off_day_verified')
        with self.assertRaises(ValueError):import_daily_snapshot(self.store,document,DAY,NOW)
        document['inventory_off_day_verified']=True
        report=import_daily_snapshot(self.store,document,DAY,NOW)
        self.assertTrue(report['complete'])
        self.assertTrue(report['inventory_off_day_verified'])
        self.assertTrue(report['inventory_verified'])
        self.assertEqual(report['inventory_game_ids'],[])
        self.assertEqual(report['team_final_game_ids'],{})

    def test_verified_offday_still_rechecks_latest_real_game_publication(self):
        older='2026-10-28';game=self.final(older,'PHI')
        empty=self.verified(self.empty())
        import_daily_snapshot(self.store,game,older,NOW)
        import_daily_snapshot(self.store,empty,DAY,NOW)
        self.status();self.now[0]=NOW+dt.timedelta(hours=1)
        service,worker=self.service({DAY:empty,older:game})
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/'+DAY+'.json'))
        self.assertTrue(worker.calls[1].endswith('/'+older+'.json'))
        for day in (DAY,older):
            self.assertEqual(self.store.get_checkpoint('daily_nba:'+day)['last_checked_at'],self.now[0].isoformat())
        state=self.store.get_checkpoint('nightly_nba_refresh')
        self.assertTrue(state['target_inventory_verified'])
        self.assertEqual(state['target_inventory_checked_at'],NOW.isoformat())
        self.assertIn('Independent day inventory matched',state['completeness_scope'])

    def test_relevant_team_publications_fit_bounded_batch_then_backfill_is_not_starved(self):
        self.upcoming();older='2026-10-28';oldest='2026-10-27';backfill='2026-10-26'
        documents={DAY:self.verified(self.empty()),older:self.final(older,'PHI'),
                   oldest:self.final(oldest,'NYK'),backfill:self.empty(backfill)}
        for day in (older,oldest):
            import_daily_snapshot(self.store,documents[day],day,NOW)
            check=self.store.get_checkpoint('daily_nba:'+day)
            check['last_checked_at']=(NOW-dt.timedelta(days=1)).isoformat()
            self.store.set_checkpoint('daily_nba:'+day,check)
        self.status();state=self.store.get_checkpoint('nightly_nba_refresh')
        state['pending_dates']=[backfill];self.store.set_checkpoint('nightly_nba_refresh',state)
        self.now[0]=NOW+dt.timedelta(hours=1)
        service,worker=self.service(documents)
        asyncio.run(service.tick())
        first=[url.rsplit('/',1)[-1][:-5] for url in worker.calls if url.endswith('.json') and not url.endswith('existing_tracking.json')]
        self.assertEqual(set(first),{DAY,older,oldest})
        self.assertEqual(len(first),3)
        self.assertIn(backfill,self.store.get_checkpoint('nightly_nba_refresh')['pending_dates'])
        worker.calls.clear();self.now[0]+=dt.timedelta(hours=1)
        asyncio.run(service.tick())
        second=[url.rsplit('/',1)[-1][:-5] for url in worker.calls if url.endswith('.json') and not url.endswith('existing_tracking.json')]
        self.assertEqual(second[0],DAY)
        self.assertIn(backfill,second)
        self.assertLessEqual(len(second),3)
        self.assertTrue(self.store.get_checkpoint('daily_nba:'+backfill)['complete'])

    def test_older_publisher_file_remains_failed_and_queued_for_retry(self):
        document=snapshot();document['published_at']=(NOW-dt.timedelta(hours=1)).isoformat()
        accepted=import_daily_snapshot(self.store,snapshot(26),DAY,NOW)
        self.status();self.now[0]=NOW+dt.timedelta(hours=1)
        service,worker=self.service({DAY:document})
        asyncio.run(service.tick())
        check=self.store.get_checkpoint('daily_nba:'+DAY)
        self.assertEqual(check['sha256'],accepted['sha256'])
        self.assertEqual(check['last_refresh_status'],'failed')
        self.assertEqual(check['last_refresh_error'],'OlderPublicationIgnored')
        self.assertIn(DAY,self.store.get_checkpoint('nightly_nba_refresh')['pending_dates'])

    def test_failed_relevant_rechecks_rotate_without_starving_other_pending_days(self):
        self.upcoming();older='2026-10-28';oldest='2026-10-27';backfill='2026-10-26'
        for day,team in ((older,'PHI'),(oldest,'NYK')):
            import_daily_snapshot(self.store,self.final(day,team),day,NOW)
            check=self.store.get_checkpoint('daily_nba:'+day)
            check['last_checked_at']=(NOW-dt.timedelta(days=1)).isoformat()
            self.store.set_checkpoint('daily_nba:'+day,check)
        self.status();state=self.store.get_checkpoint('nightly_nba_refresh')
        state['pending_dates']=[backfill];self.store.set_checkpoint('nightly_nba_refresh',state)
        self.now[0]=NOW+dt.timedelta(hours=1)
        service,worker=self.service({DAY:self.empty(),backfill:self.empty(backfill)})
        asyncio.run(service.tick())
        self.assertEqual(self.store.get_checkpoint('daily_nba:'+older)['last_refresh_status'],'failed')
        self.assertEqual(self.store.get_checkpoint('daily_nba:'+oldest)['last_refresh_status'],'failed')
        self.now[0]+=dt.timedelta(hours=1);worker.calls.clear()
        asyncio.run(service.tick())
        self.assertTrue(self.store.get_checkpoint('daily_nba:'+backfill)['complete'])
        self.assertLessEqual(len([u for u in worker.calls if u.endswith('.json') and not u.endswith('existing_tracking.json')]),3)


if __name__=='__main__':unittest.main()
