import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from feeds import canonical_player
from history_backfill import _validate_bundle_record, current_identities, history_exclusion, import_history_bundle, insert_missing, normalize_history
from master import MasterStore


def source_rows(day='2026-11-02', phase=2):
    rows = []
    for team, other, side, offset in [('NY', 'CHI', 'home', 0), ('CHI', 'NY', 'away', 10)]:
        for i in range(5):
            pid = i + offset + 1
            rows.append({'game_id': 123, 'game_date': day, 'game_date_time': day+'T19:30:00-05:00',
                'season': 2027, 'season_type': phase, 'athlete_id': pid,
                'athlete_display_name': 'Player '+str(pid), 'team_abbreviation': team,
                'opponent_team_abbreviation': other, 'home_away': side,
                'team_score': 10, 'opponent_team_score': 10, 'minutes': 40,
                'points': 2, 'rebounds': 4, 'assists': 3, 'did_not_play': False, 'starter': True})
    return rows


def identity(pid=1):
    return {'player': 'Player '+str(pid), 'player_id': canonical_player('Player '+str(pid)),
            'espn_player_id': str(pid), 'aliases': ['Player '+str(pid)]}


def normalized(rows=None):
    return normalize_history(rows or source_rows(), [identity()], 'https://example.org/source',
        '2026-11-03T12:00:00+00:00', 'a'*64, '2026-11-02')


class HistoryTests(unittest.TestCase):
    def test_complete_game_before_selection(self):
        games, rows, report = normalized()
        self.assertEqual((len(games), len(rows)), (1, 1))
        self.assertEqual(rows[0]['player_id'], 'name:player1')
        self.assertEqual(rows[0]['espn_player_id'], '1')
        self.assertFalse(report['rejected_games'])

    def test_partial_game_rejected_before_selection(self):
        games, rows, report = normalized(source_rows()[:-1])
        self.assertEqual((games, rows), ([], []))
        self.assertEqual(len(report['rejected_games']), 1)

    def test_score_conflict_rejected(self):
        raw = source_rows(); raw[0]['points'] = 20
        self.assertTrue(normalized(raw)[2]['rejected_games'])

    def test_source_game_identity_conflict(self):
        raw = source_rows(); raw[0]['game_id'] = 999
        self.assertTrue(normalized(raw)[2]['rejected_games'])

    def test_namesake_provider_not_selected(self):
        raw = source_rows(); raw[0]['athlete_id'] = 999
        games, rows, report = normalized(raw)
        self.assertEqual(rows, [])
        self.assertFalse(report['rejected_games'])

    def test_provider_alias_requires_approval(self):
        raw = source_rows(); raw[0]['athlete_display_name'] = 'Somebody Else'
        self.assertTrue(normalized(raw)[2]['rejected_games'])

    def test_empty_unnamed_nonparticipant_omitted_without_losing_game(self):
        raw = source_rows()
        placeholder = {**raw[0], 'athlete_id':None, 'athlete_display_name':None,
            'points':None, 'rebounds':None, 'assists':None, 'minutes':None,
            'did_not_play':True, 'active':False}
        raw.append(placeholder)
        games, rows, report = normalized(raw)
        self.assertEqual(len(rows),1)
        self.assertEqual(len(report['omitted_empty_nonparticipants']),1)
        self.assertFalse(report['rejected_games'])

    def test_unnamed_player_with_statistics_rejects_game(self):
        raw = source_rows()
        raw.append({**raw[0], 'athlete_id':None, 'athlete_display_name':None, 'active':False})
        self.assertTrue(normalized(raw)[2]['rejected_games'])

    def test_current_name_collision_rejected(self):
        raw = source_rows(); raw[1]['athlete_display_name'] = raw[0]['athlete_display_name']
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            current_identities(raw)

    def test_current_roster_dnp_is_identity(self):
        raw = source_rows(); raw[0]['did_not_play'] = True
        self.assertEqual(len(current_identities(raw)), 10)

    def test_phase_separation(self):
        for phase, label in [(2, 'Regular Season'), (3, 'Playoffs'), (5, 'Play-In')]:
            games, rows, report = normalized(source_rows(phase=phase))
            self.assertEqual(rows[0]['season_type'], label)

    def test_allstar_and_cup_excluded(self):
        raw = source_rows()[0]
        self.assertIsNotNone(history_exclusion({**raw, 'team_abbreviation': 'EAST'}))
        self.assertIsNotNone(history_exclusion({**raw, 'game_id':401809839, 'game_date':'2025-12-16',
            'team_abbreviation':'NY','opponent_team_abbreviation':'SA'}))

    def test_historical_franchise_accepted(self):
        self.assertIsNone(history_exclusion({**source_rows()[0], 'team_abbreviation':'SEA'}))

    def test_unknown_phase_and_future_cutoff(self):
        self.assertIsNotNone(history_exclusion({**source_rows()[0], 'season_type':9}))
        games, rows, report = normalized(source_rows(day='2026-11-03'))
        self.assertEqual(rows, [])
        self.assertTrue(report['excluded_games'])

    def test_nonfinite_stat_rejected(self):
        raw = source_rows(); raw[0]['rebounds'] = float('nan')
        self.assertTrue(normalized(raw)[2]['rejected_games'])

    def make_bundle(self, root, bad_record=False):
        games, rows, _ = normalized()
        events = [{'kind':'game','data':games[0]}, {'kind':'player_game','data':rows[0]}]
        if bad_record:
            events[-1]['data']['espn_player_id'] = '999'
        blob = gzip.compress(('\n'.join(json.dumps(x) for x in events)+'\n').encode())
        (root/'part.gz').write_bytes(blob)
        manifest = {'schema_version':1,'format':'nba-history-jsonl-gzip-v1',
            'source_data_through_date':'2026-11-02','players':[identity()],
            'parts':[{'file':'part.gz','bytes':len(blob),'sha256':hashlib.sha256(blob).hexdigest(),'records':2}]}
        path=root/'manifest.json';path.write_text(json.dumps(manifest))
        return path

    def test_idempotent_import_and_original_conflict_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=MasterStore(root/'master.sqlite')
            games, rows, _ = normalized(); old = {**rows[0], 'pts':99}
            store.upsert_game(games[0]); store.upsert_player_game(old)
            path=self.make_bundle(root)
            result=import_history_bundle(store,path)
            self.assertEqual(result['player_game_conflict_pts'],1)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT pts FROM player_games').fetchone()[0],99)
            again=import_history_bundle(store,path)
            self.assertTrue(again['already_imported'])

    def test_checksum_failure_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MasterStore(root/'master.sqlite');path=self.make_bundle(root)
            (root/'part.gz').write_bytes(b'bad')
            with self.assertRaises(ValueError): import_history_bundle(store,path)
            with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM games').fetchone()[0],0)

    def test_bad_identity_atomic_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MasterStore(root/'master.sqlite');path=self.make_bundle(root,True)
            with self.assertRaisesRegex(ValueError,'identity'): import_history_bundle(store,path)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM games').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0],0)

    def test_part_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MasterStore(root/'master.sqlite');path=self.make_bundle(root)
            doc=json.loads(path.read_text());doc['parts'][0]['file']='../part.gz';path.write_text(json.dumps(doc))
            with self.assertRaises(ValueError):import_history_bundle(store,path)

    def test_bundle_statistic_and_season_validation(self):
        games, rows, _ = normalized()
        valid=rows[0]; identities={'1':identity()}
        for patch in [{'reb':-1},{'reb':float('inf')},{'reb':True},
                      {'fgm':5,'fga':4},{'tpm':3,'tpa':2},{'ftm':4,'fta':3},
                      {'season':'2020-21'},{'season':'2026-29'}, {'minutes':0}]:
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    _validate_bundle_record('player_game',{**valid,**patch},identities,'2026-11-02')
        with self.assertRaises(ValueError):
            _validate_bundle_record('game',{**games[0],'home_score':float('nan')},identities,'2026-11-02')

    def test_existing_game_participant_conflict_blocks_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MasterStore(root/'master.sqlite');path=self.make_bundle(root)
            games,rows,_=normalized();store.upsert_game({**games[0],'home_team':'BOS'})
            with self.assertRaisesRegex(ValueError,'participants'):import_history_bundle(store,path)
            with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM player_games').fetchone()[0],0)


if __name__ == '__main__': unittest.main()
