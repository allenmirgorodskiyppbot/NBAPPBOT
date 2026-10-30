"""Fast statistics must honor sample filters and never invent or zero-fill values."""
import asyncio
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app
from master import MasterStore
from player_stats import answer_player_question, player_log, player_log_command

QUESTION = ('Using my stored master dataset, list Jalen Brunson’s last five completed regular-season '
            'games that he played in, showing each date and his points. Calculate his average points '
            'across those games. State how current the data is and flag any missing values.')


class PlayerStatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        for index, points in enumerate([10, None, 30, 40, 50], 1):
            self.add(index, pts=points)
        self.add(6, pts=99, season_type='Playoffs')
        self.add(7, pts=99, appearance_state='DNP')
        self.add(8, pts=99, status='live')
        self.add(9, pts=99, date='2099-01-01')

    def tearDown(self):
        self.directory.cleanup()

    def add(self, index, *, player='Jalen Brunson', pts=20, date=None, status='final',
            appearance_state='Played', season_type='Regular Season'):
        game_id = 'test:' + str(index)
        date = date or f'2026-03-{index:02d}'
        self.store.upsert_game({'id': game_id, 'date': date, 'status': status,
                               'home_team': 'NYK', 'away_team': 'BOS'})
        from feeds import canonical_player
        self.store.upsert_player_game({'game_id': game_id, 'player_id': canonical_player(player),
            'player': player, 'team': 'NYK', 'opponent': 'BOS', 'date': date,
            'season': '2025-26', 'season_type': season_type, 'appearance_state': appearance_state,
            'pts': pts, 'reb': 5, 'ast': 7, 'source': 'FIXTURE SOURCE'})

    def test_exact_failed_question_uses_sql_and_reports_missingness_dates_and_sources(self):
        with patch.dict('os.environ', {'NBA_DATA_SOURCE': 'manual'}):
            result = answer_player_question(self.store, QUESTION)
        self.assertIsNotNone(result)
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['data']['averages']['pts'], 32.5)
        self.assertEqual(len(result['data']['games']), 5)
        self.assertEqual(result['data']['games'][0]['date'], '2026-03-05')
        for expected in ['4/5 observed, 1 missing', 'Average pts: 32.50', '2026-03-01',
                         '2026-03-05', 'FIXTURE SOURCE', 'updates require uploads']:
            self.assertIn(expected, result['answer'])
        self.assertNotIn('2099', result['answer'])

    def test_command_supports_compound_stats_and_preserves_missing_components(self):
        answer = player_log_command(self.store, 'Jalen Brunson | pra | 5')
        self.assertIn('44.50', answer)
        self.assertIn('1 missing', answer)

    def test_explicit_season_is_not_silently_ignored(self):
        result = answer_player_question(self.store, 'Show Jalen Brunson last five games in 2026-27 and points.')
        self.assertIn('No completed', result['answer'])
        self.assertIn('2026-27', result['answer'])

    def test_unsupported_filters_are_left_for_the_general_query_planner(self):
        for question in ['Jalen Brunson last five games against Boston points',
                         'Jalen Brunson last five games for NYK points',
                         'Jalen Brunson last five games since 2026-03-01 points',
                         'Jalen Brunson last five games in October points',
                         'Jalen Brunson last five games in 2026 points']:
            with self.subTest(question=question):
                self.assertIsNone(answer_player_question(self.store, question))

    def test_ambiguous_names_are_not_merged(self):
        self.add(10, player='Jalen Smith')
        self.add(11, player='Jabari Smith')
        self.assertIn('ambiguous', player_log_command(self.store, 'Smith | points | 5'))
        self.assertIn('Jalen Smith', player_log_command(self.store, 'Jalen Smith | points | 5'))

    def test_user_text_is_never_interpolated_into_sql(self):
        before = self.store.status()['counts']
        player_log_command(self.store, "Jalen Brunson'; DROP TABLE games; -- | points | 5")
        self.assertEqual(self.store.status()['counts'], before)
        for bad in ['Jalen Brunson | points | 0', 'Jalen Brunson | points | 999',
                    'Jalen Brunson | unsupported | 5', 'Jalen Brunson | pts | 5 | 2026-25']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                player_log_command(self.store, bad)

    async def test_natural_lookup_works_even_when_model_is_busy_or_not_ready(self):
        state = app.app.state
        state.master = self.store
        state.phase, state.detail = 'model', 'Loading'
        state.chat_lock = asyncio.Lock()
        await state.chat_lock.acquire()
        try:
            with patch.object(app.httpx, 'AsyncClient', side_effect=AssertionError('AI must not be called')):
                answer = await app.telegram_answer(QUESTION)
            self.assertIn('32.50', answer)
        finally:
            state.chat_lock.release()

    async def test_web_answer_path_also_skips_model_and_workbook_loading(self):
        class NoWorkbook:
            def context(self):
                raise AssertionError('No workbook scan is needed')
        with patch.object(app.httpx, 'AsyncClient', side_effect=AssertionError('AI must not be called')):
            result = await app.answer_question(NoWorkbook(), app.ChatRequest(message=QUESTION), {}, self.store)
        self.assertEqual(result['data']['averages']['pts'], 32.5)


if __name__ == '__main__':
    unittest.main()
