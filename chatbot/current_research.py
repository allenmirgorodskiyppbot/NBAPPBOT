"""Bounded read-only access to the same derivatives used by the Excel export."""
from __future__ import annotations

import datetime as dt
import json
import re

TABLE_FILTERS = {
    'Player_Metrics': {'player', 'season', 'season_type', 'statistic'},
    'Calculated_Splits': {'player', 'season_type', 'window', 'definition', 'filter',
                          'opponent_team', 'named_matchup_player', 'statistic', 'split_id'},
    'Correlations': {'player', 'other_player', 'x', 'y', 'window', 'season_type', 'relation'},
    'Pair_Summaries': {'assignment_id', 'direction', 'player', 'named_player', 'role', 'window', 'season_type'},
    'Tracking_Metrics': {'entity', 'category', 'season', 'season_type', 'metric'},
    'Measured_Pair_Metrics': {'offense_player', 'defense_player', 'season', 'season_type', 'metric'},
    'Matchup_Assignments': {'selected_player', 'opponent_team', 'assignment_id'},
    'Team_Schedule': {'team', 'date', 'opponent', 'season'},
    'Refresh_Coverage': {'original_sheet', 'group', 'category', 'status'},
}
ENTITY_FILTERS = {'player', 'other_player', 'named_player', 'entity', 'offense_player',
                  'defense_player', 'selected_player', 'team', 'assignment_id', 'split_id'}
NOTE = ('Read from saved evidence using the same calculations as the current Excel export. '
        'Read time is not source freshness. Source dates, sample sizes, missing inputs and '
        'unavailable/stale statuses remain authoritative; original workbook rows are separate.')


def wants_current_research(question):
    """Route requests for derivative capabilities, not player-specific answers."""
    return bool(re.search(r'\b(?:correlations?|correlat(?:ed|es|ion)|pearson|splits?|coappearances?|'
                          r'co-appearances?|back[ -]to[ -]backs?|rest[ -](?:days?|splits?|adjusted)|'
                          r'home\s+(?:vs\.?|versus|and)\s+away|home/away|'
                          r'pair\s+summar(?:y|ies))\b', str(question), re.I))


def _equal(left, right):
    if left is None:
        return right is None
    return str(left).strip().casefold() == str(right).strip().casefold()


def read_current_research(master, table, filters=None, limit=5, now=None):
    if table not in TABLE_FILTERS:
        raise ValueError('Choose an available current research table: ' + ', '.join(TABLE_FILTERS))
    if filters is None:
        filters = {}
    if not isinstance(filters, dict) or len(filters) > 10:
        raise ValueError('Use a small object of exact named filters.')
    unknown = set(filters) - TABLE_FILTERS[table]
    if unknown:
        raise ValueError('Unsupported filter for ' + table + '; available filters: ' + ', '.join(sorted(TABLE_FILTERS[table])))
    if any(value is not None and (not isinstance(value, str) or not 1 <= len(value.strip()) <= 150)
           for value in filters.values()):
        raise ValueError('Filter values must be exact nonempty text or null.')
    if table != 'Refresh_Coverage' and not ENTITY_FILTERS.intersection(filters):
        raise ValueError('Choose a specific player/entity, team or existing assignment before reading ' + table + '.')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError('Use a result limit between 1 and 20.')
    now = now or dt.datetime.now(dt.timezone.utc)
    from refresh_export import build_export_tables
    records = build_export_tables(master, now=now).get(table, [])
    matches = [row for row in records if all(key in row and _equal(row[key], value)
                                            for key, value in filters.items())]
    selected = []
    for row in matches[:limit]:
        if table == 'Matchup_Assignments':
            from assignment_refresh import refresh_caution
            original = row
            # Deep historical candidates remain in the downloadable workbook;
            # they must not crowd out the original assignment date/caveat here.
            row = {k: v for k, v in row.items() if k not in {'candidate_checks', 'historical_measured_candidates'}}
            row['recheck_caution'] = refresh_caution(original)
        selected.append(row)
    return {'table': table, 'filters': dict(filters), 'rows': selected,
            'matched_rows': len(matches), 'returned_rows': len(selected), 'truncated': len(matches) > limit,
            'read_at': now.isoformat(), 'note': NOTE,
            'source': 'Regenerated saved-master research; same definitions as NBA_Master.xlsx',
            'available_filters': sorted(TABLE_FILTERS[table]),
            'no_matches_note': ('No saved existing definition/evidence matched these exact filters; '
                                'no broader season, player or metric was substituted.') if not matches else None}


def current_research_card(result, index):
    """Keep complete source rows and caveats; truncate only between rows."""
    if not isinstance(result, dict) or result.get('table') not in TABLE_FILTERS:
        return None
    rows = []
    filters = json.dumps(result.get('filters', {}), ensure_ascii=False, separators=(',', ':'))
    prefix = f"Regenerated saved research: {result['table']}. Exact filters: {filters}. "
    suffix = ' Read time is not freshness. Source dates/statuses and missing counts apply; whole-game coappearance is not guarding.'
    for row in result.get('rows', [])[:3]:
        encoded = json.dumps([*rows, row], ensure_ascii=False, default=str, separators=(',', ':'))
        if len(prefix) + len(encoded) + len(suffix) > 2450:
            break
        rows.append(row)
    if result.get('rows') and not rows:
        # Do not drop freshness fields to squeeze a large factual record in.
        return None
    body = json.dumps(rows, ensure_ascii=False, default=str, separators=(',', ':')) if rows else str(result.get('no_matches_note'))
    excerpt = bool(result.get('truncated') or len(rows) < len(result.get('rows', [])))
    return {'id': f'E{index}', 'kind': 'query_result', 'source': 'read_current_research',
            'text': prefix + body + suffix,
            'scope': 'Exact named filters; no population or source freshness inferred.',
            'query': {'table': result['table'], 'filters': result.get('filters', {})},
            'excerpt': excerpt,
            'prompt_descriptor': ('Regenerated saved research records were retrieved for '
                                  + result['table'] + ' using exact requested filters. '
                                  + ('The matching saved records and their dates/sample limits will be shown verbatim. '
                                     if rows else 'No saved existing definition/evidence matched; explain that scope limit. ')
                                  + 'Read time does not establish freshness; unavailable values are not estimates. '
                                    'Correlations and whole-game coappearances do not prove a defender effect.')}
