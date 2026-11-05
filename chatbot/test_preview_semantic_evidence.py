"""Semantic routing chooses a read-only lookup; generated facts stay scoped."""
import unittest

import test_upcoming_game as fixtures
from grounded_reply import render_natural_reply, ReplyValidationError
from upcoming_game import build_preview, preview_evidence, validate_preview_analysis


class SemanticPreviewEvidenceTests(unittest.TestCase):
    setUp = fixtures.UpcomingGameTests.setUp
    calendar = fixtures.UpcomingGameTests.calendar
    final = fixtures.UpcomingGameTests.final
    snapshot = fixtures.UpcomingGameTests.snapshot

    def prepared(self):
        self.final('2026-11-04','NYK','CHI',105,100)
        self.final('2026-11-02','BKN','NYK',110,100)
        self.final('2026-11-01','CLE','BOS',99,90)
        return build_preview(self.store,"what's upcoming?",now=fixtures.NOW,
                             team='NYK',resolved_intent=True)

    def test_semantic_intent_bypasses_phrase_match_but_not_read_only_evidence(self):
        self.assertIsNone(build_preview(self.store,"what's upcoming?",now=fixtures.NOW))
        before = self.snapshot()
        result = build_preview(self.store,"anything on the horizon?",now=fixtures.NOW,
                               team='NYK',resolved_intent=True)
        self.assertEqual(result['next_game_focus']['opponent'],'CLE')
        self.assertIn('8:00 PM Eastern',result['answer_header'])
        self.assertIn('current timing is unverified',result['answer_header'])
        self.assertIn('availability and starting lineups are not verified',result['answer_header'])
        self.assertEqual(self.snapshot(),before)

    def test_semantic_route_keeps_offered_prop_and_multi_team_ambiguity_guards(self):
        self.assertIsNone(build_preview(self.store,'Randle more1.5threes next game',
            now=fixtures.NOW,team='NYK',resolved_intent=True))
        result = build_preview(self.store,'anything upcoming for the Bulls and Lakers?',
            now=fixtures.NOW,team='CHI',resolved_intent=True)
        self.assertIn('Which team',result['answer'])
        self.assertNotIn('next_game_focus',result)
        with self.assertRaises(ValueError):
            build_preview(self.store,'what upcoming',now=fixtures.NOW,team='XYZ',resolved_intent=True)

    def test_explicit_subject_and_first_person_opponent_are_distinguished(self):
        their = build_preview(self.store,'what is on the horizon for Cleveland?',
            now=fixtures.NOW,team='NYK',resolved_intent=True)
        self.assertEqual(their['upcoming_game_team'],'CLE')
        ours = build_preview(self.store,'how do we look going into Cleveland?',
            now=fixtures.NOW,team='NYK',resolved_intent=True)
        self.assertEqual(ours['upcoming_game_team'],'NYK')

    def test_preview_evidence_keeps_sample_dates_and_offense_defense_separate(self):
        result = self.prepared();cards=preview_evidence(result)
        self.assertLessEqual(len(cards),4)
        team_cards={card['entity']:card for card in cards if card.get('entity')}
        self.assertEqual(team_cards['NYK']['data_through_date'],'2026-11-04')
        self.assertEqual(team_cards['CLE']['data_through_date'],'2026-11-01')
        facts=team_cards['NYK']['approved_facts']
        self.assertEqual(facts[0]['values'],[102.5])
        self.assertEqual(facts[1]['values'],[105])
        self.assertIn('allowed',facts[1]['text'])
        self.assertNotIn('8:00',str(cards))
        self.assertFalse(any(card.get('kind')=='verified_availability' for card in cards))

    def test_actual_scoped_synthesis_passes_existing_validator_and_alias_guard(self):
        result=self.prepared();cards=preview_evidence(result)
        text='NYK averaged 102.5 points per game; CLE allowed an average of 90 points per game. These samples came against different opponents.'
        generated=render_natural_reply({'evidence_ids':['G1','G2'],'reply':text,
            'needs_more_data':False},cards)
        self.assertTrue(validate_preview_analysis(generated['answer'],result))
        self.assertTrue(validate_preview_analysis(text.replace('NYK','the Knicks').replace('CLE','Cleveland'),result))
        self.assertTrue(validate_preview_analysis('If rotation minutes change, counting statistics can change too.',result))
        self.assertTrue(validate_preview_analysis("CLE's saved sample is through 2026-11-01.",result))

    def test_wrong_team_wrong_direction_and_new_values_never_become_approved(self):
        result=self.prepared()
        for text in ['The Knicks averaged 99 points per game.',
                     'The Knicks allowed an average of 102.5 points per game.',
                     'Cleveland averaged 102.5 points per game.',
                     'NYK averaged 120 points per game.',
                     'NYK has a 95% chance to win.',
                     'NYK is guaranteed to win.']:
            with self.subTest(text=text):
                self.assertFalse(validate_preview_analysis(text,result))

    def test_model_cannot_override_application_timing_or_availability_header(self):
        result=self.prepared()
        for text in ['Tipoff is confirmed at 8:00 PM.', 'This game is tonight.', 'It is confirmed.',
                     'The starting lineup is confirmed.', 'Josh Hart is injured.',
                     'Their player availability is confirmed.']:
            with self.subTest(text=text):
                self.assertFalse(validate_preview_analysis(text,result))
        self.assertIn('current timing is unverified',result['answer_header'])

    def test_today_without_clock_stays_a_candidate_without_becoming_verified_upcoming(self):
        self.calendar([('2026-11-05','NYK','CHI',None,'time_tbd'),
                       ('2026-11-07','CLE','NYK','2026-11-08T01:00:00Z','scheduled')])
        # Complete date coverage does not establish same-day event timing.
        self.store.set_checkpoint('season_calendar:2026-27',{
            'covered_teams':['NYK','CHI','CLE'],'complete_for_published_games':True})
        before=self.snapshot()
        result=build_preview(self.store,"anything upcoming?",now=fixtures.NOW,
                             team='NYK',resolved_intent=True)
        self.assertEqual(result['next_game_focus']['date'],'2026-11-05')
        self.assertEqual(result['next_game_focus']['opponent'],'CHI')
        self.assertFalse(result['next_game_focus']['upcoming_established'])
        self.assertFalse(result['next_game_focus']['timing_verified'])
        self.assertIn('Next saved matchup',result['answer_header'])
        self.assertNotIn('Next game for',result['answer_header'])
        self.assertIn('time TBD',result['answer_header'])
        self.assertIn("can't tell whether it has started",result['answer_header'])
        self.assertIn('current status are unverified',result['answer_header'])
        limits=next(card for card in preview_evidence(result) if card['id']=='G3')
        self.assertIn('not established to still be upcoming',limits['text'])
        self.assertFalse(any(card.get('kind')=='verified_availability' for card in preview_evidence(result)))
        self.assertEqual(self.snapshot(),before)

    def test_known_live_final_and_postponed_today_still_allow_future_candidate(self):
        for status in ['live','final','postponed','canceled']:
            with self.subTest(status=status):
                self.calendar([('2026-11-05','NYK','CHI',None,'time_tbd'),
                               ('2026-11-07','CLE','NYK','2026-11-08T01:00:00Z','scheduled')])
                self.store.upsert_game({'id':fixtures.canonical_game('2026-11-05','NYK','CHI'),
                    'date':'2026-11-05','home_team':'NYK','away_team':'CHI','status':status,
                    'source':'https://example.test/observed-status'})
                result=build_preview(self.store,'next Knicks game',now=fixtures.NOW)
                self.assertEqual(result['next_game_focus']['date'],'2026-11-07')
                self.assertTrue(result['next_game_focus']['upcoming_established'])


if __name__=='__main__':
    unittest.main()
