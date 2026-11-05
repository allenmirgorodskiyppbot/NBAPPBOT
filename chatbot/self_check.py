"""Owner self-checks: temporary fixtures, plus read-only live database checks."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from master import MasterStore
from manual_import import import_stats, parse_stats_document
from news_digest import is_promotion, news_summary

INTEGRITY_BUDGET_SECONDS = 8.0
STATUS_BUDGET_SECONDS = 3.0
READ_BUSY_SECONDS = 0.2
_CHECK_WORKERS = set()


async def run_serialized_checks(lock, function, *args, **kwargs):
    """Keep the lock for the native worker lifetime, even if its caller stops.

    Cancellation detaches the caller promptly. The synchronous worker releases
    the lock after its actual cleanup; a cancelled asyncio wrapper is not proof
    that its underlying thread stopped.
    """
    await lock.acquire()
    loop = asyncio.get_running_loop()
    gate = threading.Lock()
    state = {'started': False, 'finished': False, 'aborted': False, 'released': False}
    owner = None

    def release():
        with gate:
            if state['released']:
                return
            state['released'] = True
        try:
            lock.release()
        finally:
            if owner is not None:
                _CHECK_WORKERS.discard(owner)

    def worker():
        with gate:
            if state['aborted']:
                return None
            state['started'] = True
        try:
            return function(*args, **kwargs)
        finally:
            with gate:
                state['finished'] = True
            try:
                loop.call_soon_threadsafe(release)
            except RuntimeError:
                # No new caller can enter a closed event loop. Release only
                # after the real function has finished, including this case.
                release()

    def completed(task):
        if not task.cancelled():
            task.exception()  # Consume detached failures without logging values.
        with gate:
            if not state['started']:
                state['aborted'] = True
                safe = True
            else:
                safe = state['finished']
        if safe:
            release()

    coroutine = asyncio.to_thread(worker)
    try:
        owner = asyncio.create_task(coroutine, name='self-check-worker')
    except BaseException:
        coroutine.close()
        release()
        raise
    _CHECK_WORKERS.add(owner)
    owner.add_done_callback(completed)
    return await asyncio.shield(owner)


def _require(condition):
    if not condition:
        raise AssertionError('Self-check expectation was not met.')


@contextmanager
def _readonly(path, budget_seconds):
    """Fresh read-only connection with short lock waits and a VM deadline."""
    if path is None:
        raise FileNotFoundError('The read-only database is unavailable.')
    started = time.monotonic()
    deadline = started + max(0, budget_seconds)
    expired = False
    def progress():
        nonlocal expired
        expired = time.monotonic() >= deadline
        return int(expired)
    if progress():
        raise TimeoutError('Read-only check budget was exhausted.')
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True,
                         timeout=min(READ_BUSY_SECONDS, max(0, budget_seconds)))
    try:
        db.set_progress_handler(progress, 1000)
        yield db
        if progress():
            raise TimeoutError('Read-only check budget was exhausted.')
    except sqlite3.Error as error:
        code = getattr(error, 'sqlite_errorcode', None)
        if expired or type(code) is int and code & 255 == sqlite3.SQLITE_INTERRUPT:
            raise TimeoutError('Read-only check could not finish within its budget.') from None
        raise
    finally:
        db.close()


def _sqlite_ok(path, *, budget_seconds=None):
    budget = INTEGRITY_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    with _readonly(path, budget) as db:
        _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)])


def _status_snapshot(path, *, budget_seconds=None):
    budget = STATUS_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    with _readonly(path, budget) as db:
        db.row_factory = sqlite3.Row
        counts = {name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                  for name in ('games','player_games','news','boards','entries')}
        feeds = [dict(row) for row in db.execute('SELECT name,last_success,error FROM feed_status ORDER BY name')]
    return {'counts':counts, 'feeds':feeds}


def _unverified(error):
    if isinstance(error, (TimeoutError, FileNotFoundError, PermissionError)):
        return True
    code = getattr(error, 'sqlite_errorcode', None)
    return (isinstance(error, sqlite3.Error) and type(code) is int
            and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN,
                              sqlite3.SQLITE_INTERRUPT})


def run_self_checks(live_master, data_dir, exporter=None, *, progress=None):
    """Never send messages, call a model, or write fixtures to the live master."""
    checks = []
    started = time.monotonic()
    def stage(name):
        if progress is not None:
            try:
                progress(name)
            except Exception:
                pass  # Diagnostics must not break a check or expose callback data.
    def check(name, function, scope='isolated fixtures', *, uncertain=False):
        stage(name)
        check_started = time.monotonic()
        try:
            result = function()
        except Exception as error:
            checks.append({'name': name, 'status': 'WARN' if uncertain or _unverified(error) else 'FAIL',
                           'scope': scope, 'error_type': type(error).__name__,
                           'elapsed_seconds': round(time.monotonic()-check_started, 3)})
            return None
        checks.append({'name': name, 'status': 'PASS', 'scope': scope,
                       'elapsed_seconds': round(time.monotonic()-check_started, 3)})
        return result
    stage('Resolving read-only live database path')
    def live_database_path():
        value = getattr(live_master, 'database_path', None) or live_master.path
        path = Path(value).resolve()
        if not path.is_file():
            raise FileNotFoundError('The live database is unavailable.')
        return path
    live_path = check('Live database path availability (read-only)', live_database_path, 'live runtime', uncertain=True)
    check('Live database integrity (read-only)', lambda: _sqlite_ok(live_path), 'live runtime')
    status = check('Live database counts and feed snapshot (read-only)',
                   lambda: _status_snapshot(live_path), 'live runtime', uncertain=True)
    status_known = status is not None
    status = status or {'counts': {}, 'feeds': []}
    now = dt.datetime.now(dt.timezone.utc)
    stage('Checking saved RSS feed recency')
    if status_known and not any(str(feed.get('name', '')).startswith('rss_') for feed in status['feeds']):
        checks.append({'name': 'No RSS collection results yet', 'status': 'WARN', 'scope': 'live runtime',
                       'elapsed_seconds': 0.0})
    for feed in status['feeds']:
        if not str(feed.get('name', '')).startswith('rss_'):
            continue
        stage('Checking saved RSS feed recency')
        try:
            stamp = dt.datetime.fromisoformat(str(feed['last_success']).replace('Z', '+00:00'))
            fresh = not feed.get('error') and -60 <= (now - stamp).total_seconds() <= 1800
        except (TypeError, ValueError):
            fresh = False
        checks.append({'name': 'Recent successful ' + feed['name'],
                       'status': 'PASS' if fresh else 'WARN', 'scope': 'live runtime', 'elapsed_seconds': 0.0})
    backups = []
    def discover_backups():
        vanished = False
        for path in (Path(data_dir) / 'backups').glob('*.sqlite'):
            try:
                backups.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                vanished = True
        backups.sort(key=lambda item:item[0])
        if vanished:
            raise FileNotFoundError('A backup was pruned during discovery.')
        return True
    discovered = check('Live backup discovery (read-only)', discover_backups, 'live runtime', uncertain=True)
    if backups:
        backup_stamp, backup_path = backups[-1]
        check('Latest live backup integrity (read-only)', lambda: _sqlite_ok(backup_path), 'live runtime')
        stage('Checking saved backup age')
        if now.timestamp() - backup_stamp > 7 * 3600:
            checks.append({'name': 'Latest live backup is older than seven hours',
                           'status': 'WARN', 'scope': 'live runtime', 'elapsed_seconds': 0.0})
    elif discovered:
        checks.append({'name': 'Live backup not created yet', 'status': 'WARN', 'scope': 'live runtime',
                       'elapsed_seconds': 0.0})

    stage('Preparing isolated fixture database')
    with tempfile.TemporaryDirectory(prefix='knicks-selftest-') as directory:
        root = Path(directory)
        fixture = MasterStore(root / 'fixture.sqlite')
        stage('Preparing isolated sample entries')
        date = (now.date() - dt.timedelta(days=2)).isoformat()
        legs = [{'player': 'Jalen Brunson', 'team': 'NYK', 'stat': 'pts', 'side': 'more',
                 'line': 20.5, 'date': date},
                {'player': 'Cade Cunningham', 'team': 'DET', 'stat': 'pts', 'side': 'less',
                 'line': 30.5, 'date': date}]
        entry = fixture.create_entry(legs, 10, 'power', payout_multiplier=3, source='ISOLATED SELFTEST')
        stage('Preparing isolated CSV records')
        csv = ('date,player,team,opponent,game_status,appearance_state,minutes,pts,season_type\n'
               f'{date},Jalen Brunson,NYK,DET,final,Played,35,24,Regular Season\n'
               f'{date},Cade Cunningham,DET,NYK,final,Played,36,25,Regular Season\n').encode()
        rows = parse_stats_document('fixture.csv', csv)
        def import_and_grade():
            result = import_stats(fixture, rows, source='ISOLATED SELFTEST')
            _require(result['status'] == 'complete')
            _require(fixture.get_entry(entry['id'])['provisional_status'] == 'won')
            _require(fixture.get_entry(entry['id'])['actual_payout'] is None)
        check('CSV import and provisional grading', import_and_grade)
        def player_calculations():
            from player_stats import answer_player_question
            report = answer_player_question(fixture, 'List Jalen Brunson last five games and average points.')
            _require(report is not None and not report['ai_used'])
            _require(report['data']['averages']['pts'] == 24)
            _require(date in report['answer'] and '1/1 observed' in report['answer'])
        check('Direct player logs and arithmetic without AI', player_calculations)
        def reject_bad_import():
            before = fixture.status()['counts']
            invalid = [dict(row) for row in rows]
            invalid[0]['pts'] = -1
            try:
                import_stats(fixture, invalid, source='ISOLATED INVALID SELFTEST')
            except ValueError:
                pass
            else:
                raise AssertionError('Negative points were accepted.')
            _require(fixture.status()['counts'] == before)
            _require(fixture.get_entry(entry['id'])['provisional_status'] == 'won')
        check('Invalid statistics rejected without partial write', reject_bad_import)
        def corrections():
            corrected = [dict(row) for row in rows]
            corrected[0]['pts'] = 19
            import_stats(fixture, corrected, source='ISOLATED CORRECTION')
            _require(fixture.get_entry(entry['id'])['provisional_status'] == 'lost')
            _require(fixture.get_entry(entry['id'])['actual_payout'] is None)
            import_stats(fixture, rows, source='ISOLATED CORRECTION')
            _require(fixture.get_entry(entry['id'])['provisional_status'] == 'won')
        check('Corrected final statistics update provisional results', corrections)
        def ledger():
            fixture.mark_settlement(entry['id'], 30, 'ISOLATED SELFTEST')
            fixture.mark_settlement(entry['id'], 30, 'ISOLATED SELFTEST repeat')
            summary = fixture.ledger_summary()
            _require(summary['settled_entries'] == 1 and summary['net_profit'] == 20)
            losing_legs = [dict(leg) for leg in legs]
            losing_legs[0]['line'] = 40.5
            loser = fixture.create_entry(losing_legs, 10, 'power', payout_multiplier=3,
                                         source='ISOLATED LOSING SELFTEST')
            fixture.grade_entries()
            _require(fixture.get_entry(loser['id'])['provisional_status'] == 'lost')
            fixture.mark_settlement(loser['id'], 0, 'ISOLATED SELFTEST LOSS')
            summary = fixture.ledger_summary()
            _require(summary['settled_entries'] == 2 and summary['net_profit'] == 10)
            _require(summary['settled_wins'] == summary['settled_losses'] == 1)
        check('Confirmed wins/losses and duplicate-settlement accounting', ledger)
        stage('Preparing isolated news records')
        fixture.record_news({'id': 'injury', 'title': 'Fixture player: Questionable with ankle injury',
                             'source': 'ISOLATED FIXTURE', 'published_at': now.isoformat(),
                             'url': 'https://example.com/selftest-injury'})
        fixture.record_news({'id': 'promotion', 'title': 'Promo code: Trade $25 get bonus bets',
                             'source': 'ISOLATED FIXTURE', 'published_at': now.isoformat()})
        def reports():
            text = news_summary(fixture)
            _require('Questionable' in text and 'Published:' in text)
            _require('https://example.com/selftest-injury' in text)
            _require('bonus bets' not in text)
            _require(is_promotion({'title': 'Promo code: Trade $25 get bonus bets'}))
        check('Cited injury digest excludes promotional offers', reports)
        async def telegram():
            from telegram_bot import TelegramBot
            async def no_model(*args):
                raise AssertionError('Self-test must not invoke AI.')
            bot = TelegramBot(fixture, no_model, token='123456:ISOLATED_SELFTEST', allowed_user_ids=[1])
            def update(number, user=1, command='/summary'):
                return {'update_id': number, 'message': {'from': {'id': user},
                        'chat': {'id': user, 'type': 'private'}, 'text': command}}
            _require(not await bot.process_update(update(1, user=999)))
            _require(await bot.process_update(update(2)))
            _require(not await bot.process_update(update(2)))
            _require(bot.status()['pending_messages'] == 1)
            reopened = TelegramBot(MasterStore(root / 'fixture.sqlite'), no_model,
                                  token='123456:ISOLATED_SELFTEST', allowed_user_ids=[1])
            _require(reopened.status()['pending_messages'] == 1)
        check('Telegram allowlist, duplicate handling and durable outbox', lambda: asyncio.run(telegram()))
        check('Saved records survive database reopen', lambda: _require(
            MasterStore(root / 'fixture.sqlite').get_entry(entry['id'])['actual_payout'] == 30))
        def backup_restore():
            fixture.backup(str(root / 'backup.sqlite'))
            _sqlite_ok(root / 'backup.sqlite')
            restored = MasterStore(root / 'backup.sqlite')
            _require(restored.ledger_summary()['net_profit'] == 10)
            _require(restored.status()['counts'] == fixture.status()['counts'])
        check('Backup restores records and confirmed ledger', backup_restore)
        if exporter is not None:
            def export():
                import openpyxl
                path = exporter(fixture, root / 'export')
                workbook = openpyxl.load_workbook(path, read_only=True)
                try:
                    _require({'games', 'player_games', 'news', 'entries'} <= set(workbook.sheetnames))
                    _require(len(list(workbook['player_games'].values)) == 3)
                finally:
                    workbook.close()
            check('Master Excel export contains imported data', export)
    stage('Finishing self-test report')
    return {'checks': checks, 'counts': status['counts'],
            'passed': sum(item['status'] == 'PASS' for item in checks),
            'failed': sum(item['status'] == 'FAIL' for item in checks),
            'warnings': sum(item['status'] == 'WARN' for item in checks),
            'elapsed_seconds': round(time.monotonic()-started, 3),
            'scope': 'Sample data is confined to temporary databases. Live checks are read-only. '
                     'AI generation and actual PrizePicks lines are separate tests.'}


def format_self_check(report):
    lines = [f"Self-test: {report['passed']} passed, {report['failed']} failed, {report['warnings']} warnings."]
    for item in report['checks']:
        details = []
        if 'elapsed_seconds' in item:
            details.append(f"{item['elapsed_seconds']:g}s")
        error_type = item.get('error_type')
        if item['status'] != 'PASS' and error_type:
            details.append(error_type if isinstance(error_type,str) and len(error_type)<=64
                           and error_type.isascii() and error_type.isidentifier() else 'Error')
        lines.append(f"{item['status']}: {item['name']}" + (' ('+'; '.join(details)+')' if details else ''))
    lines.append(report['scope'])
    return '\n'.join(lines)
