"""Large source validation permits other writers; final inserts remain atomic."""
import contextlib
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import history_backfill as history
from master import MasterStore
import test_history_backfill as fixtures


class ConnectionProxy:
    def __init__(self,connection,owner):self.connection,self.owner=connection,owner
    def __getattr__(self,name):return getattr(self.connection,name)
    def execute(self,sql,*args):
        if sql=='BEGIN IMMEDIATE' and self.owner.before_write:
            callback=self.owner.before_write;self.owner.before_write=None;callback()
        return self.connection.execute(sql,*args)


class HookStore(MasterStore):
    def __init__(self,path):self.before_write=None;super().__init__(path)
    @contextlib.contextmanager
    def connect(self):
        with super().connect() as db:yield ConnectionProxy(db,self)


class HistoryPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=HookStore(self.root/'master.sqlite')
        self.games,self.players,_=fixtures.normalized()
        self.events=[{'kind':'game','data':self.games[0]},{'kind':'player_game','data':self.players[0]}]

    def bundle(self,events=None,parts=None):
        groups=parts if parts is not None else [events if events is not None else self.events]
        metadata=[]
        for number,records in enumerate(groups):
            content=gzip.compress(('\n'.join(json.dumps(row) for row in records)+'\n').encode())
            name=f'part-{number}.jsonl.gz';(self.root/name).write_bytes(content)
            metadata.append({'file':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'records':len(records)})
        document={'schema_version':1,'format':'nba-history-jsonl-gzip-v1','source_data_through_date':'2026-11-02',
                  'players':[fixtures.identity()],'parts':metadata}
        path=self.root/'manifest.json';path.write_text(json.dumps(document));return path

    def counts(self):
        with self.store.connect() as db:return [db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
            for name in ('games','player_games','raw_snapshots')]

    def test_real_competing_writer_commits_while_source_validation_is_paused(self):
        manifest=self.bundle();entered=threading.Event();release=threading.Event();errors=[]
        original=history._validate_bundle_record
        def validate(*args):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):raise RuntimeError('fixture release not received')
            return original(*args)
        def run():
            try:history.import_history_bundle(self.store,manifest)
            except BaseException as error:errors.append(error)
        with patch('history_backfill._validate_bundle_record',side_effect=validate):
            thread=threading.Thread(target=run);thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with sqlite3.connect(self.store.path,timeout=.05) as db:
                    db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?)',('telegram-poll-fixture','{}'))
            finally:release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.counts()[:2],[1,1])

    def test_late_invalid_record_never_starts_master_writer_transaction(self):
        events=copy.deepcopy(self.events);events[-1]['data']['espn_player_id']='999'
        started=[];self.store.before_write=lambda:started.append(True)
        with self.assertRaisesRegex(ValueError,'identity'):
            history.import_history_bundle(self.store,self.bundle(events))
        self.assertEqual(started,[]);self.assertEqual(self.counts(),[0,0,0])

    def test_concurrent_live_row_wins_and_its_conflict_is_reported(self):
        manifest=self.bundle()
        def live():
            other=MasterStore(self.store.path);other.upsert_game(self.games[0])
            other.upsert_player_game({**self.players[0],'pts':99})
        self.store.before_write=live
        result=history.import_history_bundle(self.store,manifest)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT pts FROM player_games').fetchone()[0],99)
        self.assertEqual(result['player_game_conflict_pts'],1)

    def test_concurrent_identity_change_even_in_fully_overlapping_game_aborts_marker(self):
        self.store.upsert_game(self.games[0]);self.store.upsert_player_game(self.players[0])
        manifest=self.bundle();before=self.counts()
        def change():
            other=MasterStore(self.store.path)
            with other.connect() as db:db.execute('UPDATE games SET home_team=?',('BOS',))
        self.store.before_write=change
        with self.assertRaisesRegex(ValueError,'participants'):
            history.import_history_bundle(self.store,manifest)
        key='career_history_bundle:'+hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertFalse(self.store.get_checkpoint(key));self.assertEqual(self.counts(),before)

    def test_concurrent_deletion_of_old_key_is_restored_from_validated_disk_plan(self):
        self.store.upsert_game(self.games[0]);self.store.upsert_player_game(self.players[0])
        def remove():
            other=MasterStore(self.store.path)
            with other.connect() as db:db.execute('DELETE FROM player_games')
        self.store.before_write=remove
        result=history.import_history_bundle(self.store,self.bundle())
        self.assertEqual(result['player_game_inserted'],1);self.assertEqual(self.counts()[:2],[1,1])

    def test_duplicate_late_part_and_orphan_preserve_master_atomically(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            history.import_history_bundle(self.store,self.bundle(parts=[self.events,[self.events[1]]]))
        self.assertEqual(self.counts(),[0,0,0])
        with self.assertRaisesRegex(ValueError,'orphan'):
            history.import_history_bundle(self.store,self.bundle(events=[self.events[1]]))
        self.assertEqual(self.counts(),[0,0,0])

    def test_player_part_before_game_part_still_commits_together(self):
        result=history.import_history_bundle(self.store,self.bundle(parts=[[self.events[1]],[self.events[0]]]))
        self.assertEqual(result['game_inserted'],1);self.assertEqual(result['player_game_inserted'],1)
        self.assertEqual(self.counts()[:2],[1,1])

    def test_write_failure_rolls_back_new_games_players_snapshots_and_marker(self):
        manifest=self.bundle();original=self.store._snapshot
        def fail(db,kind,key,row):
            if kind=='player_games':raise RuntimeError('synthetic final write failure')
            return original(db,kind,key,row)
        with patch.object(self.store,'_snapshot',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'final write'):
                history.import_history_bundle(self.store,manifest)
        self.assertEqual(self.counts(),[0,0,0])
        key='career_history_bundle:'+hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertFalse(self.store.get_checkpoint(key))

    def test_concurrent_same_manifest_completion_is_reused_under_writer_lease(self):
        manifest=self.bundle()
        self.store.before_write=lambda:history.import_history_bundle(MasterStore(self.store.path),manifest)
        result=history.import_history_bundle(self.store,manifest)
        self.assertTrue(result['already_imported'])
        self.assertEqual(self.counts()[:2],[1,1])

    def test_expanded_record_and_inventory_bounds_checked_before_master_write(self):
        for key,value in [('MAX_BUNDLE_RECORDS',1),('MAX_BUNDLE_EXPANDED_BYTES',1),('MAX_RECORD_CHARS',10)]:
            with self.subTest(key=key),patch('history_backfill.'+key,value):
                with self.assertRaises(ValueError):history.import_history_bundle(self.store,self.bundle())
            self.assertEqual(self.counts(),[0,0,0])


if __name__=='__main__':unittest.main()
