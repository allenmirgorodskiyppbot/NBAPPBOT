"""Ninety-minute reminders, fresh catch-up scans and private board ownership."""
import asyncio
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from feeds import canonical_game
from gameday_digest import (GamedayDigest, _recent_uploaded_board, _uploaded_pick_scan,
                            parse_schedule, reminder_status, render_digest)
from master import MasterStore
from season_calendar import import_calendar
from telegram_bot import TelegramBot
import test_gameday_digest as fixtures

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 7, 23, 30, tzinfo=UTC)  # 6:30 PM Eastern.
TIP = dt.datetime(2026, 11, 8, 1, tzinfo=UTC)       # 8 PM Eastern.
DAY = '2026-11-07'
ID = canonical_game(DAY, 'CLE', 'NYK')


class NinetyMinuteReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        env = patch.dict(os.environ, {'NEWS_ALERT_MODE':'knicks_gameday',
            'KNICKS_DIGEST_MINUTES_BEFORE':'90', 'KNICKS_SCHEDULE_URL':fixtures.URL})
        env.start(); self.addCleanup(env.stop)
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name)/'master.sqlite')
        self.current = NOW
        self.document = fixtures.calendar(TIP.isoformat())
        first = self.document['leagueSchedule']['gameDates'][0]['games'][0]
        first['homeTeam']['teamTricode'] = 'CLE'
        first['awayTeam']['teamTricode'] = 'NYK'
        self.games = parse_schedule(self.document, NOW.date(), fixtures.URL)
        self.game = self.games[0]
        self.bot = TelegramBot(self.store, AsyncMock(), token='fake-reminder-token', allowed_user_ids=[123])
        owner = self
        class Worker:
            def __init__(inner):
                inner._cycle_lock = asyncio.Lock()
                inner.collect_news_sources = AsyncMock()
            async def request(inner, url):
                return owner.document
        self.worker = Worker()
        self.digest = GamedayDigest(self.store, self.worker, self.bot.notify, now=lambda:self.current)

    def outbox(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox')]

    def board(self, owner=123, age=0, *, game=None):
        game = game or self.game
        leg = {'player':'Jalen Brunson', 'team':'NYK', 'opponent':game['home_team'],
            'game_id':game['id'], 'date':game['date'], 'stat':'pts', 'line':25.5,
            'side':'more', 'offer_variant':'standard'}
        board = self.store.create_board([leg], source=f'screenshot:{owner}:fixture-{age}')
        with self.store.connect() as db:
            db.execute('UPDATE boards SET created_at=? WHERE id=?',
                       ((self.current-dt.timedelta(seconds=age)).isoformat(), board['id']))
        return self.store.get_board(board['id'])

    async def test_default_is_ninety_and_explicit_custom_lead_is_preserved(self):
        with patch.dict(os.environ):
            os.environ.pop('KNICKS_DIGEST_MINUTES_BEFORE', None)
            default = GamedayDigest(self.store, self.worker, self.bot.notify)
        self.assertEqual(default.lead, 90)
        with patch.dict(os.environ, {'KNICKS_DIGEST_MINUTES_BEFORE':'45'}):
            custom = GamedayDigest(self.store, self.worker, self.bot.notify)
        self.assertEqual(custom.lead, 45)

    async def test_eight_pm_game_waits_until_six_thirty_then_queues_once(self):
        self.current = NOW-dt.timedelta(seconds=1)
        waiting = await self.digest.tick()
        self.assertEqual(waiting['state'], 'waiting')
        self.assertEqual(dt.datetime.fromisoformat(waiting['send_at']), NOW)
        self.assertIn('06:30 PM Eastern', reminder_status(self.store))
        self.assertEqual(self.outbox(), [])
        self.current = NOW
        with patch('gameday_digest.render_digest', return_value='Current four-option check') as render:
            result = await self.digest.tick()
            again = await self.digest.tick()
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(again['state'], 'already_queued_today')
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.outbox()[0]['expires_at'], TIP.timestamp())
        self.assertEqual(render.call_args.kwargs['board_owner_chat_id'], 123)

    async def test_no_current_board_asks_for_current_full_screenshots_and_independent_targets(self):
        text = _uploaded_pick_scan(self.store, self.game, NOW, 123)
        self.assertIn('current full PrizePicks screenshots', text)
        self.assertIn('four readable', text)
        self.assertIn('/picks', text)
        self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])

    async def test_only_fresh_same_game_boards_for_the_single_owner_are_scanned(self):
        selected = self.board(age=25)
        self.board(owner=456, age=0)
        self.board(age=31*60)
        self.board(age=-10)
        other = {**self.game, 'id':canonical_game(DAY, 'BOS', 'NYK'), 'home_team':'BOS'}
        self.board(age=1, game=other)
        result = _recent_uploaded_board(self.store, self.game, NOW, 123)
        self.assertEqual(result['id'], selected['id'])
        self.assertIsNone(_recent_uploaded_board(self.store, self.game, NOW, None))

    async def test_scan_recomputes_at_actual_check_time_and_requests_four(self):
        board = self.board()
        comparison = {'leg':board['legs'][0], 'projection':{'reference_projection':27.25,
            'n':20, 'last_date':'2026-11-06'}}
        before_boards = self.store.boards()
        report = {'board_id':board['id'], 'original_legs':board['legs']}
        with patch('analysis_engine.analyze_board', return_value=report) as analyze, \
             patch('board_shortlist.enrich_shortlist', return_value={
                 'board_id':board['id'], 'generated_at':NOW.isoformat(), 'shortlist':[comparison]}) as shortlist:
            text = _uploaded_pick_scan(self.store, self.game, NOW, 123)
        analyze.assert_called_once_with(self.store, board['id'], as_of=NOW)
        shortlist.assert_called_once_with(self.store, report, now=NOW, count=4)
        self.assertIn('More 25.5 points', text)
        self.assertIn('mean 27.25', text)
        self.assertIn('1/4 eligible', text)
        self.assertEqual(self.store.boards(), before_boards)
        self.assertEqual(self.store.list_entries(), [])
        with self.store.connect() as db:
            archive = dict(db.execute('SELECT * FROM research_runs').fetchone())
        self.assertEqual(archive['owner_key'], 'telegram:123')
        self.assertEqual(archive['kind'], 'board_shortlist')
        self.assertEqual(archive['board_id'], board['id'])

    async def test_held_research_is_not_reported_as_four_eligible_picks(self):
        board = self.board()
        held = {'player':'Jalen Brunson', 'stat':'pts', 'line':25.5,
            'mean':27.25, 'median':27, 'n':20, 'source_data_through_date':'2026-11-06'}
        with patch('analysis_engine.analyze_board', return_value={}), \
             patch('board_shortlist.enrich_shortlist', return_value={'shortlist':[], 'research_candidates':[held]}):
            text = _uploaded_pick_scan(self.store, self.game, NOW, 123)
        self.assertIn('Research held:', text)
        self.assertIn('0/4 eligible', text)
        self.assertIn('Research holds are not picks', text)
        self.assertEqual(self.store.list_entries(), [])

    async def test_multiple_allowed_users_do_not_share_one_owners_uploaded_board(self):
        bot = TelegramBot(self.store, AsyncMock(), token='fake-token', allowed_user_ids=[123,456])
        digest = GamedayDigest(self.store, self.worker, bot.notify, now=lambda:self.current)
        self.assertIsNone(digest._board_owner_chat_id)

    async def test_full_digest_keeps_four_comparisons_and_provenance_in_one_message(self):
        names = ['Jalen Brunson', 'Karl-Anthony Towns', 'Donovan Mitchell', 'Darius Garland']
        legs = [{'player':name, 'team':'NYK' if number < 2 else 'CLE',
            'opponent':'CLE' if number < 2 else 'NYK', 'game_id':ID, 'date':DAY,
            'stat':'pts', 'line':20.5+number, 'side':'more', 'offer_variant':'standard'}
            for number,name in enumerate(names)]
        board = self.store.create_board(legs, source='screenshot:123:four-current-lines')
        with self.store.connect() as db:
            db.execute('UPDATE boards SET created_at=? WHERE id=?', (NOW.isoformat(), board['id']))
        shortlist = {'board_id':board['id'], 'generated_at':NOW.isoformat(),
            'shortlist':[{'leg':leg, 'projection':{'reference_projection':27.25,
                'n':20, 'last_date':'2026-11-06'}} for leg in legs]}
        with patch('analysis_engine.analyze_board', return_value={'board_id':board['id']}), \
             patch('board_shortlist.enrich_shortlist', return_value=shortlist), \
             patch('independent_picks.build_independent_picks', return_value={'note':'No additional target.'}):
            text = render_digest(self.store, self.game, NOW, NOW.isoformat(), False,
                                 board_owner_chat_id=123)
        for name in names:
            self.assertIn(name, text)
        self.assertIn('4/4 eligible', text)
        self.assertIn('Schedule checked:', text)
        self.assertIn('No upload is needed', text)
        self.assertIn('Publisher reports/manual notes', text)
        self.assertLessEqual(len(text), 3450)
        self.assertEqual(self.store.list_entries(), [])

    async def test_late_verified_ready_game_recomputes_a_fresh_catchup_before_tipoff(self):
        self.current = NOW+dt.timedelta(minutes=40)
        with patch('pregame_freshness.assess_pregame_freshness', return_value={'ready':True}) as ready, \
             patch('gameday_digest.render_digest', return_value='Fresh catch-up four-option check') as render:
            result = await self.digest.tick()
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(ready.call_count, 2)
        self.assertEqual(render.call_args.args[2], self.current)
        self.assertTrue(render.call_args.args[1]['catch_up_notice'])
        self.assertEqual(self.outbox()[0]['text'], 'Fresh catch-up four-option check')
        self.assertEqual(len(self.outbox()), 1)

    async def test_late_unready_game_holds_research_and_asks_for_new_lines(self):
        self.current = NOW+dt.timedelta(minutes=40)
        with patch('pregame_freshness.assess_pregame_freshness', return_value={'ready':False}), \
             patch('gameday_digest.render_digest') as render:
            result = await self.digest.tick()
        render.assert_not_called()
        self.assertEqual(result['notice_kind'], 'late_catchup')
        text = self.outbox()[0]['text']
        self.assertIn('Catch-up notice', text)
        self.assertIn('Picks held', text)
        self.assertIn('current full PrizePicks screenshots', text)

    async def test_an_existing_sixty_minute_notice_is_not_duplicated_by_the_new_policy(self):
        self.current = NOW+dt.timedelta(minutes=40)
        key = 'knicks-gameday:'+DAY
        await self.bot.notify('Existing reminder', event_key=key, expires_at=TIP.timestamp())
        self.store.set_checkpoint(key, {'queued_at':self.current.isoformat(), 'minutes_before_tipoff':60})
        result = await self.digest.tick()
        self.assertEqual(result['state'], 'already_queued_today')
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.outbox()[0]['text'], 'Existing reminder')

    async def test_fresh_partial_inventory_for_other_game_does_not_hold_verified_knicks_notice(self):
        import_calendar(self.store, {'season':'2026-27', 'covered_teams':['NYK'],
            'games':self.games, 'source':'https://www.nba.com/knicks/schedule',
            'retrieved_at':'2026-10-29T12:00:00+00:00'})
        self.store.upsert_game(self.game)
        missing = canonical_game(DAY, 'LAL', 'GSW')
        report = {'date':DAY, 'status':'partial', 'checked_at':NOW.isoformat(),
            'source':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261107',
            'games':[{**self.game, 'reported_status':'scheduled'}],
            'expected_game_ids':[ID,missing], 'missing_game_ids':[missing], 'conflicting_game_ids':[],
            'error':'An unrelated saved event is missing.'}
        async def blocked(url):
            raise httpx.ReadTimeout('fixture only')
        async def checked(*args, **kwargs):
            self.store.set_checkpoint('nba_schedule_today', report)
            return report
        self.worker.request = blocked
        with patch('game_schedule_check.refresh_game_schedule', side_effect=checked), \
             patch('gameday_digest.render_digest', return_value='Verified Knicks timing') as render:
            result = await self.digest.tick()
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['schedule_mode'], 'live_checked')
        render.assert_called_once()
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today')['status'], 'partial')

    async def test_at_tipoff_never_sends_a_late_scan(self):
        self.current = TIP
        with patch('gameday_digest.render_digest') as render:
            result = await self.digest.tick()
        self.assertEqual(result['state'], 'game_started')
        render.assert_not_called()
        self.assertEqual(self.outbox(), [])


if __name__ == '__main__':
    unittest.main()
