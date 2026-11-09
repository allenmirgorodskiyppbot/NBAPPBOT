"""Choose one offer source without exposing credentials or silently switching it."""
from __future__ import annotations

import asyncio
import os


def _provider():
    value = os.getenv('PRIZEPICKS_FEED_PROVIDER', 'auto').strip().lower()
    if value == 'auto':
        return 'the_odds_api' if os.getenv('THE_ODDS_API_KEY', '').strip() else 'public'
    return value if value in {'public', 'the_odds_api'} else 'invalid'


def prizepicks_source_name():
    """A safe source label for model context, with no credential or raw setting."""
    provider = _provider()
    if provider == 'invalid':
        return 'configuration_error'
    if os.getenv('PRIZEPICKS_FEED_ENABLED', '1') != '1':
        return 'manual_uploads'
    return 'the_odds_api' if provider == 'the_odds_api' else 'public_site_feed'


class InvalidPrizePicksSource:
    """A typo must never trigger requests to another source."""
    enabled = False

    def status(self):
        return {'phase': 'configuration_error', 'enabled': False,
                'source': 'configuration_error', 'configured': False,
                'error_code': 'invalid_provider',
                'detail': 'Set PRIZEPICKS_FEED_PROVIDER to auto, public, or the_odds_api.',
                'matched_offers': 0, 'readable_selections': 0}

    def offers_for_game(self, game=None, now=None):
        return {'game': game, 'fresh': False, 'offers': [], 'legs': [],
                'exclusions': [], 'status': self.status()}

    async def refresh(self, force=False, game=None):
        return self.status()

    async def ensure_for_game(self, game=None):
        return self.status()

    async def run(self):
        await asyncio.Future()

    async def aclose(self):
        pass


def create_prizepicks_feed(store, **kwargs):
    """Auto-select The Odds API when its key is set; explicit choices are final."""
    provider = _provider()
    if provider == 'the_odds_api':
        from the_odds_api_feed import TheOddsAPIFeed
        return TheOddsAPIFeed(store, **kwargs)
    if provider == 'public':
        from prizepicks_feed import PrizePicksFeed
        return PrizePicksFeed(store, **kwargs)
    return InvalidPrizePicksSource()
