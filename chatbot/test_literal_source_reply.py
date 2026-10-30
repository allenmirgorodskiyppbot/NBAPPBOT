import unittest
import app


class LiteralSourceTests(unittest.TestCase):
    def card(self, row=12):
        return app.query_evidence_card('read_sheet', {'sheet': 'Jalen Brunson'},
            {'sheet': 'Jalen Brunson', 'rows': [{'_row': row, 'A': 'Recent 10 available', 'B': 10, 'C': 23.6}]}, 1)

    def test_exact_requested_row_is_quoted_without_generated_scope_claims(self):
        card = self.card()
        request = app.ChatRequest(message='Read row 12 of Jalen Brunson and tell me exactly what is stored there.')
        result = app.literal_source_reply(request, [card])
        self.assertEqual(result['answer'], card['text'])
        self.assertIn('23.6', result['answer'])
        self.assertIn('Recent 10 available', result['answer'])
        self.assertIn('no generated interpretation', result['grounding'])
        self.assertIn('verified source rows: 12', card['prompt_descriptor'])

    def test_wrong_missing_or_partial_rows_do_not_claim_to_fulfill_request(self):
        for text in ('Read row 15 of Jalen Brunson.', 'Read rows 12 and row 15 of Jalen Brunson.', 'Read rows 12-15 of Jalen Brunson.'):
            with self.subTest(text=text):
                self.assertIsNone(app.literal_source_reply(app.ChatRequest(message=text), [self.card()]))

    def test_interpretation_remains_model_generated_and_original_cell_can_be_shown(self):
        self.assertIsNone(app.literal_source_reply(app.ChatRequest(message='Read row 12 and explain why it differs.'), [self.card()]))
        result = app.literal_source_reply(app.ChatRequest(message='Show cell C12 of Jalen Brunson.'), [self.card()])
        self.assertIn('23.6', result['answer'])


if __name__ == '__main__':
    unittest.main()
