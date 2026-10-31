"""Published season calendar in the master dataset, separate from result grading."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')
SEED_PATH = Path(__file__).with_name('season_schedule_2026_27.json')
FIELDS = ['id','season','date','home_team','away_team','start_time','status','source',
          'source_asof','source_kind','checked_at','payload_json']


def current_season(now):
    year = now.astimezone(EASTERN).year - (now.astimezone(EASTERN).month < 7)
    return f'{year}-{str(year+1)[-2:]}'


def import_calendar(store, document, source_kind='saved_publication'):
    from feeds import canonical_game, canonical_team, TEAM_WORDS
    from news_digest import _aware
    if not isinstance(document, dict) or not re.fullmatch(r'20\d{2}-\d{2}', str(document.get('season',''))):
        raise ValueError('Calendar needs a season in YYYY-YY form.')
    season = document['season']
    start_year = int(season[:4])
    if season != f'{start_year}-{str(start_year+1)[-2:]}':
        raise ValueError('Calendar season is inconsistent.')
    rows = document.get('games')
    teams = {canonical_team(team) for team in document.get('covered_teams', [])}
    if not isinstance(rows,list) or not rows or len(rows)>2500 or not teams or not teams <= set(TEAM_WORDS):
        raise ValueError('Calendar needs recognized covered teams and game records.')
    checked = document.get('retrieved_at')
    if not _aware(checked):
        raise ValueError('Calendar needs a timezone-aware retrieval timestamp.')
    prepared, seen = [], set()
    for raw in rows:
        if not isinstance(raw,dict):
            raise ValueError('Invalid calendar record.')
        row = dict(raw)
        day = dt.date.fromisoformat(str(row.get('date')))
        if not dt.date(start_year,7,1) <= day < dt.date(start_year+1,7,1):
            raise ValueError('Calendar date falls outside its season.')
        home, away = canonical_team(row.get('home_team')), canonical_team(row.get('away_team'))
        if home not in TEAM_WORDS or away not in TEAM_WORDS or home==away or not {home,away}&teams:
            raise ValueError('Calendar has unresolved or out-of-scope teams.')
        identity = canonical_game(day.isoformat(),home,away)
        if identity in seen:
            raise ValueError('Duplicate calendar game.')
        seen.add(identity)
        tip = _aware(row.get('start_time'))
        if row.get('start_time') and (not tip or tip.astimezone(EASTERN).date()!=day):
            raise ValueError('Tipoff must be timezone-aware and match the Eastern game date.')
        state = row.get('status','scheduled')
        if state not in {'scheduled','postponed','canceled','time_tbd','live','final','past_schedule_only'}:
            raise ValueError('Unrecognized calendar status.')
        if not tip and state=='scheduled':
            state='time_tbd'
        source=str(row.get('source') or document.get('source') or '')
        if not source.startswith('https://'):
            raise ValueError('Calendar records need a public HTTPS source.')
        row.update(id=identity,season=season,date=day.isoformat(),home_team=home,away_team=away,
                   start_time=tip.isoformat() if tip else None,status=state,source=source,
                   source_asof=str(row.get('source_asof') or checked[:10]),source_kind=source_kind,checked_at=checked)
        row['payload_json']=json.dumps(raw,sort_keys=True,default=str)
        prepared.append(row)
    complete = document.get('complete_for_published_games',True)
    if complete and any(sum(team in (r['home_team'],r['away_team']) for r in prepared)<70 for team in teams):
        raise ValueError('A complete published season calendar cannot contain fewer than 70 team games.')
    missing=document.get('unassigned_games',0)
    if type(missing) is not int or missing<0:
        raise ValueError('Unassigned game count must be a nonnegative integer.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT value_json FROM checkpoints WHERE name=?',('season_calendar:'+season,)).fetchone()
        existing=json.loads(prior[0]) if prior else {}
        previous_checked=_aware(existing.get('source_checked_at'))
        # Compare instants, not local-clock strings, and guard the replacement
        # in the same transaction so an older import cannot undo a newer one.
        if previous_checked and previous_checked>_aware(checked):
            return existing
        windows=document.get('unassigned_windows')
        if missing and not windows and teams<=set(existing.get('covered_teams',[])):
            windows=existing.get('unassigned_windows',[])
        if windows is None or not missing:
            windows=[]
        if not isinstance(windows,list):
            raise ValueError('Unassigned schedule windows must be a list.')
        clean_windows=[]
        for window in windows:
            if not isinstance(window,dict):
                raise ValueError('Invalid unassigned schedule window.')
            start=dt.date.fromisoformat(str(window.get('start')))
            end=dt.date.fromisoformat(str(window.get('end')))
            if not dt.date(start_year,7,1)<=start<=end<dt.date(start_year+1,7,1):
                raise ValueError('Unassigned window falls outside its season.')
            clean_windows.append({**window,'start':start.isoformat(),'end':end.isoformat()})
        coverage={'complete_for_published_games':bool(complete),'unassigned_games':missing,
                  'unassigned_windows':clean_windows,'unassigned_coverage_unknown':bool(missing and not clean_windows)}
        revision_rows=[{k:r[k] for k in FIELDS if k not in {'checked_at','payload_json'}} for r in prepared]
        revision=hashlib.sha256(json.dumps({'games':revision_rows,'coverage':coverage},sort_keys=True).encode()).hexdigest()
        meta={'season':season,'covered_teams':sorted(teams),'published_games':len(prepared),
              'regular_season_total':document.get('regular_season_total'),
              'unassigned_note':document.get('unassigned_note') or (existing.get('unassigned_note','') if missing else ''),
              **coverage,'source_kind':source_kind,
              'source':document.get('source'),'source_checked_at':checked,'revision':revision}
        placeholders=','.join('?' for _ in teams)
        db.execute(f'DELETE FROM season_schedule WHERE season=? AND '
                   f'(home_team IN ({placeholders}) OR away_team IN ({placeholders}))',
                   (season,*sorted(teams),*sorted(teams)))
        db.executemany('INSERT INTO season_schedule ('+','.join(FIELDS)+') VALUES ('+
                       ','.join('?' for _ in FIELDS)+')',[[r[k] for k in FIELDS] for r in prepared])
        db.execute('INSERT INTO checkpoints VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                   ('season_calendar:'+season,json.dumps(meta)))
    return meta


def seed_calendar(store, path=SEED_PATH):
    from news_digest import _aware
    path=Path(path)
    if not path.exists():
        store.feed_failure('season_calendar_seed','Published schedule seed is missing; automatic live collection remains available.')
        return None
    data=path.read_bytes()
    key='season_calendar_seed:'+hashlib.sha256(data).hexdigest()
    document=json.loads(data)
    existing=store.get_checkpoint('season_calendar:'+str(document.get('season')), {})
    if store.get_checkpoint(key):
        return existing
    # A bundled publication must not replace a later live/manual calendar update.
    previous_checked=_aware(existing.get('source_checked_at'))
    seed_checked=_aware(document.get('retrieved_at'))
    if previous_checked and seed_checked and previous_checked>seed_checked:
        store.set_checkpoint(key,True)
        return existing
    result=import_calendar(store,document)
    store.set_checkpoint(key,True)
    store.feed_success('season_calendar_seed',f"Loaded {result['published_games']} published games for {result['season']}; "
                       f"{result['unassigned_games']} additional games remain TBD. Schedule-only; no results/statistics imported.")
    return result


def saved_schedule(store, now):
    season=current_season(now)
    meta=store.get_checkpoint('season_calendar:'+season,{})
    if not meta:
        return {}
    with store.connect() as db:
        games=[dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE season=? '
               "AND (home_team='NYK' OR away_team='NYK') ORDER BY date,id",(season,))]
    return {'games':games,'checked_at':meta['source_checked_at'],'url':meta.get('source'),
            'source_kind':meta['source_kind'],'metadata':meta,'revision':meta['revision']}


def calendar_context(store, date, team):
    """Return scheduled team rest/opponent, never assumed player participation."""
    with store.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM season_schedule WHERE date<=? '
               'AND (home_team=? OR away_team=?) ORDER BY date DESC LIMIT 3',(date,team,team))]
    target=next((r for r in rows if r['date']==date),None)
    previous=next((r for r in rows if r['date']<date and r['status'] not in {'canceled','postponed'}),None)
    if not target:
        return {}
    year=int(date[:4])-(int(date[5:7])<7)
    meta=store.get_checkpoint(f'season_calendar:{year}-{str(year+1)[-2:]}',{})
    rest_covered=(team in meta.get('covered_teams',[]) and meta.get('complete_for_published_games') is True
                  and not meta.get('unassigned_coverage_unknown')
                  and not (meta.get('unassigned_games',0) and not meta.get('unassigned_windows')))
    if previous and any(window['start']<=date and window['end']>=previous['date']
                        for window in meta.get('unassigned_windows',[])):
        rest_covered=False
    return {'game_id':target['id'],'opponent':target['away_team'] if target['home_team']==team else target['home_team'],
            'venue':'Home' if target['home_team']==team else 'Away','tipoff':target['start_time'],
            'status':target['status'],'source':target['source'],'source_asof':target['source_asof'],
            'previous_scheduled_team_game':previous['date'] if previous else None,
            'scheduled_rest_days':max(0,(dt.date.fromisoformat(date)-dt.date.fromisoformat(previous['date'])).days-1) if previous and rest_covered else None,
            'participation_note':'Team calendar dates do not establish which players participated.'}


def refreshed_rest_rows(store, now=None):
    """Derive rest only where a published calendar covers the whole interval.

    Actual final games must match the saved date and opponents. No player-log gap
    or incomplete league game inventory can establish a team's rest schedule.
    The calendar source date stays visible, including saved-publication limits.
    """
    now=now or dt.datetime.now(dt.timezone.utc)
    today=now.astimezone(EASTERN).date().isoformat()
    with store.connect() as db:
        games=[dict(row) for row in db.execute(
            "SELECT s.* FROM season_schedule s JOIN games g ON s.id=g.id "
            "WHERE g.status='final' AND g.date=s.date AND g.home_team=s.home_team "
            "AND g.away_team=s.away_team AND s.date<=? "
            "AND s.status NOT IN ('canceled','postponed')",(today,))]
    result=[]
    for game in games:
        for team in (game['home_team'],game['away_team']):
            context=calendar_context(store,game['date'],team)
            days=context.get('scheduled_rest_days')
            if days is None:
                continue
            # A same-season calendar cannot prove a prior offseason interval.
            previous=context.get('previous_scheduled_team_game')
            if not previous or current_season(dt.datetime.fromisoformat(previous).replace(tzinfo=EASTERN))!=game['season']:
                continue
            bucket='0 (B2B)' if days==0 else str(days) if days in (1,2) else '3-6' if days<=6 else '7+'
            result.append({'team':team,'date':game['date'],'game_id':game['id'],
                           'opponent':context['opponent'],'venue':context['venue'],
                           'previous_team_game':previous,'rest_days':days,'rest_bucket':bucket,
                           'offseason_gap':False,'source':game['source'],
                           'source_asof':game['source_asof'],'source_kind':game['source_kind'],
                           'status':'derived_from_published_calendar',
                           'note':'Rest follows the complete published interval. Later unverified schedule changes may be missed.'})
    return result
