"""Source refusals are retry diagnostics, never fresh schedules or hidden crashes."""
import asyncio
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from game_schedule_check import _parse_relay
from nba_daily_export import PublicCollector, main_async
from test_pregame_schedule import NOW, DAY, scoreboard


class ScheduleJobHealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.collector = PublicCollector(None, NOW)

    def publish(self):
        return asyncio.run(self.collector.publish_current_schedule(self.output))

    def run_cli(self):
        args = SimpleNamespace(output=str(self.output), days=7, schedule_only=True)
        output = io.StringIO()
        with patch('nba_daily_export.PublicCollector', return_value=self.collector), contextlib.redirect_stdout(output):
            result = asyncio.run(main_async(args))
        return result, output.getvalue()

    def test_refused_sources_save_honest_retry_status_and_preserve_good_relay(self):
        self.collector.request = AsyncMock(return_value=scoreboard())
        self.publish()
        target = self.output / 'schedule_today.json'
        original = target.read_bytes()
        self.collector.now += dt.timedelta(minutes=20)

        async def refused(url):
            if 'espn.com' in url:
                response = httpx.Response(403, request=httpx.Request('GET', url))
                raise httpx.HTTPStatusError('refused fixture', request=response.request, response=response)
            raise httpx.ReadTimeout('timeout fixture')

        self.collector.request = refused
        result, output = self.run_cli()
        status = json.loads((self.output / 'schedule_status.json').read_text())
        self.assertEqual(result, 0)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(status['status'], 'unavailable')
        self.assertEqual(status['date'], DAY)
        self.assertEqual(status['attempted_at'], self.collector.now.isoformat())
        self.assertIsNone(status['source_checked_at'])
        self.assertEqual([r['error_type'] for r in status['attempts']], ['HTTPStatusError', 'ReadTimeout'])
        self.assertEqual(status['attempts'][0]['http_status'], 403)
        self.assertEqual(status['retry_interval_seconds'], 600)
        self.assertTrue(status['next_retry_is_estimate'])
        self.assertIn('::notice::', output)
        self.assertIn('remains unverified', output)
        with self.assertRaises(ValueError):
            _parse_relay(status, DAY, self.collector.now)
        with self.assertRaises(ValueError):
            _parse_relay(json.loads(original), DAY, self.collector.now)

    def test_first_failed_run_has_status_but_no_invented_event_file(self):
        self.collector.request = AsyncMock(side_effect=httpx.ReadTimeout('fixture'))
        result, _ = self.run_cli()
        self.assertEqual(result, 0)
        self.assertTrue((self.output / 'schedule_status.json').exists())
        self.assertFalse((self.output / 'schedule_today.json').exists())

    def test_success_health_matches_actual_source_time_and_does_not_become_a_relay(self):
        self.collector.request = AsyncMock(return_value=scoreboard())
        result, _ = self.run_cli()
        self.assertEqual(result, 0)
        relay = json.loads((self.output / 'schedule_today.json').read_text())
        health = json.loads((self.output / 'schedule_status.json').read_text())
        self.assertEqual(health['status'], 'success')
        self.assertEqual(health['source_checked_at'], relay['source_checked_at'])
        self.assertEqual(health['source'], relay['source_url'])
        self.assertEqual(health['games'], len(relay['game_ids']))
        _parse_relay(relay, DAY, NOW)
        with self.assertRaises(ValueError):
            _parse_relay(health, DAY, NOW)

    def test_unexpected_code_and_output_failures_still_fail_the_job(self):
        self.collector.publish_current_schedule = AsyncMock(side_effect=KeyError('programming defect'))
        with self.assertRaises(KeyError):
            self.run_cli()
        self.collector = PublicCollector(None, NOW)
        self.collector.request = AsyncMock(return_value=scoreboard())
        with patch('nba_daily_export.write_schedule_status', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.run_cli()

    def test_unknown_collector_state_is_not_treated_as_handled_source_failure(self):
        self.collector.publish_current_schedule = AsyncMock(return_value={'status': 'broken'})
        with self.assertRaisesRegex(ValueError, 'Unexpected schedule'):
            self.run_cli()

    def test_workflow_can_publish_health_without_a_prior_schedule_file(self):
        local = Path(__file__).with_name('nba-schedule-data.yml')
        repository = Path(__file__).resolve().parent.parent / '.github' / 'workflows' / 'nba-schedule-data.yml'
        workflow = (local if local.exists() else repository).read_text()
        publish = workflow.split('      - name: Publish source timestamp', 1)[1]
        script = publish.split('        run: |\n', 1)[1].split('          if git diff', 1)[0]
        script = '\n'.join(line[10:] for line in script.splitlines())
        subprocess.run(['git', 'init', '-q', str(self.output)], check=True, capture_output=True)
        status = self.output / 'data' / 'live' / 'schedule_status.json'
        status.parent.mkdir(parents=True)
        status.write_text('{"status":"unavailable"}\n')
        result = subprocess.run(['bash', '-e', '-c', script], cwd=self.output, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        staged = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=self.output,
                                capture_output=True, text=True, check=True)
        self.assertEqual(staged.stdout.strip(), 'data/live/schedule_status.json')


if __name__ == '__main__':
    unittest.main()
