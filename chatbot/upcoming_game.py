"""Fast, read-only next-game previews from saved schedules and dated evidence.

A calendar lookup never becomes a new schedule check, availability report or
player projection. Conversation history supplies only the selected NBA team.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from zoneinfo import ZoneInfo

from basketball_research import TEAM_NAMES, _has
from feeds import canonical_game, canonical_team
from game_phase import regular_phase_exclusion
from news_digest import _aware
from season_calendar import current_season

EASTERN = ZoneInfo('America/New_York')
_FOLLOWUP = re.compile(r'\b(?:what to expect|what (?:can|should|do) (?:we|i) expect|'
                      r'what should (?:we|i) watch|preview (?:it|that)|how does it look)\b', re.I)
_WORKFLOW = re.compile(r'\b(?:deploy(?:ment|ing|ed)?|refresh(?:ed|ing)?|updat(?:e[ds]?|ing)|'
                      r'import(?:s|ed|ing)?|upload(?:s|ed|ing)?|download(?:s|ed|ing)?|'
                      r'excel|workbooks?|install(?:ation|ed|ing)?|commands?|docker|akash|files?|coding|code|tests?)\b', re.I)
_FOCUS = re.compile(r'\bNext (?:game|saved matchup) for\s+([A-Z]{3})\s*\(', re.I)
_PROP_BOUNDARY = re.compile(r'\b(?:more|less|over|under)\s*(?:\d+(?:\.\d+)?|\.\d+)(?![\d.])|'
    r'\b\d+(?:\.\d+)?\s*(?:points?|pts|rebounds?|rebs?|assists?|asts?|pra|threes|3ptm|3pm|fga|tpa)\b', re.I)


def _text(message):
    if hasattr(message, 'model_dump'):
        message = message.model_dump()
    return str(message.get('content') or '') if isinstance(message, dict) else ''


def _teams(text):
    return [team for team, aliases in TEAM_NAMES.items()
            if any(_has(text, alias) for alias in (team, *aliases))]


def _normalize(text):
    text = str(text or '')[:3000].replace('’', "'")
    text = re.sub(r'\b(what\s*\x27?s?)\s+uo\s+next\b', r'\1 up next', text, flags=re.I)
    return re.sub(r'\bupnext\b', 'up next', text, flags=re.I)


def _history_team(history):
    for message in reversed((history or [])[-12:]):
        text = _text(message)
        match = _FOCUS.search(text)
        if match and canonical_team(match[1]) in TEAM_NAMES:
            return canonical_team(match[1])
        if _PROP_BOUNDARY.search(text):
            return None
        if _WORKFLOW.search(text) and not re.search(r'\b(?:nba|basketball|games?|matchups?|opponent)\b', text, re.I):
            return None
    return None


def _context_barrier(history):
    for message in reversed((history or [])[-12:]):
        text = _text(message)
        if _FOCUS.search(text):
            return False
        if _PROP_BOUNDARY.search(text):
            return True
        if _WORKFLOW.search(text) and not re.search(r'\b(?:nba|basketball|games?|matchups?|opponent)\b', text, re.I):
            return True
    return False


def wants_next_game_request(text, history=None):
    """Recognize an NBA next-game request, including bounded mobile typos."""
    text = _normalize(text)
    if _PROP_BOUNDARY.search(text):
        # "More 1.5 threes next game" supplies an offered prop. Its temporal
        # qualifier does not request a schedule preview or replace that line.
        return False
    teams = _teams(text)
    if _WORKFLOW.search(text) and not re.search(r'\b(?:nba|basketball|games?|matchups?|opponent)\b', text, re.I):
        return False
    if _FOLLOWUP.search(text):
        if re.search(r'\b(?:pra|fga|tpa|3pm|3ptm|props?|line|more or less|up or down)\b', text, re.I):
            return False
        return bool(_history_team(history) or teams and re.search(r'\bgame\b', text, re.I))
    if re.search(r'\b(?:next|upcoming)\s+(?:(?:nba|knicks|\w+)\s+)?(?:game|matchup|opponent)\b|'
                 r'\b(?:game|opponent)\s+(?:is\s+)?next\b', text, re.I):
        return True
    if teams and re.search(r'\b(?:next|upcoming)\b', text, re.I):
        return True
    if re.search(r'\b(?:who|what team)\s+(?:(?:do|will|are)\s+)?we\s+'
                 r'(?:play(?:ing)?|fac(?:e|ing))\b', text, re.I):
        return True
    if re.fullmatch(r"\s*(?:what\s*'?s?|who\s*'?s?)\s+(?:up\s+)?next[?.!\s]*", text, re.I):
        last = ' '.join(_text(item) for item in (history or [])[-2:])
        return not (_WORKFLOW.search(last) and not _history_team(history))
    return False


def _checkpoint(store, key, default=None):
    if callable(getattr(store, 'get_checkpoint', None)):
        return store.get_checkpoint(key, default)
    with store.connect() as db:
        row = db.execute('SELECT value_json FROM checkpoints WHERE name=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _team_choice(text, history, context):
    teams = _teams(text)
    if len(teams) == 1:
        # "Who do we play against Cleveland?" still has the Knicks as subject.
        if re.search(r'\bwe\b.*\b(?:against|versus|vs\.?|at)\b', text, re.I):
            return 'NYK', []
        return teams[0], []
    if len(teams) > 1:
        subjects = []
        for candidate in teams:
            for alias in (candidate, *TEAM_NAMES[candidate]):
                escaped = re.escape(alias)
                if re.search(r'\bnext\s+' + escaped + r'\s+game\b|\b' + escaped +
                             r"(?:'s)?\s+next\s+game\b|\bnext\s+game\s+for\s+" + escaped + r'\b', text, re.I):
                    subjects.append(candidate)
        if len(set(subjects)) == 1:
            return subjects[0], []
        return None, teams
    chosen = canonical_team(context) if context else None
    return (chosen if chosen in TEAM_NAMES else _history_team(history) or 'NYK'), []


def _schedule(store, team, now):
    today = now.astimezone(EASTERN).date().isoformat()
    with store.connect() as db:
        calendar = [dict(row) for row in db.execute('SELECT * FROM season_schedule WHERE date>=? '
            'AND (home_team=? OR away_team=?) ORDER BY date,id LIMIT 100', (today, team, team))] if _table(db, 'season_schedule') else []
        observed = [dict(row) for row in db.execute('SELECT * FROM games WHERE date>=? '
            'AND (home_team=? OR away_team=?) ORDER BY date,id LIMIT 100', (today, team, team))] if _table(db, 'games') else []
    games = {row['id']: {**row, '_saved_calendar': True} for row in calendar}
    for row in observed:
        prior = games.get(row['id'], {})
        conflict = any(prior.get(key) and row.get(key) and prior[key] != row[key]
                       for key in ('date', 'home_team', 'away_team', 'start_time'))
        games[row['id']] = {**prior, **{k:v for k,v in row.items() if v is not None},
                             '_schedule_conflict': conflict}
    eligible = []
    for row in games.values():
        try:
            day = dt.date.fromisoformat(str(row['date']))
            if row['id'] != canonical_game(row['date'], row['home_team'], row['away_team']):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        if (row.get('status') not in {'scheduled', 'time_tbd', 'unknown'} or
                row['home_team'] not in TEAM_NAMES or row['away_team'] not in TEAM_NAMES):
            continue
        tip = _aware(row.get('start_time'))
        if tip and (tip.astimezone(EASTERN).date() != day or tip <= now):
            continue
        # A same-day unknown time cannot establish that a game is still ahead.
        if not tip and day <= now.astimezone(EASTERN).date():
            continue
        row['_tipoff'] = tip
        eligible.append(row)
    return min(eligible, key=lambda row:(row['date'], row.get('start_time') or '99', row['id'])) if eligible else None


def _verified_timing(store, game, now):
    saved = _checkpoint(store, 'nba_schedule_today', {}) or {}
    checked = _aware(saved.get('checked_at'))
    if (saved.get('status') != 'success' or saved.get('error') or not saved.get('source')
            or saved.get('date') != game['date'] or not checked
            or not 0 <= (now-checked).total_seconds() <= 900):
        return None
    source = str(saved['source'])
    if not (source.startswith('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?')
            or source == 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'):
        return None
    matches = [row for row in saved.get('games', []) if isinstance(row, dict) and row.get('id') == game['id']]
    if len(matches) != 1:
        return None
    actual = matches[0]
    if (any(actual.get(key) != game.get(key) for key in ('date','home_team','away_team'))
            or actual.get('status') != 'scheduled'
            or actual.get('reported_status') in {'live','final','postponed','canceled','cancelled'}
            or not _aware(actual.get('start_time')) or _aware(actual['start_time']) != game['_tipoff']):
        return None
    return {'checked_at': saved['checked_at'], 'source': source}


def _coverage(store, team, game, now):
    metadata = _checkpoint(store,'season_calendar:'+current_season(now),{}) or {}
    covered = {canonical_team(value) for value in metadata.get('covered_teams',[])}
    if metadata.get('team'):
        covered.add(canonical_team(metadata['team']))
    established = (team in covered and metadata.get('complete_for_published_games') is True
                   and not metadata.get('unassigned_coverage_unknown')
                   and not (metadata.get('unassigned_games') and not metadata.get('unassigned_windows')))
    today = now.astimezone(EASTERN).date().isoformat()
    for window in metadata.get('unassigned_windows',[]):
        if str(window.get('start') or '') <= game['date'] and str(window.get('end') or '') >= today:
            established = False
    return {'selected_team_coverage_established':bool(established),'covered_teams':sorted(covered),
            'source_checked_at':metadata.get('source_checked_at')}


def _form(store, team, now):
    today = now.astimezone(EASTERN).date().isoformat()
    season = current_season(now); first = season[:4] + '-07-01'
    with store.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM games WHERE status='final' AND date>=? AND date<=? "
            'AND (home_team=? OR away_team=?) ORDER BY date DESC,id DESC LIMIT 20', (first,today,team,team))]
    valid = []
    for row in rows:
        if not row.get('source'):
            continue
        try:
            if row['id'] != canonical_game(row['date'],row['home_team'],row['away_team']):
                continue
            if regular_phase_exclusion(row):
                continue
        except ValueError:
            continue
        if _aware(row.get('start_time')) and _aware(row['start_time']) > now:
            continue
        scores = [row.get('home_score'),row.get('away_score')]
        if any(type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 400
               or int(value) != value for value in scores) or scores[0] == scores[1]:
            continue
        won = scores[0] > scores[1] if row['home_team'] == team else scores[1] > scores[0]
        valid.append((row, won))
        if len(valid) == 5:
            break
    if not valid:
        return None
    points_for = [row['home_score'] if row['home_team']==team else row['away_score'] for row,_ in valid]
    points_against = [row['away_score'] if row['home_team']==team else row['home_score'] for row,_ in valid]
    return {'team':team,'games':len(valid),'wins':sum(won for _,won in valid),
            'losses':sum(not won for _,won in valid),'data_through_date':valid[0][0]['date'],
            'points_for_average':round(sum(points_for)/len(valid),1),
            'points_against_average':round(sum(points_against)/len(valid),1),
            'sources':list(dict.fromkeys(row['source'] for row,_ in valid)),
            'scope':'Latest saved current-season final games with known scores; completeness is not established.'}


def _availability(store, game, now):
    from structured_availability import current_official_availability
    with store.connect() as db:
        if not _table(db, 'structured_availability'):
            return []
        players = [row[0] for row in db.execute('SELECT player FROM structured_availability WHERE game_date=? '
            'AND team IN (?,?) AND player IS NOT NULL GROUP BY player ORDER BY MAX(report_at) DESC LIMIT 30',
            (game['date'],game['home_team'],game['away_team']))]
    result = []
    for player in players:
        record = current_official_availability(store, player, game['date'], now)
        if record.get('known'):
            result.append(record)
    priority = {'Out':0,'Doubtful':1,'Questionable':2,'Probable':3,'Available':4}
    result.sort(key=lambda row:(priority.get(row['status'],5),row['player']))
    return result[:3]


def _clock(stamp):
    local = stamp.astimezone(EASTERN)
    return local.strftime('%I:%M %p').lstrip('0') + ' Eastern'


def build_preview(store, text='', history=None, *, now=None, team=None):
    """Return a computed preview, or None when the message is not a next-game request."""
    text = _normalize(text)
    if _PROP_BOUNDARY.search(text):
        return None
    if not wants_next_game_request(text,history) and not (team and _FOLLOWUP.search(text)
            and not _WORKFLOW.search(text) and not _context_barrier(history)
            and not re.search(r'\b(?:pra|fga|tpa|3pm|3ptm|props?|line)\b', text, re.I)):
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Next-game previews require a timezone-aware clock.')
    selected, ambiguous = _team_choice(text,history,team)
    if ambiguous:
        return {'answer':'Which team’s next game do you mean: ' + ' or '.join(ambiguous) + '?',
                'sources':[],'ai_used':False,'response_mode':'computed'}
    base = {'sources':[],'ai_used':False,'response_mode':'computed','upcoming_game_team':selected}
    if store is None:
        return {**base,'answer':'The saved NBA schedule is not ready yet, so I can’t establish the next game.'}
    game = _schedule(store,selected,current)
    if not game:
        return {**base,'answer':f'I don’t have a saved upcoming {selected} game. A current published schedule is needed to establish its next opponent and time.'}
    opponent = game['away_team'] if game['home_team'] == selected else game['home_team']
    timing = _verified_timing(store,game,current)
    coverage = _coverage(store,selected,game,current)
    venue = 'home' if game['home_team'] == selected else 'away'
    day = dt.date.fromisoformat(game['date'])
    date_label = f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}, {day.year}"
    tip_label = _clock(game['_tipoff']) if game['_tipoff'] else 'time TBD'
    matchup = game['away_team'] + ' at ' + game['home_team']
    if game.get('_schedule_conflict') and not timing:
        matchup = selected + ' vs ' + opponent
        venue = 'venue needs confirmation'
    title = 'Next game' if coverage['selected_team_coverage_established'] else 'Next saved matchup'
    heading = f"{title} for {selected} ({TEAM_NAMES[selected][-1]}): {matchup} — {date_label}, {tip_label} ({venue})."
    if timing:
        qualification = f"Timing independently checked at {_clock(_aware(timing['checked_at']))}."
    else:
        asof = str(game.get('source_asof') or '')[:10]
        label = 'saved calendar' if game.get('_saved_calendar') else 'saved schedule records'
        qualification = ('Scheduled in the ' if game['status']=='scheduled' else 'Listed in the ') + label + (f' as of {asof}' if asof else '') + '; current timing is unverified.'
        if game.get('_schedule_conflict'):
            qualification = 'Saved schedule sources disagree; the listed time and venue need a current check.'
    if not coverage['selected_team_coverage_established']:
        qualification += f" Full {selected} schedule coverage is not established; this may not be their next NBA game."
    forms = [record for record in (_form(store,selected,current),_form(store,opponent,current)) if record]
    details = []
    if forms:
        details.append('Recent saved form: ' + '; '.join(f"{row['team']} {row['wins']}–{row['losses']} in {row['games']} stored finals through {row['data_through_date']}" for row in forms) + '. This is a saved sample, not a forecast.')
    by_team = {row['team']:row for row in forms}
    if selected in by_team and opponent in by_team:
        own,other = by_team[selected],by_team[opponent]
        details.append(f"Scoring/defense samples: {selected} scored {own['points_for_average']:g}/game while {opponent} allowed {other['points_against_average']:g}; "
                       f"{opponent} scored {other['points_for_average']:g} while {selected} allowed {own['points_against_average']:g}. These use different opponents.")
    availability = _availability(store,game,current)
    if availability:
        details.append('Saved official availability: ' + '; '.join(f"{row['player']} — {row['status']} (report {_aware(row['report_at']).astimezone(EASTERN).date().isoformat()} {_clock(_aware(row['report_at']))})" for row in availability) + '. These designations do not establish complete lineups.')
    else:
        details.append('Game-specific availability and starting lineups are not verified here yet.')
    details.append('What to watch: if pace or rotation minutes change, shot opportunities and counting stats can change too.')
    sources = [{'tool':'saved_schedule','source':game.get('source') or 'saved schedule',
                'source_asof':game.get('source_asof'),'checked_at':game.get('checked_at'),
                'timing_verified':bool(timing)}]
    if timing:
        sources.append({'tool':'independent_schedule_check',**timing})
    for form in forms:
        sources.extend({'tool':'saved_team_results','team':form['team'],'source':source,
                        'data_through_date':form['data_through_date']} for source in form['sources'])
    sources.extend({'tool':'official_availability','source':row['source_url'],
                    'report_at':row['report_at'],'player':row['player']} for row in availability)
    focus = {key:game.get(key) for key in ('id','date','home_team','away_team','start_time','status')}
    focus.update(team=selected,opponent=opponent,timing_verified=bool(timing))
    return {**base,'answer':heading+' '+qualification+'\n\n'+'\n'.join(details),
            'sources':sources,'next_game_focus':focus,'recent_form':forms,'availability':availability,
            'schedule_coverage':coverage,
            'grounding':'Saved schedule and dated observations; no current-game pick or injury inference.'}
