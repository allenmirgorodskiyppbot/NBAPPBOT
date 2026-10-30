"""Refresh only the players, teams and statistics already authorized by the workbook."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from feeds import canonical_player
from master import normalized_name

SCOPE_PATH=Path(__file__).with_name('refresh_scope_2026_27.json')


def load_scope(path=SCOPE_PATH):
    document=json.loads(Path(path).read_text())
    document['revision']=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
    return document


def initialize_scope(store):
    scope=load_scope()
    store.set_checkpoint('dataset_refresh_scope',scope)
    return scope


def select_existing_rows(rows,scope):
    names={normalized_name(row['player']):row for row in scope['players']}
    nba_ids={str(row['nba_player_id']):row for row in scope['players'] if row.get('nba_player_id')}
    selected=[]
    for row in rows:
        identity=nba_ids.get(str(row.get('nba_player_id'))) or names.get(normalized_name(row.get('player','')))
        if not identity:
            continue
        # Match known NBA person IDs when supplied, otherwise require the existing
        # canonical full name. No fuzzy automatic identity merge or scope expansion.
        selected.append({**row,'player':identity['player'],'player_id':canonical_player(identity['player'])})
    return selected


def index_workbook_evidence(store,workbook):
    """Keep existing dated inputs available to projections and refresh coverage."""
    from basketball_research import assignment_rows
    from matchup_queries import _records
    from workbook import quote
    import sqlite3
    groups={'assignments':assignment_rows(workbook),'measured_pairs':_records(workbook)}
    for sheet,key in [('Team_Schedule','rest_schedule'),('Calculated_Splits','splits'),('Correlations','correlations')]:
        info=getattr(workbook,'sheets',{}).get(sheet)
        if info and info.get('named_table_rows'):
            with workbook.connect() as db:
                db.row_factory=sqlite3.Row
                groups[key]=[dict(row) for row in db.execute('SELECT * FROM '+quote(info['sql_table']))]
    revision=hashlib.sha256(json.dumps(groups,sort_keys=True,default=str).encode()).hexdigest()
    prior=store.get_checkpoint('research_evidence_revision',{})
    if prior.get('revision')==revision:
        return
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS workbook_evidence (kind TEXT NOT NULL,entity TEXT NOT NULL,payload_json TEXT NOT NULL)')
        db.execute('DELETE FROM workbook_evidence')
        for kind,rows in groups.items():
            db.executemany('INSERT INTO workbook_evidence VALUES (?,?,?)',[
                (kind,str(row.get('selected_player') or row.get('player') or row.get('offense_player') or row.get('team') or ''),json.dumps(row,default=str))
                for row in rows])
    store.set_checkpoint('research_evidence_revision',{'revision':revision,'scope':'Existing workbook records only; no new metric types or pair definitions.'})
    store.set_checkpoint('metric_refresh_coverage',{
        'player_production_efficiency_volume':'Recomputed from new observations for existing players after every imported completed game, against any opponent.',
        'overall_team_statistics':'Only existing team metrics and matching source observations may refresh; incomplete player subsets do not establish team totals.',
        'whole_game_opponent_splits':'Only actual games against the named opponent enter that population.',
        'named_defender_pairs':'Only actual measured named-pair evidence updates this population; unrelated games never enter it.',
        'passing_rebounding_defense_shot_profiles':'Require their own NBA tracking observations. Missing or blocked sources retain prior values with stale/unavailable status.',
        'historical_seasons':'Dated historical cohorts remain separate from current-season updates.'})
