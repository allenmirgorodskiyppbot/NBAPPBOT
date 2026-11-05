"""Natural sentence structure cannot mix entities, populations or metric values."""
import copy
import unittest

from grounded_reply import render_natural_reply, ReplyValidationError
from pick_discussion import discussion_evidence
from upcoming_game import preview_evidence


def team_cards():
    preview={'next_game_focus':{'team':'NYK','opponent':'CLE'},'recent_form':[
        {'team':'NYK','games':5,'wins':2,'losses':3,'points_for_average':108.4,
         'points_against_average':108,'data_through_date':'2026-10-30','sources':['fixture NYK finals']},
        {'team':'CLE','games':1,'wins':1,'losses':0,'points_for_average':125,
         'points_against_average':121,'data_through_date':'2026-10-24','sources':['fixture CLE finals']} ]}
    return preview_evidence(preview)


def prop_cards():
    report={'player':'Jalen Brunson','stat':'pts','line':25.5,'team':'NYK',
        'season':'2026-27','season_type':'Regular Season','decision':'pass',
        'reason':'The observed margin is too small to support a side.',
        'conditional_risks':['Playing time can change.','This is a research benchmark.'],
        'sample':{'n':6,'more':2,'less':4,'ties':0,'mean':25.5,'median':23.5,
                  'values':[24,38,23,28,19,21],'last_date':'2026-10-30'},
        'baseline':{'available':True,'n':20,'reference_projection':25.55,
                    'baseline_median':27,'seasons':['2025-26','2026-27'],'last_date':'2026-10-30'},
        'workload':{'reference_minutes':None}}
    return discussion_evidence(report)


class NaturalMixedTeamClauseTests(unittest.TestCase):
    def render(self,text,cards=None,ids=None):
        cards=cards if cards is not None else team_cards()
        return render_natural_reply({'reply':text,'evidence_ids':ids or ['G1','G2'],
            'needs_more_data':False},cards)

    def reject(self,text,cards=None,ids=None):
        with self.assertRaises(ReplyValidationError,msg=text):
            self.render(text,cards,ids)

    def test_named_offense_and_defense_clauses_keep_each_subject(self):
        for text in ['CLE scored 125 points per game, while NYK allowed 108 points per game.',
                     'CLE scored 125 PPG, whereas NYK allowed 108 PPG.',
                     'CLE averaged 125 points per game and NYK allowed 108 points per game.',
                     'CLE scored 125 PPG, as NYK allows 108 PPG.',
                     'CLE scored 125 PPG, NYK allowed 108 PPG.']:
            with self.subTest(text=text):self.assertIn(text,self.render(text)['answer'])

    def test_nested_watch_subject_is_not_an_opponent_specific_average(self):
        text=('Watch NYK’s defensive performance against CLE’s high scoring (125 PPG), '
              'as NYK allows 108 PPG. CLE’s single-game sample limits insight. '
              'NYK’s sample size is 5 games.')
        self.assertIn(text,self.render(text)['answer'])

    def test_swapped_entities_and_offense_defense_never_share_numeric_support(self):
        for text in ['NYK scored 125 PPG, as CLE allowed 108 PPG.',
                     'CLE allowed 125 PPG, while NYK scored 108 PPG.',
                     'Watch NYK’s defense against CLE’s scoring (108 PPG), as NYK allows 125 PPG.',
                     'CLE scored 125 PPG, as NYK allows 121 PPG.']:
            with self.subTest(text=text):self.reject(text)

    def test_invented_values_and_missing_quantified_subjects_are_not_detached(self):
        for text in ['CLE scored 999 PPG, as NYK allows 108 PPG.',
                     'Scored 125 PPG, as NYK allows 108 PPG.',
                     'CLE scored 125 PPG, as allowed 108 PPG.',
                     'Watch defensive performance against high scoring (125 PPG), as NYK allows 108 PPG.']:
            with self.subTest(text=text):self.reject(text)

    def test_all_opponent_scoring_cannot_become_a_measured_head_to_head_average(self):
        self.reject('CLE averaged 125 PPG against NYK, as NYK allowed 108 PPG.')
        self.reject('NYK allowed 108 PPG against CLE.')

    def test_actual_model_sample_median_baseline_average_and_threshold_label_are_supported(self):
        text=("Jalen Brunson's sample shows 2 of 6 games above 25.5 points, with a median of 23.5. "
              'The 20-game baseline average is 25.55, but recent performance includes below-25.5 games. '
              'Risk: blowouts or foul trouble may reduce minutes and shot attempts.')
        self.assertIn(text,self.render(text,prop_cards(),['P1','P5'])['answer'])

    def test_hyphen_threshold_label_requires_actual_line_and_nonzero_direction_count(self):
        self.render('Jalen Brunson has above-25.5 games in the saved sample.',prop_cards(),['P1'])
        self.reject('Jalen Brunson has below-26 games in the saved sample.',prop_cards(),['P1'])
        self.reject('Jalen Brunson played 25.5 games.',prop_cards(),['P1'])
        cards=copy.deepcopy(prop_cards())
        sample=next(f for f in cards[0]['approved_facts'] if f['fact_type']=='sample')
        sample.update(text='Jalen Brunson: above 25.5 points in 6 of 6 stored games, below in 0, tied in 0.',
                      values=[25.5,6,0])
        self.reject('Jalen Brunson has below-25.5 games in the saved sample.',cards,['P1'])

    def test_baseline_and_recent_sample_statistics_cannot_trade_labels(self):
        cards=prop_cards()
        for text in ["Jalen Brunson's baseline median is 23.5 points.",
                     "Jalen Brunson's sample median is 27 points.",
                     "Jalen Brunson's baseline average is 25.5 points.",
                     "Jalen Brunson's sample average is 25.55 points.",
                     "Jalen Brunson's baseline average is 27 points.",
                     "Jalen Brunson's baseline median is 25.55 points."]:
            with self.subTest(text=text):self.reject(text,cards,['P1','P5'])
        self.render("Jalen Brunson's sample median is 23.5 points. His baseline average is 25.55 points.",cards,['P1','P5'])

    def test_baseline_comparison_uses_average_instead_of_game_count_or_median(self):
        cards=prop_cards()
        self.render("Jalen Brunson's baseline average is above 25.5 points.",cards,['P1','P5'])
        self.reject("Jalen Brunson's baseline average is below 25.5 points.",cards,['P1','P5'])


if __name__=='__main__':unittest.main()
