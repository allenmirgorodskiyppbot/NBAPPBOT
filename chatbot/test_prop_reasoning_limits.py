"""Historical centers/counts neither measure stability nor replace a data hold."""
import unittest

from grounded_reply import render_natural_reply, ReplyValidationError
from pick_discussion import discussion_evidence


BAD=('Historical mean and median exceed the line, indicating consistent performance above it. '
     'Recent outcomes show balanced results, supporting current hold.')


class PropReasoningLimitsTests(unittest.TestCase):
    def setUp(self):
        report={'player':'Jalen Brunson','stat':'pra','line':30,'team':'NYK',
            'season':'2026-27','season_type':'Regular Season','decision':'pass',
            'reason':'Pass for now: A current game-day refresh is required; future dates remain historical research.',
            'conditional_risks':['Playing time can change.','This is a research benchmark.'],
            'sample':{'n':8,'more':5,'less':3,'ties':0,'mean':32.75,'median':33,
                      'values':[34,38,32,35,26,29,40,28],'last_date':'2026-11-04'},
            'baseline':{'available':True,'n':20,'reference_projection':35,
                        'baseline_median':35,'seasons':['2025-26','2026-27'],'last_date':'2026-11-04'},
            'workload':{'reference_minutes':None}}
        self.cards=discussion_evidence(report)

    def accept(self,text,cards=None):
        return render_natural_reply({'evidence_ids':['P1','P5','P3'],'reply':text,'needs_more_data':False},
                                    self.cards if cards is None else cards)

    def reject(self,text,cards=None):
        with self.assertRaises(ReplyValidationError,msg=text):self.accept(text,cards)

    def test_actual_live_paragraph_cannot_infer_consistency_from_mean_and_median(self):
        self.reject(BAD)

    def test_correct_center_to_line_comparison_remains_available(self):
        self.accept('Historical mean and median exceed the offered line. This remains historical context.')

    def test_equal_mean_and_median_alone_are_not_stability_evidence(self):
        self.reject('The equal historical mean and median show reliable performance.')

    def test_observed_above_line_count_does_not_license_consistent_hitting(self):
        self.reject('Jalen Brunson consistently clears the line.')

    def test_baseline_reference_does_not_establish_a_stable_role(self):
        self.reject('Jalen Brunson has a stable role and reliable production.')

    def test_unmeasured_variance_cannot_be_declared_low(self):
        self.reject('The historical mean and median show low variance.')

    def test_unmeasured_dispersion_cannot_be_declared_limited(self):
        self.reject('Jalen Brunson has limited dispersion and a tight spread.')

    def test_prediction_confidence_is_not_a_property_of_descriptive_averages(self):
        self.reject('The baseline makes this a high-confidence pick.')

    def test_positive_outcome_confidence_requires_calibration(self):
        self.reject('The averages give high confidence in clearing this line.')

    def test_negative_consistency_statement_is_a_valid_limit(self):
        self.accept('Historical centers above the line do not establish consistent performance. The future game still needs a current game-day refresh.')

    def test_conditional_role_and_variance_mechanisms_remain_discussable(self):
        self.accept('If the role became stable, minutes could be more predictable. That is a condition to check, not an observed fact.')

    def test_explicit_unknown_stability_is_valid(self):
        self.accept('It is unknown whether this is consistent performance. The saved sample alone cannot establish that.')

    def test_individual_outcomes_can_vary_around_their_center(self):
        self.accept('Individual outcomes can fall above or below their mean. This does not establish the next result.')

    def test_both_sides_observed_is_not_an_equal_count_or_future_probability(self):
        self.accept('Stored outcomes occur on both sides of the line. Those observations do not establish a future probability.')

    def test_source_data_hold_can_be_explained_even_when_centers_support_more(self):
        text=('The historical mean and median exceed the offered line, but I am holding because '
              'this future game requires a current game-day data refresh.')
        self.assertIn(text,self.accept(text)['answer'])

    def test_actual_v4_typicality_claim_is_not_established_by_centers(self):
        self.reject('The historical mean and median are above the line, which is normal for this stat. '
                    'The decision to hold is due to the need for a current game-day verification, '
                    'not because of past performance trends.')

    def test_centers_do_not_establish_typical_performance(self):
        for text in ('The mean and median show typical performance.',
                     'That is usual for this metric.',
                     'This is routine statistical performance.'):
            with self.subTest(text=text):self.reject(text)

    def test_typicality_limits_can_be_discussed_without_asserting_them(self):
        for text in ('The historical median is above the line, but this does not establish typical performance.',
                     'It is unknown whether this is normal for this stat.',
                     'If this became typical performance, the evidence would still need review.'):
            with self.subTest(text=text):self.accept(text)

    def test_actual_v4_live_update_cannot_validate_an_unplayed_result(self):
        self.reject('The decision is a pass because a current game-day verification is required. '
                    'Historical data shows the mean and median are above the line, but this does not '
                    "confirm today's outcome—only a live update can validate the result.")

    def test_pregame_verification_does_not_establish_results(self):
        for text in ('A live update confirms the outcome.',
                     'Current data verification assures a successful result.',
                     'Pregame data checks prove future performance.',
                     'A current game-day refresh validates the game result.'):
            with self.subTest(text=text):self.reject(text)

    def test_negative_and_conditional_result_validation_remain_discussable(self):
        for text in ('A live update cannot validate the result.',
                     'Current data verification does not confirm the outcome.',
                     'If live data could confirm the outcome, that would be a different kind of evidence.',
                     'A current game-day refresh may confirm the outcome, but that possibility is not established.'):
            with self.subTest(text=text):self.accept(text)

    def test_live_verification_can_check_inputs_without_validating_the_result(self):
        for text in ('A live update can confirm the schedule. It cannot establish a future result.',
                     'Current game-day verification checks the inputs, rather than proving performance.',
                     'A live data check can validate the stored data.'):
            with self.subTest(text=text):self.accept(text)


if __name__=='__main__':unittest.main()
