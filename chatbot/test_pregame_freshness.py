"""A fresh fetch and proven game inventory are separate pregame requirements."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from feeds import canonical_game, canonical_player
from master import MasterStore
from season_calendar import seed_calendar


NOW = dt.datetime(2026, 11, 4, 17, tzinfo=dt.timezone.utc)
TODAY = '2026-11-04'
YESTERDAY = '2026-11-03'
PLAYER = 'Jalen Brunson'


class PregameFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'freshness.sqlite')
        self.schedule()

    def schedule(self, *, now=NOW, game_day=TODAY, **changes):
        next_utc_day = dt.date.fromisoformat(game_day) + dt.timedelta(days=1)
        game = {'id': canonical_game(game_day, 'NYK', 'CHI'), 'date': game_day,
                'home_team': 'NYK', 'away_team': 'CHI', 'status': 'scheduled',
                'start_time': next_utc_day.isoformat() + 'T00:30:00+00:00', 'source': 'independent schedule fixture'}
        self.store.upsert_game(game)
        value = {'date': game_day, 'status': 'success', 'error': None, 'checked_at': now.isoformat(),
                 'source': 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
                 'games': [game]}
        value.update(changes)
        self.store.set_checkpoint('nba_schedule_today', value)
        return value

    def game(self, day=YESTERDAY, *, team='NYK', opponent='PHI', status='final',
             player=PLAYER, appearance='Played'):
        identity = canonical_game(day, team, opponent)
        self.store.upsert_game({'id': identity, 'date': day, 'home_team': team,
            'away_team': opponent, 'status': status, 'source': 'isolated final-game fixture'})
        if player is not None:
            self.store.upsert_player_game({'game_id': identity, 'player': player,
                'player_id': canonical_player(player), 'team': team, 'opponent': opponent,
                'date': day, 'season': '2026-27', 'season_type': 'Regular Season',
                'appearance_state': appearance,
                'pts': 24 if appearance == 'Played' else None,
                'minutes': 32 if appearance == 'Played' else 0 if appearance == 'DNP' else None,
                'source': 'isolated final observation'})
        return identity

    def checked(self, day=YESTERDAY, *, ids=(), teams=None, verified=True, **changes):
        value = {'date': day, 'last_checked_at': NOW.isoformat(),
                 'last_refresh_status': 'success', 'last_refresh_error': None,
                 'source': 'nba', 'publisher_retrieved_at': NOW.isoformat(),
                 'complete': True, 'final_game_ids': list(ids),
                 'team_final_game_ids': teams if teams is not None else {'NYK': list(ids)},
                 'inventory_verified': verified,
                 'inventory_verified_at': NOW.isoformat() if verified else None,
                 'inventory_verification_source': 'https://example.invalid/full-game-inventory' if verified else None,
                 'inventory_game_ids': list(ids), 'inventory_off_day_verified': verified and not ids}
        value.update(changes)
        self.store.set_checkpoint('daily_nba:' + day, value)
        return value

    def assess(self, *, date=TODAY, team='NYK', player=PLAYER, now=NOW, stat=None):
        from pregame_freshness import assess_pregame_freshness
        return assess_pregame_freshness(self.store, date, team, now, player=player, stat=stat)

    def current(self, *, appearance='Played', player=PLAYER):
        identity = self.game(appearance=appearance, player=player)
        self.checked(ids=[identity])
        return identity

    def rest_day(self):
        seed_calendar(self.store)
        identity = self.game('2026-11-02', opponent='BKN')
        self.checked('2026-11-02', ids=[identity], verified=False)
        self.checked(ids=[], verified=False)
        return identity

    def test_verified_current_inventory_can_work_without_complete_team_calendar(self):
        self.current()
        self.assertFalse(self.store.get_checkpoint('season_calendar:2026-27'))
        self.assertTrue(self.assess()['ready'])
        self.assertTrue(self.assess(player=None)['ready'])

    def test_current_schedule_provenance_is_required_even_with_current_game_statistics(self):
        self.current()
        self.store.set_checkpoint('nba_schedule_today', {})
        result = self.assess()
        self.assertFalse(result['ready'])
        self.assertTrue(any('schedule within 15 minutes' in reason for reason in result['reasons']))
        # A recent collection timestamp on a saved season publication is not an
        # independently checked current game.
        seed_calendar(self.store)
        calendar = self.store.get_checkpoint('season_calendar:2026-27')
        calendar['source_checked_at'] = NOW.isoformat()
        self.store.set_checkpoint('season_calendar:2026-27', calendar)
        self.assertFalse(self.assess()['ready'])

    def test_failed_partial_stale_or_future_schedule_checks_hold(self):
        self.current()
        for changes in ({'status': 'failed', 'error': 'ReadTimeout'}, {'status': 'partial'},
                        {'status': 'success', 'error': 'ReadTimeout'},
                        {'checked_at': (NOW - dt.timedelta(minutes=15, seconds=1)).isoformat()},
                        {'checked_at': (NOW + dt.timedelta(minutes=5, seconds=1)).isoformat()},
                        {'checked_at': NOW.replace(tzinfo=None).isoformat()},
                        {'date': YESTERDAY}):
            with self.subTest(changes=changes):
                self.schedule(**changes)
                self.assertFalse(self.assess()['ready'])
        self.schedule(checked_at=(NOW - dt.timedelta(minutes=15)).isoformat())
        self.assertTrue(self.assess()['ready'])

    def test_live_postponed_final_or_elapsed_current_schedule_is_not_pregame(self):
        self.current()
        for status in ('live', 'postponed', 'cancelled', 'past_schedule_only', 'final'):
            with self.subTest(status=status):
                value = self.schedule()
                value['games'][0]['status'] = status
                self.store.set_checkpoint('nba_schedule_today', value)
                self.assertFalse(self.assess()['ready'])
        value = self.schedule()
        value['games'][0]['start_time'] = NOW.isoformat()
        self.store.set_checkpoint('nba_schedule_today', value)
        self.assertFalse(self.assess()['ready'])

    def test_current_schedule_identity_date_and_clock_must_match_saved_event(self):
        self.current()
        for change in ({'id': 'another-event'}, {'away_team': 'BOS'}, {'date': YESTERDAY},
                       {'start_time': '2026-11-06T00:30:00+00:00'},
                       {'start_time': '2026-11-05T01:00:00+00:00'}):
            with self.subTest(change=change):
                value = self.schedule()
                value['games'][0].update(change)
                self.store.set_checkpoint('nba_schedule_today', value)
                self.assertFalse(self.assess()['ready'])
        value = self.schedule()
        value['games'].append(dict(value['games'][0]))
        self.store.set_checkpoint('nba_schedule_today', value)
        self.assertFalse(self.assess()['ready'])

    def test_game_rows_without_a_recent_source_check_are_history_only(self):
        self.game()
        self.assertFalse(self.assess()['ready'])

    def test_current_failed_or_incomplete_refresh_does_not_inherit_old_success(self):
        identity = self.game()
        for changes in ({'last_refresh_status': 'failed'}, {'complete': False},
                        {'last_refresh_status': 'success', 'last_refresh_error': 'ReadTimeout'}):
            with self.subTest(changes=changes):
                self.checked(ids=[identity], **changes)
                self.assertFalse(self.assess()['ready'])

    def test_fetch_timestamp_must_be_on_the_current_eastern_day(self):
        identity = self.game()
        # UTC November 4 is still November 3 in New York at this instant.
        self.checked(ids=[identity], last_checked_at='2026-11-04T01:00:00+00:00')
        self.assertFalse(self.assess()['ready'])
        self.checked(ids=[identity], last_checked_at='2026-11-04T16:00:00+00:00')
        self.assertTrue(self.assess()['ready'])

    def test_unknown_or_future_fetch_time_cannot_establish_current_freshness(self):
        identity = self.game()
        for stamp in (None, '2026-11-04T12:00:00', '2026-11-05T17:00:00+00:00', 'invalid'):
            with self.subTest(stamp=stamp):
                self.checked(ids=[identity], last_checked_at=stamp)
                self.assertFalse(self.assess()['ready'])

    def test_latest_known_team_final_must_be_in_refreshed_inventory(self):
        old = self.game('2026-11-02', opponent='BKN')
        self.game()
        self.checked(ids=[old])
        self.assertFalse(self.assess()['ready'])

    def test_player_missing_from_latest_team_game_is_not_fresh(self):
        self.game('2026-11-02', opponent='BKN')
        newest = self.game(player=None)
        self.checked(ids=[newest])
        self.assertFalse(self.assess()['ready'])
        self.assertTrue(self.assess(player=None)['ready'])

    def test_unknown_player_appearance_is_not_verified_game_coverage(self):
        self.current(appearance='Unknown')
        self.assertFalse(self.assess()['ready'])

    def test_latest_row_with_wrong_event_opponent_cannot_claim_player_freshness(self):
        self.current()
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET opponent=?', ('BOS',))
        result = self.assess(stat='pts')
        self.assertFalse(result['ready'])
        self.assertIsNone(result['player_data_through_date'])
        self.assertTrue(self.assess(player=None)['ready'])

    def test_dnp_is_coverage_without_becoming_a_statistical_appearance(self):
        self.current(appearance='DNP')
        self.assertTrue(self.assess()['ready'])
        for stat in ('pts', 'fga', 'pra'):
            self.assertTrue(self.assess(stat=stat)['ready'])
        with self.store.connect() as db:
            row = db.execute('SELECT appearance_state,pts,minutes FROM player_games').fetchone()
            self.assertEqual(tuple(row), ('DNP', None, 0))

    def test_missing_latest_stat_cannot_use_older_games_to_claim_current_market_coverage(self):
        self.current()
        points = self.assess(stat='pts')
        attempts = self.assess(stat='fga')
        self.assertTrue(points['ready'])
        self.assertFalse(attempts['ready'])
        self.assertIn('pts', points['current_player_stat_coverage'])
        self.assertNotIn('fga', attempts['current_player_stat_coverage'])

    def test_conflicting_latest_stat_aliases_hold_that_market_without_hiding_agreeing_stats(self):
        self.current()
        with self.store.connect() as db:
            db.execute('UPDATE player_games SET fga=18')
            alias = dict(db.execute('SELECT * FROM player_games').fetchone())
        alias.update(player_id='provider:brunson-alias', fga=24)
        self.store.upsert_player_game(alias)
        points = self.assess(stat='pts')
        attempts = self.assess(stat='fga')
        self.assertTrue(points['ready'])
        self.assertFalse(attempts['ready'])
        self.assertIn('pts', points['current_player_stat_coverage'])
        self.assertNotIn('fga', attempts['current_player_stat_coverage'])

    def test_unsourced_latest_dnp_cannot_establish_fresh_participation(self):
        self.current(appearance='DNP')
        for source in (None, ''):
            with self.subTest(source=source):
                with self.store.connect() as db:
                    db.execute('UPDATE player_games SET source=?', (source,))
                self.assertFalse(self.assess()['ready'])
                self.assertFalse(self.assess(stat='fga')['ready'])

    def test_a_different_players_row_does_not_cover_the_requested_player(self):
        self.current(player='Josh Hart')
        self.assertFalse(self.assess()['ready'])

    def test_unverified_empty_inventory_without_complete_calendar_is_not_an_off_day(self):
        identity = self.game('2026-11-02', opponent='BKN')
        self.checked('2026-11-02', ids=[identity], verified=False)
        self.checked(ids=[], verified=False)
        self.assertFalse(self.assess()['ready'])

    def test_complete_published_calendar_permits_a_proven_rest_day(self):
        self.rest_day()
        self.assertTrue(self.assess()['ready'])

    def test_rest_day_still_requires_current_recheck_of_latest_team_game_publication(self):
        identity = self.rest_day()
        self.checked('2026-11-02', ids=[identity], verified=False,
                     last_checked_at='2026-11-03T17:00:00+00:00')
        self.assertFalse(self.assess()['ready'])

    def test_unknown_calendar_coverage_and_unassigned_windows_cannot_prove_rest(self):
        self.rest_day()
        original = self.store.get_checkpoint('season_calendar:2026-27')
        for changes in ({'complete_for_published_games': False},
                        {'unassigned_coverage_unknown': True},
                        {'unassigned_windows': [{'start': '2026-11-02', 'end': '2026-11-03'}]}):
            with self.subTest(changes=changes):
                self.store.set_checkpoint('season_calendar:2026-27', {**original, **changes})
                self.assertFalse(self.assess()['ready'])

    def test_calendar_for_another_team_cannot_prove_requested_team_rest_day(self):
        seed_calendar(self.store)
        identity = self.game('2026-11-02', team='PHI', opponent='BOS')
        self.checked('2026-11-02', ids=[identity], teams={'PHI': [identity]}, verified=False)
        self.checked(ids=[], teams={}, verified=False)
        self.assertFalse(self.assess(team='PHI')['ready'])

    def test_known_past_game_still_unfinished_blocks_even_with_verified_inventory(self):
        for status in ('scheduled', 'live'):
            with self.subTest(status=status):
                identity = self.game('2026-11-02', opponent='BKN')
                self.game(status=status, player=None)
                self.checked('2026-11-02', ids=[identity])
                self.checked(ids=[])
                self.assertFalse(self.assess()['ready'])

    def test_future_and_past_game_requests_do_not_claim_today_pregame_readiness(self):
        self.current()
        for day in ('2026-11-03', '2026-11-05'):
            with self.subTest(day=day):
                self.assertFalse(self.assess(date=day)['ready'])

    def test_assessment_is_read_only_for_game_data_and_financial_records(self):
        self.current()
        with self.store.connect() as db:
            before = {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                      for name in ('games', 'player_games', 'checkpoints', 'entries', 'grade_events')}
        self.assertTrue(self.assess()['ready'])
        with self.store.connect() as db:
            after = {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                     for name in before}
        self.assertEqual(after, before)

    def interval(self, *, include_middle=False, missing_local_game=False):
        now = dt.datetime(2026, 10, 30, 17, tzinfo=dt.timezone.utc)
        self.schedule(now=now, game_day='2026-10-30')
        latest = self.game('2026-10-27')
        days = [('2026-10-27', [latest]), ('2026-10-29', [])]
        if include_middle:
            missing = canonical_game('2026-10-28', 'NYK', 'CHI')
            days.append(('2026-10-28', [missing] if missing_local_game else []))
        for day, ids in days:
            self.checked(day, ids=ids, last_checked_at=now.isoformat(),
                         publisher_retrieved_at=now.isoformat(),
                         inventory_verified_at=now.isoformat())
        return {'date': '2026-10-30', 'now': now}

    def test_verified_yesterday_does_not_cover_an_unchecked_intervening_game_day(self):
        target = self.interval()
        self.assertFalse(self.assess(**target)['ready'])

    def test_independently_verified_entire_interval_can_prove_intervening_rest_days(self):
        target = self.interval(include_middle=True)
        self.assertTrue(self.assess(**target)['ready'])

    def test_game_listed_in_intervening_inventory_requires_a_local_final_import(self):
        target = self.interval(include_middle=True, missing_local_game=True)
        self.assertFalse(self.assess(**target)['ready'])

    def test_inventory_verification_needs_a_current_reliable_instant_for_uncovered_team(self):
        identity = self.game()
        for stamp in ('2026-11-03T17:00:00+00:00', '2026-11-05T17:00:00+00:00',
                      '2026-11-04T17:20:00+00:00', '2026-11-04T12:00:00', None):
            with self.subTest(stamp=stamp):
                self.checked(ids=[identity], inventory_verified_at=stamp)
                self.assertFalse(self.assess()['ready'])

    def test_malformed_pending_date_metadata_holds_without_crashing(self):
        self.current()
        for malformed in (None, YESTERDAY, {'date': YESTERDAY}, 42):
            for field in ('pending_dates', 'older_pending_dates'):
                with self.subTest(field=field, value=malformed):
                    self.store.set_checkpoint('nightly_nba_refresh', {field: malformed})
                    self.assertFalse(self.assess()['ready'])


if __name__ == '__main__':
    unittest.main()
