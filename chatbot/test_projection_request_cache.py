"""Request-scoped raw projection reads preserve market-specific exclusions."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import career_identity
from contextual_projection import _comparable_observations, projection_read_scope
from feeds import canonical_game, canonical_player
from master import MasterStore


class TracedStore(MasterStore):
    def __init__(self, path):
        self.queries = []
        super().__init__(path)

    @contextmanager
    def connect(self):
        with super().connect() as db:
            db.set_trace_callback(self.queries.append)
            yield db

    def read_counts(self):
        return (sum(q.startswith('SELECT DISTINCT player,player_id FROM player_games WHERE date<')
                    for q in self.queries),
                sum(q.startswith('WITH eligible AS (SELECT p.*,g.home_team,g.away_team')
                    for q in self.queries))


class ProjectionRequestCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = self.new_store('one')

    def new_store(self, name, points=24):
        store = TracedStore(Path(self.directory.name) / (name + '.sqlite'))
        # A bounded fixture exercises the broad identity scan without adding
        # minutes to CI. Real-master query counts are benchmarked separately.
        with store.connect() as db:
            for day in range(1, 22):
                date = f'2026-10-{day:02}'
                game = canonical_game(date, 'NYK', 'BOS')
                db.execute('INSERT INTO games(id,date,status,home_team,away_team,updated_at) '
                           'VALUES(?,?,?,?,?,?)', (game, date, 'final', 'NYK', 'BOS', date))
                for player in ('Jalen Brunson', 'Josh Hart'):
                    db.execute('INSERT INTO player_games(game_id,player_id,player,team,opponent,date,'
                               'season,season_type,appearance_state,pts,reb,ast,fga,minutes,source,updated_at) '
                               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (game, canonical_player(player), player, 'NYK', 'BOS', date,
                                '2026-27', 'Regular Season', 'Played', points, 5, 4, 15, 32,
                                'isolated cache fixture', date))
            db.executemany('INSERT INTO player_games(game_id,player_id,player,date,updated_at) '
                           'VALUES(?,?,?,?,?)',
                           [('distractor-game', f'fixture:{n}', f'Unrelated Fixture {n}',
                             '2026-10-01', '2026-10-01') for n in range(1500)])
        store.queries.clear()
        return store

    def sample(self, components=('pts',), *, store=None, player='Jalen Brunson',
               cutoff='2026-11-01', team='NYK'):
        return _comparable_observations(store or self.store, player, canonical_player(player),
                                        team, cutoff, components)

    def five_markets(self, **kwargs):
        return [self.sample(components, **kwargs) for components in
                [('pts',), ('reb',), ('ast',), ('pts', 'reb', 'ast'), ('fga',)]]

    def test_five_markets_match_uncached_and_only_read_each_player_once(self):
        expected = [self.five_markets(player=player) for player in ('Jalen Brunson', 'Josh Hart')]
        self.assertEqual(self.store.read_counts(), (10, 10))
        self.store.queries.clear()
        with patch('career_identity.resolve_career_identity',
                   wraps=career_identity.resolve_career_identity) as resolve:
            with projection_read_scope():
                actual = [self.five_markets(player=player)
                          for player in ('Jalen Brunson', 'Josh Hart')]
        self.assertEqual(actual, expected)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(self.store.read_counts(), (1, 2))

    def test_decorator_nesting_and_next_request_observe_mutations(self):
        @projection_read_scope()
        def request():
            first = self.sample()
            with projection_read_scope():
                self.assertEqual(self.sample(), first)
            with self.store.connect() as db:
                db.execute("UPDATE player_games SET pts=31 WHERE player='Jalen Brunson'")
            self.assertEqual(self.sample(), first)
            return first
        self.assertEqual(request()[0][0]['pts'], 24)
        self.assertEqual(self.store.read_counts(), (1, 1))
        self.assertEqual(request()[0][0]['pts'], 31)
        self.assertEqual(self.store.read_counts(), (2, 2))

    def test_conflicts_and_missing_values_remain_specific_to_each_market(self):
        with self.store.connect() as db:
            row = dict(db.execute("SELECT * FROM player_games WHERE player='Jalen Brunson' "
                                  'ORDER BY date DESC LIMIT 1').fetchone())
            row.update(player_id='provider:duplicate', player='Jalen-Brunson', pts=99)
            columns = list(row)
            db.execute('INSERT INTO player_games(' + ','.join(columns) + ') VALUES(' +
                       ','.join('?' for _ in columns) + ')', [row[key] for key in columns])
            db.execute("UPDATE player_games SET ast=NULL WHERE player='Jalen Brunson' "
                       "AND date='2026-10-20'")
        with projection_read_scope():
            rebounds, rebound_quality = self.sample(('reb',))
            points, point_quality = self.sample(('pts',))
            assists, assist_quality = self.sample(('ast',))
            self.assertEqual(self.sample(('reb',)), (rebounds, rebound_quality))
        self.assertEqual((len(rebounds), len(points), len(assists)), (21, 20, 20))
        self.assertEqual(rebound_quality['conflicting_games'], 0)
        self.assertEqual(point_quality['conflicting_games'], 1)
        self.assertEqual(assist_quality['incomplete_games'], 1)
        self.assertEqual(self.store.read_counts(), (1, 1))

    def test_store_cutoff_and_team_are_separate_cache_keys(self):
        other = self.new_store('two', points=77)
        with projection_read_scope():
            self.assertEqual(self.sample()[0][0]['pts'], 24)
            self.assertEqual(self.sample(store=other)[0][0]['pts'], 77)
            self.assertEqual(len(self.sample(cutoff='2026-10-10')[0]), 9)
            self.assertEqual(self.sample(team='BOS')[0], [])
            self.assertEqual(self.sample()[0][0]['pts'], 24)
        self.assertEqual(self.store.read_counts(), (2, 3))
        self.assertEqual(other.read_counts(), (1, 1))

    def test_exception_and_cancellation_restore_context(self):
        for error in (RuntimeError('fixture'), asyncio.CancelledError()):
            try:
                with projection_read_scope():
                    self.sample()
                    raise error
            except (RuntimeError, asyncio.CancelledError):
                pass
            self.sample()
        self.assertEqual(self.store.read_counts(), (4, 4))

    def test_unknown_player_becomes_visible_in_new_request(self):
        with projection_read_scope():
            self.assertEqual(self.sample(player='New Fixture Player')[0], [])
            with self.store.connect() as db:
                db.execute("UPDATE player_games SET player='New Fixture Player',player_id='fixture:new' "
                           "WHERE player='Josh Hart'")
            self.assertEqual(self.sample(player='New Fixture Player')[0], [])
        with projection_read_scope():
            self.assertEqual(len(self.sample(player='New Fixture Player')[0]), 21)


class ProjectionCacheAsyncIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_and_inherited_thread_leave_no_shared_cache(self):
        fixture = ProjectionRequestCacheTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ready = asyncio.Event()

        async def cancelled_request():
            with projection_read_scope():
                fixture.sample()
                ready.set()
                await asyncio.Future()

        task = asyncio.create_task(cancelled_request())
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with fixture.store.connect() as db:
            db.execute("UPDATE player_games SET pts=39 WHERE player='Jalen Brunson'")
        with projection_read_scope():
            self.assertEqual(fixture.sample()[0][0]['pts'], 39)

            def thread_request():
                with fixture.store.connect() as db:
                    db.execute("UPDATE player_games SET pts=57 WHERE player='Jalen Brunson'")
                with projection_read_scope():
                    self.assertEqual(fixture.sample()[0][0]['pts'], 57)
                    fixture.sample(('reb',))

            await asyncio.to_thread(thread_request)
            self.assertEqual(fixture.sample()[0][0]['pts'], 39)
        with projection_read_scope():
            self.assertEqual(fixture.sample()[0][0]['pts'], 57)
        self.assertEqual(fixture.store.read_counts(), (4, 4))

    async def test_child_tasks_do_not_inherit_mutable_parent_cache(self):
        fixture = ProjectionRequestCacheTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first_ready, second_ready = asyncio.Event(), asyncio.Event()

        async def first():
            with projection_read_scope():
                self.assertEqual(fixture.sample()[0][0]['pts'], 24)
                first_ready.set()
                await second_ready.wait()
                self.assertEqual(fixture.sample()[0][0]['pts'], 24)

        async def second():
            await first_ready.wait()
            with fixture.store.connect() as db:
                db.execute("UPDATE player_games SET pts=52 WHERE player='Jalen Brunson'")
            with projection_read_scope():
                self.assertEqual(fixture.sample()[0][0]['pts'], 52)
                second_ready.set()

        with projection_read_scope():
            self.assertEqual(fixture.sample()[0][0]['pts'], 24)
            await asyncio.gather(first(), second())
            self.assertEqual(fixture.sample()[0][0]['pts'], 24)
        self.assertEqual(fixture.store.read_counts(), (3, 3))


if __name__ == '__main__':
    unittest.main()
