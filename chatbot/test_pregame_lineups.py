"""Pregame selection honors preferences without bypassing event/evidence gates."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from analysis_engine import analyze_board
from board_shortlist import enrich_shortlist, player_availability
from feeds import canonical_game, canonical_player
from independent_picks import build_independent_picks
from master import MasterStore
from season_calendar import seed_calendar
from test_board_shortlist import candidate, report


DAY = '2026-10-30'
NOW = dt.datetime(2026, 10, 30, 22, 40, tzinfo=dt.timezone.utc)
GAME = canonical_game(DAY, 'NYK', 'PHI')


class PregameLineupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)

    def observations(self, player, team, n=20):
        other = 'BOS' if team != 'BOS' else 'TOR'
        for offset in range(n):
            day = (dt.date(2026, 10, 29) - dt.timedelta(days=offset)).isoformat()
            game = canonical_game(day, team, other)
            self.store.upsert_game({'id': game, 'date': day, 'home_team': team,
                'away_team': other, 'status': 'final', 'source': 'isolated pregame fixture'})
            self.store.upsert_player_game({'game_id': game, 'date': day, 'player': player,
                'player_id': canonical_player(player), 'team': team, 'opponent': other,
                'season': '2026-27', 'season_type': 'Regular Season', 'appearance_state': 'Played',
                'minutes': 33, 'pts': 24 + offset % 3, 'reb': 6, 'ast': 4,
                'fga': 18 + offset % 2, 'source': 'isolated pregame fixture'})
        self.refresh_fixture()

    def refresh_fixture(self):
        """An explicit verified publication for this isolated synthetic league."""
        with self.store.connect() as db:
            games = [dict(row) for row in db.execute("SELECT * FROM games WHERE date=? AND status='final'",
                                                    ('2026-10-29',))]
        teams = {}
        for game in games:
            for team in (game['home_team'], game['away_team']):
                teams.setdefault(team, []).append(game['id'])
        self.store.set_checkpoint('daily_nba:2026-10-29', {
            'date': '2026-10-29', 'complete': True, 'games': len(games), 'expected_games': len(games),
            'source': 'isolated verified fixture publication', 'inventory_scope': 'Complete synthetic fixture inventory',
            'last_checked_at': NOW.isoformat(), 'last_refresh_status': 'success', 'last_refresh_error': None,
            'imported_at': NOW.isoformat(), 'publisher_retrieved_at': NOW.isoformat(),
            'inventory_verified': True, 'inventory_verified_at': NOW.isoformat(),
            'inventory_verification_source': 'independent synthetic schedule fixture',
            'final_game_ids': [game['id'] for game in games], 'team_final_game_ids': teams})
        with self.store.connect() as db:
            targets = {row['id']: dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE date=?', (DAY,))}
            for row in db.execute('SELECT * FROM games WHERE date=?', (DAY,)):
                targets[row['id']] = {**targets.get(row['id'], {}), **dict(row)}
        self.store.set_checkpoint('nba_schedule_today', {'date': DAY, 'status': 'success', 'error': None,
            'checked_at': NOW.isoformat(), 'source': 'independent synthetic current-game scoreboard',
            'games': list(targets.values())})

    def leg(self, player='Jalen Brunson', team='NYK', stat='pts', line=10.5, **extra):
        return {'player': player, 'team': team, 'stat': stat, 'line': line,
                'side': 'more', 'date': DAY, **extra}

    def board(self, legs, **kwargs):
        board = self.store.create_board(legs, source='isolated pregame board fixture')
        return enrich_shortlist(self.store, analyze_board(self.store, board['id'], NOW),
                                NOW, **kwargs)

    def observe_target(self, **extra):
        self.store.upsert_game({'id': GAME, 'date': DAY, 'home_team': 'NYK',
            'away_team': 'PHI', 'status': 'scheduled',
            'start_time': '2026-10-30T23:30:00+00:00', 'source': 'observed event fixture', **extra})

    def financial_counts(self):
        with self.store.connect() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                         for name in ('entries', 'grade_events'))

    def test_actual_multi_market_board_honors_attempts_after_eligibility(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Tyrese Maxey', 'PHI')
        legs = [self.leg(), self.leg(stat='fga', line=15.5),
                self.leg('Tyrese Maxey', 'PHI')]
        before = self.financial_counts()
        ordinary = self.board(legs, count=2)
        preferred = self.board(legs, count=2, constraints={'preferred_stat': 'fga'})
        first = {p['leg']['player']: p['leg'] for p in ordinary['shortlist']}
        changed = {p['leg']['player']: p['leg'] for p in preferred['shortlist']}
        self.assertEqual(first['Jalen Brunson']['stat'], 'pts')
        self.assertEqual((changed['Jalen Brunson']['stat'], changed['Jalen Brunson']['line']), ('fga', 15.5))
        self.assertEqual(changed['Tyrese Maxey']['stat'], 'pts')
        self.assertTrue(preferred['lineup_complete'])
        self.assertEqual(preferred['lineup_constraints']['preferred_stat'], 'fga')
        self.assertEqual(self.financial_counts(), before)
        self.assertTrue(all('win_probability' not in item for item in preferred['shortlist']))

    def test_preferred_unqualified_market_and_sparse_opponent_do_not_become_filler(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Tyrese Maxey', 'PHI', n=2)
        data = self.board([self.leg(), self.leg(stat='fga', line=30.5),
                           self.leg('Tyrese Maxey', 'PHI')], count=2,
                          constraints={'preferred_stat': 'fga'})
        self.assertEqual([(p['leg']['player'], p['leg']['stat']) for p in data['shortlist']],
                         [('Jalen Brunson', 'pts')])
        self.assertFalse(data['lineup_complete'])
        self.assertEqual(data['missing_teams'], ['PHI'])
        self.assertTrue(any('opposite offered side' in p['reason'] for p in data['skipped'] if p['stat'] == 'fga'))

    def test_preferred_market_cannot_resolve_conflicting_offer_variants(self):
        self.observations('Jalen Brunson', 'NYK')
        data = self.board([self.leg(), self.leg(stat='fga', line=15.5, offer_variant='goblin'),
                           self.leg(stat='fga', line=15.5, offer_variant='demon')], count=1,
                          constraints={'preferred_stat': 'fga'})
        self.assertEqual(data['shortlist'][0]['leg']['stat'], 'pts')
        self.assertTrue(any('Conflicting offer variants' in item['reason'] for item in data['skipped']))

    def test_unsupported_preference_does_not_create_an_unavailable_market(self):
        self.observations('Jalen Brunson', 'NYK')
        item = candidate()
        default = enrich_shortlist(self.store, report(item), NOW, count=1)
        unknown = enrich_shortlist(self.store, report(item), NOW, count=1,
                                   constraints={'preferred_stat': 'first_quarter_points'})
        self.assertEqual(default['shortlist'], unknown['shortlist'])
        self.assertNotIn('preferred_stat', unknown['lineup_constraints'])

    def test_earlier_observed_tipoff_blocks_that_game_but_preserves_later_game(self):
        self.observe_target(start_time='2026-10-30T22:00:00+00:00')
        later_id = canonical_game(DAY, 'LAL', 'PHX')
        self.store.upsert_game({'id': later_id, 'date': DAY, 'home_team': 'LAL',
            'away_team': 'PHX', 'status': 'scheduled', 'start_time': '2026-10-31T02:00:00+00:00',
            'source': 'late event fixture'})
        for player, team in [('Jalen Brunson', 'NYK'), ('Late Laker', 'LAL'), ('Late Sun', 'PHX')]:
            self.observations(player, team)
        data = self.board([self.leg(), self.leg('Late Laker', 'LAL', game_id=later_id),
                           self.leg('Late Sun', 'PHX', game_id=later_id)], count=2)
        self.assertEqual({p['leg']['player'] for p in data['shortlist']}, {'Late Laker', 'Late Sun'})
        self.assertTrue(data['lineup_complete'])
        self.assertTrue(any(p['player'] == 'Jalen Brunson' and 'tipoff has passed' in p['reason']
                            for p in data['skipped']))
        self.assertEqual(build_independent_picks(self.store, DAY, now=NOW)['picks'], [])

    def test_exact_unknown_game_id_never_falls_back_to_another_saved_event(self):
        item = candidate()
        item['leg']['game_id'] = 'not-the-saved-game'
        data = enrich_shortlist(self.store, report(item), NOW, count=1)
        self.assertEqual(data['shortlist'], [])
        self.assertIn('specified game is not', data['skipped'][0]['reason'])

    def test_saved_calendar_exact_id_resolves_without_an_observed_game_or_date(self):
        self.observations('Jalen Brunson', 'NYK')
        item = candidate()
        item['leg'].pop('date')
        item['leg']['game_id'] = GAME
        data = enrich_shortlist(self.store, report(item), NOW, count=1)
        self.assertEqual(data['shortlist'][0]['leg']['date'], DAY)
        self.assertEqual(data['shortlist'][0]['context']['game_id'], GAME)

    def test_explicit_opponent_conflict_is_held(self):
        item = candidate()
        item['leg'].update(game_id=GAME, opponent='BOS')
        data = enrich_shortlist(self.store, report(item), NOW, count=1)
        self.assertFalse(data['shortlist'])
        self.assertIn('opponent conflicts', data['skipped'][0]['reason'])

    def test_calendar_postponement_cannot_be_overridden_by_scheduled_observation(self):
        self.observe_target()
        self.observations('Jalen Brunson', 'NYK')
        with self.store.connect() as db:
            db.execute('UPDATE season_schedule SET status=? WHERE id=?', ('postponed', GAME))
        self.assertFalse(enrich_shortlist(self.store, report(candidate()), NOW, count=1)['shortlist'])
        self.assertFalse(build_independent_picks(self.store, DAY, count=1, now=NOW)['picks'])

    def test_actual_engines_hold_when_latest_daily_check_failed(self):
        self.observations('Jalen Brunson', 'NYK')
        checkpoint = self.store.get_checkpoint('daily_nba:2026-10-29')
        checkpoint.update(last_refresh_status='failed', last_refresh_error='ReadTimeout')
        self.store.set_checkpoint('daily_nba:2026-10-29', checkpoint)
        before = self.financial_counts()
        board = self.board([self.leg()], count=1)
        research = build_independent_picks(self.store, DAY, count=1, now=NOW)
        self.assertFalse(board['shortlist'])
        self.assertFalse(research['picks'])
        self.assertIn('Current game data is not ready', board['skipped'][0]['reason'])
        self.assertIn('/freshness', research['note'])
        self.assertEqual(self.financial_counts(), before)

    def test_latest_stat_missing_prevents_old_data_from_driving_that_market(self):
        self.observations('Jalen Brunson', 'NYK')
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET fga=NULL WHERE player=? AND date=?',
                       ('Jalen Brunson', '2026-10-29'))
        board = self.board([self.leg(stat='fga', line=15.5)], count=1)
        research = build_independent_picks(self.store, DAY, count=1, now=NOW,
                                          constraints={'preferred_stat': 'fga'})
        self.assertFalse(board['shortlist'])
        self.assertIn('statistic is missing', board['skipped'][0]['reason'])
        self.assertTrue(research['picks'])
        self.assertNotEqual(research['picks'][0]['stat'], 'fga')

    def test_watchlist_omits_stale_player_even_when_older_volume_looks_useful(self):
        self.observations('Jalen Brunson', 'NYK')
        self.observations('Josh Hart', 'NYK')
        self.observations('Tyrese Maxey', 'PHI')
        with self.store.connect() as db:
            db.execute('DELETE FROM player_games WHERE player=? AND date=?', ('Josh Hart', '2026-10-29'))
        result = self.board([self.leg()], count=1)
        self.assertEqual([item['leg']['player'] for item in result['shortlist']], ['Jalen Brunson'])
        self.assertNotIn('Josh Hart', [item['player'] for item in result['watchlist']])
        self.assertIn('Tyrese Maxey', [item['player'] for item in result['watchlist']])
        self.assertTrue(any(item['player'] == 'Josh Hart' and 'not ready' in item['reason']
                            for item in result['watchlist_held']))

    def test_tipoff_on_a_different_eastern_game_date_is_held_in_both_paths(self):
        self.observe_target(start_time='2026-11-01T23:30:00+00:00')
        self.observations('Jalen Brunson', 'NYK')
        data = enrich_shortlist(self.store, report(candidate()), NOW, count=1)
        self.assertFalse(data['shortlist'])
        self.assertIn('tipoff conflicts', data['skipped'][0]['reason'])
        self.assertFalse(build_independent_picks(self.store, DAY, count=1, now=NOW)['picks'])

    def test_simultaneous_clearance_and_out_report_hold_regardless_of_feed_order(self):
        shared = {'published_at': '2026-10-30T22:00:00+00:00', 'source': 'publisher fixture'}
        out = {**shared, 'id': 'out', 'title': 'Jalen Brunson: Ruled out for Friday'}
        cleared = {**shared, 'id': 'cleared', 'title': 'Jalen Brunson: Cleared to play Friday'}
        for values in ([out, cleared], [cleared, out]):
            result = player_availability(values, 'Jalen Brunson', DAY, NOW)
            self.assertTrue(result['blocked'])
            self.assertEqual({r['state'] for r in result['reports']}, {'out_report', 'available_report'})
        for item in [out, cleared]:
            self.store.record_news(item)
        self.observations('Jalen Brunson', 'NYK')
        self.assertFalse(self.board([self.leg()], count=1)['shortlist'])
        self.assertFalse(build_independent_picks(self.store, DAY, count=1, now=NOW)['picks'])


if __name__ == '__main__':
    unittest.main()
