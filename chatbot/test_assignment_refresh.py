"""Revalidation must never turn stale candidate names into confirmed defenders."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from assignment_refresh import (assignment_rows_for, assignment_refresh_status,
                                refresh_assignments, refresh_caution)
from feeds import canonical_game, canonical_player
from master import MasterStore

NOW = dt.datetime(2026, 10, 31, 16, 0, tzinfo=dt.timezone.utc)
SOURCE = {'_row': 8, 'assignment_id': 'A1', 'selected_player': 'Jalen Brunson',
          'opponent_team': 'PHI', 'primary': 'Tyrese Maxey', 'alternate': 'Joel Embiid',
          'assignment_asof': '2026-10-22T12:00:00', 'assignment_status': 'Tentative'}


class AssignmentRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MasterStore(Path(self.temp.name) / 'master.sqlite')
        with self.store.connect() as db:
            db.execute('CREATE TABLE workbook_evidence (kind TEXT,entity TEXT,payload_json TEXT)')
            db.execute('INSERT INTO workbook_evidence VALUES (?,?,?)',
                       ('assignments', SOURCE['selected_player'], json.dumps(SOURCE)))
        self.store.upsert_game({'id': 'upcoming', 'date': '2026-10-31', 'status': 'scheduled',
                               'home_team': 'NYK', 'away_team': 'PHI',
                               'start_time': '2026-10-31T23:30:00+00:00', 'source': 'fixture calendar'})
        self.observation('Jalen Brunson', 'NYK')
        self.observation('Tyrese Maxey', 'PHI')
        self.observation('Joel Embiid', 'PHI')

    def observation(self, player, team, day='2026-10-30', season='2026-27', phase='Regular Season'):
        game_id = canonical_game(day, team, 'BOS' if team != 'BOS' else 'TOR')
        self.store.upsert_game({'id': game_id, 'date': day, 'status': 'final', 'home_team': team,
                               'away_team': 'BOS' if team != 'BOS' else 'TOR', 'source': 'fixture games'})
        self.store.upsert_player_game({'game_id': game_id, 'player': player, 'player_id': canonical_player(player),
                                      'team': team, 'opponent': 'BOS' if team != 'BOS' else 'TOR',
                                      'date': day, 'season': season, 'season_type': phase,
                                      'appearance_state': 'Played', 'minutes': 30, 'pts': 20,
                                      'source': 'fixture observation'})

    def pair(self, offense='Jalen Brunson', defense='Tyrese Maxey', **changes):
        record = {'entity': offense + ' | ' + defense, 'category': 'Named pair',
                  'season': '2026-27', 'season_type': 'Regular Season', 'gp': 2,
                  'source': 'fixture directional measured source', 'published_at': '2026-10-31T14:00:00+00:00',
                  'data_through_date': '2026-10-30', 'fields': {'matchup_minutes': 15, 'partial_possessions': 25,
                                                           'fgm': 3, 'fga': 8}}
        record.update(changes)
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                       (record['season'], record['entity'], record['category'], json.dumps(record)))

    def rows(self, now=NOW):
        return assignment_rows_for(self.store, now=now)

    def test_recheck_keeps_original_names_dates_and_scope(self):
        status = refresh_assignments(self.store, NOW)
        row = self.rows()[0]
        for key, value in SOURCE.items():
            self.assertEqual(row[key], value)
        self.assertEqual(row['source_asof'], SOURCE['assignment_asof'])
        self.assertEqual(row['last_checked'], NOW.isoformat())
        self.assertEqual(row['refresh_state'], 'legacy_estimate_rechecked')
        self.assertFalse(row['verified_game_day_assignment'])
        self.assertEqual(status['new_verified_assignments'], 0)
        self.assertEqual(row['next_game_date'], '2026-10-31')
        self.assertEqual(row['candidate_checks'][1]['roster_state'], 'observed_with_expected_team')
        self.assertIn('original assignment date unchanged', refresh_caution(row))

    def test_out_report_flags_candidate_without_promoting_alternate(self):
        self.store.record_news({'id': 'out', 'title': 'Tyrese Maxey: Out Saturday', 'body': 'Knee injury.',
                                'published_at': '2026-10-31T14:00:00+00:00', 'source': 'fixture reporter',
                                'url': 'https://example.com/out'})
        refresh_assignments(self.store, NOW)
        row = self.rows()[0]
        self.assertEqual(row['primary'], 'Tyrese Maxey')
        self.assertEqual(row['alternate'], 'Joel Embiid')
        self.assertEqual(row['refresh_state'], 'needs_review')
        self.assertTrue(row['candidate_checks'][1]['availability']['blocked'])
        self.assertIn('publisher reports', refresh_caution(row))

    def test_old_or_wrong_game_news_does_not_invalidate_future_game(self):
        self.store.record_news({'id': 'old', 'title': 'Tyrese Maxey: Out Friday',
                                'published_at': '2026-10-30T14:00:00+00:00', 'source': 'fixture'})
        refresh_assignments(self.store, NOW)
        self.assertEqual(self.rows()[0]['refresh_state'], 'legacy_estimate_rechecked')

    def test_matching_weekday_report_cannot_apply_to_game_weeks_later(self):
        with self.store.connect() as db:
            db.execute("UPDATE games SET date='2026-11-28',start_time='2026-11-29T00:30:00+00:00' WHERE id='upcoming'")
        self.store.record_news({'id': 'out', 'title': 'Tyrese Maxey: Out Saturday',
                                'published_at': '2026-10-31T14:00:00+00:00', 'source': 'fixture'})
        refresh_assignments(self.store, NOW)
        self.assertEqual(self.rows()[0]['refresh_state'], 'legacy_estimate_rechecked')
        self.assertEqual(self.rows()[0]['candidate_checks'][1]['availability']['reports'], [])

    def test_roster_conflict_requires_review_not_a_replacement(self):
        self.observation('Tyrese Maxey', 'BOS', '2026-10-31')
        refresh_assignments(self.store, NOW)
        row = self.rows()[0]
        self.assertEqual(row['refresh_state'], 'needs_review')
        self.assertEqual(row['primary'], 'Tyrese Maxey')
        self.assertEqual(row['candidate_checks'][1]['roster_state'], 'team_conflict')
        self.assertIn('BOS', refresh_caution(row))

    def test_stale_old_season_and_future_rosters_are_not_current_evidence(self):
        with self.store.connect() as db:
            db.execute('DELETE FROM player_games')
        self.observation('Tyrese Maxey', 'BOS', '2026-11-01')
        self.observation('Jalen Brunson', 'NYK', '2025-10-30', season='2025-26')
        self.observation('Joel Embiid', 'PHI', '2026-10-20')
        refresh_assignments(self.store, NOW)
        self.assertTrue(all(r['roster_state'] == 'unknown' for r in self.rows()[0]['candidate_checks']))

    def test_directional_measured_history_does_not_change_tentative_assignment(self):
        self.pair('Jalen Brunson', 'Joel Embiid')
        self.pair('Tyrese Maxey', 'Jalen Brunson')
        refresh_assignments(self.store, NOW)
        row = self.rows()[0]
        self.assertEqual(row['primary'], 'Tyrese Maxey')
        self.assertEqual(row['assignment_direction'], 'unspecified')
        self.assertEqual(len(row['historical_measured_candidates']), 2)
        directions = {r['direction'] for r in row['historical_measured_candidates']}
        self.assertEqual(directions, {'opponent_defended_selected', 'selected_defended_opponent'})
        self.assertTrue(all(r['verified_game_day_assignment'] is False for r in row['historical_measured_candidates']))

    def test_wrong_season_phase_future_stale_and_unmatched_team_pairs_excluded(self):
        invalid = [{'season': '2025-26'}, {'season_type': 'Playoffs'},
                   {'published_at': '2026-11-01T14:00:00+00:00'},
                   {'data_through_date': '2026-11-01'}, {'data_through_date': '2026-10-20'},
                   {'fields': {'partial_possessions': 0, 'matchup_minutes': 0}}]
        for changes in invalid:
            with self.subTest(changes=changes), self.store.connect() as db:
                db.execute('DELETE FROM refreshed_tracking')
            self.pair(**changes)
            refresh_assignments(self.store, NOW)
            self.assertEqual(self.rows()[0]['historical_measured_candidates'], [])
        self.observation('Tyrese Maxey', 'BOS', '2026-10-31')
        self.pair()
        refresh_assignments(self.store, NOW)
        self.assertEqual(self.rows()[0]['historical_measured_candidates'], [])

    def test_unknown_tracking_coverage_is_retained_not_filled_from_games(self):
        self.pair(data_through_date=None)
        refresh_assignments(self.store, NOW)
        history = self.rows()[0]['historical_measured_candidates'][0]
        self.assertIsNone(history['data_through_date'])
        self.assertEqual(history['published_at'], '2026-10-31T14:00:00+00:00')

    def test_read_is_read_only_and_expired_recheck_is_marked(self):
        refresh_assignments(self.store, NOW)
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        row = self.rows(NOW + dt.timedelta(days=2))[0]
        self.assertEqual(row['refresh_state'], 'recheck_overdue')
        self.assertEqual(row['last_checked'], NOW.isoformat())
        self.assertIn('overdue', refresh_caution(row))
        with self.store.connect() as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))

    def test_future_recheck_does_not_leak_into_historical_read(self):
        refresh_assignments(self.store, NOW)
        row = self.rows(NOW - dt.timedelta(days=1))[0]
        self.assertNotIn('last_checked', row)

    def test_changed_workbook_row_does_not_reuse_old_cached_check(self):
        refresh_assignments(self.store, NOW)
        changed = {**SOURCE, 'primary': 'New Source Name', 'assignment_asof': '2026-10-30'}
        row = assignment_rows_for(self.store, [changed], NOW)[0]
        self.assertEqual(row['primary'], 'New Source Name')
        self.assertNotIn('last_checked', row)

    def test_historical_assignment_is_preserved_without_current_roster_checks(self):
        with self.store.connect() as db:
            db.execute('UPDATE workbook_evidence SET payload_json=?',
                       (json.dumps({**SOURCE, 'assignment_asof': '2025-10-22'}),))
        refresh_assignments(self.store, NOW)
        row = self.rows()[0]
        self.assertEqual(row['refresh_state'], 'historical_snapshot')
        self.assertEqual(row['candidate_checks'], [])
        self.assertIsNone(row['next_game_date'])

    def test_second_check_changes_check_time_not_evidence_revision(self):
        first = refresh_assignments(self.store, NOW)
        second = refresh_assignments(self.store, NOW + dt.timedelta(minutes=5))
        self.assertEqual(first['revision'], second['revision'])
        self.assertNotEqual(first['checked_at'], second['checked_at'])
        self.assertEqual(assignment_refresh_status(self.store), second)

    def test_direct_answers_ai_cards_and_cached_workbook_use_shared_recheck(self):
        from basketball_research import answer_assignment_question, research_context
        from grounded_reply import evidence_cards, prompt_evidence
        from unittest.mock import patch
        self.observation('Tyrese Maxey', 'BOS', '2026-10-31')
        refresh_assignments(self.store, NOW)
        workbook = SimpleNamespace(_research_assignments=[SOURCE], sheets={})
        question = 'Who is Jalen Brunson matched up against on Philadelphia?'
        resolved = {'original_question': question, 'question': question, 'players': ['Jalen Brunson'],
                    'teams': ['PHI'], 'inherited_player': False}
        # The source cache remains the original row; the latest master check is
        # resolved independently, without replacing its source date.
        with patch('assignment_refresh._now', return_value=NOW):
            direct = answer_assignment_question(workbook, self.store, question, resolved=resolved)
            context = research_context(workbook, self.store, resolved)
        self.assertIn('conflicts with the saved team pairing', direct['answer'])
        self.assertIn('2026-10-22', direct['answer'])
        self.assertEqual(workbook._research_assignments, [SOURCE])
        cards = evidence_cards(context, question)
        card = next(row for row in cards if row['kind'] == 'tentative_assignment')
        self.assertIn('conflicts with the saved team pairing', card['text'])
        self.assertIn('do not treat its names as current assignments',
                      next(row for row in prompt_evidence(cards) if row['kind'] == 'tentative_assignment')['text'])


if __name__ == '__main__':
    unittest.main()
