"""Recorded NBA live-data response example from nba_api's public documentation.

Fixture provenance: https://github.com/swar/nba_api/blob/master/docs/nba_api/live/endpoints/boxscore.md
The upstream CDN is not contacted by these tests. Live verification can be denied
by the origin; schema tests do not establish deployment-host accessibility.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from master import MasterStore
from nba_cdn import (NBADataSource, SCHEDULE_URL, SCOREBOARD_URL, BOX_URL,
                     game_start, parse_duration, parse_game, parse_players, status_of)


class NBAParsingTests(unittest.TestCase):
    def test_arena_local_time_without_offset_is_never_assumed_eastern(self):
        self.assertIsNone(game_start({'gameTimeHome': '2026-10-29T19:30:00'}))
        fallback = game_start({'gameTimeLocal': '2026-10-29T19:30:00',
                               'gameDateTimeEst': '2026-10-29T22:30:00Z'})
        self.assertEqual(fallback.hour, 22)
        aware = game_start({'gameTimeLocal': '2026-10-29T19:30:00-07:00'})
        self.assertEqual(aware.astimezone(dt.timezone.utc).hour, 2)
        self.assertEqual(aware.astimezone(dt.timezone.utc).day, 30)

    def test_recorded_full_game_minutes_and_numeric_statistics(self):
        game = parse_game(BOX_EXAMPLE['game'], BOX_URL.format('0022000180'))
        rows = parse_players(BOX_EXAMPLE, game)
        self.assertEqual(game['id'], 'nba:2021-01-15:BOS:ORL')
        self.assertEqual(game['status'], 'final')
        self.assertEqual(game['nba_season'], '2020-21')
        self.assertEqual(game['nba_season_type'], 'Regular Season')
        self.assertEqual(rows[0]['player'], 'Jaylen Brown')
        self.assertEqual(rows[0]['player_id'], 'name:jaylenbrown')
        self.assertEqual(rows[0]['nba_player_id'], '1627759')
        self.assertEqual(rows[0]['pts'], 21)
        self.assertEqual(rows[0]['fga'], 12)
        self.assertAlmostEqual(rows[0]['minutes'], 25 + 1 / 60)
        self.assertAlmostEqual(rows[1]['minutes'], 14 + 34 / 60)

    def test_duration_status_and_missing_values_are_not_guessed(self):
        self.assertEqual(parse_duration('PT1H02M30.50S'), 62 + 30.5 / 60)
        self.assertEqual(parse_duration('PT25M'), 25)
        self.assertEqual(parse_duration('25:01'), 25 + 1 / 60)
        self.assertIsNone(parse_duration('N/A'))
        self.assertEqual(status_of({'gameStatus': 1, 'gameStatusText': 'PPD'}), 'postponed')
        self.assertEqual(status_of({'gameStatus': 3, 'gameStatusText': 'Cancelled'}), 'canceled')
        with self.assertRaises(ValueError):
            status_of({'gameStatus': 99, 'gameStatusText': 'Unknown'})
        bad = copy.deepcopy(BOX_EXAMPLE)
        first = bad['game']['homeTeam']['players'][0]
        first.pop('played')
        first['statistics'] = {}
        with self.assertRaisesRegex(ValueError, 'appearance is unknown'):
            parse_players(bad, parse_game(bad['game']))

    def test_explicit_dnp_is_different_from_missing_live_stats(self):
        box = copy.deepcopy(BOX_EXAMPLE)
        dnp = {'personId': 12345, 'name': 'Example Player', 'played': '0',
               'status': 'INACTIVE', 'statistics': {'points': 0, 'minutes': 'PT00M00.00S'}}
        box['game']['homeTeam']['players'].append(dnp)
        game = parse_game(box['game'])
        rows = parse_players(box, game)
        self.assertEqual(rows[1]['appearance_state'], 'DNP')
        self.assertNotIn('pts', rows[1])
        self.assertNotIn('minutes', rows[1])
        box['game']['gameStatus'] = 2
        live = parse_players(box, parse_game(box['game']))
        self.assertEqual(len(live), 2)
        self.assertFalse(any(row['player'] == 'Example Player' for row in live))

    def test_final_missing_points_or_abbreviated_name_rejects_response(self):
        for change in ['points', 'name']:
            with self.subTest(change=change):
                box = copy.deepcopy(BOX_EXAMPLE)
                player = box['game']['awayTeam']['players'][0]
                if change == 'points':
                    player['statistics'].pop('points')
                else:
                    player['name'] = 'J. Ennis III'
                with self.assertRaises(ValueError):
                    parse_players(box, parse_game(box['game']))


class NBADataSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MasterStore(str(Path(self.directory.name) / 'master.sqlite'))
        self.box = copy.deepcopy(BOX_EXAMPLE)
        native_game = copy.deepcopy(BOX_EXAMPLE['game'])
        self.schedule = {'leagueSchedule': {'seasonYear': '2020-21', 'gameDates': [
            {'gameDate': '01/15/2021 00:00:00', 'games': [native_game]},
            {'gameDate': '01/16/2021 00:00:00', 'games': []}]}}
        self.board = {'scoreboard': {'gameDate': '2021-01-15', 'games': [copy.deepcopy(native_game)]}}
        self.calls = []
        async def request(url):
            self.calls.append(url)
            if url == SCHEDULE_URL:
                return self.schedule
            if url == SCOREBOARD_URL:
                return self.board
            if url == BOX_URL.format('0022000180'):
                return self.box
            raise AssertionError('Unexpected URL: ' + url)
        self.request = request
        self.source = NBADataSource(self.store, self.request,
                                    now=lambda: dt.datetime(2021, 1, 16, 2, tzinfo=dt.timezone.utc))

    async def asyncTearDown(self):
        self.directory.cleanup()

    def existing(self, game_status='final', minutes=25, pts=21):
        game = parse_game(BOX_EXAMPLE['game'])
        game['status'] = game_status
        game['source'] = 'existing source'
        game['provider_game_id'] = '401909877'  # A different provider's native ID.
        self.store.upsert_game(game)
        self.store.upsert_player_game({'game_id': game['id'], 'player_id': 'name:jaylenbrown',
                                      'player': 'Jaylen Brown', 'date': game['date'], 'team': 'BOS',
                                      'appearance_state': 'Played', 'minutes': minutes, 'pts': pts,
                                      'reb': 7, 'source': 'existing source'})
        return game

    async def test_actual_store_upsert_and_cached_schedule_use_native_nba_id(self):
        self.existing()
        self.assertEqual(await self.source.update_games('2021-01-15'), (1, 2))
        self.assertEqual(await self.source.update_games('2021-01-15'), (1, 2))
        self.assertEqual(self.calls.count(SCHEDULE_URL), 1)
        self.assertFalse(any('401909877' in url for url in self.calls))
        rows = self.store.query('SELECT player,pts,minutes FROM player_games ORDER BY player')['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], 21)
        self.assertAlmostEqual(rows[1][2], 25 + 1 / 60)
        self.assertIn('/boxscore_0022000180.json', self.store.games('2021-01-15')[0]['source'])

    async def test_optional_missing_statistics_do_not_erase_prior_observations(self):
        self.existing()
        for player in self.box['game']['homeTeam']['players']:
            player['statistics'].pop('reboundsTotal')
        await self.source.update_games('2021-01-15')
        result = self.store.query("SELECT pts,reb FROM player_games WHERE player_id='name:jaylenbrown'")
        self.assertEqual(result['rows'], [(21, 7)])

    async def test_invalid_final_row_leaves_all_existing_data_untouched(self):
        self.existing(pts=99)
        self.box['game']['awayTeam']['players'][0]['statistics'].pop('points')
        with self.assertRaisesRegex(ValueError, 'missing points or minutes'):
            await self.source.update_games('2021-01-15')
        self.assertEqual(self.store.query('SELECT pts FROM player_games')['rows'], [(99,)])
        self.assertEqual(self.store.games('2021-01-15')[0]['source'], 'existing source')

    async def test_schedule_only_call_cannot_publish_unvalidated_final_marker(self):
        self.box['game']['awayTeam']['players'][0]['statistics'].pop('points')
        with self.assertRaisesRegex(ValueError, 'missing points or minutes'):
            await self.source.update_games('2021-01-15', with_boxscores=False)
        self.assertEqual(self.store.status()['counts']['games'], 0)
        self.assertEqual(self.store.status()['counts']['player_games'], 0)
        self.assertIn(BOX_URL.format('0022000180'), self.calls)

    async def test_stale_scheduled_status_does_not_downgrade_a_final(self):
        game = self.existing()
        self.schedule['leagueSchedule']['gameDates'][0]['games'][0]['gameStatus'] = 1
        self.board['scoreboard']['games'][0]['gameStatus'] = 1
        with self.assertRaisesRegex(ValueError, 'conflicts with stored final'):
            await self.source.update_games('2021-01-15')
        persisted = self.store.games('2021-01-15')[0]
        self.assertEqual(persisted['status'], 'final')
        self.assertEqual(persisted['home_score'], game['home_score'])
        self.assertEqual(persisted['source'], 'existing source')

    async def test_contradictory_dnp_cannot_replace_a_live_played_observation(self):
        self.existing(game_status='live', minutes=10, pts=5)
        first = self.box['game']['homeTeam']['players'][0]
        first['played'] = '0'
        first['statistics'] = {'minutes': 'PT00M00.00S', 'points': 0}
        # Keep another played player so the response otherwise satisfies final-team checks.
        extra = copy.deepcopy(self.box['game']['awayTeam']['players'][0])
        extra['name'], extra['personId'] = 'Another Example', 99999
        self.box['game']['homeTeam']['players'].append(extra)
        with self.assertRaisesRegex(ValueError, 'DNP conflicts'):
            await self.source.update_games('2021-01-15')
        prior = self.store.query("SELECT appearance_state,minutes,pts FROM player_games WHERE player_id='name:jaylenbrown'")
        self.assertEqual(prior['rows'], [('Played', 10, 5)])

    async def test_out_of_season_and_403_are_failures_not_empty_success(self):
        with self.assertRaisesRegex(ValueError, 'outside this NBA schedule season'):
            await self.source.update_games('2020-01-01')
        calls = []
        async def forbidden(url):
            calls.append(url)
            response = httpx.Response(403, request=httpx.Request('GET', url))
            response.raise_for_status()
        denied = NBADataSource(self.store, forbidden)
        with self.assertRaises(httpx.HTTPStatusError):
            await denied.update_games('2021-01-15')
        self.assertEqual(calls, [SCHEDULE_URL])

    def second_game(self):
        good = copy.deepcopy(BOX_EXAMPLE)
        good['game']['gameId'] = '0022000181'
        good['game']['homeTeam']['teamTricode'] = 'NYK'
        self.schedule['leagueSchedule']['gameDates'][0]['games'].append(copy.deepcopy(good['game']))
        self.board['scoreboard']['games'].append(copy.deepcopy(good['game']))
        return good

    async def test_one_malformed_final_does_not_prevent_later_valid_game_refresh(self):
        good = self.second_game()
        self.box['game']['awayTeam']['players'][0]['statistics'].pop('points')
        original_request = self.request
        async def request(url):
            if url == BOX_URL.format('0022000181'):
                return good
            return await original_request(url)
        self.source.request = request
        with self.assertRaisesRegex(ValueError, '1 games refreshed; incomplete NBA game data'):
            await self.source.update_games('2021-01-15')
        self.assertEqual([row['id'] for row in self.store.games('2021-01-15')],
                         ['nba:2021-01-15:NYK:ORL'])
        self.assertEqual(self.store.status()['counts']['player_games'], 2)

    async def test_authorization_or_quota_denial_stops_remaining_game_requests(self):
        self.second_game()
        original_request = self.request
        for code in [401, 403, 429]:
            with self.subTest(status=code):
                calls = []
                async def request(url):
                    calls.append(url)
                    if '/boxscore/' in url:
                        httpx.Response(code, request=httpx.Request('GET', url)).raise_for_status()
                    return await original_request(url)
                source = NBADataSource(self.store, request, now=self.source.now)
                with self.assertRaises(httpx.HTTPStatusError):
                    await source.update_games('2021-01-15')
                self.assertNotIn(BOX_URL.format('0022000181'), calls)
                self.assertEqual(self.store.status()['counts']['games'], 0)


BOX_EXAMPLE = json.loads("{\n   \"meta\":{\n      \"version\":1,\n      \"code\":200,\n      \"request\":\"http://nba.cloud/games/0022000180/boxscore?Format=json\",\n      \"time\":\"2021-01-15 23:51:25.282704\"\n   },\n   \"game\":{\n      \"gameId\":\"0022000180\",\n      \"gameTimeLocal\":\"2021-01-15T19:30:00-05:00\",\n      \"gameTimeUTC\":\"2021-01-16T00:30:00Z\",\n      \"gameTimeHome\":\"2021-01-15T19:30:00-05:00\",\n      \"gameTimeAway\":\"2021-01-15T19:30:00-05:00\",\n      \"gameEt\":\"2021-01-15T19:30:00-05:00\",\n      \"duration\":125,\n      \"gameCode\":\"20210115/ORLBOS\",\n      \"gameStatusText\":\"Final\",\n      \"gameStatus\":3,\n      \"regulationPeriods\":4,\n      \"period\":4,\n      \"gameClock\":\"PT00M00.00S\",\n      \"attendance\":0,\n      \"sellout\":\"0\",\n      \"arena\":{\n         \"arenaId\":17,\n         \"arenaName\":\"TD Garden\",\n         \"arenaCity\":\"Boston\",\n         \"arenaState\":\"MA\",\n         \"arenaCountry\":\"US\",\n         \"arenaTimezone\":\"America/New_York\"\n      },\n      \"officials\":[\n         {\n            \"personId\":201638,\n            \"name\":\"Brent Barnaky\",\n            \"nameI\":\"B. Barnaky\",\n            \"firstName\":\"Brent\",\n            \"familyName\":\"Barnaky\",\n            \"jerseyNum\":\"36\",\n            \"assignment\":\"OFFICIAL1\"\n         }\n      ],\n      \"homeTeam\":{\n         \"teamId\":1610612738,\n         \"teamName\":\"Celtics\",\n         \"teamCity\":\"Boston\",\n         \"teamTricode\":\"BOS\",\n         \"score\":124,\n         \"inBonus\":\"1\",\n         \"timeoutsRemaining\":2,\n         \"periods\":[\n            {\n               \"period\":1,\n               \"periodType\":\"REGULAR\",\n               \"score\":34\n            }\n         ],\n         \"players\":[\n            {\n               \"status\":\"ACTIVE\",\n               \"order\":1,\n               \"personId\":1627759,\n               \"jerseyNum\":\"7\",\n               \"position\":\"SF\",\n               \"starter\":\"1\",\n               \"oncourt\":\"0\",\n               \"played\":\"1\",\n               \"statistics\":{\n                  \"assists\":8,\n                  \"blocks\":0,\n                  \"blocksReceived\":0,\n                  \"fieldGoalsAttempted\":12,\n                  \"fieldGoalsMade\":6,\n                  \"fieldGoalsPercentage\":0.5,\n                  \"foulsOffensive\":0,\n                  \"foulsDrawn\":4,\n                  \"foulsPersonal\":1,\n                  \"foulsTechnical\":0,\n                  \"freeThrowsAttempted\":7,\n                  \"freeThrowsMade\":7,\n                  \"freeThrowsPercentage\":1.0,\n                  \"minus\":50.0,\n                  \"minutes\":\"PT25M01.00S\",\n                  \"minutesCalculated\":\"PT25M\",\n                  \"plus\":65.0,\n                  \"plusMinusPoints\":15.0,\n                  \"points\":21,\n                  \"pointsFastBreak\":0,\n                  \"pointsInThePaint\":6,\n                  \"pointsSecondChance\":0,\n                  \"reboundsDefensive\":2,\n                  \"reboundsOffensive\":0,\n                  \"reboundsTotal\":2,\n                  \"steals\":1,\n                  \"threePointersAttempted\":5,\n                  \"threePointersMade\":2,\n                  \"threePointersPercentage\":0.4,\n                  \"turnovers\":2,\n                  \"twoPointersAttempted\":7,\n                  \"twoPointersMade\":4,\n                  \"twoPointersPercentage\":0.5714285714285711\n               },\n               \"name\":\"Jaylen Brown\",\n               \"nameI\":\"J. Brown\",\n               \"firstName\":\"Jaylen\",\n               \"familyName\":\"Brown\"\n            }\n         ],\n         \"statistics\":{\n            \"assists\":25,\n            \"assistsTurnoverRatio\":2.27272727272727,\n            \"benchPoints\":66,\n            \"biggestLead\":29,\n            \"biggestLeadScore\":\"72-101\",\n            \"biggestScoringRun\":13,\n            \"biggestScoringRunScore\":\"72-101\",\n            \"blocks\":4,\n            \"blocksReceived\":1,\n            \"fastBreakPointsAttempted\":9,\n            \"fastBreakPointsMade\":5,\n            \"fastBreakPointsPercentage\":0.555555555555556,\n            \"fieldGoalsAttempted\":88,\n            \"fieldGoalsEffectiveAdjusted\":0.607954545454545,\n            \"fieldGoalsMade\":45,\n            \"fieldGoalsPercentage\":0.511363636363636,\n            \"foulsOffensive\":0,\n            \"foulsDrawn\":16,\n            \"foulsPersonal\":18,\n            \"foulsTeam\":18,\n            \"foulsTechnical\":0,\n            \"foulsTeamTechnical\":0,\n            \"freeThrowsAttempted\":19,\n            \"freeThrowsMade\":17,\n            \"freeThrowsPercentage\":0.894736842105263,\n            \"leadChanges\":4,\n            \"minutes\":\"PT240M00.00S\",\n            \"minutesCalculated\":\"PT240M\",\n            \"points\":124,\n            \"pointsAgainst\":97,\n            \"pointsFastBreak\":13,\n            \"pointsFromTurnovers\":16,\n            \"pointsInThePaint\":48,\n            \"pointsInThePaintAttempted\":36,\n            \"pointsInThePaintMade\":24,\n            \"pointsInThePaintPercentage\":0.666666666666666,\n            \"pointsSecondChance\":11,\n            \"reboundsDefensive\":39,\n            \"reboundsOffensive\":6,\n            \"reboundsPersonal\":45,\n            \"reboundsTeam\":6,\n            \"reboundsTeamDefensive\":2,\n            \"reboundsTeamOffensive\":4,\n            \"reboundsTotal\":51,\n            \"secondChancePointsAttempted\":6,\n            \"secondChancePointsMade\":4,\n            \"secondChancePointsPercentage\":0.666666666666666,\n            \"steals\":7,\n            \"threePointersAttempted\":42,\n            \"threePointersMade\":17,\n            \"threePointersPercentage\":0.40476190476190504,\n            \"timeLeading\":\"PT47M16.00S\",\n            \"timesTied\":0,\n            \"trueShootingAttempts\":96.36,\n            \"trueShootingPercentage\":0.6434205064342051,\n            \"turnovers\":10,\n            \"turnoversTeam\":1,\n            \"turnoversTotal\":11,\n            \"twoPointersAttempted\":46,\n            \"twoPointersMade\":28,\n            \"twoPointersPercentage\":0.608695652173913\n         }\n      },\n      \"awayTeam\":{\n         \"teamId\":1610612753,\n         \"teamName\":\"Magic\",\n         \"teamCity\":\"Orlando\",\n         \"teamTricode\":\"ORL\",\n         \"score\":97,\n         \"inBonus\":\"1\",\n         \"timeoutsRemaining\":2,\n         \"periods\":[\n            {\n               \"period\":1,\n               \"periodType\":\"REGULAR\",\n               \"score\":28\n            }\n         ],\n         \"players\":[\n            {\n               \"status\":\"ACTIVE\",\n               \"order\":1,\n               \"personId\":203516,\n               \"jerseyNum\":\"11\",\n               \"position\":\"SF\",\n               \"starter\":\"1\",\n               \"oncourt\":\"0\",\n               \"played\":\"1\",\n               \"statistics\":{\n                  \"assists\":0,\n                  \"blocks\":0,\n                  \"blocksReceived\":0,\n                  \"fieldGoalsAttempted\":4,\n                  \"fieldGoalsMade\":1,\n                  \"fieldGoalsPercentage\":0.25,\n                  \"foulsOffensive\":0,\n                  \"foulsDrawn\":0,\n                  \"foulsPersonal\":3,\n                  \"foulsTechnical\":0,\n                  \"freeThrowsAttempted\":0,\n                  \"freeThrowsMade\":0,\n                  \"freeThrowsPercentage\":0.0,\n                  \"minus\":41.0,\n                  \"minutes\":\"PT14M34.00S\",\n                  \"minutesCalculated\":\"PT14M\",\n                  \"plus\":36.0,\n                  \"plusMinusPoints\":-5.0,\n                  \"points\":2,\n                  \"pointsFastBreak\":0,\n                  \"pointsInThePaint\":2,\n                  \"pointsSecondChance\":0,\n                  \"reboundsDefensive\":1,\n                  \"reboundsOffensive\":0,\n                  \"reboundsTotal\":1,\n                  \"steals\":0,\n                  \"threePointersAttempted\":2,\n                  \"threePointersMade\":0,\n                  \"threePointersPercentage\":0.0,\n                  \"turnovers\":0,\n                  \"twoPointersAttempted\":2,\n                  \"twoPointersMade\":1,\n                  \"twoPointersPercentage\":0.5\n               },\n               \"name\":\"James Ennis III\",\n               \"nameI\":\"J. Ennis III\",\n               \"firstName\":\"James\",\n               \"familyName\":\"Ennis III\"\n            }\n         ],\n         \"statistics\":{\n            \"assists\":14,\n            \"assistsTurnoverRatio\":1.07692307692308,\n            \"benchPoints\":33,\n            \"biggestLead\":1,\n            \"biggestLeadScore\":\"4-3\",\n            \"biggestScoringRun\":13,\n            \"biggestScoringRunScore\":\"72-101\",\n            \"blocks\":1,\n            \"blocksReceived\":4,\n            \"fastBreakPointsAttempted\":7,\n            \"fastBreakPointsMade\":2,\n            \"fastBreakPointsPercentage\":0.28571428571428603,\n            \"fieldGoalsAttempted\":94,\n            \"fieldGoalsEffectiveAdjusted\":0.441489361702128,\n            \"fieldGoalsMade\":38,\n            \"fieldGoalsPercentage\":0.404255319148936,\n            \"foulsOffensive\":2,\n            \"foulsDrawn\":18,\n            \"foulsPersonal\":16,\n            \"foulsTeam\":14,\n            \"foulsTechnical\":1,\n            \"foulsTeamTechnical\":0,\n            \"freeThrowsAttempted\":22,\n            \"freeThrowsMade\":14,\n            \"freeThrowsPercentage\":0.636363636363636,\n            \"leadChanges\":4,\n            \"minutes\":\"PT240M00.00S\",\n            \"minutesCalculated\":\"PT240M\",\n            \"points\":97,\n            \"pointsAgainst\":124,\n            \"pointsFastBreak\":8,\n            \"pointsFromTurnovers\":10,\n            \"pointsInThePaint\":52,\n            \"pointsInThePaintAttempted\":47,\n            \"pointsInThePaintMade\":26,\n            \"pointsInThePaintPercentage\":0.553191489361702,\n            \"pointsSecondChance\":20,\n            \"reboundsDefensive\":31,\n            \"reboundsOffensive\":15,\n            \"reboundsPersonal\":46,\n            \"reboundsTeam\":12,\n            \"reboundsTeamDefensive\":4,\n            \"reboundsTeamOffensive\":8,\n            \"reboundsTotal\":58,\n            \"secondChancePointsAttempted\":16,\n            \"secondChancePointsMade\":9,\n            \"secondChancePointsPercentage\":0.5625,\n            \"steals\":5,\n            \"threePointersAttempted\":28,\n            \"threePointersMade\":7,\n            \"threePointersPercentage\":0.25,\n            \"timeLeading\":\"PT00M30.00S\",\n            \"timesTied\":0,\n            \"trueShootingAttempts\":103.68,\n            \"trueShootingPercentage\":0.46778549382716,\n            \"turnovers\":11,\n            \"turnoversTeam\":2,\n            \"turnoversTotal\":13,\n            \"twoPointersAttempted\":66,\n            \"twoPointersMade\":31,\n            \"twoPointersPercentage\":0.46969696969696995\n         }\n      }\n   }\n}")

