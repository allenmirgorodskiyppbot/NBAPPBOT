"""Isolated screenshot-session, calendar, replay and Telegram integration checks."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master import MasterStore
from season_calendar import seed_calendar
from screenshot_intake import (combined_board, resolve_cards, roster_candidates,
                               save_extraction, screenshot_date)
from telegram_bot import TelegramBot, parse_board_text

NOW = dt.datetime(2026, 10, 30, 22, 40, tzinfo=dt.timezone.utc)
DAY = '2026-10-30'
USER = 123


def card(player='Jalen Brunson', team='NYK', line=24.5, stat='pts', sides=None):
    return {'player': player, 'team': team, 'line': line, 'stat': stat,
            'sides': sides or ['more', 'less'], 'confidence': 95}


def extraction(*cards, unresolved=None):
    return {'cards': list(cards), 'raw_text': 'SYNTHETIC RAW TEXT NOT FOR REPLIES',
            'unresolved': unresolved or [], 'reader_version': 'test-fixture'}


def photo(number, group=None, caption=''):
    message = {'from': {'id': USER}, 'chat': {'id': USER, 'type': 'private'},
               'date': NOW.timestamp(), 'caption': caption,
               'photo': [{'file_id': str(number), 'width': 1000, 'height': 1500, 'file_size': 100}]}
    if group:
        message['media_group_id'] = group
    return {'update_id': number, 'message': message}


class StoreFixture:
    def prepare(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)
        for player, team in [('Jalen Brunson', 'NYK'), ('Tyrese Maxey', 'PHI'), ('LeBron James', 'LAL')]:
            self.store.upsert_player_game({'game_id': 'fixture-'+team, 'date': '2026-10-28',
                'player': player, 'team': team, 'season': '2026-27', 'season_type': 'Regular Season',
                'pts': 20, 'minutes': 30, 'appearance_state': 'Played', 'source': 'isolated test fixture'})
        self.bot = TelegramBot(self.store, AsyncMock(), token='fake-test', allowed_user_ids=[USER])

    def save(self, number, result, now=NOW, basis='upload_day'):
        roster = roster_candidates(self.store, DAY)
        legs, issues = resolve_cards(self.store, result, DAY, roster)
        save_extraction(self.store, USER, number, DAY, basis, result, legs, issues, now)
        return legs, issues


class ScreenshotSessionTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        self.prepare()

    def test_upload_day_uses_eastern_and_explicit_caption_wins(self):
        message = {'date': dt.datetime(2026, 10, 31, 1, tzinfo=dt.timezone.utc).timestamp()}
        self.assertEqual(screenshot_date(message, NOW), (DAY, 'upload_day'))
        message['caption'] = '2026-11-01'
        self.assertEqual(screenshot_date(message, NOW), ('2026-11-01', 'caption'))
        message['caption'] = '2026-10-30 or 2026-10-31'
        with self.assertRaises(ValueError):
            screenshot_date(message, NOW)

    def test_player_team_and_target_event_never_default_to_knicks(self):
        legs, issues = self.save(1, extraction(card('Tyrese Maxey', 'PHI'), card('LeBron James', 'LAL')))
        self.assertEqual({leg['team'] for leg in legs}, {'PHI'})
        self.assertEqual({leg['opponent'] for leg in legs}, {'NYK'})
        self.assertTrue(all(leg['game_id'] == 'nba:2026-10-30:NYK:PHI' for leg in legs))
        self.assertIn('LeBron James', ' '.join(issues))

    def test_inconsistent_team_unsupported_stat_and_unavailable_sides_stay_out(self):
        result = extraction(card('Jalen Brunson', 'PHI'), card(stat='two_point_attempts'))
        self.assertEqual(self.save(2, result)[0], [])
        value = card(); value['sides'] = []
        self.assertEqual(self.save(3, extraction(value))[0], [])

    def test_two_visible_sides_are_alternatives_and_never_entries(self):
        before = self.store.status()['counts']['player_games']
        self.save(4, extraction(card()))
        bundle = combined_board(self.store, USER, DAY, NOW)
        self.assertEqual(len(bundle['board']['legs']), 2)
        self.assertEqual(bundle['market_count'], 1)
        self.assertTrue(bundle['date_assumed'])
        self.assertEqual(self.store.list_entries(), [])
        self.assertEqual(self.store.status()['counts']['player_games'], before)

    def test_overlapping_cards_deduplicate_but_alternate_lines_are_preserved(self):
        self.save(5, extraction(card()))
        self.save(6, extraction(card(), card(line=29.5)))
        bundle = combined_board(self.store, USER, DAY, NOW)
        self.assertEqual(bundle['market_count'], 2)
        self.assertEqual(len(bundle['board']['legs']), 4)
        self.assertEqual({leg['line'] for leg in bundle['board']['legs']}, {24.5, 29.5})

    def test_restart_and_duplicate_receipt_reuse_the_same_immutable_board(self):
        self.save(7, extraction(card()))
        first = combined_board(self.store, USER, DAY, NOW)
        self.save(7, extraction(card(line=999)))  # ignored duplicate source update
        reopened = MasterStore(self.store.path)
        second = combined_board(reopened, USER, DAY, NOW)
        self.assertEqual(first['board']['id'], second['board']['id'])
        self.assertEqual(second['board']['legs'][0]['line'], 24.5)
        self.assertEqual(len(self.store.boards()), 1)

    def test_expiry_reset_and_chat_scope_do_not_reuse_other_uploads(self):
        self.save(8, extraction(card()), now=NOW-dt.timedelta(minutes=31))
        self.assertIsNone(combined_board(self.store, USER, DAY, NOW)['board'])
        self.save(9, extraction(card()), now=NOW-dt.timedelta(minutes=1))
        self.assertIsNotNone(combined_board(self.store, USER, DAY, NOW)['board'])
        self.assertIsNone(combined_board(self.store, USER, DAY, NOW, reset_at=NOW.isoformat())['board'])
        self.assertIsNone(combined_board(self.store, USER+1, DAY, NOW)['board'])

    def test_existing_offensive_rebounds_can_be_read_and_graded_without_new_stats(self):
        legs = parse_board_text(DAY+'\nJalen Brunson | offensive rebounds | more | 1.5 | NYK')
        board = self.store.create_board(legs)
        self.assertEqual(board['legs'][0]['stat'], 'oreb')
        self.assertEqual(self.store.status()['counts']['player_games'], 3)

    def test_reset_update_excludes_an_older_photo_that_finishes_after_reset(self):
        self.save(20, extraction(card()), now=NOW+dt.timedelta(seconds=20))
        self.assertIsNone(combined_board(self.store, USER, DAY, NOW+dt.timedelta(seconds=30),
                                        reset_at=NOW.isoformat(), reset_update=21)['board'])
        self.save(22, extraction(card('Tyrese Maxey', 'PHI')), now=NOW+dt.timedelta(seconds=25))
        result = combined_board(self.store, USER, DAY, NOW+dt.timedelta(seconds=30),
                                reset_at=NOW.isoformat(), reset_update=21)
        self.assertEqual({leg['player'] for leg in result['board']['legs']}, {'Tyrese Maxey'})


class ScreenshotTelegramTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prepare()
        async def download(identity, size):
            return identity.encode()
        self.bot._download = download
        self.bot._typing = AsyncMock()
        self.bot._shortlist_reply = AsyncMock(return_value='My shortlist\nSynthetic test response.')
        self.bot.research_refresh_callback = AsyncMock()

    def output(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM telegram_outbox ORDER BY id')]

    async def test_album_one_reply_combines_cards_and_inherits_caption(self):
        def reader(payload, roster):
            return extraction(card() if payload == b'10' else card('Tyrese Maxey', 'PHI', line=27.5))
        with patch('screenshot_board.extract_screenshot', side_effect=reader), \
             patch('analysis_engine.analyze_board', return_value={'original_legs': []}):
            await asyncio.gather(self.bot.process_update(photo(10, 'album', DAY)),
                                 self.bot.process_update(photo(11, 'album')))
        output = self.output()
        self.assertEqual(len(output), 1)
        self.assertIn('2 distinct lines', output[0]['text'])
        self.assertNotIn('Assumed game date', output[0]['text'])
        self.assertNotIn('SYNTHETIC RAW TEXT', output[0]['text'])
        self.assertEqual(len(self.store.boards()[0]['legs']), 4)
        self.assertEqual(self.store.list_entries(), [])
        self.bot.research_refresh_callback.assert_awaited_once()
        self.assertFalse(await self.bot.process_update(photo(10, 'album', DAY)))
        self.assertEqual(len(self.output()), 1)

    async def test_parallel_separate_uploads_serialize_cumulative_board_writes(self):
        def reader(payload, roster):
            return extraction(card() if payload == b'12' else card('Tyrese Maxey', 'PHI'))
        with patch('screenshot_board.extract_screenshot', side_effect=reader), \
             patch('analysis_engine.analyze_board', return_value={'original_legs': []}):
            await asyncio.gather(self.bot.process_update(photo(12, caption=DAY)),
                                 self.bot.process_update(photo(13, caption=DAY)))
        self.assertEqual(max(len(b['legs']) for b in self.store.boards()), 4)
        self.assertEqual(len(self.output()), 2)

    async def test_bad_image_is_concise_and_does_not_stop_readable_album_cards(self):
        def reader(payload, roster):
            if payload == b'14':
                raise ValueError('image corrupt')
            return extraction(card(), unresolved=['Unclear second card'])
        with patch('screenshot_board.extract_screenshot', side_effect=reader), \
             patch('analysis_engine.analyze_board', return_value={'original_legs': []}):
            await asyncio.gather(self.bot.process_update(photo(14, 'bad-good', DAY)),
                                 self.bot.process_update(photo(15, 'bad-good')))
        self.assertEqual(len(self.store.boards()), 1)
        self.assertIn('Unclear second card', self.output()[0]['text'])

    async def test_unreadable_photo_returns_actionable_short_feedback_without_ocr_dump(self):
        with patch('screenshot_board.extract_screenshot', return_value=extraction(unresolved=['Unsupported first-quarter market'])):
            await self.bot.process_update(photo(16, caption=DAY))
        self.assertEqual(self.store.boards(), [])
        text = self.output()[0]['text']
        self.assertIn('first-quarter', text)
        self.assertNotIn('SYNTHETIC RAW TEXT', text)
        self.assertLess(len(text), 900)

    async def test_original_image_document_routes_through_same_reader(self):
        value = photo(17, caption=DAY)
        value['message'].pop('photo')
        value['message']['document'] = {'file_id': '17', 'mime_type': 'image/png', 'file_name': 'card.png'}
        with patch('screenshot_board.extract_screenshot', return_value=extraction(card())), \
             patch('analysis_engine.analyze_board', return_value={'original_legs': []}):
            await self.bot.process_update(value)
        self.assertEqual(len(self.store.boards()), 1)

    async def test_research_board_cannot_record_all_image_alternatives_as_placed(self):
        self.save(18, extraction(card()))
        board = combined_board(self.store, USER, DAY, NOW)['board']
        message = photo(19)['message']; message.pop('photo')
        message['text'] = '/track '+board['id']+' 10 power 3'
        await self.bot.process_update({'update_id': 19, 'message': message})
        self.assertEqual(self.store.list_entries(), [])
        self.assertIn('exact selections', self.output()[0]['text'])


if __name__ == '__main__':
    unittest.main()
