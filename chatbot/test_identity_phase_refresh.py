"""Provider identity collisions and known non-regular events never become history."""
import unittest
from dataset_refresh import select_existing_rows
from game_phase import regular_phase_exclusion
from feeds import canonical_game


class IdentityPhaseTests(unittest.TestCase):
    def setUp(self):
        self.scope={'players':[{'player':'Brandon Williams','espn_player_id':'4397040','nba_player_id':'1630314'},
                               {'player':'OG Anunoby','espn_player_id':'3934719','aliases':['O.G. Anunoby']}]}

    def test_retired_same_name_player_never_falls_back_after_id_mismatch(self):
        for identity in ('932','4397040'):
            row={'player':'Brandon Williams','provider_player_id':identity,
                 'source':'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_2005.parquet'}
            result=select_existing_rows([row],self.scope)
            self.assertEqual(len(result),1 if identity=='4397040' else 0)

    def test_nba_identity_and_explicit_espn_id_are_distinct(self):
        self.assertEqual(len(select_existing_rows([{'player':'Brandon Williams','nba_player_id':'1630314',
            'provider_player_id':'1630314'}],self.scope)),1)
        self.assertEqual(select_existing_rows([{'player':'Brandon Williams','nba_player_id':'999'}],self.scope),[])
        self.assertEqual(select_existing_rows([{'player':'Brandon Williams','espn_player_id':'932'}],self.scope),[])

    def test_aliases_preserve_authorized_canonical_identity(self):
        row=select_existing_rows([{'player':'O.G. Anunoby','espn_player_id':'3934719'}],self.scope)[0]
        self.assertEqual(row['player'],'OG Anunoby');self.assertEqual(row['player_id'],'name:oganunoby')

    def test_ambiguous_name_without_provider_id_is_not_auto_merged(self):
        self.scope['players'].append({'player':'Brandon Williams','espn_player_id':'932'})
        self.assertEqual(select_existing_rows([{'player':'Brandon Williams'}],self.scope),[])

    def test_known_cup_championship_excluded_from_legacy_normalized_history(self):
        row={'date':'2025-12-16','team':'NYK','opponent':'SAS',
             'game_id':canonical_game('2025-12-16','NYK','SAS'),'season_type':'Regular Season'}
        self.assertIn('NBA Cup',regular_phase_exclusion(row)['reason'])
        row['date']='2025-12-17';self.assertIsNone(regular_phase_exclusion(row))

    def test_provider_cup_identity_conflict_is_rejected_not_silently_skipped(self):
        with self.assertRaises(ValueError):
            regular_phase_exclusion({'game_date':'2025-12-17','team_abbreviation':'NYK',
                'opponent_team_abbreviation':'SAS','game_id':'401809839','season_type':2})

    def test_allstar_and_explicit_other_phases_are_excluded(self):
        for row in ({'date':'2025-02-16','team':'SHQ','opponent':'CHK','season_type':2},
                    {'date':'2025-12-16','team':'NYK','opponent':'CHI','nba_season_type':'NBA Cup Final'}):
            self.assertIsNotNone(regular_phase_exclusion(row))
