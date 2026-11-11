"""Replay the user's real KAT correction and availability question through chat."""
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from chat_flow import answer_conversation, try_fast_answer
from conversation import resolve_conversation
from feeds import canonical_game, canonical_player
from master import MasterStore
from structured_availability import (normalize_official_pdf, build_availability_document,
                                     import_availability_document)


NOW = dt.datetime(2026, 11, 11, 19, 30, tzinfo=dt.timezone.utc)
DATE = '2026-11-11'
PLAYER = 'Karl-Anthony Towns'
PDF = 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-11_01_30PM.pdf'
TIMING_HOLD = {
    'ready': False, 'statistics_ready': True, 'timing_verified': False,
    'statistics_issues': [],
    'timing_issues': ['A successful independent check of today’s game schedule within 15 minutes is required.'],
    'reasons': ['A successful independent check of today’s game schedule within 15 minutes is required.'],
}


class KATFollowupFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Import before replacing the shared clock so a direct function import
        # cannot retain this test's MagicMock after cleanup.
        import availability_discussion
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.master = MasterStore(Path(temp.name) / 'conversation.sqlite')
        self.master.upsert_game({
            'id': canonical_game(DATE, 'NYK', 'MIA'), 'date': DATE, 'home_team': 'NYK', 'away_team': 'MIA',
            'status': 'scheduled', 'start_time': '2026-11-12T00:30:00+00:00',
            'source': 'Synthetic saved schedule for conversation regression only',
        })
        for index in range(20):
            day = (dt.date(2026, 11, 10) - dt.timedelta(days=index)).isoformat()
            identity = canonical_game(day, 'NYK', 'BOS')
            self.master.upsert_game({
                'id': identity, 'date': day, 'home_team': 'NYK', 'away_team': 'BOS',
                'status': 'final', 'source': 'Synthetic final-game fixture',
            })
            for player in (PLAYER, 'Jalen Brunson'):
                self.master.upsert_player_game({
                    'game_id': identity, 'player_id': canonical_player(player), 'player': player,
                    'team': 'NYK', 'opponent': 'BOS', 'date': day, 'season': '2026-27',
                    'season_type': 'Regular Season', 'appearance_state': 'Played',
                    'pts': 22, 'reb': 8, 'ast': 6, 'fga': 16, 'tpa': 5, 'tpm': 2,
                    'minutes': 32, 'source': 'Synthetic master observations',
                })
        for target in ('conversation._now', 'pick_discussion._now', 'availability_discussion._now'):
            p = patch(target, return_value=NOW)
            p.start()
            self.addCleanup(p.stop)
        p = patch('pick_discussion.assess_pregame_freshness', return_value=TIMING_HOLD)
        p.start()
        self.addCleanup(p.stop)

    def state(self, question='kat 15.5 PRA'):
        return resolve_conversation(None, self.master, question, now=NOW)['state']

    def request(self, message, history=None):
        return SimpleNamespace(message=message, history=history or [], lineup_preferences=None)

    def official(self, status):
        row = normalize_official_pdf([
            {'game_date': DATE, 'away_team': 'MIA', 'home_team': 'NYK', 'team': 'NYK',
             'player': PLAYER, 'status': status, 'reason': 'Synthetic reported condition'},
        ], source_url=PDF, report_at='2026-11-11T13:30:00-05:00',
            text_sha256='f' * 64, now=NOW)[0]
        import_availability_document(self.master, build_availability_document([row], now=NOW), now=NOW)

    def snapshot(self):
        with self.master.connect() as db:
            return '\n'.join(db.iterdump())

    async def focused(self, message, *, state=None, history=None, fast=False):
        request = self.request(message, history)
        turn = resolve_conversation(None, self.master, message, history=history,
                                    state=state, now=NOW)
        model = AsyncMock(side_effect=AssertionError('Availability reply must not generate AI text'))
        network = AsyncMock(side_effect=AssertionError('No source request'))
        with patch('httpx.AsyncClient.send', new=network), \
                patch('chat_flow.build_pick_discussion', side_effect=AssertionError('Do not replay the full prop')) as full_prop:
            if fast:
                # This is the app's pre-model readiness/queue route. History is
                # intentional: the public helper resolves the turn itself.
                result = await try_fast_answer(None, self.master, request, {}, prefer_natural=True)
            else:
                result = await answer_conversation(None, self.master, request, {}, model,
                                                   turn=turn, natural_dialogue=True)
        model.assert_not_awaited()
        network.assert_not_awaited()
        full_prop.assert_not_called()
        self.assertIsInstance(result, dict)
        self.assertFalse(result['ai_used'])
        return result, turn

    async def test_screenshot_sequence_corrects_prior_pra_and_answers_questionable_followup(self):
        state = self.state()
        history = []
        for question in ('kat 15.5 p+r', 'kat 15.5 p+r'):
            turn = resolve_conversation(None, self.master, question, state=state, now=NOW)
            self.assertEqual((turn['slots']['player'], turn['slots']['stat'], turn['slots']['line']),
                             (PLAYER, 'pr', 15.5))
            self.assertIsNone(turn.get('clarification'))
            result = await answer_conversation(None, self.master, self.request(question, history),
                {}, AsyncMock(), turn=turn, natural_dialogue=False)
            self.assertEqual(result['discussion']['stat'], 'pr')
            self.assertNotIn('PTS+REB+AST', result['answer'])
            history.extend([{'role': 'user', 'content': question},
                            {'role': 'assistant', 'content': result['answer']}])
            state = turn['state']
        before = self.snapshot()
        result, turn = await self.focused('kat is listed as questionable what should we do?', state=state)
        text = result['answer'].lower()
        self.assertEqual((turn['slots']['stat'], turn['slots']['line']), ('pr', 15.5))
        self.assertIn('questionable', text)
        self.assertRegex(text, r'\b(?:you|your)\b')
        self.assertRegex(text, r'\b(?:hold|wait|skip)\b')
        self.assertRegex(text, r'\b(?:cleared|clearance|available|active)\b')
        self.assertRegex(text, r'\b(?:minutes?|workload|reported limit)\b')
        self.assertNotIn('15 minutes is required', text)
        self.assertNotIn('20-game same-team baseline', text)
        self.assertNotIn('36.85', text)
        self.assertEqual(before, self.snapshot())

    async def test_fast_path_answers_before_model_queue_with_real_dialogue_history(self):
        result, _ = await self.focused('kat is listed as questionable what should we do?', fast=True,
            history=[{'role': 'user', 'content': 'kat 15.5 p+r'},
                     {'role': 'assistant', 'content': 'Karl-Anthony Towns — Pass 15.5 points + rebounds.'}])
        self.assertIn('questionable', result['answer'].lower())
        self.assertNotIn('15 minutes is required', result['answer'])

    async def test_existing_wrong_pra_bot_history_does_not_require_clearing_the_chat(self):
        old_reply = (
            'Karl-Anthony Towns — Pass 15.5 PTS+REB+AST (2026-11-11).\n'
            'Pass for now: unresolved reported availability or minutes; '
            'A successful independent check of today’s game schedule within 15 minutes is required.\n'
            'Baseline: 20 same-team games (2025-26, 2026-27), average 36.85, median 37, '
            'through 2026-11-10.'
        )
        history = [
            {'role': 'user', 'content': 'kat 15.5 p+r'},
            {'role': 'assistant', 'content': old_reply},
            {'role': 'user', 'content': 'kat 15.5 p+r'},
            {'role': 'assistant', 'content': old_reply},
        ]
        result, turn = await self.focused('kat is listed as questionable what should we do?', history=history)
        self.assertEqual((turn['slots']['player'], turn['slots']['stat'], turn['slots']['line']),
                         (PLAYER, 'pr', 15.5))
        self.assertEqual(result['discussion']['stat'], 'pr')
        self.assertNotIn('PTS+REB+AST', result['answer'])
        self.assertNotIn('15 minutes is required', result['answer'])
        self.assertNotIn('36.85', result['answer'])

    async def test_fresh_saved_official_out_cannot_be_erased_by_a_user_questionable_report(self):
        self.official('Out')
        result, _ = await self.focused('kat is questionable, what should we do?',
                                      state=self.state('kat 15.5 p+r'))
        text = result['answer'].lower()
        self.assertRegex(text, r'\bout\b')
        self.assertRegex(text, r'\b(?:skip|omit|leave (?:him )?out|hold|do not use|don.t use)\b')
        self.assertIn(PDF, json.dumps(result.get('sources', [])))
        self.assertIsNot(result.get('approved_for_entry'), True)
        self.assertIsNot(result.get('offer_verified'), True)

    async def test_unknown_status_does_not_turn_an_assistant_caution_into_a_user_report(self):
        result, _ = await self.focused('what is his status?', history=[
            {'role': 'user', 'content': 'kat 15.5 p+r'},
            {'role': 'assistant', 'content': 'Karl-Anthony Towns — Pass 15.5 points + rebounds. He might be questionable.'},
        ])
        text = result['answer'].lower()
        self.assertNotIn('you report he is questionable', text)
        self.assertNotIn('you said he is questionable', text)
        self.assertNotIn('confirmed healthy', text)
        self.assertNotIn('cleared for full minutes', text)
        self.assertNotIn('15 minutes is required', text)

    async def test_ambiguous_market_still_clarifies_before_any_availability_answer(self):
        result, turn = await self.focused('kat 15.5 p+r+x is he questionable?',
                                          state=self.state('kat 15.5 PRA'))
        self.assertIsNotNone(turn.get('clarification'))
        self.assertEqual(result['answer'], turn['clarification'])
        self.assertNotIn('Since you report', result['answer'])

    async def test_new_player_in_status_question_does_not_inherit_kat_line(self):
        result, turn = await self.focused('is Brunson questionable, what should we do?',
                                          state=self.state('kat 15.5 p+r'))
        self.assertEqual(turn['slots']['player'], 'Jalen Brunson')
        self.assertNotIn('line', turn['slots'])
        self.assertIn('Jalen Brunson', result['answer'])
        self.assertNotIn('15.5', result['answer'])
        self.assertNotIn(PLAYER, result['answer'])

    async def test_compound_status_question_is_not_recast_as_a_user_assertion(self):
        result, _ = await self.focused('He is not out, is he questionable?',
                                      state=self.state('kat 15.5 p+r'))
        text = result['answer'].lower()
        self.assertNotIn('you report that he is questionable', text)
        self.assertNotIn('you said he is questionable', text)
        self.assertIsNone(result['availability_discussion']['official_status'])
        self.assertFalse(result['availability_discussion']['availability_verified'])

    async def test_newly_reported_available_status_retains_its_unverified_source(self):
        result, _ = await self.focused('He was questionable, but now he is available.',
                                      state=self.state('kat 15.5 p+r'))
        metadata = result['availability_discussion']
        self.assertEqual(metadata['user_report']['status'], 'available')
        self.assertFalse(metadata['availability_verified'])
        self.assertIn('not independently verified', result['answer'].lower())
        self.assertNotIn('you report that he is questionable', result['answer'].lower())


if __name__ == '__main__':
    unittest.main()
