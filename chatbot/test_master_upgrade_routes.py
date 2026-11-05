"""Integration boundaries for additive workbook uploads and archived research."""
import asyncio
import datetime as dt
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import openpyxl
from starlette.requests import ClientDisconnect

import app
import bootstrap
from master import GAME_FIELDS, PLAYER_FIELDS, MasterStore
from pregame_archive import get_research
from telegram_bot import MAX_MASTER_UPLOAD, TelegramBot

PASSWORD = 'offline-test-password-with-no-real-account'
HEADERS = {'X-Chat-Password': PASSWORD, 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}


def workbook_bytes(*, points=21):
    game = {'id': 'nba:2026-11-01:CHI:NYK', 'date': '2026-11-01', 'status': 'final',
            'home_team': 'NYK', 'away_team': 'CHI', 'source': 'dated published fixture',
            'updated_at': '2026-11-02T11:00:00+00:00'}
    player = {'game_id': game['id'], 'player_id': 'nba:1628973', 'player': 'Jalen Brunson',
              'team': 'NYK', 'opponent': 'CHI', 'date': game['date'], 'season': '2026-27',
              'season_type': 'Regular Season', 'appearance_state': 'Played',
              'pts': points, 'reb': 4, 'ast': 8, 'minutes': 35,
              'source': 'dated published fixture', 'updated_at': game['updated_at']}
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, columns, row in [('games', GAME_FIELDS + ['updated_at'], game),
                               ('player_games', PLAYER_FIELDS + ['updated_at'], player)]:
        sheet = book.create_sheet(name)
        sheet.append(columns)
        sheet.append([row.get(key) for key in columns])
    data = BytesIO()
    book.save(data)
    return data.getvalue(), game, player


class StreamRequest:
    def __init__(self, chunks=(), error=None):
        self.chunks, self.error = chunks, error

    async def stream(self):
        for value in self.chunks:
            yield value
        if self.error:
            raise self.error


class MasterUpgradeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.patchers = [patch.object(app, 'DATA_DIR', self.root),
                         patch.object(app, 'CHAT_PASSWORD', PASSWORD),
                         patch.object(app, '_MASTER_IMPORT_LOCK', asyncio.Lock()),
                         patch.object(app.app.state, 'master', self.store, create=True),
                         patch.object(app.app.state, 'data_ready', True, create=True)]
        for patcher in self.patchers:
            patcher.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test')
        self.bot = TelegramBot(self.store, AsyncMock(return_value='unused'),
                               token='123:OFFLINE_TEST_TOKEN', allowed_user_ids=[100])

    async def asyncTearDown(self):
        await self.client.aclose()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.directory.cleanup()

    def document(self, caption='/importmaster', filename='NBA_Master_updated.xlsx'):
        return {'message_id': 1, 'chat': {'id': 100, 'type': 'private'},
                'from': {'id': 100}, 'caption': caption,
                'document': {'file_id': 'fixture-file', 'file_name': filename,
                             'file_size': 100, 'mime_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}}

    def imports_clean(self):
        self.assertFalse(list((self.root / 'imports').glob('*')))
        self.assertFalse(app._MASTER_IMPORT_LOCK.locked())

    async def test_web_requires_auth_and_does_not_consume_unauthorized_body(self):
        consumed = []
        async def body():
            consumed.append(True)
            yield b'not-yet-authorized'
        response = await self.client.post('/api/import/master', content=body())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(consumed, [])
        self.assertEqual(self.store.status()['counts']['player_games'], 0)
        self.imports_clean()

    async def test_web_additive_merge_preserves_ledger_chat_current_checks_and_conflicting_rows(self):
        data, game, player = workbook_bytes(points=99)
        self.store.upsert_game(game)
        self.store.upsert_player_game({**player, 'pts': 30, 'updated_at': '2026-11-04T22:00:00+00:00'})
        legs = [{'player': 'Jalen Brunson', 'team': 'NYK', 'date': '2026-11-01',
                 'stat': 'pts', 'line': 20.5, 'side': 'more'},
                {'player': 'Josh Giddey', 'team': 'CHI', 'date': '2026-11-01',
                 'stat': 'ast', 'line': 7.5, 'side': 'less'}]
        entry = self.store.create_entry(legs, 10, 'power', 3)
        self.store.mark_settlement(entry['id'], 30, 'User confirmed')
        self.store.set_checkpoint('game_schedule_check', {'success': False, 'error': 'HTTP403'})
        self.bot._set_state('fixture_chat_preference', 'Keep both teams')
        response = await self.client.post('/api/import/master', headers=HEADERS, content=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['player_games_preserved'], 1)
        self.assertEqual(self.store.query('SELECT pts FROM player_games')['rows'], [(30,)])
        self.assertEqual(self.store.get_entry(entry['id'])['actual_payout'], 30)
        self.assertEqual(self.store.get_checkpoint('game_schedule_check')['error'], 'HTTP403')
        self.assertEqual(self.bot._get_state('fixture_chat_preference'), 'Keep both teams')
        self.assertTrue(Path(response.json()['backup']).is_file())
        again = await self.client.post('/api/import/master', headers=HEADERS, content=data)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()['skipped'])
        self.assertEqual(len(list((self.root / 'backups').glob('before-master-import-*'))), 1)
        self.imports_clean()

    async def test_malformed_upload_returns_validation_error_without_partial_data(self):
        before = self.store.status()['counts']
        response = await self.client.post('/api/import/master', headers=HEADERS, content=b'not an xlsx')
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.status()['counts'], before)
        self.assertFalse(list((self.root / 'backups').glob('*')))
        self.imports_clean()

    async def test_stream_limit_enforced_on_bytes_not_declared_length(self):
        async def body():
            yield b'12345678'
            yield b'123456789'
        with patch.object(app, 'MAX_MASTER_BYTES', 16):
            response = await self.client.post('/api/import/master', headers=HEADERS, content=body())
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(self.store.status()['counts']['games'], 0)
        self.imports_clean()

    async def test_disconnect_during_upload_discards_file_and_releases_lock(self):
        with self.assertRaises(ClientDisconnect):
            await app.import_master_workbook(StreamRequest([b'PK-partial'], ClientDisconnect()))
        self.imports_clean()
        self.assertEqual(self.store.status()['counts']['games'], 0)

    async def test_second_import_gets_conflict_without_body_read(self):
        await app._MASTER_IMPORT_LOCK.acquire()
        consumed = []
        async def body():
            consumed.append(True)
            yield b'x'
        try:
            response = await self.client.post('/api/import/master', headers=HEADERS, content=body())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(consumed, [])
        finally:
            app._MASTER_IMPORT_LOCK.release()

    async def test_cancelled_response_keeps_merge_locked_until_worker_finishes(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked_merge(store, path):
            self.assertTrue(Path(path).is_file())
            started.set()
            if not release.wait(3):
                raise AssertionError('Test worker was not released')
            finished.set()
            return {'imported': True}
        with patch('master_upgrade.merge_enriched_workbook', side_effect=blocked_merge):
            task = asyncio.create_task(app.import_master_workbook(StreamRequest([b'fixture'])))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(app._MASTER_IMPORT_LOCK.locked())
                self.assertTrue(list((self.root / 'imports').glob('*')))
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                for _ in range(20):
                    if not app._MASTER_IMPORT_LOCK.locked():
                        break
                    await asyncio.sleep(.01)
        self.imports_clean()

    async def test_telegram_explicit_caption_merges_xlsx_not_a_board(self):
        data, _, _ = workbook_bytes()
        download = AsyncMock(return_value=data)
        from master_upgrade import merge_enriched_workbook
        def inspect_receipt_then_merge(*args, **kwargs):
            with self.store.connect() as db:
                receipt = db.execute('SELECT event_key,text FROM telegram_outbox').fetchone()
            self.assertEqual(receipt['event_key'], 'master-import-start:1:100:0')
            self.assertIn('Validating and merging', receipt['text'])
            self.assertEqual(self.store.status()['counts']['player_games'], 0)
            return merge_enriched_workbook(*args, **kwargs)
        with patch.object(self.bot, '_download', download), \
             patch('master_upgrade.merge_enriched_workbook', side_effect=inspect_receipt_then_merge):
            reply = await self.bot._handle_message(self.document('/importmaster@MyBot'), 1)
        download.assert_awaited_once_with('fixture-file', 100, max_bytes=MAX_MASTER_UPLOAD)
        self.assertIn('Master merge complete', reply)
        self.assertEqual(self.store.status()['counts']['player_games'], 1)
        self.assertEqual(self.store.boards(), [])
        self.assertEqual(self.store.list_entries(), [])

    async def test_telegram_duplicate_master_reports_noop_without_claiming_new_backup(self):
        data, _, _ = workbook_bytes()
        with patch.object(self.bot, '_download', AsyncMock(return_value=data)):
            first = await self.bot._handle_message(self.document(), 40)
            second = await self.bot._handle_message(self.document(), 41)
            replay = await self.bot._handle_message(self.document(), 41)
        self.assertIn('Master merge complete', first)
        self.assertIn('already imported', second)
        self.assertIn('No changes', second)
        self.assertNotIn('backup', second.lower())
        self.assertEqual(second, replay)
        self.assertEqual(self.store.status()['counts']['player_games'], 1)
        self.assertEqual(len(list((self.root / 'backups').glob('before-master-import-*'))), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_outbox').fetchone()[0], 2)
        self.assertEqual(self.store.list_entries(), [])

    async def test_ordinary_or_ambiguous_document_never_merges_the_master(self):
        for caption, filename in [('', 'NBA_Master.xlsx'), ('/importmaster extra', 'NBA_Master.xlsx'),
                                  ('/importmaster', 'board.csv')]:
            with self.subTest(caption=caption, filename=filename), patch.object(self.bot, '_download', AsyncMock()) as download:
                with self.assertRaises(ValueError):
                    await self.bot._handle_message(self.document(caption, filename), 2)
                download.assert_not_awaited()
        self.assertEqual(self.store.status()['counts']['player_games'], 0)

    async def test_strategy_and_help_explain_strict_baseline_and_import(self):
        reply = await self.bot._handle_message({'chat': {'id': 100}, 'text': '/strategy'}, 3)
        self.assertIn('20', reply)
        self.assertIn('arithmetic', reply)
        self.assertIn('Pass', reply)
        self.assertIn('did not establish', reply)
        help_text = await self.bot._handle_message({'chat': {'id': 100}, 'text': '/help'}, 4)
        self.assertIn('/strategy', help_text)
        self.assertIn('/importmaster', help_text)
        for module in ('master_upgrade.py', 'pregame_archive.py', 'strategy_info.py'):
            self.assertIn(module, bootstrap.APP_FILES)

    async def test_independent_reply_archives_every_selected_and_pass_without_offered_lines(self):
        report = {'generated_at': '2026-11-05T22:00:00+00:00', 'picks': [],
                  'evaluated_candidates': [
                      {'player': 'Jalen Brunson', 'stat': 'pts', 'status': 'selected',
                       'projection': {'available': True, 'method': 'same-team-mean20-v1', 'reference_projection': 27}},
                      {'player': 'Josh Giddey', 'stat': 'ast', 'status': 'pass',
                       'reason': 'Fewer than 20 comparable observations'}]}
        with patch.object(self.bot, '_refresh_board_news', AsyncMock()), \
             patch.object(self.bot, '_pick_constraints', return_value={}), \
             patch.object(self.bot, '_typing', AsyncMock()), \
             patch('independent_picks.build_independent_picks', return_value=report), \
             patch('independent_picks.format_independent_picks', return_value='Research reply'):
            reply = await self.bot._independent_picks_reply({'date': '2026-11-05'}, 100)
        self.assertEqual(reply, 'Research reply')
        with self.store.connect() as db:
            run = db.execute('SELECT id,owner_key FROM research_runs').fetchone()
        archived = get_research(self.store, run['id'], owner_key='telegram:100')
        self.assertEqual([r['disposition'] for r in archived['candidates']], ['selected', 'pass'])
        self.assertTrue(all(r['line'] is None for r in archived['candidates']))

    async def test_web_analysis_creates_scoped_archive_and_keeps_all_legs(self):
        legs = [{'player': 'Jalen Brunson', 'team': 'NYK', 'date': '2026-11-05',
                 'stat': 'pts', 'line': 25.5, 'side': 'more'},
                {'player': 'Josh Giddey', 'team': 'CHI', 'date': '2026-11-05',
                 'stat': 'ast', 'line': 7.5, 'side': 'more'}]
        board = self.store.create_board(legs)
        report = {'board_id': board['id'], 'generated_at': '2026-11-05T22:00:00+00:00',
                  'legs': [{'leg': legs[0], 'eligible_for_ranking': True,
                            'projection': {'available': True, 'method': 'same-team-mean20-v1', 'reference_projection': 27}},
                           {'leg': legs[1], 'eligible_for_ranking': False, 'reason': 'Unavailable'}]}
        with patch.object(app, 'analyze_board', return_value=report):
            response = await self.client.post('/api/boards/' + board['id'] + '/analyze', headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        with self.store.connect() as db:
            run = db.execute('SELECT id FROM research_runs').fetchone()
        archived = get_research(self.store, run['id'], owner_key='web:authenticated')
        self.assertEqual([r['disposition'] for r in archived['candidates']], ['candidate', 'pass'])
        self.assertEqual(self.store.list_entries(), [])

    async def test_shortlist_reply_archives_final_pass_decision_and_unconfirmed_offer(self):
        leg = {'player': 'Jalen Brunson', 'team': 'NYK', 'date': '2026-11-05',
               'stat': 'pts', 'line': 25.5, 'side': 'more',
               'offer_variant': 'standard', 'screenshot_unconfirmed': True}
        board = self.store.create_board([leg], source='screenshot:100:fixture')
        source = {'board_id': board['id'], 'original_legs': [leg],
                  'legs': [{'leg': leg, 'eligible_for_ranking': True,
                            'projection': {'available': True, 'method': 'same-team-mean20-v1',
                                           'reference_projection': 26}}]}
        shortlist = {'board_id': board['id'], 'shortlist': [],
                     'skipped': [{**leg, 'reason': 'Current game schedule could not be verified'}]}
        with patch.object(self.bot, '_pick_constraints', return_value={}), \
             patch('board_shortlist.enrich_shortlist', return_value=shortlist), \
             patch('board_shortlist.format_shortlist', return_value='No supported picks'):
            reply = await self.bot._shortlist_reply(source, 100)
        self.assertEqual(reply, 'No supported picks')
        with self.store.connect() as db:
            run = db.execute('SELECT id FROM research_runs').fetchone()
        archived = get_research(self.store, run['id'], owner_key='telegram:100')
        self.assertEqual(archived['candidates'][0]['disposition'], 'pass')
        self.assertIn('schedule', archived['candidates'][0]['reason'])
        self.assertTrue(archived['payload']['board_snapshot']['legs'][0]['screenshot_unconfirmed'])

    async def test_unauthorized_telegram_upload_never_downloads_or_merges(self):
        message = self.document()
        message['from']['id'] = message['chat']['id'] = 999
        with patch.object(self.bot, '_download', AsyncMock()) as download:
            result = await self.bot.process_update({'update_id': 99, 'message': message})
        self.assertFalse(result)
        download.assert_not_awaited()
        self.assertEqual(self.store.status()['counts']['player_games'], 0)


if __name__ == '__main__':
    unittest.main()
