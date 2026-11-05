"""Exact missing-value coverage for saved observations, without inventing data."""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import json
import math
import re
from urllib.parse import urlsplit

BOX_FIELDS = {
    'pts': 'Points', 'reb': 'Rebounds', 'ast': 'Assists', 'tpm': 'Three-pointers made',
    'fga': 'Field-goal attempts', 'tpa': 'Three-point attempts', 'minutes': 'Minutes played',
    'stl': 'Steals', 'blk': 'Blocks', 'tov': 'Turnovers', 'fgm': 'Field goals made',
    'ftm': 'Free throws made', 'fta': 'Free-throw attempts', 'oreb': 'Offensive rebounds',
    'dreb': 'Defensive rebounds', 'fouls': 'Personal fouls',
    'plus_minus': 'Plus/minus', 'starter': 'Historical starter flag',
}
GAME_FIELDS = {'home_score':'Home team final score', 'away_score':'Away team final score'}


def _valid(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    if field == 'starter':
        return value in (0, 1)
    return field == 'plus_minus' or value >= 0


def _score_issue(value):
    if value is None:
        return 'missing'
    if type(value) not in (int,float) or not math.isfinite(value):
        return 'non_numeric_or_non_finite'
    if value < 0:
        return 'negative_score'
    if not float(value).is_integer():
        return 'fractional_score'
    if value > 400:
        return 'score_exceeds_verified_source_bound'
    return None


def _document(value):
    if isinstance(value,str):
        try:
            return json.loads(value)
        except (ValueError,TypeError):
            return None
    return value


def _completed_scores(tables, games):
    """Match saved immutable completion proofs; never treat any URL as proof."""
    completed=set()
    for audit in tables.get('Game_Score_Completions',tables.get('game_score_completions',[])):
        game=games.get(audit.get('game_id'))
        field=audit.get('statistic')
        if not game or field not in GAME_FIELDS or _score_issue(game.get(field)):
            continue
        before,after,evidence=(_document(audit.get(key)) for key in
            ('before_row_json','after_row_json','source_evidence_json'))
        value=_document(audit.get('new_value_json'))
        if (not all(isinstance(row,dict) for row in (before,after,evidence))
                or str(audit.get('old_value_json')).strip()!='null'
                or _score_issue(value) or value!=game[field] or before.get(field) is not None
                or after.get(field)!=value
                or not re.fullmatch(r'[0-9a-f]{64}',str(audit.get('manifest_sha256') or ''))):
            continue
        source=evidence.get('game')
        if not isinstance(source,dict) or source.get('upstream_full_game_validated') is not True:
            continue
        identity=('id','date','home_team','away_team')
        if (any(any(row.get(key)!=game.get(key) for key in identity) for row in (before,after,source))
                or source.get('status')!='final' or source.get(field)!=value
                or not re.fullmatch(r'[0-9a-f]{64}',str(source.get('source_content_sha256') or ''))
                or any(before.get(key)!=after.get(key) for key in set(before)|set(after) if key not in GAME_FIELDS)
                or any(_score_issue(source.get(key)) for key in GAME_FIELDS)
                or source['home_score']==source['away_score']):
            continue
        try:
            address=urlsplit(str(source.get('source') or ''))
        except ValueError:
            continue
        supported=((address.hostname=='github.com' and address.path.startswith(
            '/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/'))
            or (address.hostname=='site.api.espn.com' and address.path=='/apis/site/v2/sports/basketball/nba/summary')
            or (address.hostname=='github.com' and address.path.startswith(
                '/sportsdataverse/sportsdataverse-data/releases/download/nba_stats_player_boxscores/')))
        try:
            published=dt.datetime.fromisoformat(str(source.get('source_published_at')).replace('Z','+00:00'))
            completed_at=dt.datetime.fromisoformat(str(audit.get('completed_at')).replace('Z','+00:00'))
            source_day=dt.date.fromisoformat(str(source.get('date')))
            if (address.scheme!='https' or address.username or address.password or not supported
                    or published.tzinfo is None or completed_at.tzinfo is None
                    or completed_at<published or source_day>published.date()):
                continue
        except (ValueError,TypeError):
            continue
        completed.add((game['id'],field))
    return completed


def _game_gap_tables(tables, games):
    final=[row for row in games.values() if row.get('status')=='final']
    completed=_completed_scores(tables,games)
    summaries,gaps=[],[]
    for field,label in GAME_FIELDS.items():
        missing,invalid=[],[]
        source_completed=0
        for game in final:
            issue=_score_issue(game.get(field))
            if not issue and all(_score_issue(game.get(key)) is None for key in GAME_FIELDS) \
                    and game['home_score']==game['away_score']:
                issue='tied_final_score'
            if issue:
                (missing if issue=='missing' else invalid).append(game)
                value=game.get(field)
                gaps.append({**{key:game.get(key) for key in
                    ('date','home_team','away_team','status','source','provider_game_id')},
                    'game_id':game['id'],'statistic':label,'field':field,
                    'saved_value':repr(value) if type(value) is float and not math.isfinite(value) else value,
                    'issue':'missing' if issue=='missing' else 'invalid_value',
                    'detail':issue,'population':'Saved final games only; scheduled/live/canceled games excluded'})
            elif (game['id'],field) in completed:
                source_completed+=1
        affected=missing+invalid
        dates=[row['date'] for row in affected if row.get('date')]
        valid=len(final)-len(affected)
        summaries.append({'category':'Game final score','statistic':label,'field':field,
            'eligible_final_games':len(final),'records_with_valid_value':valid,
            'missing_records':len(missing),'invalid_records':len(invalid),
            'source_completed_records':source_completed,
            'valid_records_without_completion_audit':valid-source_completed,
            'first_affected_game':min(dates) if dates else None,
            'last_affected_game':max(dates) if dates else None,
            'status':'missing_values' if affected else 'populated_in_saved_final_games' if final else 'no_saved_final_games',
            'source_completion_scope':'Matches saved immutable full-game completion audit; export does not refetch sources.',
            'scope':'Saved final events only; populated scores do not establish complete league or lifetime game coverage.'})
    return summaries,sorted(gaps,key=lambda row:(row.get('date') or '',row['game_id'],row['field']))


def augment_gap_tables(master, tables):
    """Append exact field and record lists to an existing export snapshot.

    DNPs and unknown participation are separate populations. An undefined rate
    with a zero denominator is not a missing counting statistic.
    """
    from advanced_refresh import MAPS
    games = {row['id']: row for row in tables.get('games', [])}
    game_coverage,game_gaps=_game_gap_tables(tables,games)
    played, uncertain, not_final = [], [], []
    dnp_count = 0
    for row in tables.get('player_games', []):
        state = str(row.get('appearance_state') or '').lower()
        if state == 'dnp':
            dnp_count += 1
        elif state != 'played':
            uncertain.append({key: row.get(key) for key in
                ('player', 'player_id', 'game_id', 'date', 'team', 'season', 'season_type', 'appearance_state', 'source')})
        elif games.get(row.get('game_id'), {}).get('status') != 'final':
            not_final.append(row)
        else:
            played.append(row)
    missing, summaries = [], []
    for field, label in BOX_FIELDS.items():
        absent = [row for row in played if row.get(field) is None]
        invalid = [row for row in played if row.get(field) is not None and not _valid(row[field], field)]
        affected = defaultdict(int)
        for row in absent + invalid:
            affected[str(row.get('player') or '')] += 1
            missing.append({**{key: row.get(key) for key in
                ('player', 'player_id', 'game_id', 'date', 'team', 'opponent', 'season', 'season_type', 'source')},
                'statistic': label, 'field': field,
                'issue': 'missing' if row.get(field) is None else 'invalid_value',
                'population': 'Final played games; DNP and unknown appearances excluded'})
        dates = [row['date'] for row in absent + invalid if row.get('date')]
        summaries.append({'category': 'Player game box score', 'statistic': label, 'field': field,
            'eligible_played_records': len(played), 'missing_records': len(absent),
            'invalid_records': len(invalid), 'affected_players': len(affected),
            'affected_player_counts': dict(sorted(affected.items())),
            'first_affected_game': min(dates) if dates else None,
            'last_affected_game': max(dates) if dates else None,
            'status': 'missing_values' if absent or invalid else 'populated_in_saved_played_games',
            'scope': 'Counts describe saved records, not an independently complete career inventory.'})
    tracking = defaultdict(list)
    for row in tables.get('refreshed_tracking', []):
        record = json.loads(row['payload_json']) if isinstance(row.get('payload_json'), str) else row.get('payload_json', {})
        tracking[str(row.get('category') or record.get('category'))].append(record)
    tracking_rows = []
    def covered_date(record, field):
        return record.get('field_provenance', {}).get(field, {}).get('data_through_date',
               record.get('data_through_date'))
    for category, mapping in MAPS.items():
        records = tracking.get(category, [])
        expected = list(dict.fromkeys(mapping.values()))
        if category in {'Overall', 'Less Than 6Ft', 'Rim', 'Team defense', 'Named pair'}:
            expected.append('fg_pct' if category == 'Named pair' else 'fg_pct_allowed')
        for field in expected:
            observed = [record for record in records if record.get('fields', {}).get(field) is not None]
            undefined = 0
            if field in {'fg_pct', 'fg_pct_allowed'}:
                numerator, denominator = (('fgm', 'fga') if category == 'Named pair' else
                    ('rim_defended_fgm', 'rim_defended_fga') if category in {'Rim', 'Less Than 6Ft'} else
                    ('defended_fgm', 'defended_fga'))
                undefined = sum(record.get('fields', {}).get(numerator) is not None and
                    record.get('fields', {}).get(denominator) == 0 for record in records)
            missing_count = len(records) - len(observed) - undefined
            tracking_rows.append({'category': category, 'field': field, 'records_collected': len(records),
                'records_with_value': len(observed), 'missing_values_in_collected_records': missing_count,
                'undefined_zero_attempt_ratios': undefined,
                'records_with_unknown_covered_date': sum(not covered_date(record, field) for record in observed),
                'status': 'source_unavailable' if not records else 'missing_values' if missing_count else
                          'undefined_zero_attempt_ratio' if undefined == len(records) else
                          'value_present_coverage_unknown' if any(not covered_date(r, field) for r in observed) else
                          'source_values_dated',
                'scope': 'Refreshed tracking population only; dated historical measured-pair rows remain separate.'})
    tables['Missing_Stats_Summary'] = summaries
    tables['Missing_Stats_By_Game'] = sorted(missing, key=lambda row: (row.get('date') or '', row['player'], row['field']))
    tables['Tracking_Field_Coverage'] = tracking_rows
    tables['Game_Stat_Coverage'] = game_coverage
    tables['Game_Stat_Gaps'] = game_gaps
    tables['Unknown_Participation'] = uncertain
    tables['Missing_Stats_ReadMe'] = [
        {'topic':'Start here','value':'Missing_Stats_Summary lists missing or invalid player statistics; Missing_Stats_By_Game identifies each affected player and game.'},
        {'topic':'Game scores','value':'Game_Stat_Coverage summarizes home_score and away_score; Game_Stat_Gaps lists each missing or invalid final-game score.'},
        {'topic':'Tracking gaps','value':'Tracking_Field_Coverage identifies exact unavailable fields and values with unknown covered-game dates.'},
        {'topic':'Completed score evidence','value':'Game_Score_Completions preserves source repair evidence when available. Source-completed counts are separate from other populated values.'},
        {'topic': 'Eligible records', 'value': len(played)},
        {'topic': 'DNP observations', 'value': dnp_count, 'note': 'Not missing played statistics; never imputed as played zeroes.'},
        {'topic': 'Unknown participation', 'value': len(uncertain), 'note': 'Participation is unverified; not silently counted as DNP or Played.'},
        {'topic': 'Played observations awaiting final event', 'value': len(not_final)},
        {'topic': 'Population limits', 'value': 'Populated fields do not prove that every expected career game has been imported.'},
        {'topic': 'Tracking dates', 'value': 'Publication, retrieval, report time and actual covered-game dates are separate.'},
    ]
    return tables
