import datetime as dt
from pathlib import Path
import tempfile
import unittest

from analysis_engine import analyze_board
from board_shortlist import enrich_shortlist, format_shortlist, player_availability
from feeds import canonical_game, canonical_player
from master import MasterStore
from season_calendar import seed_calendar


NOW = dt.datetime(2026, 10, 30, 22, 40, tzinfo=dt.timezone.utc)
DATE = '2026-10-30'


def candidate(player='Jalen Brunson', side='more', line=19.5, team='NYK', stat='pts', **changes):
    value = {'leg': {'player': player, 'side': side, 'line': line, 'team': team, 'stat': stat, 'date': DATE},
             'eligible_for_ranking': True, 'n': 20, 'reason': 'Enough observations.',
             'projection': {'available': True, 'reference_projection': 25.5,
                            'selected_side_margin': 6 if side == 'more' else -6,
                            'standardized_margin': .75 if side == 'more' else -.75,
                            'projected_minutes': 34, 'n': 20, 'last_date': '2026-10-29',
                            'seasons': ['2026-27'], 'cohorts': [
                                {'type': 'opponent_team', 'team': 'PHI', 'n': 2, 'adjustment': 0}]}}
    value.update(changes)
    return value


def report(*items):
    return {'id': 'test-report', 'board_id': 'test-board', 'generated_at': NOW.isoformat(),
            'original_legs': [item['leg'] for item in items], 'legs': list(items)}


class ShortlistTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)

    def observations(self, player, team='NYK', n=10, minutes=32, pts=20, reb=6, ast=5):
        for offset in range(n):
            day = (dt.date(2026, 10, 29) - dt.timedelta(days=offset)).isoformat()
            other = 'BOS' if team != 'BOS' else 'PHI'
            game_id = canonical_game(day, team, other)
            self.store.upsert_game({'id': game_id, 'date': day, 'status': 'final',
                                    'home_team': team, 'away_team': other, 'source': 'synthetic fixture'})
            self.store.upsert_player_game({'game_id': game_id, 'date': day,
                'player_id': canonical_player(player), 'player': player, 'team': team,
                'opponent': other, 'appearance_state': 'Played', 'season': '2026-27',
                'season_type': 'Regular Season', 'minutes': minutes, 'pts': pts + offset % 4,
                'reb': reb, 'ast': ast, 'fga': 15, 'source': 'synthetic fixture'})

    def news(self, player='Jalen Brunson', status='Ruled out for Friday', published='2026-10-30T18:00:00-04:00', **extra):
        return {'id': status + published, 'title': f'{player}: {status}',
                'body': 'An ankle injury was reported.', 'published_at': published,
                'source': 'test publisher', 'url': 'https://publisher.example/report', **extra}

    def test_best_provided_side_and_one_market_per_player(self):
        high = candidate()
        another = candidate(stat='pra', line=30.5)
        another['projection']['standardized_margin'] = .25
        low = candidate(side='less')
        maxey = candidate('Tyrese Maxey', team='PHI')
        result = enrich_shortlist(self.store, report(high, another, low, maxey), NOW)
        self.assertEqual(len(result['shortlist']), 2)
        self.assertEqual([item['leg']['side'] for item in result['shortlist']], ['more', 'more'])
        self.assertEqual(len({item['leg']['player'] for item in result['shortlist']}), 2)
        self.assertIn('no matchup adjustment', result['shortlist'][0]['reason'])

    def test_insufficient_projection_or_sample_does_not_become_pick(self):
        first = candidate(eligible_for_ranking=False, n=5, reason='Only 5 observed prior games; at least 10 needed.')
        second = candidate('Josh Hart', projection={'available': False, 'reason': 'Missing minutes.'})
        result = enrich_shortlist(self.store, report(first, second), NOW)
        self.assertEqual(result['shortlist'], [])
        text = format_shortlist(result)
        self.assertIn('Only 5 observed', text)
        self.assertIn('Missing minutes', text)

    def test_multiple_thresholds_require_offer_resolution(self):
        result = enrich_shortlist(self.store, report(candidate(line=19.5), candidate(line=29.5)), NOW)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('Multiple thresholds', result['skipped'][0]['reason'])
        self.assertEqual(len(result['skipped']), 1)

    def test_offer_variant_preserved_and_displayed_without_assumed_multiplier(self):
        for variant in ('standard', 'goblin', 'demon', 'unknown'):
            item = candidate()
            item['leg']['offer_variant'] = variant
            value = enrich_shortlist(self.store, report(item), NOW)
            self.assertEqual(len(value['shortlist']), 1, variant)
            self.assertEqual(value['shortlist'][0]['leg']['offer_variant'], variant)
            text = format_shortlist(value)
            self.assertIn({'standard': 'standard offer', 'goblin': 'Goblin offer',
                           'demon': 'Demon offer', 'unknown': 'offer type unverified'}[variant], text)
            if variant in {'goblin', 'demon'}:
                self.assertIn('payouts depend on the actual offer', text)
                self.assertIn('no universal multiplier', text)

    def test_conflicting_variants_for_exact_same_selection_are_held(self):
        first, second = candidate(), candidate()
        first['leg']['offer_variant'] = 'goblin'
        second['leg']['offer_variant'] = 'demon'
        value = enrich_shortlist(self.store, report(first, second), NOW)
        self.assertEqual(value['shortlist'], [])
        self.assertEqual({item['offer_variant'] for item in value['skipped']}, {'goblin', 'demon'})
        self.assertTrue(all('Conflicting offer variants' in item['reason'] for item in value['skipped']))
        text = format_shortlist(value)
        self.assertIn('Goblin offer', text)
        self.assertIn('Demon offer', text)

    def test_opposite_sides_can_have_different_offer_variants(self):
        more = candidate('Karl-Anthony Towns', stat='tov', line=2.5)
        more['leg']['offer_variant'] = 'goblin'
        less = candidate('Karl-Anthony Towns', stat='tov', line=2.5, side='less')
        less['leg']['offer_variant'] = 'demon'
        value = enrich_shortlist(self.store, report(more, less), NOW)
        self.assertEqual(len(value['shortlist']), 1)
        self.assertEqual(value['shortlist'][0]['leg']['offer_variant'], 'goblin')
        self.assertFalse(any('Conflicting offer variants' in item['reason'] for item in value['skipped']))

    def test_held_variant_remains_visible_when_game_has_started(self):
        item = candidate()
        item['leg']['offer_variant'] = 'demon'
        value = enrich_shortlist(self.store, report(item), NOW + dt.timedelta(hours=2))
        self.assertEqual(value['shortlist'], [])
        self.assertEqual(value['skipped'][0]['offer_variant'], 'demon')
        self.assertIn('Demon offer', format_shortlist(value))

    def test_started_calendar_and_stale_workload_are_held(self):
        later = NOW + dt.timedelta(hours=2)
        result = enrich_shortlist(self.store, report(candidate()), later)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('tipoff has passed', result['skipped'][0]['reason'])
        old = candidate()
        old['projection']['last_date'] = '2026-10-12'
        result = enrich_shortlist(self.store, report(old), NOW)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('seven days', result['skipped'][0]['reason'])

    def test_missing_event_or_tipoff_is_not_a_pick(self):
        item = candidate('Other Player', team='LAL')
        result = enrich_shortlist(self.store, report(item), NOW)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('event timing is unresolved', result['skipped'][0]['reason'])
        self.store.upsert_game({'id': 'other-game', 'date': DATE, 'home_team': 'LAL',
            'away_team': 'BOS', 'status': 'scheduled', 'source': 'fixture'})
        self.assertEqual(enrich_shortlist(self.store, report(item), NOW)['shortlist'], [])
        self.store.upsert_game({'id': 'other-game', 'date': DATE, 'home_team': 'LAL',
            'away_team': 'BOS', 'status': 'scheduled', 'start_time': '2026-10-31T02:00:00Z', 'source': 'fixture'})
        self.assertEqual(len(enrich_shortlist(self.store, report(item), NOW)['shortlist']), 1)

    def test_exact_game_id_with_no_date_resolves_without_guessing(self):
        self.store.upsert_game({'id': 'other-game', 'date': DATE, 'home_team': 'LAL',
            'away_team': 'BOS', 'status': 'scheduled', 'start_time': '2026-10-31T02:00:00Z', 'source': 'fixture'})
        item = candidate('Other Player', team='LAL')
        item['leg'].pop('date')
        item['leg']['game_id'] = 'other-game'
        self.assertEqual(enrich_shortlist(self.store, report(item), NOW)['shortlist'][0]['leg']['date'], DATE)
        item['leg']['team'] = 'NYK'
        result = enrich_shortlist(self.store, report(item), NOW)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('conflicts', result['skipped'][0]['reason'])

    def test_actual_game_status_overrides_saved_calendar(self):
        self.store.upsert_game({'id': canonical_game(DATE, 'NYK', 'PHI'), 'date': DATE,
            'home_team': 'NYK', 'away_team': 'PHI', 'status': 'postponed', 'source': 'fixture'})
        value = enrich_shortlist(self.store, report(candidate()), NOW)
        self.assertEqual(value['shortlist'], [])
        self.assertIn('postponed', value['skipped'][0]['reason'])

    def test_out_report_blocks_as_publisher_report_not_verified_fact(self):
        self.store.record_news(self.news())
        result = enrich_shortlist(self.store, report(candidate()), NOW)
        self.assertEqual(result['shortlist'], [])
        self.assertIn('publisher reports', result['skipped'][0]['reason'])
        self.assertNotIn('official', result['skipped'][0]['reason'])

    def test_unknown_timezone_out_report_is_unresolved_hold(self):
        item = self.news(published='2026-10-30T18:00:00')
        value = player_availability([item], 'Jalen Brunson', DATE, NOW)
        self.assertTrue(value['blocked'])
        self.assertIn('unconfirmed timezone', value['note'])
        self.assertFalse(value['reports'][0]['timing_confirmed'])

    def test_bare_headline_weekday_matches_only_target_game_day(self):
        for status, blocked in (('Out Friday', True), ('Questionable Friday', False)):
            item = self.news(status=status, published='2026-10-29T18:00:00-04:00',
                             body="Brunson's status was reported for Friday's game against Philadelphia.")
            friday = player_availability([item], 'Jalen Brunson', DATE, NOW)
            self.assertEqual(friday['blocked'], blocked, status)
            self.assertEqual(len(friday['reports']), 1, status)
            self.assertEqual(friday['reports'][0]['state'], 'out_report' if blocked else 'uncertain')
            monday = player_availability([item], 'Jalen Brunson', '2026-11-02', NOW)
            self.assertEqual(monday['reports'], [], status)

    def test_body_possessive_game_day_without_headline_weekday_is_recognized(self):
        item = self.news(status='Out', published='2026-10-29T18:00:00-04:00',
                         body="Brunson has been ruled out of Friday's game against Philadelphia.")
        self.assertTrue(player_availability([item], 'Jalen Brunson', DATE, NOW)['blocked'])
        self.assertEqual(player_availability([item], 'Jalen Brunson', '2026-11-02', NOW)['reports'], [])

    def test_subject_weekday_is_not_replaced_by_other_body_weekday(self):
        item = self.news(status='Out Monday', published='2026-10-29T18:00:00-04:00',
                         body="Josh Hart may miss Friday's game; Brunson's absence is for Monday.")
        self.assertEqual(player_availability([item], 'Jalen Brunson', DATE, NOW)['reports'], [])
        item = self.news(status='Will miss practice Friday', published='2026-10-29T18:00:00-04:00')
        self.assertFalse(player_availability([item], 'Jalen Brunson', DATE, NOW)['blocked'])
        item = self.news(player='Josh Hart', status='Out Friday', published='2026-10-29T18:00:00-04:00',
                         body='Jalen Brunson is a teammate.')
        self.assertEqual(player_availability([item], 'Jalen Brunson', DATE, NOW)['reports'], [])

    def test_other_game_day_teammate_and_negation_do_not_block(self):
        cases = [self.news(status='Ruled out for Saturday'), self.news(status='Not ruled out for Friday'),
                 self.news(status='Ruled out for next Friday'),
                 self.news(status='Could miss Friday'),
                 self.news(status='May not be available Friday'),
                 self.news(status='Will miss practice Friday'),
                 self.news(player='Josh Hart', body='Jalen Brunson is a teammate.'),
                 self.news(status='Ruled out', game_date='2026-11-01')]
        for item in cases:
            self.assertFalse(player_availability([item], 'Jalen Brunson', DATE, NOW)['blocked'], item)

    def test_latest_timed_clearance_supersedes_older_timed_out_report(self):
        old = self.news(published='2026-10-30T10:00:00-04:00')
        new = self.news(status='Cleared to play Friday', published='2026-10-30T18:00:00-04:00')
        value = player_availability([old, new], 'Jalen Brunson', DATE, NOW)
        self.assertFalse(value['blocked'])
        self.assertEqual(value['reports'][0]['state'], 'available_report')
        new['body'] = ''
        self.assertFalse(player_availability([old, new], 'Jalen Brunson', DATE, NOW)['blocked'])

    def test_negated_or_conditional_clearance_does_not_clear_prior_absence(self):
        old = self.news(published='2026-10-30T10:00:00-04:00')
        for status in ('Not available Friday', 'Not cleared to play Friday',
                       'Could be available Friday', 'May be cleared to play Friday',
                       'Expected to be available Friday', 'Not yet cleared for Friday'):
            new = self.news(status=status, published='2026-10-30T18:00:00-04:00')
            value = player_availability([old, new], 'Jalen Brunson', DATE, NOW)
            self.assertTrue(value['blocked'], status)
            self.assertFalse(any(row['state'] == 'available_report' for row in value['reports']), status)

    def test_questionable_caution_keeps_source_and_uncertainty(self):
        self.store.record_news(self.news(status='Questionable for Friday'))
        result = enrich_shortlist(self.store, report(candidate()), NOW)
        self.assertEqual(len(result['shortlist']), 1)
        text = format_shortlist(result)
        self.assertIn('Questionable', text)
        self.assertIn('test publisher', text)

    def test_outside_watchlist_scoped_existing_players_and_no_imagined_offer(self):
        self.observations('Josh Hart', n=5, reb=9)
        self.observations('Tyrese Maxey', 'PHI', n=5, pts=28)
        self.observations('Jaylen Brown', 'BOS', n=5, pts=30)
        self.observations('Jalen Brunson', n=5)
        with self.store.connect() as db:
            before = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ('player_games', 'boards', 'entries', 'news')}
        result = enrich_shortlist(self.store, report(candidate()), NOW)
        self.assertEqual({item['player'] for item in result['watchlist']}, {'Josh Hart', 'Tyrese Maxey'})
        for item in result['watchlist']:
            self.assertNotIn('line', item)
            self.assertNotIn('side', item)
            self.assertNotIn('probability', item)
        self.assertIn('check these lines', format_shortlist(result))
        with self.store.connect() as db:
            after = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in before}
        self.assertEqual(before, after)

    def test_watchlist_excludes_published_out_and_low_observation_players(self):
        self.observations('Josh Hart', n=5)
        self.observations('Tyrese Maxey', 'PHI', n=2)
        self.store.record_news(self.news(player='Josh Hart'))
        result = enrich_shortlist(self.store, report(candidate()), NOW)
        self.assertEqual(result['watchlist'], [])

    def test_formatter_contains_source_date_and_command_bounded(self):
        result = enrich_shortlist(self.store, report(candidate(), candidate('Tyrese Maxey', team='PHI'),
                                                     candidate('Josh Hart')), NOW)
        for item in result['shortlist']:
            item['reason'] += ' source context' * 100
            item['caution'] = 'Questionable per publisher. ' * 30
        text = format_shortlist(result)
        self.assertLessEqual(len(text), 2400)
        self.assertIn('More 19.5 points', text)
        self.assertIn('2026-10-29', text)
        self.assertIn('/analyze test-board', text)
        self.assertIn('No entry placed', text)

    def test_real_engine_produces_clean_shortlist_without_entry(self):
        self.observations('Jalen Brunson', n=15)
        self.observations('Josh Hart', n=5, reb=9)
        board = self.store.create_board([candidate()['leg'], candidate(side='less')['leg']])
        actual = analyze_board(self.store, board['id'], as_of=NOW)
        value = enrich_shortlist(self.store, actual, NOW)
        self.assertEqual(len(value['shortlist']), 1)
        self.assertEqual(value['shortlist'][0]['leg']['side'], 'more')
        self.assertEqual(value['board_id'], board['id'])
        self.assertEqual(len(self.store.list_entries()), 0)


if __name__ == '__main__':
    unittest.main()
