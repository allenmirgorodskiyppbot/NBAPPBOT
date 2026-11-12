"""Authority contracts are independent of particular user prompt wording."""
import unittest

from dialogue_authority import validate_authority


PLAYER = 'Jalen Brunson'
FOCUS = {'player': PLAYER, 'stat': 'pts', 'line': 25.5}
CARD = {'id': 'E1', 'entity': PLAYER, 'kind': 'saved_observations',
        'text': 'Synthetic saved player facts.'}


def assessment(player=PLAYER, stat='pts', line=25.5, decision='more', available=True):
    return {'tool': 'prop_assessment', 'args': {'player': player, 'stat': stat, 'line': line},
            'decision': decision, 'available': available, 'cards': [CARD]}


class DialogueAuthorityTests(unittest.TestCase):
    def rejects(self, text, **kwargs):
        with self.assertRaises(ValueError, msg=text):
            validate_authority(text, **kwargs)

    def test_no_read_empirical_claims_are_rejected_without_numeric_claims(self):
        for text in (
            'Brunson has a larger role tonight.', 'HE IS CONFIRMED ACTIVE.',
            'The Knicks traded him yesterday.', 'He usually dominates Cleveland.',
            'He is listed as questionable.', 'He is on a minutes restriction.',
            'He averaged plenty of points against Cleveland.',
            'Brunson recorded a triple-double.', 'Jalen Brunson has dominated Cleveland.',
            'The next game is in Cleveland.', 'Tipoff is at the usual time.',
            'He is healthy. But I cannot confirm that.',
            "He's active.", "He's a game-time decision.",
            'He will play tonight.', 'He is expected to start.',
        ):
            with self.subTest(text=text):
                self.rejects(text, focus=FOCUS)

    def test_unsourced_side_recommendations_do_not_borrow_general_data_authority(self):
        for text in ('Take More.', 'I like the over.', 'The over is my play.', 'Bet over.',
                     'Brunson: More.', 'Jalen Brunson over 25.5 points.',
                     'I would lean toward **down**.', 'I recommend More for Brunson.'):
            with self.subTest(text=text):
                self.rejects(text, cards=[CARD], focus=FOCUS)

    def test_bare_explicit_bet_does_not_require_a_previous_player_focus(self):
        self.rejects('Bet over.')
        self.rejects('The over is my play.')
        self.rejects('I recommend More.')

    def test_conditional_mechanisms_and_attributed_reports_are_not_current_facts(self):
        for text in (
            'If his minutes fall, counting statistics may fall too.',
            'You said he is questionable; that does not establish whether he will play.',
            'According to your report he is questionable, so wait for clarification.',
            'If he is active, that still does not establish unrestricted minutes.',
            'If the actual line is low enough, I would consider More.',
            'There is no basis to recommend More.',
            'I cannot confirm whether he is healthy.',
        ):
            with self.subTest(text=text):
                validate_authority(text, focus=FOCUS)

    def test_conceptual_and_software_help_survive_an_old_player_focus(self):
        for text in (
            'Attempts count shots taken; points also depend on accuracy and free throws.',
            'An assist is recorded when the official scorer credits a pass leading to a basket.',
            'I recommend more sleep.', 'I would lean more on observed data than hype.',
            'I recommend more testing before deployment.',
            'The service is available over HTTP.',
            'A statistical average does not guarantee the next result.',
        ):
            with self.subTest(text=text):
                validate_authority(text, focus=FOCUS)

    def test_only_executed_successful_exact_prop_assessment_authorizes_a_side(self):
        validate_authority('I recommend More for Brunson.', cards=[CARD],
                           reads=[assessment()], focus=FOCUS, allow_decisions=True)
        for read in ({'tool': 'prop_assessment', 'args': assessment()['args']},
                     assessment(available=False), assessment(decision='pass'),
                     {'tool': 'player_games', **{k: v for k, v in assessment().items() if k != 'tool'}},
                     assessment(player='Josh Giddey')):
            with self.subTest(read=read):
                self.rejects('I recommend More for Brunson.', cards=[CARD], reads=[read],
                             focus=FOCUS, allow_decisions=True)

    def test_a_strict_result_cannot_override_explanation_only_authority(self):
        self.rejects('I recommend More for Brunson.', cards=[CARD], reads=[assessment()],
                     focus=FOCUS, allow_decisions=False)

    def test_entity_direction_metric_and_threshold_must_match(self):
        self.rejects('I recommend Less for Brunson.', cards=[CARD], reads=[assessment()],
                     focus=FOCUS, allow_decisions=True)
        self.rejects('I recommend More 15.5 points for Brunson.', cards=[CARD], reads=[assessment()],
                     focus=FOCUS, allow_decisions=True)
        self.rejects('I recommend More 25.5 rebounds for Brunson.', cards=[CARD], reads=[assessment()],
                     focus=FOCUS, allow_decisions=True)

    def test_multiple_assessments_need_an_unambiguous_target_market(self):
        reads = [assessment(), assessment(stat='reb', line=3.5, decision='less')]
        self.rejects('Brunson: More.', cards=[CARD], reads=reads, focus=FOCUS, allow_decisions=True)
        validate_authority('I recommend More 25.5 points for Brunson.', cards=[CARD], reads=reads,
                           focus=FOCUS, allow_decisions=True)
        validate_authority('I recommend Less 3.5 rebounds for Brunson.', cards=[CARD], reads=reads,
                           focus=FOCUS, allow_decisions=True)

    def test_two_players_cannot_supply_an_ambiguous_heading(self):
        reads = [assessment(), assessment(player='Josh Giddey', stat='ast', line=7.5)]
        self.rejects('More.', reads=reads, cards=[CARD], allow_decisions=True)
        validate_authority('More.', reads=reads, cards=[CARD], focus=FOCUS, allow_decisions=True)

    def test_plan_like_executable_content_is_not_an_assessment(self):
        self.rejects('Take More.', focus=FOCUS, reads=[{'tool': 'prop_assessment',
                     'args': {'player': PLAYER, 'stat': 'pts', 'line': 25.5},
                     'decision': 'more'}], allow_decisions=True)

    def test_scoring_cards_do_not_confer_current_status_or_role_authority(self):
        for text in ('JALEN BRUNSON IS CONFIRMED ACTIVE.',
                     'Jalen Brunson has a larger role tonight.', 'He will play tonight.'):
            with self.subTest(text=text):
                self.rejects(text, cards=[CARD], reads=[{'tool': 'player_games', 'args': {'player': PLAYER}}])

    def test_status_claim_in_a_clarification_is_not_excused_by_a_later_question(self):
        for text in ('Brunson is confirmed active. Which statistic do you mean?',
                     'JALEN BRUNSON IS HEALTHY. What is the line?'):
            with self.subTest(text=text):
                self.rejects(text)
        validate_authority('Is Brunson available?')
        validate_authority('Which line do you mean if he is available?')

    def test_no_read_baseline_relation_requires_factual_evidence(self):
        for text in ('The baseline is above the line.', 'His average being above the line is encouraging.',
                     'The saved median is comfortably below the threshold.'):
            with self.subTest(text=text):
                self.rejects(text, focus=FOCUS)
        validate_authority('If the baseline is above the line, it still does not establish a win probability.', focus=FOCUS)
        validate_authority('The baseline is above the line.', focus=FOCUS, cards=[CARD])

    def test_unknown_availability_read_cannot_authorize_clearance(self):
        read = {'tool': 'availability', 'available': True, 'args': {'player': PLAYER, 'date': '2026-11-11'},
                'data': {'official_designation_known': False, 'official_designation': {'known': False}}}
        self.rejects('Jalen Brunson is available.', focus=FOCUS, reads=[read], cards=[CARD])

    def test_fresh_exact_official_status_is_supported_but_does_not_imply_role_or_health(self):
        read = {'tool': 'availability', 'available': True, 'args': {'player': PLAYER, 'date': '2026-11-11'},
                'data': {'official_designation_known': True, 'official_designation': {
                    'known': True, 'status': 'Available', 'game_date': '2026-11-11',
                    'report_at': '2026-11-11T19:00:00+00:00', 'source_url': 'https://example.test/official'}}}
        validate_authority('Jalen Brunson is Available according to the official NBA report.',
                           focus=FOCUS, reads=[read], cards=[CARD])
        for text in ('Jalen Brunson is healthy.', 'Jalen Brunson is confirmed active.',
                     'Jalen Brunson will play tonight.', 'Jalen Brunson has a larger role tonight.'):
            with self.subTest(text=text):
                self.rejects(text, focus=FOCUS, reads=[read], cards=[CARD])

    def test_official_status_is_bound_to_player_and_exact_designation(self):
        read = {'tool': 'availability', 'available': True, 'args': {'player': PLAYER, 'date': '2026-11-11'},
                'data': {'official_designation': {'known': True, 'status': 'Out', 'game_date': '2026-11-11',
                    'report_at': '2026-11-11T19:00:00+00:00', 'source_url': 'https://example.test/official'}}}
        validate_authority('Jalen Brunson is Out as of the dated official NBA report.',
                           focus=FOCUS, reads=[read], cards=[CARD])
        self.rejects('Jalen Brunson is available.', focus=FOCUS, reads=[read], cards=[CARD])
        self.rejects('Josh Giddey is Out.', focus=FOCUS, reads=[read], cards=[CARD])
        read['data']['official_superseded_or_limited'] = True
        self.rejects('Jalen Brunson is Out.', focus=FOCUS, reads=[read], cards=[CARD])
        validate_authority('Jalen Brunson is Out as of the dated official NBA report.',
                           focus=FOCUS, reads=[read], cards=[CARD])

    def test_read_only_engine_never_confirms_a_mutation_or_entry_submission(self):
        for text in ('Your entry has been placed.', 'I submitted your lineup.',
                     'The database has been updated.', 'I refreshed your master file.',
                     'The daily refresh completed.', 'Refresh is done.'):
            with self.subTest(text=text):
                self.rejects(text, cards=[CARD], reads=[assessment()])
        for text in ('You said your entry has been placed.', 'If your entry has been placed, check its receipt.',
                     'When the daily refresh completed, you should have received its status.',
                     'Use /refresh to check the dataset.', 'I cannot confirm that your entry has been placed.'):
            with self.subTest(text=text):
                validate_authority(text, focus=FOCUS)

    def test_unknown_or_empty_text_is_not_authoritative(self):
        for value in ('', None, {}, []):
            self.rejects(value)


if __name__ == '__main__':
    unittest.main()
