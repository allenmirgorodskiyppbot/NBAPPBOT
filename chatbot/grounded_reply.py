"""General evidence contracts for generated explanations, independent of player names.

The model selects evidence and writes an interpretation. Statistics, populations,
dates and citations are rendered from the selected records, not rewritten by it.
Source rendering protects numeric wording and population labels. Model-written
query filters/calculations and qualitative interpretation still need review.
"""
from __future__ import annotations

import json
import math
import re


class ReplyValidationError(ValueError):
    """A safe reason code, never the rejected model text or source payload."""
    REASONS = frozenset({'invalid_json', 'invalid_schema', 'invalid_evidence',
                         'analysis_too_long', 'retrieval_with_analysis',
                         'empty_reply', 'numerical_prose', 'unsupported_assignment', 'unsupported_scope'})

    def __init__(self, reason):
        self.reason = reason if reason in self.REASONS else 'invalid_schema'
        super().__init__(self.reason)


def parse_reply(text):
    """Accept JSON or a single enclosing JSON fence, never extract from prose."""
    if not isinstance(text, str):
        raise ReplyValidationError('invalid_json')
    value = text.strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', value, re.I)
    if fence:
        value = fence.group(1).strip()
    try:
        document=json.loads(value)
        # "reply" requests user-facing text. Keep "analysis" as the internal
        # legacy name so existing validation and saved fixtures remain readable.
        if isinstance(document,dict) and 'reply' in document and 'analysis' not in document:
            document={('analysis' if key=='reply' else key):value for key,value in document.items()}
        return document
    except (ValueError, TypeError):
        raise ReplyValidationError('invalid_json') from None


def has_numerical_assertion(text, reference_rows=()):
    """Reserve quantitative claims for source cards, allowing basketball terms.

    Lexical terms such as the 76ers or three-point shooting are not statistical
    measurements. This does not independently verify qualitative assertions.
    """
    verified_rows = {n for n in reference_rows if type(n) is int and n > 0}
    qualitative = re.sub(r'\brow\s+(\d+)\b',
                         lambda match: 'source row' if int(match.group(1)) in verified_rows else match.group(0), text, flags=re.I)
    qualitative = re.sub(r'\b76ers\b|\btwo[- ]way\b|\bone[- ]on[- ]one\b', '', qualitative, flags=re.I)
    qualitative = re.sub(r'\b(?:two|three)[- ]pointers?\b', '', qualitative, flags=re.I)
    qualitative = re.sub(
        r'\b(?:two|three)[- ]point(?=\s+(?:shooting|shots?|attempts?|range|line|percentage|'
        r'accuracy|defense|scoring|threat|volume|creation|shooters?)\b)', '', qualitative, flags=re.I)
    return bool(re.search(r'\d|\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
                          r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
                          r'thirty|forty|fifty|hundred|thousand)\b', qualitative, re.I))


def has_unsupported_assignment(text, cards):
    """Catch assertive guarding claims without directional source evidence.

    This is a bounded check for a reproduced error, not semantic validation of
    every qualitative claim. Explicit uncertainty and hypothetical assignments
    remain valid; an unrelated caveat cannot license a later assertive clause.
    """
    action = r'(?:guard(?:s|ing|ed)?|defend(?:s|ing|ed)?)'
    adverb = r'(?:(?:(?:most\s+)?likely|probably|presumably|certainly|definitely|also|primarily|mainly|typically|still)\s+){0,3}'
    auxiliary = (r'(?:(?:will|would|should)\s+' + adverb + r'(?:be\s+)?|'
                 r'(?:is|are|was|were)\s+' + adverb + r'(?:(?:expected|projected|scheduled|assigned)\s+to\s+)?(?:be\s+)?|'
                 r'(?:expected|likely|projected)\s+(?:to\s+)?(?:be\s+)?)')
    assertions = re.compile(
        r'\b' + auxiliary + action + r'\b|'
        r'\b' + auxiliary + r'matched\s+(?:up\s+)?(?:onto|on|to|with|against)\b|'
        r'\b(?:his|their|the)\s+(?:likely|primary|expected|projected)\s+defender\b|'
        r'\b(?:guards|defends|covers|marks)\s+(?:him|them)\b', re.I)
    uncertainty = re.compile(
        r'\b(?:unknown|unconfirmed|uncertain|unclear|undetermined)\b|'
        r'\bnot\s+(?:known|confirmed|established|specified)\b|'
        r'\b(?:does|do|did)\s+not\s+(?:know|confirm|establish|specify)\b|'
        r'\bdon[\x27\u2019]t\s+know\b|\bno\s+(?:evidence|confirmation)\b|'
        r'\b(?:cannot|can[\x27\u2019]t)\s+(?:confirm|establish|determine)\b|'
        r'\bwithout\s+(?:a\s+)?confirmed\s+assignment\b', re.I)
    conditional = re.compile(r'\b(?:if|assuming|provided)\b|\bdepending\s+on\b', re.I)
    names = {str(card[key]) for card in cards for key in ('entity', 'primary', 'alternate') if card.get(key)}
    named_object = (re.compile(r'\b(?:guards|defends|covers|marks)\s+(?:'
                    + '|'.join(re.escape(name) for name in names) + r')\b', re.I) if names else None)

    def verified(clause):
        # No current workbook projection qualifies. A future verified source
        # must supply both named roles, and the claim must preserve direction.
        for card in cards:
            if card.get('kind') != 'verified_defensive_assignment' or card.get('verified_game_day_assignment') is not True:
                continue
            defender, offense = card.get('defender'), card.get('offensive_player')
            if not isinstance(defender, str) or not isinstance(offense, str) or not defender or not offense:
                continue
            defender, offense = re.escape(defender), re.escape(offense)
            if (re.search(r'\b'+defender+r'\b(?:\s+\w+){0,6}\s+guard(?:s|ing)?\s+'+offense+r'\b', clause, re.I)
                    or re.search(r'\b'+offense+r'\b(?:\s+\w+){0,6}\s+guarded\s+by\s+'+defender+r'\b', clause, re.I)):
                return True
        return False

    for clause in re.split(r'(?<=[.!?;])\s+|,|\b(?:but|however|although|despite)\b', text):
        if clause.rstrip().endswith('?'):
            continue
        match = assertions.search(clause) or (named_object.search(clause) if named_object else None)
        if not match:
            continue
        caveat = uncertainty.search(clause)
        hypothesis = conditional.search(clause)
        indirect_unknown = bool(caveat and re.search(r'\b(?:who|whether)\b', clause[:match.start()], re.I))
        if ((caveat and caveat.start() < match.start()) or indirect_unknown or (hypothesis and hypothesis.start() < match.start())
                or verified(clause)):
            continue
        return True
    return False


def has_unscoped_absence(text, cards):
    """An incomplete stored sample cannot establish that real games never exist.

    This guards an observed model error, not every possible semantic mistake.
    Official-report and named-defender absence are separate evidence questions.
    """
    if not any(card.get('kind') == 'player_opponent_sample' for card in cards):
        return False
    absence = re.compile(
        r'\bno\s+(?:(?:confirmed|actual|real|completed|matching|direct|prior|recorded)\s+){0,3}'
        r'(?:matchups?|appearances?|games?|game\s+data)\s+(?:exist\w*|occur\w*|happen\w*|is\s+available|are\s+available)\b', re.I)
    for clause in re.split(r'(?<=[.!?;])\s+', text):
        if absence.search(clause) and not re.search(r'\b(?:stored|collected|provided|dataset|database|workbook|sample|records)\b', clause, re.I):
            return True
    return False


def number(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.') if type(value) in (int,float) and math.isfinite(value) else 'unavailable'


def date(value):
    return str(value)[:10] if value else 'unknown'


METRICS = {
    'pts': ('ppg', 'PPG', 'points_observed'),
    'reb': ('rpg', 'RPG', 'rebounds_observed'),
    'ast': ('apg', 'APG', 'assists_observed'),
    'minutes': ('minutes_per_game', 'minutes/game', None),
    'tpm': ('threes_per_game', 'made threes/game', None),
    'stl': ('steals_per_game', 'steals/game', None),
    'blk': ('blocks_per_game', 'blocks/game', None),
    'tov': ('turnovers_per_game', 'turnovers/game', None),
    'fg_pct': ('fg_pct', 'FG%', None),
    'three_pct': ('three_pct', '3P%', None),
    'ft_pct': ('ft_pct', 'FT%', None),
}

# These imported fields are additive source totals, not ordinary box-score
# substitutes. Rates are derived only for display from the same record's GP.
TRACKED_METRICS = {
    'potential_assists': ('Passing', 'potential assists', r'\bpotential[ _-]+assists?\b'),
    'passes': ('Passing', 'passes', r'\bpasses(?:[ _-]+made)?\b'),
    'assists': ('Passing', 'assists', r'\b(?:actual|recorded)[ _-]+assists?\b'),
    'rebound_chances': ('Rebounding', 'rebound chances', r'\brebound[ _-]+chances?\b'),
    'rebounds': ('Rebounding', 'rebounds', r'\b(?:actual|recorded)[ _-]+rebounds?\b'),
    'touches': ('Possessions', 'touches', r'\btouches\b'),
    'possession_minutes': ('Possessions', 'possession minutes', r'\b(?:possession[ _-]+(?:minutes?|time)|time[ _-]+of[ _-]+possession)\b'),
}


def requested_tracking_metrics(question):
    return [key for key, (_, _, pattern) in TRACKED_METRICS.items()
            if re.search(pattern, question, re.I)]


def requested_metrics(question):
    """Select statistic dimensions, never an entity-specific answer template."""
    patterns = {
        'pts': r'\b(?:points?|ppg|scor(?:e|es|ers?|ing))\b',
        'reb': r'\b(?:rebounds?|rpg)\b', 'ast': r'\b(?:assists?|apg)\b',
        'minutes': r'\b(?:minutes?|workload)\b',
        'tpm': r'\b(?:threes|three[- ]pointers?|3pm|made threes|made three[- ]point)\b',
        'stl': r'\bsteals?\b', 'blk': r'\bblocks?\b', 'tov': r'\bturnovers?\b',
        'fg_pct': r'\bfg\s*%|field[- ]goal.*?(?:percent|%)|shooting percentage',
        'three_pct': r'(?:three[- ]point|3p|3pt).*?(?:percent|%)',
        'ft_pct': r'\b(?:free[- ]throw|ft)\b.*?(?:percent|%)',
    }
    selected = [key for key, pattern in patterns.items() if re.search(pattern, question, re.I)]
    if 'pts' in selected:
        scoring_text = re.sub(r'\b(?:three|3)[- ]point(?:ers?)?\b', ' ', question, flags=re.I)
        if not re.search(patterns['pts'], scoring_text, re.I):
            selected.remove('pts')
    for compound, components in {'pra': ('pts', 'reb', 'ast'), 'pr': ('pts', 'reb'),
                                 'pa': ('pts', 'ast'), 'ra': ('reb', 'ast'),
                                 'stocks': ('stl', 'blk')}.items():
        if re.search(r'\b' + compound + r'\b', question, re.I):
            selected.extend(components)
    return list(dict.fromkeys(selected)) or ['pts', 'reb', 'ast']


def metric_text(row, metrics, opponent_sample=False):
    games = row.get('n') if opponent_sample else row.get('games', row.get('games_played'))
    values = row.get('averages', {}) if opponent_sample else row
    descriptions = []
    for metric in metrics:
        field, label, count_field = METRICS[metric]
        value = values.get(metric if opponent_sample else field)
        if metric.endswith('_pct'):
            descriptions.append(number(value*100 if type(value) in (int,float) else None) + ' ' + label + ' (shot ratio)')
            continue
        observed = row.get('observed_counts', {}).get(metric)
        if observed is None and count_field:
            observed = row.get(count_field)
        if observed is None and not opponent_sample and row.get('published_at') and value is not None:
            observed = games  # Published complete totals have the declared games denominator.
        text = number(value) + ' ' + label
        if observed is None:
            text += ' (observed count unavailable)'
        elif observed != games:
            text += f' ({observed}/{games} observed)'
        descriptions.append(text)
    return ', '.join(descriptions)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sample_descriptor(count):
    if not _finite(count) or count < 0:
        return 'sample size is unknown'
    if count == 0:
        return 'no matching completed appearances are stored'
    if count == 1:
        return 'a minimal stored sample is available'
    if count < 8:
        return 'a small stored sample is available'
    return 'a stored sample is available'


def _scope_descriptor(row, context):
    seasons = sorted(set(context.get('requested_population', {}).get('seasons', [])))
    label = ('earlier requested season' if len(seasons) > 1 and row.get('season') == seasons[0] else
             'later requested season' if len(seasons) > 1 and row.get('season') == seasons[-1] else 'requested season')
    phase = str(row.get('season_type') or 'Regular Season')
    return label + (' playoffs' if phase == 'Playoffs' else ' preseason' if phase == 'Preseason' else ' regular-season population')


def _published_snapshots(context):
    """Retain undated aggregate values separately from dated partial observations."""
    result = []
    for record in context.get('current_tracking', []):
        if record.get('category') != 'Player totals' or record.get('data_through_date'):
            continue
        gp, fields = record.get('gp'), record.get('fields', {})
        if not _finite(gp) or gp <= 0 or not record.get('published_at') or not record.get('season_type'):
            continue
        row = {key: record.get(key) for key in ('entity', 'season', 'season_type', 'source', 'published_at')}
        row.update(player=record.get('entity'), games_played=gp, data_through_date=None,
                   source_kind='published snapshot; covered-game date unknown', coverage_unknown=True)
        for metric in ('pts', 'reb', 'ast', 'minutes', 'tpm', 'stl', 'blk', 'tov'):
            value = fields.get(metric)
            row[METRICS[metric][0]] = value / gp if _finite(value) else None
            if METRICS[metric][2]:
                row[METRICS[metric][2]] = gp if _finite(value) else 0
        for metric, made, attempted in [('fg_pct', 'fgm', 'fga'), ('three_pct', 'tpm', 'tpa'), ('ft_pct', 'ftm', 'fta')]:
            row[metric] = (fields[made] / fields[attempted] if _finite(fields.get(made))
                           and _finite(fields.get(attempted)) and fields[attempted] > 0 else None)
        result.append(row)
    return result


def _baseline_key(row):
    return (row.get('player') or row.get('entity'), row.get('season'), row.get('season_type', 'Regular Season'))


def _opponent_descriptor(row, context, metrics):
    entity = str(row.get('player') or row.get('entity') or 'The player')
    opponent = str(row.get('opponent') or 'the requested opponent')
    count = row.get('n')
    text = f'{entity} against {opponent}, {_scope_descriptor(row, context)}: {_sample_descriptor(count)}. '
    if count == 0:
        return text + ('Coverage is incomplete; this does not prove no real game occurred. '
                       'All-opponent averages cannot substitute for an opponent-specific sample.')
    baselines = [r for r in context.get('_comparison_baselines', context.get('current_player_statistics', []))
                 if (r.get('player') or r.get('entity')) == entity and r.get('season') == row.get('season')
                 and r.get('season_type', 'Regular Season') == row.get('season_type', 'Regular Season')]
    comparisons = []
    labels = {'pts': 'scoring', 'reb': 'rebounding', 'ast': 'assist', 'minutes': 'playing-time',
              'tpm': 'made-threes', 'stl': 'steal', 'blk': 'block', 'tov': 'turnover'}
    if len(baselines) == 1:
        baseline = baselines[0]
        for metric in metrics:
            if metric not in labels:
                continue
            value = row.get('averages', {}).get(metric)
            observed = row.get('observed_counts', {}).get(metric)
            baseline_value = baseline.get(METRICS[metric][0])
            baseline_count = baseline.get(METRICS[metric][2]) if METRICS[metric][2] else None
            if baseline_count is None and baseline.get('published_at'):
                baseline_count = baseline.get('games', baseline.get('games_played'))
            if not (_finite(value) and _finite(baseline_value) and _finite(observed) and observed > 0
                    and _finite(baseline_count) and baseline_count > 0):
                continue
            relationship = ('equal to' if math.isclose(value, baseline_value, rel_tol=1e-9, abs_tol=1e-9)
                            else 'below' if value < baseline_value else 'above')
            baseline_label = 'published all-opponent snapshot' if baseline.get('coverage_unknown') else 'all-opponent baseline'
            comparisons.append(f'The stored opponent {labels[metric]} average is {relationship} the {baseline_label}.')
        if comparisons and baseline.get('coverage_unknown'):
            comparisons.append('The publication does not establish its covered-game date, so its freshness is unknown.')
    return text + ' '.join(comparisons) + (' ' if comparisons else '') + (
        'This incomplete whole-game sample cannot isolate an opponent or named-defender effect or establish a future projection.')


def _baseline_descriptor(row, context, metrics):
    entity = str(row.get('player') or row.get('entity') or 'The player')
    count = row.get('games', row.get('games_played'))
    label = _scope_descriptor(row, context)
    source = 'published season aggregate' if row.get('published_at') else 'stored observations with incomplete coverage'
    text = f'{entity}: an all-opponent baseline for the {label} is available from {source}; {_sample_descriptor(count)}. '
    text += 'It is separate from performance against a named opponent or defender. '
    if row.get('published_at') and not (row.get('data_through_date') or row.get('through_date')):
        text += 'Its covered-game date is unknown; the publication time does not establish current game coverage. '
    if 'pts' in metrics:
        text += 'Scoring volume alone does not measure shooting efficiency, current minutes, consistency or usage rate. '
        text += _peer_scoring_descriptor(row, context)
    text += 'The baseline is not a forecast of the next game.'
    return text


def _peer_scoring_descriptor(row, context):
    players = list(dict.fromkeys(context.get('resolved_players', [])))
    baselines = context.get('_comparison_baselines', [])
    if len(players) != 2 or row not in baselines:
        return ''
    entity = row.get('player') or row.get('entity')
    if entity not in players:
        return ''
    other = next(player for player in players if player != entity)
    peers = [value for value in baselines if (value.get('player') or value.get('entity')) == other
             and value.get('season') == row.get('season')
             and value.get('season_type', 'Regular Season') == row.get('season_type', 'Regular Season')]
    if len(peers) != 1:
        return ''
    peer = peers[0]
    for value in (row, peer):
        observed = value.get('points_observed')
        if observed is None and value.get('published_at'):
            observed = value.get('games', value.get('games_played'))
        if not (_finite(value.get('ppg')) and _finite(observed) and observed > 0):
            return ''
    relation = ('equal to' if math.isclose(row['ppg'], peer['ppg'], rel_tol=1e-9, abs_tol=1e-9)
                else 'lower than' if row['ppg'] < peer['ppg'] else 'higher than')
    text = f"{entity}'s stored all-opponent scoring average is {relation} {other}'s in the matching season and phase. "
    covered = [value.get('data_through_date') or value.get('through_date') for value in (row, peer)]
    if not all(covered):
        text += 'At least one covered-game date is unknown, so this comparison does not establish current freshness. '
    elif covered[0] != covered[1]:
        text += 'The covered-game periods differ, so these are not synchronized current snapshots. '
    return text + 'This is a descriptive scoring-volume comparison, not overall scoring ability or efficiency. '


def _additive_tracking_values(row):
    fields = row.get('fields', {})
    return [(key, label, fields[key]) for key, (category, label, _) in TRACKED_METRICS.items()
            if row.get('category') == category and _finite(fields.get(key)) and fields[key] >= 0]


def _tracking_descriptor(row, context):
    entity = str(row.get('entity') or 'The requested entity')
    category, fields = row.get('category'), row.get('fields', {})
    scope = _scope_descriptor(row, context)
    prefix = f'{entity}, {scope}: '
    additive = _additive_tracking_values(row)
    if category in {'Passing', 'Rebounding', 'Possessions'}:
        labels = [label for _, label, _ in additive]
        prefix += ('Available measured fields: ' + ', '.join(labels) + '. ' if labels else
                   'No finite measured totals are available for this category. ')
        if additive and _finite(row.get('gp')) and row['gp'] > 0:
            prefix += ('The source contains totals and the displayed per-game averages are those totals divided '
                       'by the source games denominator; an average request is answerable from this evidence. ')
        elif additive:
            prefix += 'Only totals are available; a valid games denominator is missing, so per-game averages are unavailable. '
    if category == 'Team defense':
        available = []
        if _finite(fields.get('points_allowed')) and _finite(row.get('gp')) and row['gp'] > 0:
            available.append('aggregate points allowed')
        if _finite(fields.get('defended_fgm')) and _finite(fields.get('defended_fga')) and fields['defended_fga'] > 0:
            available.append('opponent field-goal conversion')
        return prefix + (' and '.join(available) or 'team defensive records') + ' are available; ' + _sample_descriptor(row.get('gp')) + (
            '. No league comparison baseline, blocks, scheme or individual defender traits are established by these records. '
            "These team aggregates do not isolate this player's scoring effect.")
    if category == 'Possessions':
        return prefix + ('season aggregate touches and possession minutes may be present in the source record. '
                         'These are totals, not usage rates; they do not establish a current role, playing time or matchup-specific opportunity.')
    if category == 'Passing':
        return prefix + ('season passing observations are available across all opponents. '
                         'Potential assists measure opportunities and are distinct from recorded actual assists. '
                         'Passing totals are distinct from current role, future assists or opponent-specific opportunity.')
    if category == 'Rebounding':
        return prefix + ('season rebounding observations are available across all opponents. '
                         'Rebound chances are not guaranteed rebounds, and aggregate totals do not establish current playing time.')
    if category == 'Rim':
        return prefix + ('rim-only defended-shot observations are available. '
                         'They do not establish overall defended shooting, a named-opponent matchup, or a current guarding assignment.')
    if category == 'Team totals':
        return prefix + ('team offensive production is available across all opponents. '
                         'Offensive production is not points allowed, pace, an individual projection or a defensive matchup measurement.')
    return prefix + 'stored tracking observations are available; their exact metric and scope are shown in the source evidence.'


def evidence_cards(context, question=''):
    cards=[]
    def add(kind,text,record,source=None):
        cards.append({'id':f'E{len(cards)+1}','kind':kind,'text':text,
                      'source':source or record.get('source'),
                      'data_through_date':record.get('data_through_date') or record.get('through_date'),
                      'published_at':record.get('published_at')})
    metrics = requested_metrics(question)
    snapshots = _published_snapshots(context)
    snapshot_keys = {_baseline_key(row) for row in snapshots}
    descriptor_context = {**context, '_comparison_baselines': [*snapshots,
        *(row for row in context.get('current_player_statistics', []) if _baseline_key(row) not in snapshot_keys)]}
    players = context.get('resolved_players', [])
    teams = context.get('resolved_teams', [])
    coverage = context.get('availability_coverage')
    if isinstance(coverage, dict) and coverage.get('complete_official_report_in_context') is False:
        attempts = [r.get('last_attempt') for r in coverage.get('feeds', []) if r.get('last_attempt')]
        checked = max(attempts) if attempts else None
        text = ('The collected context does not contain a verified complete current official injury report'
                + (' for ' + ', '.join(players) if players else '')
                + '. Player health and game-day availability cannot be confirmed from these records. '
                + ('Latest stored coverage check: ' + str(checked) + '. ' if checked else 'No stored coverage-check timestamp is available. ')
                + 'Source: stored injury-feed coverage status. Missing reports do not mean a player is healthy or unavailable.')
        add('availability_coverage', text, {}, 'stored injury-feed coverage status')
        cards[-1]['prompt_descriptor'] = (
            'The requested verified current official injury report is not present in the collected context. '
            'Player health and availability are unknown from these records. This is a known source-coverage limit; '
            'local workbook tools cannot fetch a new official report. Explain that limit directly instead of '
            'requesting the same missing data or claiming a player is healthy, injured or ruled out.')
    for row in context.get('assignment_projections',[])[:2]:
        text=(f"{row['selected_player']}–{row['opponent_team']} pairing: {row.get('primary') or 'unavailable'}"
              +(f"; alternate {row['alternate']}" if row.get('alternate') else '')
              +f". Tentative as of {date(row.get('assignment_asof'))}; guarding direction unconfirmed. "
              +f"Source: Matchup_Assignments row {row.get('_row','unknown')}.")
        add('tentative_assignment',text,row,'Matchup_Assignments')
        cards[-1].update(entity=row['selected_player'], opponent=row['opponent_team'],
                         primary=row.get('primary'), alternate=row.get('alternate'),
                         assignment_date=date(row.get('assignment_asof')),
                         assignment_direction='unconfirmed', verified_game_day_assignment=False,
                         matches_resolved_matchup=(len(players)==1 and len(teams)==1 and
                             players[0]==row['selected_player'] and teams[0]==row['opponent_team']))
    for row in context.get('opponent_player_statistics',[])[:2]:
        n=row.get('n',0)
        text=f"{row.get('player') or row.get('entity')} versus {row['opponent']}, {row['season']} {row.get('season_type','Regular Season')}: "
        if not n:
            text+='no matching completed appearances in the stored sample; coverage is incomplete.'
        else:
            text+=(f"{metric_text(row,metrics,opponent_sample=True)}; {n} stored {'appearance' if n==1 else 'appearances'}, "
                   f"{date(row.get('first_date'))}–{date(row.get('data_through_date'))}; incomplete sample. Source: master player games.")
        add('player_opponent_sample',text,row,'master player games')
        cards[-1].update(entity=row.get('player') or row.get('entity'), opponent=row.get('opponent'),
                         matches_resolved_matchup=((row.get('player') or row.get('entity')) in players and row.get('opponent') in teams),
                         prompt_descriptor=_opponent_descriptor(row, descriptor_context, metrics))
    for row in [*snapshots[:2], *context.get('current_player_statistics',[])[:2]]:
        observed_alongside_snapshot = not row.get('published_at') and _baseline_key(row) in snapshot_keys
        text=(f"{row.get('player') or row.get('entity')}: {metric_text(row,metrics)}; "
              f"{row.get('games',row.get('games_played','unknown'))} games, {row['season']} {row.get('season_type','Regular Season')}, all opponents. "
              +('Stored sample; coverage may be incomplete. ' if not row.get('published_at') else '')+
              ('Published snapshot; covered-game date is unknown, so freshness cannot be established. ' if row.get('coverage_unknown') else '')+
              f"Coverage through {date(row.get('data_through_date') or row.get('through_date'))}; published {date(row.get('published_at'))}. "
              'Source: '+('published NBA Stats extract.' if row.get('published_at') else 'stored player observations.'))
        add('player_observed_sample' if observed_alongside_snapshot else 'player_all_opponent_averages',text,row)
        cards[-1].update(entity=row.get('player') or row.get('entity'),
                         matches_resolved_player=(row.get('player') or row.get('entity')) in players,
                         prompt_descriptor=_baseline_descriptor(row, descriptor_context, metrics))
    for row in context.get('current_tracking',[])[:8]:
        fields=row.get('fields',{});category=row.get('category');gp=row.get('gp')
        if category=='Player totals':
            continue  # Already represented by the population-labeled season averages.
        if category=='Team defense':
            parts=[]
            if fields.get('points_allowed') is not None and gp:
                parts.append(number(fields['points_allowed']/gp)+' points allowed/game')
            if fields.get('defended_fgm') is not None and fields.get('defended_fga'):
                parts.append(number(100*fields['defended_fgm']/fields['defended_fga'])+'% opponent FG')
            metric=', '.join(parts)
        elif category=='Team totals':
            metric=(number(fields['pts']/gp)+' points scored/game') if gp and fields.get('pts') is not None else ''
        elif category in {'Passing', 'Rebounding', 'Possessions'}:
            metric = ', '.join(number(value) + ' ' + label + ' total' +
                (' (' + number(value / gp) + ' ' + label + '/game)' if _finite(gp) and gp > 0 else '')
                for _, label, value in _additive_tracking_values(row))
        else:
            labels={'potential_assists':'potential assists','passes':'passes','assists':'assists',
                    'rebound_chances':'rebound chances','rebounds':'rebounds','touches':'touches',
                    'possession_minutes':'possession minutes','rim_defended_fgm':'rim FGM allowed',
                    'rim_defended_fga':'rim FGA defended','defended_fgm':'FGM allowed','defended_fga':'FGA defended'}
            metric=', '.join(number(v)+' '+labels[k]+' total' for k,v in fields.items() if k in labels and v is not None)
        if not metric:continue
        text=(f"{row['entity']} {category.lower()}: {metric}; {gp} games, {row['season']}, all opponents. "
              f"Coverage through {date(row.get('data_through_date'))}; published {date(row.get('published_at'))}. Source: published NBA Stats extract.")
        add('tracking_'+str(category),text,row)
        cards[-1].update(entity=row.get('entity'),
                         available_tracking_metrics=[key for key, _, _ in _additive_tracking_values(row)],
                         matches_resolved_entity=row.get('entity') in [*players, *teams],
                         prompt_descriptor=_tracking_descriptor(row, context))
    for row in context.get('news',[])[:2]:
        add('publisher_report',f"Publisher report: {row.get('title','')} — {row.get('source','unknown')}, published {row.get('published_at') or 'time unknown'}; not independently verified.",row,row.get('url'))
    # Earlier cards carry assignment and population boundaries. Optional tracking
    # and news cannot crowd those out of the prompt.
    while len(json.dumps(prompt_evidence(cards),ensure_ascii=False))>6500 and len(cards)>1:
        cards.pop()
    return cards


def required_evidence(question,cards):
    required = []
    if re.search(r'\b(?:injur\w*|health\w*|availab\w*|official\s+report)\b', question, re.I):
        required.extend(c['id'] for c in cards if c['kind']=='availability_coverage')
    tracked_metrics = set(requested_tracking_metrics(question))
    if tracked_metrics:
        required.extend(c['id'] for c in cards if c.get('matches_resolved_entity') and
                        tracked_metrics.intersection(c.get('available_tracking_metrics', [])))
    if re.search(r'\b(match\s*ups?|against|versus|vs|guard(?:s|ing)?|assign(?:ment|ed)?)\b',question,re.I):
        matches = [c['id'] for c in cards if c['kind']=='tentative_assignment' and c.get('matches_resolved_matchup')]
        if len(matches)==1:
            required.extend(matches)
    if re.search(r'\b(?:scor\w*|points?|ppg|match\s*ups?|against|versus|vs)\b', question, re.I):
        required.extend(c['id'] for c in cards if c['kind']=='player_all_opponent_averages' and c.get('matches_resolved_player'))
        required.extend(c['id'] for c in cards if c['kind']=='player_opponent_sample' and c.get('matches_resolved_matchup'))
    return list(dict.fromkeys(required))[:3]


def prompt_evidence(cards):
    """Send qualitative scope/limits; render exact source facts outside the model.

    Numeric records remain intact for citations and user display. Descriptors
    prevent the model from converting scoring volume into efficiency or named
    defender traits. Comparisons are computed only from matching observed scopes.
    """
    result = []
    for card in cards:
        value = {key: card[key] for key in ('id', 'kind')}
        value['text'] = card.get('prompt_descriptor') or {
            'player_opponent_sample': 'A stored opponent-specific sample is available; coverage and observed counts require checking. It cannot isolate a named-defender effect.',
            'player_all_opponent_averages': 'An all-opponent baseline is available. It is distinct from named-opponent performance and is not a forecast.',
            'tracking_Team defense': 'Team aggregate defense is available; it does not establish a named defender, scheme or individual scoring effect.',
            'query_result': 'Stored query results are available. Their filters, calculation and population require review; exact values are displayed separately. Do not infer a season or opponent scope from an unlabeled result.',
            'workbook_excerpt': 'A dated workbook excerpt is available. Its column headings and population require checking; exact source text is displayed separately.',
            'experimental_analysis': 'An experimental research analysis is available. Its assumptions, limitations and exact findings are displayed separately; it does not establish a calibrated win probability.',
            'publisher_report': 'A dated publisher report is collected but not independently verified. It does not establish complete injury coverage or a confirmed current lineup.',
            'collection_status': 'A data-collection status record is available. Its exact phase and covered dates are displayed separately; successful collection does not prove current coverage.',
        }.get(card['kind'], 'A source record is available. Its exact values, dates and scope are displayed separately; do not invent missing measurements.')
        if card['kind'] == 'tentative_assignment' and card.get('verified_game_day_assignment') is not True:
            value['text'] = ('A dated tentative pairing exists; guarding direction and the current lineup '
                             'are unconfirmed. Exact sourced pairing names will be displayed separately.')
        result.append(value)
    return result


def compact_evidence(cards, question, max_chars=2600):
    """Choose whole relevant cards for a bounded CPU inference prompt.

    This selects evidence, not an answer. IDs, labels and values are unchanged;
    callers disclose how many records were omitted and retain the full set for
    later retrieval. A record that cannot fit is omitted rather than shortened.
    """
    if max_chars < 2:
        return []
    metrics = set(requested_metrics(question))
    required = set(required_evidence(question, cards))
    shooting = bool(metrics & {'pts', 'tpm', 'fg_pct', 'three_pct', 'ft_pct'})
    defense = bool(re.search(r'\b(?:defen\w*|guard\w*|match\s*ups?)\b', question, re.I))

    def priority(card):
        kind = card.get('kind', '')
        if card.get('id') in required:
            return 0
        if kind in {'query_result', 'workbook_excerpt', 'experimental_analysis'}:
            return 1
        if kind in {'player_opponent_sample', 'player_all_opponent_averages'}:
            return 2
        if kind == 'player_observed_sample':
            return 4
        if kind == 'tentative_assignment':
            return 3
        if kind == 'tracking_Team defense':
            return 4 if shooting or defense else None
        if kind == 'tracking_Passing':
            return 5 if 'ast' in metrics or re.search(r'\bpass(?:es|ing)?\b', question, re.I) else None
        if kind == 'tracking_Rebounding':
            return 5 if 'reb' in metrics else None
        if kind == 'tracking_Possessions':
            return 5 if metrics & {'pts', 'minutes', 'tov'} or re.search(r'\b(?:touches|possessions?)\b', question, re.I) else None
        if kind == 'tracking_Rim':
            # Rim-only defense must not stand in for overall defended shooting.
            return 5 if re.search(r'\b(?:rim|paint|interior)\b', question, re.I) else None
        if kind == 'tracking_Team totals':
            return 7 if re.search(r'\b(?:team|offen\w*|pace)\b', question, re.I) else None
        if kind == 'publisher_report':
            return 4 if re.search(r'\b(?:news|injur\w*|available|availability|lineup|trade\w*)\b', question, re.I) else 6
        if kind == 'collection_status':
            return 1 if re.search(r'\b(?:updat\w*|fresh\w*|dataset|status)\b', question, re.I) else 6
        return 7

    ranked = [(priority(card), index, card) for index, card in enumerate(cards)]
    ranked = sorted((item for item in ranked if item[0] is not None), key=lambda item: item[:2])
    selected = []
    for _, _, card in ranked:
        candidate = selected + [card]
        size = len(json.dumps(prompt_evidence(candidate), ensure_ascii=False, separators=(',', ':')))
        if size <= max_chars:
            selected.append(card)
    return selected


def reply_schema(cards, text_key='reply'):
    items={'type':'string'}
    if cards:items['enum']=[c['id'] for c in cards]
    return {'type':'object','additionalProperties':False,
            'required':['evidence_ids',text_key,'needs_more_data'],
            'properties':{'evidence_ids':{'type':'array','maxItems':3 if cards else 0,'items':items},
                          text_key:{'type':'string','maxLength':700},
                          'needs_more_data':{'type':'boolean'}}}


def render_reply(document,cards,required=()):
    if not isinstance(document,dict) or set(document)!={'evidence_ids','analysis','needs_more_data'}:
        raise ReplyValidationError('invalid_schema')
    ids=document['evidence_ids'];analysis=document['analysis'];more=document['needs_more_data']
    by_id={c['id']:c for c in cards}
    if (not isinstance(ids,list) or any(not isinstance(i,str) or i not in by_id for i in ids)
            or any(not isinstance(i,str) or i not in by_id for i in required)):
        raise ReplyValidationError('invalid_evidence')
    ids = list(dict.fromkeys(ids))
    if len(ids)>3 or len(set(required))>3:
        raise ReplyValidationError('invalid_evidence')
    if not isinstance(analysis,str) or type(more) is not bool:
        raise ReplyValidationError('invalid_schema')
    if len(analysis)>700:
        raise ReplyValidationError('analysis_too_long')
    if more:
        if analysis.strip():raise ReplyValidationError('retrieval_with_analysis')
        return None
    # Mandatory facts come from the application, not from model recollection.
    # A harmless omission or repeated valid ID need not discard the explanation.
    ids = list(dict.fromkeys([*required, *ids]))[:3]
    if not analysis.strip() and not ids:
        raise ReplyValidationError('empty_reply')
    # Numerical facts and their population/date labels must come from a source
    # card. This catches the demonstrated relabeling even when all digits exist
    # elsewhere in the prompt. It is not a claim to validate all qualitative prose.
    reference_rows = [row for identity in ids for row in by_id[identity].get('reference_rows', [])
                      if by_id[identity]['kind'] == 'workbook_excerpt']
    if has_numerical_assertion(analysis, reference_rows):
        raise ReplyValidationError('numerical_prose')
    if has_unsupported_assignment(analysis, cards):
        raise ReplyValidationError('unsupported_assignment')
    if has_unscoped_absence(analysis, cards):
        raise ReplyValidationError('unsupported_scope')
    # A conversational reply may mention an entity without making a statistical
    # assertion. It remains generated by the model even when topic cards exist.
    selected=[by_id[i] for i in ids]
    answer='\n\n'.join([*(c['text'] for c in selected),analysis.strip()]).strip()
    return {'answer':answer,'sources':[{'tool':'scoped_evidence','evidence_id':c['id'],
             'population_kind':c['kind'],'source':c.get('source'),
             'data_through_date':c.get('data_through_date'),'published_at':c.get('published_at'),
             **({'query':c['query'],'excerpt':c.get('excerpt',False)} if c.get('query') else {})} for c in selected],
            'grounding':'Source-selected factual text; AI-generated qualitative interpretation.'}


def unverified_reply(cards,required=(),failure_message=None):
    selected=[c for c in cards if c['id'] in required]
    selected.extend(c for c in cards if c not in selected)
    selected=selected[:2]
    return {'answer':(failure_message or 'I could not verify the AI explanation.')+(' Here is the saved evidence:\n\n'+'\n\n'.join(c['text'] for c in selected) if selected else ''),
            'sources':[{'tool':'scoped_evidence','source':c.get('source')} for c in selected],
            'ai_used':False,'ai_failure_category':'grounding_error'}
