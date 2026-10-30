"""Read-only player logs and arithmetic; common lookups do not require inference."""
from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from master import STAT_ALIASES, STAT_COMPONENTS, normalized_name

_NUMBERS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
_METRICS = [('pts', r'\b(points?|pts|ppg)\b'), ('reb', r'\b(rebounds?|reb|rpg)\b'),
            ('ast', r'\b(assists?|ast|apg)\b'), ('tpm', r'\b(3pm|three[- ]pointers? made)\b'),
            ('stl', r'\b(steals?|stl)\b'), ('blk', r'\b(blocks?|blk)\b'),
            ('tov', r'\b(turnovers?|tov)\b'), ('minutes', r'\bminutes\b')]


@contextmanager
def _open(store):
    path = getattr(store, 'database_path', None) or store.path
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _resolve(db, text, exact=False):
    names = list(db.execute("SELECT DISTINCT player_id,player FROM player_games "
                            "WHERE player IS NOT NULL AND TRIM(player)<>''"))
    key = normalized_name(text)
    full = [row for row in names if normalized_name(row['player']) == key] if exact else [
        row for row in names if normalized_name(row['player']) in key]
    if full:
        # A full name must not silently hide a second player named by surname.
        if not exact and len({normalized_name(row['player']) for row in full}) == 1:
            last = full[0]['player'].split()[-1].casefold()
            remainder = re.sub(r'\b' + re.escape(full[0]['player']) + r'\b', ' ', text, flags=re.I)
            others = [row for row in names if row['player'].split()[-1].casefold() != last
                      and re.search(r'\b' + re.escape(row['player'].split()[-1]) + r'\b', remainder, re.I)]
            if others:
                return None
        candidates = full
    else:
        candidates = [row for row in names if re.search(
            r'\b' + re.escape(row['player'].split()[-1]) + r'\b', text, re.I)]
    ids = {row['player_id'] for row in candidates}
    return candidates[0] if len(ids) == 1 else None


def player_log(store, player, stats=('pts',), limit=5, season=None, phase='Regular Season', exact=True,
               all_games=False):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Player-log limits must be between 1 and 20 games.')
    if not stats or len(stats) > 4 or any(stat not in STAT_COMPONENTS for stat in stats):
        raise ValueError('Choose up to four supported statistics.')
    if season is not None and (not re.fullmatch(r'20\d{2}-\d{2}', season)
                              or (int(season[:4]) + 1) % 100 != int(season[-2:])):
        raise ValueError('Use a season such as 2026-27.')
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    with _open(store) as db:
        match = _resolve(db, player, exact=exact)
        if match is None:
            return {'answer': 'The player name is missing or ambiguous in the stored master. '
                    'Use /playerlog FULL PLAYER NAME | points | 5.', 'sources': [], 'ai_used': False}
        where = ["p.player_id=?", "LOWER(p.appearance_state)='played'", "LOWER(g.status)='final'",
                 'LOWER(p.season_type)=LOWER(?)', 'substr(p.date,1,10)<=?']
        params = [match['player_id'], phase, today]
        if season:
            where.append('p.season=?')
            params.append(season)
        predicate = ' AND '.join(where)
        sql = 'SELECT p.* FROM player_games p JOIN games g ON g.id=p.game_id WHERE ' + predicate
        rows = [dict(row) for row in db.execute(sql + ' ORDER BY p.date DESC,p.game_id DESC' +
                ('' if all_games else ' LIMIT ?'), params if all_games else params + [limit])]
        newest = db.execute('SELECT MAX(substr(p.date,1,10)) FROM player_games p '
                            'JOIN games g ON g.id=p.game_id WHERE ' + predicate, params).fetchone()[0]
    name = match['player']
    scope = phase + (', ' + season if season else ', all stored seasons')
    if not rows:
        return {'answer': f'No completed {scope} games played by {name} were found in the stored master. '
                'This is missing stored coverage, not proof the player did not play.',
                'sources': [], 'ai_used': False}
    def value(row, stat):
        values = [row.get(field) for field in STAT_COMPONENTS[stat]]
        return sum(values) if all(isinstance(number, (int, float)) and not isinstance(number, bool)
                                 and math.isfinite(number) for number in values) else None
    values = {stat: [value(row, stat) for row in rows] for stat in stats}
    def number(value):
        return 'missing' if value is None else f'{value:g}'
    heading = f'{name} — {len(rows)} stored games played.' if all_games else \
              f'{name} — last {len(rows)} of up to {limit} requested stored games played.'
    lines = [heading,
             'Filter: final games; Played; ' + scope + '.']
    for index, row in enumerate(rows):
        if all_games:
            break
        detail = ', '.join(stat + '=' + number(values[stat][index]) for stat in stats)
        lines.append(str(row['date'])[:10] + ' vs ' + str(row.get('opponent') or 'unknown opponent') + ': ' + detail)
    for stat in stats:
        observed = [value for value in values[stat] if value is not None]
        average = f'{statistics.mean(observed):.2f}' if observed else 'unavailable'
        lines.append(f'Average {stat}: {average}; {len(observed)}/{len(rows)} observed, '
                     f'{len(rows) - len(observed)} missing (missing values excluded, never zero-filled).')
    lines.append('Sample dates: ' + min(str(row['date'])[:10] for row in rows) + ' through ' +
                 max(str(row['date'])[:10] for row in rows) + '.')
    lines.append('Latest stored completed player game in this scope: ' + str(newest) + '.')
    mode = os.getenv('NBA_DATA_SOURCE', 'espn')
    lines.append('Statistics mode: ' + ('morning existing-dataset refresh at 11:30 AM Eastern; manual uploads also supported; check /status for covered dates.'
                 if os.getenv('DAILY_NBA_REFRESH','1')=='1' else mode +
                 ('; updates require uploads.' if mode=='manual' else '; check /status for feed freshness.')))
    sources = sorted({str(row.get('source') or 'source not supplied') for row in rows})
    groups = {}
    for source in sources:
        cell = re.search(r';row=(\d+)$', source)
        base = source[:cell.start()] if cell else source
        groups.setdefault(base, []).extend([int(cell.group(1))] if cell else [])
    for source, source_rows in groups.items():
        citation = source[:220] + ('…' if len(source) > 220 else '')
        if source_rows:
            citation += '; rows=' + ','.join(str(row) for row in sorted(set(source_rows)))
        lines.append('Source: ' + citation)
    lines.append('Stored coverage may be incomplete; these calculations are not a live league-wide feed.')
    return {'answer': '\n'.join(lines), 'sources': [{'tool': 'player_log', 'arguments': {
        'player_id': match['player_id'], 'stats': list(stats), 'limit': None if all_games else limit,
        'phase': phase, 'season': season}}],
        'ai_used': False, 'data': {'games': rows, 'averages': {stat: statistics.mean(
            [value for value in values[stat] if value is not None]) if any(value is not None for value in values[stat])
            else None for stat in stats}}}


def answer_player_question(store, question, history=None):
    text = str(question)
    lower = text.casefold()
    # Tracking qualifiers define different metrics, not synonyms for box-score
    # assists/rebounds/minutes. Leave them to the structured research tools.
    if re.search(r'\b(?:potential[\s_-]+(?:assists?|ast)|secondary[\s_-]+assists?|'
                 r'hockey[\s_-]+assists?|assist[\s_-]+chances?|screen[\s_-]+assists?|'
                 r'passes|passing|touch(?:es)?|possessions?|usage|usg|tracking|'
                 r'rebound(?:ing)?[\s_-]+chances?|reb[\s_-]+chances?|'
                 r'contested|uncontested|offensive[\s_-]+rebounds?|defensive[\s_-]+rebounds?|'
                 r'box[\s_-]*outs?|drives?|deflections?|loose[\s_-]+balls?|hustle|'
                 r'(?:offensive|defensive|net)[\s_-]+ratings?|pace|pie|'
                 r'percent(?:ages?|iles?)?|efficiency|true[\s_-]+shooting|'
                 r'effective[\s_-]+field[\s_-]+goals?|efg|ts|'
                 r'field[\s_-]+goals?|free[\s_-]+throws?|fga|fgm|fta|ftm|tpa|'
                 r'per[\s_-]+(?:100|36|48))\b|\b(?:fg|ft|3p|3pt|usg)\s*%', lower):
        return None
    # These modifiers need the general query planner; never silently ignore them.
    if re.search(r'\b(compare|versus|vs\.?|against|between|than|home|away|since|before|after|'
                 r'injur\w*|news|parlay|picks?|quarter|half|prediction|probability|week|month|'
                 r'career|lifetime|yesterday|today|january|february|march|april|may|june|july|august|'
                 r'september|october|november|december)\b', lower):
        return None
    if re.search(r'\b20\d{2}-\d{2}-\d{2}\b', lower):
        return None
    from feeds import TEAM_WORDS
    if any(re.search(r'\b' + re.escape(term) + r'\b', lower)
           for team, name in TEAM_WORDS.items() for term in (team.casefold(), name)):
        return None
    match = re.search(r'\b(?:last|latest|recent)\s+(\d{1,3}|' + '|'.join(_NUMBERS) +
                      r')\s+(?:(?:completed|regular[- ]season|played)\s+)*games?\b', lower)
    aggregate = match is None and bool(re.search(r'\b(averag\w*|ppg|rpg|apg)\b', lower))
    if not match and not aggregate:
        return None
    if not match and re.search(r'\blast\b|\ball[- ](?:time|seasons)\b', lower):
        return None
    token = match.group(1) if match else 'five'
    limit = _NUMBERS[token] if token in _NUMBERS else int(token)
    if not 1 <= limit <= 20:
        return None
    stats = [key for key, pattern in _METRICS if re.search(pattern, lower)] or ['pts']
    for combined in ('pra', 'pr', 'pa', 'ra', 'stocks'):
        if re.search(r'\b' + combined + r'\b', lower):
            stats = [combined]
            break
    if len(stats) > 4:
        return None
    season = None
    season_match = re.search(r'\b(20\d{2})[-–](\d{2})(?![-\d])\b', text)
    if season_match:
        season = season_match.group(1) + '-' + season_match.group(2)
        if (int(season[:4]) + 1) % 100 != int(season[-2:]):
            return None
    elif re.search(r'\b20\d{2}\b', text):
        return None
    elif aggregate or re.search(r'\b(this|current) season\b', lower):
        today = dt.datetime.now(ZoneInfo('America/New_York'))
        start = today.year if today.month >= 7 else today.year - 1
        season = f'{start}-{(start + 1) % 100:02d}'
    phase = 'Playoffs' if 'playoff' in lower else 'Preseason' if 'preseason' in lower else 'Regular Season'
    with _open(store) as db:
        player = _resolve(db, text)
        if player is None and re.search(r'\b(his|he|him|their)\b', lower):
            for turn in reversed(history or []):
                if turn['role'] == 'user':
                    player = _resolve(db, turn['content'])
                    if player is not None:
                        break
    if player is None:
        return None
    if aggregate and phase=='Regular Season':
        published=published_average(store,player['player'],stats,season,include_unknown_snapshot=True)
        if published is not None:
            return published
    return player_log(store, player['player'], stats=stats, limit=limit, season=season, phase=phase,
                      all_games=aggregate)


def published_average(store,player,stats,season,include_unknown_snapshot=False):
    """Keep dated observations distinct from an undated published snapshot."""
    import json
    with _open(store) as db:
        row=db.execute("SELECT payload_json FROM refreshed_tracking WHERE category='Player totals' AND entity=? AND season=?",(player,season)).fetchone()
        observed=db.execute("SELECT games_played,through_date FROM observed_season_averages WHERE player=? AND season=? AND season_type='Regular Season'",(player,season)).fetchone()
    if not row:return None
    record=json.loads(row[0]);gp=record.get('gp');fields=record.get('fields',{})
    if not gp or gp<=0:return None
    separate_snapshot=bool(observed and not record.get('data_through_date'))
    if observed:
        if observed['games_played']>gp:
            return None
        if record.get('data_through_date') and observed['through_date']>record['data_through_date']:
            return None
        if separate_snapshot:
            # Unknown coverage may supply useful broader context, but never
            # displaces the dated sample or claims to be the latest average.
            if not include_unknown_snapshot or gp<=observed['games_played']:
                return None
            from news_digest import _aware
            published=_aware(record.get('published_at'))
            if not published or published.astimezone(ZoneInfo('America/New_York')).date().isoformat()<observed['through_date']:
                return None
    averages={}
    for stat in stats:
        components=STAT_COMPONENTS.get(stat)
        if not components or any(fields.get(key) is None for key in components):return None
        averages[stat]=sum(fields[key] for key in components)/gp
    answer=(f'{player} — published {season} regular-season averages, all actual opponents.\n'+
            '\n'.join(f'{stat}: {value:.2f} per game' for stat,value in averages.items())+
            f"\nPublished games played: {gp:g}. Covered game date: {record.get('data_through_date') or 'unknown'}.\n"+
            f"Source publication: {record.get('published_at','unknown')}.\n"+
            'Source: '+str(record.get('source','unavailable'))+
            '\nSeason totals are used once; named-opponent and defender samples remain separate. These aggregates do not provide individual game logs.')
    result={'answer':answer,'ai_used':False,'sources':[{'tool':'published_player_totals','arguments':{'player':player,'season':season}}],
            'data':{'player':player,'averages':averages,'games_played':gp,'scope':'Published all-opponent season totals',
                    'through_date':record.get('data_through_date'),'source_published_at':record.get('published_at'),'games':[]}}
    if separate_snapshot:
        sample=player_log(store,player,stats=stats,season=season,phase='Regular Season',all_games=True)
        sample['answer']+='\n\nSeparate published snapshot:\n'+answer+(
            '\nThis broader snapshot has unknown game coverage; it does not establish the latest season average. '
            'The two samples are not combined.')
        sample['sources']+=result['sources']
        sample['data']['published_snapshot']=result['data']
        return sample
    return result


def player_log_command(store, argument):
    parts = [part.strip() for part in argument.split('|')]
    if not 1 <= len(parts) <= 4 or not parts[0]:
        raise ValueError('Use /playerlog FULL NAME | points | 5 | optional 2026-27')
    stat = (parts[1] if len(parts) > 1 else 'points').casefold()
    stat = STAT_ALIASES.get(stat, stat)
    try:
        limit = int(parts[2]) if len(parts) > 2 else 5
    except ValueError:
        raise ValueError('Player-log limits must be between 1 and 20 games.') from None
    season = parts[3] if len(parts) > 3 and parts[3] else None
    return player_log(store, parts[0], stats=[stat], limit=limit, season=season)['answer']
