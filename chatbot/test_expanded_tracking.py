"""Regression checks for bounded, scoped refresh and field-level preservation."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from advanced_refresh import (direct_tracking_status, import_tracking, normalize_records,
                              preserve_sparse_fields, supplement_tracking)
from dataset_refresh import initialize_scope, load_scope
from master import MasterStore

NOW = dt.datetime(2026, 10, 31, 16, tzinfo=dt.timezone.utc)


class ExpandedTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MasterStore(self.root / 'master.sqlite')
        self.scope = initialize_scope(self.store)
        self.player = self.scope['players'][0]

    def tearDown(self):
        self.temp.cleanup()

    def record(self, fields=None, **kw):
        return {'entity': self.player['player'], 'category': 'Passing', 'season': '2026-27',
                'season_type': 'Regular Season', 'gp': 4, 'per_mode': 'Totals',
                'fields': fields or {'potential_assists': 40, 'passes': 180, 'assists': 20},
                'published_at': '2026-10-29T15:00:00+00:00', 'data_through_date': '2026-10-28',
                'coverage_basis': 'source_reported', 'source': 'https://stats.nba.com/stats/leaguedashptstats', **kw}

    def document(self, record):
        return {'schema_version': 1, 'scope_revision': self.scope['revision'], 'season': '2026-27',
                'last_attempt': NOW.isoformat(), 'records': [record],
                'coverage': [{'category': record['category'], 'status': 'collected'}]}

    def saved(self):
        with self.store.connect() as db:
            return json.loads(db.execute('SELECT payload_json FROM refreshed_tracking').fetchone()[0])

    def test_per_game_source_is_not_silently_divided_again(self):
        row = {'PLAYER_ID': self.player['nba_player_id'], 'GP': 5, 'POTENTIAL_AST': 4, 'PER_MODE': 'PerGame'}
        self.assertEqual(normalize_records([row], 'Passing', self.scope, '2026-27'), [])
        with self.assertRaises(ValueError):
            normalize_records([row], 'Passing', self.scope, '2026-27', per_mode='PerGame')
        with self.assertRaises(ValueError):
            import_tracking(self.store, self.document(self.record(per_mode='PerGame')), NOW)

    def test_hustle_uses_source_g_and_explicit_cumulative_hustle_metrics(self):
        row = {'PLAYER_ID': self.player['nba_player_id'], 'G': 5, 'CONTESTED_SHOTS': 32,
               'DEFLECTIONS': 8, 'SCREEN_ASSISTS': 90, 'CHARGES_DRAWN': 20}
        rec = normalize_records([row], 'Hustle', self.scope, '2026-27')[0]
        self.assertEqual(rec['gp'], 5)
        self.assertEqual(rec['fields'], {'contested_shots': 32, 'deflections': 8,
                                         'screen_assists': 90, 'charges_drawn': 20})
        self.assertEqual(rec['field_units']['contested_shots'], 'cumulative_total')

    def test_team_population_uses_known_id_and_retains_its_own_denominator(self):
        row = {'TEAM_ID': 1610612752, 'GP': 5, 'PTS': 500, 'STL': 40, 'BLK': 20, 'OREB': 60, 'DREB': 160, 'PF': 90}
        rec = normalize_records([row], 'Team totals', self.scope, '2026-27', team_ids={'1610612752': 'NYK'})[0]
        self.assertEqual(rec['entity'], 'NYK')
        self.assertEqual(rec['fields']['stl'], 40)
        self.assertEqual(rec['fields']['oreb'], 60)
        self.assertEqual(normalize_records([row], 'Team totals', self.scope, '2026-27', team_ids={}), [])

    def test_named_pair_help_shot_makes_cannot_exceed_attempts_on_import(self):
        pairs=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())['pairs']
        pair=pairs[0]
        row=self.record({'help_fgm':2,'help_fga':1},category='Named pair',
                        entity=pair['offense_player']+' | '+pair['defense_player'])
        with self.assertRaisesRegex(ValueError,'made shots exceed'):
            import_tracking(self.store,self.document(row),NOW)

    def test_missing_fields_are_archived_without_inheriting_new_gp_or_timestamp(self):
        old = self.record()
        new = self.record({'potential_assists': 50}, gp=5, published_at='2026-10-31T15:00:00+00:00', data_through_date=None)
        result = preserve_sparse_fields(new, old)
        self.assertEqual(result['fields'], {'potential_assists': 50})
        self.assertEqual(result['retained_fields']['passes']['value'], 180)
        self.assertEqual(result['retained_fields']['passes']['gp'], 4)
        self.assertEqual(result['retained_fields']['passes']['published_at'], old['published_at'])
        self.assertEqual(result['retained_fields']['passes']['data_through_date'], '2026-10-28')
        active_provenance = result.get('field_provenance', {}).get('potential_assists', result)
        self.assertEqual(active_provenance['gp'], 5)
        self.assertEqual(active_provenance['published_at'], new['published_at'])
        self.assertNotIn('retained_fields', preserve_sparse_fields(old, result))

    def test_import_preserves_missing_fields_even_without_collector_history(self):
        import_tracking(self.store, self.document(self.record()), NOW)
        new = self.record({'potential_assists': 50}, gp=5, published_at='2026-10-31T15:00:00+00:00')
        import_tracking(self.store, self.document(new), NOW)
        result = self.saved()
        self.assertEqual(result['fields'], {'potential_assists': 50})
        self.assertEqual(result['retained_fields']['assists']['value'], 20)
        self.assertEqual(result['retained_fields']['assists']['gp'], 4)

    def test_shared_provenance_does_not_overflow_transport_for_scoped_pair_records(self):
        # All 928 authorized pairs may carry the original twenty counting fields.
        # Preserve the metadata once per row; a per-field copy needlessly pushes
        # the combined mirror plus pair publication towards its 8 MB limit.
        source=self.record({f'metric_{i}':i for i in range(20)},
                           retrieved_at=NOW.isoformat(),source_published_at='2026-10-29T15:00:00+00:00',
                           publication_time_kind='publisher_modified')
        common={key:source.get(key) for key in ('source','published_at','source_published_at',
                   'publication_time_kind','retrieved_at','data_through_date','coverage_basis','gp','per_mode')}
        source['field_provenance']={field:dict(common) for field in source['fields']}
        compact=preserve_sparse_fields(source,None)
        self.assertNotIn('field_provenance',compact)
        self.assertLess(len(json.dumps([compact]*928,indent=2).encode()),2_000_000)

    def test_genuinely_different_active_field_provenance_is_preserved(self):
        source=self.record(field_provenance={'passes':{'gp':3,'published_at':'2026-10-28T15:00:00+00:00'}})
        result=preserve_sparse_fields(source,None)
        self.assertEqual(result['field_provenance']['passes']['gp'],3)
        self.assertEqual(result['field_provenance']['passes']['source'],source['source'])
        self.assertNotIn('potential_assists',result['field_provenance'])

    def test_older_publication_does_not_replace_newer_record(self):
        current = self.record({'potential_assists': 50}, published_at='2026-10-31T15:00:00+00:00')
        import_tracking(self.store, self.document(current), NOW)
        import_tracking(self.store, self.document(self.record()), NOW)
        self.assertEqual(self.saved()['fields'], {'potential_assists': 50})

    def test_retained_values_are_validated(self):
        bad = self.record(retained_fields={'invented_metric': {'value': 5}})
        with self.assertRaises(ValueError):
            import_tracking(self.store, self.document(bad), NOW)
        bad = self.record(retained_fields={'passes': {'value': 5, 'published_at': '2027-01-01'}})
        with self.assertRaises(ValueError):
            import_tracking(self.store, self.document(bad), NOW)

    def test_failed_direct_source_keeps_mirror_and_has_separate_cooldown(self):
        previous = self.document(self.record())
        previous['coverage'] += [{'category': 'Overall', 'status': 'unavailable'}]
        path = self.root / 'existing_tracking.json'
        path.write_text(json.dumps(previous))
        class Refused:
            now = NOW
            calls = 0
            async def request(inner, url):
                inner.calls += 1
                raise RuntimeError('publisher refused')
        collector = Refused()
        asyncio.run(supplement_tracking(collector, self.root, ['Overall']))
        result = json.loads(path.read_text())
        self.assertEqual(result['records'][0], previous['records'][0])
        self.assertEqual(next(c for c in result['coverage'] if c['category'] == 'Passing')['status'], 'collected')
        self.assertEqual(collector.calls, 1)
        asyncio.run(supplement_tracking(collector, self.root, ['Overall']))
        self.assertEqual(collector.calls, 1)
        status = json.loads(path.read_text())['direct_tracking']
        self.assertEqual(status['state'], 'cooldown')
        self.assertEqual(status['last_attempt'], NOW.isoformat())
        self.assertEqual(status['next_eligible_attempt'], (NOW + dt.timedelta(hours=6)).isoformat())
        self.assertEqual(status['category_attempts']['Overall']['attempts'][0]['error_type'], 'RuntimeError')

    def test_mirror_refresh_does_not_hide_direct_failure_or_restart_cooldown(self):
        previous = self.document(self.record(data_through_date=None))
        attempted = NOW - dt.timedelta(hours=2)
        failure = {'status': 'unavailable', 'last_attempt': attempted.isoformat(), 'records': 0,
                   'attempts': [{'status': 'unavailable', 'http_status': 403, 'endpoint': 'leaguedashptdefend'}]}
        previous['direct_tracking_attempted_at'] = attempted.isoformat()
        previous['direct_tracking'] = {'category_attempts': {'Overall': failure}}
        # The independently published mirror reports its own availability and
        # does not carry the previous direct endpoint error in coverage.
        previous['coverage'] += [{'category': 'Overall', 'status': 'unavailable',
                                 'reason': 'No current-season extract in this publication.'}]
        path = self.root / 'existing_tracking.json'
        path.write_text(json.dumps(previous))
        class NoRequest:
            now = NOW
            async def request(inner, url):
                self.fail('The direct source must not be requested during its cooldown.')
        asyncio.run(supplement_tracking(NoRequest(), self.root, ['Overall']))
        saved = json.loads(path.read_text())
        self.assertEqual(saved['records'], previous['records'])
        self.assertEqual(saved['last_attempt'], previous['last_attempt'])
        self.assertEqual(saved['direct_tracking_attempted_at'], attempted.isoformat())
        self.assertEqual(saved['direct_tracking']['category_attempts']['Overall'], failure)
        self.assertEqual(saved['direct_tracking']['next_eligible_attempt'],
                         (attempted + dt.timedelta(hours=6)).isoformat())
        import_tracking(self.store, saved, NOW)
        checkpoint = self.store.get_checkpoint('tracking_refresh_status')
        self.assertEqual(checkpoint['direct_tracking']['state'], 'cooldown')
        self.assertEqual(checkpoint['direct_tracking']['category_attempts']['Overall'], failure)
        self.assertIsNone(checkpoint['source_data_through_date'])

    def test_direct_retry_status_is_due_at_boundary_without_claiming_new_attempt(self):
        document = {'direct_tracking_attempted_at': (NOW - dt.timedelta(hours=6)).isoformat()}
        status = direct_tracking_status(document, NOW)
        self.assertEqual(status['state'], 'due')
        self.assertEqual(status['last_attempt'], document['direct_tracking_attempted_at'])
        self.assertEqual(direct_tracking_status({}, NOW)['state'], 'never_attempted')

    def test_direct_supplement_keeps_validated_expanded_roster_revision(self):
        from dataset_refresh import scope_revision
        from unittest.mock import patch
        expanded = copy.deepcopy(self.scope)
        expanded['players'].append({'player': 'New Rookie', 'player_id': 'name:newrookie',
                                    'espn_player_id': '999999999', 'aliases': ['New Rookie']})
        expanded['revision'] = scope_revision(expanded)
        manifest = {'schema_version': 1, 'base_revision': self.scope['revision'],
                    'season': '2026-27', 'scope': expanded, 'upstream_full_game_validated': True,
                    'source_url': 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_2027.parquet',
                    'source_published_at': '2026-10-31T15:00:00+00:00',
                    'source_checked_at': NOW.isoformat(), 'source_data_through_date': '2026-10-30',
                    'source_content_sha256': 'a' * 64}
        (self.root / 'existing_dataset_scope.json').write_text(json.dumps(manifest))
        previous = self.document(self.record())
        previous['scope_revision'] = expanded['revision']
        path = self.root / 'existing_tracking.json'
        path.write_text(json.dumps(previous))
        class Source:
            now = NOW
            async def request(inner, url):
                return {'resultSets': [{'headers': ['PLAYER_NAME', 'G', 'CONTESTED_SHOTS'],
                                        'rowSet': [['New Rookie', 1, 8]]}]}
        with patch('advanced_refresh.load_scope', return_value=self.scope):
            asyncio.run(supplement_tracking(Source(), self.root, ['Hustle']))
        document = json.loads(path.read_text())
        self.assertEqual(document['scope_revision'], expanded['revision'])
        self.assertEqual(next(row for row in document['records'] if row['category'] == 'Hustle')['entity'], 'New Rookie')
        self.store.set_checkpoint('dataset_refresh_scope', expanded)
        self.assertEqual(import_tracking(self.store, document, NOW), 2)
        # Without the validating roster manifest, refuse to regress the revision.
        (self.root / 'existing_dataset_scope.json').unlink()
        before = path.read_bytes()
        with patch('advanced_refresh.load_scope', return_value=self.scope), self.assertRaisesRegex(ValueError, 'validated scope'):
            asyncio.run(supplement_tracking(Source(), self.root, ['Hustle']))
        self.assertEqual(path.read_bytes(), before)

    def test_old_release_manifest_cannot_block_matching_bundled_snapshot(self):
        # A new release may have an authorized bundled snapshot while the old
        # dynamic-scope manifest has not yet been replaced by a game refresh.
        previous = self.document(self.record())
        path = self.root / 'existing_tracking.json'
        path.write_text(json.dumps(previous))
        (self.root / 'existing_dataset_scope.json').write_text(json.dumps({'base_revision': 'prior-release'}))
        player_id = self.player['nba_player_id']
        class Source:
            now = NOW
            async def request(inner, url):
                return {'resultSets': [{'headers': ['PLAYER_ID', 'G', 'CONTESTED_SHOTS'],
                                        'rowSet': [[player_id, 1, 8]]}]}
        asyncio.run(supplement_tracking(Source(), self.root, ['Hustle']))
        document = json.loads(path.read_text())
        self.assertEqual(document['scope_revision'], self.scope['revision'])
        self.assertTrue(any(row['category'] == 'Hustle' for row in document['records']))

    def test_direct_success_has_retrieval_time_without_invented_publication_or_coverage(self):
        player_id = self.player['nba_player_id']
        class Source:
            now = NOW
            async def request(inner, url):
                return {'resultSets': [{'headers': ['PLAYER_ID', 'G', 'CONTESTED_SHOTS', 'DEFLECTIONS'],
                                        'rowSet': [[player_id, 5, 22, 6]]}]}
        asyncio.run(supplement_tracking(Source(), self.root, ['Hustle']))
        doc = json.loads((self.root / 'existing_tracking.json').read_text())
        row = doc['records'][0]
        self.assertEqual(row['publication_time_kind'], 'retrieval_observed')
        self.assertIsNone(row['source_published_at'])
        self.assertIsNone(row['data_through_date'])
        self.assertEqual(row['retrieved_at'], NOW.isoformat())
        import_tracking(self.store, doc, NOW)
        self.assertEqual(self.saved()['fields']['contested_shots'], 22)

    def test_successful_direct_category_refreshes_again_after_its_cooldown(self):
        player_id = self.player['nba_player_id']
        class Source:
            now = NOW
            calls = 0
            async def request(inner, url):
                inner.calls += 1
                return {'resultSets': [{'headers': ['PLAYER_ID', 'G', 'CONTESTED_SHOTS', 'DEFLECTIONS'],
                                        'rowSet': [[player_id, 5, 22, inner.calls]]}]}
        source=Source()
        asyncio.run(supplement_tracking(source, self.root, ['Hustle']))
        self.assertEqual(source.calls,1)
        source.now=NOW+dt.timedelta(hours=7)
        asyncio.run(supplement_tracking(source, self.root, ['Hustle']))
        self.assertEqual(source.calls,2)
        row=json.loads((self.root/'existing_tracking.json').read_text())['records'][0]
        self.assertEqual(row['fields']['deflections'],2)

    def test_named_pair_duration_preserves_minutes_seconds(self):
        pair=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())['pairs'][0]
        source={'OFF_PLAYER_NAME':pair['offense_player'],'DEF_PLAYER_NAME':pair['defense_player'],
                'GP':2,'MATCHUP_MIN':'12:30','MATCHUP_FGM':4,'MATCHUP_FGA':10}
        row=normalize_records([source],'Named pair',self.scope,'2026-27',[pair])[0]
        self.assertEqual(row['fields']['matchup_minutes'],12.5)
        self.assertEqual(row['fields']['fg_pct'],.4)


if __name__ == '__main__':
    unittest.main()
