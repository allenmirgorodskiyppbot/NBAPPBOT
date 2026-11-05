"""Daily CLI sources remain ordered, independent and honest about failures."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from nba_daily_export import collect_availability_inputs, main_async
from structured_availability import RELAY_NAME

NOW = dt.datetime(2026, 11, 5, 15, tzinfo=dt.timezone.utc)


class AvailabilityWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    async def test_sources_run_in_order_with_original_report_clocks(self):
        calls = []
        structured = {'status':'updated','records':2,'coverage':[{'report_at':'2026-11-05T13:00:00Z'}]}
        pdf = {'status':'collected','records':1,'report_at':'2026-11-05T14:45:00Z'}
        mirror, client = object(), object()
        async def collect(m, output, *, now):
            self.assertIs(m, mirror); self.assertEqual(output, self.output); self.assertEqual(now, NOW)
            calls.append('structured'); return structured
        async def original(c, output, *, now):
            self.assertIs(c, client); self.assertEqual(output, self.output); self.assertEqual(now, NOW)
            calls.append('pdf'); return pdf
        with patch('structured_availability.collect_availability', side_effect=collect), \
             patch('official_injury_pdf.collect_official_pdf', side_effect=original):
            result = await collect_availability_inputs(mirror, client, self.output, now=NOW)
        self.assertEqual(calls, ['structured','pdf'])
        self.assertEqual(result['sources'], {'structured_extracts':structured,'official_pdf':pdf})
        self.assertEqual(json.loads((self.output/'availability_collection_status.json').read_text()), result)
        self.assertNotIn('current', result['sources']['official_pdf'])

    async def test_unexpected_mirror_failure_keeps_prior_bytes_and_still_attempts_pdf(self):
        target = self.output / RELAY_NAME
        target.write_bytes(b'prior evidence bytes keep their own clocks')
        before = target.read_bytes()
        pdf = AsyncMock(return_value={'status':'unavailable','retained_pdf_records':4})
        with patch('structured_availability.collect_availability', side_effect=ValueError('invalid source')), \
             patch('official_injury_pdf.collect_official_pdf', pdf):
            result = await collect_availability_inputs(object(), object(), self.output, now=NOW)
        self.assertEqual(target.read_bytes(), before)
        pdf.assert_awaited_once()
        failed = result['sources']['structured_extracts']
        self.assertEqual((failed['status'], failed['error_type']), ('unavailable','ValueError'))
        self.assertNotIn('source_checked_at', failed)
        self.assertEqual(result['sources']['official_pdf']['retained_pdf_records'],4)

    async def test_official_refusal_is_health_not_a_new_successful_report(self):
        response = httpx.Response(403,request=httpx.Request('GET','https://official.nba.com/report'))
        error = httpx.HTTPStatusError('refused',request=response.request,response=response)
        with patch('structured_availability.collect_availability', new=AsyncMock(return_value={'status':'updated','records':3})), \
             patch('official_injury_pdf.collect_official_pdf', new=AsyncMock(side_effect=error)):
            result = await collect_availability_inputs(object(),object(),self.output,now=NOW)
        failed = result['sources']['official_pdf']
        self.assertEqual((failed['status'],failed['http_status']),('unavailable',403))
        self.assertNotIn('report_at',failed)
        self.assertEqual(result['sources']['structured_extracts']['records'],3)

    async def test_daily_cli_runs_availability_after_tracking_and_before_career(self):
        calls=[]
        def effect(name,result):
            async def run(*args,**kwargs):
                calls.append(name);return result
            return run
        collector=SimpleNamespace(now=NOW,
            publish_current_schedule=AsyncMock(side_effect=effect('schedule',{'status':'success'})),
            inventory=AsyncMock(return_value={}))
        day={'source':'espn','games':[],'player_games':[],'complete':True}
        mirror=SimpleNamespace(scope={'revision':'test'},configure_scope=Mock(),publish_scope=Mock(),
            day=AsyncMock(return_value=day),tracking=AsyncMock(side_effect=effect('tracking',True)))
        args=SimpleNamespace(output=str(self.output),days=1,schedule_only=False)
        with patch('nba_daily_export.PublicCollector',return_value=collector), \
             patch('mirror_sources.PublishedNBAData',return_value=mirror), \
             patch('nba_daily_export.reconcile_day_inventory',return_value=day), \
             patch('advanced_refresh.supplement_tracking',side_effect=effect('direct_tracking',False)), \
             patch('structured_availability.collect_availability',side_effect=effect('availability',{'status':'updated','records':2})), \
             patch('official_injury_pdf.collect_official_pdf',side_effect=effect('pdf',{'status':'collected','records':1})), \
             patch('career_history_refresh.refresh_career_history',side_effect=effect('career',{'phase':'updated'})), \
             contextlib.redirect_stdout(io.StringIO()):
            result=await main_async(args)
        self.assertEqual(result,0)
        self.assertEqual(calls,['schedule','tracking','direct_tracking','availability','pdf','career'])
        self.assertTrue((self.output/'availability_collection_status.json').exists())

    async def test_schedule_only_does_not_collect_other_sources(self):
        collector=SimpleNamespace(now=NOW,publish_current_schedule=AsyncMock(return_value={'status':'success'}))
        args=SimpleNamespace(output=str(self.output),days=7,schedule_only=True)
        with patch('nba_daily_export.PublicCollector',return_value=collector), \
             patch('nba_daily_export.collect_availability_inputs',new=AsyncMock()) as collect, \
             contextlib.redirect_stdout(io.StringIO()):
            result=await main_async(args)
        self.assertEqual(result,0);collect.assert_not_awaited()


class DailyWorkflowTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parent
        # The managed editing workspace is flat; GitHub stores this file in
        # the repository's workflow directory beside chatbot/.
        source=root/'nba-daily-data.yml'
        if not source.is_file():
            source=root.parent/'.github'/'workflows'/'nba-daily-data.yml'
        self.workflow=source.read_text()

    def test_required_collectors_trigger_workflow_and_dependency_stays_collector_only(self):
        for module in ('tracking_publications','historical_matchup_refresh','structured_availability','official_injury_pdf'):
            self.assertIn("- 'chatbot/"+module+".py'",self.workflow)
        self.assertIn('timeout-minutes: 20',self.workflow)
        self.assertIn('pypdf==6.1.3',self.workflow)
        self.assertNotIn('pypdf',Path(__file__).with_name('requirements.txt').read_text())
        self.assertIn("cron: '15 0-3,15-23 * * *'",self.workflow)

    def test_verified_source_cache_is_excluded_from_the_scoped_public_publisher(self):
        self.assertIn('uses: actions/cache@v4',self.workflow)
        self.assertIn('path: data/.nba-tracking-cache',self.workflow)
        self.assertIn('steps.source-cache-date.outputs.day',self.workflow)
        self.assertIn("hashFiles('chatbot/historical_matchup_refresh.py', 'chatbot/tracking_publications.py')",self.workflow)
        self.assertIn('nba-tracking-v1-${{ runner.os }}-',self.workflow)
        self.assertIn('python chatbot/nba_daily_export.py --output data/live --days 7',self.workflow)
        publish=self.workflow
        self.assertIn('python chatbot/publish_public_data.py --mode daily',publish)
        self.assertIn('GITHUB_TOKEN: ${{ github.token }}',publish)
        self.assertIn('GITHUB_REPOSITORY: ${{ github.repository }}',publish)
        from publish_public_data import inspect_changes
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(['git','init','-q',directory],check=True,capture_output=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                            'commit','--allow-empty','-qm','Isolated publication fixture'],
                           cwd=root,check=True,capture_output=True)
            live=root/'data'/'live';live.mkdir(parents=True)
            (live/'availability_collection_status.json').write_text('{"phase":"checked"}\n')
            cache=root/'data'/'.nba-tracking-cache';cache.mkdir()
            (cache/'game_matchups_2018.parquet').write_bytes(b'private source cache')
            (live/'private_notes.txt').write_text('Not a public source artifact')
            checkout,changes=inspect_changes(root,'daily')
            self.assertEqual(len(checkout),40)
            self.assertEqual([change.path for change in changes],
                             ['data/live/availability_collection_status.json'])



if __name__=='__main__':
    unittest.main()
