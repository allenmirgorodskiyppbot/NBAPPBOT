"""Real official report text plus conservative parser and collector fixtures."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from official_injury_pdf import latest_report_url, parse_report_pages, collect_official_pdf
from structured_availability import load_availability_document, _identity

NOW=dt.datetime(2026,11,5,15,tzinfo=dt.timezone.utc)
PDF='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-05_09_45AM.pdf'
INDEX='https://official.nba.com/nba-injury-report-2026-27-season/'
# Actual original 09:45 ET report text observed through the connected web tool.
# These are extracted lines, not a claim that a PDF byte download succeeded here.
PAGES=['''Injury Report: 11/05/26 09:45 AM
Page 1 of 2
Game Date Game Time Matchup Team Player Name Current Status Reason
11/05/2026 09:00 (ET) ORL@PHX Orlando Magic Banchero, Paolo Out
Injury/Illness - Right High Ankle
Sprain
Isaac, Jonathan Out
Injury/Illness - Left Knee Injury
Management
Nelson, Izaiyah Out
Injury/Illness - Left Ankle Injury
Recovery
Penda, Noah Out
Injury/Illness - Right Partially Torn
Hamstring
Suggs, Jalen Out
Injury/Illness - Right Knee Contusion
/ Bone Bruise
Phoenix Suns NOT YET SUBMITTED
10:00 (ET) MIA@GSW Miami Heat NOT YET SUBMITTED
Golden State Warriors Butler III, Jimmy Out
Injury/Illness - Right ACL Repair /
Surgery
Moody, Moses Out
Injury/Illness - Left Patellar Tendon
Repair / Surgery
Porzingis, Kristaps Out Not With Team
Santos, Gui Out Injury/Illness - Left Knee Tendinitis
11/06/2026 07:00 (ET) BOS@CHA Boston Celtics NOT YET SUBMITTED
Charlotte Hornets NOT YET SUBMITTED''','''Injury Report: 11/05/26 09:45 AM
Page 2 of 2
WAS@ATL Washington Wizards NOT YET SUBMITTED
Atlanta Hawks NOT YET SUBMITTED
07:30 (ET) PHI@CLE Philadelphia 76ers NOT YET SUBMITTED
Cleveland Cavaliers NOT YET SUBMITTED
08:00 (ET) MIN@NOP Minnesota Timberwolves NOT YET SUBMITTED
New Orleans Pelicans NOT YET SUBMITTED
OKC@MEM Oklahoma City Thunder NOT YET SUBMITTED
Memphis Grizzlies NOT YET SUBMITTED
TOR@MIL Toronto Raptors NOT YET SUBMITTED
Milwaukee Bucks NOT YET SUBMITTED
UTA@DAL Utah Jazz NOT YET SUBMITTED
Dallas Mavericks NOT YET SUBMITTED
10:00 (ET) POR@LAL Portland Trail Blazers NOT YET SUBMITTED
Los Angeles Lakers NOT YET SUBMITTED
SAS@SAC San Antonio Spurs NOT YET SUBMITTED
Sacramento Kings NOT YET SUBMITTED''']


class OfficialPDFParserTests(unittest.TestCase):
    def test_actual_official_text_recovers_only_nine_out_and_twenty_unsent(self):
        result=parse_report_pages(PAGES,PDF)
        self.assertEqual(result['status'],'collected')
        self.assertEqual(len(result['rows']),29)
        self.assertEqual(sum(row['status']=='Out' for row in result['rows']),9)
        self.assertEqual(sum(row['status']=='Not Yet Submitted' for row in result['rows']),20)
        banchero=result['rows'][0]
        self.assertEqual(banchero['reason'],'Injury/Illness - Right High Ankle Sprain')
        self.assertEqual(banchero['game_time_text'],'09:00 (ET)')
        was=next(row for row in result['rows'] if row['team']=='WAS')
        self.assertEqual(was['game_date'],'2026-11-06')
        self.assertIsNone(was['game_time_text'])
        self.assertEqual(next(row for row in result['rows'] if row['player']=='Butler III, Jimmy')['team'],'GSW')

    def test_header_mismatch_or_missing_header_rejected(self):
        with self.assertRaisesRegex(ValueError,'clocks disagree'):
            parse_report_pages([PAGES[0].replace('09:45 AM','10:00 AM')],PDF)
        with self.assertRaisesRegex(ValueError,'no verified report header'):
            parse_report_pages(['11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Out'],PDF)

    def test_new_game_without_team_does_not_reuse_previous_team(self):
        text=PAGES[0]+'\nORL@PHX Banchero, Paolo Available\n'
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['rows']),13)
        self.assertTrue(result['excluded'])
        self.assertFalse(any(row['status']=='Available' for row in result['rows']))

    def test_wrapped_player_name_and_page_continuation_keep_exact_context(self):
        first='Injury Report: 11/05/26 09:45 AM\n11/05/2026 ORL@PHX Orlando Magic Banchero,\nPaolo Out\nInjury/Illness - Right'
        second='Injury Report: 11/05/26 09:45 AM\nHigh Ankle Sprain\nPhoenix Suns NOT YET SUBMITTED'
        result=parse_report_pages([first,second],PDF)
        self.assertEqual(result['status'],'collected')
        self.assertEqual(result['rows'][0]['player'],'Banchero, Paolo')
        self.assertEqual(result['rows'][0]['reason'],'Injury/Illness - Right High Ankle Sprain')
        self.assertEqual(result['rows'][1]['team'],'PHX')

    def test_same_report_conflict_excluded_and_never_inferred_healthy(self):
        text=PAGES[0]+'\n11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Available\n'
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'partial')
        self.assertFalse(any(row['player']=='Banchero, Paolo' for row in result['rows']))

    def test_ambiguous_new_matchup_cannot_become_previous_players_reason_or_team(self):
        text='''Injury Report: 11/05/26 09:45 AM
11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Out Injury/Illness - Right Ankle
WAS @ ATL Washington Wizards
Doe, John Out'''
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['reason'],'Injury/Illness - Right Ankle')

    def test_wrapped_team_heading_clears_old_team_before_later_player(self):
        text='''Injury Report: 11/05/26 09:45 AM
11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Out Injury/Illness - Right Ankle
Phoenix
Suns
Booker, Devin Out'''
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['reason'],'Injury/Illness - Right Ankle')

    def test_date_only_line_updates_date_and_clears_old_game(self):
        text='''Injury Report: 11/05/26 09:45 AM
11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Out Injury/Illness - Right Ankle
11/06/2026
07:00 (ET) BOS@CHA Boston Celtics Tatum, Jayson Out Injury/Illness - Knee'''
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'collected')
        self.assertEqual(result['rows'][1]['game_date'],'2026-11-06')
        self.assertEqual(result['rows'][1]['team'],'BOS')

    def test_unsupported_date_clears_old_date_instead_of_misdating_player(self):
        text='''Injury Report: 11/05/26 09:45 AM
11/05/2026 ORL@PHX Orlando Magic Banchero, Paolo Out Injury/Illness - Right Ankle
11/6/2026
07:00 (ET) BOS@CHA Boston Celtics Tatum, Jayson Out Injury/Illness - Knee'''
        result=parse_report_pages([text],PDF)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['rows']),1)

    def test_latest_link_ignores_bad_hosts_future_and_previous_day(self):
        html=''.join('<a href="'+url+'">report</a>' for url in [PDF,
            PDF.replace('09_45AM','11_00AM'),PDF.replace('ak-static.cms.nba.com','bad.example'),
            PDF.replace('2026-11-05','2026-11-04')])
        self.assertEqual(latest_report_url(html,INDEX,NOW),PDF)
        with self.assertRaisesRegex(ValueError,'no nonfuture current-day'):
            latest_report_url('<a href="'+PDF.replace('09_45AM','11_00AM')+'">future</a>',INDEX,NOW)


class OfficialPDFCollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def client(self,*,fail=False,wrong_mime=False,large=False):
        def handle(request):
            if request.url.host=='official.nba.com':
                return httpx.Response(200,text='<a href="'+PDF+'">09:45 ET report</a>')
            if fail:return httpx.Response(403)
            return httpx.Response(200,content=b'%PDF-fixture-no-actual-pdf-byte-claim',
                headers={'Content-Type':'text/html' if wrong_mime else 'application/pdf',
                         'Content-Length':'8000001' if large else '42'})
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def test_collector_hashes_pdf_and_text_separately_and_repeat_does_not_freshen(self):
        async with self.client() as client:
            result=await collect_official_pdf(client,self.root,NOW,page_extractor=lambda _:PAGES)
            self.assertEqual(result['status'],'collected')
            self.assertEqual(result['records'],29)
            before=load_availability_document((self.root/'structured_availability.json.gz').read_bytes())
            row=next(row for row in before['records'] if row['player']=='Paolo Banchero')
            self.assertEqual(row['source_content_sha256'],hashlib.sha256(b'%PDF-fixture-no-actual-pdf-byte-claim').hexdigest())
            self.assertNotEqual(row['source_content_sha256'],row['source_text_sha256'])
            await collect_official_pdf(client,self.root,NOW+dt.timedelta(minutes=5),page_extractor=lambda _:PAGES)
            after=load_availability_document((self.root/'structured_availability.json.gz').read_bytes())
            self.assertEqual(before['records'],after['records'])
            self.assertIsNone(row['expected_minutes'])
            self.assertFalse(result['complete_current_player_availability'])

    async def test_refusal_wrong_mime_and_oversize_preserve_existing_evidence(self):
        async with self.client() as client:
            await collect_official_pdf(client,self.root,NOW,page_extractor=lambda _:PAGES)
        before=(self.root/'structured_availability.json.gz').read_bytes()
        for kwargs in [{'fail':True},{'wrong_mime':True},{'large':True}]:
            async with self.client(**kwargs) as client:
                result=await collect_official_pdf(client,self.root,NOW+dt.timedelta(minutes=5),page_extractor=lambda _:PAGES)
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(before,(self.root/'structured_availability.json.gz').read_bytes())
        health=json.loads((self.root/'official_injury_pdf_status.json').read_text())
        self.assertEqual(health['retained_pdf_records'],29)


if __name__=='__main__':unittest.main()
