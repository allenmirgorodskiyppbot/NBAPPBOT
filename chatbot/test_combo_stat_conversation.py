"""Explicit combo markets must not inherit a different conversation offer."""
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import _market, resolve_conversation
from pick_discussion import normalize_stat


NOW = dt.datetime(2026, 11, 11, 17, tzinfo=dt.timezone.utc)
PLAYER = 'Karl-Anthony Towns'


class ComboStatConversationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('conversation._players', return_value=[PLAYER, 'Jalen Brunson'])
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, text, **kwargs):
        return resolve_conversation(None, None, text, now=NOW, **kwargs)

    def test_user_sequence_replaces_stale_pra_and_retains_exact_pr_offer(self):
        previous = self.resolve('kat more 38.5 PRA')
        first = self.resolve('kat 15.5 p+r', state=previous['state'])
        second = self.resolve('kat 15.5 p+r', state=first['state'])
        for turn in (first, second):
            self.assertTrue(turn['complete_prop'])
            self.assertEqual(turn['slots']['player'], PLAYER)
            self.assertEqual(turn['slots']['stat'], 'pr')
            self.assertEqual(turn['slots']['line'], 15.5)
            self.assertNotIn('side', turn['slots'])
        followup = self.resolve('kat is listed as questionable what should we do?', state=second['state'])
        self.assertEqual(followup['intent'], 'explanation')
        self.assertEqual(followup['slots']['stat'], 'pr')
        self.assertEqual(followup['slots']['line'], 15.5)
        self.assertEqual(followup['original_question'], 'kat is listed as questionable what should we do?')
        self.assertNotIn('availability_verified', followup['state'])

    def test_user_sequence_also_replays_history(self):
        history = [{'role': 'user', 'content': 'kat more 38.5 PRA'},
                   {'role': 'assistant', 'content': 'Pass for now.'}]
        turn = self.resolve('kat 15.5 p+r', history=history)
        self.assertTrue(turn['complete_prop'])
        self.assertEqual(turn['slots'], {'player': PLAYER, 'stat': 'pr', 'line': 15.5})

    def test_combo_aliases_preserve_components_and_exact_line(self):
        for label, expected in [('p+r', 'pr'), ('P + R', 'pr'), ('PR', 'pr'), ('p r', 'pr'),
                                ('p+a', 'pa'), ('P + A', 'pa'), ('PA', 'pa'),
                                ('r+a', 'ra'), ('R + A', 'ra'), ('RA', 'ra'),
                                ('p+r+a', 'pra'), ('P + R + A', 'pra'), ('p r a', 'pra')]:
            with self.subTest(label=label):
                turn = self.resolve('kat 15.5 ' + label)
                self.assertTrue(turn['complete_prop'])
                self.assertEqual(turn['slots']['stat'], expected)
                self.assertEqual(turn['slots']['line'], 15.5)
                self.assertEqual(normalize_stat(label), expected)

    def test_joined_mobile_combo_offer(self):
        for text in ['kat15.5p+r', 'KAT15.5P+R', 'kat 15.5p + r']:
            with self.subTest(text=text):
                turn = self.resolve(text)
                self.assertTrue(turn['complete_prop'])
                self.assertEqual(turn['slots']['player'], PLAYER)
                self.assertEqual(turn['slots']['stat'], 'pr')
                self.assertEqual(turn['slots']['line'], 15.5)

    def test_new_combo_market_requires_its_own_line_and_side(self):
        for label, stat in [('p+r', 'pr'), ('p+a', 'pa'), ('r+a', 'ra')]:
            with self.subTest(label=label):
                prior = self.resolve('kat more 38.5 PRA')
                turn = self.resolve('actually ' + label, state=prior['state'])
                self.assertEqual(turn['intent'], 'clarification')
                self.assertEqual(turn['slots']['stat'], stat)
                self.assertNotIn('line', turn['slots'])
                self.assertNotIn('side', turn['slots'])

    def test_malformed_or_conflicting_combos_clear_old_offer(self):
        for label in ['p+r+x', 'p+r+a+z', 'p+r+', '+p+r', 'p++r', 'p+p+r',
                      'p r x', 'p/r', 'pr or ra', 'PRA or PR', 'PR assists']:
            with self.subTest(label=label):
                prior = self.resolve('kat more 38.5 PRA')
                turn = self.resolve('actually ' + label, state=prior['state'])
                self.assertEqual(turn['intent'], 'clarification')
                self.assertIn('ambiguous_market', turn['errors'])
                self.assertFalse(turn['complete_prop'])
                for slot in ('stat', 'line', 'side'):
                    self.assertNotIn(slot, turn['slots'])

    def test_malformed_explicit_combo_with_new_line_cannot_be_complete(self):
        prior = self.resolve('kat 38.5 PRA')
        turn = self.resolve('kat 15.5 p+r+x', state=prior['state'])
        self.assertEqual(turn['intent'], 'clarification')
        self.assertNotIn('stat', turn['slots'])
        self.assertEqual(turn['slots']['line'], 15.5)
        self.assertFalse(turn['complete_prop'])

    def test_specific_existing_markets_and_full_word_combos_keep_their_meaning(self):
        for label, expected in [('three-point attempts', 'tpa'), ('threes', 'tpm'),
                                ('offensive rebounds', 'oreb'), ('points + rebounds', 'pr'),
                                ('rebounds and assists', 'ra'), ('points rebounds assists', 'pra'),
                                ('points + rebounds and assists', 'pra')]:
            with self.subTest(label=label):
                self.assertEqual(_market(label), (expected, None))

    def test_nonmarket_injury_description_keeps_the_discussed_offer(self):
        prior = self.resolve('kat 15.5 p+r')
        turn = self.resolve('What if his knee + ankle issues limit him?', state=prior['state'])
        self.assertEqual(turn['intent'], 'explanation')
        self.assertEqual(turn['slots']['stat'], 'pr')
        self.assertEqual(turn['slots']['line'], 15.5)
        self.assertIsNone(turn['clarification'])

    def test_numeric_workload_followups_do_not_replace_the_offer(self):
        for message in ['what if he only plays 20 minutes?', 'he has 20 minute restriction',
                        'what if he plays less than 20 minutes?', 'he is limited to 20 minutes']:
            with self.subTest(message=message):
                prior = self.resolve('kat more 15.5 p+r')
                turn = self.resolve(message, state=prior['state'])
                self.assertEqual(turn['intent'], 'explanation')
                self.assertEqual(turn['slots'], prior['slots'])
                self.assertEqual(turn['original_question'], message)
                self.assertIsNone(turn['clarification'])

    def test_explicit_offer_survives_a_numeric_workload_condition(self):
        prior = self.resolve('kat more 38.5 PRA')
        turn = self.resolve('KAT 15.5 PR if limited 20 minutes', state=prior['state'])
        self.assertTrue(turn['complete_prop'])
        self.assertEqual(turn['slots'], {'player': PLAYER, 'stat': 'pr', 'line': 15.5})
        self.assertIsNone(turn['clarification'])

    def test_explicit_minutes_offer_still_changes_the_market(self):
        for message in ['KAT over 25.5 minutes', 'KAT over 25.5 minutes if limited to 30 minutes']:
            with self.subTest(message=message):
                prior = self.resolve('kat more 15.5 p+r')
                turn = self.resolve(message, state=prior['state'])
                self.assertTrue(turn['complete_prop'])
                self.assertEqual(turn['slots'], {'player': PLAYER, 'stat': 'minutes',
                                                'line': 25.5, 'side': 'more'})


if __name__ == '__main__':
    unittest.main()
