"""Bounded current-day event checks; never import player statistics or settle games."""
from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

from feeds import ESPN, TEAM_WORDS, parse_game
from news_digest import _aware

EASTERN = ZoneInfo('America/New_York')
CHECKPOINT = 'nba_schedule_today'
CHECK_SECONDS = 900


def _known_games(store, day):
    with store.connect() as db:
        saved = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE date=?', (day,))]
        observed = [dict(row) for row in db.execute('SELECT * FROM games WHERE date=?', (day,))]
    games = {row['id']: row for row in saved}
    for row in observed:
        games[row['id']] = {**games.get(row['id'], {}), **{k:v for k,v in row.items() if v is not None}}
    return games


def _parse_today(document, day):
    if not isinstance(document, dict) or not isinstance(document.get('events'), list):
        raise ValueError('Current NBA scoreboard is missing its event inventory.')
    leagues = document.get('leagues')
    if not isinstance(leagues, list) or not any(isinstance(x, dict) and x.get('abbreviation') == 'NBA' for x in leagues):
        raise ValueError('Current event inventory does not identify the NBA.')
    if len(document['events']) > 30:
        raise ValueError('Current NBA event inventory exceeds its expected size.')
    result = {}
    names = {'STATUS_SCHEDULED', 'STATUS_DELAYED', 'STATUS_IN_PROGRESS', 'STATUS_HALFTIME',
             'STATUS_END_PERIOD', 'STATUS_FINAL', 'STATUS_FINAL_OT', 'STATUS_POSTPONED',
             'STATUS_CANCELED', 'STATUS_SUSPENDED'}
    for event in document['events']:
        competition = (event.get('competitions') or [{}])[0]
        tip = _aware(event.get('date') or competition.get('date'))
        state = (event.get('status') or competition.get('status') or {}).get('type', {})
        if not tip or tip.astimezone(EASTERN).date().isoformat() != day or state.get('name') not in names:
            raise ValueError('Current event date, timezone or status is unresolved.')
        game = parse_game(event)
        if (game['date'] != day or game['home_team'] not in TEAM_WORDS or game['away_team'] not in TEAM_WORDS
                or game['home_team'] == game['away_team'] or game['id'] in result):
            raise ValueError('Current event identity is invalid or duplicated.')
        if state.get('name') == 'STATUS_SUSPENDED':
            game['status'] = 'suspended'
        game['start_time'] = tip.isoformat()
        result[game['id']] = game
    return result


async def refresh_game_schedule(store, worker, now=None):
    """Check only existing event IDs, sharing a 15-minute cache and worker lock.

    A failed fetch retains previous observations but makes the check failed.
    No absence is interpreted as a cancellation or a verified league off day.
    """
    now = now or (worker.now() if callable(getattr(worker, 'now', None)) else dt.datetime.now(dt.timezone.utc))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Current schedule checks require an aware clock.')
    day = now.astimezone(EASTERN).date().isoformat()
    known = _known_games(store, day)
    if not known:
        return {'date':day, 'status':'not_applicable', 'games':[], 'checked_at':now.isoformat(),
                'error':None, 'source':None}
    loop = asyncio.get_running_loop()
    if getattr(worker, '_schedule_check_loop', None) is not loop:
        worker._schedule_check_loop = loop
        worker._schedule_check_lock = asyncio.Lock()
    async with worker._schedule_check_lock:
        prior = store.get_checkpoint(CHECKPOINT, {}) or {}
        checked = _aware(prior.get('checked_at'))
        if (prior.get('date') == day and checked and 0 <= (now-checked).total_seconds() < CHECK_SECONDS
                and set(prior.get('expected_game_ids', [])) == set(known)):
            return prior
        source = ESPN + 'scoreboard?dates=' + day.replace('-', '')
        report = {'date':day, 'status':'failed', 'checked_at':now.isoformat(), 'source':source,
                  'expected_game_ids':sorted(known), 'games':prior.get('games', []) if prior.get('date') == day else [],
                  'last_success_at':prior.get('last_success_at'), 'error':None}
        store.set_checkpoint(CHECKPOINT, {**report, 'status':'checking'})
        try:
            async with asyncio.timeout(10):
                document = await worker.request(source)
            events = _parse_today(document, day)
            missing = sorted(set(known)-set(events))
            conflicts = []
            matched = []
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for identity in sorted(set(known)&set(events)):
                    event = events[identity]
                    old = db.execute('SELECT * FROM games WHERE id=?', (identity,)).fetchone()
                    old = dict(old) if old else {}
                    reported = event['status']
                    status = 'past_schedule_only' if reported == 'final' else reported
                    if old.get('status') == 'final':
                        status = 'final'
                    elif old.get('status') in {'live','suspended','past_schedule_only'} and status == 'scheduled':
                        status = old['status']; conflicts.append(identity)
                    # A scoreboard changes event timing/state only. It must not
                    # overwrite validated scores or create final player results.
                    observation = {key:event[key] for key in ('id','date','home_team','away_team',
                                   'start_time','provider_game_id','source')}
                    observation['status'] = status
                    if old.get('status') != 'final':
                        store.upsert_game({**old, **observation}, db=db)
                    matched.append({**observation, 'reported_status':reported})
            report.update(games=matched, missing_game_ids=missing, conflicting_game_ids=conflicts)
            if missing or conflicts:
                report.update(status='partial', error='Current event inventory is missing or conflicts with saved events.')
            else:
                report.update(status='success', last_success_at=now.isoformat())
        except asyncio.CancelledError:
            store.set_checkpoint(CHECKPOINT, {**report, 'status':'failed', 'error':'CancelledError'})
            store.feed_failure('nba_schedule_today', 'CancelledError; current event timing is unverified.')
            raise
        except Exception as error:
            code = getattr(getattr(error, 'response', None), 'status_code', None)
            report['error'] = type(error).__name__ + (f': HTTP {code}' if code else '')
        store.set_checkpoint(CHECKPOINT, report)
        if report['status'] == 'success':
            store.feed_success('nba_schedule_today', 'Current event timing checked; existing games only; no player statistics imported.')
        else:
            store.feed_failure('nba_schedule_today', report['error'] + '; current event timing is unverified.')
        return report
