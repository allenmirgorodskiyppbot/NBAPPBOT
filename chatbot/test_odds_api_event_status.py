"""Replay the connected-but-unmatched game report without exposing source text."""
import unittest

from telegram_bot import prizepicks_status_reply


class OddsAPIEventStatusTests(unittest.TestCase):
    def fixture(self):
        return {'home_team': 'NYK', 'away_team': 'MIL', 'date': '2026-11-10',
                'start_time': '2026-11-11T00:30:00+00:00'}

    def test_unlisted_future_event_is_connected_waiting_and_not_a_claim_about_pp_board(self):
        text = prizepicks_status_reply({
            'provider': 'the_odds_api', 'phase': 'waiting_for_event', 'http_status': 200,
            'error_code': 'provider_event_not_listed', 'expected_game': self.fixture(),
            'provider_event_count': 12, 'provider_knicks_events': [],
            'credits_remaining': 500, 'local_credits_reserved': 0})
        self.assertIn('connected; waiting', text)
        self.assertIn('MIL at NYK', text)
        self.assertIn('Nov 10, 7:30 PM Eastern', text)
        self.assertIn('NBA events returned by provider: 12', text)
        self.assertIn('offer availability has not been checked yet', text)
        self.assertIn('Provider credits remaining: 500', text)
        self.assertNotIn('unavailable on the last check', text)
        self.assertNotIn('PrizePicks has no', text)

    def test_ambiguous_fixture_shows_normalized_observations_without_asserting_free_paid_call(self):
        event = {**self.fixture(), 'start_time': '2026-11-11T00:40:00Z'}
        text = prizepicks_status_reply({
            'provider': 'the_odds_api', 'phase': 'event_mismatch', 'http_status': 200,
            'error_code': 'event_identity_ambiguous', 'expected_game': self.fixture(),
            'provider_knicks_events': [event], 'local_credits_reserved': 5})
        self.assertIn('could not be matched unambiguously', text)
        self.assertIn('Provider Knicks listing: MIL at NYK', text)
        self.assertIn('7:40 PM Eastern', text)
        self.assertIn('Master research remains available', text)
        self.assertNotIn('No credits were reserved', text)

    def test_matching_event_with_provider_clock_difference_preserves_both_clocks(self):
        text = prizepicks_status_reply({
            'provider': 'the_odds_api', 'phase': 'ready', 'provider_timing_differs': True,
            'matched_provider_start_time': '2026-11-11T00:40:00Z',
            'saved_start_time': self.fixture()['start_time']})
        self.assertIn('Provider tipoff: Nov 10, 7:40 PM Eastern', text)
        self.assertIn('saved calendar: Nov 10, 7:30 PM Eastern', text)

    def test_diagnostic_fields_are_bounded_and_never_render_untrusted_source_text(self):
        bad = {'home_team': 'SECRET_KEY_COOKIE', 'away_team': 'NYK',
               'date': 'SECRET_KEY_COOKIE', 'start_time': 'SECRET_KEY_COOKIE'}
        text = prizepicks_status_reply({
            'provider': 'the_odds_api', 'phase': 'event_mismatch',
            'expected_game': bad, 'provider_knicks_events': [bad] * 100,
            'provider_event_count': 'SECRET_KEY_COOKIE', 'response_body': 'SECRET_KEY_COOKIE',
            'matched_provider_start_time': 'SECRET_KEY_COOKIE',
            'saved_start_time': 'SECRET_KEY_COOKIE', 'provider_timing_differs': True})
        self.assertNotIn('SECRET_KEY_COOKIE', text)
        self.assertLess(len(text), 1000)

    def test_compact_waiting_status_identifies_provider_without_hiding_master_instructions(self):
        text = prizepicks_status_reply({'provider': 'the_odds_api',
            'phase': 'waiting_for_event', 'http_status': 200,
            'error_code': 'provider_event_not_listed'}, compact=True)
        self.assertIn('PrizePicks via The Odds API: connected; waiting', text)
        self.assertIn('/pp_status', text)


if __name__ == '__main__':
    unittest.main()
