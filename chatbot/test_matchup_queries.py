"""Matchup direction, metric clarification and measured-source boundaries."""
from pathlib import Path
import tempfile
import unittest

import openpyxl

from workbook import WorkbookStore
from matchup_queries import answer_matchup_question, _records


class MatchupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Pair_Tracking'
        for _ in range(6):
            sheet.append([None])
        sheet.append(['pair_id', 'offensive_player', 'defender', 'phase', 'fgm', 'fga'])
        sheet.append(['aggregate', 'Tyrese Maxey', 'Jalen Brunson', 'All (RS + playoffs)', 90, 100])
        sheet.append([None])
        sheet.append(['pair_observation_id', 'game_id', 'date', 'season', 'season_type',
                      'offense_player', 'defense_player', 'fgm', 'fga', 'source_id'])
        sheet.append(['m1', 'g1', '2026-10-20', '2026-27', 'Regular Season',
                      'Tyrese Maxey', 'Jalen Brunson', 4, 5, 'SOURCE1'])
        sheet.append(['m2', 'g1', '2026-10-20', '2026-27', 'Regular Season',
                      'Jalen Brunson', 'Tyrese Maxey', 1, 2, 'SOURCE2'])
        sheet.append(['m3', 'g2', '2026-04-20', '2025-26', 'Playoffs',
                      'Tyrese Maxey', 'Jalen Brunson', 1, 4, 'SOURCE3'])
        sheet.append(['m4', 'g3', '2026-10-21', '2026-27', 'Regular Season',
                      'Tyrese Maxey', 'Jalen Brunson', None, 1, 'SOURCE4'])
        path = root / 'fixture.xlsx'
        book.save(path)
        book.close()
        self.store = WorkbookStore(str(path), str(root / 'workbook.sqlite'))

    def tearDown(self):
        self.directory.cleanup()

    def test_current_dated_rows_are_not_combined_with_overlapping_history(self):
        answer = answer_matchup_question(self.store,
            'What FG% does Maxey shoot when Brunson guards him?')['answer']
        self.assertIn('80.0%', answer)
        self.assertIn('4 makes / 5 attempts', answer)
        self.assertIn('2026-10-20', answer)
        self.assertIn('row(s) 11', answer)
        self.assertIn('1 row(s) with missing', answer)
        self.assertNotIn('90.0%', answer)

    def test_roles_reverse_when_the_offensive_question_reverses(self):
        answer = answer_matchup_question(self.store, 'What FG% does Brunson shoot against Maxey?')['answer']
        self.assertIn('50.0%', answer)
        self.assertIn('Jalen Brunson shot', answer)
        self.assertIn('Tyrese Maxey recorded as defender', answer)

    def test_pronoun_uses_recent_user_topic_and_metric_is_clarified(self):
        history = [{'role': 'user', 'content': 'Tell me about Jalen Brunson'}]
        question = 'What is his defensive percentage against Tyrese Maxey?'
        clarification = answer_matchup_question(self.store, question, history)['answer']
        self.assertTrue(clarification.startswith('Do you mean field-goal percentage allowed'))
        self.assertIn('Jalen Brunson defends Tyrese Maxey', clarification)
        history += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': clarification}]
        answer = answer_matchup_question(self.store, 'Yes', history)['answer']
        self.assertIn('80.0%', answer)

    def test_explicit_defensive_allowance_uses_the_defender_not_the_scorer(self):
        for question in ['What FG% does Brunson allow against Maxey?',
                         'What is Brunson defensive field-goal percentage against Maxey?']:
            with self.subTest(question=question):
                answer = answer_matchup_question(self.store, question)['answer']
                self.assertIn('80.0%', answer)
                self.assertIn('Tyrese Maxey shot', answer)

    def test_missing_defender_can_be_supplied_as_a_normal_name(self):
        question = 'What is his defensive percentage against Tyrese Maxey?'
        prompt = answer_matchup_question(self.store, question)['answer']
        self.assertEqual(prompt, 'Which player is defending Tyrese Maxey?')
        answer = answer_matchup_question(self.store, 'Brunson',
                    [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': prompt}])['answer']
        self.assertIn('Jalen Brunson defends Tyrese Maxey', answer)

    def test_missing_pair_or_season_does_not_substitute_coappearance(self):
        answer = answer_matchup_question(self.store, 'Brunson FG% allowed against Maxey in 2024-25')['answer']
        self.assertIn('No dated measured', answer)
        self.assertIn('cannot substitute', answer)
        self.assertNotIn('80.0%', answer)

    def test_phases_are_kept_separate(self):
        answer = answer_matchup_question(self.store, 'Brunson FG% allowed against Maxey in playoffs')['answer']
        self.assertIn('25.0%', answer)
        self.assertIn('2025-26 Playoffs', answer)

    def test_duplicate_or_conflicting_rows_cannot_inflate_counts(self):
        rows = _records(self.store)
        rows.append(dict(rows[0]))
        self.assertIn('4 makes / 5 attempts', answer_matchup_question(
            self.store, 'Brunson FG% allowed against Maxey')['answer'])
        bad = dict(rows[0]); bad['fgm'] = 3; rows.append(bad)
        self.assertIn('Conflicting', answer_matchup_question(
            self.store, 'Brunson FG% allowed against Maxey')['answer'])

    def test_unsupported_windows_and_metrics_are_not_silently_ignored(self):
        for question in ['Brunson career FG% allowed against Maxey',
                         'Brunson FG% allowed against Maxey in the last five games',
                         'Brunson effective field goal percentage against Maxey',
                         'Brunson 3P% allowed against Maxey']:
            with self.subTest(question=question):
                self.assertIsNone(answer_matchup_question(self.store, question))


if __name__ == '__main__':
    unittest.main()
