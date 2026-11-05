"""Career questions retain available historical populations and their limits."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from basketball_research import _requested_population, research_context, resolve_question
from conversation import resolve_conversation
from feeds import canonical_game, canonical_player
from grounded_reply import evidence_cards
from master import MasterStore


NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class CareerQueryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.master = MasterStore(Path(directory.name) / 'career.sqlite')
        self.book = type('Book', (), {'sheets': {}})()
        clock = patch('basketball_research.dt.datetime', Clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.add('2026-11-04', 'CHI', 'NYK', 20, season='2026-27', reb=8, ast=9)
        self.add('2025-02-01', 'CHI', 'NYK', 10, season='2024-25', reb=7, ast=5)
        self.add('2023-12-01', 'OKC', 'NYK', 30, season='2023-24', reb=None, ast=4)
        self.add('2024-01-01', 'OKC', 'BOS', 40, season='2023-24', reb=6, ast=3)

    def add(self, day, team, opponent, points, *, player='Josh Giddey', season='2026-27',
            phase='Regular Season', state='Played', status='final', minutes=32, **stats):
        identity = canonical_game(day, team, opponent)
        self.master.upsert_game({'id': identity, 'date': day, 'home_team': team,
            'away_team': opponent, 'status': status, 'source': 'synthetic career game'})
        row = {'game_id': identity, 'date': day, 'player_id': canonical_player(player),
            'player': player, 'team': team, 'opponent': opponent, 'season': season,
            'season_type': phase, 'appearance_state': state, 'minutes': minutes,
            'pts': points, 'reb': 5, 'ast': 4, 'tpm': 2, 'tpa': 4,
            'fga': 16, 'fgm': 8, 'ftm': 2, 'fta': 3, 'stl': 1, 'blk': 0,
            'tov': 2, 'oreb': 1, 'dreb': 4, 'fouls': 2, 'plus_minus': -4,
            'source': 'https://example.test/career/' + day, **stats}
        self.master.upsert_player_game(row)
        return identity

    def context(self, question='Josh Giddey lifetime points against Knicks'):
        resolved = resolve_question(self.book, self.master, question)
        return research_context(self.book, self.master, resolved)

    def test_lifetime_includes_old_seasons_prior_teams_and_explicit_nyk_opponent(self):
        result = self.context()
        self.assertEqual(result['requested_population']['season_basis'], 'available career')
        self.assertEqual(result['requested_population']['seasons'], [])
        baseline, matchup = result['current_player_statistics'][0], result['opponent_player_statistics'][0]
        self.assertEqual((baseline['n'], baseline['ppg']), (4, 25))
        self.assertEqual((matchup['opponent'], matchup['n'], matchup['averages']['pts']), ('NYK', 3, 20))
        self.assertEqual(matchup['actual_teams'], ['CHI', 'OKC'])
        self.assertEqual(matchup['seasons'], ['2023-24', '2024-25', '2026-27'])
        self.assertEqual(matchup['first_date'], '2023-12-01')
        self.assertEqual(matchup['data_through_date'], '2026-11-04')
        self.assertEqual(result['current_tracking'], [])
        self.assertEqual(result['assignment_projections'], [])
        self.assertLessEqual(len(json.dumps(result)), 5500)

    def test_current_season_request_stays_separate(self):
        result = self.context('Josh Giddey points against Knicks this season')
        self.assertEqual(result['requested_population']['season_basis'], 'current season')
        self.assertEqual(result['opponent_player_statistics'][0]['n'], 1)
        self.assertEqual(result['current_player_statistics'][0]['ppg'], 20)

    def test_only_prior_final_regular_played_and_valid_game_identity_enter_career(self):
        for day, extra in [('2026-11-05', {}), ('2026-11-06', {}),
                ('2026-11-03', {'status': 'live'}), ('2026-11-02', {'state': 'DNP'}),
                ('2026-11-01', {'state': 'Unknown'}), ('2026-10-31', {'minutes': 0}),
                ('2026-04-10', {'phase': 'Playoffs', 'season': '2025-26'}),
                ('2026-04-15', {'phase': 'Play-In', 'season': '2025-26'})]:
            self.add(day, 'CHI', 'NYK', 999, **extra)
        mismatch = self.add('2026-10-30', 'CHI', 'NYK', 999)
        with self.master.connect() as db:
            db.execute("UPDATE games SET date='2026-10-29' WHERE id=?", (mismatch,))
        self.add('2024-12-17', 'OKC', 'MIL', 999, season='2024-25')  # Cup championship.
        result = self.context()['current_player_statistics'][0]
        self.assertEqual((result['n'], result['ppg']), (4, 25))

    def test_explicit_playoffs_and_playin_do_not_borrow_regular_games(self):
        self.add('2026-04-10', 'CHI', 'NYK', 70, phase='Playoffs', season='2025-26')
        self.add('2026-04-15', 'CHI', 'NYK', 14, phase='Play-In', season='2025-26')
        for phase, value in [('playoffs', 70), ('play-in', 14)]:
            result = self.context('Josh Giddey career points against Knicks in ' + phase)
            row = result['opponent_player_statistics'][0]
            self.assertEqual((row['n'], row['averages']['pts']), (1, value))

    def test_as_of_before_since_and_through_dates_preserve_boundaries(self):
        cases = [('as of 2025-02-01', 1, 30), ('before 2025-02-01', 1, 30),
                 ('through 2025-02-01', 2, 20), ('since 2025-02-01', 2, 15),
                 ('after 2025-02-01', 1, 20)]
        for suffix, n, mean in cases:
            with self.subTest(suffix=suffix):
                row = self.context('Josh Giddey lifetime points against Knicks ' + suffix)['opponent_player_statistics'][0]
                self.assertEqual((row['n'], row['averages']['pts']), (n, mean))

    def test_explicit_season_wins_over_unbounded_career_word(self):
        result = self.context('Josh Giddey career stats against Knicks in 2023-24')
        self.assertEqual(result['requested_population']['seasons'], ['2023-24'])
        self.assertEqual(result['requested_population']['season_basis'], 'explicit request')
        self.assertEqual(result['opponent_player_statistics'][0]['n'], 1)

    def test_missing_components_are_not_zero_and_compound_uses_complete_games(self):
        row = self.context('Josh Giddey lifetime PRA against Knicks')['opponent_player_statistics'][0]
        self.assertEqual(row['n'], 3)
        self.assertEqual(row['observed_counts']['pra'], 2)
        self.assertEqual(row['averages']['pra'], (37 + 22) / 2)
        card = evidence_cards(self.context('Josh Giddey lifetime PRA against Knicks'), 'lifetime PRA')[0]
        self.assertIn('2/3 observed', card['text'])

    def test_raw_stat_percentages_and_totals_use_requested_metric(self):
        for query, metric, expected in [('FG attempted', 'fga', 16),
                                        ('plus-minus', 'plus_minus', -4), ('FG%', 'fg_pct', .5)]:
            row = self.context('Josh Giddey career ' + query + ' against Knicks')['opponent_player_statistics'][0]
            self.assertEqual(row['averages'][metric], expected)
        result = self.context('How many total points did Josh Giddey score in his career against Knicks?')
        row = result['opponent_player_statistics'][0]
        self.assertEqual((row['totals']['pts'], row['aggregation']), (60, 'totals'))
        self.assertIn('60 points total', evidence_cards(result, 'total points')[0]['text'])

    def test_duplicate_alias_rows_are_counted_once_and_conflicting_values_unknown(self):
        with self.master.connect() as db:
            row = dict(db.execute("SELECT * FROM player_games WHERE date='2026-11-04'").fetchone())
        row['player_id'] = 'provider:giddey'
        self.master.upsert_player_game(row)
        result = self.context()['opponent_player_statistics'][0]
        self.assertEqual((result['n'], result['observed_counts']['pts']), (3, 3))
        self.assertEqual(result['duplicate_rows'], 1)
        row['pts'] = 999
        self.master.upsert_player_game(row)
        result = self.context()['opponent_player_statistics'][0]
        self.assertEqual((result['n'], result['observed_counts']['pts']), (3, 2))
        self.assertEqual(result['averages']['pts'], 20)
        self.assertEqual(result['conflicting_metric_values'], 1)

    def test_source_cards_call_career_incomplete_and_do_not_claim_named_guarding(self):
        result = self.context()
        cards = evidence_cards(result, 'Josh Giddey lifetime points against Knicks')
        matchup = next(card for card in cards if card['kind'] == 'player_opponent_sample')
        self.assertIn('Available career', matchup['text'])
        self.assertIn('completeness is not independently verified', matchup['text'])
        self.assertIn('not measured guarding', matchup['text'])
        self.assertIn('available career', matchup['prompt_descriptor'])
        resolved = {'players': ['Josh Giddey', 'Jalen Brunson'], 'teams': [],
                    'original_question': 'Josh Giddey lifetime points against Jalen Brunson'}
        pair = research_context(self.book, self.master, resolved)
        self.assertEqual(pair['opponent_player_statistics'], [])
        self.assertTrue(all(row['population'] == 'career_all_opponents' for row in pair['current_player_statistics']))
        resolved.update(teams=['BOS'], original_question='Josh Giddey lifetime points against Jalen Brunson of Boston')
        pair = research_context(self.book, self.master, resolved)
        self.assertEqual(pair['opponent_player_statistics'], [])

    def test_invalid_date_is_clarified_and_read_only(self):
        with self.master.connect() as db:
            before = '\n'.join(db.iterdump())
        result = self.context('Josh Giddey lifetime points as of 2026-02-30')
        self.assertIn('invalid', result['population_error'])
        self.assertEqual(result['current_player_statistics'], [])
        self.assertEqual(evidence_cards(result)[0]['kind'], 'research_scope_error')
        self.context()
        with self.master.connect() as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))

    def test_lifetime_request_routes_as_research_but_priced_pick_remains_prop(self):
        turn = resolve_conversation(self.book, self.master, 'Josh Giddey lifetime points against Knicks', [], now=NOW)
        self.assertEqual(turn['intent'], 'research')
        self.assertFalse(turn.get('clarification'))
        turn = resolve_conversation(self.book, self.master,
            'Is Josh Giddey more 7.5 assists a good pick based on his career stats?', [], now=NOW)
        self.assertIn(turn['intent'], {'prop', 'explanation'})
        self.assertEqual(turn['slots']['line'], 7.5)
        turn = resolve_conversation(self.book, self.master,
            'Josh Giddey lifetime points against Knicks as of 2025-02-01', [], now=NOW)
        self.assertEqual(turn['intent'], 'research')
        self.assertFalse(turn.get('clarification'))

    def test_team_followup_keeps_career_scope_and_explicit_current_season_resets_it(self):
        history = [{'role': 'user', 'content': 'Josh Giddey lifetime points against Knicks'}]
        resolved = resolve_question(self.book, self.master, 'And against Boston?', history)
        result = research_context(self.book, self.master, resolved)
        self.assertEqual(result['requested_population']['season_basis'], 'available career')
        self.assertEqual((result['opponent_player_statistics'][0]['n'],
                          result['opponent_player_statistics'][0]['averages']['pts']), (1, 40))
        resolved = resolve_question(self.book, self.master, 'And against Boston this season?', history)
        result = research_context(self.book, self.master, resolved)
        self.assertEqual(result['requested_population']['season_basis'], 'current season')
        self.assertEqual(result['opponent_player_statistics'][0]['n'], 0)

    def test_reviewed_identity_alias_is_deduplicated_without_suffix_guessing(self):
        self.add('2026-11-04', 'MIL', 'NYK', 7, player='Bobby Portis')
        self.add('2026-11-04', 'MIL', 'NYK', 7, player='Bobby Portis Jr.')
        self.add('2025-02-01', 'MIL', 'NYK', 12, player='Bobby Portis', season='2024-25')
        resolved = resolve_question(self.book, self.master, 'Bobby Portis Jr. lifetime points against Knicks')
        self.assertEqual(resolved['players'], ['Bobby Portis'])
        result = research_context(self.book, self.master, resolved)
        row = result['opponent_player_statistics'][0]
        self.assertEqual((row['n'], row['averages']['pts']), (2, 9.5))
        self.assertEqual(row['duplicate_rows'], 1)
        self.add('2026-11-04', 'MIL', 'NYK', 8, player='Example Guard Jr.')
        self.add('2025-02-01', 'MIL', 'NYK', 90, player='Example Guard', season='2024-25')
        result = research_context(self.book, self.master, {'players': ['Example Guard Jr.'],
            'teams': ['NYK'], 'original_question': 'Example Guard Jr. lifetime points against Knicks'})
        self.assertEqual((result['opponent_player_statistics'][0]['n'],
                          result['opponent_player_statistics'][0]['averages']['pts']), (1, 8))


if __name__ == '__main__':
    unittest.main()
