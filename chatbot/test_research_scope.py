"""Explicit populations and source dates for generative basketball explanations."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from basketball_research import research_context
from feeds import canonical_player
from master import MasterStore
from workbook import WorkbookStore


class ResearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Matchup_Assignments'
        for _ in range(6):
            sheet.append([None])
        sheet.append(['assignment_id', 'selected_player', 'opponent_team', 'primary', 'alternate',
                      'assignment_asof', 'assignment_status'])
        sheet.append(['A1', 'OG Anunoby', 'CHA', "Royce O'Neale", 'Grant Williams',
                      '2026-10-22T12:00:00', 'Tentative starter matchup'])
        sheet.append(['A2', 'OG Anunoby', 'CHA', 'Historical Primary', 'Historical Alternate',
                      '2025-10-22T12:00:00', 'Tentative starter matchup'])
        profile = book.create_sheet('OG Anunoby')
        for _ in range(6):
            profile.append([None])
        profile.append(['season', 'games', 'ppg'])
        profile.append(['2026-27', 4, 16.25])
        path = root / 'fixture.xlsx'
        book.save(path); book.close()
        self.workbook = WorkbookStore(str(path), str(root / 'workbook.sqlite'))
        self.master = MasterStore(root / 'master.sqlite')
        self.add_game('current', '2026-10-27', 'BOS', 20)
        self.add_game('historical', '2026-03-26', 'CHA', 17, season='2025-26')
        self.add_game('dnp', '2026-10-26', 'CHA', 99, appearance='DNP')
        self.add_game('live', '2026-10-28', 'CHA', 99, state='live')
        self.add_game('playoff', '2026-10-25', 'CHA', 99, phase='Playoffs')
        self.add_record('OG Anunoby', 'Player totals', {'pts':88, 'reb':31, 'ast':13,
                        'minutes':166.8166666667, 'fgm':26, 'fga':65})
        self.add_record('CHA', 'Team totals', {'pts':505, 'fgm':182, 'fga':436})
        self.add_record('CHA', 'Team defense', {'points_allowed':541, 'defended_fgm':196,
                        'defended_fga':422, 'fg_pct_allowed':196/422})

    def add_game(self, identity, day, opponent, points, season='2026-27', phase='Regular Season',
                 appearance='Played', state='final', rebounds=6):
        self.master.upsert_game({'id':identity, 'date':day, 'home_team':'NYK',
                                'away_team':opponent, 'status':state})
        self.master.upsert_player_game({'game_id':identity, 'date':day, 'player':'OG Anunoby',
            'team':'NYK', 'opponent':opponent, 'season':season, 'season_type':phase,
            'appearance_state':appearance, 'pts':points, 'reb':rebounds, 'ast':2, 'minutes':30,
            'source':'https://example.com/games/'+identity})

    def add_record(self, entity, category, fields, season='2026-27', phase='Regular Season'):
        record = {'entity':entity, 'player_id':canonical_player(entity), 'category':category,
            'season':season, 'season_type':phase, 'gp':5, 'fields':fields,
            'data_through_date':'2026-10-28', 'published_at':'2026-10-29T14:18:20+00:00',
            'source':'https://example.com/published/'+category.replace(' ', '-')}
        with self.master.connect() as db:
            db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                       (season, entity, category, json.dumps(record)))

    def context(self, text='Explain OG Anunoby scoring against the Hornets'):
        resolved = {'players':['OG Anunoby'], 'teams':['CHA'], 'question':text, 'original_question':text}
        return research_context(self.workbook, self.master, resolved)

    def test_all_opponent_average_keeps_population_and_two_distinct_dates(self):
        context = self.context()
        row = context['current_player_statistics'][0]
        self.assertEqual((row['ppg'], row['rpg'], row['apg']), (17.6, 6.2, 2.6))
        self.assertEqual(row['population'], 'all_opponents')
        self.assertEqual(row['games'], 5)
        self.assertEqual(row['data_through_date'], '2026-10-28')
        self.assertEqual(row['published_at'], '2026-10-29T14:18:20+00:00')
        self.assertTrue(row['source'].endswith('/Player-totals'))
        self.assertNotIn('Player totals', [r['category'] for r in context['current_tracking']])

    def test_empty_current_opponent_population_does_not_borrow_history_or_live_dnp(self):
        row = self.context()['opponent_player_statistics'][0]
        self.assertEqual(row['population'], 'player_vs_team_whole_games')
        self.assertEqual((row['season'], row['season_type'], row['opponent']), ('2026-27', 'Regular Season', 'CHA'))
        self.assertEqual(row['n'], 0)
        self.assertIsNone(row['averages']['pts'])
        self.assertIsNone(row['data_through_date'])
        self.assertEqual(row['source_records'], [])
        self.assertIn('does not prove', row['coverage_note'])

    def test_whole_game_opponent_sample_counts_observed_values_and_sources(self):
        self.add_game('cha1', '2026-10-21', 'CHA', 10, rebounds=None)
        self.add_game('cha2', '2026-10-24', 'CHA', 30, rebounds=8)
        row = self.context()['opponent_player_statistics'][0]
        self.assertEqual(row['n'], 2)
        self.assertEqual((row['observed_counts']['pts'], row['observed_counts']['reb']), (2, 1))
        self.assertEqual((row['averages']['pts'], row['averages']['reb']), (20, 8))
        self.assertEqual((row['first_date'], row['data_through_date']), ('2026-10-21', '2026-10-24'))
        self.assertEqual({r['game_id'] for r in row['source_records']}, {'cha1', 'cha2'})
        self.assertFalse(row['source_records_truncated'])

    def test_historical_request_never_prefills_current_totals_or_assignment(self):
        for season_text in ('2025-26', '2025–2026'):
            with self.subTest(season=season_text):
                context = self.context('Explain OG against Charlotte in '+season_text)
                self.assertEqual(context['requested_population']['seasons'], ['2025-26'])
                self.assertTrue(all(r['season']=='2025-26' for r in context['current_player_statistics']))
                self.assertEqual(context['current_player_statistics'][0]['ppg'], 17)
                self.assertIsNone(context['current_player_statistics'][0]['published_at'])
                self.assertEqual(context['opponent_player_statistics'][0]['n'], 1)
                self.assertEqual(context['assignment_projections'][0]['primary'], 'Historical Primary')
                self.assertEqual(context['player_sheets'], [])
                self.assertEqual(context['current_tracking'], [])

    def test_explicit_phase_is_not_replaced_by_regular_season_publication(self):
        context = self.context('Explain OG against Charlotte in the 2026-27 playoffs')
        self.assertEqual(context['requested_population']['season_type'], 'Playoffs')
        self.assertEqual(context['current_player_statistics'][0]['ppg'], 99)
        self.assertEqual(context['opponent_player_statistics'][0]['n'], 1)
        self.assertEqual(context['current_tracking'], [])
        self.assertEqual(context['assignment_projections'], [])

    def test_team_defense_is_distinct_from_production_and_assignment_direction_unknown(self):
        context = self.context()
        records = {r['category']:r for r in context['current_tracking']}
        self.assertEqual(records['Team defense']['fields']['points_allowed'], 541)
        self.assertEqual(records['Team totals']['fields']['pts'], 505)
        self.assertNotEqual(records['Team defense']['population'], records['Team totals']['population'])
        assignment = context['assignment_projections'][0]
        self.assertEqual(assignment['assignment_date'], '2026-10-22')
        self.assertEqual(assignment['assignment_direction'], 'unspecified')
        self.assertFalse(assignment['verified_game_day_assignment'])
        self.assertEqual(assignment['population'], 'tentative_assignment')

    def test_oversized_optional_evidence_cannot_displace_requested_assignment(self):
        for category in ('Passing', 'Rebounding', 'Possessions', 'Rim'):
            self.add_record('OG Anunoby', category, {'fixture_detail_'+str(i):i for i in range(180)})
        with patch.object(self.workbook, 'read', return_value={'sheet':'OG Anunoby', 'value':'x'*10000}):
            context = self.context()
        self.assertLessEqual(len(json.dumps(context)), 5500)
        self.assertEqual(context['assignment_projections'][0]['primary'], "Royce O'Neale")
        self.assertEqual(context['opponent_player_statistics'][0]['n'], 0)
        self.assertEqual(context['current_player_statistics'][0]['population'], 'all_opponents')
        self.assertIn('Team defense', [r['category'] for r in context['current_tracking']])
        self.assertTrue(context.get('omitted_evidence'))


if __name__ == '__main__':
    unittest.main()
