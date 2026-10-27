import json
import tempfile
import unittest
from pathlib import Path

from nbabot.core import Store,load_config
from nbabot.scope import matchup_scope,matchup_games


class MatchupScope(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        config=load_config(Path(self.tmp.name)/"none")
        config.update(data_dir=self.tmp.name,organized_players=[f"00{i}" for i in range(1,9)])
        self.store=Store(config)
        for i in range(1,9):
            self.store.db.execute("INSERT INTO players VALUES(?,?)",(f"00{i}",f"Knicks {i}"))
            self.store.db.execute("""INSERT INTO player_games(game_id,player_id,team,opponent,season,date_et,appeared)
                VALUES('0022400001',?,'NYK','BOS','2024-25','2025-01-01',1)""",(f"00{i}",))
        for pid,name in [('009','Nikola Vučević'),('010','Other Guard'),('011','Unrelated Roster Player')]:
            self.store.db.execute('INSERT INTO players VALUES(?,?)',(pid,name))
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def raw(self,row,n=1):
        self.store.db.execute("INSERT INTO raw_rows VALUES('fixture','Matchups',?,?)",(n,json.dumps(row)))
        self.store.db.commit()

    def test_exact_named_pairs_exclude_unrelated_roster(self):
        self.raw({'Knicks player':'Knicks 1','Opponent':'BOS','Suspected defender':'Nikola Vucevic',
            'Alternate defender':'Other Guard','Assignment status':'Estimated'})
        scope=matchup_scope(self.store)
        self.assertEqual(scope['by_opponent'],{'BOS':['009','010']})
        self.assertNotIn('011',scope['all_ids'])
        self.assertEqual(len(scope['pairs']),2)
        self.assertEqual(scope['unresolved'],[])
        self.assertTrue(all('not a measured assignment' in p['scope_note'] for p in scope['pairs']))

    def test_unknown_ambiguous_and_unselected_assignments_not_guessed(self):
        self.store.db.execute("INSERT INTO players VALUES('012','Other Guard')")
        self.raw({'Knicks player':'Knicks 1','Opponent':'BOS','Suspected defender':'Other Guard','Alternate defender':'Unknown Name'})
        self.raw({'Knicks player':'Unselected Knicks','Opponent':'BOS','Suspected defender':'Unrelated Roster Player'},2)
        scope=matchup_scope(self.store)
        self.assertEqual(scope['opponent_ids'],[])
        self.assertEqual({x['reason'] for x in scope['unresolved']},{'ambiguous_name','unknown_name'})

    def test_actual_fixture_history_does_not_require_home_away_metadata(self):
        games=matchup_games(self.store)
        self.assertEqual(len(games),1)
        self.assertEqual(games[0]['game_id'],'0022400001')
        self.assertEqual(games[0]['opponent'],'BOS')

    def test_playoff_scope_added_without_mixing_or_duplicate_games(self):
        row={'game_id':'0042400101','player_id':'001','team':'NYK','opponent':'BOS',
            'date_et':'2025-05-01','season':'2024-25','season_type':'Playoffs'}
        games=matchup_games(self.store,career_rows=[row,row])
        self.assertEqual(len(games),2)
        self.assertEqual(games[-1]['season_type'],'Playoffs')
        self.assertEqual(len(matchup_games(self.store,seasons=['2026-27'],career_rows=[row])),0)

    def test_conflicting_career_fixture_fails(self):
        row={'game_id':'0022400001','player_id':'001','team':'NYK','opponent':'ORL',
            'date_et':'2025-01-01','season':'2024-25','season_type':'Regular Season'}
        with self.assertRaisesRegex(ValueError,'fixture conflict'):
            matchup_games(self.store,career_rows=[row])


if __name__=='__main__':
    unittest.main()
