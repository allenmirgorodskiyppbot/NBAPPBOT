"""Dialogue regressions from real user messages, independent of model wording."""
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import resolve_conversation

NOW = dt.datetime(2026, 11, 4, 19, tzinfo=dt.timezone.utc)
NAMES = ['Josh Hart', 'Jalen Brunson', 'Jalen Smith', 'Julius Randle',
         'Karl-Anthony Towns', 'OG Anunoby', 'Tyrese Maxey', 'Jaylen Brown']


def user(text):
    return {'role': 'user', 'content': text}


def assistant(text):
    return {'role': 'assistant', 'content': text}


FOUR_OFFERS = ('My shortlist — NYK vs BKN · Nov 04\n'
               '1. Jalen Brunson — More 19.5 points\n'
               '2. Julius Randle — More 1.5 threes\n'
               '3. Julius Randle — More 4.5 rebounds\n'
               '4. Josh Hart — Less 22.5 PRA')
FOUR_TARGETS = ('My projections — NYK vs BKN · Nov 05, 7:30 PM Eastern\n'
                '1. Jalen Brunson · points: 25.5\nMore if line ≤19.5; Less if line ≥31.5.\n'
                '2. Julius Randle · rebounds: 8\nMore if line ≤5.5; Less if line ≥10.5.\n'
                '3. Josh Hart · PRA: 20\nMore if line ≤15.5; Less if line ≥24.5.\n'
                '4. OG Anunoby · FG Attempted: 12\nMore if line ≤9.5; Less if line ≥14.5.')


class ConversationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('conversation._players', return_value=NAMES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, text, history=None, state=None, now=NOW):
        return resolve_conversation(None, None, text, history, state, now)

    def test_missing_market_then_correction_then_choose_then_why(self):
        first = self.resolve('The Josh hart line is at 22.5 u choose up or down')
        self.assertEqual(first['intent'], 'clarification')
        self.assertEqual(first['slots']['player'], 'Josh Hart')
        self.assertEqual(first['slots']['line'], 22.5)
        self.assertNotIn('stat', first['slots'])
        self.assertIn('PRA', first['clarification'])
        second = self.resolve('22.5 I mean for points rebounds assists line', state=first['state'])
        self.assertEqual(second['slots']['player'], 'Josh Hart')
        self.assertEqual(second['slots']['stat'], 'pra')
        self.assertEqual(second['slots']['line'], 22.5)
        self.assertTrue(second['complete_prop'])
        third = self.resolve('Up or down choose', state=second['state'])
        self.assertEqual(third['intent'], 'prop')
        self.assertTrue(third['complete_prop'])
        last = self.resolve('Why?', state=third['state'])
        self.assertEqual(last['intent'], 'explanation')
        self.assertEqual(last['slots']['stat'], 'pra')
        self.assertIn('Josh Hart', last['canonical_question'])

    def test_same_correction_works_by_replaying_history(self):
        history = [user('Josh Hart line 22.5 choose up or down'),
                   assistant('Is that points, rebounds, assists, or PRA?'),
                   user('22.5 I mean points rebounds assists line'),
                   assistant('Tentative Less for Josh Hart PRA.'), user('Up or down choose')]
        result = self.resolve('Why do you think that is good?', history)
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(result['slots']['stat'], 'pra')
        self.assertEqual(result['slots']['line'], 22.5)

    def test_typo_player_and_made_threes(self):
        result = self.resolve('Does Julius Randal more 1.5 threes a good line')
        self.assertTrue(result['complete_prop'])
        self.assertEqual(result['slots'], {'player': 'Julius Randle', 'stat': 'tpm', 'line': 1.5, 'side': 'more'})

    def test_mobile_spacing_preserves_original_but_resolves_line(self):
        text = 'Does Julius Randal more1.5threes a good line'
        result = self.resolve(text)
        self.assertTrue(result['complete_prop'])
        self.assertEqual(result['slots']['line'], 1.5)
        self.assertEqual(result['slots']['side'], 'more')
        self.assertEqual(result['original_question'], text)

    def test_both_team_rule_is_not_a_team_stat_question(self):
        for text in ['The picks must me from both teams',
                     'The picks we choose we must have at least one from each team. Can’t be all Knicks',
                     'I need four picks, at least one from each team',
                     'The card cannot be all Knicks picks']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'lineup_constraint')
                self.assertTrue(result['constraints']['both_teams'])
                self.assertIsNone(result['clarification'])

    def test_constraint_retains_prop_without_replacing_opponent(self):
        previous = self.resolve('Josh Hart more 22.5 PRA against Brooklyn tonight')
        changed = self.resolve('The picks must be from both teams', state=previous['state'])
        self.assertEqual(changed['slots']['player'], 'Josh Hart')
        self.assertEqual(changed['slots']['opponent'], 'BKN')
        self.assertEqual(changed['slots']['game_date'], '2026-11-04')
        self.assertTrue(self.resolve('Why?', state=changed['state'])['constraints']['both_teams'])

    def test_both_team_statistics_are_not_lineup_constraints(self):
        for text in ['Show both teams stats', 'How good is each team defense?',
                     'Compare both teams against the Knicks']:
            with self.subTest(text=text):
                self.assertNotEqual(self.resolve(text)['intent'], 'lineup_constraint')

    def test_new_subject_does_not_inherit_old_market_or_line(self):
        previous = self.resolve('Josh Hart more 22.5 PRA against Brooklyn')
        result = self.resolve('What about Julius Randle?', state=previous['state'])
        self.assertEqual(result['slots']['player'], 'Julius Randle')
        self.assertNotIn('line', result['slots'])
        self.assertNotIn('stat', result['slots'])
        self.assertNotIn('opponent', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_tell_me_more_does_not_change_less_to_more(self):
        previous = self.resolve('Josh Hart less 22.5 PRA')
        result = self.resolve('Tell me more about that', state=previous['state'])
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(result['slots']['side'], 'less')

    def test_market_switch_requires_a_new_line(self):
        previous = self.resolve('Brunson more 19.5 points')
        result = self.resolve('What about his field goal attempts?', state=previous['state'])
        self.assertEqual(result['slots']['stat'], 'fga')
        self.assertNotIn('line', result['slots'])
        self.assertEqual(result['intent'], 'clarification')
        next_result = self.resolve('18', state=result['state'])
        self.assertEqual(next_result['slots']['line'], 18)
        self.assertTrue(next_result['complete_prop'])

    def test_attempts_without_qualifier_does_not_guess_a_market(self):
        previous = self.resolve('Brunson more 19.5 points')
        result = self.resolve('What about his attempts?', state=previous['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertNotIn('stat', result['slots'])
        self.assertNotIn('line', result['slots'])
        self.assertIn('three-point attempts', result['clarification'])

    def test_correcting_number_retains_market_and_player(self):
        history = [user('Josh Hart 22.5 PRA more or less')]
        result = self.resolve('Actually 21.5', history)
        self.assertEqual(result['slots']['line'], 21.5)
        self.assertEqual(result['slots']['stat'], 'pra')
        self.assertEqual(result['slots']['player'], 'Josh Hart')

    def test_three_point_attempts_not_made_threes(self):
        for text in ['Brunson more 6.5 three-point attempts', 'Brunson 6.5 3PTA good?',
                     'Brunson more 6.5 three pointers attempts']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['slots']['stat'], 'tpa')
                self.assertEqual(result['slots']['line'], 6.5)

    def test_integer_line_zero_and_combined_markets(self):
        for text, stat, line in [('OG more 10 FG attempted', 'fga', 10),
                                 ('Randle more 0.5 turnovers', 'tov', .5),
                                 ('Towns over 12.5 rebounds and assists', 'ra', 12.5),
                                 ('Brunson line 24.5 points + assists', 'pa', 24.5)]:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['slots']['stat'], stat)
                self.assertEqual(result['slots']['line'], line)

    def test_dates_and_pick_counts_do_not_become_lines(self):
        result = self.resolve('Give me four picks November 4')
        self.assertEqual(result['slots']['pick_count'], 4)
        self.assertEqual(result['slots']['game_date'], '2026-11-04')
        self.assertNotIn('line', result['slots'])
        result = self.resolve('Brunson more 19.5 points on 11/4')
        self.assertEqual(result['slots']['line'], 19.5)
        self.assertEqual(result['slots']['game_date'], '2026-11-04')

    def test_ambiguous_name_never_borrows_previous_player(self):
        previous = self.resolve('Josh Hart more 22.5 PRA')
        result = self.resolve('Jalen 19.5 points up or down', state=previous['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertNotIn('player', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_multiple_players_do_not_get_one_shared_line(self):
        result = self.resolve('Brunson and Randle more 19.5 points?')
        self.assertEqual(result['intent'], 'clarification')
        self.assertFalse(result['complete_prop'])

    def test_unknown_player_needs_clarification(self):
        result = self.resolve('Unknown Person more 19.5 points good?')
        self.assertEqual(result['intent'], 'clarification')
        self.assertFalse(result['complete_prop'])

    def test_multiple_lines_never_silently_choose_one(self):
        result = self.resolve('Brunson 19.5 or 24.5 points more?')
        self.assertEqual(result['intent'], 'clarification')
        self.assertIn('multiple_lines', result['errors'])
        self.assertFalse(result['complete_prop'])

    def test_invalid_and_conflicting_dates_need_clarification(self):
        for text in ['Brunson more 19.5 points November 31',
                     'Brunson more 19.5 points today tomorrow']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'clarification')
                self.assertFalse(result['complete_prop'])

    def test_partial_and_special_markets_never_become_full_game(self):
        for text in ['Brunson 12.5 first half points more?', 'Brunson 1Q 6.5 points over?',
                     'Randle 3.5 quarters with 3 points more?', 'OG 1.5 dunks more?']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'clarification')
                self.assertFalse(result['complete_prop'])

    def test_social_turns_do_not_request_old_player_stats(self):
        prior = self.resolve('Josh Hart 22.5 PRA up or down')
        for text in ['thank you', 'Thanks!', 'hello', 'Good night', 'ok']:
            with self.subTest(text=text):
                result = self.resolve(text, state=prior['state'])
                self.assertEqual(result['intent'], 'chat')
                self.assertEqual(result['slots'], {})
                self.assertEqual(result['canonical_question'], text)
                self.assertEqual(result['state']['slots']['player'], 'Josh Hart')

    def test_preference_not_generic_stat_lookup(self):
        prior = self.resolve('Brunson more 19.5 points')
        result = self.resolve('I prefer field goal attempts', state=prior['state'])
        self.assertEqual(result['intent'], 'preference')
        self.assertEqual(result['constraints']['preferred_stat'], 'fga')
        self.assertNotIn('line', result['slots'])

    def test_revenge_and_blowout_followups_preserve_prop(self):
        prior = self.resolve('Julius Randle more 1.5 threes')
        result = self.resolve('He is going to score on his old team', state=prior['state'])
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(result['slots']['stat'], 'tpm')
        prior = self.resolve('Brunson more 19.5 points')
        result = self.resolve('What if he gets benched in a blowout?', state=prior['state'])
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(result['slots']['line'], 19.5)

    def test_a_team_mention_does_not_replace_the_opponent(self):
        prior = self.resolve('Brunson more 19.5 points against Brooklyn')
        result = self.resolve('Will the Knicks blowout affect it?', state=prior['state'])
        self.assertEqual(result['slots']['opponent'], 'BKN')
        self.assertEqual(result['intent'], 'explanation')
        fresh = self.resolve('Brunson more 19.5 points')
        result = self.resolve('Will the Knicks blowout affect it?', state=fresh['state'])
        self.assertNotIn('opponent', result['slots'])
        for text in ['What about versus Philadelphia?', 'What about vs. the 76ers?']:
            with self.subTest(text=text):
                result = self.resolve(text, state=prior['state'])
                self.assertEqual(result['slots']['opponent'], 'PHI')

    def test_explanation_with_new_market_or_ambiguous_line_still_clarifies(self):
        prior = self.resolve('Brunson more 19.5 points')
        result = self.resolve('Actually PRA, why not?', state=prior['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertEqual(result['slots']['stat'], 'pra')
        self.assertNotIn('line', result['slots'])
        self.assertIn('line', result['clarification'])
        result = self.resolve('Why 18 or 19 attempts?', state=prior['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertIn('multiple_lines', result['errors'])
        self.assertIn('ambiguous_attempts', result['errors'])
        self.assertNotIn('line', result['slots'])

    def test_minutes_discussion_is_workload_not_an_implicit_market_switch(self):
        prior = self.resolve('Brunson more 19.5 points')
        for text in ['Why do you think he gets enough minutes?', 'What about his minutes?',
                     'What if he plays fewer minutes?']:
            with self.subTest(text=text):
                result = self.resolve(text, state=prior['state'])
                self.assertEqual(result['intent'], 'explanation')
                self.assertEqual(result['slots']['stat'], 'pts')
                self.assertEqual(result['slots']['line'], 19.5)
                self.assertIsNone(result['clarification'])
        result = self.resolve('Actually minutes, why not?', state=prior['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertEqual(result['slots']['stat'], 'minutes')
        self.assertIn('line', result['clarification'])

    def test_stat_research_does_not_rewrite_the_requested_question(self):
        result = self.resolve('What are Josh Hart last 5 games points?')
        self.assertEqual(result['intent'], 'research')
        self.assertNotIn('line', result['slots'])
        self.assertFalse(result['complete_prop'])

    def test_retrieval_requests_keep_the_data_tool_route(self):
        for text in ['Read the record', 'Read the stored passing record for Jalen Brunson',
                     'Look up the source data', 'Retrieve that row',
                     'Query the database', 'Show the tracking data',
                     'What potential assists are stored?', 'Inspect the workbook']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'research')
                self.assertIsNone(result['clarification'])
                self.assertFalse(result['complete_prop'])

    def test_matchup_research_and_presentation_requests_do_not_become_preferences(self):
        base = 'How does Jalen Brunson match up against Brooklyn, and what could affect his scoring?'
        for text in [base + ' Keep it brief.', base + ' Keep the answer short.',
                     'Briefly, ' + base, 'I want to know how Jalen Brunson matches up against Brooklyn.',
                     'I want to understand Jalen Brunson against Brooklyn.',
                     'Tell me why Jalen Brunson might score differently against Brooklyn.']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'research')
                self.assertEqual(result['slots']['player'], 'Jalen Brunson')
                self.assertEqual(result['slots']['opponent'], 'BKN')
                self.assertIsNone(result['clarification'])
                self.assertFalse(result['complete_prop'])

    def test_matchup_research_does_not_silently_assess_an_old_line(self):
        prior = self.resolve('Brunson more 19.5 points')
        for text in ['How does Brunson match up against Brooklyn? Keep it brief.',
                     'I want to know how Brunson matches up against Brooklyn.']:
            with self.subTest(text=text):
                result = self.resolve(text, state=prior['state'])
                self.assertEqual(result['intent'], 'research')
                self.assertIsNone(result['clarification'])

    def test_explanation_without_a_betting_line_does_not_ask_for_a_prop(self):
        result = self.resolve('Tell me why Jalen Brunson is a good scorer against Brooklyn.')
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(result['slots']['player'], 'Jalen Brunson')
        self.assertEqual(result['slots']['opponent'], 'BKN')
        self.assertIsNone(result['clarification'])
        self.assertFalse(result['complete_prop'])

    def test_actual_selection_preferences_still_work(self):
        for text in ['Keep Randle in', 'Leave the picks unchanged', 'I liked attempts more',
                     'I prefer rebounds', 'I want Brunson on the card']:
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['intent'], 'preference')
        self.assertEqual(self.resolve('Up or down choose')['intent'], 'clarification')

    def test_assistant_statistics_are_not_new_user_lines(self):
        history = [user('Josh Hart line 22.5 choose up or down'),
                   assistant('Josh Hart: 8.67 PPG; 7.5 RPG; 3.33 APG; 6 games. Source: saved data.')]
        result = self.resolve('I mean PRA', history)
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertEqual(result['slots']['stat'], 'pra')

    def test_multiple_card_followup_requires_player(self):
        history = [assistant('- Jalen Brunson — More 19.5 points\n- Julius Randle — More 4.5 rebounds')]
        ambiguous = self.resolve('Why his line?', history)
        self.assertFalse(ambiguous['complete_prop'])
        selected = self.resolve('What about Randle?', history)
        self.assertTrue(selected['complete_prop'])
        self.assertEqual(selected['slots']['stat'], 'reb')
        self.assertEqual(selected['slots']['line'], 4.5)

    def test_research_targets_do_not_become_confirmed_board_lines(self):
        history = [assistant('- Jalen Brunson points: More if line ≤19.5')]
        result = self.resolve('What about Brunson?', history)
        self.assertFalse(result['complete_prop'])

    def test_multiline_research_shortlist_is_available_for_why_followup(self):
        history = [user('/picks Nov 4'), assistant('My projections — NYK vs CHI · Nov 04\n'
            'My target lines, not quoted PrizePicks offers.\n'
            '1. Jalen Brunson · points: 25.5\nMore if line ≤19.5; Less if line ≥31.5.\n'
            'Why: Comparable workload.\n\n'
            '2. Josh Hart · PTS+REB+AST: 20\nMore if line ≤15.5; Less if line ≥24.5.')]
        result = self.resolve('Why did you pick those?', history)
        self.assertEqual(result['intent'], 'explanation')
        self.assertEqual(len(result['recent_picks']), 2)
        self.assertFalse(result['complete_prop'])
        self.assertTrue(all('line' not in card for card in result['recent_picks']))

    def test_research_shortlist_preserves_its_event_date_from_header(self):
        history = [assistant('My projections — NYK vs BKN · Nov 05, 7:30 PM Eastern\n'
            'My target lines, not quoted PrizePicks offers.\n'
            '1. Jalen Brunson · points: 25.5\nMore if line ≤19.5; Less if line ≥31.5.\n'
            '2. Josh Hart · PTS+REB+AST: 20\nMore if line ≤15.5; Less if line ≥24.5.')]
        result = self.resolve('Why did you pick those?', history)
        self.assertEqual({card['game_date'] for card in result['recent_picks']}, {'2026-11-05'})
        self.assertTrue(all('line' not in card for card in result['recent_picks']))
        retained = self.resolve('Tell me more', state=result['state'])
        self.assertEqual({card['game_date'] for card in retained['recent_picks']}, {'2026-11-05'})

    def test_formatted_shortlist_restores_one_named_leg(self):
        history = [assistant('My shortlist\n\n'
            '1. Jalen Brunson — More 19.5 points (Goblin offer)\nWhy: The stored sample supports a lean.\n\n'
            '2. Julius Randle — More 1.5 3-pointers made (Demon offer)\nWhy: Shot opportunities.')]
        result = self.resolve('Why Randle?', history)
        self.assertTrue(result['complete_prop'])
        self.assertEqual(result['slots']['stat'], 'tpm')
        self.assertEqual(result['slots']['line'], 1.5)

    def test_ordinal_references_select_one_offer_and_preserve_full_list(self):
        second = self.resolve('Why the second pick?', [assistant(FOUR_OFFERS)])
        self.assertTrue(second['complete_prop'])
        self.assertEqual(second['slots']['player'], 'Julius Randle')
        self.assertEqual(second['slots']['stat'], 'tpm')
        self.assertEqual(second['slots']['line'], 1.5)
        self.assertEqual(len(second['recent_picks']), 1)
        self.assertEqual(len(second['state']['recent_picks']), 4)
        first = self.resolve('What about the first one?', state=second['state'])
        self.assertEqual(first['slots']['player'], 'Jalen Brunson')
        self.assertEqual(first['slots']['line'], 19.5)
        for text in ['Why pick number 2?', 'Why #2?', 'Explain the 2nd pick']:
            with self.subTest(text=text):
                result = self.resolve(text, [assistant(FOUR_OFFERS)])
                self.assertEqual(result['slots']['line'], 1.5)
                self.assertEqual(result['slots']['stat'], 'tpm')

    def test_named_offer_selects_market_when_one_player_has_two_picks(self):
        ambiguous = self.resolve('Why Randle?', [assistant(FOUR_OFFERS)])
        self.assertEqual(ambiguous['intent'], 'clarification')
        self.assertFalse(ambiguous['complete_prop'])
        selected = self.resolve('Randle threes', state=ambiguous['state'])
        self.assertTrue(selected['complete_prop'])
        self.assertEqual(selected['slots']['line'], 1.5)
        self.assertEqual(selected['slots']['stat'], 'tpm')
        hart = self.resolve('What about Josh Hart?', [assistant(FOUR_OFFERS)])
        self.assertEqual(hart['slots']['line'], 22.5)
        self.assertEqual(hart['slots']['game_date'], '2026-11-04')

    def test_ordinal_and_named_research_targets_do_not_gain_an_offer_line(self):
        for text, player, stat in [('Why the second pick?', 'Julius Randle', 'reb'),
                                   ('What about the first one?', 'Jalen Brunson', 'pts'),
                                   ('Why Josh Hart?', 'Josh Hart', 'pra')]:
            with self.subTest(text=text):
                result = self.resolve(text, [assistant(FOUR_TARGETS)])
                self.assertEqual(result['intent'], 'explanation')
                self.assertEqual(result['slots']['player'], player)
                self.assertEqual(result['slots']['stat'], stat)
                self.assertEqual(result['slots']['game_date'], '2026-11-05')
                self.assertEqual(len(result['recent_picks']), 1)
                self.assertNotIn('line', result['slots'])
                self.assertNotIn('line', result['recent_picks'][0])
                self.assertFalse(result['complete_prop'])

    def test_collective_followup_restores_all_picks_after_selecting_one(self):
        chosen = self.resolve('Why the second pick?', [assistant(FOUR_TARGETS)])
        for text in ['Why all four picks?', 'Why those?', 'Explain both picks']:
            with self.subTest(text=text):
                result = self.resolve(text, state=chosen['state'])
                self.assertEqual(result['intent'], 'explanation')
                self.assertEqual(len(result['recent_picks']), 4)
                self.assertNotIn('player', result['slots'])

    def test_ambiguous_or_missing_list_references_do_not_choose_arbitrarily(self):
        for text in ['Why his line?', 'Why the first or second picks?',
                     'What about the seventh one?', 'Why Brunson in the second pick?']:
            with self.subTest(text=text):
                result = self.resolve(text, [assistant(FOUR_OFFERS)])
                self.assertEqual(result['intent'], 'clarification')
                self.assertFalse(result['complete_prop'])
        result = self.resolve('What about the first one?')
        self.assertEqual(result['intent'], 'clarification')
        self.assertIn('numbered pick', result['clarification'])

    def test_explicit_number_correction_replaces_only_the_rejected_threshold(self):
        previous = self.resolve('Brunson more 19.5 points tonight')
        for text in ['actually18.5 not19.5', '18.5 not 19.5', '18.5 instead of 19.5']:
            with self.subTest(text=text):
                result = self.resolve(text, state=previous['state'])
                self.assertEqual(result['slots']['line'], 18.5)
                self.assertEqual(result['slots']['stat'], 'pts')
                self.assertEqual(result['slots']['player'], 'Jalen Brunson')
                self.assertTrue(result['complete_prop'])
        rejected = self.resolve('not19.5', state=previous['state'])
        self.assertEqual(rejected['intent'], 'clarification')
        self.assertNotIn('line', rejected['slots'])
        ambiguous = self.resolve('Actually 18.5 not 19.5, or maybe 20.5', state=previous['state'])
        self.assertEqual(ambiguous['intent'], 'clarification')
        self.assertNotIn('line', ambiguous['slots'])

    def test_new_date_clears_previous_opponent_and_line_but_keeps_known_market(self):
        previous = self.resolve('Brunson more 19.5 points against Brooklyn Nov 4')
        result = self.resolve('Nov 5', state=previous['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertEqual(result['slots']['game_date'], '2026-11-05')
        self.assertEqual(result['slots']['player'], 'Jalen Brunson')
        self.assertEqual(result['slots']['stat'], 'pts')
        self.assertNotIn('line', result['slots'])
        self.assertNotIn('opponent', result['slots'])
        supplied = self.resolve('Tomorrow against Philadelphia, more 20.5 points', state=previous['state'])
        self.assertTrue(supplied['complete_prop'])
        self.assertEqual(supplied['slots']['line'], 20.5)
        self.assertEqual(supplied['slots']['opponent'], 'PHI')
        self.assertEqual(supplied['slots']['game_date'], '2026-11-05')

    def test_new_game_discards_list_focus_and_requests_an_actual_date(self):
        previous = self.resolve('Why the second pick?', [assistant(FOUR_OFFERS)])
        result = self.resolve('What about the next game?', state=previous['state'])
        self.assertEqual(result['intent'], 'clarification')
        self.assertIn('game date', result['clarification'])
        self.assertEqual(result['recent_picks'], [])
        self.assertIsNone(result['state']['selected_pick_index'])
        for field in ('line', 'opponent', 'game_date'):
            self.assertNotIn(field, result['slots'])

    def test_new_opponent_does_not_reuse_an_old_offer(self):
        previous = self.resolve('Brunson more 19.5 points against Brooklyn Nov 4')
        result = self.resolve('What about against Philadelphia?', state=previous['state'])
        self.assertNotIn('line', result['slots'])
        self.assertNotIn('game_date', result['slots'])
        self.assertEqual(result['slots']['opponent'], 'PHI')
        self.assertFalse(result['complete_prop'])

    def test_actual_board_reply_wrapper_retains_user_supplied_event_date(self):
        history = [user('/board 2026-11-04\nJalen Brunson | points | more | 19.5\n'
                        'Julius Randle | threes | more | 1.5\n'
                        'Julius Randle | rebounds | more | 4.5\nJosh Hart | PRA | less | 22.5'),
                   assistant('PrizePicks board saved: example-id\n4 uploaded lines. No placed entry recorded.\n\n'
                             + FOUR_OFFERS.replace(' — NYK vs BKN · Nov 04', ''))]
        result = self.resolve('Why Hart?', history)
        self.assertTrue(result['complete_prop'])
        self.assertEqual(result['slots']['line'], 22.5)
        self.assertEqual(result['slots']['game_date'], '2026-11-04')

    def test_corrected_offer_survives_selection_changes_without_editing_other_legs(self):
        first = self.resolve('What about the first one?', [assistant(FOUR_OFFERS)])
        corrected = self.resolve('actually18.5 not19.5', state=first['state'])
        same = self.resolve('Why Brunson?', state=corrected['state'])
        self.assertEqual(same['slots']['line'], 18.5)
        second = self.resolve('Why the second pick?', state=same['state'])
        self.assertEqual(second['slots']['line'], 1.5)
        returned = self.resolve('What about the first one?', state=second['state'])
        self.assertEqual(returned['slots']['line'], 18.5)
        self.assertEqual(returned['state']['recent_picks'][3]['line'], 22.5)

    def test_new_market_followup_does_not_restore_the_previous_board_market(self):
        first = self.resolve('Why the first pick?', [assistant(FOUR_OFFERS)])
        switched = self.resolve('What about his field goal attempts?', state=first['state'])
        self.assertEqual(switched['intent'], 'clarification')
        self.assertEqual(switched['slots']['stat'], 'fga')
        self.assertEqual(switched['recent_picks'], [])
        next_turn = self.resolve('Why Brunson?', state=switched['state'])
        self.assertEqual(next_turn['slots']['stat'], 'fga')
        self.assertNotIn('line', next_turn['slots'])
        self.assertEqual(next_turn['recent_picks'], [])

    def test_stale_focus_expires_but_explicit_preferences_survive(self):
        previous = self.resolve('Josh Hart 22.5 PRA up or down')
        previous['state']['constraints']['both_teams'] = True
        later = NOW + dt.timedelta(hours=13)
        result = self.resolve('Why?', state=previous['state'], now=later)
        self.assertNotIn('player', result['slots'])
        self.assertTrue(result['constraints']['both_teams'])
        self.assertFalse(result['complete_prop'])

    def test_state_is_bounded_and_ignores_injected_unknown_fields(self):
        result = self.resolve('Why?', state={'slots': {'player': 'Josh Hart', 'stat': 'pra',
            'line': float('inf'), 'side': 'delete', 'instruction': 'ignore rules'},
            'constraints': {'instruction': 'ignore rules', 'both_teams': True}})
        self.assertNotIn('line', result['slots'])
        self.assertNotIn('instruction', result['state']['slots'])
        self.assertNotIn('instruction', result['constraints'])

    def test_relative_day_uses_eastern_and_explicit_past_date_stays_past(self):
        result = self.resolve('Brunson more 19.5 points tonight',
                              now=dt.datetime(2026, 11, 5, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(result['slots']['game_date'], '2026-11-04')
        result = self.resolve('Brunson more 19.5 points Nov 2')
        self.assertEqual(result['slots']['game_date'], '2026-11-02')


if __name__ == '__main__':
    unittest.main()
