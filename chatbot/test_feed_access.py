"""Access failures remain explicit; diagnostics never dump credentials."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from feed_diagnostics import diagnose_feeds, PUBLIC_CHECKS
from feeds import FeedWorker
from master import MasterStore
from analysis_engine import _feed_health
import datetime as dt


class FeedAccessTests(unittest.TestCase):
    def test_diagnostics_reports_refusal_without_exposing_unselected_headers(self):
        def handler(request):
            return httpx.Response(403, text='<html><title>Access Denied</title></html>',
                headers={'content-type':'text/html','server':'test-edge','set-cookie':'private-cookie-value'})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await diagnose_feeds(client)
        result=asyncio.run(run())
        self.assertEqual(len(result),len(PUBLIC_CHECKS))
        self.assertTrue(all(item['status']==403 for item in result))
        self.assertEqual(result[0]['error_summary'],'Access Denied')
        self.assertNotIn('private-cookie-value',json.dumps(result))
        self.assertIn('does not identify the cause',result[0]['meaning'])

    def test_403_cooldown_survives_restart_and_stops_same_host_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            master=MasterStore(Path(directory)/'master.sqlite');calls=[]
            def handler(request):
                calls.append(str(request.url));return httpx.Response(403,text='Access denied')
            async def run():
                with patch.dict('os.environ',{'NBA_DATA_SOURCE':'espn','NEWS_SOURCE':'espn'}):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        worker=FeedWorker(master,client=client)
                        with self.assertRaises(httpx.HTTPStatusError):await worker.request('https://site.api.espn.com/scoreboard')
                        reopened=MasterStore(Path(directory)/'master.sqlite')
                        second=FeedWorker(reopened,client=client)
                        with self.assertRaisesRegex(RuntimeError,'HTTP 403 cooldown'):
                            await second.request('https://site.api.espn.com/news')
            asyncio.run(run())
            self.assertEqual(len(calls),1)
            self.assertGreater(master.get_checkpoint('feed_http_cooldown:site.api.espn.com')['until'],time.time())

    def test_429_respects_retry_after_and_different_host_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            master=MasterStore(Path(directory)/'master.sqlite')
            def handler(request):
                if request.url.host=='feed.example':return httpx.Response(429,headers={'retry-after':'7200'})
                return httpx.Response(200,json={'ok':True})
            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    worker=FeedWorker(master,client=client)
                    with self.assertRaises(httpx.HTTPStatusError):await worker.request('https://feed.example/nba')
                    self.assertEqual(await worker.request('https://other.example/news'),{'ok':True})
            asyncio.run(run())
            self.assertGreater(master.get_checkpoint('feed_http_cooldown:feed.example')['until'],time.time()+7100)

    def test_nba_cdn_health_counts_as_data_and_disabled_espn_is_excluded(self):
        now=dt.datetime.now(dt.timezone.utc)
        health=_feed_health({'feeds':[{'name':'nba_cdn_games','last_success':now.isoformat(),'error':None},
            {'name':'espn_games','last_success':now.isoformat(),'error':'Disabled: NBA_DATA_SOURCE=nba_cdn'}]},now)
        self.assertIn('nba_cdn_games',health['fresh_feeds'])
        self.assertNotIn('espn_games',health['fresh_feeds'])
        self.assertFalse(any('Feed espn_games' in text for text in health['warnings']))


    def test_explicit_nba_and_rss_selection_uses_no_espn_requests(self):
        import copy
        from nba_cdn import SCHEDULE_URL, SCOREBOARD_URL, BOX_URL
        from test_nba_cdn import BOX_EXAMPLE
        with tempfile.TemporaryDirectory() as directory:
            master=MasterStore(Path(directory)/'master.sqlite')
            master.feed_failure('espn_games','HTTP 403')
            master.set_checkpoint('last_reconciled_date','2021-01-15')
            master.set_checkpoint('feeds_initialized',True)
            native=copy.deepcopy(BOX_EXAMPLE['game']);calls=[]
            def handler(request):
                url=str(request.url);calls.append(url)
                if url==SCHEDULE_URL:return httpx.Response(200,json={'leagueSchedule':{'seasonYear':'2020-21','gameDates':[
                    {'gameDate':'01/15/2021 00:00:00','games':[native]},
                    {'gameDate':'01/16/2021 00:00:00','games':[]}]}})
                if url==SCOREBOARD_URL:return httpx.Response(200,json={'scoreboard':{'gameDate':'2021-01-15','games':[native]}})
                if url==BOX_URL.format(native['gameId']):return httpx.Response(200,json=BOX_EXAMPLE)
                if url=='https://publisher.example/nba.xml':return httpx.Response(200,text='<rss><channel><item><guid>news1</guid><title>Knicks injury update</title><link>https://publisher.example/story</link></item></channel></rss>')
                raise AssertionError('Unexpected source request '+url)
            async def run():
                with patch.dict('os.environ',{'NBA_DATA_SOURCE':'nba_cdn','NEWS_SOURCE':'rss',
                    'NEWS_FEED_URLS':'https://publisher.example/nba.xml','INJURY_FEED_URL':''}):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        worker=FeedWorker(master,client=client,now=lambda:dt.datetime(2021,1,16,2,tzinfo=dt.timezone.utc))
                        await worker.collect_once()
            asyncio.run(run())
            self.assertFalse(any('espn.com' in url for url in calls))
            status=master.status();feeds={f['name']:f for f in status['feeds']}
            self.assertEqual(status['counts']['player_games'],2)
            self.assertEqual(status['counts']['news'],1)
            self.assertIsNotNone(feeds['nba_cdn_games']['last_success'])
            self.assertIsNotNone(feeds['rss_1']['last_success'])
            self.assertTrue(feeds['espn_games']['error'].startswith('Disabled:'))
            self.assertTrue(feeds['espn_news']['error'].startswith('Disabled:'))

    def test_manual_statistics_mode_still_scans_news_and_labels_rumors(self):
        with tempfile.TemporaryDirectory() as directory:
            master=MasterStore(Path(directory)/'master.sqlite');master.set_checkpoint('feeds_initialized',True)
            master.feed_failure('espn_games','HTTP 403');calls=[];alerts=[]
            async def notify(text,event_key=None):alerts.append(text)
            def handler(request):
                calls.append(str(request.url))
                self.assertEqual(request.url.host,'publisher.example')
                title='Initial Knicks injury report' if len(calls)==1 else 'Knicks could trade player after injury'
                return httpx.Response(200,text='<rss><channel><item><guid>inj1</guid><title>'+title+'</title><link>https://publisher.example/story</link></item></channel></rss>')
            async def run():
                with patch.dict('os.environ',{'NBA_DATA_SOURCE':'manual','NEWS_SOURCE':'rss',
                    'NEWS_FEED_URLS':'https://publisher.example/nba.xml','INJURY_FEED_URL':'','NEWS_ALERT_SCOPE':'league'}):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        worker=FeedWorker(master,notify=notify,client=client)
                        await worker.collect_once();await worker.collect_once();await worker.collect_once()
            asyncio.run(run())
            self.assertEqual(len(calls),3)
            self.assertEqual(master.status()['counts']['games'],0)
            self.assertEqual(master.status()['counts']['news'],1)
            self.assertEqual(len(alerts),1)
            self.assertIn('rumor/unconfirmed',alerts[0])
            self.assertIn('Source: publisher.example',alerts[0])
            feeds={f['name']:f for f in master.status()['feeds']}
            self.assertTrue(feeds['espn_games']['error'].startswith('Disabled:'))
            self.assertIsNotNone(feeds['rss_1']['last_success'])

    def test_promotional_trade_offer_is_archived_without_sending_trade_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            master = MasterStore(Path(directory) / 'master.sqlite')
            master.set_checkpoint('feeds_initialized', True)
            alerts = []
            async def notify(text, event_key=None):
                alerts.append(text)
            async def run():
                with patch.dict('os.environ', {'NBA_DATA_SOURCE': 'manual', 'NEWS_SOURCE': 'rss',
                                              'NEWS_ALERT_SCOPE': 'league'}):
                    worker = FeedWorker(master, notify=notify)
                    await worker.save_news({'id': 'promo',
                        'title': 'OG Promo Code ELITE: Trade $25, Get $50 Bonus',
                        'body': 'Offer for trading on NBA and NFL markets.',
                        'source': 'Elite Sports NY', 'url': 'https://elitesportsny.com/og-promo-code/'})
                    await worker.save_news({'id': 'transaction',
                        'title': 'Knicks acquire player in trade', 'body': 'A reported roster transaction.',
                        'source': 'Publisher', 'url': 'https://publisher.example/knicks-trade'})
            asyncio.run(run())
            self.assertEqual(master.status()['counts']['news'], 2)
            self.assertEqual(len(alerts), 1)
            self.assertIn('Knicks acquire player', alerts[0])
            self.assertNotIn('Promo Code', alerts[0])


if __name__=='__main__':unittest.main()
