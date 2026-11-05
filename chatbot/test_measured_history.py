"""Runtime measured-history validation, rollback, identity and query coverage."""
import copy
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from master import MasterStore
import measured_history as mh
from matchup_queries import answer_matchup_question

NOW = dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc)
SCOPE = {'revision': 'scope-test', 'players': [
    {'player': 'Jalen Brunson', 'nba_player_id': '1628973'},
    {'player': 'Tyrese Maxey', 'nba_player_id': '1630178'}]}
PAIRS = {'pairs': [{'offense_player': 'Tyrese Maxey', 'defense_player': 'Jalen Brunson'}]}


def observation(game='0022600001', day='2026-10-20', phase='Regular Season', made=2, attempts=4):
    year = 2000 + int(game[3:5]); season = f'{year}-{(year+1)%100:02d}'
    return {**{key: 0 for key in mh.COUNTS}, 'game_id': game, 'date': day,
        'season': season, 'season_type': phase, 'offense_player': 'Tyrese Maxey',
        'defense_player': 'Jalen Brunson', 'offense_nba_player_id': '1630178',
        'defense_nba_player_id': '1628973', 'pair_observation_id': game+'_1630178_1628973',
        'fgm': made, 'fga': attempts, 'matchup_seconds': 30, 'partial_possessions': 3.5,
        'published_at': '2026-11-04T15:00:00Z', 'retrieved_at': '2026-11-05T15:00:00Z',
        'source_content_sha256': 'a'*64,
        'source': f'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_game_matchups/game_matchups_{year+1}.parquet',
        'date_mapping_sources': [{'source': f'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_schedules/nba_schedule_{year+1}.parquet',
                                 'source_content_sha256': 'b'*64, 'source_published_at': '2026-11-03T15:00:00Z'}]}


class EmptyWorkbook:
    sheets = {}


class MeasuredHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.store = MasterStore(self.root/'master.sqlite')

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, rows=None, **changes):
        rows = [observation()] if rows is None else rows
        body = gzip.compress(b''.join((mh._json({'kind':'measured_pair','data':r})+'\n').encode() for r in rows),mtime=0)
        digest = hashlib.sha256(body).hexdigest(); name = f'tracking_pair_history_{digest[:12]}_001.jsonl.gz'
        (self.root/name).write_bytes(body)
        manifest = {'schema_version':1,'format':'nba-measured-pair-history-jsonl-gzip-v1',
            'scope_revision':SCOPE['revision'],'pair_scope_sha256':hashlib.sha256(mh._json(PAIRS).encode()).hexdigest(),
            'parts':[{'file':name,'bytes':len(body),'sha256':digest,'records':len(rows)}],
            'measured_pair_games':len(rows),'season_aggregate_records':0,
            'first_observed_game_date':min((r['date'] for r in rows),default=None),
            'last_observed_game_date':max((r['date'] for r in rows),default=None),**changes}
        (self.root/mh.MANIFEST).write_text(json.dumps(manifest));return manifest

    def refresh(self):
        return mh.refresh_measured_history(self.store,self.root,client=object(),scope=SCOPE,pair_scope=PAIRS,now=NOW)

    def test_actual_observation_imports_with_source_and_fractional_exposure(self):
        self.bundle();r=self.refresh();self.assertEqual(r['added'],1)
        saved=mh.measured_records(self.store)[0];self.assertEqual(saved['partial_possessions'],3.5)
        self.assertEqual(saved['date'],'2026-10-20');self.assertEqual(saved['source_content_sha256'],'a'*64)

    def test_repeat_import_and_new_retrieval_do_not_duplicate(self):
        self.bundle();self.refresh();row=observation();row['retrieved_at']='2026-11-05T16:00:00Z'
        self.bundle([row]);self.assertEqual(self.refresh()['added'],0);self.assertEqual(len(mh.measured_records(self.store)),1)

    def test_known_count_conflict_is_flagged_instead_of_summed(self):
        self.bundle();self.refresh();self.bundle([observation(made=3)]);self.assertEqual(self.refresh()['conflicts'],1)
        row=mh.measured_records(self.store)[0];self.assertTrue(row['_measured_conflict']);self.assertEqual(row['fgm'],2)

    def test_malformed_second_record_rolls_back_entire_import(self):
        bad=observation('0022600002');bad['fga']=-1
        self.bundle([observation(),bad]);self.assertEqual(self.refresh()['phase'],'unavailable');self.assertEqual(mh.measured_records(self.store),[])

    def test_scope_revision_mismatch_is_rejected(self):
        self.bundle(scope_revision='other');self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_pair_definition_hash_mismatch_is_rejected(self):
        self.bundle(pair_scope_sha256='0'*64);self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_checksum_failure_preserves_existing_rows(self):
        self.bundle();self.refresh();m=self.bundle([observation('0022600002')]);(self.root/m['parts'][0]['file']).write_bytes(b'bad')
        self.assertEqual(self.refresh()['phase'],'unavailable');self.assertEqual(len(mh.measured_records(self.store)),1)

    def test_manifest_cannot_escape_directory_or_select_other_host(self):
        m=self.bundle();m['parts'][0]['file']='../secret.jsonl.gz';(self.root/mh.MANIFEST).write_text(json.dumps(m))
        self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_namesake_with_wrong_nba_identity_is_held(self):
        r=observation();r['offense_nba_player_id']='999';self.bundle([r]);self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_all_star_cannot_be_labeled_playoffs(self):
        self.bundle([observation('0032600001',phase='Playoffs')]);self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_source_publication_cannot_date_future_game(self):
        self.bundle([observation(day='2026-11-05')]);self.assertEqual(self.refresh()['phase'],'unavailable')

    def test_measured_zero_attempts_remain_zero_without_invented_percentage(self):
        self.bundle([observation(made=0,attempts=0)]);self.assertEqual(self.refresh()['phase'],'updated')
        a=answer_matchup_question(EmptyWorkbook(),'Brunson career FG% allowed against Maxey',master=self.store)['answer']
        self.assertIn('zero attempts',a)

    def test_legacy_overlap_deduplicates_and_conflict_is_held(self):
        self.bundle();self.refresh();old=observation();old.pop('offense_nba_player_id');old.pop('defense_nba_player_id');old['game_id']='nba:2026-10-20:PHI:NYK'
        with self.store.connect() as db:
            db.execute('CREATE TABLE workbook_evidence(kind TEXT,entity TEXT,payload_json TEXT)')
            db.execute('INSERT INTO workbook_evidence VALUES(?,?,?)',('measured_pairs','Tyrese Maxey',json.dumps(old)))
        self.assertEqual(len(mh.measured_records(self.store)),1)
        old['fgm']=3
        with self.store.connect() as db:db.execute('UPDATE workbook_evidence SET payload_json=?',(json.dumps(old),))
        self.assertTrue(mh.measured_records(self.store)[0]['_measured_conflict'])

    def test_career_aliases_and_asof_keep_phases_and_dates_separate(self):
        self.bundle([observation(),observation('0022500001','2025-10-20',made=1,attempts=4),observation('0042500001','2026-05-02','Playoffs',made=4,attempts=4)])
        self.refresh();a=answer_matchup_question(EmptyWorkbook(),'Brunson lifetime FG% allowed against Maxey',master=self.store)['answer']
        self.assertIn('37.5%',a);self.assertIn('2 game(s)',a);self.assertIn('not verified complete lifetime',a)
        a=answer_matchup_question(EmptyWorkbook(),'Brunson career FG% allowed against Maxey as of 2026-01-01',master=self.store)['answer']
        self.assertIn('25.0%',a);self.assertIn('1 game(s)',a)
        a=answer_matchup_question(EmptyWorkbook(),'Brunson career FG% allowed against Maxey in playoffs',master=self.store)['answer']
        self.assertIn('100.0%',a);self.assertIn('Playoffs',a)


if __name__=='__main__':unittest.main()
