"""Owner self-checks: temporary fixtures, plus read-only live database checks."""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
import sqlite3
import tempfile

from master import MasterStore
from manual_import import import_stats, parse_stats_document
from news_digest import is_promotion, news_summary


def _require(condition):
    if not condition:
        raise AssertionError('Self-check expectation was not met.')


def _sqlite_ok(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        _require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)])


def run_self_checks(live_master, data_dir, exporter=None):
    """Never send messages, call a model, or write fixtures to the live master."""
    checks = []
    def check(name, function, scope='isolated fixtures'):
        try:
            function()
            checks.append({'name': name, 'status': 'PASS', 'scope': scope})
        except Exception as error:
            checks.append({'name': name, 'status': 'FAIL', 'scope': scope,
                           'error_type': type(error).__name__})

    live_path = getattr(live_master, 'database_path', None) or live_master.path
    check('Live database integrity (read-only)', lambda: _sqlite_ok(live_path), 'live runtime')
    try:
        status = live_master.status()
    except Exception:
        status = {'counts': {}, 'feeds': []}
    now = dt.datetime.now(dt.timezone.utc)
    if not any(str(feed.get('name', '')).startswith('rss_') for feed in status['feeds']):
        checks.append({'name': 'No RSS collection results yet', 'status': 'WARN', 'scope': 'live runtime'})
    for feed in status['feeds']:
        if not str(feed.get('name', '')).startswith('rss_'):
            continue
        try:
            stamp = dt.datetime.fromisoformat(str(feed['last_success']).replace('Z', '+00:00'))
            fresh = not feed.get('error') and -60 <= (now - stamp).total_seconds() <= 1800
        except (TypeError, ValueError):
            fresh = False
        checks.append({'name': 'Recent successful ' + feed['name'],
                       'status': 'PASS' if fresh else 'WARN', 'scope': 'live runtime'})
    backups = sorted((Path(data_dir) / 'backups').glob('*.sqlite'), key=lambda path: path.stat().st_mtime)
    if backups:
        check('Latest live backup integrity (read-only)', lambda: _sqlite_ok(backups[-1]), 'live runtime')
        if now.timestamp() - backups[-1].stat().st_mtime > 7 * 3600:
            checks.append({'name': 'Latest live backup is older than seven hours',
                           'status': 'WARN', 'scope': 'live runtime'})
    else:
        checks.append({'name': 'Live backup not created yet', 'status': 'WARN', 'scope': 'live runtime'})

    with tempfile.TemporaryDirectory(prefix='knicks-selftest-') as directory:
        root = Path(directory)
        fixture = MasterStore(root / 'fixture.sqlite')
        date = (now.date() - dt.timedelta(days=2)).isoformat()
        legs = [{'player': 'Jalen Brunson', 'team': 'NYK', 'stat': 'pts', 'side': 'more',
                 'line': 20.5, 'date': date},
                {'player': 'Cade Cunningham', 'team': 'DET', 'stat': 'pts', 'side': 'less',
                 'line': 30.5, 'date': date}]
        entry = fixture.create_entry(legs, 10, 'power', payout_multiplier=3, source='ISOLATED SELFTEST')
        csv = ('date,player,team,opponent,game_status,appearance_state,minutes,pts\n'
               f'{date},Jalen Brunson,NYK,DET,final,Played,35,24\n'
               f'{date},Cade Cunningham,DET,NYK,final,Played,36,25\n').encode()
        rows = parse_stats_document('fixture.csv', csv)
        def import_and_grade():
            result = import_stats(fixture, rows, source='ISOLATED SELFTEST')
            _require(result['status'] == 'complete')
            _require(fixture.get_entry(entry['id'])['provisional_status'] == 'won')
            _require(fixture.get_entry(entry['id'])['actual_payout'] is None)
        check('CSV import and provisional grading', import_and_grade)
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
    return {'checks': checks, 'counts': status['counts'],
            'passed': sum(item['status'] == 'PASS' for item in checks),
            'failed': sum(item['status'] == 'FAIL' for item in checks),
            'warnings': sum(item['status'] == 'WARN' for item in checks),
            'scope': 'Sample data is confined to temporary databases. Live checks are read-only. '
                     'AI generation and tomorrow\'s actual PrizePicks lines are separate tests.'}


def format_self_check(report):
    lines = [f"Self-test: {report['passed']} passed, {report['failed']} failed, {report['warnings']} warnings."]
    lines.extend(f"{item['status']}: {item['name']}" for item in report['checks'])
    lines.append(report['scope'])
    return '\n'.join(lines)
