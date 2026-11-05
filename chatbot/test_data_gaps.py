"""Coverage distinguishes absent statistics, nonparticipants and undefined rates."""
import json
import copy
import unittest

from data_gaps import BOX_FIELDS, augment_gap_tables


class DataGapsTests(unittest.TestCase):
    def base(self):
        row = {field: 1 for field in BOX_FIELDS}
        row.update(player='Test Player', player_id='name:testplayer', game_id='a', date='2025-01-01',
                   season='2024-25', season_type='Regular Season', appearance_state='Played',
                   team='NYK', opponent='BOS', source='verified fixture', plus_minus=-2)
        return row

    def tables(self, rows, tracking=()):
        return {'games': [{'id': 'a', 'date': '2025-01-01', 'status': 'final'}],
                'player_games': rows, 'refreshed_tracking': list(tracking)}

    def test_exact_missing_stats_exclude_dnp_and_unknown(self):
        row = self.base(); row.update(plus_minus=None, starter=None)
        dnp = {**self.base(), 'appearance_state': 'DNP', **{field: None for field in BOX_FIELDS}}
        unknown = {**dnp, 'appearance_state': 'Unknown'}
        report = augment_gap_tables(None, self.tables([row, dnp, unknown]))
        fields = {item['field'] for item in report['Missing_Stats_By_Game']}
        self.assertEqual(fields, {'plus_minus', 'starter'})
        self.assertEqual(len(report['Unknown_Participation']), 1)
        points = next(item for item in report['Missing_Stats_Summary'] if item['field'] == 'pts')
        self.assertEqual((points['eligible_played_records'], points['missing_records']), (1, 0))

    def test_negative_plus_minus_and_bench_zero_are_valid(self):
        row = self.base(); row['starter'] = 0
        report = augment_gap_tables(None, self.tables([row]))
        self.assertFalse(report['Missing_Stats_By_Game'])

    def test_no_tracking_records_has_exact_unavailable_field_names(self):
        report = augment_gap_tables(None, self.tables([]))
        overall = [row for row in report['Tracking_Field_Coverage'] if row['category'] == 'Overall']
        self.assertEqual({row['field'] for row in overall}, {'defended_fgm', 'defended_fga', 'fg_pct_allowed'})
        self.assertTrue(all(row['status'] == 'source_unavailable' for row in overall))

    def test_zero_attempt_ratio_is_undefined_not_a_missing_count(self):
        record = {'category': 'Rim', 'fields': {'rim_defended_fgm': 0, 'rim_defended_fga': 0,
                  'fg_pct_allowed': None}, 'data_through_date': None}
        report = augment_gap_tables(None, self.tables([], [{'category': 'Rim', 'payload_json': json.dumps(record)}]))
        ratio = next(row for row in report['Tracking_Field_Coverage']
                     if row['category'] == 'Rim' and row['field'] == 'fg_pct_allowed')
        self.assertEqual(ratio['missing_values_in_collected_records'], 0)
        self.assertEqual(ratio['undefined_zero_attempt_ratios'], 1)
        self.assertEqual(ratio['status'], 'undefined_zero_attempt_ratio')

    def test_field_coverage_can_use_its_own_verified_date(self):
        record = {'category': 'Hustle', 'fields': {'deflections': 5}, 'data_through_date': None,
                  'field_provenance': {'deflections': {'data_through_date': '2026-11-04'}}}
        report = augment_gap_tables(None, self.tables([], [{'category': 'Hustle', 'payload_json': json.dumps(record)}]))
        field = next(row for row in report['Tracking_Field_Coverage']
                     if row['category'] == 'Hustle' and row['field'] == 'deflections')
        self.assertEqual(field['records_with_unknown_covered_date'], 0)

    def game(self, game_id='a', **changes):
        return {'id':game_id,'date':'2025-01-01','status':'final','home_team':'NYK',
                'away_team':'BOS','home_score':101,'away_score':100,
                'provider_game_id':'401000001','source':'saved original source',**changes}

    def score_report(self, games, audits=()):
        return augment_gap_tables(None, {'games':games,'player_games':[],
            'refreshed_tracking':[],'Game_Score_Completions':list(audits)})

    def completion(self, game):
        before={**game,'home_score':None}
        source={**game,'source':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary',
                'source_content_sha256':'a'*64,'source_published_at':'2025-01-02T15:00:00Z',
                'upstream_full_game_validated':True}
        return {'game_id':game['id'],'statistic':'home_score',
                'completed_at':'2025-01-03T15:00:00Z','manifest_sha256':'b'*64,
                'old_value_json':'null','new_value_json':json.dumps(game['home_score']),
                'before_row_json':json.dumps(before),'after_row_json':json.dumps(game),
                'source_evidence_json':json.dumps({'game':source,'provider_binding':None,
                                                  'provider_crosswalk_sha256':None})}

    def test_final_score_gaps_are_independent_of_player_rows_and_exclude_upcoming_games(self):
        report=self.score_report([self.game(home_score=None),
            self.game('live',status='live',home_score=None,away_score=None),
            self.game('scheduled',status='scheduled',home_score=None,away_score=None),
            self.game('canceled',status='canceled',home_score=None,away_score=None)])
        self.assertEqual([(r['game_id'],r['field'],r['issue']) for r in report['Game_Stat_Gaps']],
                         [('a','home_score','missing')])
        fields={r['field']:r for r in report['Game_Stat_Coverage']}
        self.assertEqual(fields['home_score']['eligible_final_games'],1)
        self.assertEqual(fields['home_score']['missing_records'],1)
        self.assertEqual(fields['away_score']['records_with_valid_value'],1)
        self.assertEqual(fields['away_score']['source_completed_records'],0)

    def test_fractional_negative_nonfinite_boolean_and_text_scores_are_invalid(self):
        values=[-1,101.5,float('inf'),float('nan'),True,'101',401]
        report=self.score_report([self.game(str(index),home_score=value) for index,value in enumerate(values)])
        home=next(r for r in report['Game_Stat_Coverage'] if r['field']=='home_score')
        self.assertEqual((home['missing_records'],home['invalid_records'],home['records_with_valid_value']),
                         (0,len(values),0))
        self.assertEqual({r['field'] for r in report['Game_Stat_Gaps']},{'home_score'})
        self.assertTrue(all(r['issue']=='invalid_value' for r in report['Game_Stat_Gaps']))

    def test_tied_final_scores_are_invalid_and_valid_numeric_zero_is_not_missing(self):
        report=self.score_report([self.game('tied',home_score=100,away_score=100),
                                  self.game('zero',home_score=0,away_score=100.0)])
        self.assertEqual({(r['game_id'],r['field'],r['detail']) for r in report['Game_Stat_Gaps']},
                         {('tied','home_score','tied_final_score'),('tied','away_score','tied_final_score')})
        self.assertTrue(all(r['missing_records']==0 and r['invalid_records']==1
                            for r in report['Game_Stat_Coverage']))

    def test_source_completed_counts_require_matching_full_game_audit(self):
        game=self.game(); audit=self.completion(game)
        report=self.score_report([game],[audit])
        fields={r['field']:r for r in report['Game_Stat_Coverage']}
        self.assertEqual(fields['home_score']['source_completed_records'],1)
        self.assertEqual(fields['home_score']['valid_records_without_completion_audit'],0)
        self.assertEqual(fields['away_score']['source_completed_records'],0)
        self.assertEqual(fields['away_score']['valid_records_without_completion_audit'],1)
        self.assertFalse(report['Game_Stat_Gaps'])

    def test_invalid_or_nonmatching_completion_proof_does_not_turn_populated_values_into_missing(self):
        game=self.game(); audit=self.completion(game)
        evidence=json.loads(audit['source_evidence_json'])
        wrong_game=copy.deepcopy(evidence);wrong_game['game']['id']='other'
        no_proof=copy.deepcopy(evidence);no_proof['game']['upstream_full_game_validated']=False
        for changed in [{'new_value_json':'99'},{'manifest_sha256':'invalid'},
                        {'old_value_json':'bad json'},
                        {'source_evidence_json':json.dumps(wrong_game)},
                        {'source_evidence_json':json.dumps(no_proof)}]:
            with self.subTest(changed=changed):
                report=self.score_report([game],[{**audit,**changed}])
                home=next(r for r in report['Game_Stat_Coverage'] if r['field']=='home_score')
                self.assertEqual((home['source_completed_records'],home['records_with_valid_value']),(0,1))
                self.assertFalse(report['Game_Stat_Gaps'])

    def test_duplicate_completion_rows_cannot_inflate_source_completed_count(self):
        game=self.game();audit=self.completion(game)
        report=self.score_report([game],[audit,copy.deepcopy(audit)])
        home=next(r for r in report['Game_Stat_Coverage'] if r['field']=='home_score')
        self.assertEqual(home['source_completed_records'],1)

    def test_score_audit_is_read_only_and_readme_points_to_exact_gap_sheets(self):
        games=[self.game(home_score=None)];before=copy.deepcopy(games)
        report=self.score_report(games)
        self.assertEqual(games,before)
        directions=' '.join(str(row['value']) for row in report['Missing_Stats_ReadMe'][:4])
        for name in ('Missing_Stats_Summary','Missing_Stats_By_Game','Game_Stat_Coverage',
                     'Game_Stat_Gaps','Tracking_Field_Coverage','Game_Score_Completions'):
            self.assertIn(name,directions)
        empty=self.score_report([])
        self.assertTrue(all(row['status']=='no_saved_final_games' for row in empty['Game_Stat_Coverage']))


if __name__ == '__main__':
    unittest.main()
