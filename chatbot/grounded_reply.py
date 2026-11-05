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
                         'empty_reply', 'numerical_prose', 'unsupported_assignment', 'unsupported_scope',
                         'unsupported_number', 'unsupported_metric', 'unsupported_availability',
                         'unsupported_certainty', 'unsupported_recommendation'})

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
    'fga': ('fga_per_game', 'FGA/game', 'fga_observed'),
    'tpa': ('tpa_per_game', 'three-point attempts/game', 'tpa_observed'),
    'fgm': ('fgm_per_game', 'field goals made/game', 'fgm_observed'),
    'ftm': ('ftm_per_game', 'free throws made/game', 'ftm_observed'),
    'fta': ('fta_per_game', 'free throw attempts/game', 'fta_observed'),
    'oreb': ('oreb_per_game', 'offensive rebounds/game', 'oreb_observed'),
    'dreb': ('dreb_per_game', 'defensive rebounds/game', 'dreb_observed'),
    'fouls': ('fouls_per_game', 'fouls/game', 'fouls_observed'),
    'plus_minus': ('plus_minus_per_game', 'plus-minus/game', 'plus_minus_observed'),
    'pra': ('pra_per_game', 'PRA/game', 'pra_observed'),
    'pr': ('pr_per_game', 'points+rebounds/game', 'pr_observed'),
    'pa': ('pa_per_game', 'points+assists/game', 'pa_observed'),
    'ra': ('ra_per_game', 'rebounds+assists/game', 'ra_observed'),
    'stocks': ('stocks_per_game', 'steals+blocks/game', 'stocks_observed'),
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
        if row.get('aggregation') == 'totals':
            total = row.get('totals', {}).get(metric)
            observed = row.get('observed_counts', {}).get(metric, 0)
            total_label = {'pts': 'points', 'reb': 'rebounds', 'ast': 'assists', 'minutes': 'minutes',
                'tpm': 'made threes', 'stl': 'steals', 'blk': 'blocks', 'tov': 'turnovers',
                'fga': 'FG attempts', 'tpa': 'three-point attempts', 'fgm': 'field goals made',
                'ftm': 'free throws made', 'fta': 'free throw attempts', 'oreb': 'offensive rebounds',
                'dreb': 'defensive rebounds', 'fouls': 'fouls', 'plus_minus': 'plus-minus',
                'pra': 'PRA', 'pr': 'points+rebounds', 'pa': 'points+assists',
                'ra': 'rebounds+assists', 'stocks': 'steals+blocks'}[metric]
            descriptions.append(f'{number(total)} {total_label} total ({observed}/{games} observed)')
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
    if row.get('season_basis') == 'available career':
        return 'available career ' + str(row.get('season_type') or 'Regular Season').casefold() + ' population'
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
    metrics = context.get('requested_metrics') or requested_metrics(question)
    if context.get('population_error'):
        add('research_scope_error', context['population_error'], {}, 'Requested career date filters')
        cards[-1]['prompt_descriptor'] = context['population_error'] + ' Ask the user to correct the date before calculating history.'
        return cards
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
        from assignment_refresh import refresh_caution
        text=(f"{row['selected_player']}–{row['opponent_team']} pairing: {row.get('primary') or 'unavailable'}"
              +(f"; alternate {row['alternate']}" if row.get('alternate') else '')
              +f". Tentative as of {date(row.get('assignment_asof'))}; guarding direction unconfirmed. "
              +f"Source: Matchup_Assignments row {row.get('_row','unknown')}.")
        caution = refresh_caution(row)
        if caution:
            text += ' ' + caution
        add('tentative_assignment',text,row,'Matchup_Assignments')
        cards[-1].update(entity=row['selected_player'], opponent=row['opponent_team'],
                         primary=row.get('primary'), alternate=row.get('alternate'),
                         assignment_date=date(row.get('assignment_asof')),
                         assignment_refresh_state=row.get('refresh_state'),
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
        if row.get('season_basis') == 'available career':
            text += ' Across available seasons and actual teams; full career completeness is not independently verified. Whole-game opponent appearances are not measured guarding.'
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
        if row.get('season_basis') == 'available career':
            text += (' Across available seasons and actual teams, strictly before '
                     + str(row.get('cutoff_exclusive')) + '. Full career completeness is not independently verified.')
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
            if card.get('assignment_refresh_state') == 'needs_review':
                value['text'] += (' Collected roster or game-specific availability evidence conflicts with using '
                                  'the saved pairing; do not treat its names as current assignments.')
            elif card.get('assignment_refresh_state') == 'recheck_overdue':
                value['text'] += ' Its roster and availability recheck is overdue.'
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


# Conversational answers use the exact retrieved facts, rather than the older
# qualitative-only prompt. These checks are deliberately bounded: they catch
# unsupported values, entity/metric swaps and known scope errors; they are not a
# proof of every English sentence or of the quality of a betting recommendation.
_NUMBER_WORDS = dict(zip(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(),
    range(21)))
_NUMBER_WORDS.update(thirty=30, forty=40, fifty=50, sixty=60, seventy=70, eighty=80, ninety=90)
_NUMBER = re.compile(r'(?<![\w.])[-+]?\d+(?:\.\d+)?(?!\w|\.\d)')
_UNITS = {
    'pra': r'PRA\b|(?:PTS|points?)\s*\+\s*(?:REBS?|rebounds?)\s*\+\s*(?:ASTS?|assists?)\b',
    'pr': r'(?:PTS|points?)\s*\+\s*(?:REBS?|rebounds?)\b',
    'pa': r'(?:PTS|points?)\s*\+\s*(?:ASTS?|assists?)\b',
    'ra': r'(?:REBS?|rebounds?)\s*\+\s*(?:ASTS?|assists?)\b',
    'fga': r'(?:FG|field[- ]goal)\s*(?:attempts?|attempted)\b|FGA\b|shots?\s+attempted\b',
    'tpa': r'3PTA\b|three[- ]point\s+attempts?\b',
    'attempts': r'attempts?\b|shots?\s+taken\b',
    'tpm': r'3P(?:T)?M\b|(?:made\s+)?threes\b|(?:3|three)[- ]pointers?(?:\s+made)?\b',
    'pts': r'PPG\b|points?(?:\s+(?:per\s+game|a\s+game))?\b',
    'reb': r'RPG\b|rebounds?(?:\s+(?:per\s+game|a\s+game))?\b',
    'ast': r'APG\b|assists?(?:\s+(?:per\s+game|a\s+game))?\b',
    'minutes': r'minutes?\b|mins?\b|MPG\b',
    'games': r'games?\b|appearances?\b|observations?\b',
    'tov': r'turnovers?\b', 'stl': r'steals?\b', 'blk': r'blocks?\b',
    'percent': r'%|percent(?:age)?\b',
}
_UNIT_AFTER = re.compile(r'^\s*(?:(?:made|stored|observed|completed)\s+)?(' + '|'.join('(?:'+p+')' for p in _UNITS.values()) + ')', re.I)


def _number_text(text):
    # Preserve basketball vocabulary before converting spelled-out quantities.
    text = re.sub(r'\b([23])[- ]point(?:ers?)?\b',
                  lambda m: m.group().replace(m.group(1), 'two' if m.group(1)=='2' else 'three', 1), str(text), flags=re.I)
    value = re.sub(r'\b(?:two|three)[- ]point(?:ers?)?\b',
                   lambda m: m.group().replace('two', 'two~').replace('three', 'three~'), str(text), flags=re.I)
    value = re.sub(r'\b(?:one[- ]on[- ]one|two[- ]way)\b', 'basketball-term', value, flags=re.I)
    value = re.sub(r'\b('+'|'.join(_NUMBER_WORDS)+r')\b(?!~)',
                   lambda m: str(_NUMBER_WORDS[m.group().casefold()]), value, flags=re.I)
    return value.replace('~', '')


def _numbers(text):
    return {float(match.group()) for match in _NUMBER.finditer(_number_text(text))}


def _entities(cards):
    return {str(c['entity']) for c in cards if c.get('entity')}


def _entity_mentions(text, entities):
    found = set()
    for entity in entities:
        parts = entity.split()
        aliases = [entity]
        if len(parts) > 1:
            aliases.append(parts[-1])
            if parts[-1].casefold() in {'jr.', 'jr', 'iii', 'ii'} and len(parts) > 2:
                aliases.append(parts[-2])
        if any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)', text, re.I) for alias in aliases):
            found.add(entity)
    return found


def _card_facts(card):
    explicit = card.get('approved_facts')
    if isinstance(explicit, list) and explicit:
        return [dict(fact, entity=fact.get('entity') or card.get('entity'),
                     opponent=fact.get('opponent') or card.get('opponent'))
                for fact in explicit if isinstance(fact, dict) and isinstance(fact.get('text'), str)]
    return [{'text': str(card.get('text', '')), 'entity': card.get('entity'),
             'opponent': card.get('opponent'), 'scope': card.get('scope', ''),
             'fact_type': 'average' if card.get('kind') in
                 {'player_all_opponent_averages', 'player_observed_sample', 'player_opponent_sample'} else None}]


def natural_prompt_evidence(cards, max_chars=3000):
    """Return whole exact facts that fit the prompt, with scope and no mutation.

    The caller should validate against the returned card IDs only. Omission is
    explicit in its diagnostics; a truncated numeric fact is never supplied.
    """
    result = []
    for card in cards:
        facts = _card_facts(card)
        text = ' '.join(f['text'] for f in facts)
        # Tentative names remain source records, never confirmed guarding roles.
        value = {'id': card['id'], 'kind': card.get('kind'), 'text': text}
        if card.get('entity'):
            value['entity'] = card['entity']
        scopes = list(dict.fromkeys(str(f.get('scope')) for f in facts if f.get('scope')))
        if scopes:
            value['scope'] = '; '.join(scopes)
        if card.get('data_through_date'):
            value['through'] = str(card['data_through_date'])[:10]
        if len(json.dumps(result+[value], ensure_ascii=False, separators=(',', ':'))) <= max_chars:
            result.append(value)
    return result


def compact_natural_evidence(cards, question='', max_chars=2600):
    """Select the same intact records that the natural prompt actually includes."""
    required = set(required_evidence(question, cards))
    # Application-computed prop assessments precede optional historical records.
    ranked = sorted(enumerate(cards), key=lambda pair: (
        0 if pair[1].get('id') in required else
        1 if pair[1].get('kind') in {'research_unavailable','prop_unavailable'} or
             (pair[1].get('kind')=='research_projection' and pair[1].get('decision')=='pass') else
        2 if pair[1].get('kind') == 'prop_assessment' else
        3 if str(pair[1].get('kind','')).startswith('prop_') else
        4 if pair[1].get('matches_resolved_player') else 5, pair[0]))
    by_id = {c['id']: c for c in cards}
    return [by_id[value['id']] for value in natural_prompt_evidence([c for _,c in ranked], max_chars=max_chars)]


def _metric_for_unit(text):
    for metric, pattern in _UNITS.items():
        if re.match(pattern, text, re.I):
            return metric
    return None


def _quantity_supported(value, unit, fact):
    text = _number_text(fact.get('text', ''))
    explicit_units = set()
    for match in _NUMBER.finditer(text):
        if float(match.group()) != value:
            continue
        following = _UNIT_AFTER.match(text[match.end():])
        if following:
            explicit_units.add(_metric_for_unit(following.group(1)))
    if unit in explicit_units or (unit=='attempts' and explicit_units & {'fga','tpa'}):
        return True
    if unit == 'games' and fact.get('fact_type') in {'sample','sample_counts','counts'}:
        # A numerator in "3 of 7 games" is itself a count of games, even
        # though the noun follows the denominator in the source sentence.
        count = re.escape(number(value))
        if re.search(r'\b'+count+r'\s+(?:out\s+of|of|/)\s*\d+\s+(?:(?:stored|observed)\s+)?games?\b',text,re.I):
            return True
        if re.search(r'\b(?:above|below|over|under|tied)\s+in\s+'+count+r'\b',text,re.I):
            return True
    if explicit_units:
        return False
    # Structured facts may use a list of values followed by one shared label.
    metric = str(fact.get('metric', '')).casefold()
    metric_key = _metric_for_unit(metric) or {'points':'pts', 'rebounds':'reb', 'assists':'ast',
                  'threes':'tpm', '3pm':'tpm', '3ptm':'tpm', 'fg_attempted':'fga'}.get(metric, metric)
    if (metric_key == unit or (unit=='attempts' and metric_key in {'fga','tpa'})) and value in {float(n) for n in fact.get('values', []) if _finite(n)}:
        # Count denominators must not become a scoring or projection value.
        return fact.get('fact_type') in {'recent_values', 'projection', 'median', 'average', 'line'}
    return False


def _construction_numbers(text, question=''):
    """Card-composition counts are instructions, not measured player statistics."""
    normalized = _number_text(text)
    requested = _numbers(question)
    normalized = re.sub(r'(?<!\w)([1-6])\s+(?:separate\s+)?(?:legs?|picks?|selections?)(?!\w)',
        lambda m: 'requested selections' if float(m.group(1)) in requested else m.group(), normalized, flags=re.I)
    normalized = re.sub(r'\b1\s+(?:(?:qualified|eligible|supported|distinct)\s+)*(?:pick|player|selection)?\s*(?:from|for|on)\s+each\s+(?:team|side)\b',
                        'a selection from each team', normalized, flags=re.I)
    normalized = re.sub(r'\b(?:players?|picks?|selections?|legs?)\s+'
                        r'(?:(?:are|come|coming|drawn|chosen|selected)\s+)?from\s+(?:at least\s+)?2\s+(?:different\s+)?teams?\b',
                        'selections from both teams',normalized,flags=re.I)
    return normalized


def _sample_direction_claims(text):
    value = _number_text(text)
    patterns = (
        r'\b(?P<direction>above|over|more|exceeded|cleared|below|under|less)\b[^;\n]{0,65}?\bin\s+(?P<hits>\d+)\s+(?:out\s+of|of|/)\s*(?P<n>\d+)\b',
        r'\b(?P<hits>\d+)\s+(?:out\s+of|of|/)\s*(?P<n>\d+)\s+(?:(?:stored|observed)\s+)?(?:games?\s+)?'
        r'(?:(?:were|finished|ended|landed)\s+)?(?P<direction>above|over|more|exceeded|cleared|below|under|less)\b',
    )
    result = []
    for pattern in patterns:
        for match in re.finditer(pattern, value, re.I):
            direction = 'less' if match['direction'].casefold() in {'below','under','less'} else 'more'
            result.append((direction,int(match['hits']),int(match['n'])))
    if result:
        denominator = result[0][2]
        for match in re.finditer(r'\b(?P<direction>below|under|less|above|over|more)\s+in\s+(?P<hits>\d+)\s*(?=[,.]|$)',value,re.I):
            direction = 'less' if match['direction'].casefold() in {'below','under','less'} else 'more'
            result.append((direction,int(match['hits']),denominator))
    for match in re.finditer(r'(?<![\w.])(?P<hits>\d+)\s+(?:(?:(?:stored|observed)\s+)?games?\s+)?(?P<direction>above|over|below|under)\b',value,re.I):
        # In "3 of 7 games above", seven is the sample denominator,
        # not a second assertion that seven games cleared the line.
        if re.search(r'(?:\bof|/)\s*$',value[:match.start()],re.I):
            continue
        direction = 'less' if match['direction'].casefold() in {'below','under'} else 'more'
        result.append((direction,int(match['hits']),None))
    return result


def _unsupported_sample_equality(text, cards):
    """Check explicit equal hit-count claims, not qualitative balance language."""
    facts = [fact for card in cards for fact in _card_facts(card)
             if fact.get('fact_type') in {'sample','sample_counts','counts'}]
    entities = {fact.get('entity') for fact in facts if fact.get('entity')}
    active_entities = set()
    for sentence in re.split(r'(?<=[.!?;])\s+',text):
        mentioned = _entity_mentions(sentence,entities)
        if mentioned:
            active_entities = mentioned
        equality = re.search(r'\b(?:equal|identical|same)\s+(?:numbers?|counts?|frequenc(?:y|ies))\b|'
                             r'\b(?:numbers?|counts?|frequencies)\b[^.!?;]{0,80}?\b(?:are|were|is|was)\s+(?:equal|identical|the same)\b|'
                             r'\b(?:evenly split|split evenly)\b',sentence,re.I)
        if not equality or not re.search(r'\b(?:above|over|more)\b',sentence,re.I) or not re.search(r'\b(?:below|under|less)\b',sentence,re.I):
            continue
        if re.search(r'\b(?:if|assuming|not|never)\b',sentence[:equality.start()],re.I):
            continue
        selected_entities = mentioned or active_entities or entities
        if len(selected_entities)>1:
            continue  # Comparing several players is a different claim.
        for fact in facts:
            if selected_entities and fact.get('entity') not in selected_entities:
                continue
            claims = _sample_direction_claims(fact['text'])
            above = {hits for direction,hits,_ in claims if direction=='more'}
            below = {hits for direction,hits,_ in claims if direction=='less'}
            if len(above)==len(below)==1 and above!=below:
                return True
    return False


def _retired_line_correction_span(clause, facts, question):
    """Allow an old user line only while explicitly correcting that same line.

    The old number never becomes an approved value. Its single occurrence in
    a pure correction clause may be repeated when the new value is a supplied
    application-authored line fact and the latest user message gives the same
    correction pair. Statistics, forecasts and other clauses remain checked.
    """
    question = _number_text(question)
    if not re.search(r'\b(?:actually|correction|corrected|line|i mean|i meant)\b', question, re.I):
        return None
    if re.search(r'\b(?:averag\w*|median|scored|recorded|forecast|projection|if|assuming)\b', question, re.I):
        return None
    number = r'\d+(?:\.\d+)?'
    unit = '(?:' + '|'.join('(?:' + value + ')' for value in _UNITS.values()) + ')'
    pair = (r'(?P<new>' + number + r')(?:\s+' + unit + r')?\s*[,—-]?\s*'
            r'(?:not|rather than|instead of)\s+(?P<old>' + number + r')')
    requested = {(float(match['new']), float(match['old']))
                 for match in re.finditer(pair, question, re.I)}
    if not requested:
        return None
    pattern = (r'\s*(?:(?:the|your|that|this|corrected)\s+)?line\s+'
               r'(?:(?:is|was)\s+)?(?:(?:now|corrected to|updated to)\s+)?'
               r'(?P<new>' + number + r')(?:\s+(?P<new_unit>' + unit + r'))?\s*[,—-]?\s*'
               r'(?:not|rather than|instead of)\s+(?P<old>' + number + r')'
               r'(?:\s+(?P<old_unit>' + unit + r'))?\s*[.!?]?\s*')
    correction = re.fullmatch(pattern, clause, re.I)
    if not correction:
        return None
    new, old = float(correction['new']), float(correction['old'])
    if new == old or (new, old) not in requested:
        return None
    line_facts = [fact for fact in facts if fact.get('fact_type') == 'line' and
                  re.search(r'\buser[- ]provided line is\s+' + re.escape(correction['new']) +
                            r'(?![\d.])', fact.get('text', ''), re.I)]
    if not line_facts:
        return None
    for label in (correction['new_unit'], correction['old_unit']):
        if label and (re.search(r'\b(?:per|PPG|RPG|APG|MPG|games?|percent)\b|%', label, re.I) or
                      not any(_quantity_supported(new, _metric_for_unit(label), fact) for fact in line_facts)):
            return None
    return correction.span('old')


def _validate_natural_numbers(text, selected, all_cards, question=''):
    if _unsupported_sample_equality(text,selected):
        raise ReplyValidationError('unsupported_metric')
    facts = [fact for card in selected for fact in _card_facts(card)]
    entities = _entities(all_cards)
    active_entities = set()
    for clause in re.split(r'(?<=[.!?;])\s+|\b(?:whereas|while|and|but)\b', text):
        mentioned = _entity_mentions(clause, entities)
        if mentioned:
            active_entities = mentioned
        elif active_entities:
            mentioned = active_entities
        candidates = [fact for fact in facts if not mentioned or fact.get('entity') in mentioned]
        defensive_claim = bool(re.search(r'\b(?:allowed|allows?|conced\w*|opponent|defended)\b', clause, re.I))
        if _numbers(clause):
            candidates = [fact for fact in candidates if
                bool(re.search(r'\b(?:allowed|allows?|conced\w*|opponent FG|defended)\b', fact.get('text',''), re.I)) == defensive_claim
                or (defensive_claim and fact.get('opponent'))]
        # Broad season averages cannot be relabeled as opponent-specific results.
        historical = re.search(r'\b(?:averag\w*|scor(?:ed|ing)|recorded|had|made|shot|grabbed|collected)\b', clause, re.I)
        if historical:
            for opponent_claim in re.finditer(r'\b(?:against|versus|vs\.?|facing)\s+([A-Za-z][A-Za-z -]{1,60})', clause, re.I):
                target = opponent_claim.group(1)
                if re.match(r'(?:all|every|any)\s+(?:opponents?|teams?)\b',target,re.I):
                    continue  # This explicitly preserves the broad population.
                scope_prefix = clause[:opponent_claim.start()]
                if re.search(r'\bnot\s+(?:reflect|represent|predict|forecast|establish|describe|measure|isolate)\b[^.;]{0,70}$|'
                             r'\bnot\s+(?:a|an)\s+(?:measure|forecast|prediction|estimate)\b[^.;]{0,70}$',scope_prefix,re.I):
                    continue  # A baseline disclaimer is not an opponent average.
                from basketball_research import TEAM_NAMES
                matching = []
                for fact in candidates:
                    opponent = str(fact.get('opponent') or '')
                    aliases = (opponent,*TEAM_NAMES.get(opponent,())) if opponent else ()
                    if any(re.search(r'\b'+re.escape(alias)+r'\b',target,re.I) for alias in aliases):
                        matching.append(fact)
                    elif opponent_claim.group(0).casefold() in fact.get('text','').casefold():
                        matching.append(fact)
                candidates = matching
        normalized = _construction_numbers(clause, question)
        efficiency = re.search(r'\b(?:suggests?|indicates?|shows?|demonstrates?|reflects?|establishes?|proves?)\s+'
                               r'(?:high|good|great|strong|poor|low|weak|excellent|elite)\s+(?:(?:shooting|scoring)\s+)?efficiency\b',normalized,re.I)
        if efficiency and not re.search(r'\b(?:not|never|cannot|can[’\x27]t)\b',normalized[:efficiency.start()],re.I):
            efficiency_metrics = r'FG\s*%|FG[_ ]?PCT|(?:e?FG|TS)%|shooting percentage|true[- ]shooting|effective field[- ]goal|points per (?:shot|possession)|efficiency rating'
            if not any(re.search(efficiency_metrics,str(f.get('metric',''))+' '+f.get('text',''),re.I)
                       and _numbers(f.get('text','')) for f in candidates):
                raise ReplyValidationError('unsupported_metric')
        # A list of made-shot outcomes does not describe usage or attempts.
        # Keep qualitative possibilities available, but bind empirical workload
        # descriptions to actual workload measurements when numbers are given.
        workload = re.search(r'\b(?:low|limited|reduced)[- ](?:usage|(?:shot[- ])?volume|attempts?|minutes?)\b',normalized,re.I)
        if workload and not re.search(r'\b(?:if|could|can|may|might|would)\b',normalized,re.I):
            workload_values = _numbers(normalized[workload.end():])
            measured = set()
            for fact in candidates:
                metric = str(fact.get('metric',''))
                if _metric_for_unit(metric) in {'fga','tpa','attempts','minutes'} or re.search(r'\b(?:usage|touches|possessions)\b',metric,re.I):
                    measured.update(float(n) for n in fact.get('values',[]) if _finite(n))
            if workload_values-measured:
                raise ReplyValidationError('unsupported_metric')
        allowed = set().union(*(_numbers(f.get('text', '')) | {float(n) for n in f.get('values', []) if _finite(n)}
                               for f in candidates)) if candidates else set()
        correction_span = _retired_line_correction_span(normalized, candidates, question)
        for match in _NUMBER.finditer(normalized):
            if match.span() == correction_span:
                continue
            value = float(match.group())
            if value not in allowed:
                raise ReplyValidationError('unsupported_number')
            unit = _UNIT_AFTER.match(normalized[match.end():])
            if unit and not any(_quantity_supported(value, _metric_for_unit(unit.group(1)), f) for f in candidates):
                raise ReplyValidationError('unsupported_metric')
        for claim in _sample_direction_claims(clause):
            if not any(any(claim[:2]==other[:2] and (claim[2] is None or claim[2]==other[2])
                           for other in _sample_direction_claims(f.get('text',''))) for f in candidates):
                raise ReplyValidationError('unsupported_metric')
        # Forecast, average and median are different measurements even if a
        # coincidentally equal number appears in another fact's sample count.
        for claim_type, pattern in [('median', r'\bmedian\b'), ('projection', r'\b(?:project(?:ion|ed)|forecast)\b'),
                                    ('average', r'\b(?:averag\w*|mean)\b')]:
            if not re.search(pattern, clause, re.I) or not _numbers(clause):
                continue
            typed = [f for f in candidates if f.get('fact_type') == claim_type or re.search(pattern, f.get('text', ''), re.I)]
            for term in re.finditer(pattern, normalized, re.I):
                if re.search(r'\b(?:no|not|without|unsupported|unavailable|unknown)\b',normalized[:term.start()],re.I):
                    continue
                if not typed:
                    raise ReplyValidationError('unsupported_metric')
                after = _NUMBER.search(normalized[term.end():term.end()+24])
                before = list(_NUMBER.finditer(normalized[max(0,term.start()-30):term.start()]))
                claimed = float(after.group()) if after else float(before[-1].group()) if before else None
                comparison = (re.search(r'\b(above|below|over|under|exceed\w*|less than|more than|equal to)\b',
                                        normalized[term.end():term.end()+after.start()],re.I) if after else None)
                if comparison:
                    centers = [float(f['values'][0]) for f in typed if f.get('values') and _finite(f['values'][0])]
                    if centers:
                        operator = comparison.group().casefold()
                        supported = (any(n<claimed for n in centers) if operator in {'below','under','less than'} else
                                     any(n==claimed for n in centers) if operator=='equal to' else any(n>claimed for n in centers))
                        if not supported:
                            raise ReplyValidationError('unsupported_metric')
                    continue
                if claimed is not None and not any(claimed in _numbers(f.get('text','')) for f in typed):
                    raise ReplyValidationError('unsupported_metric')


def _unsupported_certainty(text):
    for clause in re.split(r'(?<=[.!?;])\s+|\bbut\b', text):
        match = re.search(r'\b(?:guaranteed(?:\s+win|\s+profit)?|sure[- ]?fire|risk[- ]free|cannot lose|can[’\x27]t lose|'
                          r'(?:will|must|definitely)\s+(?:hit|cash|win|score|make|record|attempt)|(?:a|the)\s+lock)\b', clause, re.I)
        if match and not re.search(r'\b(?:not|no|never|isn[’\x27]t|aren[’\x27]t|isnt|arent|without)\b', clause[:match.start()], re.I):
            return True
    return False


def _unsupported_probability(text, cards):
    if not re.search(r'\b(?:chance|probabilit\w*|win\s*rate|likely\s+to\s+hit)\b', text, re.I):
        return False
    if not re.search(r'\d\s*%|\bpercent\b', text, re.I):
        return False
    return not any(c.get('kind') == 'calibrated_probability' and c.get('verified_calibration') is True for c in cards)


def _unsupported_research_stability(text, cards):
    """A reference level and workload mean do not measure player stability."""
    research = {card.get('entity') for card in cards if card.get('kind') == 'research_projection'}
    research.discard(None)
    if not research:
        return False
    entities, active = _entities(cards), set()
    facts = [fact for card in cards for fact in _card_facts(card)]
    production = r'(?:production|output|performance|workload|minutes|role|volume|baseline)(?:\s+level)?'
    stability = re.compile(
        r'\b(?:consistent|steady|stable|reliable)\s+(?:(?:scoring|statistical|recent|current|historical)\s+)?' + production + r'\b|'
        r'\b' + production + r'\s+(?:is|has been|looks|appears|remains)\s+(?:consistent|steady|stable|reliable)\b|'
        r'\b(?:shows?|indicates?|reflects?|demonstrates?|establishes?|proves?|confirms?|suggests?|implies)'
        r'[^.;]{0,50}\b(?:consistency|stability)\b', re.I)
    current = re.compile(r'\b(?:under|in|reflects?|describes?|captures?|represents?|based on)\s+'
                         r'(?:the\s+)?current\s+(?:conditions|role|workload|minutes|lineup)\b', re.I)
    for clause in re.split(r'(?<=[.!?;])\s+|\b(?:whereas|while|and|but)\b', text):
        mentioned = _entity_mentions(clause, entities)
        if mentioned:
            active = mentioned
        subjects = mentioned or active or research
        if not subjects & research:
            continue
        candidates = [fact for fact in facts if fact.get('entity') in subjects & research]
        for kind, pattern in (('stability', stability), ('current_conditions', current)):
            for match in pattern.finditer(clause):
                prefix, suffix = clause[:match.start()], clause[match.end():]
                # Keep uncertainty and mechanisms discussable; a later risk
                # sentence does not undo an earlier empirical stability claim.
                if re.search(r'\b(?:if|assuming|could|may|might|would|not|no|never|unknown|unclear|unconfirmed|'
                             r'cannot|can[’\x27]t|don[’\x27]t|doesn[’\x27]t)\b', prefix, re.I):
                    continue
                if re.match(r'\s+(?:is|are|has been)\s+(?:not|never|unconfirmed|unknown)\b', suffix, re.I):
                    continue
                if kind == 'stability':
                    supported = any(fact.get('fact_type') in {'stability', 'dispersion', 'workload_stability'}
                                    for fact in candidates)
                else:
                    supported = any(fact.get('fact_type') == 'current_conditions' and
                                    fact.get('verified_current') is True for fact in candidates)
                if not supported:
                    return True
    return False


def _unsupported_recommendation(text, cards):
    # Formatting must not change whether a recommendation contradicts the
    # computed assessment (for example, "lean toward **down**").
    text = re.sub(r'[*_`]+', '', text)
    decisions_by_entity = {}
    for card in cards:
        if (str(card.get('kind','')).startswith('prop_') or card.get('kind') in {'research_projection','research_unavailable'}) and card.get('decision') in {'more','less','pass'}:
            decisions_by_entity.setdefault(card.get('entity'),set()).add(card['decision'])
    if not decisions_by_entity:
        return False
    pattern = re.compile(
        r'\b(?:I|we)(?:[’\x27]d|\s+would)?\s+(?:lean|choose|prefer|recommend|take|pick|go\s+with)\s+'
        r'(?:(?:toward|towards|the)\s+)?(?P<first>more|less|over|under|up|down)\b|'
        r'\b(?:my|the)\s+(?:pick|lean|recommendation|assessment)\s+(?:is|would\s+be)\s+(?P<second>more|less|over|under|up|down)\b|'
        r'\b(?:lean(?:s|ing)?|recommend(?:s|ing)?|prefer(?:s|ring)?|favor(?:s|ing)?)\s+'
        r'(?:(?:slightly|cautiously|tentatively)\s+)?(?:(?:towards?|to|on)\s+)?(?:the\s+)?'
        r'(?P<direction>more|less|over|under|up|down)\b|'
        r'^\s*(?:lean\s*[:—-]?\s*)?(?P<heading>more|less|over|under|up|down)\s*(?:[.,:;—-]|on\b|$)|'
        r'^\s*(?:take|choose|pick)\s+(?P<imperative>more|less|over|under|up|down)\b', re.I)
    active_entities = set()
    for clause in re.split(r'(?<=[.!?;])\s+|\n|\b(?:but|and)\b', text):
        mentioned = _entity_mentions(clause,{e for e in decisions_by_entity if e})
        if mentioned:
            active_entities = mentioned
        for match in pattern.finditer(clause):
            prefix = clause[:match.start()]
            if re.search(r'\b(?:if|assuming|provided|previously|earlier|not|never)\b|\bdon[’\x27]t\b', prefix, re.I):
                continue
            if re.search(r'\bno\s+(?:(?:strong|clear|good|sufficient|compelling)\s+)?'
                         r'(?:justification|reason|basis|support|evidence)(?:\s+(?:exists|is available))?\s+(?:to|for)\s*$|'
                         r'\bno\s+(?:(?:strong|clear|definite|decisive)\s+)?$',prefix,re.I):
                continue
            if re.search(r'\bonly\s+if\b',clause[match.end():],re.I):
                continue
            entities = mentioned or active_entities
            if len(entities)>1:
                # References to another player in an explanation must not
                # lend that player's decision to the recommended selection.
                target = re.split(r'\b(?:because|since|with|whereas)\b',clause[match.end():],maxsplit=1,flags=re.I)[0]
                entities = _entity_mentions(target,entities)
            if not entities and len(decisions_by_entity)==1:
                entities = set(decisions_by_entity)
            decisions = set().union(*(decisions_by_entity[e] for e in entities)) if entities else set()
            if len(decisions)!=1:
                continue  # An ambiguous referent is not a verified contradiction.
            decision = next(iter(decisions))
            side = next(value for value in match.groupdict().values() if value)
            side = 'more' if side.casefold() in {'more','over','up'} else 'less'
            if side != decision:
                return True
    return False


def _unsupported_availability(text, cards):
    verified = any(c.get('kind') == 'verified_availability' and c.get('verified_current') is True for c in cards)
    if verified:
        return False
    for clause in re.split(r'(?<=[.!?;])\s+|\bbut\b', text):
        match = re.search(r'\b(?:he|she|they|[A-Z][a-z]+)\s+(?:is|are|will be|has been)\s+'
                          r'(?:healthy|available|out|ruled out|starting|cleared|playing tonight)\b', clause)
        if match and not re.search(r'\b(?:if|whether|unknown|unclear|unconfirmed|not|cannot confirm|can[’\x27]t confirm|reported|report says)\b',
                                   clause[:match.start()], re.I):
            return True
    return False


def _natural_sources(cards):
    return [{'tool':'scoped_evidence', 'evidence_id':c['id'], 'population_kind':c.get('kind'),
             'source':c.get('source'), 'data_through_date':c.get('data_through_date'),
             'published_at':c.get('published_at'), 'text':c.get('text', ''),
             **({'query':c['query'], 'excerpt':c.get('excerpt',False)} if c.get('query') else {})}
            for c in cards]


def _natural_footer(cards):
    if not cards:
        return ''
    labels = []
    for card in cards:
        source = str(card.get('source') or 'saved records')
        if source.startswith(('http://', 'https://')):
            from urllib.parse import urlsplit
            source = urlsplit(source).netloc.removeprefix('www.')
        # Source values can be long query/source payloads; cite stable labels,
        # while retaining their original detail in structured sources.
        labels.append(source if len(source) <= 70 else 'saved records')
    dates = sorted({str(c['data_through_date'])[:10] for c in cards if c.get('data_through_date')})
    suffix = ('; stored coverage '+('–'.join([dates[0],dates[-1]]) if len(dates)>1 else dates[0])) if dates else ''
    unknown = any(not c.get('data_through_date') and c.get('kind') in
                  {'player_all_opponent_averages','player_observed_sample','tracking_Team defense',
                   'tracking_Team totals','opponent_team_context'} for c in cards)
    if unknown:
        suffix += '; some covered-game dates unknown'
    tentative = [c for c in cards if c.get('kind') == 'tentative_assignment'
                 and c.get('verified_game_day_assignment') is not True]
    if tentative:
        asof = sorted({str(c.get('assignment_date'))[:10] for c in tentative
                       if c.get('assignment_date') and c.get('assignment_date') != 'unknown'})
        suffix += '; pairing tentative'+(' as of '+', '.join(asof) if asof else '')+'; guarding direction unconfirmed'
    if any(c.get('kind') == 'availability_coverage' for c in cards):
        suffix += '; current availability unverified'
    if any(c.get('kind') == 'publisher_report' for c in cards):
        suffix += '; publisher reports unverified'
    return 'Sources: '+', '.join(dict.fromkeys(labels))+suffix+'.'


def _brief_source_fact(card):
    """Shorten presentation only when the fact's scope and uncertainty survive."""
    if card.get('kind') == 'tentative_assignment':
        entity = str(card.get('entity') or 'the player')
        opponent = ' vs '+str(card['opponent']) if card.get('opponent') else ''
        primary = str(card.get('primary') or 'unavailable')
        alternate = ' (alternate '+str(card['alternate'])+')' if card.get('alternate') else ''
        asof = str(card.get('assignment_date') or 'unknown')
        text = f'Saved tentative pairing for {entity}{opponent}: {primary}{alternate}. Dated {asof}; guarding direction unconfirmed.'
        if card.get('assignment_refresh_state') == 'needs_review':
            text += ' Current roster or availability evidence conflicts with this saved pairing.'
        elif card.get('assignment_refresh_state') == 'recheck_overdue':
            text += ' Its roster and availability recheck is overdue.'
        return text
    text = str(card.get('brief_text') or _card_facts(card)[0].get('text', ''))
    sentences = re.split(r'(?<=[.!?])\s+', text)
    first = sentences[0].strip()
    # A first sentence can be a factual number while the next carries the
    # indispensable sample/publication qualifier. Never silently drop it.
    qualifiers = [part.strip() for part in sentences[1:] if re.search(
        r'\b(?:coverage (?:may be incomplete|is incomplete)|covered-game date is unknown|'
        r'not independently verified|guarding direction unconfirmed|tentative as of)\b', part, re.I)]
    if card.get('kind') == 'availability_coverage':
        qualifiers.append('Player health and game-day availability cannot be confirmed from these records.')
    return ' '.join(dict.fromkeys([first,*qualifiers]))


def render_natural_reply(document, cards, required=(), question=''):
    """Render a concise explanation with scoped numerical checks and citations.

    `approved_facts` may be supplied by deterministic analysis tools. It must
    contain application-authored facts, never values taken from model output.
    The question is reserved for context; user-stated statistics are not silently
    promoted to verified facts. Legacy rendering retains its original contract.
    """
    if isinstance(document, dict) and 'reply' in document and 'analysis' not in document:
        document = {('analysis' if k == 'reply' else k):v for k,v in document.items()}
    if not isinstance(document, dict) or set(document) != {'evidence_ids','analysis','needs_more_data'}:
        raise ReplyValidationError('invalid_schema')
    ids, text, more = document['evidence_ids'], document['analysis'], document['needs_more_data']
    by_id = {c['id']:c for c in cards}
    if (not isinstance(ids,list) or any(not isinstance(i,str) or i not in by_id for i in ids)
            or any(not isinstance(i,str) or i not in by_id for i in required)):
        raise ReplyValidationError('invalid_evidence')
    if not isinstance(text,str) or type(more) is not bool:
        raise ReplyValidationError('invalid_schema')
    if len(text)>1600:
        raise ReplyValidationError('analysis_too_long')
    if more:
        if text.strip():
            raise ReplyValidationError('retrieval_with_analysis')
        return None
    inline = re.findall(r'\[([A-Z]\d+)\]', text)
    if any(i not in by_id for i in inline):
        raise ReplyValidationError('invalid_evidence')
    ids = list(dict.fromkeys([*required,*ids,*inline]))
    if len(ids)>4:
        raise ReplyValidationError('invalid_evidence')
    selected = [by_id[i] for i in ids]
    text = re.sub(r'\s*\[[A-Z]\d+\]', '', text).strip()
    # IDs identify tool records, not useful concepts in a user conversation.
    # Preserve the claim and its citations while making bare labels readable.
    for identity in by_id:
        text = re.sub(r'\b(?:Evidence\s+)?'+re.escape(identity)+r'\b(?:[’\x27]s)?(?:\s+data)?',
                      'The saved data', text, flags=re.I)
    if not text:
        raise ReplyValidationError('empty_reply')
    _validate_natural_numbers(text, selected, cards, question)
    if has_unsupported_assignment(text, cards):
        raise ReplyValidationError('unsupported_assignment')
    if has_unscoped_absence(text, cards):
        raise ReplyValidationError('unsupported_scope')
    if _unsupported_availability(text, selected):
        raise ReplyValidationError('unsupported_availability')
    if _unsupported_certainty(text) or _unsupported_probability(text, selected):
        raise ReplyValidationError('unsupported_certainty')
    if _unsupported_research_stability(text, cards):
        raise ReplyValidationError('unsupported_metric')
    if _unsupported_recommendation(text, cards):
        raise ReplyValidationError('unsupported_recommendation')
    footer = _natural_footer(selected)
    return {'answer': text+('\n\n'+footer if footer else ''), 'sources':_natural_sources(selected),
            'grounding':'Retrieved facts with entity, metric and numeric checks; generated interpretation is not a calibrated forecast.'}


def natural_fallback_reply(cards, required=(), failure_message=None):
    """Preserve useful source facts without returning several raw evidence cards."""
    selected = [c for c in cards if c['id'] in required]
    selected.extend(c for c in cards if c not in selected)
    facts = []
    used = []
    for card in selected:
        sentence = _brief_source_fact(card)
        if sentence and len(sentence)<=500 and sentence not in facts:
            facts.append(sentence)
            used.append(card)
        if len(facts)==2:
            break
    message = '\n'.join(facts) or 'I need a clearer player, statistic or line to give a useful assessment.'
    if facts:
        message += '\nI could not complete the explanation, so these are saved facts rather than a pick recommendation.'
    footer = _natural_footer(used)
    return {'answer':message+('\n\n'+footer if footer else ''), 'sources':_natural_sources(used),
            'ai_used':False, 'ai_failure_category':'grounding_error'}
