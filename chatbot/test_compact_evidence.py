"""Prompt compaction preserves source scope while bounding CPU input work."""
import copy
import json
import unittest

from grounded_reply import compact_evidence, evidence_cards, prompt_evidence, unverified_reply


def card(identity, kind, text, **metadata):
    return {'id': identity, 'kind': kind, 'text': text, 'source': 'fixture source',
            'published_at': '2026-10-29T12:00:00Z', **metadata}


def size(cards):
    return len(json.dumps(prompt_evidence(cards), ensure_ascii=False, separators=(',', ':')))


class CompactEvidenceTests(unittest.TestCase):
    def test_only_prompt_fields_are_copied_and_source_cards_are_unchanged(self):
        cards = [card('E7', 'player_all_opponent_averages',
                      'Player A: 20 PPG; all opponents; through 2026-10-28; source fixture.',
                      query='original query', data_through_date='2026-10-28')]
        before = copy.deepcopy(cards)
        selected = compact_evidence(cards, 'Explain scoring')
        self.assertIs(selected[0], cards[0])
        self.assertEqual(cards, before)
        self.assertEqual(prompt_evidence(selected)[0]['id'], 'E7')
        self.assertEqual(prompt_evidence(selected)[0]['kind'], cards[0]['kind'])
        self.assertIn('all-opponent', prompt_evidence(selected)[0]['text'])
        self.assertNotIn('20 PPG', prompt_evidence(selected)[0]['text'])
        self.assertEqual(selected[0]['query'], 'original query')

    def test_whole_source_text_and_dates_survive_a_tight_budget(self):
        essential = card('E9', 'player_opponent_sample',
                         'Player A versus BBB: no stored completed appearances; coverage incomplete.')
        optional = card('E1', 'publisher_report', 'Publisher headline ' + 'x' * 500)
        limit = size([essential])
        selected = compact_evidence([optional, essential], 'How might scoring change?', limit)
        self.assertEqual(selected, [essential])
        self.assertLessEqual(size(selected), limit)
        self.assertEqual(selected[0]['text'], essential['text'])

    def test_oversized_card_is_omitted_without_truncating_values(self):
        oversized = card('E1', 'query_result', 'x' * 1000)
        self.assertEqual(compact_evidence([oversized], 'Explain', 100), [])
        self.assertEqual(len(oversized['text']), 1000)
        self.assertEqual(compact_evidence([oversized], 'Explain', 0), [])

    def test_assignment_query_and_population_evidence_precede_optional_material(self):
        assignment = card('E9', 'tentative_assignment',
                          'A–BBB pairing, tentative; guarding direction unconfirmed.',
                          matches_resolved_matchup=True)
        query = card('E8', 'query_result', 'Stored result: 20. Filters need review.')
        opponent = card('E7', 'player_opponent_sample', 'A vs BBB: 18 PPG; one stored game.')
        current = card('E6', 'player_all_opponent_averages', 'A: 20 PPG; all opponents.')
        defense = card('E5', 'tracking_Team defense', 'BBB allows 108 points/game; all opponents.')
        news = card('E1', 'publisher_report', 'Some publisher report.')
        essential = [assignment, query, opponent, current, defense]
        selected = compact_evidence([news, *reversed(essential)], 'Explain the scoring matchup', size(essential))
        self.assertEqual(selected[0], assignment)
        self.assertEqual(selected[1], query)
        self.assertEqual({c['id'] for c in selected}, {c['id'] for c in essential})
        self.assertNotIn(news, selected)

    def test_comparison_keeps_both_players_and_both_population_labels(self):
        cards = [
            card('E1', 'player_opponent_sample', 'Player A versus BBB: stored sample only.'),
            card('E2', 'player_opponent_sample', 'Player C versus DDD: stored sample only.'),
            card('E3', 'player_all_opponent_averages', 'Player A season average: all opponents.'),
            card('E4', 'player_all_opponent_averages', 'Player C season average: all opponents.'),
            card('E5', 'tracking_Passing', 'A passing data ' + 'x' * 700),
        ]
        selected = compact_evidence(cards, 'Compare their scoring', size(cards[:4]))
        self.assertEqual(selected, cards[:4])

    def test_optional_tracking_tracks_question_dimensions_without_rim_substitution(self):
        cards = [card('E1', 'tracking_Passing', 'Passing totals.'),
                 card('E2', 'tracking_Rebounding', 'Rebounding totals.'),
                 card('E3', 'tracking_Possessions', 'Possession totals.'),
                 card('E4', 'tracking_Rim', 'Rim-only defended shots.'),
                 card('E5', 'tracking_Team defense', 'Overall team defense.')]
        scoring = compact_evidence(cards, 'What could affect his scoring?')
        self.assertEqual({c['id'] for c in scoring}, {'E3', 'E5'})
        passing = compact_evidence(cards, 'Explain his assists')
        self.assertEqual([c['id'] for c in passing], ['E1'])
        rebounding = compact_evidence(cards, 'Explain his rebounds')
        self.assertEqual([c['id'] for c in rebounding], ['E2'])
        rim = compact_evidence(cards, 'Explain rim defense')
        self.assertIn(cards[3], rim)
        self.assertIn('Rim-only', next(c for c in rim if c['id']=='E4')['text'])

    def test_singular_opponent_appearance_is_rendered_correctly(self):
        cards = evidence_cards({'opponent_player_statistics': [{
            'player': 'Player A', 'opponent': 'BBB', 'season': '2026-27', 'n': 1,
            'averages': {'pts': 21}, 'observed_counts': {'pts': 1},
            'first_date': '2026-10-20', 'data_through_date': '2026-10-20'}]}, 'points')
        self.assertIn('1 stored appearance,', cards[0]['text'])
        self.assertNotIn('1 stored appearances', cards[0]['text'])

    def test_failure_reason_is_shown_without_changing_saved_evidence(self):
        cards = [card('E1', 'player_opponent_sample', 'Player A versus BBB: sample only.')]
        original = unverified_reply(cards)
        timeout = unverified_reply(cards, failure_message='The AI request timed out.')
        self.assertTrue(timeout['answer'].startswith('The AI request timed out.'))
        self.assertNotIn('could not verify', timeout['answer'])
        self.assertIn(cards[0]['text'], timeout['answer'])
        self.assertEqual(timeout['sources'], original['sources'])
        self.assertTrue(original['answer'].startswith('I could not verify'))


if __name__ == '__main__':
    unittest.main()
