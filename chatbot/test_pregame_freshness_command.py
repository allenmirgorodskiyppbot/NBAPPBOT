"""The concise freshness command reads real saved coverage without AI or fetches."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from feeds import canonical_game
from master import MasterStore
from telegram_bot import HELP, RESPONSIVE_COMMANDS, TelegramBot, freshness_report
from test_telegram import TOKEN, USER_ID, update


NOW = dt.datetime(2026, 11, 4, 20, tzinfo=dt.timezone.utc)


class PregameFreshnessCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.answer = AsyncMock(side_effect=AssertionError('Freshness must not call AI.'))
        self.refresh = AsyncMock(side_effect=AssertionError('Freshness must not fetch external data.'))
        self.bot = TelegramBot(self.store, self.answer, token=TOKEN, allowed_user_ids=[USER_ID],
                               research_refresh_callback=self.refresh)
        self.today_id = canonical_game('2026-11-04', 'NYK', 'BKN')
        self.store.upsert_game({'id': self.today_id, 'date': '2026-11-04',
            'home_team': 'NYK', 'away_team': 'BKN', 'status': 'scheduled',
            'start_time': '2026-11-05T00:30:00+00:00', 'source': 'saved test schedule'})
        self.store.set_checkpoint('nba_schedule_today', {'date':'2026-11-04',
            'status':'success', 'error':None, 'checked_at':NOW.isoformat(),
            'source':'https://example.invalid/current-day-schedule',
            'expected_game_ids':[self.today_id], 'games':self.store.games('2026-11-04')})
        previous_ids = {}
        for team, opponent in [('NYK', 'BOS'), ('BKN', 'CHI')]:
            identity = canonical_game('2026-11-03', team, opponent)
            previous_ids[team] = [identity]
            self.store.upsert_game({'id': identity, 'date': '2026-11-03',
                'home_team': team, 'away_team': opponent, 'status': 'final',
                'start_time': '2026-11-04T00:30:00+00:00', 'source': 'test final-game inventory'})
        self.daily = {'date': '2026-11-03', 'complete': True, 'games': 2, 'expected_games': 2,
            'source': 'https://example.invalid/nba/2026-11-03.json',
            'inventory_scope': 'Independently verified complete final-game inventory',
            'inventory_verified': True, 'inventory_verified_at': '2026-11-04T19:00:00+00:00',
            'inventory_verification_source': 'https://example.invalid/scoreboard/2026-11-03',
            'last_checked_at': '2026-11-04T19:00:00+00:00',
            'last_refresh_status': 'success', 'imported_at': '2026-11-04T16:31:00+00:00',
            'publisher_retrieved_at': '2026-11-04T16:00:00+00:00',
            'final_game_ids': [identity for values in previous_ids.values() for identity in values],
            'team_final_game_ids': previous_ids}
        self.store.set_checkpoint('daily_nba:2026-11-03', self.daily)
        self.store.set_checkpoint('nightly_nba_refresh', {'enabled': True, 'phase': 'updated',
            'target_game_date': '2026-11-03', 'source_data_through_date': '2026-11-03',
            'pending_dates': [], 'last_attempt': '2026-11-04T19:59:00+00:00'})
        self.store.set_checkpoint('tracking_refresh_status', {'coverage': [
            {'category': 'Player totals', 'status': 'collected', 'source_data_through_date': '2026-11-03'},
            {'category': 'Passing', 'status': 'collected', 'source_data_through_date': None},
            {'category': 'Rebounding', 'status': 'collected'},
            {'category': 'Overall', 'status': 'unavailable', 'source_data_through_date': None}]})
        with self.store.connect() as db:
            db.execute('INSERT INTO feed_status(name,last_success,last_attempt,detail) VALUES (?,?,?,?)',
                ('nba_daily_refresh', '2026-11-04T16:31:00+00:00', '2026-11-04T19:59:00+00:00', 'test import'))

    def snapshot(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def outbox(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox ORDER BY id')]

    def test_read_only_report_distinguishes_game_coverage_from_unknown_tracking(self):
        before = self.snapshot()
        report = freshness_report(self.store, NOW)
        self.assertIn('target: 2026-11-03; covered through: 2026-11-03', report)
        self.assertIn('Last successful import: Nov 04, 11:31 AM Eastern', report)
        self.assertNotIn('3:59 PM', report)
        self.assertIn('2 collected categories have unknown covered-game dates; 1 unavailable', report)
        self.assertIn('Game-stat readiness: current for both teams', report)
        self.assertIn('BKN at NYK', report)
        self.assertIn('11:30 AM Eastern', report)
        self.assertIn('hourly retries', report)
        self.assertLess(len(report), 1600)
        self.assertEqual(before, self.snapshot())

    def test_updated_worker_phase_cannot_hide_missing_daily_evidence(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM checkpoints WHERE name='daily_nba:2026-11-03'")
        report = freshness_report(self.store, NOW)
        self.assertIn('waiting for verified coverage', report)
        self.assertIn('Needs:', report)
        self.assertNotIn('current for both teams', report)

    def test_schedule_failure_shows_safe_cause_without_a_status_dump(self):
        check = self.store.get_checkpoint('nba_schedule_today')
        check.update(status='failed', error='HTTPStatusError: HTTP 403')
        self.store.set_checkpoint('nba_schedule_today', check)
        report = freshness_report(self.store, NOW)
        self.assertIn('Schedule check: failed — HTTPStatusError: HTTP 403', report)
        self.assertIn('Nov 04, 3:00 PM Eastern', report)
        self.assertNotIn('current for both teams', report)

    def test_schedule_report_does_not_echo_arbitrary_exception_or_url_content(self):
        check = self.store.get_checkpoint('nba_schedule_today')
        check.update(status='failed', error='Bad response https://source.invalid/?token=private-value')
        self.store.set_checkpoint('nba_schedule_today', check)
        report = freshness_report(self.store, NOW)
        self.assertIn('Schedule check: failed — source check failed', report)
        self.assertNotIn('private-value', report)
        self.assertNotIn('https://source.invalid', report)

    def test_pending_and_disabled_updates_remain_visible(self):
        state = self.store.get_checkpoint('nightly_nba_refresh')
        state.update(enabled=False, source_data_through_date='2026-11-01',
                     pending_dates=['2026-11-03', '2026-11-02'])
        self.store.set_checkpoint('nightly_nba_refresh', state)
        report = freshness_report(self.store, NOW)
        self.assertIn('covered through: 2026-11-01', report)
        self.assertIn('Pending import dates: 2026-11-02, 2026-11-03', report)
        self.assertIn('Automatic refresh is disabled', report)

    async def test_command_uses_saved_evidence_without_ai_external_refresh_or_broadcast(self):
        actual_report = freshness_report
        with patch('telegram_bot.freshness_report', side_effect=lambda store: actual_report(store, NOW)):
            self.assertTrue(await self.bot.process_update(update(1, '/freshness')))
        rows = self.outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['chat_id'], USER_ID)
        self.assertIn('Game-stat readiness:', rows[0]['text'])
        self.answer.assert_not_awaited()
        self.refresh.assert_not_awaited()
        self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])

    async def test_freshness_preserves_private_allowlist_boundary(self):
        with patch('telegram_bot.freshness_report') as report:
            self.assertFalse(await self.bot.process_update(update(2, '/freshness', user=999)))
            self.assertFalse(await self.bot.process_update(update(3, '/freshness', chat_type='group', chat_id=-100)))
            self.assertFalse(await self.bot.process_update(update(4, '/freshness', chat_id=999)))
            report.assert_not_called()
        self.assertEqual(self.outbox(), [])

    async def test_help_registration_and_arguments_do_not_call_ai(self):
        self.assertIn('/freshness —', HELP)
        self.assertIn('/freshness', RESPONSIVE_COMMANDS)
        with patch('telegram_bot.freshness_report') as report:
            await self.bot.process_update(update(5, '/freshness today'))
            report.assert_not_called()
        self.assertIn('Use /freshness alone', self.outbox()[0]['text'])
        self.answer.assert_not_awaited()

    def test_future_game_is_not_advertised_as_fresh_for_that_future_day(self):
        with self.store.connect() as db:
            db.execute('DELETE FROM games WHERE id=?', (self.today_id,))
        self.store.upsert_game({'id': canonical_game('2026-11-05', 'NYK', 'BKN'), 'date': '2026-11-05',
            'home_team': 'NYK', 'away_team': 'BKN', 'status': 'scheduled',
            'start_time': '2026-11-06T00:30:00+00:00', 'source': 'saved test schedule'})
        report = freshness_report(self.store, NOW)
        self.assertIn('Nov 05', report)
        self.assertIn('waiting for verified coverage', report)
        self.assertNotIn('current for both teams', report)

    def test_missing_collection_metadata_is_reported_as_unknown(self):
        empty = MasterStore(Path(self.directory.name) / 'empty.sqlite')
        report = freshness_report(empty, NOW)
        self.assertIn('covered through: unknown', report)
        self.assertIn('Tracking coverage: not recorded; freshness unknown', report)
        self.assertIn('Game-stat readiness: not verified', report)
        self.assertIn('Worker state not recorded', report)

    def test_failed_schedule_lists_safe_per_source_causes(self):
        from feeds import ESPN
        from nba_cdn import SCOREBOARD_URL
        from game_schedule_check import RELAY_URL
        value = self.store.get_checkpoint('nba_schedule_today')
        value.update(status='failed', error='RuntimeError', attempts=[
            {'source':ESPN+'scoreboard?dates=20261104','error':'RuntimeError: HTTP 403'},
            {'source':SCOREBOARD_URL,'error':'ReadTimeout'},
            {'source':RELAY_URL,'error':'HTTPStatusError: HTTP 404'}])
        self.store.set_checkpoint('nba_schedule_today', value)
        report = freshness_report(self.store, NOW)
        self.assertIn('ESPN: RuntimeError: HTTP 403', report)
        self.assertIn('NBA: ReadTimeout', report)
        self.assertIn('schedule relay: HTTPStatusError: HTTP 404', report)
        value['attempts'] = [{'source':'https://private.invalid/?token=SECRET', 'error':'SECRET configured-token'}]
        self.store.set_checkpoint('nba_schedule_today', value)
        report = freshness_report(self.store, NOW)
        self.assertNotIn('SECRET', report)
        self.assertNotIn('private.invalid', report)


if __name__ == '__main__':
    unittest.main()
