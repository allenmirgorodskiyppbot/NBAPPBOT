import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from feeds import (FeedWorker, canonical_game, canonical_player, canonical_team,
                   parse_boxscore, parse_game, parse_rss)


NOW = dt.datetime(2026, 10, 29, 18, 0, tzinfo=dt.timezone.utc)


def event(day='2026-10-29', final=True):
    return {'id': '1234', 'date': day + 'T23:00Z',
            'status': {'type': {'completed': final, 'state': 'post' if final else 'in',
                                'name': 'STATUS_FINAL' if final else 'STATUS_IN_PROGRESS'}},
            'competitions': [{'competitors': [
                {'homeAway': 'home', 'team': {'abbreviation': 'NY'}, 'score': '101'},
                {'homeAway': 'away', 'team': {'abbreviation': 'BOS'}, 'score': '99'}]}]}


def summary(points='22'):
    return {'header': {'season': {'year': 2027, 'type': 2}},
            'boxscore': {'players': [{'team': {'abbreviation': 'NY'}, 'statistics': [{
                'names': ['PTS', 'MIN', 'FG', '3PT', 'REB', 'AST', 'TO', 'STL', 'BLK'],
                'athletes': [{'athlete': {'id': '11', 'displayName': 'Jalen Brunson'},
                              'stats': [points, '35:30', '8-20', '3-8', '4', '5', '2', '1', '0'],
                              'didNotPlay': False, 'starter': True},
                             {'athlete': {'id': '12', 'displayName': 'Miles McBride'},
                              'stats': [], 'didNotPlay': True}]}]}]},
            'injuries': [{'team': {'abbreviation': 'BOS'}, 'injuries': [{
                'athlete': {'displayName': 'Derrick White'}, 'status': 'Questionable',
                'date': '2026-10-29T17:00Z', 'shortComment': 'Ankle injury'}]}]}


class MemoryStore:
    def __init__(self):
        self.checkpoints = {'last_reconciled_date': '2026-10-29', 'feeds_initialized': True}
        self.game_rows, self.player_rows, self.news, self.feeds = {}, {}, {}, {}
        self.graded = 0

    def get_checkpoint(self, name, default=None):
        return self.checkpoints.get(name, default)

    def set_checkpoint(self, name, value):
        self.checkpoints[name] = value

    def upsert_game(self, game):
        self.game_rows[game['id']] = game

    def upsert_player_game(self, row):
        self.player_rows[(row['game_id'], row['player_id'])] = row

    def record_news(self, item):
        changed = self.news.get(item['id']) != item
        self.news[item['id']] = item
        return changed

    def feed_success(self, name, detail=''):
        self.feeds[name] = {'success': True, 'detail': detail}

    def feed_failure(self, name, error):
        self.feeds[name] = {'success': False, 'error': error}

    def games(self, date=None):
        return [row for row in self.game_rows.values() if not date or row['date'] == date]

    def grade_entries(self):
        self.graded += 1

    def list_entries(self):
        return [{'settled': False, 'legs': [{'player': 'Derrick White'}]}]


class ParsingTests(unittest.TestCase):
    def test_pre_tip_fast_poll_survives_master_database_round_trip(self):
        from master import MasterStore
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'master.sqlite')
            store = MasterStore(path)
            tip = (NOW + dt.timedelta(minutes=30)).isoformat()
            payload = event()
            payload['date'] = tip
            payload['status']['type'] = {'completed': False, 'state': 'pre', 'name': 'STATUS_SCHEDULED'}
            store.upsert_game(parse_game(payload))
            # Reopen the actual durable database instead of retaining an in-memory dict.
            reopened = MasterStore(path)
            worker = FeedWorker(reopened, now=lambda: NOW)
            self.assertEqual(reopened.games('2026-10-29')[0].get('start_time'), tip)
            self.assertEqual(worker.next_delay(), 60)

    def test_source_ids_join_across_provider_and_excel_conventions(self):
        self.assertEqual(canonical_team('NY'), 'NYK')
        self.assertEqual(canonical_player('José Alvarado'), 'name:josealvarado')
        self.assertEqual(canonical_game('2026-10-29', 'NY', 'BOS'),
                         canonical_game('2026-10-29', 'BOS', 'NYK'))

    def test_game_date_uses_eastern_and_final_not_numeric_score_only(self):
        game = event()
        game['date'] = '2026-10-30T02:00Z'
        parsed = parse_game(game)
        self.assertEqual(parsed['date'], '2026-10-29')
        self.assertEqual(parsed['id'], 'nba:2026-10-29:BOS:NYK')
        self.assertEqual(parsed['status'], 'final')
        self.assertEqual(parsed['home_score'], 101)
        game['status']['type'] = {'name': 'STATUS_SCHEDULED', 'state': 'pre', 'completed': False}
        self.assertIsNone(parse_game(game)['home_score'])

    def test_reordered_stat_names_and_missing_dnp_values(self):
        rows = parse_boxscore(summary(), parse_game(event()))
        self.assertEqual(rows[0]['pts'], 22)
        self.assertEqual(rows[0]['minutes'], 35.5)
        self.assertEqual(rows[0]['fga'], 20)
        self.assertEqual(rows[0]['tpm'], 3)
        self.assertEqual(rows[0]['tpa'], 8)
        self.assertEqual(rows[0]['season'], '2026-27')
        self.assertEqual(rows[1]['appearance_state'], 'DNP')
        self.assertIsNone(rows[1]['pts'])
        self.assertIsNone(rows[1]['minutes'])

    def test_rss_provenance_and_entity_rejection(self):
        content = b'<rss><channel><item><guid>one</guid><title>Knicks injury update</title><link>https://example.org/one</link><description>Player out</description></item></channel></rss>'
        item = parse_rss(content, 'example.org')[0]
        self.assertEqual(item['source'], 'example.org')
        self.assertEqual(item['url'], 'https://example.org/one')
        with self.assertRaises(ValueError):
            parse_rss(b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>', 'test')


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.calls, self.notices = [], []
        self.points = '22'
        self.fail_news = False

        def route(request):
            self.calls.append(str(request.url))
            if request.url.path.endswith('scoreboard'):
                day = request.url.params['dates']
                date = day[:4] + '-' + day[4:6] + '-' + day[6:8]
                return httpx.Response(200, json={'events': [event(date)]})
            if request.url.path.endswith('summary'):
                return httpx.Response(200, json=summary(self.points))
            if self.fail_news:
                return httpx.Response(503)
            return httpx.Response(200, json={'articles': [{
                'id': 'n1', 'headline': 'Derrick White questionable with ankle injury',
                'description': 'Boston injury report update.', 'published': '2026-10-29T17:00Z',
                'links': {'web': {'href': 'https://www.espn.com/example'}}}]})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(route))
        async def notice(text, key):
            self.notices.append((text, key))
        with patch.dict(os.environ, {'NEWS_FEED_URLS': '', 'INJURY_FEED_URL': '', 'FEED_BACKFILL_DAYS': '1'}):
            self.worker = FeedWorker(self.store, notify=notice, client=self.client, now=lambda: NOW)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_collection_is_idempotent_and_reconciles_stat_corrections(self):
        await self.worker.collect_once()
        self.assertTrue(self.store.feeds['espn_games']['success'])
        self.assertEqual(len(self.store.player_rows), 2)
        self.assertGreater(len(self.notices), 0)
        notices = len(self.notices)
        self.points = '25'
        await self.worker.collect_once()
        self.assertEqual(len(self.store.player_rows), 2)
        played = next(row for row in self.store.player_rows.values() if row['player'] == 'Jalen Brunson')
        self.assertEqual(played['pts'], 25)
        self.assertEqual(len(self.notices), notices)
        self.assertFalse(self.store.feeds['injuries_configured']['success'])
        self.assertIn('Disabled', self.store.feeds['injuries_configured']['error'])

    async def test_news_failure_does_not_hide_successful_game_updates(self):
        self.fail_news = True
        await self.worker.collect_once()
        self.assertTrue(self.store.feeds['espn_games']['success'])
        self.assertFalse(self.store.feeds['espn_news']['success'])
        self.assertIn('HTTP 503', self.store.feeds['espn_news']['error'])

    async def test_historical_summary_cannot_overwrite_current_injury_status(self):
        await self.worker.update_games('2026-10-28')
        self.assertEqual(self.store.news, {})

    async def test_catchup_checkpoint_advances_in_batches_without_dropping_missed_days(self):
        self.store.checkpoints['last_reconciled_date'] = '2026-10-20'
        await self.worker.collect_once()
        self.assertEqual(self.store.checkpoints['last_reconciled_date'], '2026-10-21')
        self.assertTrue(any('dates=20261020' in call for call in self.calls))
        await self.worker.collect_once()
        self.assertEqual(self.store.checkpoints['last_reconciled_date'], '2026-10-22')

    async def test_live_games_across_midnight_continue_refreshing(self):
        old = parse_game(event('2026-10-28', final=False))
        self.store.upsert_game(old)
        await self.worker.collect_once()
        self.assertTrue(any('dates=20261028' in call for call in self.calls))
        self.assertEqual(self.store.game_rows[old['id']]['status'], 'final')

    async def test_pre_game_polling_and_relevant_tracked_player(self):
        game = parse_game(event(final=False))
        self.store.upsert_game(game)
        self.assertEqual(self.worker.next_delay(), 60)
        self.assertTrue(self.worker.relevant_news({'title': 'Derrick White ruled out', 'body': ''}))
        self.assertFalse(self.worker.relevant_news({'title': 'NBA fan art collection', 'body': ''}))

    async def test_private_injury_url_key_is_not_stored_or_published(self):
        private_url = 'https://data.example.org/injuries?api_key=private-key#secret'
        async def request(url):
            return [{'id': 'p1', 'player': 'Jalen Brunson', 'status': 'Out', 'team': 'NYK'}]
        self.worker.injury_url = private_url
        with patch.object(self.worker, 'request', request):
            await self.worker.collect_injuries()
        item = self.store.news['injury:p1']
        self.assertEqual(item['url'], 'https://data.example.org/injuries')
        self.assertNotIn('private-key', str(self.store.news))
        self.assertNotIn('private-key', str(self.notices))


if __name__ == '__main__':
    unittest.main()
