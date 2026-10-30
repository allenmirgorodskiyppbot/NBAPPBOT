"""Morning refresh, source lag and canonical dataset-boundary regression tests."""
from __future__ import annotations

import asyncio
import copy
import csv
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from dataset_refresh import initialize_scope
from feeds import canonical_game, canonical_player
from master import MasterStore
from mirror_sources import existing_game_snapshots
from nightly_data import NightlyNBARefresh, import_daily_snapshot
from player_stats import published_average
from test_nightly_data import snapshot

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 30, 15, 30, tzinfo=UTC)  # 11:30 Eastern.
DAY = '2026-10-29'


class BoundaryFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.scope = initialize_scope(self.store)

    def empty_day(self, day, now=NOW):
        return {'schema_version':1, 'date':day, 'source':'espn',
                'published_at':now.isoformat(), 'expected_games':0, 'games':[],
                'player_games':[], 'complete':True, 'pending_game_ids':[], 'errors':[],
                'scope_revision':self.scope['revision'], 'upstream_full_game_validated':True,
                'source_data_through_date':day}

    def tracking(self, through='2026-10-28', now=NOW):
        return {'schema_version':1, 'scope_revision':self.scope['revision'], 'season':'2026-27',
                'last_attempt':now.isoformat(), 'records':[], 'coverage':[],
                'source_data_through_date':through}

    def worker(self, now, unavailable=()):
        owner = self
        class Worker:
            def __init__(self):
                self.calls = []
            async def request(self, url):
                self.calls.append(url)
                if url.endswith('/existing_tracking.json'):
                    return owner.tracking(now=now[0])
                day = url.rsplit('/',1)[-1][:-5]
                if day in unavailable:
                    response = httpx.Response(404, request=httpx.Request('GET',url))
                    response.raise_for_status()
                return owner.empty_day(day, now[0])
        return Worker()


class MorningBoundaryTests(BoundaryFixtures):
    def test_restart_after_missing_deadline_catches_up_previous_day_immediately(self):
        now = [NOW + dt.timedelta(hours=2)]
        worker = self.worker(now)
        # Prior running process missed the Oct30 morning deadline. A recent failed
        # attempt on another date must not postpone a newly due target.
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True, 'handled_target_date':'2026-10-28', 'pending_dates':[],
            'last_attempt':(now[0]-dt.timedelta(minutes=5)).isoformat()})
        service = NightlyNBARefresh(self.store, worker, now=lambda:now[0])
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/2026-10-29.json'))
        state = self.store.get_checkpoint('nightly_nba_refresh')
        self.assertEqual(state['target_game_date'], DAY)
        self.assertEqual(state['handled_target_date'], DAY)
        self.assertEqual(state['daily_time'],'11:30')
        self.assertEqual(state['phase'],'updated')
        self.assertEqual(state['source_data_through_date'], DAY)
        self.assertEqual(state['next_daily_run'],'2026-10-31T11:30:00-04:00')

    def test_target_is_prioritized_before_backfill_on_initial_due_pass(self):
        now = [NOW]
        worker = self.worker(now)
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True, 'handled_target_date':'2026-10-28',
            'pending_dates':['2026-10-22','2026-10-23','2026-10-24','2026-10-25']})
        asyncio.run(NightlyNBARefresh(self.store,worker,now=lambda:now[0]).tick())
        day_calls = [url for url in worker.calls if not url.endswith('/existing_tracking.json')]
        self.assertTrue(day_calls[0].endswith('/'+DAY+'.json'))
        self.assertEqual(len(day_calls),3)

    def test_missing_required_day_stays_first_on_every_hourly_retry(self):
        now = [NOW]
        worker = self.worker(now, unavailable={DAY})
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True, 'handled_target_date':'2026-10-28',
            'pending_dates':['2026-10-22','2026-10-23','2026-10-24','2026-10-25','2026-10-26']})
        service = NightlyNBARefresh(self.store,worker,now=lambda:now[0])
        asyncio.run(service.tick())
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh')['phase'],'waiting_for_publication')
        before = len(worker.calls)
        now[0] += dt.timedelta(minutes=59,seconds=59)
        asyncio.run(service.tick())
        self.assertEqual(len(worker.calls),before)
        now[0] += dt.timedelta(seconds=2)
        asyncio.run(service.tick())
        new_calls = [url for url in worker.calls[before:] if not url.endswith('/existing_tracking.json')]
        self.assertTrue(new_calls[0].endswith('/'+DAY+'.json'),new_calls)
        status = self.store.get_checkpoint('nightly_nba_refresh')
        self.assertEqual(status['phase'],'waiting_for_publication')
        self.assertIn(DAY,status['pending_dates'])
        self.assertEqual(status['retry_seconds'],3600)

    def test_before_deadline_does_not_start_todays_previous_day_update(self):
        now = [NOW-dt.timedelta(seconds=1)]
        worker = self.worker(now)
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True, 'handled_target_date':'2026-10-28','pending_dates':[]})
        service = NightlyNBARefresh(self.store,worker,now=lambda:now[0])
        asyncio.run(service.tick())
        self.assertEqual(worker.calls,[])
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh')['target_game_date'],'2026-10-28')
        now[0] = NOW
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/'+DAY+'.json'))

    def test_winter_refresh_is_1130_eastern_after_dst(self):
        now = [dt.datetime(2026,11,3,16,29,59,tzinfo=UTC)]
        worker = self.worker(now)
        self.store.set_checkpoint('nightly_nba_refresh',{
            'initialized':True,'handled_target_date':'2026-11-01','pending_dates':[]})
        service = NightlyNBARefresh(self.store,worker,now=lambda:now[0])
        asyncio.run(service.tick())
        self.assertEqual(worker.calls,[])
        now[0] += dt.timedelta(seconds=1)
        asyncio.run(service.tick())
        self.assertTrue(worker.calls[0].endswith('/2026-11-02.json'))
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh')['next_daily_run'],
                         '2026-11-04T11:30:00-05:00')


class ImportBoundaryTests(BoundaryFixtures):
    def test_canonical_id_team_opponent_season_and_phase_errors_are_atomic(self):
        mutations = [
            {'player_id':canonical_player('Paul George')},
            {'team':'NYK'}, {'opponent':'ATL'}, {'team':'PHI','opponent':'PHI'},
            {'season':'2025-26'}, {'season_type':'Playoffs'},
            {'player':'Unrequested New Player','player_id':'name:newplayer'},
        ]
        before = self.store.status()['counts']
        for change in mutations:
            with self.subTest(change=change):
                document = snapshot()
                # Mutate the last row so a non-atomic importer would have already
                # written the preceding nine valid observations.
                document['player_games'][-1].update(change)
                with self.assertRaises(ValueError):
                    import_daily_snapshot(self.store,document,DAY,NOW)
                self.assertEqual(self.store.status()['counts'],before)
                self.assertFalse(self.store.get_checkpoint('daily_nba:'+DAY))

    def test_complete_day_correction_is_idempotent_for_canonical_player(self):
        document = snapshot(24)
        import_daily_snapshot(self.store,document,DAY,NOW)
        repeated = import_daily_snapshot(self.store,document,DAY,NOW)
        self.assertTrue(repeated['unchanged'])
        changed = copy.deepcopy(document)
        changed['published_at'] = (NOW+dt.timedelta(minutes=1)).isoformat()
        changed['player_games'][0]['pts'] = 25
        changed['games'][0]['home_score'] += 1
        import_daily_snapshot(self.store,changed,DAY,NOW+dt.timedelta(minutes=1))
        with self.store.connect() as db:
            rows = db.execute("SELECT pts FROM player_games WHERE player_id=?",(canonical_player('Tyrese Maxey'),)).fetchall()
        self.assertEqual([row['pts'] for row in rows],[25])


class PublishedAggregateBoundaryTests(BoundaryFixtures):
    def add_observation(self, day='2026-10-29', points=35):
        game=canonical_game(day,'PHI','CLE')
        self.store.upsert_game({'id':game,'date':day,'home_team':'PHI','away_team':'CLE','status':'final'})
        self.store.upsert_player_game({'game_id':game,'player':'Tyrese Maxey','team':'PHI','opponent':'CLE',
            'date':day,'season':'2026-27','season_type':'Regular Season','appearance_state':'Played',
            'pts':points,'minutes':35,'reb':4,'ast':5})

    def add_control(self, covered='2026-10-28', gp=20, points=500):
        record={'entity':'Tyrese Maxey','player_id':canonical_player('Tyrese Maxey'),
            'category':'Player totals','season':'2026-27','season_type':'Regular Season',
            'published_at':NOW.isoformat(),'data_through_date':covered,
            'gp':gp,'fields':{'pts':points,'reb':80,'ast':100,'minutes':700},
            'source':'https://example.com/published-controls'}
        with self.store.connect() as db:
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                       ('2026-27','Tyrese Maxey','Player totals',json.dumps(record)))

    def test_later_retrieval_does_not_allow_stale_control_to_hide_newer_observed_game(self):
        self.add_observation()
        self.add_control(covered='2026-10-28')
        with self.store.connect() as db:
            row=db.execute("SELECT ppg,games_played,through_date FROM season_averages WHERE player='Tyrese Maxey'").fetchone()
        self.assertEqual(tuple(row),(35,1,'2026-10-29'))
        self.assertIsNone(published_average(self.store,'Tyrese Maxey',['pts'],'2026-27'))

    def test_control_with_fewer_games_cannot_hide_observed_complete_games(self):
        self.add_observation('2026-10-27',20)
        self.add_observation('2026-10-28',30)
        self.add_control(covered='2026-10-29',gp=1,points=99)
        with self.store.connect() as db:
            row=db.execute("SELECT ppg,games_played FROM season_averages WHERE player='Tyrese Maxey'").fetchone()
        self.assertEqual(tuple(row),(25,2))
        self.assertIsNone(published_average(self.store,'Tyrese Maxey',['pts'],'2026-27'))

    def test_published_covered_date_is_not_replaced_by_newer_retrieval_date(self):
        self.add_observation('2026-10-27',35)
        self.add_control(covered='2026-10-28',gp=20,points=500)
        with self.store.connect() as db:
            row=db.execute("SELECT ppg,games_played,through_date FROM season_averages WHERE player='Tyrese Maxey'").fetchone()
        self.assertEqual(tuple(row),(25,20,'2026-10-28'))
        answer=published_average(self.store,'Tyrese Maxey',['pts'],'2026-27')
        self.assertEqual(answer['data']['through_date'],'2026-10-28')
        self.assertEqual(answer['data']['source_published_at'],NOW.isoformat())
        self.assertIn('Covered game date: 2026-10-28',answer['answer'])
        self.assertIn('Source publication: '+NOW.isoformat(),answer['answer'])


class MirrorScopeBoundaryTests(BoundaryFixtures):
    def csv_fixture(self, phase='2'):
        fields=['season_type','game_date','home_away','team_abbreviation','opponent_team_abbreviation',
                'team_score','opponent_team_score','game_id','game_date_time','athlete_display_name',
                'athlete_id','did_not_play','minutes','points','season','rebounds','assists']
        names={
            'PHI':['Tyrese Maxey','Paul George','Joel Embiid','VJ Edgecombe','Adem Bona','Unrequested Alpha'],
            'CLE':['James Harden','Donovan Mitchell','Darius Garland','Evan Mobley','Jarrett Allen','Unrequested Beta']}
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader()
        for team,players in names.items():
            for index,name in enumerate(players):
                writer.writerow({'season_type':phase,'game_date':DAY,'home_away':'home' if team=='PHI' else 'away',
                    'team_abbreviation':team,'opponent_team_abbreviation':'CLE' if team=='PHI' else 'PHI',
                    'team_score':60,'opponent_team_score':60,'game_id':'fixture-game',
                    'game_date_time':'2026-10-29T23:00:00Z','athlete_display_name':name,
                    'athlete_id':str(index),'did_not_play':'false','minutes':30,'points':10,
                    'season':'2027','rebounds':3,'assists':2})
        return buffer.getvalue().encode()

    def test_full_source_validation_precedes_existing_player_filter(self):
        days=existing_game_snapshots(self.csv_fixture(),self.scope,NOW.isoformat(),
                                     'https://example.com/published-regular-season.csv')
        self.assertEqual(list(days),[DAY])
        document=days[DAY]
        self.assertTrue(document['complete'])
        self.assertEqual(document['expected_games'],1)
        self.assertEqual(len(document['player_games']),10)
        allowed={p['player'] for p in self.scope['players']}
        self.assertTrue(all(row['player'] in allowed for row in document['player_games']))
        self.assertNotIn('Unrequested',json.dumps(document['player_games']))
        self.assertEqual(document['source_data_through_date'],DAY)
        import_daily_snapshot(self.store,document,DAY,NOW)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0],10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM player_games WHERE player LIKE 'Unrequested%'").fetchone()[0],0)

    def test_preseason_source_rows_cannot_enter_regular_season_extract(self):
        self.assertEqual(existing_game_snapshots(self.csv_fixture(phase='1'),self.scope,NOW.isoformat(),
                         'https://example.com/published-preseason.csv'),{})


if __name__=='__main__':
    unittest.main()
