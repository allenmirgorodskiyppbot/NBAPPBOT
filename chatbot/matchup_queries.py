"""Natural-language access to measured pair rows, with explicit metric and scope."""
import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

from master import normalized_name
from workbook import quote


def _records(store):
    cached = getattr(store, '_measured_pair_cache', None)
    if cached is not None:
        return cached
    info = getattr(store, 'sheets', {}).get('Pair_Tracking')
    if not info:
        return []
    db = store.connect()
    try:
        cursor = db.execute('SELECT * FROM ' + quote(info['raw_table']) + ' ORDER BY _row')
        headers = None
        records = []
        for raw in cursor:
            values = list(raw)[1:]
            if {'offense_player', 'defense_player', 'fgm', 'fga', 'date'} <= set(
                    value for value in values if isinstance(value, str)):
                headers = values
                continue
            if headers and values and values[0] is not None:
                record = dict(zip(headers, values))
                if record.get('offense_player') and record.get('defense_player') and record.get('game_id'):
                    try:
                        dt.date.fromisoformat(str(record.get('date'))[:10])
                    except ValueError:
                        continue
                    record['_row'] = raw[0]
                    records.append(record)
        store._measured_pair_cache = records
        return records
    finally:
        db.close()


def _names(text, players):
    key = normalized_name(text)
    found = []
    for player in players:
        full = normalized_name(player)
        if full in key:
            found.append((key.index(full), player))
            continue
        surname = player.split()[-1]
        candidates = [name for name in players if name.split()[-1].casefold() == surname.casefold()]
        match = re.search(r'\b' + re.escape(surname) + r'\b', text, re.I)
        if match and len(candidates) == 1:
            found.append((len(normalized_name(text[:match.start()])), player))
    return [player for _, player in sorted(found)]


def answer_matchup_question(store, question, history=None):
    text = str(question).strip()
    history = history or []
    last_assistant = next((turn['content'] for turn in reversed(history) if turn['role'] == 'assistant'), '')
    missing_defender = re.fullmatch(r'Which player is defending (.+)\?', last_assistant)
    if missing_defender and len(text) < 80:
        text = f'What is {text} defensive percentage against {missing_defender.group(1)}?'
    if re.fullmatch(r'(yes|yes please|fg%|field.goal percentage)[.! ]*', text, re.I) and \
            last_assistant.startswith('Do you mean field-goal percentage allowed'):
        text = last_assistant
    if not (re.search(r'\b(defen\w*|guard\w*|against|versus|vs|matchup)\b', text, re.I)
            and re.search(r'percent|%|shoot|field.goal', text, re.I)):
        return None
    records = _records(store)
    players = sorted({str(row[field]) for row in records for field in ('offense_player', 'defense_player')})
    mentioned = _names(text, players)
    defensive = bool(re.search(r'defen|allow\w*|guarding', text, re.I))
    if re.search(r'\b(last|recent|career|historical|history|since|before|after|home|away|difference|'
                 r'differential|effective|efg|rim|three|3pt|3p)\b', text, re.I):
        return None
    if len(mentioned) == 1 and re.search(r'\b(his|he|him|their)\b', text, re.I):
        for turn in reversed(history):
            if turn['role'] != 'user':
                continue
            previous = [name for name in _names(turn['content'], players) if name not in mentioned]
            if len(previous) == 1:
                mentioned = previous + mentioned if defensive else mentioned + previous
                break
    def result(answer):
        return {'answer': answer, 'sources': [], 'ai_used': False}
    if len(mentioned) != 2:
        return result('Which player is defending ' + mentioned[0] + '?' if len(mentioned) == 1 and defensive else
                      'Which offensive player and defender do you mean? You can give their names in a normal sentence.')
    defender, offense = mentioned if defensive else list(reversed(mentioned))
    verb = re.search(r'\b(guards?|defends?|is defending|allows?)\b', text, re.I)
    if verb:
        before = normalized_name(text[:verb.start()])
        named_defender = [name for name in mentioned if before.endswith(normalized_name(name))
                          or before.endswith(normalized_name(name.split()[-1]))]
        if len(named_defender) == 1:
            defender = named_defender[0]
            offense = next(name for name in mentioned if name != defender)
    fg = bool(re.search(r'fg\s*%|field.goal|shooting percentage', text, re.I))
    if not fg:
        return result(f'Do you mean field-goal percentage allowed when {defender} defends {offense}, '
                      'or a different defensive metric? You do not need to supply shot counts.')
    season_match = re.search(r'\b(20\d{2})[-–](\d{2})\b', text)
    season = season_match.group(0).replace('–', '-') if season_match else None
    if season is None and re.search(r'\b(this|current) season\b', text, re.I):
        current = dt.datetime.now(ZoneInfo('America/New_York'))
        year = current.year if current.month >= 7 else current.year - 1
        season = f'{year}-{(year + 1) % 100:02d}'
    phase = 'Playoffs' if 'playoff' in text.casefold() else 'Regular Season'
    today = dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    pair = [row for row in records if row['offense_player'] == offense and row['defense_player'] == defender
            and str(row.get('date', ''))[:10] <= today and str(row.get('season_type', '')).casefold() == phase.casefold()]
    if season is None and pair:
        season = max(str(row.get('season') or '') for row in pair)
    pair = [row for row in pair if row.get('season') == season]
    if not pair:
        return result(f'No dated measured {phase} matchup rows for {offense} against {defender}'
                      + (f' in {season}' if season else '') + ' were found. Whole-game coappearances '
                      'do not establish who guarded whom, so I cannot substitute them for defensive FG%.')
    valid, seen = [], {}
    for row in pair:
        key = (row['game_id'], row['offense_player'], row['defense_player'])
        counts = (row.get('fgm'), row.get('fga'))
        if key in seen and seen[key] != counts:
            return result('Conflicting measured matchup rows were found. The percentage needs source review.')
        if key in seen:
            continue
        seen[key] = counts
        if all(type(number) in (int, float) and math.isfinite(number) and number >= 0
               and float(number).is_integer() for number in counts) \
                and counts[0] <= counts[1]:
            valid.append(row)
    made = sum(row['fgm'] for row in valid)
    attempted = sum(row['fga'] for row in valid)
    if not attempted:
        return result('No complete, positive field-goal-attempt sample is available for that measured matchup. '
                      'A percentage cannot be calculated from missing data or zero attempts.')
    dates = sorted({str(row['date'])[:10] for row in valid})
    games = len({row['game_id'] for row in valid})
    rows = ', '.join(str(row['_row']) for row in valid)
    sources = ', '.join(sorted({str(row.get('source_id') or 'unknown') for row in valid}))
    answer = (f'{offense} shot {made / attempted * 100:.1f}% with {defender} recorded as defender '
              f'in the stored measured matchup rows: {made:g} makes / {attempted:g} attempts across {games} game(s).\n'
              f'Scope: {season} {phase}; {dates[0]} through {dates[-1]}. '
              'This is FG% allowed in that tracked sample, not an overall defensive rating.\n'
              f'Source: Pair_Tracking, Excel row(s) {rows}; source ID(s): {sources}.\n'
              'Small tracked samples do not establish defensive ability or predict the next matchup.')
    if len(valid) < len(seen):
        answer += f' {len(seen) - len(valid)} row(s) with missing/invalid shooting counts were excluded.'
    return {'answer': answer, 'ai_used': False,
            'sources': [{'tool': 'measured_matchup', 'arguments': {'offense': offense, 'defender': defender,
                          'season': season, 'phase': phase}}]}
