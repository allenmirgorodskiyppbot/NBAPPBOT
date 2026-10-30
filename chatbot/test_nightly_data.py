"""Existing-player refresh, late games, corrections and population boundaries."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from dataset_refresh import initialize_scope, load_scope, select_existing_rows
from feeds import canonical_game,canonical_player,parse_boxscore
from master import MasterStore
from nba_daily_export import PublicCollector,validate_final
from nightly_data import import_daily_snapshot,NightlyNBARefresh

UTC=dt.timezone.utc
NOW=dt.datetime(2026,10,30,15,30,tzinfo=UTC)  # 11:30 Oct 30 Eastern; previous day's games.
DAY='2026-10-29'


def snapshot(points=20):
    game={'id':canonical_game(DAY,'PHI','CLE'),'date':DAY,'home_team':'PHI','away_team':'CLE',
          'home_score':50,'away_score':50,'status':'final','provider_game_id':'fixture-game','source':'https://example.com/source'}
    names={'PHI':['Tyrese Maxey','Paul George','Joel Embiid','VJ Edgecombe','Adem Bona'],
           'CLE':['James Harden','Donovan Mitchell','Darius Garland','Evan Mobley','Jarrett Allen']}
    rows=[]
    for team,players in names.items():
        for name in players:
            rows.append({'game_id':game['id'],'player_id':canonical_player(name),'player':name,
                'team':team,'opponent':'CLE' if team=='PHI' else 'PHI','date':DAY,'season':'2026-27',
                'season_type':'Regular Season','appearance_state':'Played','minutes':30,'pts':10,
                'reb':3,'ast':2,'fgm':4,'fga':10,'tpm':1,'tpa':3,'ftm':1,'fta':2,'source':'https://example.com/source'})
    rows[0]['pts']=points
    game['home_score']=40+points
    validate_final(game,rows)
    scope=load_scope()
    return {'schema_version':1,'date':DAY,'source':'espn','published_at':NOW.isoformat(),
        'games':[game],'expected_games':1,'player_games':select_existing_rows(rows,scope),
        'scope_revision':scope['revision'],'upstream_full_game_validated':True,
        'complete':True,'pending_game_ids':[],'errors':[]}


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'master.sqlite')
        initialize_scope(self.store)

    def test_non_knicks_game_updates_maxey_broad_stats_without_changing_knicks_split(self):
        old_day='2026-10-20';game_id=canonical_game(old_day,'NYK','PHI')
        self.store.upsert_game({'id':game_id,'date':old_day,'home_team':'NYK','away_team':'PHI','status':'final'})
        self.store.upsert_player_game({'game_id':game_id,'player':'Tyrese Maxey','team':'PHI','opponent':'NYK',
            'date':old_day,'season':'2026-27','season_type':'Regular Season','appearance_state':'Played','pts':30,'minutes':35})
        import_daily_snapshot(self.store,snapshot(20),DAY,NOW)
        with self.store.connect() as db:
            average=db.execute("SELECT ppg,games_played FROM season_averages WHERE player='Tyrese Maxey'").fetchone()
            self.assertEqual(tuple(average),(25,2))
            versus=db.execute("SELECT AVG(pts),COUNT(*) FROM player_games WHERE player='Tyrese Maxey' AND opponent='NYK'").fetchone()
            self.assertEqual(tuple(versus),(30,1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM player_games WHERE player='James Harden'").fetchone()[0],1)
        self.assertEqual(self.store.status()['counts']['entries'],0)

    def test_repeat_and_correction_do_not_duplicate_games_or_average_averages(self):
        first=import_daily_snapshot(self.store,snapshot(20),DAY,NOW)
        again=import_daily_snapshot(self.store,snapshot(20),DAY,NOW)
        self.assertTrue(again['unchanged'])
        changed=snapshot(26);changed['published_at']=(NOW+dt.timedelta(minutes=1)).isoformat()
        import_daily_snapshot(self.store,changed,DAY,NOW+dt.timedelta(minutes=1))
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT ppg,games_played FROM season_averages WHERE player='Tyrese Maxey'").fetchone()),(26,1))
        self.assertEqual(self.store.status()['counts']['player_games'],10)

    def test_new_unrequested_player_is_rejected_atomically(self):
        data=snapshot();data['player_games'][0]['player']='Unrequested New Player'
        data['player_games'][0]['player_id']='name:newplayer'
        before=self.store.status()['counts']
        with self.assertRaises(ValueError):import_daily_snapshot(self.store,data,DAY,NOW)
        self.assertEqual(self.store.status()['counts'],before)

    def test_partial_day_is_saved_as_pending_without_zeroing_live_players(self):
        data=snapshot();data['games'][0]['status']='live';data['player_games']=[]
        data.update(complete=False,pending_game_ids=[data['games'][0]['id']])
        result=import_daily_snapshot(self.store,data,DAY,NOW)
        self.assertFalse(result['complete']);self.assertEqual(self.store.status()['counts']['player_games'],0)

    def test_real_espn_labels_take_precedence_over_long_names(self):
        data=snapshot();game=data['games'][0]
        summary={'boxscore':{'players':[{'team':{'abbreviation':'PHI'},'statistics':[{
            'names':['minutes','points'],'labels':['MIN','PTS'],'athletes':[{
                'athlete':{'displayName':'Tyrese Maxey','id':'1'},'stats':['35','27'],'didNotPlay':False}]}]}]}}
        rows=parse_boxscore(summary,game)
        self.assertEqual(rows[0]['pts'],27);self.assertEqual(rows[0]['minutes'],35)

    def test_invalid_numeric_observation_rolls_back_the_entire_day(self):
        data=snapshot();data['player_games'][-1]['reb']=-1
        with self.assertRaises(ValueError):import_daily_snapshot(self.store,data,DAY,NOW)
        self.assertEqual(self.store.status()['counts']['player_games'],0)
        self.assertEqual(self.store.status()['counts']['games'],0)

    def test_morning_previous_day_refresh_and_missing_publication_retries(self):
        class Worker:
            async def request(inner,url):
                day=url.rsplit('/',1)[-1][:-5]
                data=snapshot();data['date']=day
                data['games']=[];data['player_games']=[];data['expected_games']=0
                if day==DAY:
                    data['expected_games']=1;data['pending_game_ids']=['pending'];data['complete']=False
                return data
        now=[NOW-dt.timedelta(minutes=1)]
        service=NightlyNBARefresh(self.store,Worker(),now=lambda:now[0])
        self.store.set_checkpoint('nightly_nba_refresh',{'initialized':True,'pending_dates':[],'handled_target_date':'2026-10-28'})
        asyncio.run(service.tick())
        self.assertEqual(self.store.get_checkpoint('nightly_nba_refresh')['handled_target_date'],'2026-10-28')
        now[0]=NOW;asyncio.run(service.tick())
        status=self.store.get_checkpoint('nightly_nba_refresh')
        self.assertEqual(status['handled_target_date'],DAY);self.assertIn(DAY,status['pending_dates'])
        self.assertEqual(status['daily_time'],'11:30')


if __name__=='__main__':unittest.main()
