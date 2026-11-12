"""Assistant questions preserve reference identity, never offered observations."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from conversation import resolve_conversation
from feeds import canonical_game, canonical_player
from master import MasterStore

NOW = dt.datetime(2026, 11, 11, 19, 30, tzinfo=dt.timezone.utc)
KAT = 'Karl-Anthony Towns'


def user(content):
    return {'role': 'user', 'content': content}


def assistant(content):
    return {'role': 'assistant', 'content': content}


class SemanticClarificationContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.master = MasterStore(Path(temp.name) / 'clarification.sqlite')
        game = canonical_game('2026-11-10', 'NYK', 'BOS')
        self.master.upsert_game({'id': game, 'date': '2026-11-10', 'home_team': 'NYK',
            'away_team': 'BOS', 'status': 'final', 'source': 'Synthetic clarification fixture'})
        for player in (KAT, 'Jalen Brunson', 'Jalen Smith'):
            self.master.upsert_player_game({'game_id': game, 'date': '2026-11-10',
                'player_id': canonical_player(player), 'player': player, 'team': 'NYK',
                'opponent': 'BOS', 'season': '2026-27', 'season_type': 'Regular Season',
                'appearance_state': 'Played', 'pts': 20, 'reb': 8, 'ast': 6, 'minutes': 32,
                'source': 'Synthetic clarification observations'})

    def resolve(self, message, question, *, prior='kat more 36.5 PRA against Brooklyn', change=None):
        history = [user(prior)]
        if change:
            history.append(user(change))
        history.append(assistant(question))
        return resolve_conversation(None, self.master, message, history, now=NOW)

    def test_new_market_question_then_bare_number_uses_ra_and_discards_old_pra_quote(self):
        result = self.resolve('22.5', 'What is Karl-Anthony Towns’s rebounds+assists line?',
                              change='Use rebounds plus assists instead for the same player.')
        self.assertEqual(result['slots']['player'], KAT)
        self.assertEqual(result['slots']['stat'], 'ra')
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertNotIn('side', result['slots'])
        self.assertTrue(result['complete_prop'])

    def test_assistant_numbers_never_become_new_quote_thresholds(self):
        result = self.resolve('Why?', 'What is Towns’s rebounds+assists line, perhaps 18.5?')
        self.assertEqual(result['slots']['stat'], 'ra')
        self.assertNotIn('line', result['slots'])
        self.assertNotIn('side', result['slots'])

    def test_only_user_value_supplies_the_new_threshold(self):
        result = self.resolve('22.5', 'What is Towns’s RA line? Is it 18.5?')
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertEqual(result['slots']['stat'], 'ra')

    def test_new_player_question_clears_old_market_line_side_and_opponent(self):
        result = self.resolve('25.5', 'What is Jalen Brunson’s points line?')
        self.assertEqual(result['slots']['player'], 'Jalen Brunson')
        self.assertEqual(result['slots']['stat'], 'pts')
        self.assertEqual(result['slots']['line'], 25.5)
        self.assertNotIn('side', result['slots'])
        self.assertNotIn('opponent', result['slots'])

    def test_multiple_players_in_one_question_do_not_select_an_old_player(self):
        result = self.resolve('22.5', 'What is Towns or Jalen Brunson’s rebounds line?')
        self.assertNotIn('player', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_two_targeted_subject_questions_are_ambiguous(self):
        result = self.resolve('22.5', 'What is Towns’s RA line? What is Jalen Brunson’s points line?')
        self.assertNotIn('player', result['slots'])
        self.assertNotIn('stat', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_question_requesting_player_identity_does_not_inherit_old_player(self):
        for question in ('Whose RA line is that?', 'Which player’s rebounds+assists line do you mean?'):
            with self.subTest(question=question):
                result = self.resolve('22.5', question)
                self.assertNotIn('player', result['slots'])
                self.assertFalse(result['complete_prop'])

    def test_attempts_or_points_are_distinct_markets_not_an_implicit_precedence(self):
        result = self.resolve('22.5', 'Is the offered line for field-goal attempts or points?')
        self.assertNotIn('stat', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_alternative_point_and_rebound_markets_do_not_select_one(self):
        result = self.resolve('22.5', 'Which points or rebounds line do you mean?')
        self.assertNotIn('stat', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_conditional_minutes_question_does_not_create_a_minutes_offer(self):
        result = self.resolve('Why?', 'What if his minutes are limited to 25?')
        self.assertEqual(result['slots']['stat'], 'pra')
        self.assertEqual(result['slots']['line'], 36.5)

    def test_explicit_minutes_market_question_can_be_a_reference_without_copying_number(self):
        result = self.resolve('30', 'What is his minutes line, perhaps 25?')
        self.assertEqual(result['slots']['stat'], 'minutes')
        self.assertEqual(result['slots']['line'], 30)
        self.assertNotIn('side', result['slots'])

    def test_targeted_market_question_can_include_scenario_without_copying_minutes(self):
        for question in ('What is his RA line if he only plays 25 minutes?',
                         'If he only plays 25 minutes, what is Towns’s rebounds+assists line?'):
            with self.subTest(question=question):
                result = self.resolve('22.5', question)
                self.assertEqual(result['slots']['stat'], 'ra')
                self.assertEqual(result['slots']['line'], 22.5)
                self.assertNotIn('side', result['slots'])

    def test_named_conditional_player_resolves_main_question_pronoun(self):
        for question in ('If Jalen Brunson only plays 25 minutes, what is his RA line?',
                         'What is his RA line if Jalen Brunson only plays 25 minutes?'):
            with self.subTest(question=question):
                result = self.resolve('22.5', question)
                self.assertEqual(result['slots']['player'], 'Jalen Brunson')
                self.assertEqual(result['slots']['stat'], 'ra')
                self.assertEqual(result['slots']['line'], 22.5)
                self.assertNotIn('side', result['slots'])

    def test_main_question_player_takes_precedence_over_conditional_other_player(self):
        result = self.resolve('25.5', 'If Towns sits out, what is Jalen Brunson’s points line?')
        self.assertEqual(result['slots']['player'], 'Jalen Brunson')
        self.assertEqual(result['slots']['stat'], 'pts')
        self.assertEqual(result['slots']['line'], 25.5)

    def test_availability_condition_never_becomes_an_asserted_status_or_old_quote(self):
        result = self.resolve('22.5', 'Assuming he is active, what is Towns’s rebounds line?')
        self.assertEqual(result['slots']['stat'], 'reb')
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertNotIn('side', result['slots'])
        self.assertNotIn('availability', result['slots'])

    def test_unknown_explicit_player_cannot_fall_back_to_the_old_player(self):
        for question in ('What is LeBron James’s rebounds line?',
                         'What is the rebounds line for LeBron James?'):
            with self.subTest(question=question):
                result = self.resolve('22.5', question)
                self.assertNotIn('player', result['slots'])
                self.assertFalse(result['complete_prop'])

    def test_ordinary_assistant_statistics_do_not_update_reference_market_or_quote(self):
        result = self.resolve('Why?', 'Towns averaged 18.5 rebounds in an older sample. Source dates are unknown.')
        self.assertEqual(result['slots']['stat'], 'pra')
        self.assertEqual(result['slots']['line'], 36.5)

    def test_source_prose_before_targeted_question_does_not_supply_player_or_numbers(self):
        result = self.resolve('22.5', 'Jalen Brunson averaged 18.5 points. What is his RA line?')
        self.assertEqual(result['slots']['player'], KAT)
        self.assertEqual(result['slots']['stat'], 'ra')
        self.assertEqual(result['slots']['line'], 22.5)

    def test_informational_question_does_not_switch_active_offer_market(self):
        for question in ('Is a rebounds line a better measure of his opportunities?',
                         'What is the historical average points line for Towns?'):
            with self.subTest(question=question):
                result = self.resolve('Why?', question)
                self.assertEqual(result['slots']['stat'], 'pra')
                self.assertEqual(result['slots']['line'], 36.5)

    def test_generic_input_question_does_not_resurrect_old_market_after_category_change(self):
        result = self.resolve('22.5', 'What line would you like to use?', change='Try a different market.')
        self.assertNotIn('stat', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_clarifying_missing_stat_preserves_user_threshold(self):
        result = self.resolve('rebounds', 'Which statistic is Towns’s line for: points or rebounds?', prior='kat 22.5')
        self.assertEqual(result['slots']['stat'], 'reb')
        self.assertEqual(result['slots']['line'], 22.5)

    def test_assistant_dates_do_not_replace_user_game_date(self):
        result = self.resolve('22.5', 'What is Towns’s RA line for November 14?', prior='kat 36.5 PRA November 11')
        self.assertEqual(result['slots']['game_date'], '2026-11-11')

    def test_old_single_proposal_cannot_restore_previous_market_after_clarification(self):
        history = [user('Suggest research.'), assistant('Karl-Anthony Towns — More 36.5 PRA.'),
                   assistant('What is Karl-Anthony Towns’s RA line?')]
        result = resolve_conversation(None, self.master, '22.5', history, now=NOW)
        self.assertEqual(result['slots']['stat'], 'ra')
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertNotIn('side', result['slots'])

    def test_reference_resolution_is_read_only(self):
        with self.master.connect() as db:
            before = '\n'.join(db.iterdump())
        self.resolve('22.5', 'What is Towns’s RA line?')
        with self.master.connect() as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))


if __name__ == '__main__':
    unittest.main()
