"""Current event verification recovers without promoting a saved calendar."""
import copy
import datetime as dt
import hashlib
import json
import unittest

import httpx

from feeds import ESPN, FeedWorker, canonical_game
from game_schedule_check import CHECKPOINT, RELAY_URL, refresh_game_schedule
from nba_cdn import SCOREBOARD_URL
from pregame_freshness import _current_schedule_check
import test_pregame_schedule as fixture


def nba_board(*pairs):
    return {'scoreboard':{'gameDate':fixture.DAY, 'games':[
        {'gameId':'00226001'+str(index).zfill(2), 'gameTimeUTC':fixture.TIP,
         'gameStatus':1, 'homeTeam':{'teamTricode':home}, 'awayTeam':{'teamTricode':away}}
        for index,(home,away) in enumerate(pairs)]}}


def relay(document, checked):
    return {'schema_version':1, 'date':fixture.DAY, 'source_checked_at':checked.isoformat(),
            'source_url':ESPN+'scoreboard?dates='+fixture.DAY.replace('-',''),
            'document':document, 'source_content_sha256':hashlib.sha256(
                json.dumps(document,sort_keys=True,separators=(',',':')).encode()).hexdigest()}


class ScheduleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.PregameScheduleTests.setUp

    def extra(self, home='LAL', away='GSW'):
        identity=canonical_game(fixture.DAY,home,away)
        self.store.upsert_game({'id':identity, 'date':fixture.DAY, 'home_team':home,
            'away_team':away, 'status':'scheduled', 'start_time':fixture.TIP,
            'source':'isolated saved fixture'})
        return identity

    async def check(self, now=fixture.NOW):
        return await refresh_game_schedule(self.store,self.worker,now=now)

    async def test_failed_sources_are_rechecked_after_sixty_seconds_not_fifteen_minutes(self):
        async def unavailable(url):
            self.calls.append(url)
            raise httpx.ReadTimeout('fixture')
        self.worker.request=unavailable
        first=await self.check()
        self.assertEqual(first['status'],'failed')
        self.assertEqual(len(self.calls),3)
        async def recovered(url):
            self.calls.append(url)
            return fixture.scoreboard()
        self.worker.request=recovered
        cached=await self.check(fixture.NOW+dt.timedelta(seconds=59))
        self.assertEqual(cached['status'],'failed');self.assertEqual(len(self.calls),3)
        recovered=await self.check(fixture.NOW+dt.timedelta(seconds=60))
        self.assertEqual(recovered['status'],'success');self.assertEqual(len(self.calls),4)

    async def test_missing_primary_events_try_a_complete_independent_backup(self):
        other=self.extra()
        async def request(url):
            self.calls.append(url)
            if url==SCOREBOARD_URL:return nba_board(('NYK','CHI'),('LAL','GSW'))
            if url==RELAY_URL:raise AssertionError('Complete backup needs no relay.')
            return fixture.scoreboard()
        self.worker.request=request
        report=await self.check()
        self.assertEqual(report['status'],'success');self.assertEqual(report['source'],SCOREBOARD_URL)
        self.assertEqual(len(self.calls),2)
        self.assertEqual({game['id'] for game in report['games']},{fixture.ID,other})
        self.assertEqual(report['attempts'][0]['error'],'IncompleteInventory')
        self.assertEqual(self.store.status()['counts']['player_games'],0)

    async def test_best_partial_is_one_source_not_a_merged_invented_inventory(self):
        other=self.extra();missing=self.extra('BOS','MIA')
        checked=fixture.NOW-dt.timedelta(minutes=7)
        async def request(url):
            self.calls.append(url)
            if url==SCOREBOARD_URL:return nba_board(('NYK','CHI'),('LAL','GSW'))
            if url==RELAY_URL:return relay(fixture.scoreboard(fixture.event('BOS','MIA')),checked)
            return fixture.scoreboard(fixture.event('LAL','GSW'))
        self.worker.request=request
        report=await self.check()
        self.assertEqual(report['status'],'partial');self.assertEqual(len(self.calls),3)
        self.assertEqual(report['source'],SCOREBOARD_URL)
        self.assertEqual({game['id'] for game in report['games']},{fixture.ID,other})
        self.assertEqual(report['missing_game_ids'],[missing])
        self.assertTrue(all(game['source']==SCOREBOARD_URL for game in report['games']))

    async def partial(self):
        async def request(url):
            self.calls.append(url)
            if url==SCOREBOARD_URL or url==RELAY_URL:raise httpx.ReadTimeout('fixture')
            return fixture.scoreboard()
        self.worker.request=request
        return await self.check()

    async def test_missing_unrelated_game_does_not_invalidate_the_matched_matchup(self):
        missing=self.extra();report=await self.partial()
        self.assertEqual(report['status'],'partial');self.assertEqual(report['missing_game_ids'],[missing])
        for team in ('NYK','CHI'):
            detail,issue=_current_schedule_check(self.store,fixture.DAY,team,fixture.NOW)
            self.assertIsNone(issue);self.assertTrue(detail['game_verified'])
            self.assertEqual(detail['status'],'partial')
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'LAL',fixture.NOW)
        self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])

    async def test_conflict_in_the_requested_game_still_blocks_it(self):
        self.extra()
        self.store.upsert_game({'id':fixture.ID,'date':fixture.DAY,'home_team':'NYK',
            'away_team':'CHI','status':'live','start_time':fixture.TIP})
        report=await self.partial()
        self.assertIn(fixture.ID,report['conflicting_game_ids'])
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW)
        self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])
        self.assertEqual(self.store.games(fixture.DAY)[0]['status'],'live')

    async def test_a_missing_alternative_saved_game_for_the_same_team_is_not_ignored(self):
        self.extra('NYK','BOS');await self.partial()
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW)
        self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])

    async def test_partial_relay_preserves_source_time_and_uses_attempt_time_only_for_backoff(self):
        self.extra();checked=fixture.NOW-dt.timedelta(minutes=8)
        async def request(url):
            self.calls.append(url)
            if url==RELAY_URL:return relay(fixture.scoreboard(),checked)
            raise httpx.ReadTimeout('fixture')
        self.worker.request=request
        first=await self.check()
        self.assertEqual(first['status'],'partial');self.assertEqual(first['checked_at'],checked.isoformat())
        self.assertEqual(first['attempted_at'],fixture.NOW.isoformat())
        self.assertEqual(first['relay_url'],RELAY_URL)
        self.assertEqual(len(self.calls),3)
        await self.check(fixture.NOW+dt.timedelta(seconds=59));self.assertEqual(len(self.calls),3)
        second=await self.check(fixture.NOW+dt.timedelta(seconds=60));self.assertEqual(len(self.calls),6)
        self.assertEqual(second['checked_at'],checked.isoformat())
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW)
        self.assertIsNone(issue)
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',checked+dt.timedelta(minutes=15,seconds=1))
        self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])

    async def test_failed_refresh_cannot_reuse_previous_partial_as_current(self):
        self.extra();await self.partial()
        async def unavailable(url):raise httpx.ReadTimeout('fixture')
        self.worker.request=unavailable
        report=await self.check(fixture.NOW+dt.timedelta(seconds=60))
        self.assertEqual(report['status'],'failed');self.assertTrue(report['games'])
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW+dt.timedelta(seconds=60))
        self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])

    async def test_partial_records_require_explicit_nonconflicting_inventory_metadata(self):
        self.extra();await self.partial()
        original=self.store.get_checkpoint(CHECKPOINT)
        for changes in ({'missing_game_ids':[fixture.ID]}, {'conflicting_game_ids':[fixture.ID]},
                        {'expected_game_ids':[]}, {'missing_game_ids':[{}]}):
            with self.subTest(changes=changes):
                self.store.set_checkpoint(CHECKPOINT,{**original,**changes})
                detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW)
                self.assertIsNotNone(issue);self.assertFalse(detail['game_verified'])

    async def test_attempt_details_expose_only_public_sources_and_bounded_error_codes(self):
        await self.check()
        original=self.store.get_checkpoint(CHECKPOINT)
        original['attempts']=[{'source':RELAY_URL,'error':'HTTPStatusError: HTTP 403'},
            {'source':'https://private.invalid/?key=hidden','error':'RuntimeError: secret-body'},
            {'source':SCOREBOARD_URL,'error':'ValueError'}]
        self.store.set_checkpoint(CHECKPOINT,original)
        detail,issue=_current_schedule_check(self.store,fixture.DAY,'NYK',fixture.NOW)
        self.assertIsNone(issue)
        encoded=json.dumps(detail['attempts'])
        self.assertNotIn('hidden',encoded);self.assertNotIn('secret-body',encoded)
        self.assertEqual(detail['attempts'][0]['error'],'HTTPStatusError: HTTP 403')
        self.assertEqual(detail['attempts'][1],{'source':'unrecognized_source','error':'SourceCheckFailed'})

    async def test_short_schedule_retry_does_not_bypass_publisher_http_cooldowns(self):
        calls=[]
        def denied(request):
            calls.append(str(request.url))
            return httpx.Response(403,text='fixture denial')
        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
            worker=FeedWorker(self.store,client=client)
            first=await refresh_game_schedule(self.store,worker,now=fixture.NOW)
            self.assertEqual(first['status'],'failed');self.assertEqual(len(calls),3)
            second=await refresh_game_schedule(self.store,worker,now=fixture.NOW+dt.timedelta(seconds=60))
        self.assertEqual(second['status'],'failed');self.assertEqual(len(calls),3)
        self.assertTrue(all(attempt['error']=='RuntimeError' for attempt in second['attempts']))


if __name__=='__main__':unittest.main()
