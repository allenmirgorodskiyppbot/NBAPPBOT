"""Questionable-status questions receive focused evidence and a conditional plan."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from availability_discussion import can_handle_availability, build_availability_discussion
from master import MasterStore
from structured_availability import normalize_official_pdf, build_availability_document, import_availability_document

NOW = dt.datetime(2026, 11, 11, 19, tzinfo=dt.timezone.utc)
DAY = '2026-11-11'
PLAYER = 'Karl-Anthony Towns'
PDF = 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-11_01_30PM.pdf'


class AvailabilityFollowupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MasterStore(Path(folder.name) / 'master.sqlite')
        self.slots = {'player': PLAYER, 'stat': 'pr', 'line': 15.5, 'game_date': DAY}
        self.turn = {'slots': self.slots, 'entities': {'players': [PLAYER]},
                     'clarification': None, 'errors': []}
        clock = patch('availability_discussion._now', side_effect=lambda value=None: value or NOW)
        clock.start(); self.addCleanup(clock.stop)

    def build(self, text, **kwargs):
        return build_availability_discussion(self.store, self.slots, text, now=NOW, **kwargs)

    def official(self, status, *, stamp='2026-11-11T13:30:00-05:00'):
        rows = normalize_official_pdf([{'game_date': DAY, 'home_team': 'NYK', 'away_team': 'BOS',
            'team': 'NYK', 'player': PLAYER, 'status': status, 'reason': 'Synthetic controlled status'}],
            source_url=PDF, report_at=stamp, text_sha256='b' * 64, now=NOW)
        import_availability_document(self.store, build_availability_document(rows, now=NOW), now=NOW)

    def test_exact_status_followup_keeps_pr_context_without_schedule_or_stat_dump(self):
        question = 'kat is listed as questionable what should we do?'
        self.assertTrue(can_handle_availability(question, self.turn))
        with patch('pregame_freshness.assess_pregame_freshness', side_effect=AssertionError('No schedule check')):
            result = self.build(question)
        self.assertFalse(result['ai_used'])
        self.assertEqual(result['discussion']['stat'], 'pr')
        self.assertEqual(result['discussion']['line'], 15.5)
        self.assertIn('You report that he is questionable', result['answer'])
        self.assertIn('hold', result['answer'].lower())
        self.assertIn('15.5 points + rebounds', result['answer'])
        self.assertNotIn('15 minutes', result['answer'])
        self.assertNotIn('20-game', result['answer'])
        self.assertNotIn('PTS+REB+AST', result['answer'])
        self.assertFalse(result['availability_discussion']['availability_verified'])

    def test_current_official_out_is_not_erased_by_user_questionable_report(self):
        self.official('Out')
        result = self.build('kat is questionable now what should we do?')
        self.assertEqual(result['availability_discussion']['official_status'], 'Out')
        self.assertIn('lists Out', result['answer'])
        self.assertIn('Leave him out', result['answer'])
        self.assertIn('You report', result['answer'])
        self.assertIn(PDF, result['sources'])

    def test_official_available_does_not_establish_normal_minutes_or_erase_user_conflict(self):
        self.official('Available')
        result = self.build('he is questionable what should we do?')
        self.assertIn('lists Available', result['answer'])
        self.assertIn('hold', result['answer'].lower())
        self.assertFalse(result['availability_discussion']['minutes_verified'])
        self.assertFalse(result['availability_discussion']['offer_verified'])

    def test_negated_user_status_is_not_repeated_as_positive_questionable(self):
        result = self.build('he is not questionable anymore, what should we do?')
        report = result['availability_discussion']['user_report']
        self.assertTrue(report['negated'])
        self.assertIn('not questionable', result['answer'])
        self.assertNotIn('You report that he is questionable;', result['answer'])
        self.assertIsNone(result['availability_discussion']['official_status'])

    def test_conditional_status_is_not_promoted_to_a_user_assertion(self):
        result = self.build('what if he is questionable?')
        self.assertEqual(result['availability_discussion']['user_report']['mode'], 'conditional')
        self.assertNotIn('You report that he is questionable', result['answer'])

    def test_short_status_question_and_local_question_clause_are_not_assertions(self):
        result = self.build('KAT questionable?')
        self.assertEqual(result['availability_discussion']['user_report']['mode'], 'question')
        self.assertNotIn('You report that he is questionable', result['answer'])
        mixed = self.build('He is not out, is he questionable?')
        self.assertNotIn('You report that he is questionable', mixed['answer'])

    def test_latest_explicit_status_and_exact_negation_are_retained(self):
        result = self.build('He was questionable, but now available')
        self.assertEqual(result['availability_discussion']['user_report']['status'], 'available')
        self.assertIn('You report that he is available', result['answer'])
        cleared = self.build('He is no longer questionable')
        self.assertIn('no longer questionable', cleared['answer'])

    def test_numeric_minutes_condition_does_not_create_a_minutes_forecast(self):
        question = 'what if he only plays 20 minutes?'
        self.assertTrue(can_handle_availability(question, self.turn))
        result = self.build(question)
        self.assertEqual(result['discussion']['line'], 15.5)
        self.assertEqual(result['discussion']['stat'], 'pr')
        self.assertIn('No numerical minutes forecast', result['answer'])
        self.assertFalse(result['availability_discussion']['minutes_verified'])

    def test_why_wait_uses_only_typed_same_player_same_day_availability_history(self):
        question = 'kat is listed as questionable what should we do?'
        answer = self.build(question)['answer']
        history = [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]
        self.assertTrue(can_handle_availability('why wait?', self.turn, history=history))
        again = self.build('why wait?', history=history)
        self.assertTrue(again['availability_discussion']['user_report']['from_history'])
        self.assertIn('You previously reported', again['answer'])
        self.assertEqual(again['availability_discussion']['focus'], 'why_wait')
        self.assertIn('usual full-game history cannot settle', again['answer'])
        self.assertNotIn('I’d hold this pick while', again['answer'])
        self.assertNotIn('15 minutes', again['answer'])
        self.assertFalse(can_handle_availability('why wait?', self.turn, history=[
            {'role': 'assistant', 'content': 'Pass: current availability is not fully verified.'}]))
        other = {**self.turn, 'slots': {**self.slots, 'player': 'Jalen Brunson'}}
        self.assertFalse(can_handle_availability('why wait?', other, history=history))
        tomorrow = {**self.turn, 'slots': {**self.slots, 'game_date': '2026-11-12'}}
        self.assertFalse(can_handle_availability('why wait?', tomorrow, history=history))

    def test_if_he_plays_followup_answers_participation_versus_minutes(self):
        question = 'kat is listed as questionable what should we do?'
        first = self.build(question)
        history = [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': first['answer']}]
        self.assertTrue(can_handle_availability('what if he plays?', self.turn, history=history))
        result = self.build('what if he plays?', history=history)
        self.assertEqual(result['availability_discussion']['focus'], 'if_plays')
        self.assertIn('Playing would settle participation', result['answer'])
        self.assertIn('normal minutes', result['answer'])
        self.assertNotIn('I’d hold this pick while', result['answer'])
        self.assertFalse(result['availability_discussion']['minutes_verified'])
        self.official('Out')
        still_out = self.build('what if he plays?', history=history)
        self.assertIn('Leave him out', still_out['answer'])
        self.assertNotIn('Playing would settle participation', still_out['answer'])

    def test_stale_official_report_is_not_current_clearance(self):
        self.official('Available')
        later = NOW + dt.timedelta(hours=7)
        result = build_availability_discussion(self.store, self.slots, 'is he available?', now=later)
        self.assertIsNone(result['availability_discussion']['official_status'])
        self.assertFalse(result['availability_discussion']['availability_verified'])

    def test_missing_player_ambiguity_workflow_or_offer_availability_are_not_captured(self):
        for text in ('what is the refresh status?', 'are his PrizePicks lines available?',
                     'what is the offer availability?', '10 out of 20 games', 'give me picks'):
            with self.subTest(text=text):
                self.assertFalse(can_handle_availability(text, self.turn))
        self.assertFalse(can_handle_availability('is he questionable?', {'slots': {}}))
        self.assertFalse(can_handle_availability('is he questionable?', {**self.turn, 'clarification': 'Which player?'}))

    def test_definition_and_fresh_player_status_do_not_require_a_prop_line(self):
        slots = {'player': PLAYER, 'game_date': DAY}
        question = 'what does questionable mean for kat?'
        self.assertTrue(can_handle_availability(question, {**self.turn, 'slots': slots}))
        result = build_availability_discussion(self.store, slots, question, now=NOW)
        self.assertIn('playing status is undecided', result['answer'])
        self.assertIsNone(result['discussion']['stat'])
        self.assertIsNone(result['discussion']['line'])

    def test_focused_discussion_is_read_only_and_does_not_mark_readiness_current(self):
        self.store.set_checkpoint('nba_schedule_today', {'status': 'failed', 'error': '403'})
        before = self.store.get_checkpoint('nba_schedule_today')
        self.build('kat is questionable what should we do?')
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
