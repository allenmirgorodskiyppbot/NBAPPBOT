"""Coverage constraints select qualified opponents or clearly leave a partial draft."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from board_shortlist import enrich_shortlist, format_shortlist
from feeds import canonical_game, canonical_player
from independent_picks import build_independent_picks, format_independent_picks
from lineup_selection import select_lineup
from master import MasterStore
from season_calendar import seed_calendar
from test_board_shortlist import candidate, report, NOW, DATE


class LineupSelectionTests(unittest.TestCase):
    def test_best_eligible_opponent_replaces_weakest_same_team_candidate(self):
        rows = [{'player': player, 'team': team} for player, team in
                [('A', 'NYK'), ('B', 'NYK'), ('C', 'NYK'), ('D', 'NYK'),
                 ('E', 'PHI'), ('F', 'PHI')]]
        chosen, coverage = select_lineup(rows, 4, required_teams=['NYK', 'PHI'])
        self.assertEqual([row['player'] for row in chosen], ['A', 'B', 'C', 'E'])
        self.assertTrue(coverage['lineup_complete'])
        self.assertEqual(coverage['selected_teams'], ['NYK', 'PHI'])
        self.assertEqual(coverage['missing_teams'], [])

    def test_duplicate_player_cannot_fill_missing_team_or_an_extra_leg(self):
        rows = [{'player': 'Jalen Brunson', 'team': 'NYK'},
                {'player': 'Jalen Brunson', 'team': 'PHI'},
                {'player': 'Josh Hart', 'team': 'NYK'}]
        chosen, coverage = select_lineup(rows, 2, required_teams=['NYK', 'PHI'])
        self.assertEqual(len(chosen), 2)
        self.assertFalse(coverage['lineup_complete'])
        self.assertEqual(coverage['missing_teams'], ['PHI'])
        self.assertIn('partial research', coverage['constraint_note'])

    def test_unknown_team_is_not_counted_as_the_opponent(self):
        rows = [{'player': 'A', 'team': 'NYK'}, {'player': 'B', 'team': 'unknown'}]
        chosen, coverage = select_lineup(rows, 2)
        self.assertEqual(len(chosen), 2)
        self.assertFalse(coverage['two_team_coverage'])
        self.assertFalse(coverage['lineup_complete'])

    def test_explicit_single_team_preference_is_supported_but_not_invented(self):
        rows = [{'player': 'A', 'team': 'NYK'}, {'player': 'B', 'team': 'NYK'},
                {'player': 'C', 'team': 'PHI'}]
        chosen, coverage = select_lineup(rows, 2, constraints={'both_teams': False})
        self.assertEqual([row['player'] for row in chosen], ['A', 'B'])
        self.assertTrue(coverage['lineup_complete'])
        self.assertFalse(coverage['two_team_coverage'])

    def test_invalid_request_does_not_silently_relax_constraint(self):
        for count in (0, 7, True):
            with self.assertRaises(ValueError):
                select_lineup([], count)
        with self.assertRaises(ValueError):
            select_lineup([], 4, constraints={'both_teams': 'false'})
        _, coverage = select_lineup([{'player': 'A', 'team': 'NYK'}], 1,
                                    constraints={'both_teams': True})
        self.assertFalse(coverage['lineup_complete'])
        self.assertIn('At least two', coverage['constraint_note'])


class LineupCoverageIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Keep these lineup-composition fixtures independent of daily feeds.
        for name in ('board_shortlist', 'independent_picks'):
            readiness = patch(name + '.assess_pregame_freshness', return_value={
                'ready': True, 'reasons': [], 'current_player_stat_coverage': ['pts', 'reb', 'ast', 'pra', 'fga']})
            readiness.start()
            self.addCleanup(readiness.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)

    def observations(self, player, team, n=20):
        for offset in range(n):
            day = (dt.date(2026, 10, 29) - dt.timedelta(days=offset)).isoformat()
            identity = canonical_game(day, team, 'BOS')
            self.store.upsert_game({'id': identity, 'date': day, 'status': 'final',
                'home_team': team, 'away_team': 'BOS', 'source': 'synthetic coverage fixture'})
            self.store.upsert_player_game({'game_id': identity, 'player_id': canonical_player(player),
                'player': player, 'team': team, 'opponent': 'BOS', 'date': day,
                'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
                'minutes': 33, 'pts': 22 + offset % 3, 'reb': 6, 'ast': 4, 'fga': 18,
                'source': 'synthetic coverage fixture'})

    def test_board_uses_supported_opponent_even_when_knicks_rank_above_him(self):
        items = [candidate(name) for name in ('Jalen Brunson', 'Josh Hart', 'OG Anunoby', 'Karl-Anthony Towns')]
        opponent = candidate('Tyrese Maxey', team='PHI')
        opponent['projection']['standardized_margin'] = .10
        data = enrich_shortlist(self.store, report(*items, opponent), NOW, count=4)
        self.assertEqual(len(data['shortlist']), 4)
        self.assertIn('Tyrese Maxey', [item['leg']['player'] for item in data['shortlist']])
        self.assertTrue(data['lineup_complete'])
        self.assertEqual(data['required_teams'], ['NYK', 'PHI'])
        self.assertIn('each team', format_shortlist(data))

    def test_ineligible_opponent_never_becomes_filler_to_complete_board(self):
        items = [candidate(name) for name in ('Jalen Brunson', 'Josh Hart', 'OG Anunoby', 'Karl-Anthony Towns')]
        opponent = candidate('Tyrese Maxey', team='PHI', eligible_for_ranking=False,
                             reason='Only two current-team observations are available.')
        before = len(self.store.list_entries())
        data = enrich_shortlist(self.store, report(*items, opponent), NOW, count=4)
        self.assertEqual(len(data['shortlist']), 4)
        self.assertFalse(data['lineup_complete'])
        self.assertFalse(data['constraints_satisfied'])
        self.assertEqual(data['missing_teams'], ['PHI'])
        text = format_shortlist(data)
        self.assertTrue(text.startswith('Partial shortlist'))
        self.assertIn('need an eligible selection from PHI', text)
        self.assertEqual(len(self.store.list_entries()), before)

    def test_opponent_absence_blocks_coverage_even_with_high_projected_margin(self):
        self.store.record_news({'id': 'out', 'title': 'Tyrese Maxey: Ruled out for Friday',
            'body': 'Reported ankle injury.', 'published_at': '2026-10-30T18:00:00-04:00',
            'source': 'synthetic publisher'})
        data = enrich_shortlist(self.store, report(candidate(), candidate('Tyrese Maxey', team='PHI')),
                                NOW, count=2)
        self.assertEqual([item['leg']['player'] for item in data['shortlist']], ['Jalen Brunson'])
        self.assertFalse(data['lineup_complete'])
        self.assertEqual(data['missing_teams'], ['PHI'])

    def test_independent_partial_has_explicit_constraints_and_no_financial_mutation(self):
        for player in ('Jalen Brunson', 'Josh Hart', 'OG Anunoby', 'Karl-Anthony Towns'):
            self.observations(player, 'NYK')
        self.observations('Tyrese Maxey', 'PHI', n=2)
        before = (len(self.store.list_entries()), len(self.store.list_boards()))
        data = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(len(data['picks']), 4)
        self.assertEqual(data['lineup_constraints'], {'both_teams': True})
        self.assertFalse(data['lineup_complete'])
        self.assertEqual(data['missing_teams'], ['PHI'])
        for compact in (False, True):
            text = format_independent_picks(data, compact)
            self.assertTrue(text.startswith('Partial research'))
            self.assertIn('need an eligible selection from PHI', text)
            self.assertNotIn('Tyrese Maxey ·', text)
        self.assertEqual((len(self.store.list_entries()), len(self.store.list_boards())), before)
        self.assertNotIn('win_probability', json.dumps(data))

    def test_independent_constraint_can_be_changed_explicitly(self):
        for player in ('Jalen Brunson', 'Josh Hart'):
            self.observations(player, 'NYK')
        data = build_independent_picks(self.store, DATE, count=2, now=NOW,
                                       constraints={'both_teams': False})
        self.assertTrue(data['lineup_complete'])
        self.assertTrue(data['constraints_satisfied'])
        self.assertFalse(data['two_team_coverage'])
        self.assertFalse(format_independent_picks(data).startswith('Partial'))

    def test_market_preference_changes_each_players_qualified_market(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Tyrese Maxey', 'PHI')
        default = build_independent_picks(self.store, DATE, count=2, now=NOW)
        preferred = build_independent_picks(self.store, DATE, count=2, now=NOW,
            constraints={'preferred_stat': 'fga'})
        self.assertTrue(any(item['stat'] != 'fga' for item in default['picks']))
        self.assertEqual([item['stat'] for item in preferred['picks']], ['fga', 'fga'])
        self.assertTrue(preferred['lineup_complete'])
        self.assertEqual(preferred['lineup_constraints'], {'both_teams': True, 'preferred_stat': 'fga'})
        self.assertTrue(all('line' not in item and 'side' not in item for item in preferred['picks']))

    def test_market_preference_does_not_fill_missing_or_ineligible_evidence(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Tyrese Maxey', 'PHI')
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET fga=NULL WHERE player=?', ('Tyrese Maxey',))
        data = build_independent_picks(self.store, DATE, count=2, now=NOW,
            constraints={'preferred_stat': 'fga'})
        by_player = {item['player']: item for item in data['picks']}
        self.assertEqual(by_player['Jalen Brunson']['stat'], 'fga')
        self.assertNotEqual(by_player['Tyrese Maxey']['stat'], 'fga')
        self.assertTrue(data['lineup_complete'])
        self.store.record_news({'id': 'out', 'title': 'Tyrese Maxey: Ruled out for Friday',
            'body': 'Reported ankle injury.', 'published_at': '2026-10-30T18:00:00-04:00',
            'source': 'synthetic publisher'})
        blocked = build_independent_picks(self.store, DATE, count=2, now=NOW,
            constraints={'preferred_stat': 'fga'})
        self.assertEqual([item['player'] for item in blocked['picks']], ['Jalen Brunson'])
        self.assertFalse(blocked['lineup_complete'])

    def test_unsupported_market_preference_does_not_expand_market_set(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Tyrese Maxey', 'PHI')
        default = build_independent_picks(self.store, DATE, count=2, now=NOW)
        unsupported = build_independent_picks(self.store, DATE, count=2, now=NOW,
            constraints={'preferred_stat': 'unverified special market'})
        self.assertEqual(unsupported['picks'], default['picks'])
        self.assertNotIn('preferred_stat', unsupported['lineup_constraints'])

    def test_long_six_leg_shortlist_keeps_every_selection_and_footer(self):
        items = [candidate('Long Fictional Player Name ' + str(i), team='NYK' if i < 3 else 'PHI')
                 for i in range(6)]
        data = enrich_shortlist(self.store, report(*items), NOW, count=6)
        for item in data['shortlist']:
            item['reason'] += ' Extra measured context.' * 100
            item['caution'] = 'Questionable per publisher; verify clearance. ' * 50
        text = format_shortlist(data)
        self.assertLessEqual(len(text), 2400)
        self.assertEqual(text.count('More 19.5 points'), 6)
        self.assertEqual(text.count('Watch:'), 6)
        self.assertIn('No entry placed.', text)
        self.assertTrue(text.endswith('/analyze test-board'))


if __name__ == '__main__':
    unittest.main()
