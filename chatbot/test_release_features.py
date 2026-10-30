"""Calendar, cache and projection regression checks using isolated source fixtures."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from advanced_refresh import import_tracking, normalize_records
from contextual_projection import projection_for_leg
from dataset_refresh import initialize_scope
from efficient_ai import _CACHE, CACHE_SECONDS, data_revision, efficient_chat, usage_status
from feeds import canonical_game, canonical_player
from gameday_digest import GamedayDigest
from master import MasterStore
from season_calendar import SEED_PATH, calendar_context, import_calendar, saved_schedule, seed_calendar

UTC = dt.timezone.utc
EASTERN = ZoneInfo('America/New_York')
NOW = dt.datetime(2026, 10, 29, 22, tzinfo=UTC)


class IsolatedMaster(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')

    def observation(self, day, points=20, minutes=30, opponent='ATL', team='NYK',
                    player='Jalen Brunson', phase='Regular Season', state='final'):
        identity = canonical_game(day, team, opponent)
        self.store.upsert_game({'id': identity, 'date': day, 'home_team': team,
                               'away_team': opponent, 'status': state, 'source': 'ISOLATED FIXTURE'})
        self.store.upsert_player_game({'game_id': identity, 'player': player,
            'team': team, 'opponent': opponent, 'date': day, 'season': '2026-27',
            'season_type': phase, 'appearance_state': 'Played', 'pts': points,
            'minutes': minutes, 'source': 'ISOLATED FIXTURE'})
        return identity


class CalendarFeatureTests(IsolatedMaster):
    def test_published_eighty_games_keep_two_unknown_games_out_of_results(self):
        info = seed_calendar(self.store)
        self.assertEqual(info['published_games'], 80)
        self.assertEqual(info['unassigned_games'], 2)
        self.assertEqual(info['regular_season_total'], 82)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM season_schedule').fetchone()[0], 80)
        self.assertEqual(self.store.status()['counts']['games'], 0)
        self.assertEqual(self.store.status()['counts']['player_games'], 0)
        first_revision = info['revision']
        self.assertEqual(seed_calendar(self.store)['revision'], first_revision)
        self.assertEqual(len(saved_schedule(self.store, NOW)['games']), 80)

    def test_tipoff_uses_eastern_day_and_changes_utc_offset_with_dst(self):
        seed_calendar(self.store)
        october = calendar_context(self.store, '2026-10-30', 'NYK')
        november = calendar_context(self.store, '2026-11-02', 'NYK')
        october_tip = dt.datetime.fromisoformat(october['tipoff']).astimezone(EASTERN)
        november_tip = dt.datetime.fromisoformat(november['tipoff']).astimezone(EASTERN)
        self.assertEqual((october_tip.hour, october_tip.minute), (19, 30))
        self.assertEqual((november_tip.hour, november_tip.minute), (19, 30))
        self.assertEqual(october_tip.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(november_tip.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(october['scheduled_rest_days'], 1)
        # A Knicks-only calendar must not invent Philadelphia's prior rest.
        self.assertIsNone(calendar_context(self.store, '2026-10-30', 'PHI')['scheduled_rest_days'])

    def test_cup_gap_never_becomes_a_known_nine_day_rest_period(self):
        seed_calendar(self.store)
        context = calendar_context(self.store, '2026-12-12', 'NYK')
        self.assertIsNone(context['scheduled_rest_days'])

    def test_invalid_new_calendar_preserves_previously_loaded_calendar(self):
        original = json.loads(SEED_PATH.read_text())
        info = import_calendar(self.store, original)
        invalid = copy.deepcopy(original)
        invalid['games'].append(copy.deepcopy(invalid['games'][0]))
        with self.assertRaises(ValueError):
            import_calendar(self.store, invalid)
        self.assertEqual(saved_schedule(self.store, NOW)['revision'], info['revision'])
        invalid = copy.deepcopy(original)
        invalid['games'][-1]['start_time'] = '2027-04-13T23:00:00+00:00'
        with self.assertRaises(ValueError):
            import_calendar(self.store, invalid)
        self.assertEqual(len(saved_schedule(self.store, NOW)['games']), 80)

    def test_saved_calendar_can_send_one_labeled_prizepicks_reminder_without_live_access(self):
        seed_calendar(self.store)
        now = dt.datetime(2026, 10, 30, 22, 30, tzinfo=UTC)  # 6:30pm ET.
        class BlockedWorker:
            def __init__(self):
                import asyncio
                self._cycle_lock = asyncio.Lock()
                self.collect_news_sources = AsyncMock()
            async def request(self, url):
                response = httpx.Response(403, request=httpx.Request('GET', url))
                response.raise_for_status()
        notify = AsyncMock(return_value=True)
        async def run():
            digest = GamedayDigest(self.store, BlockedWorker(), notify, now=lambda: now)
            async def inline(function, *args, **kwargs):
                return function(*args, **kwargs)
            # Exercise real rendering/storage inline; executor scheduling has
            # separate coverage in the existing asynchronous Telegram tests.
            with patch('gameday_digest.asyncio.to_thread', new=inline):
                first = await digest.tick()
                await digest.tick()
                return first
        import asyncio
        result = asyncio.run(run())
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['schedule_mode'], 'saved_calendar')
        notify.assert_awaited_once()
        text = notify.await_args.args[0]
        self.assertIn('saved published season calendar', text)
        self.assertIn('PrizePicks reminder', text)
        self.assertIn('7:30 PM Eastern', text)
        self.assertEqual(notify.await_args.kwargs['expires_at'], dt.datetime(2026, 10, 30, 23, 30, tzinfo=UTC).timestamp())


class ProjectionFeatureTests(IsolatedMaster):
    def leg(self, **changes):
        return {'player': 'Jalen Brunson', 'team': 'NYK', 'opponent': 'PHI',
                'date': '2026-10-30', 'stat': 'pts', 'side': 'more', 'line': 19.5, **changes}

    def fill(self):
        for index in range(16):
            day = (dt.date(2026, 10, 10) + dt.timedelta(days=index)).isoformat()
            self.observation(day)

    def test_missing_minutes_prevents_a_projection_instead_of_imputing_workload(self):
        for index in range(10):
            self.observation((dt.date(2026, 10, 10) + dt.timedelta(days=index)).isoformat(), minutes=None)
        result = projection_for_leg(self.store, self.leg(), NOW)
        self.assertFalse(result['available'])
        self.assertIn('No missing minutes', result['missing_policy'])

    def test_future_live_playoff_and_old_team_observations_cannot_change_reference(self):
        self.fill()
        self.observation('2026-10-30', points=999)
        self.observation('2026-10-31', points=999)
        self.observation('2026-10-28', points=999, state='live')
        self.observation('2026-10-27', points=999, phase='Playoffs')
        self.observation('2026-10-26', points=999, team='DAL')
        result = projection_for_leg(self.store, self.leg(), NOW)
        self.assertTrue(result['available'])
        self.assertAlmostEqual(result['reference_projection'], 20)
        self.assertEqual(result['n'], 16)
        self.assertEqual(result['actual_teams'], ['NYK'])
        self.assertEqual(result['last_date'], '2026-10-25')
        self.assertTrue(all(game['date'] <= '2026-10-25' for game in result['source_games']))

    def test_opponent_signal_uses_only_actual_games_against_that_opponent(self):
        for index in range(16):
            day = (dt.date(2026, 10, 10) + dt.timedelta(days=index)).isoformat()
            self.observation(day, points=30 if index < 4 else 20,
                             opponent='PHI' if index < 4 else 'ATL')
        against_phi = projection_for_leg(self.store, self.leg(), NOW)
        against_cha = projection_for_leg(self.store, self.leg(opponent='CHA'), NOW)
        self.assertEqual(against_phi['cohorts'][0]['n'], 4)
        self.assertEqual(against_cha['cohorts'][0]['n'], 0)
        self.assertGreater(against_phi['reference_projection'], against_cha['reference_projection'])
        self.assertLessEqual(against_phi['cohorts'][0]['adjustment'], .08)

    def test_aggressive_pick_is_not_forced_when_line_has_no_supported_margin(self):
        self.fill()
        result = projection_for_leg(self.store, self.leg(line=999), NOW)
        self.assertFalse(result['aggressive_research_candidate'])
        self.assertLess(result['selected_side_margin'], 0)
        self.assertIn('not a calibrated forecast interval', result['empirical_scenario_range']['meaning'])

    def test_future_assignment_is_not_used_in_an_earlier_as_of_projection(self):
        for index in range(16):
            self.observation((dt.date(2026, 10, 1) + dt.timedelta(days=index)).isoformat())
        with self.store.connect() as db:
            db.execute('CREATE TABLE workbook_evidence (kind TEXT,entity TEXT,payload_json TEXT)')
            db.execute('INSERT INTO workbook_evidence VALUES (?,?,?)', ('assignments','Jalen Brunson',json.dumps({
                'opponent_team':'PHI','primary':'Tyrese Maxey','alternate':'Paul George',
                'assignment_asof':'2026-10-22T12:00:00','_row':8})))
        result = projection_for_leg(self.store, self.leg(date='2026-10-19'),
                                    dt.datetime(2026, 10, 18, 22, tzinfo=UTC))
        self.assertIsNone(result['tentative_pairing'])


class TrackingFeatureTests(IsolatedMaster):
    def setUp(self):
        super().setUp()
        self.scope = initialize_scope(self.store)
        self.maxey = next(player for player in self.scope['players'] if player['player'] == 'Tyrese Maxey')

    def record(self):
        return {'entity':'Tyrese Maxey','player_id':canonical_player('Tyrese Maxey'),
            'category':'Player totals','season':'2026-27','season_type':'Regular Season',
            'gp':5,'fields':{'pts':125,'reb':20,'ast':35,'minutes':175},
            'published_at':NOW.isoformat(),'source':'https://stats.nba.com/stats/leaguedashplayerstats'}

    def document(self, record):
        return {'schema_version':1,'scope_revision':self.scope['revision'],'season':'2026-27',
                'last_attempt':NOW.isoformat(),'records':[record], 'coverage':[]}

    def test_source_normalization_discards_untracked_players_and_unrequested_fields(self):
        source = [{'PLAYER_ID':self.maxey['nba_player_id'],'PLAYER_NAME':'Tyrese Maxey','GP':5,
                   'PTS':125,'REB':20,'AST':35,'MIN':175,'UNREQUESTED_STAT':900},
                  {'PLAYER_ID':'unknown','PLAYER_NAME':'New Unrequested Player','GP':5,
                   'PTS':100,'REB':10,'AST':10,'MIN':100}]
        rows = normalize_records(source, 'Player totals', self.scope, '2026-27')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['entity'], 'Tyrese Maxey')
        self.assertNotIn('UNREQUESTED_STAT', rows[0]['fields'])
        import_tracking(self.store, self.document(rows[0] | {'published_at':NOW.isoformat()}), NOW)
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT ppg,games_played FROM season_averages WHERE player='Tyrese Maxey'").fetchone()), (25,5))

    def test_import_rejects_unknown_metric_or_wrong_season_without_partial_write(self):
        for change in ({'fields':{'new_metric':2}}, {'season':'2025-26'}, {'entity':'New Unrequested Player'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    import_tracking(self.store, self.document(self.record() | change), NOW)
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM refreshed_tracking').fetchone()[0], 0)

    def test_empty_tracking_category_is_not_successful_collected_evidence(self):
        rows = normalize_records([{'PLAYER_ID':self.maxey['nba_player_id'], 'PLAYER_NAME':'Tyrese Maxey', 'GP':5}],
                                 'Passing',self.scope,'2026-27')
        self.assertEqual(rows, [])

    def test_import_rejects_impossible_games_played_and_shooting_counts(self):
        invalid = [self.record() | {'gp':-1}, self.record() | {'gp':float('nan')},
                   self.record() | {'category':'Overall','fields':{'defended_fgm':8,'defended_fga':3}}]
        for record in invalid:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    import_tracking(self.store,self.document(record),NOW)


class EfficiencyFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        _CACHE.clear()
        self.calls = []
        def handler(request):
            self.calls.append(json.loads(request.content))
            return httpx.Response(200,json={'message':{'content':'response '+str(len(self.calls))},
                'done_reason':'stop','prompt_eval_count':20,'eval_count':4})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.client.aclose)
        self.payload = {'model':'qwen2.5:3b','messages':[{'role':'user','content':'Explain this stored matchup'}]}

    async def test_repeat_avoids_model_call_and_reports_work_without_claiming_credit_savings(self):
        first = await efficient_chat(self.client,self.payload,self.store)
        second = await efficient_chat(self.client,self.payload,self.store)
        self.assertEqual(first,second)
        self.assertEqual(len(self.calls),1)
        report = usage_status(self.store)
        counts = {row['kind']:row for row in report['inference']}
        self.assertEqual(counts['model_request']['calls'],1)
        self.assertEqual(counts['cache_hit']['calls'],1)
        self.assertEqual(counts['model_request']['generated_tokens'],4)
        self.assertIn('does not change the lease price',report['billing'])

    async def test_changed_observations_and_expired_cache_require_new_inference(self):
        await efficient_chat(self.client,self.payload,self.store)
        game=canonical_game('2026-10-28','NYK','PHI')
        self.store.upsert_game({'id':game,'date':'2026-10-28','home_team':'NYK','away_team':'PHI','status':'final'})
        await efficient_chat(self.client,self.payload,self.store)
        self.assertEqual(len(self.calls),2)
        key=next(reversed(_CACHE))
        stamp,value=_CACHE[key]
        _CACHE[key]=(stamp-CACHE_SECONDS-1,value)
        await efficient_chat(self.client,self.payload,self.store)
        self.assertEqual(len(self.calls),3)

    async def test_refreshed_season_totals_invalidate_previously_cached_research(self):
        await efficient_chat(self.client,self.payload,self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO refreshed_tracking VALUES (?,?,?,?)',('2026-27','Tyrese Maxey','Player totals',
                json.dumps({'season':'2026-27','entity':'Tyrese Maxey','category':'Player totals',
                            'gp':5,'fields':{'pts':125},'published_at':NOW.isoformat()})))
        await efficient_chat(self.client,self.payload,self.store)
        self.assertEqual(len(self.calls),2)


if __name__ == '__main__':
    unittest.main()
