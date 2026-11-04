"""Projection samples require consistent player, event and duplicate identities."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from contextual_projection import _comparable_observations, projection_for_leg
from feeds import canonical_game, canonical_player
from master import MasterStore


NOW = dt.datetime(2026, 11, 4, 19, tzinfo=dt.timezone.utc)
DAY = '2026-11-04'
PLAYER = 'Jalen Brunson'


class PregameProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')

    def add(self, day, *, player=PLAYER, player_id=None, game_id=None, team='NYK',
            opponent='BOS', home='NYK', away='BOS', event_date=None,
            season='2026-27', pts=24, reb=5, ast=4, minutes=32, fga=18,
            source='isolated projection fixture'):
        game_id = game_id or canonical_game(day, home, away)
        self.store.upsert_game({'id': game_id, 'date': event_date or day,
            'home_team': home, 'away_team': away, 'status': 'final', 'source': source})
        row = {'game_id': game_id, 'date': day, 'player': player,
            'player_id': player_id or canonical_player(player), 'team': team,
            'opponent': opponent, 'season': season, 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'pts': pts, 'reb': reb, 'ast': ast,
            'minutes': minutes, 'fga': fga, 'source': source}
        self.store.upsert_player_game(row)
        return row

    def base(self, n=8):
        return [self.add((dt.date(2026, 11, 3) - dt.timedelta(days=i)).isoformat())
                for i in range(n)]

    def alias(self, row, player_id='provider:alias', **changes):
        copied = {**row, 'player_id': player_id, **changes}
        self.store.upsert_player_game(copied)
        return copied

    def project(self, **changes):
        return projection_for_leg(self.store, {'player': PLAYER, 'team': 'NYK',
            'date': DAY, 'stat': 'pts', **changes}, NOW)

    def sample(self, **changes):
        arguments = {'player': PLAYER, 'player_id': canonical_player(PLAYER),
                     'team': 'NYK', 'cutoff': DAY, 'components': ['pts']}
        return _comparable_observations(self.store, **{**arguments, **changes})

    def assert_sample_unchanged(self, actual, expected):
        for field in ('n', 'reference_projection', 'projected_minutes', 'per_minute_baseline',
                      'empirical_scenario_range', 'first_date', 'last_date', 'actual_teams', 'seasons'):
            self.assertEqual(actual[field], expected[field], field)

    def test_reused_player_id_does_not_admit_another_players_statistics(self):
        self.base()
        before = self.project()
        self.add('2026-10-20', player='Julius Randle',
                 player_id=canonical_player(PLAYER), pts=100, minutes=40)
        self.assert_sample_unchanged(self.project(), before)
        self.add('2026-10-19', player='Josh Hart', player_id='provider:collision', pts=90)
        self.assert_sample_unchanged(self.project(player_id='provider:collision'), before)

    def test_player_date_must_match_the_joined_final_game(self):
        self.base()
        before = self.project()
        self.add('2026-11-03', game_id='bad-date-fixture', event_date='2026-10-20', pts=90)
        result = self.project()
        self.assert_sample_unchanged(result, before)
        self.assertNotIn('bad-date-fixture', [r['game_id'] for r in result['source_games']])

    def test_exact_name_provider_ids_and_punctuation_aliases_count_one_game(self):
        rows = self.base()
        before = self.project()
        for row in rows:
            self.alias(row, player='Jalen-Brunson')
        result = self.project()
        self.assert_sample_unchanged(result, before)
        self.assertEqual(result['sample_quality']['duplicate_rows'], len(rows))
        self.assertEqual(len(result['source_games']), 8)

    def test_provider_only_identity_is_resolved_from_exact_normalized_name(self):
        self.base()
        before = self.project()
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET player_id=?', ('nba-provider:12345',))
        self.assert_sample_unchanged(self.project(), before)

    def test_alias_rows_cannot_inflate_a_subminimum_number_of_games(self):
        rows = self.base(n=7)
        for row in rows:
            self.alias(row)
        result = self.project()
        self.assertFalse(result['available'])
        self.assertEqual(result['observations'], 7)
        self.assertEqual(result['sample_quality']['duplicate_rows'], 7)

    def test_conflicting_complete_component_excludes_the_whole_game(self):
        rows = self.base()
        self.alias(rows[0], pts=90)
        result = self.project()
        self.assertFalse(result['available'])
        self.assertEqual(result['observations'], 7)
        self.assertEqual(result['sample_quality']['conflicting_games'], 1)

    def test_partial_alias_with_a_conflicting_component_cannot_be_cherry_picked(self):
        rows = self.base()
        self.alias(rows[0], pts=None, reb=90)
        result = self.project(stat='pra')
        self.assertFalse(result['available'])
        self.assertEqual(result['observations'], 7)
        self.assertEqual(result['sample_quality']['conflicting_games'], 1)

    def test_conflicting_minutes_exclude_the_game_even_when_statistics_agree(self):
        rows = self.base()
        for minutes in (16, 0):
            with self.subTest(minutes=minutes):
                self.alias(rows[0], minutes=minutes)
                result = self.project()
                self.assertFalse(result['available'])
                self.assertEqual(result['observations'], 7)
                self.assertEqual(result['sample_quality']['conflicting_games'], 1)

    def test_complete_alias_can_replace_missing_values_without_combining_partial_rows(self):
        rows = self.base()
        self.alias(rows[0], pts=None, minutes=None)
        valid, quality = self.sample()
        self.assertEqual(len(valid), 8)
        self.assertEqual(quality['conflicting_games'], 0)
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET pts=NULL WHERE game_id=? AND player_id=?',
                       (rows[0]['game_id'], canonical_player(PLAYER)))
            db.execute('UPDATE player_games SET pts=24,minutes=NULL WHERE game_id=? AND player_id=?',
                       (rows[0]['game_id'], 'provider:alias'))
        incomplete = self.project()
        self.assertFalse(incomplete['available'])
        self.assertEqual(incomplete['observations'], 7)

    def test_conflicting_optional_fga_is_not_quoted_as_shot_volume(self):
        rows = self.base()
        before = self.project()
        self.assertTrue(any('Recent shot volume:' in reason for reason in before['supporting_reasons']))
        for row in rows[:5]:
            self.alias(row, fga=99)
        result = self.project()
        self.assert_sample_unchanged(result, before)
        self.assertFalse(any('Recent shot volume:' in reason for reason in result['supporting_reasons']))
        # For an actual FGA projection the same discrepancy is a required-stat
        # conflict, so those games cannot support the eight-game minimum.
        fga = self.project(stat='fga')
        self.assertFalse(fga['available'])
        self.assertEqual(fga['observations'], 3)

    def test_provider_game_aliases_for_the_same_event_count_once(self):
        rows = self.base()
        before = self.project()
        for number, row in enumerate(rows):
            self.add(row['date'], game_id='provider-game:' + str(number))
        result = self.project()
        self.assert_sample_unchanged(result, before)
        self.assertEqual(result['sample_quality']['duplicate_rows'], 8)

    def test_all_aliases_of_an_included_game_are_checked_before_sampling(self):
        rows = self.base()
        for number in range(105):
            self.alias(rows[0], player_id='provider:' + str(number))
        result = self.project()
        self.assertTrue(result['available'])
        self.assertEqual(result['n'], 8)
        self.assertEqual(result['sample_quality']['duplicate_rows'], 105)
        self.alias(rows[0], player_id='provider:last-conflict', pts=99)
        conflict = self.project()
        self.assertFalse(conflict['available'])
        self.assertEqual(conflict['observations'], 7)
        self.assertEqual(conflict['sample_quality']['conflicting_games'], 1)

    def test_same_team_bubble_regular_season_rows_preserve_multiseason_fallback(self):
        self.base()
        self.add('2020-08-13', season='2019-20')
        self.add('2020-07-30', season='2019-20')
        rows, quality = self.sample()
        self.assertEqual(len(rows), 10)
        self.assertEqual({row['season'] for row in rows}, {'2019-20', '2026-27'})
        self.assertEqual(quality['invalid_season_rows'], 0)

    def test_wrong_team_and_opponent_relationships_cannot_create_cohorts(self):
        self.base()
        before = self.project(opponent='PHI')
        self.add('2026-10-20', home='PHI', away='BOS', team='NYK', pts=90)
        self.add('2026-10-19', opponent='PHI', pts=90)
        result = self.project(opponent='PHI')
        self.assert_sample_unchanged(result, before)
        self.assertEqual(result['sample_quality']['invalid_event_rows'], 2)
        self.assertEqual(result['cohorts'][0]['n'], 0)

    def test_missing_opponent_is_resolved_from_the_joined_event(self):
        self.base()
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET opponent=NULL')
        result = self.project(opponent='BOS')
        self.assertTrue(result['available'])
        self.assertEqual(result['cohorts'][0]['n'], 8)

    def test_invalid_season_labels_are_excluded_without_breaking_valid_history(self):
        self.base()
        for index, season in enumerate((None, 'unknown', '2026-29', '2010-11')):
            self.add('2026-10-' + str(20 - index), season=season, pts=90)
        result = self.project()
        self.assertTrue(result['available'])
        self.assertEqual(result['n'], 8)
        self.assertEqual(result['seasons'], ['2026-27'])
        self.assertEqual(result['sample_quality']['invalid_season_rows'], 4)


if __name__ == '__main__':
    unittest.main()
