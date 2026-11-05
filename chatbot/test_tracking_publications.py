"""Measured defending populations, cumulative units and source dates stay exact."""
import copy
import datetime as dt
import unittest

from tracking_publications import game_date_inventory, measured_matchups, normalize_hustle

NOW = dt.datetime(2026, 11, 5, 15, tzinfo=dt.timezone.utc)


class TrackingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.scope = {'players': [{'player': 'Jalen Brunson', 'nba_player_id': '1628973'},
                                  {'player': 'Tyrese Maxey', 'nba_player_id': '1630178'}],
                      'teams': ['NYK', 'PHI']}
        self.pairs = {'pairs': [{'offense_player': 'Jalen Brunson', 'defense_player': 'Tyrese Maxey'}],
                      'team_ids': {'1610612752': 'NYK', '1610612755': 'PHI'}}
        self.schedule = [self.game('0022600001', '2026-10-20'), self.game('0022600002', '2026-10-23')]
        self.inventory, _ = game_date_inventory(self.schedule, [], '2026-27',
            mapping_sources=[{'source': 'https://example.test/NBA-results', 'source_content_sha256': 'b' * 64}])

    def game(self, key, day, **changes):
        return {'game_id': key, 'season': 2027, 'season_type': 'regular-season', 'game_date': day,
                'home_team_abbreviation': 'NYK', 'away_team_abbreviation': 'PHI',
                'home_pts': 110, 'away_pts': 100, 'home_wl': 'W', 'away_wl': 'L', **changes}

    def row(self, key='0022600001', **changes):
        return {'game_id': key, 'season': 2027, 'season_type_id': '2',
                'off_person_id': 1628973, 'off_first_name': 'Jalen', 'off_family_name': 'Brunson',
                'def_person_id': 1630178, 'def_first_name': 'Tyrese', 'def_family_name': 'Maxey',
                'off_team_id': 1610612752, 'off_team_tricode': 'NYK', 'def_team_id': 1610612755,
                'matchup_minutes': '1:30', 'partial_possessions': 5.5, 'player_points': 5,
                'team_points': 7, 'matchup_field_goals_made': 2, 'matchup_field_goals_attempted': 4,
                'matchup_three_pointers_made': 1, 'matchup_three_pointers_attempted': 2,
                'matchup_free_throws_made': 0, 'matchup_free_throws_attempted': 0,
                'matchup_assists': 1, 'matchup_turnovers': 0, 'shooting_fouls': 0,
                'matchup_potential_assists': 2, 'matchup_blocks': 0,
                'help_blocks': 0, 'help_field_goals_made': 0, 'help_field_goals_attempted': 1,
                'switches_on': 1, **changes}

    def normalize(self, rows, **changes):
        return measured_matchups(rows, self.inventory, self.scope, self.pairs, '2026-27',
            source='https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_game_matchups/game_matchups_2027.parquet',
            published_at='2026-10-28T15:00:00Z', source_hash='a' * 64, now=NOW, **changes)

    def test_hustle_uses_G_and_only_cumulative_counting_fields(self):
        source = {'player_id': 1628973, 'player_name': 'Jalen Brunson', 'season': 2027,
            'season_type': 'regular-season', 'per_mode': 'totals', 'g': 4, 'min': 120,
            'contested_shots': 8, 'screen_assists': 3, 'screen_ast_pts': 6,
            'deflections': 10, 'off_boxouts': 1, 'pct_box_outs_off': .4}
        row = normalize_hustle([source], self.scope, '2026-27')[0]
        self.assertEqual(row['gp'], 4)
        self.assertEqual(row['fields']['deflections'], 10)
        self.assertEqual(row['fields']['screen_assist_points'], 6)
        self.assertNotIn('pct_box_outs_off', row['fields'])
        self.assertEqual(normalize_hustle([{**source, 'per_mode': 'pergame'}], self.scope, '2026-27'), [])
        self.assertEqual(normalize_hustle([{**source, 'season_type': 'playoffs'}], self.scope, '2026-27'), [])

    def test_actual_pair_games_sum_once_and_minutes_are_minutes(self):
        records, measured, report = self.normalize([self.row(), self.row('0022600002', matchup_minutes='0:30')])
        self.assertEqual(len(measured), 2)
        row = records[0]
        self.assertEqual(row['gp'], 2)
        self.assertEqual(row['fields']['matchup_minutes'], 2)
        self.assertEqual(row['fields']['partial_possessions'], 11)
        self.assertEqual(row['fields']['fga'], 8)
        self.assertEqual(row['fields']['fg_pct'], .5)
        self.assertEqual(row['data_through_date'], '2026-10-23')
        self.assertEqual(row['first_observed_game_date'], '2026-10-20')
        self.assertEqual(row['coverage_basis'], 'verified_game_inventory')
        self.assertEqual(measured[0]['matchup_seconds'], 90)
        self.assertEqual(report['measured_pair_games'], 2)

    def test_identical_source_duplicate_cannot_double_pair_GP_or_shots(self):
        row = self.row()
        records, measured, report = self.normalize([row, copy.deepcopy(row)])
        self.assertEqual(records[0]['gp'], 1)
        self.assertEqual(records[0]['fields']['fga'], 4)
        self.assertEqual(len(measured), 1)
        self.assertEqual(report['held_rows']['identical_duplicate_pair_game'], 1)

    def test_conflicting_duplicate_pair_game_is_excluded(self):
        records, measured, report = self.normalize([self.row(), self.row(matchup_assists=99), self.row('0022600002')])
        self.assertEqual(records[0]['gp'], 1)
        self.assertEqual(measured[0]['game_id'], '0022600002')
        self.assertEqual(report['held_rows']['conflicting_duplicate_pair_game'], 1)

    def test_missing_one_observed_stat_never_becomes_an_aggregate_zero(self):
        records, _, _ = self.normalize([self.row(), self.row('0022600002', matchup_assists=None)])
        self.assertNotIn('ast', records[0]['fields'])
        self.assertEqual(records[0]['fields']['fga'], 8)

    def test_wrong_team_unknown_date_reused_person_and_bad_shots_are_held(self):
        for changes in [{'game_id': '0022600999'}, {'def_team_id': 1610612747},
                        {'off_first_name': 'Different'}, {'matchup_field_goals_made': 5},
                        {'off_person_id': None}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.normalize([self.row(**changes)])[0], [])

    def test_season_or_phase_does_not_leak_between_populations(self):
        self.assertEqual(self.normalize([self.row(season=2026)])[0], [])
        self.assertEqual(self.normalize([self.row(season_type_id='4')])[0], [])
        playoff_inventory,_=game_date_inventory([self.game('0042600001','2026-10-20',season_type='playoffs')],[],
                                               '2026-27',season_type='Playoffs')
        records, _, _ = measured_matchups([self.row('0042600001',season_type_id='4')], playoff_inventory,
            self.scope, self.pairs, '2026-27', source='fixture', published_at='2026-10-28T15:00:00Z',
            source_hash='a' * 64, now=NOW, season_type='Playoffs')
        self.assertEqual(records[0]['season_type'], 'Playoffs')
        self.assertEqual(measured_matchups([self.row('0042600001',season_type_id='3')],playoff_inventory,
            self.scope,self.pairs,'2026-27',source='fixture',published_at='2026-10-28T15:00:00Z',
            source_hash='a'*64,now=NOW,season_type='Playoffs')[0],[])

    def test_missing_exposure_or_zero_evidence_cannot_establish_guarding(self):
        self.assertEqual(self.normalize([self.row(matchup_minutes='bad')])[0], [])
        zero = {key: 0 for key in self.row() if key.startswith(('matchup_', 'help_', 'partial_', 'player_points', 'team_points', 'shooting_', 'switches_'))}
        zero['matchup_minutes'] = '0:00'
        self.assertEqual(self.normalize([self.row(**zero)])[0], [])

    def test_official_date_sources_must_agree_and_scores_must_be_completed(self):
        conflicting = self.game('0022600001', '2026-10-21')
        future = self.game('0022600003', '2026-11-20', home_pts=None, away_pts=None, home_wl=None, away_wl=None)
        invalid = self.game('0022600004', '2025-10-20')
        fractional = self.game('0022600005', '2026-10-20', home_pts=110.5)
        result, report = game_date_inventory(self.schedule + [conflicting, future, invalid, fractional], [], '2026-27')
        self.assertNotIn('0022600001', result)
        self.assertEqual(set(result), {'0022600002'})
        self.assertEqual(report['mapping_source_conflict'], 1)

    def test_team_log_plus_minus_is_used_only_for_game_date_not_player_statistics(self):
        logs = [{'game_id': '0022600001', 'season': 2027, 'season_type': 'regular-season',
                 'team_id': key, 'team_abbreviation': team, 'game_date': '2026-10-20',
                 'matchup': matchup, 'pts': score, 'wl': outcome, 'plus_minus': pm}
                for key, team, matchup, score, outcome, pm in
                [(1610612752, 'NYK', 'NYK vs. PHI', 110, 'W', 10),
                 (1610612755, 'PHI', 'PHI @ NYK', 100, 'L', -10)]]
        result, _ = game_date_inventory([], logs, '2026-27')
        self.assertEqual(result['0022600001']['date'], '2026-10-20')
        records, measured, _ = self.normalize([self.row()])
        self.assertNotIn('plus_minus', records[0]['fields'])
        self.assertNotIn('plus_minus', measured[0])

    def test_newer_log_game_credits_only_the_actual_contributing_mapping_source(self):
        logs = [{'game_id': '0022600003', 'season': 2027, 'season_type': 'regular-season',
                 'team_id': key, 'team_abbreviation': team, 'game_date': '2026-10-29',
                 'matchup': matchup, 'pts': score, 'wl': outcome}
                for key, team, matchup, score, outcome in
                [(1610612752, 'NYK', 'NYK vs. PHI', 110, 'W'),
                 (1610612755, 'PHI', 'PHI @ NYK', 100, 'L')]]
        schedule = {'source':'https://github.com/source/releases/download/nba_stats_schedules/nba_schedule_2027.parquet',
                    'source_published_at':'2026-10-28T15:00:00Z'}
        team_logs = {'source':'https://github.com/source/releases/download/nba_stats_player_game_logs/player_game_logs_2027.parquet',
                     'source_published_at':'2026-10-30T15:00:00Z'}
        result, _ = game_date_inventory(self.schedule, logs, '2026-27', mapping_sources=[schedule, team_logs])
        self.assertEqual(result['0022600001']['mapping_sources'], [schedule])
        self.assertEqual(result['0022600003']['mapping_sources'], [team_logs])
        duplicate = copy.deepcopy(logs[0]); duplicate['pts'] = 115
        result, report = game_date_inventory([], logs + [duplicate], '2026-27')
        self.assertEqual(result, {})
        self.assertEqual(report['team_log_duplicate_conflict'], 1)


if __name__ == '__main__':
    unittest.main()
