"""Automatic post-upgrade checks; fixtures never enter the live database."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from basketball_research import answer_assignment_question, assignment_rows
from news_digest import _aware
from self_check import run_self_checks

log = logging.getLogger('knicks.release')


def model_startup_completed(master):
    """Refresh only readiness when a long model download outlasts the checks."""
    report = master.get_checkpoint('release_checks', {})
    if not isinstance(report.get('checks'), list):
        return
    report['model_phase'] = 'ready'
    for check in report['checks']:
        if check['name'] == 'AI model startup ready (generation not tested)':
            check['status'] = 'PASS'
    report['passed'] = sum(c['status'] == 'PASS' for c in report['checks'])
    report['warnings'] = sum(c['status'] == 'WARN' for c in report['checks'])
    master.set_checkpoint('release_checks', report)


def verify_release(master, workbook, data_dir, exporter=None, model_phase=None, telegram=None):
    report = run_self_checks(master, data_dir, exporter)
    checks = report['checks']
    try:
        rows = assignment_rows(workbook)
        if rows:
            row = rows[0]
            answer = answer_assignment_question(workbook, master,
                f"Who is {row['selected_player']}'s matchup against {row['opponent_team']}?")
            passed = answer and not answer['ai_used'] and answer['sources'][0]['excel_rows']
            checks.append({'name': 'Sourced conversational workbook matchup lookup',
                           'status': 'PASS' if passed else 'FAIL', 'scope': 'read-only live workbook'})
        else:
            checks.append({'name': 'Workbook matchup assignment coverage unavailable', 'status': 'WARN',
                           'scope': 'read-only live workbook'})
    except Exception as error:
        checks.append({'name': 'Workbook matchup lookup', 'status': 'FAIL', 'scope': 'read-only live workbook',
                       'error_type': type(error).__name__})
    digest = master.get_checkpoint('knicks_digest_status', {})
    schedule_ok = digest.get('state') not in {None, 'starting', 'schedule_unavailable', 'disabled'}
    checks.append({'name': 'Current Knicks schedule available for digest timing',
                   'status': 'PASS' if schedule_ok else 'WARN', 'scope': 'live source status'})
    if telegram is not None:
        checks.append({'name': 'Private Telegram configured and connected',
                       'status': 'PASS' if telegram.get('enabled') and telegram.get('connected') else 'WARN',
                       'scope': 'live connection status'})
    checks.append({'name': 'AI model startup ready (generation not tested)',
                   'status': 'PASS' if model_phase == 'ready' else 'WARN', 'scope': 'live startup phase'})
    report.update(passed=sum(c['status'] == 'PASS' for c in checks),
                  failed=sum(c['status'] == 'FAIL' for c in checks),
                  warnings=sum(c['status'] == 'WARN' for c in checks),
                  checked_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  source_ref=os.getenv('SOURCE_REF', 'local-development'),
                  model_phase=model_phase,
                  statistics_mode=os.getenv('NBA_DATA_SOURCE', 'espn'),
                  scope='Business logic uses temporary fixtures; live database/workbook checks are read-only. '
                        'No generated AI answer, actual PrizePicks entry or live scheduled-message delivery is tested.')
    return report


async def automatic_release_checks(state, data_dir, exporter=None):
    """Allow initial collectors to finish, then save a bounded owner status report."""
    state.master.set_checkpoint('release_checks', {'phase': 'running',
        'source_ref': os.getenv('SOURCE_REF', 'local-development')})
    try:
        boot = _aware(state.boot_started_at)
        for _ in range(90):
            status = state.master.status()
            names = (['rss_' + str(i + 1) for i in range(len(state.worker.rss_urls))]
                     if state.worker.news_source == 'rss' else ['espn_news'])
            feeds = {f['name']: f for f in status['feeds']}
            attempted = all(name in feeds and _aware(feeds[name].get('last_attempt'))
                            and _aware(feeds[name]['last_attempt']) >= boot for name in names)
            digest = state.master.get_checkpoint('knicks_digest_status', {})
            if attempted and digest.get('checked_at') and _aware(digest['checked_at']) >= boot:
                break
            await asyncio.sleep(1)
        async with state.self_test_lock:
            report = await asyncio.to_thread(verify_release, state.master, state.store, data_dir,
                                              exporter, state.phase, state.telegram.status())
        report['phase'] = 'checked'
        state.master.set_checkpoint('release_checks', report)
        if state.phase == 'ready':
            model_startup_completed(state.master)
        log.info('RELEASE CHECKS — passed=%s failed=%s warnings=%s source=%s',
                 report['passed'], report['failed'], report['warnings'], report['source_ref'])
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state.master.set_checkpoint('release_checks', {'phase': 'error',
            'error_type': type(error).__name__, 'source_ref': os.getenv('SOURCE_REF', 'local-development')})
        log.warning('Release checks could not finish: %s', type(error).__name__)
