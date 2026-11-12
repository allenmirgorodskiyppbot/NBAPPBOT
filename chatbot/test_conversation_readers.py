"""Typed conversation reads preserve scope, arithmetic and strict decisions."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conversation_readers import CAPABILITIES, PLAN_CAPABILITIES, TOOL_NAMES, execute_read, validate_task
from feeds import canonical_game, canonical_player
from master import MasterStore
from season_calendar import import_calendar
from structured_availability import (normalize_official_pdf, build_availability_document,
                                     import_availability_document)

NOW = dt.datetime(2026, 11, 11, 19, tzinfo=dt.timezone.utc)
SOURCE = 'https://example.test/published-boxscores'


class ConversationReaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'master.sqlite')

    def add(self, day, *, player='Jalen Brunson', team='NYK', opponent='CLE',
            points=20, rebounds=4, assists=6, minutes=34, season='2026-27',
            phase='Regular Season', appearance='Played', status='final', player_id=None):
        game = canonical_game(day, team, opponent)
        self.store.upsert_game({'id': game, 'date': day, 'home_team': team, 'away_team': opponent,
            'status': status, 'home_score': 110, 'away_score': 100, 'source': SOURCE})
        self.store.upsert_player_game({'game_id': game, 'player_id': player_id or canonical_player(player),
            'player': player, 'team': team, 'opponent': opponent, 'date': day, 'season': season,
            'season_type': phase, 'appearance_state': appearance, 'pts': points,
            'reb': rebounds, 'ast': assists, 'minutes': minutes, 'fga': 18, 'tpa': 7, 'tpm': 2,
            'fta': 6, 'source': SOURCE})

    def read(self, tool, *, focus=None, **args):
        return execute_read(self.store, {'tool': tool, 'args': args}, focus=focus, now=NOW)

    def calendar(self):
        import_calendar(self.store, {'season': '2026-27', 'covered_teams': ['NYK'],
            'retrieved_at': '2026-10-30T02:00:00Z', 'source': 'https://www.nba.com/team/1610612752/schedule',
            'complete_for_published_games': False, 'games': [
                {'date': '2026-11-11', 'home_team': 'NYK', 'away_team': 'BOS',
                 'start_time': '2026-11-12T00:30:00Z', 'status': 'scheduled', 'source_asof': '2026-10-29'},
                {'date': '2026-11-14', 'home_team': 'CLE', 'away_team': 'NYK',
                 'start_time': '2026-11-15T01:00:00Z', 'status': 'scheduled', 'source_asof': '2026-10-29'}]})

    def snapshot(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def test_interface_is_small_and_rejects_code_or_unknown_arguments(self):
        self.assertLess(len(CAPABILITIES), 1200)
        self.assertLessEqual(len(PLAN_CAPABILITIES), 650)
        self.assertEqual(len(TOOL_NAMES), 8)
        for task in [None, [], {'tool': []}, {'tool': 'sql', 'args': {'query': 'SELECT 1'}},
                     {'tool': 'player_games', 'args': {'player': 'Jalen Brunson', 'sql': 'DROP TABLE games'}},
                     {'tool': 'player_games', 'args': {'window': True}},
                     {'tool': 'player_games', 'args': {'window': float('nan')}},
                     {'tool': 'player_games', 'args': {'player': lambda: None}},
                     {'tool': 'research_table', 'args': {'filters': {'player': {'query': 'x'}}}}]:
            with self.subTest(task=repr(task)):
                self.assertIsNone(validate_task(task))
                self.assertFalse(execute_read(self.store, task, now=NOW)['available'])

    def test_logs_calculate_only_observed_values_and_preserve_dates(self):
        self.add('2026-11-10', points=30)
        self.add('2026-11-09', points=None)
        self.add('2026-11-08', points=10)
        result = self.read('player_games', player='Brunson', stat='points', window=3)
        self.assertTrue(result['available'])
        self.assertEqual(result['args']['player'], 'Jalen Brunson')
        self.assertEqual(result['data']['summary']['mean'], 20)
        self.assertEqual(result['data']['summary']['missing'], 1)
        self.assertEqual(result['data']['summary']['observed'], 2)
        self.assertEqual([row['value'] for row in result['data']['games']], [30, None, 10])
        self.assertIn('never zero-filled', result['answer'])
        self.assertIn('2026-11-10', result['answer'])
        self.assertIn(SOURCE, result['sources'])
        self.assertFalse(result['data']['complete_coverage_verified'])

    def test_combinations_require_every_component_per_game(self):
        self.add('2026-11-10', points=20, rebounds=None, assists=5)
        self.add('2026-11-09', points=10, rebounds=4, assists=3)
        result = self.read('player_games', player='Jalen Brunson', stat='p+r', window=2)
        self.assertEqual(result['args']['stat'], 'pr')
        self.assertEqual(result['data']['summary']['mean'], 14)
        self.assertEqual(result['data']['summary']['observed'], 1)
        self.assertEqual(result['data']['summary']['missing'], 1)

    def test_small_requested_window_retains_full_matching_stored_history_count_and_extent(self):
        for index in range(20):
            self.add((dt.date(2026, 11, 10) - dt.timedelta(days=index)).isoformat(), opponent='BOS',
                     points=None if index == 0 else 20)
        self.add('2026-10-01', opponent='CLE')
        self.add('2025-11-01', opponent='MIL', season='2025-26')
        result = self.read('player_games', player='Jalen Brunson', stat='points', opponent='Boston', window=5)
        self.assertEqual(result['data']['game_count'], 5)
        self.assertEqual(result['data']['summary']['observed'], 4)
        self.assertEqual(result['data']['summary']['missing'], 1)
        self.assertEqual(result['data']['total_matching_saved_games'], 20)
        self.assertEqual(result['data']['matching_first_date'], '2026-10-22')
        self.assertEqual(result['data']['matching_last_date'], '2026-11-10')
        self.assertIn('20 matching stored games played against BOS', result['answer'])
        self.assertIn('not verified complete career history', result['answer'])
        facts = [fact for card in result['cards'] for fact in card['approved_facts']]
        coverage = [fact for fact in facts if 'master holds' in fact['text']]
        self.assertEqual(coverage[0]['values'], [20])
        self.assertEqual(coverage[0]['metric'], 'games')
        missing = self.read('player_games', player='Jalen Brunson', stat='points', opponent='DET', window=5)
        self.assertEqual(missing['data']['total_matching_saved_games'], 0)
        self.assertIsNone(missing['data']['summary']['mean'])
        self.assertIsNone(missing['data']['matching_first_date'])

    def test_dnp_unknown_appearance_zero_minutes_and_nonfinal_rows_are_excluded(self):
        self.add('2026-11-10', points=17)
        self.add('2026-11-09', points=0, minutes=0, appearance='DNP')
        self.add('2026-11-08', points=50, appearance='Unknown')
        self.add('2026-11-07', points=50, minutes=0)
        self.add('2026-11-06', points=50, status='scheduled')
        self.add('2026-11-12', points=50)
        result = self.read('player_games', player='Jalen Brunson', stat='points', window=100)
        self.assertEqual(result['data']['game_count'], 1)
        self.assertEqual(result['data']['summary']['mean'], 17)

    def test_career_opponent_scope_is_stored_mixed_season_history_not_lifetime_claim(self):
        self.add('2026-11-10', points=30)
        self.add('2025-11-10', points=20, team='DAL', season='2025-26')
        self.add('2024-11-10', points=10, team='DAL', season='2024-25', opponent='MIL')
        self.add('2026-05-10', points=99, season='2025-26', phase='Playoffs')
        result = self.read('player_games', player='Jalen Brunson', stat='points',
                           opponent='Cleveland Cavaliers', window='career')
        self.assertEqual(result['data']['game_count'], 2)
        self.assertEqual(result['data']['summary']['mean'], 25)
        self.assertEqual(result['data']['summary']['total'], 50)
        self.assertEqual(result['args']['opponent'], 'CLE')
        self.assertIn('All stored history', result['answer'])
        self.assertIn('2025-26, 2026-27', result['scope'])
        self.assertIn('DAL, NYK', result['scope'])
        self.assertIn('unverified', result['scope'])

    def test_phase_and_season_filters_do_not_silently_broaden(self):
        self.add('2026-11-10', points=30)
        self.add('2026-05-10', points=99, season='2025-26', phase='Playoffs')
        result = self.read('player_games', player='Jalen Brunson', stat='points', window=5,
                           season='2025-26', phase='Playoffs')
        self.assertEqual(result['data']['summary']['mean'], 99)
        absent = self.read('player_games', player='Jalen Brunson', stat='points', season='2024-25')
        self.assertFalse(absent['available'])
        self.assertIn('missing saved coverage', absent['answer'])
        for args in [{'window': 101}, {'window': -1}, {'season': '2025-99'}, {'phase': 'anything'}]:
            self.assertFalse(self.read('player_games', player='Jalen Brunson', stat='points', **args)['available'])

    def test_ambiguous_surnames_and_conflicting_full_identities_do_not_merge(self):
        self.add('2026-11-10', player='Jalen Brunson')
        self.add('2026-11-09', player='Rick Brunson')
        result = self.read('player_games', player='Brunson', stat='points')
        self.assertFalse(result['available'])
        self.assertIn('unique', result['answer'])
        self.add('2026-11-08', player='Test Name', player_id='provider-one')
        self.add('2026-11-07', player='Test Name', player_id='provider-two')
        self.assertFalse(self.read('player_games', player='Test Name', stat='points')['available'])

    def test_all_missing_stat_is_unknown_instead_of_zero_average(self):
        self.add('2026-11-10', points=None)
        result = self.read('player_games', player='Jalen Brunson', stat='points')
        self.assertFalse(result['available'])
        self.assertIsNone(result['data']['summary']['mean'])
        self.assertEqual(result['data']['summary']['observed'], 0)

    def test_additional_source_boxscore_field_is_available_without_new_phrase_handler(self):
        self.add('2026-11-10')
        result = self.read('player_games', player='Jalen Brunson', stat='free throw attempts')
        self.assertEqual(result['args']['stat'], 'fta')
        self.assertEqual(result['data']['summary']['mean'], 6)

    def test_profile_shares_selected_games_and_labels_independent_missing_denominators(self):
        self.add('2026-11-10', rebounds=None, minutes=None)
        self.add('2026-11-09', rebounds=8, minutes=30)
        result = self.read('player_profile', player='Jalen Brunson', window=2)
        self.assertEqual(result['data']['game_count'], 2)
        self.assertEqual(result['data']['summary']['pts']['observed'], 2)
        self.assertEqual(result['data']['summary']['reb']['observed'], 1)
        self.assertEqual(result['data']['summary']['minutes']['mean'], 30)
        self.assertIn('current roster', result['scope'])
        self.assertLessEqual(len(result['cards']), 2)

    def test_broad_mixed_season_profile_keeps_complete_source_qualifier_in_short_answer(self):
        for year in range(2007, 2027):
            self.add(f'{year}-11-10', player='Karl-Anthony Towns', season=f'{year}-{str(year + 1)[-2:]}')
        result = self.read('player_profile', player='Karl-Anthony Towns', window=20)
        self.assertLessEqual(len(result['answer']), 1000)
        self.assertTrue(result['answer'].endswith('Stored teams do not confirm a current roster.'))
        self.assertIn('through 2026-11-10', result['answer'])

    def test_approved_numeric_facts_do_not_let_counts_become_averages_or_offense_become_defense(self):
        from grounded_reply import ReplyValidationError, render_natural_reply
        self.add('2026-11-10', points=20)
        self.add('2026-11-09', points=20)
        profile = self.read('player_profile', player='Jalen Brunson', window=2)
        cards = profile['cards']
        document = {'evidence_ids': [cards[0]['id']], 'analysis': 'Jalen Brunson averaged 20 points in the saved sample.', 'needs_more_data': False}
        self.assertTrue(render_natural_reply(document, cards))
        document['analysis'] = 'Jalen Brunson averaged 2 points in the saved sample.'
        with self.assertRaises(ReplyValidationError):
            render_natural_reply(document, cards)
        team = self.read('team_games', team='NYK')
        cards = team['cards']
        document = {'evidence_ids': [cards[0]['id']], 'analysis': 'NYK averaged 110 points scored per game in the saved sample.', 'needs_more_data': False}
        self.assertTrue(render_natural_reply(document, cards))
        document['analysis'] = 'NYK allowed an average of 100 points per game in the saved sample.'
        self.assertTrue(render_natural_reply(document, cards))
        document['analysis'] = 'NYK averaged 100 points scored per game in the saved sample.'
        with self.assertRaises(ReplyValidationError):
            render_natural_reply(document, cards)

    def test_team_games_uses_actual_team_side_and_saved_sources(self):
        self.add('2026-11-10', team='CLE', opponent='NYK')
        self.add('2026-11-09', team='NYK', opponent='MIL')
        result = self.read('team_games', team='New York Knicks', window=2)
        self.assertEqual(result['data']['summary']['wins'], 1)
        self.assertEqual(result['data']['summary']['losses'], 1)
        self.assertEqual(result['data']['summary']['points_for_average'], 105)
        self.assertIn('inventory is unverified', result['scope'])

    def test_calendar_lookup_retains_saved_timing_qualification_and_explicit_anchor(self):
        self.calendar()
        result = self.read('next_game')
        self.assertTrue(result['available'])
        self.assertIn('BOS at NYK', result['answer'])
        self.assertIn('current timing is unverified', result['answer'])
        self.assertFalse(result['data']['next_game_focus']['timing_verified'])
        later = self.read('next_game', team='Knicks', date='2026-11-14')
        self.assertIn('on/after 2026-11-14', later['answer'])
        self.assertIn('NYK at CLE', later['answer'])
        self.assertFalse(self.read('next_game', date='2026-02-30')['available'])

    def test_next_game_after_date_excludes_the_focused_game_day(self):
        self.calendar()
        inclusive = self.read('next_game', date='2026-11-11')
        self.assertEqual(inclusive['data']['next_game_focus']['date'], '2026-11-11')
        later = self.read('next_game', after_date='2026-11-11', focus={'game_date': '2026-11-11'})
        self.assertEqual(later['data']['next_game_focus']['date'], '2026-11-14')
        self.assertEqual(later['args']['after_date'], '2026-11-11')
        self.assertNotIn('date', later['args'])
        self.assertIn('strictly after 2026-11-11', later['answer'])
        conflict = self.read('next_game', date='2026-11-11', after_date='2026-11-11')
        self.assertFalse(conflict['available'])
        self.assertIn('not both', conflict['answer'])

    def test_next_game_after_date_handles_new_year_rollover_and_rejects_bad_dates(self):
        import_calendar(self.store, {'season': '2026-27', 'covered_teams': ['NYK'],
            'retrieved_at': '2026-10-30T02:00:00Z', 'source': 'https://www.nba.com/team/1610612752/schedule',
            'complete_for_published_games': False, 'games': [
                {'date': '2026-12-31', 'home_team': 'NYK', 'away_team': 'BOS',
                 'start_time': '2027-01-01T01:00:00Z', 'status': 'scheduled', 'source_asof': '2026-10-29'},
                {'date': '2027-01-01', 'home_team': 'CLE', 'away_team': 'NYK',
                 'start_time': '2027-01-02T01:00:00Z', 'status': 'scheduled', 'source_asof': '2026-10-29'}]})
        later = self.read('next_game', after_date='2026-12-31')
        self.assertEqual(later['data']['next_game_focus']['date'], '2027-01-01')
        self.assertIn('starting 2027-01-01', later['answer'])
        self.assertFalse(self.read('next_game', after_date='2026-02-30')['available'])
        self.assertFalse(self.read('next_game', after_date='9999-12-31')['available'])

    def test_season_schedule_lists_saved_fixtures_with_exact_bounds_and_pagination(self):
        self.calendar()
        result = self.read('schedule', limit=1)
        self.assertEqual(result['data']['season'], '2026-27')
        self.assertEqual(result['data']['matched_rows'], 2)
        self.assertEqual(result['data']['returned_rows'], 1)
        self.assertEqual(result['data']['next_start_date'], '2026-11-14')
        self.assertIn('BOS at NYK', result['answer'])
        self.assertIn('7:30 PM Eastern', result['answer'])
        self.assertIn('current timing unverified', result['answer'])
        bounded = self.read('schedule', team='Knicks', start_date='2026-11-14', end_date='2026-11-14')
        self.assertEqual(bounded['data']['matched_rows'], 1)
        self.assertIn('NYK at CLE', bounded['answer'])
        self.assertFalse(self.read('schedule', team='CLE')['available'])
        self.assertFalse(self.read('schedule', start_date='2026-11-14', end_date='2026-11-01')['available'])
        self.assertFalse(self.read('schedule', limit=21)['available'])

    def test_next_game_retains_both_teams_form_as_separate_grounded_entities(self):
        self.calendar()
        self.add('2026-11-10', team='NYK', opponent='CLE')
        self.add('2026-11-09', team='BOS', opponent='MIL')
        result = self.read('next_game')
        self.assertEqual(len(result['cards']), 3)
        self.assertEqual({card['entity'] for card in result['cards'] if card['kind'] == 'upcoming_team_form'}, {'NYK', 'BOS'})
        for card in result['cards'][1:]:
            self.assertTrue(all(fact['entity'] == card['entity'] for fact in card['approved_facts']))
            self.assertIn('sample completeness is not established', card['approved_facts'][0]['scope'])

    def test_missing_availability_is_unknown_and_does_not_claim_healthy_or_normal_minutes(self):
        self.add('2026-11-10', player='Karl-Anthony Towns')
        result = self.read('availability', player='Karl-Anthony Towns')
        self.assertTrue(result['available'])
        self.assertFalse(result['data']['official_designation_known'])
        self.assertIn('Status is unknown', result['answer'])
        self.assertIsNone(result['data']['expected_minutes'])
        self.assertTrue(result['missing'])
        self.assertNotIn('schedule within 15', result['answer'])

    def test_fresh_official_status_has_authority_date_and_clock_without_minutes_inference(self):
        self.add('2026-11-10', player='Karl-Anthony Towns')
        official = normalize_official_pdf([{'game_date': '2026-11-11', 'away_team': 'BOS',
            'home_team': 'NYK', 'team': 'NYK', 'player': 'Karl-Anthony Towns',
            'status': 'Questionable', 'reason': 'Right knee soreness'}],
            source_url='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-11_01_30PM.pdf',
            report_at='2026-11-11T13:30:00-05:00', text_sha256='b' * 64, now=NOW)
        import_availability_document(self.store, build_availability_document(official, now=NOW), now=NOW)
        result = self.read('availability', player='Karl-Anthony Towns', date='2026-11-11')
        self.assertTrue(result['data']['official_designation_known'])
        self.assertEqual(result['data']['official_designation']['status'], 'Questionable')
        self.assertIn('reported 2026-11-11T13:30:00-05:00', result['answer'])
        self.assertTrue(result['data']['blocked'])
        self.assertIsNone(result['data']['confirmed_starter'])
        self.assertIn('unrestricted minutes', result['answer'])

    def test_prop_decision_preserves_strict_pass_despite_high_saved_average(self):
        for index in range(20):
            day = (dt.date(2026, 11, 10) - dt.timedelta(days=index)).isoformat()
            self.add(day, points=40)
        result = self.read('prop_assessment', player='Jalen Brunson', stat='points', line=15.5)
        self.assertEqual(result['decision'], 'pass')
        self.assertEqual(result['data']['decision'], 'pass')
        self.assertIn('Pass', result['answer'])
        self.assertEqual(result['cards'][-1]['decision'], 'pass')
        self.assertFalse(result['data']['offer_verified'])

    def test_focused_same_prop_inherits_slots_but_changed_market_needs_its_own_line(self):
        self.add('2026-11-10', player='Karl-Anthony Towns')
        focus = {'player': 'Karl-Anthony Towns', 'stat': 'pra', 'line': 15.5, 'game_date': '2026-11-11'}
        inherited = self.read('prop_assessment', focus=focus)
        self.assertEqual(inherited['args']['line'], 15.5)
        self.assertEqual(inherited['args']['stat'], 'pra')
        changed = self.read('prop_assessment', focus=focus, stat='p+r')
        self.assertFalse(changed['available'])
        self.assertIn('numeric offered line', changed['answer'])
        explicit = self.read('prop_assessment', focus=focus, stat='p+r', line=15.5)
        self.assertEqual(explicit['args']['stat'], 'pr')
        other = self.read('prop_assessment', focus=focus, player='Jalen Brunson', stat='points')
        self.assertFalse(other['available'])

    def test_research_filters_cannot_accept_sql_columns_or_broaden_scope(self):
        self.add('2026-11-10')
        bad = self.read('research_table', table='Player_Metrics', filters={'where': '1=1'})
        self.assertFalse(bad['available'])
        self.assertIn('Supported filters', bad['answer'])
        bad_table = self.read('research_table', table='games; DROP TABLE games', filters={})
        self.assertFalse(bad_table['available'])
        self.assertFalse(self.read('research_table', table='Player_Metrics', filters={})['available'])
        missing = self.read('research_table', table='Player_Metrics', filters={'player': 'Nobody Here'})
        self.assertFalse(missing['available'])

    def test_targeted_derivatives_match_existing_export_without_rebuilding_every_table(self):
        from current_research import read_current_research
        self.add('2026-11-10', points=30)
        self.add('2026-11-09', points=10)
        self.add('2026-11-08', player='Donovan Mitchell', team='CLE', opponent='NYK')
        records = {
            'splits': [{'split_id': 's', 'player': 'Jalen Brunson', 'window': 'Available career',
                        'season_type': 'Regular Season', 'definition': 'Home/Away', 'filter': 'Home', 'statistic': 'pts'}],
            'correlations': [{'player': 'Jalen Brunson', 'other_player': 'Jalen Brunson', 'x': 'pts', 'y': 'ast',
                              'window': 'Available career', 'season_type': 'Regular Season', 'relation': 'Within player'}],
            'pair_summaries': [{'assignment_id': 'a', 'direction': 'Knicks offense', 'player': 'Jalen Brunson',
                                'named_player': 'Donovan Mitchell', 'role': 'primary', 'window': 'Available career', 'season_type': 'Regular Season'}],
            'assignments': [{'assignment_id': 'a', 'selected_player': 'Jalen Brunson', 'opponent_team': 'CLE',
                             'primary': 'Donovan Mitchell', 'assignment_asof': '2026-10-29'}],
            'rest_schedule': [{'team': 'NYK', 'date': '2026-11-10', 'rest_days': 1, 'rest_bucket': '1'}],
            'measured_pairs': [{'game_id': canonical_game('2026-11-10', 'NYK', 'CLE'), 'date': '2026-11-10',
                                'offense_player': 'Jalen Brunson', 'defense_player': 'Donovan Mitchell',
                                'offense_team': 'NYK', 'defense_team': 'CLE', 'season': '2026-27',
                                'season_type': 'Regular Season', 'matchup_seconds': 25, 'source': SOURCE}],
        }
        tracking = {'entity': 'Jalen Brunson', 'category': 'Passing', 'season': '2026-27',
                    'season_type': 'Regular Season', 'gp': 2, 'fields': {'potential_assists': 20},
                    'data_through_date': None, 'published_at': '2026-11-11T15:00:00Z', 'source': SOURCE}
        with self.store.connect() as db:
            db.execute('CREATE TABLE workbook_evidence(kind TEXT,entity TEXT,payload_json TEXT)')
            for kind, rows in records.items():
                db.executemany('INSERT INTO workbook_evidence VALUES(?,?,?)',
                               [(kind, row.get('player', ''), json.dumps(row)) for row in rows])
            db.execute('INSERT INTO refreshed_tracking VALUES(?,?,?,?)',
                       ('2026-27', 'Jalen Brunson', 'Passing', json.dumps(tracking)))
        cases = [('Player_Metrics', {'player': 'Jalen Brunson', 'statistic': 'pts'}),
                 ('Calculated_Splits', {'player': 'Jalen Brunson'}),
                 ('Correlations', {'player': 'Jalen Brunson'}), ('Pair_Summaries', {'player': 'Jalen Brunson'}),
                 ('Tracking_Metrics', {'entity': 'Jalen Brunson'}),
                 ('Measured_Pair_Metrics', {'offense_player': 'Jalen Brunson', 'metric': 'matchup_seconds'}),
                 ('Matchup_Assignments', {'selected_player': 'Jalen Brunson'}), ('Team_Schedule', {'team': 'NYK'}),
                 ('Refresh_Coverage', {})]
        for table, filters in cases:
            expected = read_current_research(self.store, table, filters, limit=10, now=NOW)
            with self.subTest(table=table), patch('refresh_export.build_export_tables', side_effect=AssertionError('full export invoked')):
                actual = self.read('research_table', table=table, filters=filters, limit=10)
                self.assertEqual(actual['data']['rows'], expected['rows'])
                self.assertEqual(actual['data']['matched_rows'], expected['matched_rows'])
        self.assertIn('"mean":20.0', self.read('research_table', table='Player_Metrics',
                                     filters={'player': 'Jalen Brunson', 'statistic': 'pts'})['answer'])
        self.add('2026-11-10', points=50)
        changed = self.read('research_table', table='Player_Metrics', filters={'player': 'Jalen Brunson', 'statistic': 'pts'})
        self.assertEqual(changed['data']['rows'][0]['mean'], 30)

    def test_all_supported_reads_are_read_only_and_small(self):
        self.add('2026-11-10')
        self.calendar()
        before = self.snapshot()
        tasks = [('player_games', {'player': 'Jalen Brunson', 'stat': 'points'}),
                 ('player_profile', {'player': 'Jalen Brunson'}), ('team_games', {'team': 'NYK'}),
                 ('next_game', {}), ('schedule', {}), ('availability', {'player': 'Jalen Brunson'}),
                 ('prop_assessment', {'player': 'Jalen Brunson', 'stat': 'points', 'line': 20.5}),
                 ('research_table', {'table': 'Refresh_Coverage', 'filters': {}, 'limit': 1})]
        for tool, args in tasks:
            result = self.read(tool, **args)
            self.assertLessEqual(len(result['answer']), 1000)
            self.assertLessEqual(len(result['cards']), 2)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
