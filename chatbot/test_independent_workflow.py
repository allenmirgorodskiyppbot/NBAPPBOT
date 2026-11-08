"""No-upload Telegram research and the once-daily pregame briefing."""
import asyncio
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from feeds import canonical_game, canonical_player
from gameday_digest import GamedayDigest, render_digest
from master import MasterStore
from season_calendar import seed_calendar
from telegram_bot import TelegramBot, chunks, parse_picks_request

NOW = dt.datetime(2026, 10, 30, 22, 30, tzinfo=dt.timezone.utc)
DATE = '2026-10-30'
USER = 123


class PickRequestTests(unittest.TestCase):
    def test_command_counts_and_dates_are_explicit(self):
        self.assertEqual(parse_picks_request('/picks', NOW), {'date': None, 'count': 4, 'team': 'NYK'})
        self.assertEqual(parse_picks_request('/picks 2026-10-30 6', NOW)['count'], 6)
        self.assertEqual(parse_picks_request('/picks 3 tomorrow', NOW)['date'], '2026-10-31')
        self.assertIsNone(parse_picks_request('/picks next 4', NOW)['date'])
        self.assertEqual(parse_picks_request('/picks@my_bot tonight 2', NOW)['date'], DATE)
        for request in ['/picks 0', '/picks 8', '/picks 2 4', '/picks today tomorrow',
                        '/picks 2026-02-30', '/picks 2026-10-30 tonight', '/picks Lakers']:
            with self.subTest(request=request), self.assertRaises(ValueError):
                parse_picks_request(request, NOW)

    def test_natural_requests_route_to_generic_data_capability(self):
        for request in ['give me picks', 'Please give me four picks tonight',
                        'make 4 picks for tonight', 'I need a 4 leg',
                        'projections for next game', 'Can you show me your best picks?',
                        'give me Knicks picks tonight', 'give me picks for NYK tonight',
                        'Knicks picks tonight', 'give me four New York Knicks picks tonight']:
            with self.subTest(request=request):
                result = parse_picks_request(request, NOW)
                self.assertIsNotNone(result)
                self.assertEqual(result['count'], 4)
        self.assertEqual(parse_picks_request('make 4 picks for tonight', NOW)['date'], DATE)
        self.assertIsNone(parse_picks_request('projections for next game', NOW)['date'])

    def test_explanations_other_teams_and_unhandled_dates_stay_in_chat(self):
        for request in ['Why did you choose those picks?', 'Explain my picks', 'Did my picks win?',
                        'Show my previous picks', 'Give me Brunson projections',
                        'Give me projections for Jalen Brunson', 'Give me Lakers picks tonight',
                        'Give me picks for the Lakers tonight', 'Give me all NBA picks',
                        'give me six picks for the Hornets game', 'give me picks next Friday',
                        '/boards', 'Thanks for the picks']:
            with self.subTest(request=request):
                self.assertIsNone(parse_picks_request(request, NOW))

    def test_tonight_uses_eastern_date_across_utc_midnight(self):
        utc_after_midnight = dt.datetime(2026, 10, 31, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(parse_picks_request('give me picks tonight', utc_after_midnight)['date'], DATE)

    def test_short_dates_and_date_only_messages_keep_default_four_targets(self):
        for text in ['Nov 2', 'November 2', '11/2', '11/02', 'NOV 2?', 'Nov 2.', '11/2.',
                     '/picks Nov 2', '/picks November 2', '/picks 11/2',
                     '/picks@my_bot Nov. 2nd', 'picks November 2',
                     'give me picks for November 2', 'give me picks on 11/2',
                     'please give me four Knicks picks for Nov 2']:
            with self.subTest(text=text):
                self.assertEqual(parse_picks_request(text, NOW),
                                 {'date': '2026-11-02', 'count': 4, 'team': 'NYK'})

    def test_date_day_is_never_count_and_explicit_year_is_preserved(self):
        for text in ['/picks Nov 20', 'November 20', 'picks for 11/20']:
            self.assertEqual(parse_picks_request(text, NOW),
                             {'date': '2026-11-20', 'count': 4, 'team': 'NYK'})
        for text in ['/picks Nov 2 6', 'give me six picks for November 2', '/picks 6 11/2']:
            self.assertEqual(parse_picks_request(text, NOW)['count'], 6)
        for text in ['/picks Nov 2, 2025', 'Nov 2 2025', 'picks for 11/2/2025']:
            self.assertEqual(parse_picks_request(text, NOW)['date'], '2025-11-02')

    def test_missing_year_uses_current_season_without_rolling_past_dates_forward(self):
        november = dt.datetime(2026, 11, 2, 17, tzinfo=dt.timezone.utc)
        january = dt.datetime(2027, 1, 10, 17, tzinfo=dt.timezone.utc)
        for text, now, expected in [('Jan 5', november, '2027-01-05'),
                                    ('Oct 30', november, '2026-10-30'),
                                    ('Nov 2', january, '2026-11-02'),
                                    ('Jan 5', january, '2027-01-05')]:
            self.assertEqual(parse_picks_request(text, now)['date'], expected)
        # June 30 in New York still belongs to the season that began in 2026.
        utc_july = dt.datetime(2027, 7, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(parse_picks_request('Nov 2', utc_july)['date'], '2026-11-02')

    def test_invalid_or_conflicting_short_dates_never_fall_back_to_next_game(self):
        for text in ['Feb 30', '13/2', '/picks 11/31', 'picks for April 31',
                     '/picks Nov 2 today', '/picks Nov 2 Nov 3',
                     '/picks 11/2 2026-11-03', '/picks Nov 2 7',
                     '/picks 11/2/26', 'Feb 29', 'picks 11/2.5',
                     'picks 2/2/2', 'picks 4/4/4', 'picks for 2/2/2', 'picks 2/2/today',
                     'picks 2026-11-02/3']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_picks_request(text, NOW)
        self.assertEqual(parse_picks_request('Feb 29 2028', NOW)['date'], '2028-02-29')

    def test_month_names_do_not_capture_unrelated_chat_or_specific_player_questions(self):
        for text in ['Who plays Nov 2?', 'How did the Nov 2 picks do?',
                     'Explain my picks from November 2', 'Give me picks for Jalen Brunson on Nov 2',
                     'Give me Lakers picks for November 2', 'I was born November 2',
                     'May I have more details?', 'picks November', 'picks next Friday',
                     'picks Nov 2.5', 'picks Nov 2–3',
                     'give me picks against Brooklyn on November 2']:
            with self.subTest(text=text):
                self.assertIsNone(parse_picks_request(text, NOW))


class IndependentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)
        for player, team in [('Jalen Brunson', 'NYK'), ('Karl-Anthony Towns', 'NYK'),
                             ('Tyrese Maxey', 'PHI'), ('Joel Embiid', 'PHI')]:
            for number in range(20):
                date = (dt.date(2026, 10, 29)-dt.timedelta(days=number)).isoformat()
                game = canonical_game(date, team, 'BOS')
                self.store.upsert_game({'id': game, 'date': date, 'home_team': team, 'away_team': 'BOS',
                    'status': 'final', 'source': 'isolated workflow fixture'})
                self.store.upsert_player_game({'game_id': game, 'date': date, 'player': player,
                    'player_id': canonical_player(player), 'team': team, 'opponent': 'BOS',
                    'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
                    'minutes': 32+number%3, 'pts': 20+number%4, 'reb': 5+number%3,
                    'ast': 4+number%2, 'fga': 17+number%4, 'source': 'isolated workflow fixture'})
        games = self.store.games('2026-10-29')
        self.store.set_checkpoint('daily_nba:2026-10-29', {
            'date':'2026-10-29', 'complete':True, 'source':'isolated workflow inventory',
            'last_checked_at':NOW.isoformat(), 'last_refresh_status':'success',
            'last_refresh_error':None, 'publisher_retrieved_at':NOW.isoformat(),
            'inventory_verified':True, 'inventory_verified_at':NOW.isoformat(),
            'inventory_verification_source':'https://example.test/workflow-inventory',
            'inventory_game_ids':[g['id'] for g in games],
            'final_game_ids':[g['id'] for g in games],
            'team_final_game_ids':{team:[g['id'] for g in games if team in (g['home_team'],g['away_team'])]
                                   for team in ('NYK','PHI','BOS')}})
        with self.store.connect() as db:
            upcoming = [dict(row) for row in db.execute("SELECT * FROM season_schedule WHERE date='2026-10-30'")]
        self.store.set_checkpoint('nba_schedule_today', {'date':'2026-10-30',
            'status':'success', 'error':None, 'checked_at':NOW.isoformat(),
            'source':'https://example.test/workflow-schedule', 'games':upcoming,
            'expected_game_ids':[g['id'] for g in upcoming]})
        self.answer = AsyncMock(return_value='Ordinary research chat.')
        self.refresh = AsyncMock()
        self.bot = TelegramBot(self.store, self.answer, token='unit-test', allowed_user_ids=[USER],
                               research_refresh_callback=self.refresh)
        self.bot._typing = AsyncMock()
        self.addAsyncCleanup(self.bot.shutdown)

    def message(self, text):
        return {'text': text, 'from': {'id': USER}, 'chat': {'id': USER, 'type': 'private'}}

    def counts(self):
        with self.store.connect() as db:
            return {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                    for table in ('boards', 'entries', 'games', 'player_games')}

    async def test_no_board_command_returns_independent_targets_without_chat_model(self):
        before = self.counts()
        with patch('independent_picks._now', return_value=NOW):
            response = await self.bot._handle_message(self.message('/picks 2026-10-30 4'), 1)
        self.assertIn('My projections', response)
        self.assertIn('My target lines', response)
        self.assertIn('More if line', response)
        self.assertIn('Less if line', response)
        self.assertIn('Jalen Brunson', response)
        self.assertIn('Tyrese Maxey', response)
        self.assertEqual(before, self.counts())
        self.assertEqual(before['boards'], 0)
        self.answer.assert_not_awaited()
        self.refresh.assert_not_awaited()

    async def test_natural_request_uses_same_builder_and_preserves_followup_context(self):
        with patch('independent_picks._now', return_value=NOW):
            response = await self.bot._handle_message(self.message('give me four picks for next game'), 2)
        self.assertIn('My target lines', response)
        self.answer.assert_not_awaited()
        history = self.bot._conversation(USER)
        self.assertEqual(history[-1]['content'], response)

    async def test_bare_month_day_runs_real_research_without_ai_or_recording_entry(self):
        before = self.counts()
        with patch('telegram_bot.parse_picks_request', side_effect=lambda text, **kw: parse_picks_request(text, NOW, **kw)), \
                patch('independent_picks._now', return_value=NOW):
            response = await self.bot._handle_message(self.message('Oct 30'), 20)
        self.assertIn('My target lines', response)
        self.assertIn('Jalen Brunson', response)
        self.answer.assert_not_awaited()
        self.refresh.assert_not_awaited()
        self.assertEqual(self.counts(), before)

    async def test_short_date_after_tipoff_is_held_instead_of_choosing_another_game(self):
        after_tip = NOW + dt.timedelta(hours=2)
        with patch('telegram_bot.parse_picks_request', side_effect=lambda text, **kw: parse_picks_request(text, after_tip, **kw)), \
                patch('independent_picks._now', return_value=after_tip):
            response = await self.bot._handle_message(self.message('10/30'), 21)
        self.assertIn('started', response)
        self.assertNotIn('Nov', response)
        self.answer.assert_not_awaited()
        self.assertEqual(self.store.list_entries(), [])

    async def test_explicit_started_day_is_not_silently_replaced_with_next_game(self):
        after_tip = NOW + dt.timedelta(hours=2)
        with patch('independent_picks._now', return_value=after_tip):
            response = await self.bot._handle_message(self.message('/picks 2026-10-30 4'), 3)
            upcoming = await self.bot._handle_message(self.message('/picks'), 4)
        self.assertIn('started', response)
        self.assertNotIn('Nov', response)
        self.assertIn('Nov', upcoming)
        self.assertEqual(self.store.list_entries(), [])

    async def test_questions_about_previous_picks_still_use_chat(self):
        response = await self.bot._handle_message(self.message('Why did you choose those picks?'), 5)
        self.assertEqual(response, 'Ordinary research chat.')
        self.answer.assert_awaited_once()
        self.refresh.assert_not_awaited()

    async def test_pregame_digest_has_targets_without_upload_and_queues_once(self):
        store = self.store
        class Worker:
            def __init__(self):
                self._cycle_lock = asyncio.Lock()
            async def request(self, url):
                response = httpx.Response(403, request=httpx.Request('GET', url))
                response.raise_for_status()
            async def collect_news_sources(self):
                store.record_news({'id': 'fresh-report', 'title': 'Knicks roster update: player acquired',
                    'team': 'NYK', 'source': 'fixture publisher', 'published_at': NOW.isoformat(),
                    'url': 'https://publisher.example/roster'})
                store.feed_success('rss_1', 'fixture news')
        digest = GamedayDigest(self.store, Worker(), self.bot.notify, now=lambda: NOW)
        first = await digest.tick()
        second = await digest.tick()
        self.assertEqual(first['state'], 'queued')
        self.assertEqual(second['state'], 'already_queued_today')
        with self.store.connect() as db:
            rows = db.execute('SELECT text FROM telegram_outbox').fetchall()
        self.assertEqual(len(rows), 1)
        text = rows[0]['text']
        self.assertIn('My target lines', text)
        self.assertIn('More if line', text)
        self.assertIn('Knicks roster update', text)
        self.assertIn('No upload is needed', text)
        self.assertIn('Schedule checked:', text)
        self.assertEqual(len(list(chunks(text))), 1)
        self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])

    async def test_projection_failure_does_not_drop_the_injury_digest(self):
        self.store.record_news({'id': 'report', 'title': 'Knicks injury: player questionable',
            'team': 'NYK', 'source': 'fixture', 'published_at': NOW.isoformat()})
        with self.store.connect() as db:
            game = dict(db.execute('SELECT * FROM season_schedule WHERE date=?', (DATE,)).fetchone())
        with patch('independent_picks.build_independent_picks', side_effect=ValueError('fixture unavailable')):
            text = render_digest(self.store, game, NOW, NOW.isoformat(), False)
        self.assertIn('temporarily unavailable', text)
        self.assertIn('player questionable', text)
        self.assertIn('Schedule checked:', text)

    async def test_large_news_and_four_targets_keep_complete_footer_in_one_chunk(self):
        for i in range(20):
            self.store.record_news({'id': f'long-{i}', 'title': 'Knicks injury: '+('player questionable '*20),
                'team': 'NYK', 'source': 'fixture publisher', 'published_at': NOW.isoformat(),
                'url': 'https://publisher.example/'+str(i)+'/'+('x'*650)})
        with self.store.connect() as db:
            game = dict(db.execute('SELECT * FROM season_schedule WHERE date=?', (DATE,)).fetchone())
        text = render_digest(self.store, game, NOW, NOW.isoformat(), True)
        self.assertLessEqual(len(text), 3450)
        self.assertEqual(len(list(chunks(text))), 1)
        self.assertIn('My target lines', text)
        self.assertIn('Schedule checked:', text)
        self.assertIn(game['source'][:250], text)


if __name__ == '__main__':
    unittest.main()
