"""Independent projections use existing evidence, never hypothetical book offers."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextual_projection import projection_for_leg
from feeds import canonical_game, canonical_player
from independent_picks import _targets, build_independent_picks, format_independent_picks
from master import MasterStore
from season_calendar import seed_calendar

NOW = dt.datetime(2026, 10, 30, 22, 40, tzinfo=dt.timezone.utc)
DATE = '2026-10-30'


class IndependentPickTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MasterStore(Path(self.directory.name) / 'master.sqlite')
        seed_calendar(self.store)

    def observations(self, name, team, n=12, days_ago=1, source='synthetic independent fixture', minutes=33):
        for offset in range(n):
            day = (dt.date(2026, 10, 30)-dt.timedelta(days=days_ago+offset)).isoformat()
            other = 'BOS'
            identity = canonical_game(day, team, other)
            self.store.upsert_game({'id': identity, 'date': day, 'status': 'final',
                'home_team': team, 'away_team': other, 'source': source})
            self.store.upsert_player_game({'game_id': identity, 'player_id': canonical_player(name),
                'player': name, 'team': team, 'opponent': other, 'date': day, 'season': '2026-27',
                'season_type': 'Regular Season', 'appearance_state': 'Played', 'minutes': minutes,
                'pts': 20+(offset%4), 'reb': 5+(offset%3), 'ast': 4+(offset%2),
                'fga': 17+(offset%3), 'tov': 2, 'source': source})

    def four_players(self):
        for name, team in [('Jalen Brunson','NYK'),('Karl-Anthony Towns','NYK'),
                           ('Tyrese Maxey','PHI'),('Joel Embiid','PHI')]:
            self.observations(name, team)

    def table_counts(self):
        with self.store.connect() as db:
            return {name: db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                    ('boards','entries','player_games','games','checkpoints','news')}

    def test_without_boards_produces_four_distinct_players_and_two_teams(self):
        self.four_players()
        before = self.table_counts()
        report = build_independent_picks(self.store, date=DATE, now=NOW)
        self.assertEqual(len(report['picks']), 4)
        self.assertEqual(len({p['player'] for p in report['picks']}), 4)
        self.assertTrue(report['two_team_coverage'])
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(before['boards'], 0)
        self.assertTrue(report['requires_prizepicks_comparison'])

    def test_projection_without_line_has_no_fabricated_offer_or_margin(self):
        self.observations('Jalen Brunson', 'NYK')
        value = projection_for_leg(self.store, {'player':'Jalen Brunson','team':'NYK','date':DATE,'stat':'pts'}, NOW)
        self.assertTrue(value['available'])
        self.assertFalse(value['priced_offer'])
        self.assertIsNone(value['selected_side_margin'])
        self.assertIsNone(value['standardized_margin'])
        self.assertFalse(value['aggressive_research_candidate'])

    def test_priced_projection_retains_previous_margin_semantics(self):
        self.observations('Jalen Brunson', 'NYK')
        base = {'player':'Jalen Brunson','team':'NYK','date':DATE,'stat':'pts','line':19.5}
        more = projection_for_leg(self.store, {**base,'side':'more'}, NOW)
        less = projection_for_leg(self.store, {**base,'side':'less'}, NOW)
        self.assertTrue(more['priced_offer'])
        self.assertEqual(more['selected_side_margin'], -less['selected_side_margin'])
        self.assertAlmostEqual(more['reference_projection']-19.5,more['selected_side_margin'],places=2)

    def test_conditional_targets_are_outward_half_points_with_no_probabilities(self):
        self.four_players()
        report = build_independent_picks(self.store, DATE, now=NOW)
        for pick in report['picks']:
            targets = pick['targets'];reference = pick['projection']['reference_projection']
            self.assertLess(targets['more_at_or_below'], reference)
            self.assertGreater(targets['less_at_or_above'], reference)
            self.assertEqual(targets['more_at_or_below']%1, .5)
            self.assertEqual(targets['less_at_or_above']%1, .5)
            self.assertNotIn('line', pick)
            self.assertNotIn('side', pick)
            self.assertIsNone(pick['projection']['selected_side_margin'])
        self.assertNotIn('win_probability', json.dumps(report))

    def test_small_projection_never_clamps_negative_more_target_to_fake_edge(self):
        value = _targets({'reference_projection': .2, 'empirical_scenario_range': {'p10':0,'p90':2}})
        self.assertIsNone(value['more_at_or_below'])
        self.assertGreater(value['less_at_or_above'], .2)

    def test_explicit_started_date_holds_and_default_selects_next_game(self):
        self.four_players()
        later = dt.datetime(2026,10,31,0,tzinfo=dt.timezone.utc)
        today = build_independent_picks(self.store, DATE, now=later)
        self.assertEqual(today['picks'], [])
        self.assertIsNone(today['game'])
        upcoming = build_independent_picks(self.store, now=later)
        self.assertGreater(upcoming['game_date'], DATE)
        self.assertIn(upcoming['game']['home_team'], ('NYK','CHA'))
        self.assertIn('Nov', format_independent_picks(upcoming))

    def test_observed_started_game_overrides_future_calendar(self):
        self.four_players()
        self.store.upsert_game({'id':'nba:2026-10-30:NYK:PHI','date':DATE,'home_team':'NYK',
            'away_team':'PHI','status':'live','start_time':'2026-10-30T23:30:00+00:00','source':'fixture live'})
        report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(report['picks'], [])

    def test_postponed_tbd_or_ambiguous_schedule_never_creates_picks(self):
        self.four_players()
        for status, start in [('postponed','2026-10-30T23:30:00+00:00'),('scheduled',None)]:
            with self.store.connect() as db:
                db.execute('UPDATE season_schedule SET status=?,start_time=? WHERE date=?',(status,start,DATE))
            report = build_independent_picks(self.store, DATE, now=NOW)
            self.assertFalse(report['picks'])

    def test_insufficient_opponent_sample_is_not_filler(self):
        self.observations('Jalen Brunson','NYK')
        self.observations('Karl-Anthony Towns','NYK')
        self.observations('Tyrese Maxey','PHI',n=5)
        report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(len(report['picks']),2)
        self.assertFalse(report['two_team_coverage'])
        self.assertIn('Two-team coverage is unavailable',report['note'])
        self.assertTrue(any(row['player']=='Tyrese Maxey' for row in report['held']))

    def test_opponent_supported_player_is_retained_in_shortlist(self):
        for i in range(4):
            self.observations('Knicks Fixture '+str(i),'NYK',n=20)
        self.observations('Opponent Fixture','PHI',n=8)
        report = build_independent_picks(self.store, DATE, count=4, now=NOW)
        self.assertTrue(report['two_team_coverage'])
        self.assertEqual(len(report['picks']),4)

    def test_latest_team_change_prevents_using_old_team_history(self):
        self.observations('Jalen Brunson','NYK')
        self.store.upsert_player_game({'game_id':'new-team-fixture','player':'Jalen Brunson','team':'BOS',
            'date':'2026-10-30','season':'2026-27','appearance_state':'DNP','source':'fixture latest team'})
        report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(report['picks'], [])

    def test_stale_or_unprovenanced_samples_are_held(self):
        self.observations('Old Fixture','NYK',days_ago=10)
        self.observations('Missing Source','PHI',source='')
        report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(report['picks'], [])
        self.assertIn('Source provenance',json.dumps(report['held']))

    def test_out_report_holds_and_unknown_timezone_is_not_health_confirmation(self):
        self.four_players()
        self.store.record_news({'id':'out-fixture','title':'Jalen Brunson: Ruled out for Friday',
            'published_at':'2026-10-30T17:00:00','source':'fixture publisher','body':'Injury reported.'})
        report = build_independent_picks(self.store, DATE, now=NOW)
        self.assertNotIn('Jalen Brunson',[row['player'] for row in report['picks']])
        self.assertIn('timezone',json.dumps(report['held']))

    def test_future_observations_do_not_change_roster_or_projection(self):
        self.four_players()
        before=build_independent_picks(self.store, DATE, now=NOW)
        self.store.upsert_player_game({'game_id':'future-fixture','player':'Jalen Brunson','team':'BOS',
            'date':'2026-11-05','season':'2026-27','appearance_state':'Played','minutes':48,'pts':90,'source':'future fixture'})
        after=build_independent_picks(self.store, DATE, now=NOW)
        self.assertEqual(before['picks'],after['picks'])

    def test_formatter_is_concise_and_explicitly_conditional(self):
        self.four_players()
        report=build_independent_picks(self.store, DATE, now=NOW)
        full=format_independent_picks(report)
        compact=format_independent_picks(report,compact=True)
        self.assertLessEqual(len(full),1800)
        self.assertLessEqual(len(compact),950)
        for text in (full,compact):
            self.assertIn('not quoted PrizePicks offers',text)
            self.assertIn('More if line',text)
            self.assertIn('Less if line',text)
            self.assertIn('no entry recorded',text)

    def test_invalid_date_count_do_not_change_records(self):
        before=self.table_counts()
        with self.assertRaises(ValueError):build_independent_picks(self.store,'2026-99-99',now=NOW)
        for count in [0,7,True]:
            with self.assertRaises(ValueError):build_independent_picks(self.store,DATE,count=count,now=NOW)
        self.assertEqual(before,self.table_counts())

    def test_long_six_player_output_keeps_complete_targets_and_footer(self):
        self.four_players()
        report=build_independent_picks(self.store, DATE, now=NOW)
        original=report['picks'][0]
        report['picks']=[{**original,'player':'Long Fictional Player Name '+str(i),
            'caution':'Questionable for the target game; verify playing clearance before comparing these targets.'}
            for i in range(6)]
        for compact in (False,True):
            text=format_independent_picks(report,compact=compact)
            self.assertLessEqual(len(text),950 if compact else 1800)
            self.assertTrue(text.endswith('no entry recorded.'))
            self.assertEqual(text.count('More if line'),text.count('Less if line'))
            if text.count('More if line') < len(report['picks']):
                self.assertIn('Showing',text)


if __name__=='__main__':
    unittest.main()
