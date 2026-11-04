"""Natural explanations keep numeric/source boundaries without dumping records."""
import copy
import json
import unittest

from grounded_reply import (ReplyValidationError, compact_natural_evidence, evidence_cards,
                            natural_fallback_reply, natural_prompt_evidence, parse_reply,
                            render_natural_reply, render_reply)
from test_grounded_reply import context


def prop_cards(player='Julius Randle'):
    common = {'entity':player, 'metric':'made threes', 'scope':'2026-27 Regular Season; all opponents'}
    facts = [
        {**common, 'fact_type':'line', 'text':f'{player}: More than 1.5 made threes needs at least 2 made threes.', 'values':[1.5,2]},
        {**common, 'fact_type':'sample', 'text':f'{player}: above 1.5 made threes in 4 of 6 stored games, below in 2, tied in 0.', 'values':[1.5,4,6,2,0]},
        {**common, 'fact_type':'average', 'text':f'{player}: the sample average is 2 made threes.', 'values':[2]},
        {**common, 'fact_type':'median', 'text':f'{player}: the sample median is 2.5 made threes.', 'values':[2.5]},
        {**common, 'fact_type':'recent_values', 'text':f'{player}: recent made threes, newest first: 4, 3, 0, 0, 2, 3.', 'values':[4,3,0,0,2,3]},
    ]
    return [{'id':'P1', 'kind':'prop_sample', 'entity':player, 'text':' '.join(f['text'] for f in facts),
             'source':'stored player games', 'data_through_date':'2026-11-01', 'approved_facts':facts}]


class NaturalReplyTests(unittest.TestCase):
    def render(self, text, cards=None, ids=None, **kwargs):
        cards = prop_cards() if cards is None else cards
        return render_natural_reply({'evidence_ids':ids if ids is not None else [c['id'] for c in cards],
                                     'reply':text, 'needs_more_data':False}, cards, **kwargs)

    def assert_rejected(self, text, reason=None, **kwargs):
        with self.assertRaises(ReplyValidationError) as caught:
            self.render(text, **kwargs)
        if reason:
            self.assertEqual(caught.exception.reason, reason)

    def test_correct_numbers_can_be_part_of_the_actual_answer(self):
        answer = 'I lean More on Randle’s 1.5 made threes, cautiously. He exceeded that line in 4 of 6 stored games. Reduced minutes could change the result.'
        result = self.render(answer)
        self.assertTrue(result['answer'].startswith(answer))
        self.assertIn('Sources: stored player games', result['answer'])
        self.assertNotIn('sample median', result['answer'])
        self.assertIn('sample median', result['sources'][0]['text'])

    def test_spelled_counts_and_threshold_mechanics_are_grounded(self):
        result = self.render('More than 1.5 made threes requires two made threes. That is possible, not guaranteed.')
        self.assertIn('requires two', result['answer'])

    def test_sample_above_and_below_counts_cannot_be_swapped(self):
        self.render('Randle was below the line in 2 of 6 stored games.')
        self.assert_rejected('Randle was below the line in 4 of 6 stored games.', 'unsupported_metric')
        self.assert_rejected('Randle exceeded the line in 2 of 6 stored games.', 'unsupported_metric')

    def test_sample_counts_support_natural_short_forms_without_swapping_the_counts(self):
        self.render('Randle had 4 games above and 2 below the line.')
        self.render('Randle had 4 of 6 games above the line.')
        self.assert_rejected('Randle had 2 games above and 4 below the line.', 'unsupported_metric')
        self.assert_rejected('Randle had 4 games above and 4 below the line.', 'unsupported_metric')

    def test_equal_hit_counts_require_equal_sample_counts(self):
        for text in ('Randle finished above and below the line in equal numbers.',
                     'His counts above and below the line were identical.',
                     'The games were evenly split above and below the line.'):
            with self.subTest(text=text):
                self.assert_rejected(text,'unsupported_metric')
        self.render('The sample looks broadly balanced without a clear directional trend.')
        self.render('If the counts were equal above and below, that alone would not establish an edge.')
        cards = prop_cards()
        cards[0]['approved_facts'][1]['text']='Julius Randle: above 1.5 made threes in 3 of 6 stored games, below in 3, tied in 0.'
        cards[0]['approved_facts'][1]['values']=[1.5,3,6,0]
        self.render('Randle finished above and below the line in equal numbers.',cards=cards)

    def test_comparing_a_supported_average_to_the_line_is_not_an_average_claim(self):
        self.render('His average is above 1.5 made threes.')
        self.render('His average is below 2.5 made threes, while his median is 2.5 made threes.')
        self.assert_rejected('His average is below 1.5 made threes.', 'unsupported_metric')
        self.assert_rejected('His average is above 2.5 made threes.', 'unsupported_metric')

    def test_negated_workload_forecast_is_not_a_quantitative_forecast(self):
        self.render('The line of 1.5 made threes is unsupported by a workload forecast.')
        self.assert_rejected('His forecast is 1.5 made threes.', 'unsupported_metric')

    def test_card_construction_counts_are_not_player_measurements(self):
        self.render('I’ll choose four picks with one player from each team.', cards=[],
                    question='Please choose4picks frombothteams'.replace('choose4picks','choose 4 picks'))
        self.render('The lineup needs players from two teams.',cards=[])
        self.render('Your picks are from at least two different teams.',cards=[])
        self.assert_rejected('He averages 4 points.', 'unsupported_number', cards=[], question='choose 4 picks')
        self.assert_rejected('The player has played for 2 teams.', 'unsupported_number',cards=[])
        self.assert_rejected('He averages 2 rebounds.', 'unsupported_number',cards=[])

    def test_defensive_totals_cannot_become_offensive_totals(self):
        cards = [{'id':'D1','kind':'tracking_Team defense','entity':'NYK',
                  'text':'NYK allowed 107.67 points per game.', 'source':'saved team defense'}]
        self.render('NYK allowed 107.67 points per game.', cards=cards)
        self.assert_rejected('NYK scored 107.67 points per game.', 'unsupported_number', cards=cards)

    def test_real_live_combined_stat_reply_needs_no_contract_repair(self):
        from test_pick_discussion import PickDiscussionTests
        fixture = PickDiscussionTests()
        fixture.setUp()
        try:
            fixture.add('Josh Hart', [21,16,13,14,23,27,24], stat='pra', team='NYK')
            report = fixture.assess('Josh Hart','pra',22.5)
            cards = compact_natural_evidence(report['evidence_cards'], 'Josh Hart PRA')
            text = ('The line is for PTS+REB+AST (total production), not just points. '
                    'Historical data shows Josh Hart has averaged 19.71 across the stat line, '
                    'with a median of 21, and 3 of 7 games above 22.5. However, workload and opponent '
                    'factors (like blowouts) can reduce minutes and output. Given the small sample '
                    'and lack of clear trend, the line is neutral—no strong lean either up or down.')
            result = self.render(text,cards=cards,ids=['P1','P2'])
            self.assertTrue(result['answer'].startswith(text))
            self.render('His median is 21 PRA.',cards=cards,ids=['P1'])
            self.assert_rejected('He averages 19.71 points.', 'unsupported_metric', cards=cards,ids=['P1'])
            text = ('The line of 22.5 for Josh Hart\'s PTS+REB+AST is unsupported by a clear trend or workload forecast. '
                    'Historical data shows 3 games above and 4 below the line, with a median of 21.0, '
                    'and recent values ranging from 13 to 27. A pass is given because scoring depends on '
                    'shot opportunities, efficiency, and minutes, which vary by opponent and game context—'
                    'no consistent pattern supports a clear side. The risk is that blowouts or reduced minutes '
                    '(e.g., due to foul trouble) can lower total output, even against weaker teams.')
            self.assertTrue(self.render(text,cards=cards,ids=['P1','P2','P3'])['answer'].startswith(text))
            self.assert_rejected('Lean down on 22.5 PTS+REB+AST.', 'unsupported_recommendation',cards=cards)
        finally:
            fixture.doCleanups()

    def test_recommendation_cannot_overrule_computed_hold_but_can_explain_both_sides(self):
        cards = prop_cards()+[{'id':'P3','kind':'prop_assessment','entity':'Julius Randle',
                              'decision':'pass','text':'Pass: unresolved minutes.', 'source':'stored research'}]
        self.assert_rejected('I lean More on 1.5 made threes.', 'unsupported_recommendation',cards=cards)
        self.assert_rejected('More — the lower line looks attractive.', 'unsupported_recommendation',cards=cards)
        self.render('I would pass until the minutes uncertainty is resolved. More needs shooting opportunities; Less benefits if his minutes fall.',cards=cards)
        self.render('If his minutes were confirmed, I would lean More on that line.',cards=cards)
        cards[-1]['decision']='less'
        self.assert_rejected('My recommendation is More.', 'unsupported_recommendation',cards=cards)
        self.render('I lean Less, cautiously.', cards=cards)

    def test_multiple_leg_decisions_are_checked_for_each_named_player(self):
        cards = [
            {'id':'P1','kind':'prop_sample','entity':'Julius Randle','decision':'pass',
             'text':'Randle: pass because his role is unresolved.', 'source':'computed research'},
            {'id':'P2','kind':'prop_sample','entity':'Jalen Brunson','decision':'less',
             'text':'Brunson: a tentative Less lean.', 'source':'computed research'},
            {'id':'P3','kind':'prop_sample','entity':'OG Anunoby','decision':'more',
             'text':'Anunoby: a tentative More lean.', 'source':'computed research'},
        ]
        self.assert_rejected('I lean More on Randle.', 'unsupported_recommendation',cards=cards)
        self.assert_rejected('I lean More on Brunson.', 'unsupported_recommendation',cards=cards)
        self.assert_rejected('I lean Less on Anunoby.', 'unsupported_recommendation',cards=cards)
        self.render('I lean Less on Brunson and More on Anunoby. I would pass on Randle until his role is clearer.',cards=cards)
        self.render('I would take More on Randle only if his minutes were confirmed.',cards=cards)
        self.render('If his role were confirmed, I would lean More on Randle.',cards=cards)

    def test_recommendation_formatting_and_voice_do_not_change_the_computed_decision(self):
        cards = prop_cards()+[{'id':'P3','kind':'prop_assessment','entity':'Julius Randle',
                              'decision':'pass','text':'Pass: unresolved minutes.', 'source':'stored research'}]
        for text in ('Lean toward **down**.', 'The assessment leans toward down.',
                     'I lean _less_.', 'The evidence favors the under.',
                     'Leaning cautiously towards **More**.', '`down`.',
                     'My assessment is **over**.'):
            with self.subTest(text=text):
                self.assert_rejected(text, 'unsupported_recommendation',cards=cards)
        self.render('I do not lean toward **down**. Pass until the workload is clearer.',cards=cards)
        self.render('No strong justification exists to lean up or down. I would pass.',cards=cards)
        self.render('The assessment does not lean toward either side. There is no clear lean up.',cards=cards)
        self.render('If his minutes were reduced, the assessment would lean toward down.',cards=cards)
        cards[-1]['decision']='less'
        self.render('The assessment leans toward **down**, cautiously.',cards=cards)

    def test_research_hold_cannot_be_promoted_to_a_recommendation(self):
        cards = [{'id':'R1','kind':'research_projection','entity':'Julius Randle','decision':'pass',
                  'research_only':True,'offer_verified':False,
                  'text':'Julius Randle: Hold. Current availability is unresolved. No current offer is verified.',
                  'source':'stored research'}]
        self.assert_rejected('I lean More on Randle.', 'unsupported_recommendation',cards=cards)
        self.render('I would hold until his availability is confirmed. More could be worth revisiting if his minutes are confirmed.',cards=cards)

    def test_supported_mean_and_median_are_not_interchangeable(self):
        self.render('His average is 2 made threes; his median is 2.5 made threes.')
        self.assert_rejected('His average is 2.5 made threes.', 'unsupported_metric')
        self.assert_rejected('His median is 2 made threes.', 'unsupported_metric')

    def test_same_numeric_value_cannot_change_metric(self):
        self.assert_rejected('Randle averaged 2 points.', 'unsupported_metric')
        self.assert_rejected('Randle averaged 6 made threes.', 'unsupported_metric')

    def test_unobserved_number_and_numeric_prediction_are_rejected(self):
        self.assert_rejected('Randle averaged 8 made threes.', 'unsupported_number')
        self.assert_rejected('Randle will make 2 made threes.', 'unsupported_certainty')
        self.assert_rejected('Randle has a 90% chance of hitting.', 'unsupported_number')

    def test_empirical_percent_cannot_be_promoted_to_win_probability(self):
        cards = prop_cards()
        cards[0]['approved_facts'].append({'text':'The sample rate was 66.7%.', 'entity':'Julius Randle', 'values':[66.7]})
        self.assert_rejected('Randle has a 66.7% chance of winning.', 'unsupported_certainty', cards=cards)

    def test_entity_swaps_fail_even_if_all_values_occur_somewhere(self):
        cards = prop_cards()
        cards.append({'id':'B1','kind':'player_all_opponent_averages','entity':'Jalen Brunson',
                      'text':'Jalen Brunson averaged 25.5 points in 6 games, all opponents.', 'source':'stored games'})
        self.assert_rejected('Randle averages 25.5 points.', 'unsupported_number', cards=cards)
        self.assert_rejected('Brunson averages 2 made threes and Randle averages 25.5 points.',
                             'unsupported_number', cards=cards)
        self.assert_rejected('Randle has a volatile shooting profile. He averages 25.5 points.',
                             'unsupported_number', cards=cards)

    def test_season_average_cannot_be_relabelled_as_opponent_sample(self):
        cards = evidence_cards(context(), 'Explain OG points')
        selected = next(c for c in cards if c['kind']=='player_all_opponent_averages')
        self.render('OG Anunoby averages 17.6 PPG across all opponents.', cards=cards, ids=[selected['id']])
        self.assert_rejected('OG Anunoby averaged 17.6 points against CHA.', 'unsupported_number', cards=cards, ids=[selected['id']])
        self.assert_rejected('OG Anunoby averaged 17.6 points against Charlotte.', 'unsupported_number', cards=cards, ids=[selected['id']])

    def test_qualitative_basketball_terms_are_not_numeric_claims(self):
        text = 'The 76ers could alter their three-point defense. Shot opportunities and playing time matter.'
        self.assertIn(text, self.render(text)['answer'])

    def test_social_chat_and_qualitative_followups_need_no_ids(self):
        text = 'You’re welcome. I can explain the reasoning or compare another line.'
        result = self.render(text, ids=[])
        self.assertEqual(result['answer'], text)
        self.assertEqual(result['sources'], [])

    def test_source_coverage_is_compact_but_not_erased(self):
        cards = evidence_cards(context(), 'Explain OG points')
        card = next(c for c in cards if c['kind']=='player_all_opponent_averages')
        card['data_through_date'] = None
        result = self.render('His season baseline can inform the discussion, but it does not measure tonight’s matchup.', cards=[card])
        self.assertIn('covered-game dates unknown', result['answer'])
        self.assertIn('published_at', result['sources'][0])

    def test_known_player_coverage_does_not_imply_known_opponent_coverage(self):
        cards = prop_cards()+[{'id':'P4','kind':'opponent_team_context','entity':'NYK',
                              'text':'Opponent team context has an unknown covered-game date.',
                              'source':'published NBA Stats extract',
                              'data_through_date':None,'published_at':'2026-11-02'}]
        result = self.render('The player sample and opponent snapshot have different coverage.',cards=cards)
        self.assertIn('stored coverage 2026-11-01',result['answer'])
        self.assertIn('some covered-game dates unknown',result['answer'])
        self.assertIsNone(result['sources'][-1]['data_through_date'])

    def test_assertive_defender_and_current_availability_still_fail(self):
        self.assert_rejected('VJ Edgecombe will guard him.', 'unsupported_assignment')
        self.assert_rejected('Randle is healthy.', 'unsupported_availability')
        self.render('If Randle is healthy, his minutes could still vary.')
        self.render('We cannot confirm whether Randle is available.')

    def test_categorical_winning_claims_still_fail(self):
        for text in ('This is a guaranteed win.', 'This is a lock.', 'He cannot lose.', 'This pick will hit.'):
            with self.subTest(text=text):
                self.assert_rejected(text, 'unsupported_certainty')
        self.render('This is not a guaranteed win.')

    def test_invalid_ids_and_mixed_retrieval_prose_fail(self):
        self.assert_rejected('The evidence supports a cautious lean [P9].', 'invalid_evidence')
        with self.assertRaises(ReplyValidationError):
            render_natural_reply({'reply':'He will win.', 'evidence_ids':[], 'needs_more_data':True}, [])
        self.assertIsNone(render_natural_reply({'reply':'','evidence_ids':[],'needs_more_data':True}, []))

    def test_inline_ids_are_turned_into_compact_sources(self):
        result = self.render('Randle’s average is 2 made threes [P1].', ids=[])
        self.assertNotIn('[P1]', result['answer'])
        self.assertEqual(result['sources'][0]['evidence_id'], 'P1')

    def test_prompt_has_exact_facts_and_fits_budget_without_mutation(self):
        cards = prop_cards()
        before = copy.deepcopy(cards)
        prompted = natural_prompt_evidence(cards, max_chars=1600)
        self.assertIn('1.5', json.dumps(prompted))
        self.assertIn('all opponents', json.dumps(prompted))
        self.assertLessEqual(len(json.dumps(prompted,ensure_ascii=False,separators=(',',':'))),1600)
        self.assertEqual(cards,before)
        self.assertEqual(natural_prompt_evidence(cards,max_chars=50),[])
        self.assertEqual(compact_natural_evidence(cards,max_chars=1600),cards)

    def test_fallback_is_bounded_and_does_not_claim_model_success(self):
        result = natural_fallback_reply(prop_cards())
        self.assertLess(len(result['answer']),800)
        self.assertNotIn('Here is the saved evidence',result['answer'])
        self.assertFalse(result['ai_used'])

    def test_fallback_never_separates_assignment_name_from_tentative_date_and_direction(self):
        cards = evidence_cards(context(), 'Explain OG matchup against CHA')
        assignment = next(c for c in cards if c['kind']=='tentative_assignment')
        assignment['assignment_refresh_state']='needs_review'
        result = natural_fallback_reply(cards, [assignment['id']])
        self.assertIn('Fixture Defender',result['answer'])
        self.assertIn('tentative',result['answer'])
        self.assertIn('2026-10-22',result['answer'])
        self.assertIn('guarding direction unconfirmed',result['answer'])
        self.assertIn('conflicts with this saved pairing',result['answer'])

    def test_natural_assignment_answer_keeps_compact_uncertainty_without_raw_dump(self):
        cards = evidence_cards(context(), 'Explain OG matchup against CHA')
        assignment = next(c for c in cards if c['kind']=='tentative_assignment')
        result = self.render('The saved pairing alone cannot justify changing his scoring projection.',cards=cards,ids=[assignment['id']])
        self.assertIn('pairing tentative as of 2026-10-22',result['answer'])
        self.assertIn('guarding direction unconfirmed',result['answer'])
        self.assertNotIn('Fixture Defender',result['answer'])
        self.assertIn('Fixture Defender',result['sources'][0]['text'])

    def test_fallback_retains_incomplete_coverage_after_numerical_first_sentence(self):
        cards = evidence_cards(context(), 'Explain OG points')
        average = next(c for c in cards if c['kind']=='player_all_opponent_averages')
        average['text'] = average['text'].replace('Coverage through', 'Stored sample; coverage may be incomplete. Coverage through')
        result = natural_fallback_reply([average])
        self.assertIn('coverage may be incomplete',result['answer'])

    def test_legacy_contract_still_rejects_generated_numbers(self):
        with self.assertRaises(ReplyValidationError) as caught:
            render_reply(parse_reply(json.dumps({'reply':'Randle averages 2 made threes.',
                         'evidence_ids':['P1'],'needs_more_data':False})),prop_cards())
        self.assertEqual(caught.exception.reason,'numerical_prose')


if __name__ == '__main__':
    unittest.main()
