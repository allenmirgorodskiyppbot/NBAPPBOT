"""A relay preserves source check time, source identity, and failure boundaries."""
import asyncio
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from game_schedule_check import (RELAY_URL, _parse_relay, _parse_nba_today,
                                 refresh_game_schedule, SUCCESS_CACHE_SECONDS)
from nba_cdn import SCOREBOARD_URL
from nba_daily_export import PublicCollector, verified_nba_day_inventory
import test_pregame_schedule as schedule_fixture
from test_pregame_schedule import NOW, DAY, TIP, ID, scoreboard


def relay(checked=NOW,document=None):
    document=scoreboard() if document is None else document
    return {'schema_version':1,'date':DAY,'source_checked_at':checked.isoformat(),
        'source_url':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261104',
        'document':document,'source_content_sha256':hashlib.sha256(
            json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def nba_document(day=DAY):
    return {'scoreboard':{'gameDate':day,'games':[{'gameId':'0022600099','gameTimeUTC':TIP,
        'gameStatus':1,'homeTeam':{'teamTricode':'NYK'},'awayTeam':{'teamTricode':'CHI'}}]}}


class RelayParsingTests(unittest.TestCase):
    def test_relay_uses_upstream_timestamp_and_matching_inventory(self):
        games,checked,source=_parse_relay(relay(),DAY,NOW+dt.timedelta(minutes=9))
        self.assertEqual(checked,NOW);self.assertEqual(list(games),[ID])
        self.assertIn('espn.com',source)

    def test_stale_future_wrong_day_source_and_content_rejected(self):
        variants=[]
        variants.append(relay(NOW-dt.timedelta(seconds=SUCCESS_CACHE_SECONDS)))
        variants.append(relay(NOW+dt.timedelta(seconds=1)))
        doc=relay();doc['date']='2026-11-03';variants.append(doc)
        doc=relay();doc['source_url']='https://example.com/scoreboard';variants.append(doc)
        doc=relay();doc['document']['events'][0]['date']='2026-11-05T02:00:00Z';variants.append(doc)
        for doc in variants:
            with self.subTest(doc=doc),self.assertRaises(ValueError):_parse_relay(doc,DAY,NOW)

    def test_official_live_scoreboard_validates_day_phase_and_identity(self):
        self.assertEqual(list(_parse_nba_today(nba_document(),DAY)),[ID])
        bad=nba_document();bad['scoreboard']['games'][0]['gameId']='0062600001'
        for document in (bad,nba_document('2026-11-03')):
            with self.assertRaises(ValueError):_parse_nba_today(document,DAY)

    def test_official_historical_calendar_requires_current_season_and_date_bounds(self):
        doc={'leagueSchedule':{'seasonYear':'2026-27','gameDates':[
            {'gameDate':'2026-11-02','games':[]},{'gameDate':DAY,'games':nba_document()['scoreboard']['games']},
            {'gameDate':'2026-11-06','games':[]}]}}
        now=NOW+dt.timedelta(days=1)
        inventory=verified_nba_day_inventory(doc,DAY,now)
        self.assertTrue(inventory['inventory_verified']);self.assertEqual(inventory['inventory_game_ids'],[ID])
        offday=verified_nba_day_inventory(doc,'2026-11-03',now)
        self.assertTrue(offday['inventory_off_day_verified'])
        for day in ('2026-11-01','2026-11-05'):
            with self.assertRaises(ValueError):verified_nba_day_inventory(doc,day,now)


class ScheduleFallbackIntegrationTests(unittest.TestCase):
    setUp=schedule_fixture.PregameScheduleTests.setUp
    check=schedule_fixture.PregameScheduleTests.check
    def test_unavailable_origin_uses_fresh_relay_without_redating_it(self):
        checked=NOW-dt.timedelta(minutes=8);calls=[]
        async def request(url):
            calls.append(url)
            if url==RELAY_URL:return relay(checked)
            raise httpx.ReadTimeout('fixture')
        self.worker.request=request
        result=self.check()
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['checked_at'],checked.isoformat())
        self.assertEqual(result['last_success_at'],checked.isoformat())
        self.assertEqual(result['relay_url'],RELAY_URL)
        self.assertEqual(len(result['attempts']),2)
        self.assertEqual(len(calls),3)
        self.assertEqual(self.store.status()['counts']['player_games'],0)

    def test_old_relay_cannot_clear_origin_failure(self):
        async def request(url):
            if url==RELAY_URL:return relay(NOW-dt.timedelta(minutes=15))
            raise httpx.ReadTimeout('fixture')
        self.worker.request=request
        result=self.check()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['attempts']),3)
        self.assertEqual(self.store.status()['counts']['games'],0)

    def test_official_fallback_does_not_request_relay_after_success(self):
        calls=[]
        async def request(url):
            calls.append(url)
            if url==SCOREBOARD_URL:return nba_document()
            raise httpx.ReadTimeout('fixture')
        self.worker.request=request
        result=self.check()
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['source'],SCOREBOARD_URL)
        self.assertEqual(len(calls),2)


class RelayCollectorTests(unittest.TestCase):
    def test_collector_preserves_last_good_file_on_failed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=PublicCollector(None,NOW)
            async def good(url):return scoreboard()
            collector.request=good
            result=asyncio.run(collector.publish_current_schedule(directory))
            self.assertEqual(result['status'],'success')
            path=Path(directory)/'schedule_today.json';before=path.read_bytes()
            _parse_relay(json.loads(before),DAY,NOW)
            async def bad(url):raise httpx.ReadTimeout('fixture')
            collector.request=bad
            result=asyncio.run(collector.publish_current_schedule(directory))
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(path.read_bytes(),before)
