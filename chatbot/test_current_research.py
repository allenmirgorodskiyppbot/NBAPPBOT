"""Current chat research and exported derivatives share populations and dates."""
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app
from current_research import read_current_research, current_research_card, wants_current_research
from feeds import canonical_game
from master import MasterStore

NOW = dt.datetime(2026, 10, 31, 16, tzinfo=dt.timezone.utc)


class CurrentResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.master = MasterStore(Path(self.temp.name) / 'master.sqlite')
        with self.master.connect() as db:
            db.execute('CREATE TABLE workbook_evidence(kind TEXT,entity TEXT,payload_json TEXT)')
            definition = {'split_id': 's1', 'player': 'Jalen Brunson', 'window': '2026-27',
                          'season_type': 'Regular Season', 'definition': 'A: Opponent team',
                          'filter': 'All', 'opponent_team': 'PHI', 'statistic': 'pts', 'n': 2}
            db.execute('INSERT INTO workbook_evidence VALUES (?,?,?)',
                       ('splits', definition['player'], json.dumps(definition)))
        self.observation('2026-10-28', 10)
        self.observation('2026-10-30', 30)

    def observation(self, date, points):
        identity = canonical_game(date, 'NYK', 'PHI')
        self.master.upsert_game({'id': identity, 'date': date, 'status': 'final',
                                'home_team': 'NYK', 'away_team': 'PHI', 'source': 'fixture'})
        self.master.upsert_player_game({'game_id': identity, 'player': 'Jalen Brunson', 'team': 'NYK',
                                       'opponent': 'PHI', 'date': date, 'season': '2026-27',
                                       'season_type': 'Regular Season', 'appearance_state': 'Played',
                                       'minutes': 30, 'pts': points, 'source': 'fixture'})

    def read(self, **filters):
        return read_current_research(self.master, 'Calculated_Splits', filters, now=NOW)

    def test_new_games_and_corrections_reach_chat_without_old_snapshot_values(self):
        first = self.read(player='Jalen Brunson', window='2026-27', statistic='pts')
        self.assertEqual(first['rows'][0]['mean'], 20)
        self.observation('2026-10-30', 50)
        second = self.read(player='Jalen Brunson', window='2026-27', statistic='pts')
        self.assertEqual(second['rows'][0]['mean'], 30)
        self.assertEqual(second['rows'][0]['n'], 2)
        self.assertEqual(second['rows'][0]['last_game'], '2026-10-30')
        self.assertNotEqual(second['read_at'][:10], second['rows'][0]['last_game'])

    def test_exact_filters_do_not_substitute_other_opponent_or_season(self):
        for filters in ({'player': 'Jalen Brunson', 'opponent_team': 'BOS'},
                        {'player': 'Jalen Brunson', 'window': '2025-26'},
                        {'player': 'Other Player', 'window': '2026-27'}):
            result = self.read(**filters)
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['matched_rows'], 0)
            self.assertIn('no broader', result['no_matches_note'])

    def test_unscoped_unknown_and_oversized_requests_rejected_before_build(self):
        with patch('refresh_export.build_export_tables') as build:
            cases = [('Other_Table', {}, 5), ('Player_Metrics', {}, 5),
                     ('Calculated_Splits', {'player': 'Jalen Brunson', 'season': '2026-27'}, 5),
                     ('Player_Metrics', {'player': 'Jalen Brunson'}, 21),
                     ('Player_Metrics', {'player': ['Jalen Brunson']}, 5)]
            for table, filters, limit in cases:
                with self.subTest(table=table, filters=filters, limit=limit), self.assertRaises(ValueError):
                    read_current_research(self.master, table, filters, limit, NOW)
            build.assert_not_called()

    def test_limit_returns_whole_rows_and_declares_excerpt(self):
        result = read_current_research(self.master, 'Player_Metrics', {'player': 'Jalen Brunson'}, 1, NOW)
        self.assertEqual(len(result['rows']), 1)
        self.assertTrue(result['truncated'])
        self.assertGreater(result['matched_rows'], 1)
        self.assertIn('missing', result['rows'][0])
        card = current_research_card(result, 7)
        self.assertEqual(card['id'], 'E7')
        self.assertTrue(card['excerpt'])
        self.assertIn('2026-10-30', card['text'])
        self.assertIn('Read time is not freshness', card['text'])

    def test_tracking_unknown_coverage_and_retained_status_survive_rendering(self):
        with self.master.connect() as db:
            record = {'entity': 'Jalen Brunson', 'category': 'Passing', 'season': '2026-27',
                      'season_type': 'Regular Season', 'gp': 5, 'fields': {'potential_assists': 65},
                      'data_through_date': None, 'published_at': '2026-10-30T14:00:00+00:00',
                      'source': 'fixture publication', 'field_units': {'potential_assists': 'source total'}}
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                       ('2026-27', 'Jalen Brunson', 'Passing', json.dumps(record)))
        result = read_current_research(self.master, 'Tracking_Metrics',
                                       {'entity': 'Jalen Brunson', 'category': 'Passing'}, now=NOW)
        row = result['rows'][0]
        self.assertIsNone(row['data_through_date'])
        self.assertEqual(row['status'], 'source_value_covered_date_unknown')
        text = current_research_card(result, 1)['text']
        self.assertIn('source_value_covered_date_unknown', text)
        self.assertIn('2026-10-30T14:00:00+00:00', text)

    def test_read_does_not_write_statistics_or_checkpoints(self):
        with self.master.connect() as db:
            before = '\n'.join(db.iterdump())
        self.read(player='Jalen Brunson')
        with self.master.connect() as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))

    def test_original_row_access_and_new_tool_dispatch_are_separate(self):
        workbook = SimpleNamespace(read=Mock(return_value={'rows': [{'_row': 8, 'A': 'original value'}]}))
        original = app.execute_tool(workbook, 'read_sheet', {'sheet': 'Calculated_Splits', 'start_row': 8}, self.master)
        self.assertEqual(original['rows'][0]['A'], 'original value')
        current = app.execute_tool(workbook, 'read_current_research',
                                   {'table': 'Calculated_Splits', 'filters': {'player': 'Jalen Brunson'}}, self.master)
        self.assertEqual(current['rows'][0]['mean'], 20)
        self.assertEqual(workbook.read.call_count, 1)
        self.assertEqual(app.query_evidence_card('read_current_research', {}, current, 2)['source'],
                         'read_current_research')

    def test_routing_is_capability_specific_not_social_or_literal_source_rewrite(self):
        self.assertTrue(wants_current_research('Show Brunson home versus away splits'))
        self.assertTrue(wants_current_research('Are points and assists correlated?'))
        self.assertTrue(wants_current_research('What are the back-to-back results?'))
        for text in ('Thanks', 'Hello', 'Give me the rest of your suggestions', 'Who guards Brunson?'):
            self.assertFalse(wants_current_research(text))
        self.assertTrue(app.explicit_source_request('Show Correlations row 8 exactly'))

    def test_oversized_factual_row_is_not_cut_mid_value(self):
        result = {'table': 'Refresh_Coverage', 'filters': {}, 'rows': [{'note': 'x' * 4000}], 'truncated': False}
        self.assertIsNone(current_research_card(result, 1))


class CurrentResearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_split_request_retrieves_derivative_instead_of_old_pairing_or_average(self):
        from contextlib import ExitStack
        fixture = CurrentResearchTests('test_read_does_not_write_statistics_or_checkpoints')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        question = 'Show Jalen Brunson matchup splits against PHI'
        resolved = {'question': question, 'original_question': question, 'players': ['Jalen Brunson'], 'teams': ['PHI']}
        calls = AsyncMock(side_effect=[
            {'message': {'content': '', 'tool_calls': [{'function': {'name': 'read_current_research',
                'arguments': {'table': 'Calculated_Splits', 'filters': {'player': 'Jalen Brunson', 'opponent_team': 'PHI',
                              'window': '2026-27', 'statistic': 'pts'}}}}]}},
            {'message': {'content': 'Records retrieved.'}}])
        final = AsyncMock(return_value={'answer': 'Reviewed fresh derivatives', 'sources': []})
        with ExitStack() as stack:
            stack.enter_context(patch('basketball_research.resolve_question', return_value=resolved))
            stack.enter_context(patch('basketball_research.canonical_history', return_value=[]))
            assignment = stack.enter_context(patch('basketball_research.answer_assignment_question', return_value={'answer': 'Old pairing'}))
            direct = stack.enter_context(patch('player_stats.answer_player_question', return_value={'answer': 'Old average'}))
            stack.enter_context(patch('basketball_research.research_context', return_value={}))
            stack.enter_context(patch('grounded_reply.evidence_cards', return_value=[]))
            compact = stack.enter_context(patch.object(app, 'generated_evidence_reply', AsyncMock(return_value={'answer': 'Unrelated compact result'})))
            stack.enter_context(patch('efficient_ai.efficient_chat', calls))
            stack.enter_context(patch.object(app, 'finish_retrieved_reply', final))
            result = await app.answer_question(SimpleNamespace(sheets={}), app.ChatRequest(message=question), {}, fixture.master)
        assignment.assert_not_called()
        direct.assert_not_called()
        compact.assert_not_awaited()
        self.assertEqual(result['answer'], 'Reviewed fresh derivatives')
        card = final.await_args.args[-1][0]
        self.assertEqual(card['source'], 'read_current_research')
        self.assertIn('"mean":20', card['text'])
        self.assertIn('2026-10-30', card['text'])

    async def test_social_reply_does_not_build_research_tables(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch('basketball_research.resolve_question', return_value={'question': 'Thanks',
                'original_question': 'Thanks', 'players': [], 'teams': []}))
            stack.enter_context(patch('basketball_research.canonical_history', return_value=[]))
            stack.enter_context(patch('basketball_research.answer_assignment_question', return_value=None))
            stack.enter_context(patch('matchup_queries.answer_matchup_question', return_value=None))
            stack.enter_context(patch('basketball_research.research_context', return_value={}))
            stack.enter_context(patch('grounded_reply.evidence_cards', return_value=[]))
            stack.enter_context(patch.object(app, 'generated_evidence_reply', AsyncMock(return_value={'answer': "You're welcome"})))
            builder = stack.enter_context(patch('refresh_export.build_export_tables'))
            result = await app.answer_question(SimpleNamespace(sheets={}), app.ChatRequest(message='Thanks'), {})
        builder.assert_not_called()
        self.assertEqual(result['answer'], "You're welcome")


if __name__ == '__main__':
    unittest.main()
