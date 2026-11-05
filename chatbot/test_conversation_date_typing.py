"""Typed month/day dates retain their game and never become offer numbers."""
import datetime as dt
import unittest
from unittest.mock import patch

from conversation import resolve_conversation


class ConversationDateTypingTests(unittest.TestCase):
    def resolve(self, text):
        with patch('conversation._players',return_value=['Jalen Brunson']):
            return resolve_conversation(None,None,text,now=dt.datetime(2026,11,5,18,tzinfo=dt.timezone.utc))

    def test_joined_month_day_keeps_exact_game_and_offer(self):
        for date in ('Nov7','November7','Nov.7','November 7'):
            with self.subTest(date=date):
                result=self.resolve('Jalen Brunson more25.5points for '+date+'?')
                self.assertEqual(result['slots']['game_date'],'2026-11-07')
                self.assertEqual(result['slots']['line'],25.5)
                self.assertTrue(result['complete_prop'])

    def test_invalid_joined_date_is_clarified_not_silently_today(self):
        result=self.resolve('Jalen Brunson more25.5points for November32?')
        self.assertIn('invalid_date',result['errors'])
        self.assertFalse(result['complete_prop'])
        self.assertIn('valid date',result['clarification'])
