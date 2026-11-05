"""Pregame explanations keep requested scope and imperfect imports explicit."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from feeds import canonical_game, canonical_player
from master import MasterStore
from pick_discussion import build_pick_discussion
from research_discussion import build_research_discussion


NOW = dt.datetime(2026, 11, 2, 21, tzinfo=dt.timezone.utc)


def record_fixture_refresh(store):
    """Describe the final games actually seeded by a test's source inventory."""
    day = '2026-11-01'
    checked = '2026-11-02T20:45:00+00:00'
    with store.connect() as db:
        games = [dict(row) for row in db.execute(
            "SELECT id,home_team,away_team FROM games WHERE date=? AND status='final'", (day,))]
    teams = {'NYK': [], 'BKN': []}
    for game in games:
        for team in (game['home_team'], game['away_team']):
            teams.setdefault(team, []).append(game['id'])
    store.set_checkpoint('daily_nba:' + day, {
        'date': day, 'complete': True, 'games': len(games), 'expected_games': len(games),
        'final_game_ids': [game['id'] for game in games], 'team_final_game_ids': teams,
        'publisher_retrieved_at': checked, 'imported_at': checked,
        'last_checked_at': checked, 'last_refresh_status': 'success',
        'inventory_verified': True, 'inventory_verified_at': checked,
        'inventory_verification_source': 'synthetic independent scoreboard fixture',
        'source': 'synthetic final-game import fixture',
        'inventory_scope': 'Explicit synthetic published inventory for this test only'})
    store.set_checkpoint('nightly_nba_refresh', {
        'enabled': True, 'game_refresh_phase': 'updated', 'target_game_date': day,
        'source_data_through_date': day, 'last_attempt': checked, 'pending_dates': []})
    with store.connect() as db:
        upcoming = [dict(row) for row in db.execute(
            "SELECT id,date,home_team,away_team,status,start_time FROM games "
            "WHERE date='2026-11-02' AND status='scheduled'")]
    store.set_checkpoint('nba_schedule_today', {
        'date': '2026-11-02', 'status': 'success', 'error': None,
        'checked_at': NOW.isoformat(), 'source': 'synthetic independent current-scoreboard fixture',
        'games': upcoming})


class PregameDiscussionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.master = MasterStore(Path(directory.name) / 'pregame.sqlite')
        self.master.upsert_game({'id': 'tonight', 'date': '2026-11-02',
            'home_team': 'NYK', 'away_team': 'BKN', 'status': 'scheduled',
            'start_time': '2026-11-03T00:30:00+00:00', 'source': 'saved schedule'})
        self.add_player('Josh Hart', 'NYK', 8)
        record_fixture_refresh(self.master)

    def add_player(self, player, team, count):
        samples = [(index, (dt.date(2026, 11, 1) - dt.timedelta(days=index)).isoformat(), '2026-27')
                   for index in range(count)]
        if count >= 8:
            samples += [(index, (dt.date(2026, 4, 10) - dt.timedelta(days=index)).isoformat(), '2025-26')
                        for index in range(12)]
        for index, day, season in samples:
            game = canonical_game(day, team, 'BOS')
            self.master.upsert_game({'id': game, 'date': day, 'home_team': team,
                'away_team': 'BOS', 'status': 'final', 'source': 'saved game'})
            self.master.upsert_player_game({'game_id': game, 'player': player,
                'team': team, 'opponent': 'BOS', 'date': day, 'season': season,
                'season_type': 'Regular Season', 'appearance_state': 'Played',
                'pts': 15 + index % 3, 'reb': 7, 'ast': 4, 'minutes': 34,
                'source': 'saved observations'})

    def prop(self, **slots):
        return build_pick_discussion(self.master,
            {'player': 'Josh Hart', 'stat': 'points', 'line': 12.5, **slots}, NOW)

    def duplicate(self, *, changes=None):
        with self.master.connect() as db:
            row = dict(db.execute('SELECT * FROM player_games WHERE player=? ORDER BY date DESC LIMIT 1',
                                  ('Josh Hart',)).fetchone())
        row.update(player_id='provider:hart', **(changes or {}))
        self.master.upsert_player_game(row)
        return row

    def test_conflicting_date_aliases_are_not_silently_resolved(self):
        result = self.prop(date='2026-11-02', game_date='2026-11-05')
        self.assertFalse(result['available'])
        self.assertIn('conflict', result['fallback_answer'].lower())
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}],
            {'date': '2026-11-02', 'game_date': '2026-11-05'}, NOW)
        self.assertFalse(result['cards'])
        self.assertIn('conflict', result['fallback_answer'].lower())

    def test_incomplete_alias_does_not_mask_complete_same_game(self):
        self.duplicate()
        with self.master.connect() as db:
            db.execute('UPDATE player_games SET pts=NULL WHERE player_id=? AND date=?',
                       (canonical_player('Josh Hart'), '2026-11-01'))
        report = self.prop()
        self.assertEqual(report['sample']['n'], 8)
        self.assertEqual(report['sample']['values'][0], 15)

    def test_conflicting_alias_statistics_are_excluded_instead_of_cherry_picked(self):
        self.duplicate(changes={'pts': 90})
        report = self.prop()
        self.assertEqual(report['sample']['n'], 7)
        self.assertEqual(report['sample']['conflicting_games'], 1)
        self.assertIn('conflicting', ' '.join(report['cautions']).lower())
        self.assertNotIn(90, report['sample']['values'])

    def test_duplicate_alias_minutes_cannot_manufacture_known_workload(self):
        self.duplicate(changes={'minutes': 14})
        report = self.prop()
        self.assertEqual(report['sample']['n'], 8)
        self.assertEqual(report['workload']['observed_games'], 7)
        self.assertEqual(report['sample']['uncertain_workload_games'], 1)

    def test_reused_provider_id_cannot_change_player_or_team(self):
        before = self.prop()
        game = canonical_game('2026-11-01', 'ATL', 'MIA')
        self.master.upsert_game({'id': game, 'date': '2026-11-01', 'home_team': 'ATL',
            'away_team': 'MIA', 'status': 'final'})
        self.master.upsert_player_game({'game_id': game,
            'player_id': canonical_player('Josh Hart'), 'player': 'Unrelated Player',
            'team': 'ATL', 'opponent': 'MIA', 'date': '2026-11-01',
            'season': '2026-27', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 48, 'pts': 99,
            'source': 'incorrect player ID fixture', 'updated_at': '2099-01-01T00:00:00Z'})
        after = self.prop()
        self.assertEqual(after['player'], 'Josh Hart')
        self.assertEqual(after['team'], 'NYK')
        self.assertEqual(after['sample'], before['sample'])

    def test_future_coverage_cannot_enter_a_future_game_discussion(self):
        row = {'entity': 'BKN', 'season': '2026-27', 'category': 'Team defense',
            'season_type': 'Regular Season', 'gp': 8, 'fields': {'points_allowed': 800},
            'data_through_date': '2026-11-04', 'published_at': None,
            'source': 'future coverage fixture'}
        with self.master.connect() as db:
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',
                ('2026-27', 'BKN', 'Team defense', json.dumps(row)))
        result = self.prop(game_date='2026-11-05', opponent='BKN')
        self.assertFalse(result['opponent_defense']['available'])

    def test_partially_supported_shortlist_keeps_the_unavailable_player_visible(self):
        self.add_player('Julius Randle', 'BKN', 3)
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'},
             {'player': 'Julius Randle', 'stat': 'reb'}], {}, NOW)
        self.assertEqual({card['entity'] for card in result['cards']},
                         {'Josh Hart', 'Julius Randle'})
        held = next(card for card in result['cards'] if card['entity'] == 'Julius Randle')
        self.assertEqual(held['decision'], 'pass')
        self.assertIn('20', held['text'])
        self.assertIn('Julius Randle', result['fallback_answer'])
        from grounded_reply import natural_prompt_evidence
        prompt = natural_prompt_evidence(result['cards'])
        self.assertTrue(any('Julius Randle' in card['text'] and 'Hold' in card['text'] for card in prompt))

    def test_research_cannot_silently_answer_playoffs_with_regular_season_model(self):
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {'season_type': 'Playoffs'}, NOW)
        self.assertFalse(result['cards'])
        self.assertIn('regular-season', result['fallback_answer'].lower())

    def test_named_market_followup_does_not_substitute_another_market(self):
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}, {'player': 'Josh Hart', 'stat': 'reb'}],
            {'player': 'Josh Hart', 'stat': 'reb'}, NOW)
        self.assertEqual([item['stat'] for item in result['assessments']], ['reb'])

    def test_research_cannot_borrow_cross_player_projection_source_rows(self):
        game = canonical_game('2026-10-24', 'NYK', 'PHI')
        self.master.upsert_game({'id': game, 'date': '2026-10-24', 'home_team': 'NYK',
            'away_team': 'PHI', 'status': 'final'})
        self.master.upsert_player_game({'game_id': game,
            'player_id': canonical_player('Josh Hart'), 'player': 'Unrelated Player',
            'team': 'NYK', 'opponent': 'PHI', 'date': '2026-10-24',
            'season': '2026-27', 'season_type': 'Regular Season',
            'appearance_state': 'Played', 'minutes': 34, 'pts': 99,
            'source': 'incorrect player ID fixture'})
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {}, NOW)
        self.assertEqual(result['assessments'][0]['projection']['n'], 20)
        self.assertLess(result['assessments'][0]['projection']['reference_projection'], 18)
        self.assertNotIn(game, {row['game_id'] for row in result['assessments'][0]['projection']['source_games']})

    def test_research_rejects_mismatched_game_and_observation_dates(self):
        with self.master.connect() as db:
            db.execute("UPDATE games SET date='2026-11-06' WHERE date='2026-11-01'")
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {}, NOW)
        self.assertFalse(result['cards'])
        self.assertIn('20', result['fallback_answer'])
        self.assertIn('Only 19', result['held'][0]['reason'])

    def test_six_leg_research_discussion_preserves_each_selected_identity(self):
        names = ['Josh Hart', 'Jalen Brunson', 'Julius Randle',
                 'OG Anunoby', 'Karl-Anthony Towns', 'Nicolas Claxton']
        for index, name in enumerate(names[1:]):
            self.add_player(name, 'NYK' if index % 2 else 'BKN', 8)
        record_fixture_refresh(self.master)
        result = build_research_discussion(self.master,
            [{'player': name, 'stat': 'pts'} for name in names], {}, NOW)
        self.assertEqual([item['player'] for item in result['assessments']], names)
        self.assertEqual(len(result['cards']), 6)
        for name in names:
            self.assertIn(name, result['fallback_answer'])

    def test_unknown_daily_refresh_holds_current_pick_but_retains_history(self):
        with self.master.connect() as db:
            db.execute("DELETE FROM checkpoints WHERE name LIKE 'daily_nba:%' OR name='nightly_nba_refresh'")
        report = self.prop()
        self.assertTrue(report['available'])
        self.assertEqual(report['sample']['n'], 8)
        self.assertEqual(report['decision'], 'pass')
        self.assertFalse(report['data_readiness']['ready'])

    def test_failed_new_refresh_cannot_reuse_old_success_as_current(self):
        checkpoint = self.master.get_checkpoint('daily_nba:2026-11-01')
        checkpoint.update(last_checked_at=NOW.isoformat(), last_refresh_status='failed')
        self.master.set_checkpoint('daily_nba:2026-11-01', checkpoint)
        report = self.prop()
        self.assertEqual(report['decision'], 'pass')
        self.assertFalse(report['data_readiness']['ready'])
        research = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {}, NOW)
        self.assertEqual(research['assessments'][0]['status'], 'held')
        self.assertEqual(research['cards'][0]['decision'], 'pass')

    def test_missing_latest_team_game_player_row_holds_two_day_old_sample(self):
        with self.master.connect() as db:
            db.execute("DELETE FROM player_games WHERE player='Josh Hart' AND date='2026-11-01'")
        report = self.prop()
        self.assertEqual(report['sample']['last_date'], '2026-10-31')
        self.assertEqual(report['sample_age_days'], 2)
        self.assertEqual(report['decision'], 'pass')
        self.assertFalse(report['data_readiness']['ready'])

    def test_latest_played_game_must_cover_the_requested_statistic(self):
        self.add_player('Josh Hart', 'NYK', 12)
        with self.master.connect() as db:
            db.execute("UPDATE player_games SET pts=NULL WHERE player='Josh Hart' AND date='2026-11-01'")
        report = self.prop()
        self.assertEqual(report['sample']['n'], 11)
        self.assertEqual(report['decision'], 'pass')
        self.assertFalse(report['data_readiness']['ready'])
        research = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {}, NOW)
        self.assertEqual(research['assessments'][0]['status'], 'held')
        self.assertFalse(research['assessments'][0]['data_readiness']['ready'])

    def test_verified_dnp_is_coverage_without_becoming_a_zero_stat_game(self):
        with self.master.connect() as db:
            db.execute("UPDATE player_games SET appearance_state='DNP',minutes=0,pts=0 "
                       "WHERE player='Josh Hart' AND date='2026-11-01'")
        report = self.prop()
        self.assertTrue(report['data_readiness']['ready'])
        self.assertEqual(report['sample']['n'], 7)
        self.assertNotIn(0, report['sample']['values'])

    def test_fresh_history_without_saved_upcoming_event_is_not_a_current_pick(self):
        with self.master.connect() as db:
            db.execute("DELETE FROM games WHERE id='tonight'")
        report = self.prop()
        self.assertTrue(report['available'])
        self.assertEqual(report['decision'], 'pass')
        self.assertIn('upcoming game', report['reason'])

    def test_refresh_failure_does_not_hide_current_out_report(self):
        checkpoint = self.master.get_checkpoint('daily_nba:2026-11-01')
        checkpoint['last_refresh_status'] = 'failed'
        self.master.set_checkpoint('daily_nba:2026-11-01', checkpoint)
        self.master.record_news({'id': 'out', 'title': 'Josh Hart: Ruled out for Monday',
            'published_at': '2026-11-02T19:00:00+00:00', 'source': 'synthetic availability fixture'})
        result = build_research_discussion(self.master,
            [{'player': 'Josh Hart', 'stat': 'pts'}], {}, NOW)
        self.assertFalse(result['assessments'][0]['data_readiness']['ready'])
        self.assertEqual(result['cards'][0]['decision'], 'pass')
        from grounded_reply import natural_prompt_evidence
        self.assertIn('out', natural_prompt_evidence(result['cards'])[0]['text'])


if __name__ == '__main__':
    unittest.main()
