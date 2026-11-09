"""Public feed choices retain source identity without becoming verified offers."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import _assistant_cards, resolve_conversation


NOW = dt.datetime(2026, 11, 9, 18, tzinfo=dt.timezone.utc)
NAMES = ['Karl-Anthony Towns', 'Jalen Brunson']
ANSWER = (
    'Game date: 2026-11-10 Eastern.\n\n'
    "From PrizePicks' public feed\n\n"
    '1. Karl-Anthony Towns — More 9.5 rebounds (NYK; Goblin offer)\n'
    'RESEARCH PICK: Research buffer supported.\n\n'
    '2. Jalen Brunson — Less 0.5 offensive rebounds (NYK; standard offer)\n'
    'RESEARCH PICK: Mixed historical support.\n\n'
    'My own calculated targets from your master\n\n'
    '1. Karl-Anthony Towns · PTS+REB+AST: 36.6\n'
    'More if line ≤30.5; Less if line ≥42.5.\n\n'
    '2. Jalen Brunson · FG attempts: 19.95\n'
    'More if line ≤15.5; Less if line ≥23.5.'
)


class PrizePicksConversationTests(unittest.TestCase):
    def setUp(self):
        names = patch('conversation._players', return_value=NAMES)
        names.start()
        self.addCleanup(names.stop)

    def resolve(self, message, *, answer=ANSWER, state=None):
        return resolve_conversation(None, None, message,
            [{'role': 'assistant', 'content': answer}] if state is None else None,
            state, now=NOW)

    def test_provider_heading_preserves_quote_and_research_mode_with_separate_master_targets(self):
        cards = _assistant_cards(ANSWER, NAMES, NOW)
        self.assertEqual(len(cards), 4)
        for card in cards[:2]:
            self.assertEqual(card['proposal_group'], 'uploaded')
            self.assertEqual(card['source_kind'], 'prizepicks_public_feed')
            self.assertTrue(card['research_choice'])
            self.assertEqual(card['game_date'], '2026-11-10')
            self.assertNotIn('offer_verified', card)
            self.assertNotIn('live_verified', card)
        for card in cards[2:]:
            self.assertEqual(card['proposal_group'], 'independent')
            self.assertEqual(card['line_kind'], 'research_target')
            self.assertNotIn('line', card)
            self.assertNotIn('side', card)
            self.assertNotIn('source_kind', card)

    def test_natural_feed_source_ordinals_select_exact_displayed_choice(self):
        for message in ('Why first from PrizePicks?', 'Explain first from the public feed.',
                        'Why first from API lines?', 'Why first from provider lines?'):
            with self.subTest(message=message):
                turn = self.resolve(message)
                self.assertIsNone(turn['clarification'])
                self.assertEqual(turn['slots']['player'], 'Karl-Anthony Towns')
                self.assertEqual(turn['slots']['stat'], 'reb')
                self.assertEqual(turn['slots']['line'], 9.5)
                self.assertEqual(turn['slots']['side'], 'more')
                self.assertEqual(turn['recent_picks'][0]['source_kind'], 'prizepicks_public_feed')

    def test_saved_state_keeps_feed_provenance_without_accepting_verification_flags(self):
        turn = self.resolve('Why second from PrizePicks?')
        state = copy.deepcopy(turn['state'])
        for card in state['recent_picks']:
            card['offer_verified'] = True
            card['live_verified'] = True
        again = self.resolve('Why that one?', state=state)
        card = again['recent_picks'][0]
        self.assertEqual(card['player'], 'Jalen Brunson')
        self.assertEqual(card['source_kind'], 'prizepicks_public_feed')
        self.assertTrue(card['research_choice'])
        self.assertNotIn('offer_verified', card)
        self.assertNotIn('live_verified', card)

    def test_unqualified_ordinal_clarifies_feed_versus_master_group(self):
        turn = self.resolve('Why the first pick?')
        self.assertIn('PrizePicks feed lines', turn['clarification'])
        self.assertIn('master targets', turn['clarification'])

    def test_missing_sides_raw_quotes_do_not_become_side_selections(self):
        raw = ('Game date: 2026-11-10 Eastern.\n'
               "From PrizePicks' public feed\n"
               '• Karl-Anthony Towns · 24.5 points (NYK; standard offer)\n'
               'Available sides are unknown.\n'
               '• Jalen Brunson · 2.5 3PTM (NYK; Demon offer)')
        self.assertEqual(_assistant_cards(raw, NAMES, NOW), [])

    def test_ordinary_upload_source_does_not_inherit_previous_feed_source(self):
        answer = (ANSWER.split('My own calculated targets')[0] +
                  'From your uploaded lines\n'
                  '3. Jalen Brunson — More 26.5 points (standard offer)\n'
                  'RESEARCH PICK: Narrow research lean.')
        cards = _assistant_cards(answer, NAMES, NOW)
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[-1]['proposal_group'], 'uploaded')
        self.assertNotIn('source_kind', cards[-1])

    def test_master_source_request_keeps_unpriced_target_separate(self):
        turn = self.resolve('Why first from my master?')
        self.assertIsNone(turn['clarification'])
        self.assertEqual(turn['slots']['player'], 'Karl-Anthony Towns')
        self.assertEqual(turn['slots']['stat'], 'pra')
        self.assertNotIn('line', turn['slots'])
        self.assertNotIn('side', turn['slots'])
        self.assertNotIn('source_kind', turn['recent_picks'][0])

    def test_recomputed_research_explanation_keeps_feed_heading_for_next_followup(self):
        import test_uploaded_research_picks as fixtures
        from uploaded_research_discussion import build_uploaded_research_discussion

        fixture = fixtures.UploadedResearchPickTests()
        fixture.setUp()
        try:
            data = fixture.build(fixture.user_fixture())
            first = data['research_picks'][0]
            pick = {key: first[key] for key in ('player', 'stat', 'line')}
            pick.update(side=first['lean_side'], game_date=fixtures.DATE, research_choice=True,
                        line_kind='assistant_proposal', proposal_group='uploaded',
                        source_kind='prizepicks_public_feed')
            with patch('uploaded_research_discussion.assess_pregame_freshness',
                       return_value=fixtures.TIMING_ONLY):
                result = build_uploaded_research_discussion(fixture.store, [pick], now=fixtures.NOW)
            self.assertIn("From PrizePicks' public feed", result['answer'])
            self.assertNotIn('From your uploaded lines', result['answer'])
            turn = resolve_conversation(None, None, 'Why first from PrizePicks?',
                [{'role': 'assistant', 'content': result['answer']}], now=fixtures.NOW)
            self.assertIsNone(turn['clarification'])
            card = turn['recent_picks'][0]
            self.assertEqual(card['source_kind'], 'prizepicks_public_feed')
            self.assertTrue(card['research_choice'])
            self.assertNotIn('offer_verified', card)
        finally:
            fixture.doCleanups()

    def test_natural_this_game_question_uses_master_values_and_actual_saved_opponent(self):
        from unittest.mock import AsyncMock
        import app
        from chat_flow import answer_conversation
        from feeds import canonical_game, canonical_player
        import test_next_matchup_master_lookup as fixtures

        fixture = fixtures.NextMatchupMasterLookupTests()
        fixture.setUp()
        try:
            with fixture.store.connect() as db:
                db.execute('UPDATE player_games SET pts=pts+10 WHERE player=?', ('Jalen Brunson',))
            day = '2026-10-18'
            identity = canonical_game(day, 'NYK', 'MIL')
            fixture.store.upsert_game({'id': identity, 'date': day, 'home_team': 'NYK',
                'away_team': 'MIL', 'status': 'final', 'source': 'synthetic opponent context'})
            fixture.store.upsert_player_game({'game_id': identity, 'date': day,
                'player': 'Jalen Brunson', 'player_id': canonical_player('Jalen Brunson'),
                'team': 'NYK', 'opponent': 'MIL', 'season': '2026-27',
                'season_type': 'Regular Season', 'appearance_state': 'Played',
                'minutes': 34, 'pts': 28, 'reb': 4, 'ast': 5, 'fga': 20,
                'source': 'synthetic opponent context'})
            reply = ('Game date: 2026-11-10 Eastern.\n'
                "From PrizePicks' public feed\n"
                '1. Jalen Brunson — More 25.5 points (NYK; standard offer)\n'
                'RESEARCH PICK: Research buffer supported.\n'
                'My own calculated targets from your master\n'
                '1. Jalen Brunson · points: 31.5\n'
                'More if line ≤25.5; Less if line ≥37.5.')
            request = app.ChatRequest(message='Why Brunson over 25.5 points against MIL this game?',
                history=[{'role': 'assistant', 'content': reply}])
            readiness = {**fixtures.UNKNOWN, 'statistics_ready': True, 'statistics_issues': []}
            model = AsyncMock()
            async def run():
                with patch('conversation._now', return_value=NOW), \
                        patch('uploaded_research_discussion._now', return_value=NOW), \
                        patch('uploaded_research_discussion.assess_pregame_freshness', return_value=readiness):
                    return await answer_conversation(None, fixture.store, request, {}, model)
            import asyncio
            result = asyncio.run(run())
            text = result['answer']
            self.assertIn('Jalen Brunson — More 25.5 points', text)
            self.assertIn('20/20 above', text)
            self.assertIn('mean 31.50', text)
            self.assertIn('NYK vs MIL on 2026-11-10', text)
            self.assertIn('1 prior comparable game against MIL', text)
            self.assertIn('mean 28.00', text)
            self.assertIn('No numerical matchup bonus', text)
            self.assertFalse(result['approved_for_entry'])
            self.assertFalse(result['offer_verified'])
            model.assert_not_called()
        finally:
            fixture.doCleanups()

    def test_new_line_with_same_market_in_both_groups_is_new_assessment(self):
        reply = ('Game date: 2026-11-10 Eastern.\n'
            "From PrizePicks' public feed\n"
            '1. Jalen Brunson — More 25.5 points (NYK; standard offer)\n'
            'RESEARCH PICK: Research buffer supported.\n'
            'My own calculated targets from your master\n'
            '1. Jalen Brunson · points: 31.5\n'
            'More if line ≤25.5; Less if line ≥37.5.')
        turn = self.resolve('What about Brunson more 27.5 points?', answer=reply)
        self.assertIsNone(turn['clarification'])
        self.assertEqual(turn['slots']['line'], 27.5)
        self.assertTrue(turn['complete_prop'])
        self.assertIsNone(turn['state']['selected_pick_index'])
        self.assertEqual(turn['state']['recent_picks'][0]['line'], 25.5)
        self.assertNotIn('line', turn['state']['recent_picks'][1])


if __name__ == '__main__':
    unittest.main()
