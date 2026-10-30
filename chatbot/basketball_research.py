"""Shared basketball entity resolution and bounded, source-backed retrieval.

Names come from stored observations and workbook records. Aliases help resolve a
question; neither a matched name nor a dated projection proves a current roster,
lineup, or actual defensive assignment. Retrieved text is untrusted evidence.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from workbook import quote

EASTERN = ZoneInfo('America/New_York')
TEAM_NAMES = {
    'ATL': ('Atlanta', 'Hawks'), 'BOS': ('Boston', 'Celtics'), 'BKN': ('Brooklyn', 'Nets'),
    'CHA': ('Charlotte', 'Hornets'), 'CHI': ('Chicago', 'Bulls'), 'CLE': ('Cleveland', 'Cavaliers', 'Cavs'),
    'DAL': ('Dallas', 'Mavericks', 'Mavs'), 'DEN': ('Denver', 'Nuggets'), 'DET': ('Detroit', 'Pistons'),
    'GSW': ('Golden State', 'Warriors'), 'HOU': ('Houston', 'Rockets'), 'IND': ('Indiana', 'Pacers'),
    'LAC': ('Los Angeles Clippers', 'LA Clippers', 'Clippers'),
    'LAL': ('Los Angeles Lakers', 'LA Lakers', 'Lakers'), 'MEM': ('Memphis', 'Grizzlies'),
    'MIA': ('Miami', 'Heat'), 'MIL': ('Milwaukee', 'Bucks'), 'MIN': ('Minnesota', 'Timberwolves', 'Wolves'),
    'NOP': ('New Orleans', 'Pelicans'), 'NYK': ('New York', 'Knicks', 'New York Knicks'),
    'OKC': ('Oklahoma City', 'Thunder'), 'ORL': ('Orlando', 'Magic'),
    'PHI': ('Philadelphia', '76ers', 'Sixers', 'Philly'), 'PHX': ('Phoenix', 'Suns'),
    'POR': ('Portland', 'Trail Blazers', 'Blazers'), 'SAC': ('Sacramento', 'Kings'),
    'SAS': ('San Antonio', 'Spurs'), 'TOR': ('Toronto', 'Raptors'), 'UTA': ('Utah', 'Jazz'),
    'WAS': ('Washington', 'Wizards')}
_STOP = set('against defensive percentage opponent matchup matching average averaging points '
            'rebounds assists tomorrow tonight current previous information starting injured '
            'injuries questionable availability summary summarise summarize season regular '
            'playoffs player players shooting allowed guards guards defending explain minutes '
            'compare difference better basketball anything latest collected reports update '
            'updates actually probable doubtful confirmed primary alternate about their'.split())


def _words(value):
    value = re.sub(r"['’]s\b", '', str(value), flags=re.I)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return re.findall(r'[a-z0-9]+', value.lower())


def _key(value):
    return ' '.join(_words(value))


def _has(text, phrase):
    return bool(re.search(r'(?<!\w)' + re.escape(_key(phrase)) + r'(?!\w)', _key(text)))


def assignment_rows(store):
    cached = getattr(store, '_research_assignments', None)
    if cached is not None:
        return cached
    info = getattr(store, 'sheets', {}).get('Matchup_Assignments')
    if not info or 'selected_player' not in info.get('columns', []):
        return []
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM ' + quote(info['sql_table']))]
    today = dt.datetime.now(EASTERN).date().isoformat()
    rows = [row for row in rows if row.get('selected_player') and row.get('opponent_team')
            and row.get('assignment_asof') and str(row['assignment_asof'])[:10] <= today]
    store._research_assignments = rows
    return rows


def _players(store, master):
    names = set()
    for row in assignment_rows(store):
        names.update(str(row[field]) for field in ('selected_player', 'primary', 'alternate') if row.get(field))
    if master is not None and callable(getattr(master, 'connect', None)):
        with master.connect() as db:
            names.update(row[0] for row in db.execute('SELECT player FROM player_games '
                         'WHERE player IS NOT NULL GROUP BY player ORDER BY MAX(date) DESC LIMIT 1600'))
    # Dated measured pairs contain opponent names even when the master has only
    # Knicks observations; do not use undated aggregate rows as measured evidence.
    if getattr(store, 'sheets', {}).get('Pair_Tracking'):
        from matchup_queries import _records
        for row in _records(store):
            names.update(str(row[field]) for field in ('offense_player', 'defense_player') if row.get(field))
    return sorted(name for name in names if len(_words(name)) >= 2 and len(name) <= 100)


def _scan(text, names):
    normalized, found, replacements, occupied = _key(text), [], {}, set()
    for name in names:
        if _has(text, name):
            found.append((normalized.index(_key(name)), name))
            occupied.update(_words(name))
    aliases = {}
    for name in names:
        words = _words(name)
        for token in {words[-1], words[0]}:
            if len(token) >= 2 and token not in _STOP:
                aliases.setdefault(token, set()).add(name)
    for alias, name in {'kat': 'Karl-Anthony Towns', 'deuce': 'Miles McBride'}.items():
        if name in names:
            aliases.setdefault(alias, set()).add(name)
    tokens = _words(text)
    for index, token in enumerate(tokens):
        if token in occupied:
            continue
        candidates = aliases.get(token, set())
        if len(candidates) == 1:
            name = next(iter(candidates))
            found.append((normalized.find(token), name))
            replacements[token] = name
            occupied.update(_words(name))
            continue
        if len(token) < 6 or token in _STOP or token in aliases:
            continue
        # A typo is corrected only with a strong unique surname match. Ambiguous
        # names stay unresolved instead of silently choosing a different player.
        surname_aliases = {word: choices for word, choices in aliases.items()
                           if len(word) >= 6 and any(_words(n)[-1] == word for n in choices)}
        close = sorted(((difflib.SequenceMatcher(None, token, word).ratio(), word)
                        for word in surname_aliases), reverse=True)
        if close and close[0][0] >= .88 and (len(close) == 1 or close[0][0] - close[1][0] >= .08):
            choices = surname_aliases[close[0][1]]
            if len(choices) == 1:
                name = next(iter(choices))
                found.append((normalized.find(token), name))
                replacements[token] = name
                occupied.update(_words(name))
    players = list(dict.fromkeys(name for _, name in sorted(found)))
    teams = [team for team, aliases_ in TEAM_NAMES.items() if
             _has(text, team) or any(_has(text, alias) for alias in aliases_)]
    rewritten = str(text)
    for alias, name in sorted(replacements.items(), key=lambda item: -len(item[0])):
        # Replace only the spoken alias; preserve percentages, dates and query
        # filters for existing statistics tools. Full names are never rewritten twice.
        rewritten = re.sub(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', name, rewritten, flags=re.I)
    return {'players': players[:4], 'teams': teams[:3], 'question': rewritten}


def resolve_question(store, master, question, history=None):
    names = _players(store, master)
    result = _scan(question, names)
    # A short team follow-up ("and against Charlotte?") can retain the previous
    # player. A new explicit player replaces the topic rather than being merged.
    followup = bool(re.search(r'\b(he|his|him|they|their)\b', str(question), re.I) or
                    (len(str(question)) < 110 and ((result['teams'] and
                     re.search(r'\b(matchup|against|versus|vs|guards?|defends?)\b', str(question), re.I)) or
                     re.match(r'\s*(why|how|what about|and|but)\b', str(question), re.I))))
    if followup:
        for turn in reversed(history or []):
            if turn.get('role') != 'user':
                continue
            previous = _scan(turn.get('content', ''), names)
            if not result['players'] and len(previous['players']) == 1:
                result['players'] = previous['players']
                result['inherited_player'] = True
            if not result['teams'] and len(previous['teams']) == 1:
                result['teams'] = previous['teams']
            if result['players'] and result['teams']:
                break
    result['original_question'] = str(question)
    return result


def canonical_history(store, master, history):
    names = _players(store, master)
    return [{'role': turn['role'], 'content': _scan(turn['content'], names)['question']
             if turn['role'] == 'user' else turn['content']} for turn in history or []]


def _opponent(resolved, master):
    teams = [team for team in resolved['teams'] if team != 'NYK']
    if len(teams) == 1:
        return teams[0]
    if teams or master is None:
        return None
    text = resolved['original_question']
    if not re.search(r'\b(today|tonight|tomorrow|next game)\b', text, re.I):
        return None
    snapshot = master.get_checkpoint('knicks_digest_schedule_v1', {})
    from news_digest import _aware
    checked = _aware(snapshot.get('checked_at'))
    now = dt.datetime.now(dt.timezone.utc)
    if not checked or not 0 <= (now - checked).total_seconds() < 1200:
        return None
    day = now.astimezone(EASTERN).date()
    if 'tomorrow' in text.lower():
        day += dt.timedelta(days=1)
    games = [game for game in snapshot.get('games', []) if game.get('date') == day.isoformat()
             and game.get('status') not in {'postponed', 'canceled', 'time_tbd'}]
    if len(games) == 1:
        return games[0]['away_team'] if games[0]['home_team'] == 'NYK' else games[0]['home_team']
    return None


def assignments_for(store, players, opponent=None):
    rows = [row for row in assignment_rows(store) if row['selected_player'] in players
            and (not opponent or row['opponent_team'] == opponent)]
    if not opponent:
        return rows[:4]
    selected = []
    for player in players:
        group = [row for row in rows if row['selected_player'] == player]
        if group:
            latest = max(str(row['assignment_asof']) for row in group)
            selected.extend(row for row in group if str(row['assignment_asof']) == latest)
    return selected[:8]


def answer_assignment_question(store, master, question, history=None, resolved=None):
    resolved = resolved or resolve_question(store, master, question, history)
    text = resolved['original_question']
    intent = bool(re.search(r'\b(match(?:ups?|ed\s+up|es\s+up)?|guard(?:s|ing)?|defend(?:s|ing)?|cover(?:s|ing)?|assigned)\b', text, re.I))
    followup = resolved.get('inherited_player') and resolved['teams'] and len(text) < 80
    if not (intent or followup) or not resolved['players']:
        return None
    if re.search(r'percent|%|\b(average\w*|points|rebounds|assists|minutes|how|why|explain|reasons?|factors?|compare|better|'
                 r'picks?|parlays?|chance|odds|rating|last|career|history)\b', text, re.I):
        return None
    if re.search(r'\b20\d{2}(?:[-–/]\d{1,2})?\b', text):
        return None  # Historical/date filters need explicit research, never today's saved pairing.
    if len(resolved['players']) > 1:
        return None  # Multiple named players can be a measured guarding pair.
    player = resolved['players'][0]
    opponent = _opponent(resolved, master)
    def result(answer, rows=None):
        return {'answer': answer, 'ai_used': False,
                'sources': [{'tool': 'matchup_assignments', 'arguments': {'player': player,
                             'opponent': opponent}, 'excel_rows': [r['_row'] for r in rows or []]}]}
    if not opponent:
        return result(f'Which opponent do you mean for {player}? I can look up the saved matchup pairing. '
                      'A confirmed game-day assignment also depends on the current lineup.')
    rows = assignments_for(store, [player], opponent)
    if not rows:
        return result(f'I don’t have a saved matchup assignment for {player} against {opponent}. '
                      'The current lineup and defensive scheme would be needed to project it; I won’t infer who guards whom from whole-game stats.')
    pairs = {(row.get('primary'), row.get('alternate')) for row in rows}
    if len(pairs) != 1:
        return result(f'The saved assignment records for {player} against {opponent} conflict. '
                      'I need a current lineup/source to resolve that pairing.', rows)
    row = rows[0]
    primary, alternate = row.get('primary'), row.get('alternate')
    if not primary:
        return result(f'The saved {player}–{opponent} assignment has no named primary matchup.', rows)
    title = TEAM_NAMES.get(opponent, (opponent,))[-1]
    answer = (f"For {player} against the {title}, the workbook lists **{primary}** as the primary "
              + (f"matchup and **{alternate}** as the alternate. " if alternate else 'matchup. ')
              + f"That is a tentative projection dated {str(row['assignment_asof'])[:10]}, not a confirmed game-day assignment.\n\n"
              + 'The record does not specify the direction of the assignment, so it does not establish who guards whom. '
              + 'Current lineups, availability, and switches can change the pairing.\n'
              + f"Source: Matchup_Assignments, Excel row {row['_row']}; assignment {row.get('assignment_id') or 'ID unavailable'}.")
    return result(answer, rows)


class ScopedNewsStore:
    """News reader scoped to resolved players/teams; no inferred health status."""
    def __init__(self, master, resolved):
        self.master, self.resolved = master, resolved

    def status(self):
        return self.master.status()

    def recent_news(self, limit=100):
        players, teams = self.resolved['players'], self.resolved['teams']
        if not players and not teams:
            return self.master.recent_news(limit)
        if callable(getattr(self.master, 'connect', None)):
            with self.master.connect() as db:
                rows = [dict(row) for row in db.execute('SELECT * FROM news ORDER BY updated_at DESC LIMIT 1000')]
        else:
            rows = self.master.recent_news(limit=100)
        aliases = players if players else [alias for team in teams for alias in (team, *TEAM_NAMES.get(team, ()))]
        selected = [row for row in rows if any(_has(str(row.get('title', '')) + ' ' + str(row.get('body', '')), name)
                    for name in aliases) or (not players and row.get('team') in teams)]
        return selected[:max(1, min(limit, 100))]


def research_context(store, master, resolved):
    """Preload relevant evidence so ordinary questions need fewer model tool rounds."""
    from news_digest import is_promotion
    context = {'resolved_players': resolved['players'], 'resolved_teams': resolved['teams'],
               'name_resolution_is_not_roster_verification': True,
               'assignment_projections': assignments_for(store, resolved['players'], _opponent(resolved, master)),
               'player_sheets': [], 'news': [], 'current_player_statistics': [], 'current_tracking': []}
    if master is not None and callable(getattr(master,'connect',None)):
        current=dt.datetime.now(EASTERN);year=current.year-(current.month<7);season=f'{year}-{str(year+1)[-2:]}'
        with master.connect() as db:
            for player in resolved['players'][:2]:
                context['current_player_statistics'].extend(dict(row) for row in db.execute(
                    "SELECT * FROM season_averages WHERE player=? AND season=? AND season_type='Regular Season'",(player,season)))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='refreshed_tracking'").fetchone():
                    context['current_tracking'].extend(json.loads(row[0]) for row in db.execute(
                        'SELECT payload_json FROM refreshed_tracking WHERE entity=? AND season=? LIMIT 3',(player,season)))
            for team in resolved['teams'][:2]:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='refreshed_tracking'").fetchone():
                    context['current_tracking'].extend(json.loads(row[0]) for row in db.execute(
                        "SELECT payload_json FROM refreshed_tracking WHERE entity=? AND season=? AND category IN ('Team totals','Team defense')",(team,season)))
        context['freshness_priority']='Current master summaries take precedence over the dated workbook snapshot. Tracking records retain their own source timestamps.'
    for player in resolved['players'][:2]:
        if player in getattr(store, 'sheets', {}):
            context['player_sheets'].append(store.read(player, 7, 4))
    if master is not None and (resolved['players'] or resolved['teams']):
        for item in ScopedNewsStore(master, resolved).recent_news(limit=10):
            if is_promotion(item):
                continue
            context['news'].append({key: str(item.get(key) or '')[:(180 if key == 'body' else 800 if key == 'url' else 350)]
                                   for key in ('title', 'body', 'published_at', 'updated_at', 'source', 'url')})
            if len(context['news']) == 3:
                break
    # Original field names, worksheet coordinates and dates remain available.
    # Drop whole evidence blocks to meet the context bound; never cut JSON midway.
    while len(json.dumps(context, default=str)) > 5500:
        if context['player_sheets']:
            context['player_sheets'].pop()
        elif context['news']:
            context['news'].pop()
        elif context['assignment_projections']:
            context['assignment_projections'].pop()
        elif context['current_tracking']:
            context['current_tracking'].pop()
        else:
            break
    return context
