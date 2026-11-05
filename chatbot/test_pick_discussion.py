"""Conversation prop assessments preserve arithmetic, scope and uncertainty."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from feeds import canonical_game, canonical_player
from master import MasterStore
from pick_discussion import build_pick_discussion, normalize_stat
from test_pregame_discussion import record_fixture_refresh


NOW = dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.timezone.utc)


class PickDiscussionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'fixture.sqlite')
        self.store.upsert_game({'id': 'target', 'date': '2026-11-02', 'home_team': 'NYK',
            'away_team': 'BKN', 'status': 'scheduled', 'start_time': '2026-11-03T00:30:00+00:00',
            'source': 'synthetic scheduled-event fixture'})

    def add(self, player, values, *, stat='tpm', team='BKN', minutes=34,
            season='2026-27', phase='Regular Season', source='test observations',
            dates=None, state='Played', status='final'):
        for i, value in enumerate(values):
            day = dates[i] if dates else (dt.date(2026, 11, 1) - dt.timedelta(days=i)).isoformat()
            game = canonical_game(day, team, 'BOS')
            self.store.upsert_game({'id': game, 'date': day, 'home_team': team,
                'away_team': 'BOS', 'status': status, 'source': source})
            row = {'game_id': game, 'player_id': canonical_player(player), 'player': player,
                'team': team, 'opponent': 'BOS', 'date': day, 'season': season,
                'season_type': phase, 'appearance_state': state, 'source': source,
                'minutes': minutes[i] if isinstance(minutes, list) else minutes,
                'pts': 20, 'reb': 5, 'ast': 4, 'fga': 16, 'tpa': 7, 'tpm': 2}
            if stat == 'pra':
                row.update(pts=value - 9 if value is not None else None, reb=5, ast=4)
            else:
                row[stat] = value
            self.store.upsert_player_game(row)
        record_fixture_refresh(self.store)

    def assess(self, player='Julius Randle', stat='threes', line=1.5, **kwargs):
        return build_pick_discussion(self.store, {'player': player, 'stat': stat,
            'line': line, **kwargs}, NOW)

    def add_prior_history(self, player, values, *, stat='tpm', team='BKN'):
        self.add(player, values, stat=stat, team=team, season='2025-26',
                 dates=[(dt.date(2026, 4, 10) - dt.timedelta(days=i)).isoformat()
                        for i in range(len(values))])

    def test_three_point_question_returns_qualified_lean_and_facts(self):
        self.add('Julius Randle', [4, 3, 0, 0, 2, 3])
        self.add_prior_history('Julius Randle', [3] * 14)
        result = self.assess()
        self.assertEqual(result['stat'], 'tpm')
        self.assertEqual(result['decision'], 'more')
        self.assertEqual(result['sample']['more'], 4)
        self.assertEqual(result['sample']['mean'], 2)
        self.assertEqual(result['confidence'], 'low')
        self.assertFalse(result['offer_verified'])
        self.assertIn('shooting accuracy', result['fallback_answer'])
        facts = [fact for card in result['evidence_cards'] for fact in card['approved_facts']]
        self.assertTrue(any(f['fact_type'] == 'line' and 2 in f['values'] for f in facts))
        self.assertTrue(all(f['entity'] == 'Julius Randle' for f in facts))

    def test_hart_pra_correction_uses_complete_sum_and_can_choose_less(self):
        self.add('Josh Hart', [17, 19, 23, 21, 18, 19], stat='pra', team='NYK')
        self.add_prior_history('Josh Hart', [18] * 14, stat='pra', team='NYK')
        result = self.assess('Josh Hart', 'points rebounds assists', 22.5)
        self.assertEqual(result['decision'], 'less')
        self.assertEqual(result['sample']['values'], [17, 19, 23, 21, 18, 19])
        self.assertEqual(result['sample']['less'], 5)
        self.assertNotIn('I could not verify', result['fallback_answer'])

    def test_average_outlier_alone_cannot_choose_over(self):
        self.add('Julius Randle', [0, 0, 0, 0, 0, 12])
        self.add_prior_history('Julius Randle', [0] * 13 + [28])
        result = self.assess()
        self.assertGreater(result['sample']['mean'], result['line'])
        self.assertEqual(result['decision'], 'pass')
        self.assertIsNone(result['research_lean'])
        self.assertEqual(result['baseline']['n'], 20)

    def test_integer_tie_is_not_a_more_or_less_hit(self):
        self.add('Jalen Brunson', [18, 19, 17, 20, 18, 21], stat='fga', team='NYK')
        self.add_prior_history('Jalen Brunson', [18] * 14, stat='fga', team='NYK')
        result = self.assess('Jalen Brunson', 'FG attempts', 18)
        self.assertEqual((result['sample']['more'], result['sample']['less'], result['sample']['ties']), (3, 1, 2))
        self.assertEqual(result['decision'], 'pass')

    def test_missing_market_or_nonfinite_line_needs_clarification(self):
        self.assertEqual(self.assess('Josh Hart', None, 22.5)['missing'], ['stat'])
        for value in (float('nan'), float('inf'), True, -1):
            self.assertEqual(self.assess(line=value)['missing'], ['line'])
        self.assertIsNone(normalize_stat('first quarter points'))
        self.assertIsNone(normalize_stat('quarters with 3+ points'))

    def test_only_current_season_same_team_final_played_complete_rows(self):
        self.add('Josh Hart', [17, 19, 20, 18, 19], stat='pra', team='NYK')
        self.add('Josh Hart', [99], stat='pra', team='NYK', dates=['2025-11-01'], season='2025-26')
        self.add('Josh Hart', [99], stat='pra', team='NYK', dates=['2026-10-26'], phase='Playoffs')
        self.add('Josh Hart', [99], stat='pra', team='PHI', dates=['2026-10-25'])
        self.add('Josh Hart', [99], stat='pra', team='NYK', dates=['2026-10-24'], state='DNP')
        self.add('Josh Hart', [99], stat='pra', team='NYK', dates=['2026-10-23'], status='live')
        self.add('Josh Hart', [None], stat='pra', team='NYK', dates=['2026-10-22'])
        result = self.assess('Josh Hart', 'pra', 22.5)
        self.assertEqual(result['sample']['n'], 5)
        self.assertEqual(result['sample']['maximum'], 20)

    def test_unknown_minutes_keep_observation_but_do_not_invent_workload(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3], minutes=None)
        result = self.assess()
        self.assertEqual(result['sample']['n'], 6)
        self.assertEqual(result['workload']['observed_games'], 0)
        self.assertIsNone(result['workload']['reference_minutes'])
        self.assertEqual(result['decision'], 'pass')

    def test_sparse_stale_and_large_workload_change_can_hold(self):
        self.add('Sparse Fixture', [4, 3, 3])
        self.assertEqual(self.assess('Sparse Fixture')['decision'], 'pass')
        self.add('Old Fixture', [4, 3, 3, 2, 2], dates=['2026-10-20','2026-10-19','2026-10-18','2026-10-17','2026-10-16'])
        self.assertEqual(self.assess('Old Fixture')['decision'], 'pass')
        self.add('Role Fixture', [4, 3, 3, 2, 2, 3], minutes=[20, 20, 20, 36, 36, 36])
        result = self.assess('Role Fixture')
        self.assertTrue(result['workload']['unstable'])
        self.assertEqual(result['decision'], 'pass')

    def test_no_news_does_not_mean_healthy_or_force_all_queries_to_pass(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        self.add_prior_history('Julius Randle', [3] * 14)
        result = self.assess()
        self.assertEqual(result['decision'], 'more')
        self.assertFalse(result['availability_confirmed'])
        self.assertIn('not a complete', ' '.join(result['cautions']))

    def test_twenty_game_chat_matches_board_benchmark_and_margin_pass(self):
        from analysis_engine import analyze_board
        self.add('Julius Randle', [4, 3, 0, 0, 2, 3])
        self.add_prior_history('Julius Randle', [3] * 14)
        for line, expected in ((1.5, 'more'), (2.5, 'pass')):
            with self.subTest(line=line):
                chat = self.assess(line=line)
                board = self.store.create_board([{'player': 'Julius Randle', 'team': 'BKN',
                    'date': '2026-11-02', 'game_id': 'target', 'stat': 'tpm',
                    'side': 'more', 'line': line}])
                report = analyze_board(self.store, board['id'], as_of=NOW)
                projection = report['legs'][0]['projection']
                self.assertEqual(chat['baseline']['reference_projection'], projection['reference_projection'])
                self.assertEqual(chat['baseline']['source_games'], projection['source_games'])
                self.assertEqual(chat['decision'], expected)
                self.assertEqual(projection['selected_side_supported'], expected == 'more')
                self.assertEqual(chat['sample']['n'], 6)
                self.assertEqual(chat['baseline']['n'], 20)
                self.assertEqual(chat['baseline']['seasons'], ['2025-26', '2026-27'])

    def test_current_season_hot_streak_without_twenty_games_stays_a_pass(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        result = self.assess()
        self.assertEqual(result['sample']['n'], 6)
        self.assertEqual(result['decision'], 'pass')
        self.assertFalse(result['baseline']['available'])
        self.assertEqual(result['baseline']['required_observations'], 20)

    def test_player_out_and_minutes_uncertainty_hold_both_sides(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        self.store.record_news({'id': 'out', 'title': 'Julius Randle: Ruled out for Monday',
            'published_at': '2026-11-02T18:00:00+00:00', 'source': 'test publisher'})
        result = self.assess(side='less')
        self.assertEqual(result['decision'], 'pass')
        self.assertTrue(result['availability']['blocked'])

    def test_target_and_future_observations_cannot_leak_into_answer(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        before = self.assess()
        self.add('Julius Randle', [50, 50], dates=['2026-11-02','2026-11-03'], team='PHI')
        after = self.assess()
        self.assertEqual(before['sample'], after['sample'])
        self.assertEqual(after['team'], 'BKN')
        retrospective = self.assess(date='2026-10-30')
        self.assertLess(retrospective['sample']['last_date'], '2026-10-30')
        self.assertEqual(retrospective['decision'], 'pass')
        self.assertEqual(retrospective['mode'], 'retrospective')

    def test_conflicting_team_or_opponent_does_not_create_supported_pick(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        self.assertFalse(self.assess(team='NYK')['available'])
        self.store.upsert_game({'id': 'target', 'date': '2026-11-02', 'home_team': 'NYK',
            'away_team': 'BKN', 'status': 'scheduled', 'start_time': '2026-11-03T00:30:00+00:00'})
        result = self.assess(opponent='PHI')
        self.assertTrue(result['event_conflict'])
        self.assertEqual(result['decision'], 'pass')

    def test_started_game_is_not_an_actionable_pregame_recommendation(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        self.store.upsert_game({'id': 'target', 'date': '2026-11-02', 'home_team': 'NYK',
            'away_team': 'BKN', 'status': 'live', 'start_time': '2026-11-02T19:30:00+00:00'})
        result = self.assess()
        self.assertEqual(result['decision'], 'pass')
        self.assertIn('started', result['reason'])

    def test_assessment_is_read_only_and_does_not_create_board_or_entry(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        def snapshot():
            with self.store.connect() as db:
                return '\n'.join(db.iterdump())
        before = snapshot()
        self.assess()
        self.assertEqual(before, snapshot())

    def test_opponent_defense_keeps_team_metrics_and_source_clocks_separate(self):
        self.add('Julius Randle', [4, 3, 0, 0, 2, 3])
        record = {'entity': 'NYK', 'category': 'Team defense', 'season': '2026-27',
                  'season_type': 'Regular Season', 'gp': 6,
                  'fields': {'opponent_tpm': 72, 'opponent_tpa': 180, 'points_allowed': 640},
                  'data_through_date': None, 'published_at': '2026-11-02T14:00:00+00:00',
                  'source': 'test published opponent data'}
        with self.store.connect() as db:
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                       ('2026-27', 'NYK', 'Team defense', json.dumps(record)))
        baseline = self.assess()
        result = self.assess(opponent='NYK')
        defense = result['opponent_defense']
        self.assertTrue(defense['available'])
        self.assertTrue(defense['coverage_unknown'])
        self.assertFalse(defense['quantitative_adjustment'])
        self.assertEqual([item['value'] for item in defense['metrics']], [12, 30, 40])
        self.assertEqual(result['decision'], baseline['decision'])
        self.assertEqual(result['workload'], baseline['workload'])
        card = next(card for card in result['evidence_cards'] if card['id'] == 'P4')
        self.assertEqual(card['entity'], 'NYK')
        self.assertIsNone(card['data_through_date'])
        self.assertEqual(card['published_at'], record['published_at'])
        self.assertNotIn('points allowed', card['text'])
        self.assertLessEqual(len(next(c for c in result['evidence_cards'] if c['id'] == 'P3')['text']), 600)

    def test_future_or_wrong_phase_opponent_snapshot_is_excluded(self):
        self.add('Julius Randle', [4, 3, 3, 2, 2, 3])
        base = {'entity': 'NYK', 'category': 'Team defense', 'season': '2026-27',
                'season_type': 'Regular Season', 'gp': 6, 'fields': {'opponent_tpm': 72},
                'data_through_date': '2026-11-01', 'published_at': '2026-11-02T14:00:00+00:00'}
        for changes in ({'published_at': '2026-11-03T14:00:00+00:00'},
                        {'data_through_date': '2026-11-02'}, {'season_type': 'Playoffs'}):
            with self.store.connect() as db:
                db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                           ('2026-27', 'NYK', 'Team defense', json.dumps({**base, **changes})))
            self.assertFalse(self.assess(opponent='NYK')['opponent_defense']['available'])


if __name__ == '__main__':
    unittest.main()
