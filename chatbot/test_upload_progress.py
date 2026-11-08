"""Photo work remains observable without exposing private uploads or replaying them."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master import MasterStore
from telegram_bot import TelegramBot
from test_screenshot_intake import photo, USER, DAY


class UploadProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name)/'master.sqlite')
        self.bot = TelegramBot(self.store, AsyncMock(), token='fake-test-token', allowed_user_ids=[USER])
        self.bot._typing = AsyncMock()
        self.bot._refresh_board_news = AsyncMock()
        self.bot._download = AsyncMock(return_value=b'private-image-bytes')
        self.bot._shortlist_reply = AsyncMock(return_value='Four historical comparisons.')
        self.leg = {'player':'Private Player Name', 'team':'NYK', 'stat':'pts',
                    'line':20.5, 'side':'more', 'date':DAY}
        patches = {
            'screenshot_intake.screenshot_date': ((DAY, 'caption'),),
            'screenshot_intake.roster_candidates': ([{'player':'Private Player Name'}],),
            'screenshot_board.extract_screenshot': ({'cards':[]},),
            'screenshot_intake.resolve_cards': (([self.leg], []),),
            'screenshot_intake.save_extraction': (None,),
            'screenshot_intake.combined_board': ({'board':{'id':'private-board-id'},
                'market_count':1, 'upload_count':1, 'unresolved':[]},),
            'analysis_engine.analyze_board': ({'original_legs':[self.leg]},),
        }
        for name, (value,) in patches.items():
            mock = patch(name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    async def test_one_durable_album_ack_and_read_only_private_progress(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def download(*args):
            entered.set()
            await release.wait()
            return b'private-image-bytes'
        self.bot._download.side_effect = download
        first = photo(101, 'album-private-unique')['message']
        second = photo(102, 'album-private-unique')['message']
        first['photo'][0]['file_id'] = 'file-private-unique'
        task = asyncio.create_task(self.bot._process_photos([(first,101), (second,102)]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            before = self.rows('telegram_outbox')
            report = self.bot.status()
            self.assertEqual(self.rows('telegram_outbox'), before)
            jobs = report['uploads']
            self.assertEqual(jobs['active_count'], 1)
            self.assertEqual(jobs['jobs'][0]['stage'], 'Reading screenshots 1/2')
            self.assertGreaterEqual(jobs['jobs'][0]['elapsed_seconds'], 0)
            self.assertIn('started_at', jobs['jobs'][0])
            serialized = json.dumps(report)
            for private in ('album-private-unique','file-private-unique','Private Player Name',
                            'private-image-bytes','private-board-id','chat_id'):
                self.assertNotIn(private, serialized)
            self.assertEqual(len(before), 1)
            self.assertIn('/status shows the current stage', before[0]['text'])
            release.set()
            await task
            finished = self.bot.status()['uploads']
            self.assertEqual(finished['active_count'], 0)
            self.assertEqual(finished['jobs'][0]['state'], 'completed')
            self.assertIn('finished_at', finished['jobs'][0])
            # A late fragment from the same album cannot queue another ACK.
            await self.bot._process_photos([(second,102)])
            self.assertEqual(len(self.rows('telegram_outbox')), 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_album_combines_images_once_and_duplicate_updates_do_not_restart_it(self):
        with patch.object(self.bot, '_process_photos', wraps=self.bot._process_photos) as process:
            await asyncio.gather(self.bot.process_update(photo(111, 'private-album')),
                                 self.bot.process_update(photo(112, 'private-album')))
            self.assertEqual(process.await_count, 1)
            await self.bot.process_update(photo(111, 'private-album'))
            await self.bot.process_update(photo(112, 'private-album'))
            self.assertEqual(process.await_count, 1)
        outputs = self.rows('telegram_outbox')
        self.assertEqual(sum(row['event_key'].startswith('photo-reading:') for row in outputs), 1)
        self.assertEqual(sum('Read 1 distinct lines' in row['text'] for row in outputs), 1)
        reopened = TelegramBot(self.store, AsyncMock(), token='fake-test-token', allowed_user_ids=[USER])
        with patch.object(reopened, '_process_photos', new=AsyncMock()) as process:
            await reopened.process_update(photo(111, 'private-album'))
            process.assert_not_awaited()

    async def test_separate_jobs_complete_and_terminal_history_is_bounded(self):
        for number in range(201, 211):
            await self.bot._process_photos([(photo(number)['message'], number)])
        result = self.bot.status()['uploads']
        self.assertEqual(result['active_count'], 0)
        self.assertEqual(len(result['jobs']), 8)
        self.assertTrue(all(job['state'] == 'completed' for job in result['jobs']))
        self.assertEqual(len(self.rows('telegram_outbox')), 10)

    async def test_unreadable_and_unexpected_errors_record_failed_cleanup(self):
        self.bot._download.side_effect = ValueError('private-download-detail')
        await self.bot._process_photos([(photo(301)['message'],301)])
        self.assertEqual(self.bot.status()['uploads']['jobs'][0]['state'], 'failed')
        self.bot._download.side_effect = KeyError('private-unexpected-detail')
        with self.assertRaises(KeyError):
            await self.bot._process_photos([(photo(302)['message'],302)])
        report = self.bot.status()['uploads']
        self.assertEqual(report['active_count'], 0)
        self.assertEqual(report['jobs'][0]['state'], 'failed')
        self.assertNotIn('private-unexpected-detail', json.dumps(report))

    async def test_cancellation_is_visible_and_releases_the_photo_lock(self):
        entered = asyncio.Event()
        async def download(*args):
            entered.set()
            await asyncio.Event().wait()
        self.bot._download.side_effect = download
        task = asyncio.create_task(self.bot._process_photos([(photo(401)['message'],401)]))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.bot._photo_locks[USER].locked())
        report = self.bot.status()['uploads']
        self.assertEqual(report['active_count'], 0)
        self.assertEqual(report['jobs'][0]['state'], 'canceled')

    async def test_upload_research_and_master_target_stages_are_separate(self):
        # Exercise the actual routing method instead of the upload transport fake.
        del self.bot._shortlist_reply
        phases = []
        def enrich(*args, **kwargs):
            phases.append(self.bot.status()['uploads']['jobs'][0]['phase'])
            return {'shortlist':[]}
        def targets(*args, **kwargs):
            phases.append(self.bot.status()['uploads']['jobs'][0]['phase'])
            return {}
        with patch('board_shortlist.enrich_shortlist', side_effect=enrich), \
             patch('board_shortlist.format_shortlist', return_value='Uploaded research'), \
             patch('board_shortlist.build_master_targets', side_effect=targets), \
             patch('independent_picks.format_independent_picks', return_value='Own targets'), \
             patch('pregame_archive.record_research'):
            await self.bot._process_photos([(photo(501)['message'],501)])
        self.assertEqual(phases, ['uploaded_research', 'master_targets'])

    async def test_direct_picks_enables_unverified_timing_research(self):
        with patch('independent_picks.build_independent_picks', return_value={}) as build, \
             patch('independent_picks.format_independent_picks', return_value='Research targets'), \
             patch('pregame_archive.record_research'):
            text = await self.bot._independent_picks_reply({'count':4}, USER)
        self.assertTrue(build.call_args.kwargs['allow_unverified_timing'])
        self.assertEqual(text, 'Research targets')


if __name__ == '__main__':
    unittest.main()
