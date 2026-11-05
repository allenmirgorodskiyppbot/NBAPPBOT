"""Source clocks, authority and missing team submissions cannot be invented."""
import datetime as dt
import hashlib
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from master import MasterStore
from structured_availability import (normalize_nba_injuries, normalize_espn_injuries,
    normalize_official_pdf, build_availability_document, import_availability_document,
    current_official_availability, load_availability_document, _identity)
from structured_availability import collect_availability

NOW = dt.datetime(2026, 11, 5, 15, tzinfo=dt.timezone.utc)
NBA = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_injury_report/injury_report_2027.parquet'
ESPN = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_injuries/injuries_2027.parquet'
PDF = 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-05_09_45AM.pdf'


class StructuredAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MasterStore(Path(self.folder.name) / 'master.sqlite')

    def official(self, **updates):
        row = {'season': 2027, 'report_datetime': dt.datetime(2026, 11, 4),
            'game_date': '2026-11-05', 'game_time': '09:00 (ET)', 'away_team_abbr': 'ORL',
            'home_team_abbr': 'PHX', 'team': 'Orlando Magic', 'report_player_name': 'Banchero, Paolo',
            'current_status': 'Out', 'reason': 'Injury/Illness - Right High Ankle Sprain',
            'person_id': 1631094, 'game_id': '0022600187', 'match_method': 'exact'}
        row.update(updates)
        return normalize_nba_injuries([row], NBA, '2026-11-04T15:05:51Z', 'a' * 64, NOW)[0]

    def pdf(self, **updates):
        row = {'game_date': '2026-11-05', 'away_team': 'ORL', 'home_team': 'PHX',
            'team': 'ORL', 'player': 'Paolo Banchero', 'status': 'Out', 'reason': 'Right high ankle sprain'}
        row.update(updates)
        return normalize_official_pdf([row], source_url=PDF, report_at='2026-11-05T09:45:00-05:00',
                                      text_sha256='b' * 64, now=NOW)[0]

    def save(self, rows):
        return import_availability_document(self.store, build_availability_document(rows, now=NOW))

    def test_official_date_only_retains_unknown_clock_and_does_not_certify_current_status(self):
        row = self.official()
        self.assertEqual(row['player'], 'Paolo Banchero')
        self.assertEqual(row['report_date'], '2026-11-04')
        self.assertIsNone(row['report_at'])
        self.assertEqual(row['report_time_precision'], 'calendar_date_only')
        self.save([row])
        self.assertFalse(current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)['known'])

    def test_unsubmitted_team_is_not_a_healthy_player(self):
        row = self.official(report_player_name=None, current_status=None, reason='NOT YET SUBMITTED', person_id=None)
        self.assertIsNone(row['player'])
        self.assertEqual(row['status'], 'Not Yet Submitted')
        self.assertFalse(row['team_report_submitted'])
        self.save([row])
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)
        self.assertFalse(result['known'])
        self.assertIsNone(result['status'])

    def test_naive_source_clock_uses_documented_publisher_timezone_and_preserves_raw(self):
        row = self.official(report_datetime=dt.datetime(2026, 11, 4, 9, 15))
        self.assertEqual(row['report_at'], '2026-11-04T09:15:00-05:00')
        self.assertEqual(row['report_time_precision'], 'source_clock_with_documented_timezone')
        self.assertEqual(row['report_datetime_raw'], '2026-11-04 09:15:00')
        self.assertEqual(row['report_timezone'], 'America/New_York')
        self.assertEqual(row['time_basis'], 'official_publisher_ET_convention')
        self.save([row])

    def test_documented_publisher_timezone_obeys_both_sides_of_daylight_saving(self):
        october = self.official(report_datetime=dt.datetime(2026, 10, 20, 9, 15))
        november = self.official(report_datetime=dt.datetime(2026, 11, 3, 17))
        self.assertEqual(october['report_at'], '2026-10-20T09:15:00-04:00')
        self.assertEqual(november['report_at'], '2026-11-03T17:00:00-05:00')
        self.save([october, november])

    def test_documented_clock_requires_explicit_unaltered_timezone_provenance(self):
        row = self.official(report_datetime=dt.datetime(2026, 11, 4, 9, 15))
        row['time_basis_source_url'] = 'https://unverified.example/'
        row['record_id'] = _identity(row)
        with self.assertRaisesRegex(ValueError, 'timezone provenance'):
            self.save([row])

    def test_source_and_collection_clocks_cannot_come_from_the_future(self):
        with self.assertRaisesRegex(ValueError, 'future'):
            self.official(report_datetime=dt.datetime(2026, 11, 5, 12))
        row = self.pdf()
        row['retrieved_at'] = (NOW + dt.timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'retrieval clock'):
            import_availability_document(self.store, build_availability_document([row], now=NOW), now=NOW)
        row = self.pdf()
        row['source_published_at'] = (NOW + dt.timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'source clock'):
            self.save([row])

    def test_family_match_does_not_claim_an_exact_nba_identity(self):
        row = self.official(match_method='family')
        self.assertIsNone(row['nba_player_id'])
        self.assertEqual(row['identity_match_method'], 'family')

    def test_team_suffixes_do_not_confuse_lakers_and_76ers(self):
        row = self.official(team='Los Angeles Lakers', away_team_abbr='LAL')
        self.assertEqual(row['team'], 'LAL')
        row = self.official(team='Philadelphia 76ers', away_team_abbr='PHI')
        self.assertEqual(row['team'], 'PHI')
        row = self.official(team='Charlotte Hornets', away_team_abbr='CHA')
        self.assertEqual(row['team'], 'CHA')

    def test_explicit_fresh_official_out_status_is_usable_but_not_starting_or_minutes_proof(self):
        row = self.pdf()
        self.save([row])
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)
        self.assertTrue(result['known'])
        self.assertTrue(result['blocked'])
        self.assertEqual(result['status'], 'Out')
        self.assertIsNone(result['expected_minutes'])
        self.assertIsNone(result['confirmed_starter'])
        self.assertIsNone(row['source_content_sha256'])
        self.assertEqual(row['source_text_sha256'], 'b' * 64)

    def test_stale_future_or_wrong_game_designations_are_not_current(self):
        self.save([self.pdf()])
        self.assertFalse(current_official_availability(self.store, 'Paolo Banchero', '2026-11-06', NOW)['known'])
        self.assertFalse(current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW + dt.timedelta(hours=7))['known'])
        self.assertFalse(current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW - dt.timedelta(hours=1))['known'])

    def test_conflicting_exact_report_statuses_remain_unresolved(self):
        self.save([self.pdf(), self.pdf(status='Available')])
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)
        self.assertFalse(result['known'])
        self.assertIn('conflict', result['reason'])

    def test_newer_date_only_conflict_cannot_leave_an_old_available_designation_current(self):
        old = self.pdf(status='Available')
        newer = self.official(report_datetime=dt.datetime(2026, 11, 5))
        newer['source_published_at'] = '2026-11-05T14:55:00Z'
        self.save([old, newer])
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)
        self.assertFalse(result['known'])
        self.assertIn('report clock is unknown', result['reason'])

    def test_newer_unsubmitted_team_report_cannot_leave_old_availability_current(self):
        old = self.pdf(status='Available')
        newer = self.official(report_datetime=dt.datetime(2026, 11, 5, 9, 55),
            report_player_name=None, current_status=None, reason='NOT YET SUBMITTED', person_id=None)
        self.save([old, newer])
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW)
        self.assertFalse(result['known'])
        self.assertIn('not yet been submitted', result['reason'])

    def test_retrieving_an_old_report_again_does_not_make_it_latest_or_fresh(self):
        latest = self.official(report_datetime=dt.datetime(2026, 11, 5, 9, 55), current_status='Out')
        older = self.pdf(status='Available')
        older['retrieved_at'] = (NOW + dt.timedelta(hours=1)).isoformat()
        import_availability_document(self.store, build_availability_document([latest, older], now=NOW),
                                     now=NOW + dt.timedelta(hours=1))
        result = current_official_availability(self.store, 'Paolo Banchero', '2026-11-05', NOW + dt.timedelta(hours=1))
        self.assertTrue(result['blocked'])
        self.assertEqual(result['report_at'], latest['report_at'])

    def test_pdf_clock_must_match_actual_file(self):
        with self.assertRaisesRegex(ValueError, 'clock disagree'):
            normalize_official_pdf([], source_url=PDF, report_at='2026-11-05T10:00:00-05:00',
                                   text_sha256='a' * 64, now=NOW)

    def test_espn_snapshot_remains_third_party_and_not_game_specific(self):
        row = {'as_of_date': '2026-11-04', 'league': 'nba', 'season': 2027,
            'team_display_name': 'Atlanta Hawks', 'athlete_id': 3146557,
            'athlete_display_name': 'Jock Landale', 'status': 'Out', 'injury_id': 537471,
            'injury_date': '2026-11-03T22:08Z', 'detail_type': 'Hip', 'detail_location': 'Torso',
            'detail_side': 'Left', 'detail_return_date': '2026-11-06', 'short_comment': 'Reported injury'}
        value = normalize_espn_injuries([row], ESPN, '2026-11-04T20:20:13Z', 'c' * 64, NOW)[0]
        self.assertFalse(value['official_report'])
        self.assertIsNone(value['game_id'])
        self.assertIsNone(value['game_date'])
        self.assertEqual(value['report_time_precision'], 'injury_item_update_not_game_report')
        self.save([value])
        self.assertFalse(current_official_availability(self.store, 'Jock Landale', '2026-11-05', NOW)['known'])

    def test_import_is_idempotent_and_preserves_stats_ledger_and_readiness(self):
        self.store.set_checkpoint('nba_schedule_today', {'status': 'failed', 'source': 'original'})
        before = self.store.get_checkpoint('nba_schedule_today')
        row = self.pdf()
        first = self.save([row, row])
        self.assertEqual(first['records_added'], 1)
        self.assertEqual(self.save([row])['records_added'], 0)
        self.assertEqual(self.store.get_checkpoint('nba_schedule_today'), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 0)

    def test_publication_hash_and_retrieval_changes_do_not_duplicate_same_facts(self):
        row=self.official(report_datetime=dt.datetime(2026,11,4,9,15))
        self.save([row])
        changed={**row,'source_content_sha256':'d'*64,'source_published_at':'2026-11-05T14:50:00Z',
                 'retrieved_at':NOW.isoformat()}
        self.assertEqual(_identity(row),_identity(changed))
        self.assertEqual(self.save([changed])['records_added'],0)
        with self.store.connect() as db:
            saved=json.loads(db.execute('SELECT payload_json FROM structured_availability').fetchone()[0])
        self.assertEqual(saved,row)

    def test_player_and_unsubmitted_lookups_use_indexes_instead_of_scanning_archive(self):
        self.save([self.pdf(),self.pdf(player=None,status='Not Yet Submitted')])
        with self.store.connect() as db:
            for sql,args in [("SELECT payload_json FROM structured_availability WHERE game_date=? AND player_id IN (?)",('2026-11-05','paolo-banchero')),
                    ("SELECT payload_json FROM structured_availability WHERE game_date=? AND player_id IS NULL AND team=? AND status='Not Yet Submitted'",('2026-11-05','ORL'))]:
                plan=' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN '+sql,args))
                self.assertIn('SEARCH structured_availability USING INDEX',plan)
                self.assertNotIn('SCAN structured_availability',plan)

    def test_invalid_last_record_rolls_back_every_new_record(self):
        good = self.pdf()
        invalid = self.pdf(status='Available')
        invalid['expected_minutes'] = 35
        invalid['record_id'] = _identity(invalid)
        with self.assertRaisesRegex(ValueError, 'do not verify lineups'):
            self.save([good, invalid])
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='structured_availability'").fetchone()
            self.assertFalse(exists)

    def test_unsupported_status_publisher_and_digest_are_rejected(self):
        row = self.pdf()
        row['status'] = 'Healthy'
        row['record_id'] = _identity(row)
        with self.assertRaisesRegex(ValueError, 'Unsupported official'):
            self.save([row])
        row = self.pdf(); row['source_url'] = 'https://untrusted.example/report.pdf'; row['record_id'] = _identity(row)
        with self.assertRaisesRegex(ValueError, 'publisher'):
            self.save([row])
        row = self.pdf(); row['reason'] = 'altered without digest'
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.save([row])

    def test_compressed_relay_retains_provenance_and_rejects_invalid_payload(self):
        document = build_availability_document([self.pdf()], now=NOW)
        payload = gzip.compress(json.dumps(document).encode())
        restored = load_availability_document(payload)
        self.assertEqual(restored, document)
        self.assertEqual(self.save(restored['records'])['records_added'], 1)
        with self.assertRaises(ValueError):
            load_availability_document(b'not json')
        with self.assertRaisesRegex(ValueError, 'unexpected population'):
            load_availability_document(b'{"kind":"not_availability"}')
        with patch('structured_availability.MAX_DOCUMENT_BYTES', 100):
            with self.assertRaisesRegex(ValueError, 'expanded bound'):
                load_availability_document(gzip.compress(b'x' * 101))


class AvailabilityCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def mirror(self, *, fail=(), broken=False):
        official = {'season': 2027, 'report_datetime': '2026-11-04 00:00:00',
            'game_date': '2026-11-05', 'game_time': '09:00 (ET)', 'away_team_abbr': 'ORL',
            'home_team_abbr': 'PHX', 'team': 'Orlando Magic', 'report_player_name': 'Banchero, Paolo',
            'current_status': 'Out', 'reason': 'Right high ankle sprain', 'match_method': 'exact', 'person_id': 1631094}
        espn = {'as_of_date': '2026-11-04', 'league': 'nba', 'season': 2027,
            'team_display_name': 'Orlando Magic', 'athlete_id': 4432573,
            'athlete_display_name': 'Paolo Banchero', 'status': 'Out', 'injury_date': '2026-11-03T22:08Z'}
        if broken:
            official['team'] = 'Unknown franchise'
        class Mirror:
            end_year = 2027
            async def asset(self, tag, name):
                if tag in fail:
                    raise TimeoutError('synthetic unavailable source')
                rows = [official] if tag == 'nba_stats_injury_report' else [espn]
                return json.dumps(rows).encode(), {'browser_download_url': NBA if tag == 'nba_stats_injury_report' else ESPN,
                                                  'updated_at': '2026-11-04T15:05:51Z'}
        return Mirror()

    async def test_collector_writes_bounded_gzip_without_promoting_report_clocks(self):
        result = await collect_availability(self.mirror(), self.root, NOW, row_loader=json.loads)
        self.assertEqual(result['status'], 'updated')
        doc = load_availability_document((self.root / 'structured_availability.json.gz').read_bytes())
        self.assertEqual(len(doc['records']), 2)
        official = next(row for row in doc['records'] if row['official_report'])
        self.assertIsNone(official['report_at'])
        self.assertEqual(official['report_date'], '2026-11-04')

    async def test_source_failure_retains_original_evidence_and_does_not_freshen_it(self):
        await collect_availability(self.mirror(), self.root, NOW, row_loader=json.loads)
        before = load_availability_document((self.root / 'structured_availability.json.gz').read_bytes())
        result = await collect_availability(self.mirror(fail=('nba_stats_injury_report', 'espn_nba_injuries')),
            self.root, NOW + dt.timedelta(hours=1), row_loader=json.loads)
        after = load_availability_document((self.root / 'structured_availability.json.gz').read_bytes())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(before['records'], after['records'])
        self.assertTrue(all(row['status'] == 'unavailable' for row in result['coverage']))

    async def test_malformed_new_source_retains_prior_valid_category(self):
        await collect_availability(self.mirror(), self.root, NOW, row_loader=json.loads)
        before = load_availability_document((self.root / 'structured_availability.json.gz').read_bytes())
        result = await collect_availability(self.mirror(broken=True), self.root, NOW + dt.timedelta(minutes=5), row_loader=json.loads)
        self.assertEqual(result['status'], 'partial')
        after = load_availability_document((self.root / 'structured_availability.json.gz').read_bytes())
        old = [row for row in before['records'] if row['source_kind'] == 'official_extract']
        current = [row for row in after['records'] if row['source_kind'] == 'official_extract']
        self.assertEqual(old, current)


if __name__ == '__main__':
    unittest.main()
