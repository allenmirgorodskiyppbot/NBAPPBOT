"""Coverage limits and short follow-ups keep the relevant source evidence."""
import unittest

from grounded_reply import compact_evidence, evidence_cards, prompt_evidence, required_evidence, render_reply
from test_grounded_reply import context


class CoverageEvidenceTests(unittest.TestCase):
    def test_unavailable_official_report_is_explicit_not_a_health_assertion(self):
        data = context('Jalen Brunson', 'PHI')
        data['availability_coverage'] = {
            'complete_official_report_in_context': False,
            'feeds': [{'name': 'availability_coverage', 'last_attempt': '2026-10-30T15:00:00Z'}]}
        question = 'Is Jalen Brunson confirmed healthy and available tonight?'
        cards = compact_evidence(evidence_cards(data, question), question)
        required = required_evidence(question, cards)
        coverage = next(c for c in cards if c['kind'] == 'availability_coverage')
        self.assertIn(coverage['id'], required)
        self.assertIn('cannot be confirmed', coverage['text'])
        self.assertIn('2026-10-30T15:00:00Z', coverage['text'])
        self.assertIn('local workbook tools cannot fetch', prompt_evidence([coverage])[0]['text'])
        result = render_reply({'evidence_ids': [], 'analysis': 'I cannot confirm his availability from a current official report.',
                               'needs_more_data': False}, cards, required)
        self.assertIn('Missing reports do not mean', result['answer'])
        self.assertEqual(result['sources'][0]['population_kind'], 'availability_coverage')

    def test_absent_check_time_is_not_fabricated(self):
        data = {'resolved_players': ['Jalen Brunson'], 'availability_coverage': {
            'complete_official_report_in_context': False, 'feeds': []}}
        card = evidence_cards(data, 'Is he healthy?')[0]
        self.assertIn('No stored coverage-check timestamp', card['text'])
        self.assertIsNone(card['data_through_date'])

    def test_do_not_invent_missing_official_coverage_for_unrelated_questions(self):
        self.assertFalse(any(c['kind'] == 'availability_coverage'
                             for c in evidence_cards(context(), 'Explain scoring')))

    def test_short_opponent_followup_keeps_baseline_and_sample(self):
        for question in ('What about OG against Charlotte?', 'OG versus Charlotte?', 'OG vs Charlotte?'):
            with self.subTest(question=question):
                cards = evidence_cards(context(), question)
                required = required_evidence(question, cards)
                selected = [c['kind'] for c in cards if c['id'] in required]
                self.assertEqual(set(selected), {'tentative_assignment', 'player_all_opponent_averages', 'player_opponent_sample'})


if __name__ == '__main__':
    unittest.main()
