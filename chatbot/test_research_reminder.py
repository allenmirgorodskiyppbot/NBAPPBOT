"""Failed live clocks may label current statistical research, never certify timing."""
import unittest
from unittest.mock import patch

from gameday_digest import _uploaded_pick_scan, reminder_status, render_digest
import test_gameday_fallback_reminder as fixtures

NOW, TIP = fixtures.NOW, fixtures.TIP


class ResearchReminderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FallbackReminderTests.asyncSetUp
    outbox = fixtures.FallbackReminderTests.outbox

    async def test_timeout_with_verified_statistics_recomputes_research_once(self):
        self.failure = 'timeout'
        def readiness(store, day, team, current):
            return {'ready':False, 'statistics_ready':True, 'timing_verified':False}
        with patch('pregame_freshness.assess_pregame_freshness', side_effect=readiness) as assess, \
             patch('gameday_digest.render_digest', return_value='Timing unverified; current historical targets') as render:
            result = await self.digest.tick()
            again = await self.digest.tick()
        self.assertEqual(result['notice_kind'], 'timing_warning')
        self.assertFalse(result['timing_verified'])
        self.assertTrue(result['research_included'])
        self.assertEqual(again['state'], 'already_queued_today')
        self.assertTrue(again['research_included'])
        render.assert_called_once()
        self.assertTrue(render.call_args.args[1]['using_saved_calendar'])
        self.assertEqual(render.call_args.kwargs['board_owner_chat_id'], 123)
        self.assertEqual({call.args[2] for call in assess.call_args_list}, {'NYK','CHI'})
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.outbox()[0]['expires_at'], TIP.timestamp())
        receipt = self.store.get_checkpoint('knicks-gameday:2026-11-04')
        self.assertEqual(receipt['notice_kind'], 'timing_warning')
        self.assertEqual(receipt['queued_at'], NOW.isoformat())
        self.assertEqual(receipt['statistics_checked_at'], NOW.isoformat())
        self.assertIn('schedule_checked_at', receipt)
        self.assertFalse(receipt['timing_verified'])
        self.assertIn('Live timing remains unverified', reminder_status(self.store))
        self.worker.collect_news_sources.assert_not_awaited()

    async def test_missing_or_unconfirmed_statistics_keeps_old_hold_notice(self):
        for case in ({'statistics_ready':False}, {'ready':True}, {}):
            with self.subTest(readiness=case):
                with self.store.connect() as db:
                    db.execute("DELETE FROM checkpoints WHERE name='knicks-gameday:2026-11-04'")
                    db.execute('DELETE FROM telegram_outbox')
                with patch('pregame_freshness.assess_pregame_freshness', return_value=case), \
                     patch('gameday_digest.render_digest') as render:
                    result = await self.digest.tick()
                render.assert_not_called()
                self.assertFalse(result['research_included'])
                self.assertIn('Picks held', self.outbox()[0]['text'])

    async def test_one_teams_missing_statistics_does_not_approve_both_teams(self):
        with patch('pregame_freshness.assess_pregame_freshness',
                   side_effect=lambda store,day,team,now:{'statistics_ready':team == 'NYK'}), \
             patch('gameday_digest.render_digest') as render:
            result = await self.digest.tick()
        render.assert_not_called()
        self.assertFalse(result['research_included'])
        self.assertFalse(result['timing_verified'])

    async def test_known_terminal_event_still_prevents_any_research_notice(self):
        for state in ('live','final','canceled','postponed','suspended'):
            with self.subTest(state=state):
                self.store.upsert_game({**self.games[0], 'status':state})
                with patch('pregame_freshness.assess_pregame_freshness', return_value={'statistics_ready':True}), \
                     patch('gameday_digest.render_digest') as render:
                    result = await self.digest.tick()
                render.assert_not_called()
                self.assertEqual(result['state'], 'no_upcoming_tipoff_today')
                self.assertEqual(self.outbox(), [])

    async def test_clock_advancing_to_tipoff_during_input_check_never_queues(self):
        def readiness(*args):
            self.current = TIP
            return {'statistics_ready':True}
        with patch('pregame_freshness.assess_pregame_freshness', side_effect=readiness), \
             patch('gameday_digest.render_digest') as render:
            result = await self.digest.tick()
        self.assertEqual(result['state'], 'game_started')
        render.assert_not_called()
        self.assertEqual(self.outbox(), [])

    async def test_saved_calendar_render_enables_research_and_preserves_source_age(self):
        game = {**self.games[0], 'using_saved_calendar':True}
        publication = '2026-10-29T12:00:00+00:00'
        with patch('assignment_refresh.refresh_assignments'), \
             patch('independent_picks.build_independent_picks', return_value={}) as build, \
             patch('independent_picks.format_independent_picks', return_value='Research only: four own targets'), \
             patch('gameday_digest._uploaded_pick_scan', return_value='No uploaded lines'):
            text = render_digest(self.store, game, NOW, publication, False, board_owner_chat_id=123)
        self.assertTrue(build.call_args.kwargs['allow_unverified_timing'])
        self.assertIn('Timing unverified', text)
        self.assertIn('Calendar publication last checked: '+publication, text)
        self.assertIn('Research only', text)
        self.assertNotIn('Game-stat readiness: current', text)

    async def test_uploaded_research_shows_actual_more_less_without_counting_eligible(self):
        game = self.games[0]
        leg = {'player':'Jalen Brunson', 'team':'NYK', 'opponent':'CHI', 'game_id':game['id'],
               'date':game['date'], 'stat':'pts', 'line':25.5, 'side':'less'}
        board = self.store.create_board([leg], source='screenshot:123:current-test')
        with self.store.connect() as db:
            db.execute('UPDATE boards SET created_at=? WHERE id=?', (NOW.isoformat(),board['id']))
        research = {'player':'Jalen Brunson', 'team':'NYK', 'stat':'pts', 'line':25.5,
            'lean_side':'less', 'leg':leg, 'mean':24.5, 'median':24, 'n':20,
            'source_data_through_date':'2026-11-03', 'research_only':True,
            'eligible_for_ranking':False, 'research_caution':'Recent form is mixed.'}
        with patch('analysis_engine.analyze_board', return_value={'original_legs':[leg]}), \
             patch('board_shortlist.enrich_shortlist', return_value={'shortlist':[],
                 'research_picks':[research], 'research_candidates':[research]}), \
             patch('pregame_archive.record_research'):
            text = _uploaded_pick_scan(self.store, game, NOW, 123)
        self.assertIn('Research only: Jalen Brunson: Less 25.5 points', text)
        self.assertIn('0/4 eligible', text)
        self.assertIn('not eligible or approved entry picks', text)
        self.assertEqual(text.count('Jalen Brunson'), 1)
        self.assertEqual(self.store.list_entries(), [])


if __name__ == '__main__':
    unittest.main()
