import unittest
from grounded_reply import ReplyValidationError, has_unscoped_absence, render_reply


class AbsenceScopeTests(unittest.TestCase):
    cards = [{'id': 'E1', 'kind': 'player_opponent_sample',
              'text': 'Player versus Team: no matching completed appearances in the stored sample; coverage is incomplete.'}]

    def test_real_matchup_absence_cannot_be_inferred_from_incomplete_records(self):
        for text in ('No confirmed matchups exist between Player and Team.',
                     'No confirmed game data exists between them.',
                     'No matching games occurred between them.'):
            with self.subTest(text=text):
                with self.assertRaises(ReplyValidationError) as caught:
                    render_reply({'evidence_ids': ['E1'], 'analysis': text, 'needs_more_data': False}, self.cards)
                self.assertEqual(caught.exception.reason, 'unsupported_scope')

    def test_stored_absence_and_unknown_defender_or_official_report_are_distinct(self):
        for text in ('No matching games exist in the stored sample.',
                     'The available records contain no matching completed appearances.',
                     'No confirmed defensive assignment is available.',
                     'No current official injury report is available in these records.',
                     'His scoring could change with playing time and shot opportunities.'):
            with self.subTest(text=text):
                self.assertFalse(has_unscoped_absence(text, self.cards))

    def test_only_selected_original_source_rows_allow_numeric_references(self):
        cards = [{'id': 'E1', 'kind': 'workbook_excerpt', 'text': 'Original row data', 'reference_rows': [12]},
                 {'id': 'E2', 'kind': 'workbook_excerpt', 'text': 'Other row data', 'reference_rows': [15]}]
        document = {'evidence_ids': ['E1'], 'analysis': 'Row 12 is shown in the original source excerpt.', 'needs_more_data': False}
        self.assertIn('Row 12', render_reply(document, cards)['answer'])
        for analysis in ('Row 15 is the requested source.', 'Row 12 shows an average of 30 points.'):
            with self.subTest(analysis=analysis), self.assertRaises(ReplyValidationError):
                render_reply({**document, 'analysis': analysis}, cards)
        sql_card = {**cards[0], 'kind': 'query_result'}
        with self.assertRaises(ReplyValidationError):
            render_reply(document, [sql_card])


if __name__ == '__main__':
    unittest.main()
