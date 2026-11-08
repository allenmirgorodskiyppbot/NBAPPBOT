"""A research choice keeps its measured explanation through a follow-up."""
import copy
import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch

import app
from board_shortlist import format_shortlist
from chat_flow import answer_conversation
import test_uploaded_research_picks as fixtures
from uploaded_research_discussion import (build_uploaded_research_discussion,
                                           matches_uploaded_research_context)

NOW, TIMING_ONLY, DATE = fixtures.NOW, fixtures.TIMING_ONLY, fixtures.DATE


class UploadedResearchExplanationTests(unittest.TestCase):
    setUp = fixtures.UploadedResearchPickTests.setUp
    observe = fixtures.UploadedResearchPickTests.observe
    offers = fixtures.UploadedResearchPickTests.offers
    build = fixtures.UploadedResearchPickTests.build
    user_fixture = fixtures.UploadedResearchPickTests.user_fixture

    def cards(self):
        data = self.build(self.user_fixture())
        return [{key: row[key] for key in ('player', 'stat', 'line')} |
                {'side': row['lean_side'], 'game_date': DATE, 'research_choice': True,
                 'proposal_group': 'uploaded', 'line_kind': 'assistant_proposal'}
                for row in data['research_picks']]

    def explain(self, cards, freshness=TIMING_ONLY, now=NOW):
        with patch('uploaded_research_discussion.assess_pregame_freshness', return_value=freshness):
            return build_uploaded_research_discussion(self.store, cards, now=now)

    def test_four_research_reasons_recompute_twenty_games_and_keep_recent_conflict(self):
        cards = self.cards()
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        result = self.explain(cards)
        text = result['answer']
        self.assertIn('More 9.5 rebounds', text)
        self.assertIn('18/20 above', text)
        self.assertIn('More 32.5 PTS+REB+AST', text)
        self.assertIn('Less 0.5 offensive rebounds', text)
        self.assertIn('Last 5: 0/5', text)
        self.assertIn('Recent observed games run against', text)
        self.assertIn('live game timing is unverified', text)
        self.assertNotIn('Pass for now', text)
        self.assertFalse(result['approved_for_entry'])
        self.assertFalse(result['eligible_for_ranking'])
        self.assertFalse(result['offer_verified'])
        self.assertTrue(all(not item['approved_for_entry'] for item in result['discussion']['assessments']))
        with self.store.connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)

    def test_readiness_out_started_or_changed_sample_does_not_reaffirm_old_side(self):
        cards = self.cards()
        for state in ({**TIMING_ONLY, 'statistics_ready': False, 'statistics_issues': ['Missing latest game.']},):
            result = self.explain(cards[:1], state)
            self.assertIn('earlier research choice is held now', result['answer'])
            self.assertNotIn('RESEARCH PICK:', result['answer'])
        with patch('uploaded_research_discussion.player_availability', return_value={
                'blocked': True, 'note': 'Officially Out.'}):
            self.assertIn('Officially Out', self.explain(cards[:1])['answer'])
        self.assertIn('held now', self.explain(cards[:1], now=NOW + dt.timedelta(hours=3))['answer'])
        old = copy.deepcopy(cards[:1]); old[0]['side'] = 'less'
        result = self.explain(old)
        self.assertIn('no longer supports that readable side', result['answer'])
        self.assertNotIn('— More', result['answer'])

    def test_only_typed_matching_uploaded_context_routes_to_research_explanation(self):
        card = self.cards()[0]
        slots = {key: card[key] for key in ('player', 'stat', 'line', 'side', 'game_date')}
        self.assertTrue(matches_uploaded_research_context({'recent_picks': [card], 'slots': slots}))
        self.assertFalse(matches_uploaded_research_context({
            'recent_picks': [{**card, 'research_choice': False}], 'slots': slots}))
        for key, value in [('line', 10.5), ('stat', 'pts'), ('side', 'less'), ('game_date', '2026-11-08')]:
            self.assertFalse(matches_uploaded_research_context({
                'recent_picks': [card], 'slots': {**slots, key: value}}))


class UploadedResearchFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_rendered_reply_ordinal_followup_uses_same_research_rule_without_model(self):
        fixture = fixtures.UploadedResearchPickTests()
        fixture.setUp()
        try:
            data = fixture.build(fixture.user_fixture())
            reply = f'Game date: {DATE} Eastern.\n\nFrom your uploaded lines\n\n' + format_shortlist(data)
            request = app.ChatRequest(message='why the fourth from the screenshots?', history=[
                {'role': 'user', 'content': 'Pick four from my screenshots'},
                {'role': 'assistant', 'content': reply}])
            model = AsyncMock()
            with patch('conversation._now', return_value=NOW), \
                    patch('uploaded_research_discussion._now', return_value=NOW), \
                    patch('uploaded_research_discussion.assess_pregame_freshness', return_value=TIMING_ONLY):
                result = await answer_conversation(None, fixture.store, request, {}, model)
            self.assertIn('Jalen Brunson — Less 0.5 offensive rebounds', result['answer'])
            self.assertIn('13/20 below', result['answer'])
            self.assertIn('Last 5: 0/5', result['answer'])
            self.assertNotIn('Pass for now', result['answer'])
            self.assertFalse(result['approved_for_entry'])
            model.assert_not_called()
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
