"""A next-matchup master lookup starts no source/assignment refresh work."""
import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from feeds import canonical_game, canonical_player
from master import MasterStore
from telegram_bot import TelegramBot

NOW = dt.datetime(2026, 11, 8, 18, tzinfo=dt.timezone.utc)
TARGET = '2026-11-10'
SOURCE = 'https://example.invalid/SYNTHETIC-cached-master-control'


class PicksCachedMasterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        self.source_entered = asyncio.Event()
        self.assignment_entered = threading.Event()

        async def blocked_source():
            self.source_entered.set()
            await asyncio.Event().wait()

        self.refresh = AsyncMock(side_effect=blocked_source)
        self.bot = TelegramBot(self.store, AsyncMock(), token='synthetic-token', allowed_user_ids=[123],
                               client=object(), research_refresh_callback=self.refresh)
        self.bot._typing = AsyncMock()
        self.addAsyncCleanup(self.bot.shutdown)
        for name, team, opponent in [('Jalen Brunson', 'NYK', 'BOS'),
                                      ('Karl-Anthony Towns', 'NYK', 'BOS'),
                                      ('AJ Green', 'MIL', 'CHI'),
                                      ('Giannis Antetokounmpo', 'MIL', 'CHI')]:
            for offset in range(20):
                day = (dt.date(2026, 11, 7) - dt.timedelta(days=offset)).isoformat()
                identity = canonical_game(day, team, opponent)
                self.store.upsert_game({'id': identity, 'date': day, 'home_team': team,
                    'away_team': opponent, 'status': 'final', 'source': SOURCE})
                self.store.upsert_player_game({'game_id': identity, 'player_id': canonical_player(name),
                    'player': name, 'team': team, 'opponent': opponent, 'date': day, 'season': '2026-27',
                    'season_type': 'Regular Season', 'appearance_state': 'Played', 'minutes': 32,
                    'pts': 20 + offset % 4, 'reb': 5 + offset % 3, 'ast': 4 + offset % 2,
                    'fga': 17 + offset % 3, 'source': SOURCE})
        self.store.upsert_game({'id': canonical_game(TARGET, 'NYK', 'MIL'), 'date': TARGET,
            'home_team': 'NYK', 'away_team': 'MIL', 'status': 'scheduled',
            'start_time': '2026-11-11T00:30:00+00:00', 'source': SOURCE})

    def source_state(self):
        with self.store.connect() as db:
            values = {table: [list(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2')]
                      for table in ('games', 'player_games', 'checkpoints', 'entries', 'news')}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    async def reply(self):
        with patch('independent_picks._now', return_value=NOW), \
                patch('assignment_refresh.refresh_assignments', side_effect=lambda *a, **k: self.assignment_entered.set()):
            return await self.bot._independent_picks_reply({'date': None, 'count': 4, 'team': 'NYK'}, 123)

    async def test_blocked_source_callback_is_never_started_before_or_after_cached_reply(self):
        before = self.source_state()
        tasks = {id(task) for task in asyncio.all_tasks()}
        answer = await asyncio.wait_for(self.reply(), 2)
        await asyncio.sleep(0)
        self.assertIn('NYK vs MIL', answer)
        self.assertIn('Nov 10', answer)
        self.assertIn('2026-11-07', answer)
        self.refresh.assert_not_awaited()
        self.assertFalse(self.source_entered.is_set())
        self.assertFalse(self.assignment_entered.is_set())
        self.assertEqual(self.source_state(), before)
        self.assertEqual({id(task) for task in asyncio.all_tasks()}, tasks)
        self.assertEqual(self.store.list_entries(), [])

    async def test_consecutive_master_lookups_do_not_create_background_jobs_or_claim_source_freshness(self):
        before = self.source_state()
        first = await asyncio.wait_for(self.reply(), 2)
        second = await asyncio.wait_for(self.reply(), 2)
        self.assertEqual(first, second)
        self.refresh.assert_not_awaited()
        self.assertFalse(self.source_entered.is_set())
        self.assertFalse(self.assignment_entered.is_set())
        self.assertEqual(self.source_state(), before)
        self.assertFalse(hasattr(self.bot, '_research_refresh_task'))

    async def test_uploaded_board_refresh_still_uses_the_source_callback(self):
        # Cached /picks is the only bypass. Explicit uploaded-board processing
        # retains its awaited source-refresh contract.
        self.bot.research_refresh_callback = AsyncMock()
        with patch('assignment_refresh.refresh_assignments', return_value=None) as assignments:
            await asyncio.wait_for(self.bot._refresh_board_news(), 1)
        self.bot.research_refresh_callback.assert_awaited_once()
        assignments.assert_called_once()


if __name__ == '__main__':
    unittest.main()
