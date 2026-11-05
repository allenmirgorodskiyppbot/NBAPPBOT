"""Shared basketball entity resolution and bounded, source-backed retrieval.

Names come from stored observations and workbook records. Aliases help resolve a
question; neither a matched name nor a dated projection proves a current roster,
lineup, or actual defensive assignment. Retrieved text is untrusted evidence.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import math
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


def assignment_rows(store, master=None):
    cached = getattr(store, '_research_assignments', None)
    if cached is not None:
        if master is not None:
            from assignment_refresh import assignment_rows_for
            return assignment_rows_for(master, cached)
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
    if master is not None:
        from assignment_refresh import assignment_rows_for
        return assignment_rows_for(master, rows)
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
    normalized_question = _key(result['question'])
    pronoun = re.search(r'\b(?:he|him|his)\b', normalized_question)
    named_position = normalized_question.find(_key(result['players'][0])) if len(result['players']) == 1 else -1
    pronoun_comparison = bool(named_position >= 0 and pronoun
        and (pronoun.group() != 'his' or pronoun.start() < named_position)
        and re.search(r'\b(?:compar\w*|versus|vs|against|than|between)\b', str(question), re.I))
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
            if (not result.get('inherited_career_scope') and previous['players']
                    and re.search(r'\b(?:career|lifetime|all[ -]time|all[ -]seasons)\b', turn.get('content', ''), re.I)):
                prior_scope = _requested_population({'original_question': turn['content']})
                if prior_scope['season_basis'] == 'available career':
                    result['inherited_career_scope'] = prior_scope
            if pronoun_comparison and previous['players']:
                # "How does he compare to Maxey?" names a new player without
                # replacing the unambiguous player referenced by the pronoun.
                # Multiple previous players remain ambiguous; do not guess or
                # reach back to an older topic to manufacture an antecedent.
                if len(previous['players']) == 1 and previous['players'][0] not in result['players']:
                    inherited = previous['players'][0]
                    result['players'] = ([inherited, *result['players']] if pronoun.start() < named_position else
                                         [*result['players'], inherited])
                    result['inherited_player'] = True
                # The old opponent is not automatically a shared opponent for
                # both players. Explicit teams in this question remain intact.
                break
            if not result['players'] and len(previous['players']) == 1:
                result['players'] = previous['players']
                result['inherited_player'] = True
            if not result['teams'] and len(previous['teams']) == 1:
                result['teams'] = previous['teams']
            if result['players'] and result['teams']:
                break
    from career_identity import resolve_career_identity
    result['players'] = list(dict.fromkeys(resolve_career_identity(master, name)['player']
                                          for name in result['players']))
    result['original_question'] = str(question)
    return result


def canonical_history(store, master, history):
    names = _players(store, master)
    return [{'role': turn['role'], 'content': _scan(turn['content'], names)['question']
             if turn['role'] == 'user' else turn['content']} for turn in history or []]


def _opponent(resolved, master):
    text = str(resolved.get('original_question') or resolved.get('question') or '')
    # NYK can be the requested opponent of an opposing player. The historical
    # Knicks-focused default must not discard an explicit "against the Knicks".
    explicit = list(re.finditer(r'\b(?:against|versus|vs\.?)\s+(?:the\s+)?', text, re.I))
    named = {team for match in explicit for team, aliases in TEAM_NAMES.items()
             if any(re.match(r'^' + re.escape(_key(alias)) + r'\b', _key(text[match.end():]))
                    for alias in (team, *aliases))}
    if named:
        return next(iter(named)) if len(named) == 1 else None
    # A named player's team is not a substitute for that player's measured
    # guarding history, even when the question mentions both names and a team.
    if explicit and any(_has(text[match.end():], name) for match in explicit
                        for name in resolved.get('players', [])):
        return None
    teams = [team for team in resolved['teams'] if team != 'NYK']
    if len(teams) == 1:
        return teams[0]
    if teams or master is None:
        return None
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


def assignments_for(store, players, opponent=None, master=None):
    rows = [row for row in assignment_rows(store, master) if row['selected_player'] in players
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
    rows = assignments_for(store, [player], opponent, master)
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
    from assignment_refresh import refresh_caution
    caution = refresh_caution(row)
    if caution:
        answer += '\n\n' + caution
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


def _requested_population(resolved):
    text = str(resolved.get('original_question') or resolved.get('question') or '')
    seasons = []
    for start, end in re.findall(r'\b(20\d{2})[-–](20\d{2}|\d{2})(?![-\d])', text):
        if (int(start) + 1 == int(end) if len(end) == 4 else (int(start) + 1) % 100 == int(end)):
            seasons.append(start + '-' + end[-2:])
    current = dt.datetime.now(EASTERN)
    year = current.year - (current.month < 7)
    relative_previous = not seasons and bool(re.search(r'\b(?:last|previous|prior)\s+season\b', text, re.I))
    if relative_previous:
        year -= 1
    explicit_career = bool(re.search(r'\b(?:career|lifetime|all[ -]time|all[ -]seasons)\b', text, re.I))
    inherited = resolved.get('inherited_career_scope', {}) if not explicit_career and not re.search(
        r'\b(?:(?:this|current|last|previous|prior)\s+season|today|tonight|tomorrow|latest)\b', text, re.I) else {}
    career = explicit_career or bool(inherited)
    result = {'seasons': list(dict.fromkeys(seasons))[:2] or ([] if career and not relative_previous else [f'{year}-{str(year+1)[-2:]}']),
            'season_type': 'Playoffs' if re.search(r'\bplayoffs?\b', text, re.I) else
                           'Play-In' if re.search(r'\bplay[ -]ins?\b', text, re.I) else
                           'Preseason' if re.search(r'\bpreseason\b', text, re.I) else 'Regular Season',
            'season_basis': 'explicit request' if seasons else 'previous season' if relative_previous else 'available career' if career else 'current season'}
    if result['season_basis'] == 'available career':
        result.update(as_of_date=current.date().isoformat(), cutoff_exclusive=current.date().isoformat())
        result.update({key: inherited[key] for key in ('as_of_date', 'cutoff_exclusive', 'from_date') if key in inherited})
        upper = re.search(r'\b(as\s+of|before|prior\s+to|through)\s+(\d{4}-\d{2}-\d{2})\b', text, re.I)
        lower = re.search(r'\b(since|after|from)\s+(\d{4}-\d{2}-\d{2})\b', text, re.I)
        try:
            if upper:
                date = dt.date.fromisoformat(upper.group(2))
                result['as_of_date'] = date.isoformat()
                if upper.group(1).casefold() == 'through':
                    date += dt.timedelta(days=1)
                result['cutoff_exclusive'] = min(date, current.date()).isoformat()
            if lower:
                date = dt.date.fromisoformat(lower.group(2))
                if lower.group(1).casefold() == 'after':
                    date += dt.timedelta(days=1)
                result['from_date'] = date.isoformat()
        except ValueError:
            result['scope_error'] = 'The requested career date is invalid. Use a real YYYY-MM-DD date.'
        result['aggregation'] = ('totals' if re.search(r'\b(?:totals?|how\s+many)\b', text, re.I) else
                                 'averages' if re.search(r'\baverag\w*\b', text, re.I) else inherited.get('aggregation', 'averages'))
    return result


def _context_assignments(store, players, opponent, population, master=None):
    # A comparison without one requested opponent has no relevant assignment
    # pair. Arbitrary first-sheet opponents can crowd out both players' actual
    # baselines and wrongly suggest that a prior matchup is still the topic.
    if not opponent:
        return []
    rows = []
    for row in assignment_rows(store, master):
        if row['selected_player'] not in players or opponent and row['opponent_team'] != opponent:
            continue
        stamp = str(row.get('assignment_asof') or '')
        try:
            date = dt.date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        year = date.year - (date.month < 7)
        season = f'{year}-{str(year+1)[-2:]}'
        if season not in population['seasons']:
            continue
        # Undeclared phase cannot establish an explicitly requested playoff sample.
        if population['season_type'] != 'Regular Season' and row.get('season_type') != population['season_type']:
            continue
        rows.append({**row, 'season': season, 'population': 'tentative_assignment',
                     'assignment_date': stamp[:10], 'assignment_direction': 'unspecified',
                     'season_type': row.get('season_type') or 'Unknown',
                     'verified_game_day_assignment': False, 'source_sheet': 'Matchup_Assignments'})
    selected = []
    for key in dict.fromkeys((r['selected_player'], r['opponent_team'], r['season']) for r in rows):
        group = [r for r in rows if (r['selected_player'], r['opponent_team'], r['season']) == key]
        latest = max(str(r['assignment_asof']) for r in group)
        selected.extend(r for r in group if str(r['assignment_asof']) == latest)
    return selected[:8]


def _player_sample(db, player, season, phase, opponent=None):
    fields = ('pts', 'reb', 'ast', 'minutes', 'tpm')
    filters = ["p.player=?", "p.season=?", "p.season_type=?", "g.status='final'",
               "LOWER(p.appearance_state)='played'", 'p.date<=?']
    args = [player, season, phase, dt.datetime.now(EASTERN).date().isoformat()]
    if opponent:
        filters.append('p.opponent=?'); args.append(opponent)
    rows = [dict(row) for row in db.execute('SELECT p.* FROM player_games p JOIN games g ON g.id=p.game_id WHERE '
             + ' AND '.join(filters) + ' ORDER BY p.date DESC,p.game_id DESC', args)]
    values = {field: [r[field] for r in rows if r.get(field) is not None] for field in fields}
    return {'entity': player, 'player': player, 'opponent': opponent,
            'population': 'player_vs_team_whole_games' if opponent else 'all_opponents',
            'season': season, 'season_type': phase, 'n': len(rows),
            'observed_counts': {field: len(v) for field, v in values.items()},
            'averages': {field: sum(v)/len(v) if v else None for field, v in values.items()},
            'first_date': min((r['date'] for r in rows), default=None),
            'data_through_date': max((r['date'] for r in rows), default=None),
            'source_records': [{k: r.get(k) for k in ('game_id', 'date', 'source')} for r in rows[:2]],
            'source_records_truncated': len(rows) > 2,
            'coverage_note': 'Stored observations may be incomplete; no matching rows does not prove no such games occurred. Whole-game samples are not measured defender matchups.'}


def _summary_scope(db, row, published):
    value = dict(row)
    is_published = (value.get('actual_teams') == 'published all-team aggregate' and published
                    and published.get('season_type') == value['season_type'])
    value.update(entity=value['player'], population='all_opponents', games=value['games_played'],
                 population_note='Season averages across all actual opponents, not a named-opponent or named-defender sample.',
                 data_through_date=value.get('through_date'), published_at=None)
    if is_published:
        value.update(published_at=published.get('published_at'), source=published.get('source'),
                     source_kind='published season totals')
    else:
        sample = _player_sample(db, value['player'], value['season'], value['season_type'])
        value.update(source_kind='stored completed observations', source_records=sample['source_records'],
                     source_records_truncated=sample['source_records_truncated'])
    return value


def _tracking_context(record):
    category = record.get('category')
    population = ('team_points_and_field_goals_allowed_all_opponents' if category == 'Team defense' else
                  'team_production_all_opponents' if category == 'Team totals' else
                  'measured_named_pair' if category == 'Named pair' else 'player_tracking_all_opponents')
    return {**{key: record.get(key) for key in ('entity', 'category', 'season', 'season_type', 'gp', 'fields',
                                               'data_through_date', 'published_at', 'source')},
            'population': population}


def _career_metrics(question):
    """Dimensions supported by the stored career calculation, not a forecast."""
    from grounded_reply import requested_metrics
    selected = requested_metrics(question)
    raw = {'fga': r'\b(?:fga|field[- ]goals?\s+attempt(?:ed|s)?|fg\s+attempt(?:ed|s)?|shots?\s+attempted)\b',
           'tpa': r'\b(?:tpa|3pta|three[- ]point\s+attempts?)\b',
           'fgm': r'\b(?:fgm|field[- ]goals?\s+made)\b',
           'ftm': r'\b(?:ftm|free[- ]throws?\s+made)\b',
           'fta': r'\b(?:fta|free[- ]throws?\s+attempt(?:ed|s)?)\b',
           'oreb': r'\boffensive\s+rebounds?\b|\boreb\b',
           'dreb': r'\bdefensive\s+rebounds?\b|\bdreb\b',
           'fouls': r'\b(?:fouls?|pf)\b', 'plus_minus': r'\bplus[ -]minus\b'}
    explicit = [key for key, pattern in raw.items() if re.search(pattern, question, re.I)]
    compounds = [key for key in ('pra', 'pr', 'pa', 'ra', 'stocks')
                 if re.search(r'\b' + key + r'\b', question, re.I)]
    return list(dict.fromkeys(compounds or explicit or selected))


def _career_names(master, player):
    """Use explicit collected identity bindings; never suffix/fuzzy-merge people."""
    from career_identity import resolve_career_identity
    identity = resolve_career_identity(master, player)
    if identity.get('ambiguous'):
        return []
    bound = {identity.get('player') or player, *identity.get('aliases', [])}
    scope = master.get_checkpoint('dataset_refresh_scope', {}) or {}
    key = _key(player)
    matches = [row for row in scope.get('players', []) if key in
               {_key(value) for value in [row.get('player', ''), *row.get('aliases', [])]}]
    ids = {str(row['espn_player_id']) for row in matches if row.get('espn_player_id')}
    if len(ids) > 1 or (ids and identity.get('espn_player_id') and identity['espn_player_id'] not in ids):
        return []
    aliases = {player, *bound}
    for row in matches:
        aliases.update(value for value in [row.get('player'), *row.get('aliases', [])] if value)
    return sorted(aliases)


def _career_sample(db, player, population, metrics, names, opponent=None):
    """Aggregate available dated game observations once, across actual teams."""
    from history_backfill import history_exclusion
    from master import STAT_COMPONENTS
    from feeds import canonical_game, canonical_team
    from grounded_reply import METRICS
    requested = list(dict.fromkeys(metrics))
    raw_fields = ('pts', 'reb', 'ast', 'minutes', 'tpm', 'stl', 'blk', 'tov',
                  'fga', 'tpa', 'fgm', 'ftm', 'fta', 'oreb', 'dreb', 'fouls', 'plus_minus')
    fields = set(raw_fields)
    filters = ['p.player IN (' + ','.join('?' for _ in names) + ')', 'p.season_type=?',
               "g.status='final'", "LOWER(p.appearance_state)='played'", 'g.date=p.date', 'p.date<?']
    args = [*names, population['season_type'], population['cutoff_exclusive']]
    if population.get('from_date'):
        filters.append('p.date>=?'); args.append(population['from_date'])
    if opponent:
        filters.append('p.opponent=?'); args.append(opponent)
    rows = [dict(row) for row in db.execute(
        'SELECT p.*,g.home_team AS event_home,g.away_team AS event_away '
        'FROM player_games p JOIN games g ON g.id=p.game_id WHERE ' + ' AND '.join(filters)
        + ' ORDER BY p.date DESC,p.game_id DESC,p.updated_at DESC', args)]
    grouped, rejected = {}, 0
    for row in rows:
        try:
            day = dt.date.fromisoformat(row['date'])
            season = re.fullmatch(r'(\d{4})-(\d{2})', str(row.get('season') or ''))
            if (not season or int(season[2]) != (int(season[1]) + 1) % 100
                    or day.year not in {int(season[1]), int(season[1]) + 1}
                    or history_exclusion(row)):
                rejected += 1; continue
        except (TypeError, ValueError):
            rejected += 1; continue
        team, other = canonical_team(row.get('team')), canonical_team(row.get('opponent'))
        home, away = canonical_team(row.get('event_home')), canonical_team(row.get('event_away'))
        if not home or not away or {team, other} != {home, away} or team == other:
            rejected += 1; continue
        minutes = row.get('minutes')
        if minutes is not None and (type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0):
            rejected += 1; continue
        grouped.setdefault((canonical_game(row['date'], home, away), team), []).append(row)
    samples, conflicts = [], 0
    for aliases in grouped.values():
        if len({row['season'] for row in aliases}) != 1:
            rejected += 1; continue
        sample = dict(aliases[0])
        for field in fields:
            values = {row.get(field) for row in aliases if type(row.get(field)) in (int, float)
                      and math.isfinite(row[field]) and (row[field] >= 0 or field == 'plus_minus')}
            sample[field] = next(iter(values)) if len(values) == 1 else None
            if len(values) > 1:
                conflicts += 1
        samples.append(sample)
    values, counts, totals = {}, {}, {}
    for metric in requested:
        if metric.endswith('_pct'):
            made, attempted = {'fg_pct': ('fgm', 'fga'), 'three_pct': ('tpm', 'tpa'),
                               'ft_pct': ('ftm', 'fta')}[metric]
            complete = [r for r in samples if r[made] is not None and r[attempted] is not None
                        and r[made] <= r[attempted]]
            denominator = sum(r[attempted] for r in complete)
            counts[metric] = len(complete)
            values[metric] = sum(r[made] for r in complete) / denominator if denominator else None
            continue
        components = STAT_COMPONENTS.get(metric, (metric,))
        observed = [sum(r[field] for field in components) for r in samples
                    if all(r.get(field) is not None for field in components)]
        counts[metric] = len(observed)
        totals[metric] = sum(observed) if observed else None
        values[metric] = sum(observed) / len(observed) if observed else None
    return {'entity': player, 'player': player, 'opponent': opponent, 'season': 'Available career',
            'season_type': population['season_type'], 'season_basis': 'available career',
            'population': 'player_vs_team_whole_games' if opponent else 'career_all_opponents',
            'n': len(samples), 'games': len(samples), 'aggregation': population.get('aggregation', 'averages'),
            'observed_counts': counts, 'averages': values, 'totals': totals,
            **{METRICS[m][0]: v for m, v in values.items()},
            **{METRICS[m][2]: counts[m] for m in requested if METRICS[m][2]},
            'actual_teams': sorted({r['team'] for r in samples}),
            'seasons': sorted({r['season'] for r in samples}),
            'first_date': min((r['date'] for r in samples), default=None),
            'data_through_date': max((r['date'] for r in samples), default=None),
            'cutoff_exclusive': population['cutoff_exclusive'], 'from_date': population.get('from_date'),
            'source_records': [{key: r.get(key) for key in ('game_id', 'date', 'source')} for r in samples[:2]],
            'source_records_truncated': len(samples) > 2, 'excluded_rows': rejected,
            'duplicate_rows': sum(len(group) - 1 for group in grouped.values()),
            'conflicting_metric_values': conflicts, 'published_at': None,
            'source_kind': 'stored completed career observations',
            'coverage_note': ('Available stored career history; full career completeness is not independently verified. '
                              'Missing statistics and DNPs are not zeroes. Whole-game opponent samples do not establish measured guarding.')}


def research_context(store, master, resolved):
    """Retrieve explicitly scoped evidence; dates and populations never substitute."""
    from news_digest import is_promotion
    population = _requested_population(resolved)
    opponent = _opponent(resolved, master)
    context = {'resolved_players': resolved['players'], 'resolved_teams': resolved['teams'],
               'requested_population': population, 'name_resolution_is_not_roster_verification': True,
               'assignment_projections': _context_assignments(store, resolved['players'], opponent, population, master),
               'player_sheets': [], 'news': [], 'current_player_statistics': [],
               'opponent_player_statistics': [], 'current_tracking': [],
               'freshness_priority': 'Prefer newer evidence only for the same entity, metric, population, season and phase. Broad season totals never replace opponent samples or tentative assignments. Covered game dates, publication dates and assignment dates are distinct.'}
    career = population['season_basis'] == 'available career'
    if career:
        context['requested_metrics'] = _career_metrics(str(resolved.get('original_question') or resolved.get('question') or ''))
        if population.get('scope_error'):
            context['population_error'] = population['scope_error']
            return context
    availability_question = bool(re.search(
        r'\b(?:availab\w*|health\w*|injur\w*|questionable|ruled\s+out)\b',
        str(resolved.get('original_question') or resolved.get('question') or ''), re.I))
    if availability_question:
        # Feed metadata can establish collection limitations, never that an
        # individual player is healthy. Do not send source error bodies, URLs,
        # or configuration values into the model context.
        context['availability_coverage'] = {'feeds': [], 'complete_official_report_in_context': False}
    if master is not None and callable(getattr(master, 'connect', None)):
        with master.connect() as db:
            if availability_question and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='feed_status'").fetchone():
                for row in db.execute("SELECT name,last_success,last_attempt,last_error FROM feed_status "
                                      "WHERE name IN ('availability_coverage','injuries_configured') ORDER BY name"):
                    value = {'name': row['name']}
                    for key in ('last_success', 'last_attempt', 'last_error'):
                        try:
                            value[key] = dt.datetime.fromisoformat(str(row[key]).replace('Z', '+00:00')).isoformat()
                        except (TypeError, ValueError):
                            value[key] = None
                    context['availability_coverage']['feeds'].append(value)
            tracking_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='refreshed_tracking'").fetchone()
            if career:
                for player in resolved['players'][:2]:
                    names = _career_names(master, player)
                    if not names:
                        context['population_error'] = 'The requested player identity is ambiguous; a verified player identity is needed before combining career records.'
                        continue
                    context['current_player_statistics'].append(
                        _career_sample(db, player, population, context['requested_metrics'], names))
                    if opponent:
                        context['opponent_player_statistics'].append(
                            _career_sample(db, player, population, context['requested_metrics'], names, opponent))
            for season in population['seasons']:
                phase = population['season_type']
                for player in resolved['players'][:2]:
                    tracking = ([json.loads(r[0]) for r in db.execute(
                        'SELECT payload_json FROM refreshed_tracking WHERE entity=? AND season=? ORDER BY category',
                        (player, season))] if tracking_exists else [])
                    tracking = [r for r in tracking if r.get('season_type') == phase]
                    published = next((r for r in tracking if r.get('category') == 'Player totals'), None)
                    summaries = [dict(r) for r in db.execute(
                        'SELECT * FROM season_averages WHERE player=? AND season=? AND season_type=?', (player, season, phase))]
                    context['current_player_statistics'].extend(_summary_scope(db, r, published) for r in summaries)
                    # Preserve an undated publication alongside dated partial
                    # observations; never replace the shared observed view.
                    represented_publication = any(r.get('actual_teams') == 'published all-team aggregate' for r in summaries)
                    context['current_tracking'].extend(_tracking_context(r) for r in tracking
                        if r.get('category') != 'Player totals' or not summaries
                        or (not r.get('data_through_date') and not represented_publication))
                    if opponent:
                        context['opponent_player_statistics'].append(_player_sample(db, player, season, phase, opponent))
                if tracking_exists:
                    for team in resolved['teams'][:2]:
                        context['current_tracking'].extend(_tracking_context(value) for r in db.execute(
                            "SELECT payload_json FROM refreshed_tracking WHERE entity=? AND season=? AND category IN ('Team totals','Team defense') ORDER BY category", (team, season))
                            if (value := json.loads(r[0])).get('season_type') == phase)
    # Raw profile rows are a dated reference; they must not supply a different
    # requested season/phase merely because the first few sheet rows contain it.
    if population['season_basis'] == 'current season' and population['season_type'] == 'Regular Season':
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
    # Opponent defense supplies more directly relevant matchup context than
    # optional broad player tracking; keep it earlier when trimming from the end.
    context['current_tracking'].sort(key=lambda item: (item.get('category') != 'Team defense',
                                                      item.get('category') != 'Player totals'))
    # Preserve the requested assignment and explicit opponent sample before
    # optional profiles, news and broad tracking. Whole omitted blocks are marked.
    while len(json.dumps(context, default=str)) > 5500:
        key = next((key for key in ('player_sheets', 'news', 'current_tracking',
                   'current_player_statistics', 'opponent_player_statistics', 'assignment_projections') if context[key]), None)
        if key is None:
            break
        context[key].pop()
        omitted = context.setdefault('omitted_evidence', {})
        omitted[key] = omitted.get(key, 0) + 1
    return context
