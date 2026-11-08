"""Held uploaded offers still receive measured master-data comparisons."""
import copy
import unittest
from unittest.mock import patch

from analysis_engine import analyze_board
from board_shortlist import enrich_shortlist, format_shortlist, build_master_targets
import test_board_shortlist as fixtures

NOW = fixtures.NOW
candidate = fixtures.candidate


HELD = {'ready': False, 'reasons': ['Today’s game schedule could not be independently checked.'],
        'current_player_stat_coverage': []}


class MasterBoardResearchTests(unittest.TestCase):
    observations = fixtures.ShortlistTests.observations

    def setUp(self):
        fixtures.ShortlistTests.setUp(self)

    def build(self, specs):
        legs = []
        for player, team, n in specs:
            self.observations(player, team, n=n)
            for side in ('more', 'less'):
                leg = candidate(player, side=side, line=15.5, team=team)['leg']
                leg['offer_variant'] = 'standard'
                legs.append(leg)
        board = self.store.create_board(legs)
        with patch('analysis_engine.assess_pregame_freshness', return_value=HELD), \
                patch('board_shortlist.assess_pregame_freshness', return_value=HELD):
            report = analyze_board(self.store, board['id'], as_of=NOW)
            before = self.snapshot()
            enriched = enrich_shortlist(self.store, report, NOW)
        self.assertEqual(self.snapshot(), before)
        return report, enriched

    def snapshot(self):
        with self.store.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('player_games', 'games', 'boards', 'entries', 'analysis_reports')}

    def test_four_held_players_get_actual_numbers_and_both_teams_without_becoming_picks(self):
        _, data = self.build([('Jalen Brunson', 'NYK', 20), ('Josh Hart', 'NYK', 20),
                              ('Tyrese Maxey', 'PHI', 20), ('Joel Embiid', 'PHI', 20)])
        self.assertEqual(data['requested_count'], 4)
        self.assertEqual(data['shortlist'], [])
        self.assertFalse(data['lineup_complete'])
        research = data['research_candidates']
        self.assertEqual(len(research), 4)
        self.assertEqual(len({row['player'] for row in research}), 4)
        self.assertEqual({row['team'] for row in research}, {'NYK', 'PHI'})
        for row in research:
            self.assertFalse(row['eligible_for_ranking'])
            self.assertFalse(row['approved_for_entry'])
            self.assertEqual(row['n'], 20)
            self.assertEqual(row['mean'], 21.5)
            self.assertEqual(row['median'], 21.5)
            self.assertEqual(row['above'], 20)
            self.assertEqual(row['below'], 0)
            self.assertEqual(row['source_data_through_date'], '2026-10-29')
            self.assertEqual(row['historical_benchmark']['side'], 'more')
            self.assertEqual(row['offered_sides'], ['less', 'more'])
            self.assertIn('could not be independently checked', row['hold_reason'])
        text = format_shortlist(data)
        self.assertTrue(text.startswith('Saved stats loaded — 4 held research comparisons'))
        for number in range(1, 5):
            self.assertIn('Research '+str(number)+'.', text)
        self.assertIn('held, not an approved lineup', text)
        self.assertIn('mean 21.50; median 21.5', text)
        self.assertIn('No entry placed', text)
        self.assertIn('/analyze '+data['board_id'], text)
        self.assertLessEqual(len(text), 2400)

    def test_fewer_actual_players_are_not_padded_to_four(self):
        _, data = self.build([('Jalen Brunson', 'NYK', 20), ('Tyrese Maxey', 'PHI', 20)])
        self.assertEqual(len(data['research_candidates']), 2)
        self.assertEqual(data['shortlist'], [])
        self.assertFalse(data['lineup_complete'])
        self.assertNotIn('Research 3.', format_shortlist(data))

    def test_short_sample_is_visible_without_pretending_twenty_games_or_lifetime_coverage(self):
        _, data = self.build([('Jalen Brunson', 'NYK', 7), ('Tyrese Maxey', 'PHI', 20)])
        row = next(row for row in data['research_candidates'] if row['player'] == 'Jalen Brunson')
        self.assertEqual(row['n'], 7)
        self.assertIsNone(row['historical_benchmark']['side'])
        self.assertIn('7 saved same-team games', format_shortlist(data))
        self.assertFalse(row['approved_for_entry'])

    def test_forged_projection_centers_do_not_replace_measured_source_values(self):
        report, _ = self.build([('Jalen Brunson', 'NYK', 20), ('Tyrese Maxey', 'PHI', 20)])
        report = copy.deepcopy(report)
        for item in report['legs']:
            item['projection']['reference_projection'] = 999
            item['projection']['baseline_median'] = 999
        data = enrich_shortlist(self.store, report, NOW)
        self.assertTrue(data['research_candidates'])
        self.assertTrue(all(row['mean'] == 21.5 and row['median'] == 21.5
                            for row in data['research_candidates']))
        self.assertNotIn('999', format_shortlist(data))

    def test_wrong_current_team_cannot_borrow_old_same_team_history(self):
        report, _ = self.build([('Jalen Brunson', 'NYK', 20), ('Tyrese Maxey', 'PHI', 20)])
        with self.store.connect() as db:
            db.execute("UPDATE player_games SET team='PHI' WHERE player='Jalen Brunson' AND date='2026-10-29'")
            # That old observation's actual game involved NYK/BOS, so the
            # inconsistent imported team must not establish a current identity.
        for item in report['legs']:
            if item['leg']['player'] == 'Jalen Brunson':
                item['leg']['team'] = 'PHI'
        data = enrich_shortlist(self.store, report, NOW)
        self.assertFalse(any(row['player'] == 'Jalen Brunson' for row in data['research_candidates']))

    def test_other_master_players_have_real_baselines_without_a_fabricated_offer(self):
        self.observations('Josh Hart', 'NYK', n=20)
        self.observations('Kelly Oubre Jr.', 'PHI', n=20)
        _, data = self.build([('Jalen Brunson', 'NYK', 20), ('Tyrese Maxey', 'PHI', 20)])
        own = data['master_watchlist']
        self.assertEqual({row['player'] for row in own}, {'Josh Hart', 'Kelly Oubre Jr.'})
        for row in own:
            self.assertTrue(row['offer_required'])
            self.assertIsNone(row['line'])
            self.assertIsNone(row['historical_benchmark'])
            self.assertNotIn('side', row['leg'])
            self.assertFalse(row['approved_for_entry'])
            self.assertEqual(row['mean'], 21.5)
        self.assertIn('actual offers needed', format_shortlist(data))


class MasterTargetTests(unittest.TestCase):
    observations = fixtures.ShortlistTests.observations

    def setUp(self):
        fixtures.ShortlistTests.setUp(self)
        patcher = patch('independent_picks.assess_pregame_freshness', return_value={
            'ready': True, 'reasons': [],
            'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga']})
        patcher.start(); self.addCleanup(patcher.stop)
        for player, team in [('Jalen Brunson', 'NYK'), ('Josh Hart', 'NYK'),
                             ('Tyrese Maxey', 'PHI'), ('Joel Embiid', 'PHI')]:
            self.observations(player, team)
        board = self.store.create_board([candidate()['leg'], candidate('Tyrese Maxey', team='PHI')['leg']])
        self.report = {'board_id': board['id'], 'original_legs': copy.deepcopy(board['legs'])}

    def test_independent_targets_use_actual_master_math_and_include_players_outside_uploads(self):
        before = copy.deepcopy(self.report)
        with self.store.connect() as db:
            counts = {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                      for table in ('boards', 'entries', 'player_games', 'games', 'checkpoints')}
        result = build_master_targets(self.store, self.report, NOW, constraints={'preferred_stat': 'pts'})
        self.assertEqual(len(result['picks']), 4)
        self.assertTrue(result['lineup_complete'])
        self.assertEqual(result['resolved_game_id'], 'nba:2026-10-30:NYK:PHI')
        self.assertEqual({row['team'] for row in result['picks']}, {'NYK', 'PHI'})
        self.assertTrue({'Josh Hart', 'Joel Embiid'} <= {row['player'] for row in result['picks']})
        self.assertFalse(result['uploaded_offers_used'])
        for row in result['picks']:
            self.assertEqual(row['stat'], 'pts')
            self.assertEqual(row['projection']['reference_projection'], 21.5)
            measured = [game['value'] for game in row['projection']['source_games']]
            self.assertEqual(sum(measured) / len(measured), 21.5)
            self.assertEqual(row['targets']['more_at_or_below'], 17.5)
            self.assertEqual(row['targets']['less_at_or_above'], 25.5)
            self.assertNotIn('line', row)
            self.assertNotIn('side', row)
        altered = copy.deepcopy(self.report)
        for leg in altered['original_legs']:
            leg.update(line=99.5, side='less', offer_variant='demon')
        other = build_master_targets(self.store, altered, NOW, constraints={'preferred_stat': 'pts'})
        self.assertEqual(other['picks'], result['picks'])
        self.assertEqual(self.report, before)
        with self.store.connect() as db:
            self.assertEqual(counts, {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                                     for table in counts})

    def test_unknown_mixed_conflicting_and_past_events_never_borrow_another_game(self):
        from datetime import timedelta
        bad_id = copy.deepcopy(self.report); bad_id['original_legs'][0]['game_id'] = 'not-a-saved-game'
        conflict = copy.deepcopy(self.report); conflict['original_legs'][0]['opponent'] = 'BOS'
        mixed = copy.deepcopy(self.report)
        mixed['original_legs'].append({**candidate()['leg'], 'date': '2026-11-02'})
        for label, report, now in [('unknown', bad_id, NOW), ('opponent', conflict, NOW),
                                   ('mixed', mixed, NOW), ('past', self.report, NOW+timedelta(days=1))]:
            with self.subTest(label=label), patch('independent_picks.build_independent_picks') as builder:
                result = build_master_targets(self.store, report, now)
                self.assertEqual(result['picks'], [])
                self.assertIsNone(result['game'])
                self.assertFalse(result['lineup_complete'])
                builder.assert_not_called()

    def test_unready_current_data_produces_zero_supported_targets_without_relaxing_gates(self):
        with patch('independent_picks.assess_pregame_freshness', return_value=HELD):
            result = build_master_targets(self.store, self.report, NOW)
        self.assertEqual(result['picks'], [])
        self.assertFalse(result['lineup_complete'])
        self.assertTrue(result['held'])
        self.assertTrue(all(row['projection'] is None and row['disposition'] == 'pass'
                            for row in result['evaluated_candidates']))
        self.assertEqual(result['resolved_game_id'], 'nba:2026-10-30:NYK:PHI')

    def test_a_builder_returning_a_different_game_is_rejected_instead_of_presented(self):
        forged = {'game': {'id': 'nba:2026-11-02:BKN:NYK', 'date': '2026-11-02'},
                  'picks': [{'player': 'Wrong Game Player'}], 'note': 'wrong event'}
        with patch('independent_picks.build_independent_picks', return_value=forged):
            result = build_master_targets(self.store, self.report, NOW)
        self.assertEqual(result['picks'], [])
        self.assertIsNone(result['game'])
        self.assertIn('exact uploaded game', result['note'])


if __name__ == '__main__':
    unittest.main()
