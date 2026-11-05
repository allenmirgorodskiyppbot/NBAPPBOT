"""Refresh the authorized NBA population with explicit provider identity boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from feeds import canonical_player
from master import normalized_name

SCOPE_PATH=Path(__file__).with_name('refresh_scope_2026_27.json')


def load_scope(path=SCOPE_PATH):
    document=json.loads(Path(path).read_text())
    document['revision']=scope_revision(document)
    return document


def initialize_scope(store):
    scope=load_scope()
    store.set_checkpoint('dataset_refresh_scope',scope)
    return scope


def select_existing_rows(rows,scope):
    """Resolve authorized identities without merging same-name players by accident.

    An explicit provider ID disagreement cannot fall back to a matching name.
    Legacy rows with no provider identity still require one unambiguous full name.
    """
    names={};nba_ids={};espn_ids={}
    players=scope['players']
    for index,identity in enumerate(players):
        for name in [identity['player'],*identity.get('aliases',[])]:
            names.setdefault(normalized_name(name),set()).add(index)
        for field,target in [('nba_player_id',nba_ids),('espn_player_id',espn_ids)]:
            if identity.get(field):target.setdefault(str(identity[field]),set()).add(index)
    selected=[]
    for row in rows:
        named=names.get(normalized_name(row.get('player','')),set())
        nba=str(row.get('nba_player_id') or '')
        espn=str(row.get('espn_player_id') or '')
        # Legacy ESPN adapters used provider_player_id; official NBA rows carry
        # their own NBA identity and must never be interpreted as ESPN IDs.
        if not espn and not nba and row.get('provider_player_id'):
            source=str(row.get('source') or '').lower()
            if 'espn' in source or 'sportsdataverse' in source:
                espn=str(row['provider_player_id'])
        candidates=nba_ids.get(nba,set()) if nba else espn_ids.get(espn,set()) if espn else named
        if (nba or espn) and not candidates and len(named)==1:
            candidate=players[next(iter(named))]
            # Old scopes may not yet record this provider ID. They can match a
            # known name, but an already bound ID cannot disagree.
            field='nba_player_id' if nba else 'espn_player_id'
            if not candidate.get(field):candidates=named
        if len(candidates)!=1:continue
        index=next(iter(candidates));identity=players[index]
        if (nba or espn) and named and index not in named:continue
        if nba and identity.get('nba_player_id') and nba!=str(identity['nba_player_id']):continue
        if espn and identity.get('espn_player_id') and espn!=str(identity['espn_player_id']):continue
        selected.append({**row,'player':identity['player'],'player_id':canonical_player(identity['player'])})
    return selected


def index_workbook_evidence(store,workbook):
    """Keep existing dated inputs available to projections and refresh coverage."""
    # Enriched exports carry already indexed evidence; seed_workbook merges it.
    # Reinterpreting their row-1 headers as the legacy row-7 layout would erase it.
    sheets = getattr(workbook, 'sheets', {})
    if 'games' in sheets and 'player_games' in sheets:
        return
    from basketball_research import assignment_rows
    from matchup_queries import _records
    from workbook import quote
    import sqlite3
    groups={'assignments':assignment_rows(workbook),'measured_pairs':_records(workbook)}
    for sheet,key in [('Team_Schedule','rest_schedule'),('Calculated_Splits','splits'),('Correlations','correlations'),
                      ('Pair_Summaries','pair_summaries'),('Season_Totals','season_totals'),
                      ('Sources','sources'),('Missing_Data','missing_data')]:
        info=getattr(workbook,'sheets',{}).get(sheet)
        if info and info.get('named_table_rows'):
            with workbook.connect() as db:
                db.row_factory=sqlite3.Row
                groups[key]=[dict(row) for row in db.execute('SELECT * FROM '+quote(info['sql_table']))]
    inventory=[{'sheet':name,'columns':info.get('columns',[]),'rows':info.get('nonempty_rows'),
                'notes':info.get('notes',[])} for name,info in getattr(workbook,'sheets',{}).items()]
    revision=hashlib.sha256(json.dumps({'groups':groups,'inventory':inventory},sort_keys=True,default=str).encode()).hexdigest()
    prior=store.get_checkpoint('research_evidence_revision',{})
    if prior.get('revision')==revision:
        return
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS workbook_evidence (kind TEXT NOT NULL,entity TEXT NOT NULL,payload_json TEXT NOT NULL)')
        existing = {(row['kind'], row['entity'], json.dumps(json.loads(row['payload_json']),
                     sort_keys=True, default=str)) for row in db.execute('SELECT * FROM workbook_evidence')}
        for kind,rows in groups.items():
            additions = []
            for row in rows:
                entity = str(row.get('selected_player') or row.get('player') or row.get('offense_player') or row.get('team') or '')
                payload = json.dumps(row, sort_keys=True, default=str)
                key = (kind, entity, payload)
                if key not in existing:
                    additions.append(key)
                    existing.add(key)
            db.executemany('INSERT INTO workbook_evidence VALUES (?,?,?)', additions)
    store.set_checkpoint('research_evidence_revision',{'revision':revision,'scope':'Existing workbook records only; no new metric types or pair definitions.'})
    prior_inventory = {item['sheet']: item for item in store.get_checkpoint('workbook_sheet_inventory', [])}
    prior_inventory.update({item['sheet']: item for item in inventory})
    store.set_checkpoint('workbook_sheet_inventory',list(prior_inventory.values()))
    store.set_checkpoint('metric_refresh_coverage',{
        'player_production_efficiency_volume':'Recomputed from new observations for existing players after every imported completed game, against any opponent.',
        'overall_team_statistics':'Only existing team metrics and matching source observations may refresh; incomplete player subsets do not establish team totals.',
        'whole_game_opponent_splits':'Only actual games against the named opponent enter that population.',
        'named_defender_pairs':'Only actual measured named-pair evidence updates this population; unrelated games never enter it.',
        'passing_rebounding_defense_shot_profiles':'Require their own NBA tracking observations. Missing or blocked sources retain prior values with stale/unavailable status.',
        'historical_seasons':'Dated historical cohorts remain separate from current-season updates.'})


def supplement_existing_observation_fields(store,workbook):
    """Restore omitted existing Excel fields without replacing newer observations."""
    import math
    import sqlite3
    from feeds import canonical_game
    from workbook import quote
    info=getattr(workbook,'sheets',{}).get('Player_Games')
    if not info:return 0
    fields=[f for f in ('fgm','ftm','fta','oreb','dreb','fouls','plus_minus','starter') if f in info.get('columns',[])]
    if not fields:return 0
    key='existing_field_supplement:'+str(store.get_checkpoint('research_evidence_revision',{}).get('revision',''))
    if store.get_checkpoint(key):return 0
    with workbook.connect() as source:
        source.row_factory=sqlite3.Row
        rows=[dict(r) for r in source.execute('SELECT * FROM '+quote(info['sql_table']))]
    changed=0
    with store.connect() as db:
        for row in rows:
            try:
                game_id=canonical_game(str(row['date'])[:10],row['team'],row['opponent'])
                player_id=canonical_player(row['player'])
            except (KeyError,TypeError):continue
            existing=db.execute('SELECT * FROM player_games WHERE game_id=? AND player_id=?',(game_id,player_id)).fetchone()
            if not existing or not str(existing['source'] or '').startswith('workbook:'):continue
            if any(row.get(k) is not None and existing[k] is not None and float(row[k])!=float(existing[k]) for k in ('pts','reb','ast','minutes')):continue
            updates={}
            for field in fields:
                value=row.get(field)
                if existing[field] is not None or value is None:continue
                if field=='starter' and isinstance(value,bool):updates[field]=int(value)
                elif type(value) in (int,float) and math.isfinite(value) and (field=='plus_minus' or value>=0):updates[field]=value
            if updates:
                from master import now
                db.execute('UPDATE player_games SET '+','.join(f'{f}=?' for f in updates)+',updated_at=? WHERE game_id=? AND player_id=?',
                           (*updates.values(),now(),game_id,player_id));changed+=1
    store.set_checkpoint(key,{'rows_supplemented':changed,'scope':'Existing original workbook fields only; live/manual observations are never overwritten.'})
    return changed


def scope_revision(document):
    """The revision identifies identities/policy, never a new retrieval clock."""
    return hashlib.sha256(json.dumps({k:v for k,v in document.items() if k!='revision'},sort_keys=True).encode()).hexdigest()


def expand_current_player_scope(scope, rows):
    """Add current NBA debuts only from fully validated, normalized source games."""
    from feeds import TEAM_WORDS, canonical_team
    from game_phase import regular_phase_exclusion
    import copy
    if scope.get('population')!='all_current_nba_players':return scope
    result=copy.deepcopy(scope)
    ids={str(p['espn_player_id']):p for p in result['players'] if p.get('espn_player_id')}
    names={}
    for player in result['players']:
        for name in [player['player'],*player.get('aliases',[])]:
            names.setdefault(normalized_name(name),set()).add(player['player'])
    for row in rows:
        provider=str(row.get('espn_player_id') or '')
        name=str(row.get('player') or '').strip()
        normalized=normalized_name(name)
        if (not provider.isdigit() or int(provider)<=0 or not 4<=len(normalized)<=100
                or regular_phase_exclusion(row) or row.get('appearance_state') not in {'Played','DNP','Unknown'}
                or canonical_team(row.get('team')) not in TEAM_WORDS):
            raise ValueError('Current roster discovery has an unresolved source identity or phase.')
        if provider in ids:
            player=ids[provider]
            if normalized not in {normalized_name(n) for n in [player['player'],*player.get('aliases',[])]}:
                raise ValueError('Current source player ID changed names without an authorized alias.')
            continue
        if normalized in names:
            raise ValueError('New provider identity collides with an existing authorized full name.')
        player={'player':name,'player_id':canonical_player(name),'espn_player_id':provider,'aliases':[name]}
        result['players'].append(player);ids[provider]=player;names[normalized]={name}
    if len(result['players'])>2000:raise ValueError('Current player scope exceeds its population bound.')
    result['players'].sort(key=lambda player:normalized_name(player['player']))
    result['revision']=scope_revision(result)
    return result


def validate_published_scope(document, bundled, previous, now):
    """Validate a trusted relay's additive identity manifest before changing scope."""
    import datetime as dt
    import re
    from news_digest import _aware
    from feeds import TEAM_WORDS
    from zoneinfo import ZoneInfo
    local=now.astimezone(ZoneInfo('America/New_York'))
    year=local.year-(local.month<7);season=f'{year}-{str(year+1)[-2:]}'
    if (bundled.get('population')!='all_current_nba_players' or not isinstance(document,dict)
            or document.get('schema_version')!=1 or document.get('season')!=season
            or document.get('base_revision')!=bundled['revision']
            or document.get('upstream_full_game_validated') is not True):
        raise ValueError('Published roster scope is outside the authorized population or release.')
    source=document.get('source_url')
    expected=f'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_{year+1}.parquet'
    published=_aware(document.get('source_published_at'));checked=_aware(document.get('source_checked_at'))
    through=document.get('source_data_through_date')
    try:day=dt.date.fromisoformat(through)
    except (TypeError,ValueError):raise ValueError('Published roster scope lacks a covered game date.') from None
    if (source!=expected or not published or not checked or published>now+dt.timedelta(minutes=5)
            or checked>now+dt.timedelta(minutes=5) or day>local.date()
            or not re.fullmatch(r'[0-9a-f]{64}',str(document.get('source_content_sha256','')))):
        raise ValueError('Published roster scope provenance is invalid.')
    scope=document.get('scope')
    if (not isinstance(scope,dict) or scope.get('population')!='all_current_nba_players'
            or not isinstance(scope.get('players'),list) or not 1<=len(scope['players'])<=2000
            or set(scope.get('teams',[]))!=set(TEAM_WORDS) or scope.get('revision')!=scope_revision(scope)):
        raise ValueError('Published roster scope content or revision is invalid.')
    if {k:v for k,v in scope.items() if k not in {'players','revision'}}!={k:v for k,v in bundled.items() if k not in {'players','revision'}}:
        raise ValueError('Published roster scope changes policy outside its additive identity permission.')
    identities={};names={};provider_ids={};nba_ids={}
    for player in scope['players']:
        if not isinstance(player,dict):raise ValueError('Published roster player has an invalid schema.')
        name=player.get('player');identity=player.get('player_id') or canonical_player(name)
        if not isinstance(name,str) or identity!=canonical_player(name) or identity in identities:
            raise ValueError('Published roster scope has a missing or duplicate canonical identity.')
        identities[identity]=player
        aliases=player.get('aliases',[])
        if not isinstance(aliases,list) or any(not isinstance(alias,str) for alias in aliases):
            raise ValueError('Published roster aliases have an invalid schema.')
        for alias in [name,*aliases]:
            normalized=normalized_name(alias)
            if normalized in names and names[normalized]!=identity:
                raise ValueError('Published roster scope contains an ambiguous full name.')
            names[normalized]=identity
        for field,seen in [('espn_player_id',provider_ids),('nba_player_id',nba_ids)]:
            value=player.get(field)
            if value is not None:
                value=str(value)
                if not value.isdigit() or int(value)<=0 or value in seen:
                    raise ValueError('Published roster scope has an invalid or duplicate provider identity.')
                seen[value]=identity
    prior_by_id={canonical_player(p['player']):p for p in previous['players']}
    for identity,player in prior_by_id.items():
        if identities.get(identity)!=player:
            raise ValueError('Published roster scope removes or rewrites an existing identity.')
    for identity,player in identities.items():
        if identity not in prior_by_id and (not player.get('espn_player_id') or
                set(player)-{'player','player_id','espn_player_id','aliases'}):
            raise ValueError('New current NBA players need only their uniquely published ESPN identity.')
    return scope


async def refresh_published_scope(store,worker,now):
    """Refresh the explicitly authorized NBA population once per hour; fail closed."""
    import datetime as dt
    from news_digest import _aware
    bundled=load_scope();prior=store.get_checkpoint('dataset_refresh_scope',bundled) or bundled
    if bundled.get('population')!='all_current_nba_players':return prior
    status=store.get_checkpoint('dataset_scope_refresh',{}) or {}
    checked=_aware(status.get('last_attempt'))
    if checked and 0<=(now-checked).total_seconds()<3600:return prior
    source='https://raw.githubusercontent.com/allenmirgorodskiyppbot/NBAPPBOT/main/data/live/existing_dataset_scope.json'
    try:
        import asyncio
        async with asyncio.timeout(10):document=await worker.request(source)
        scope=validate_published_scope(document,bundled,prior,now)
        store.set_checkpoint('dataset_refresh_scope',scope)
        store.set_checkpoint('dataset_scope_refresh',{'phase':'updated','last_attempt':now.isoformat(),
            'players':len(scope['players']),'revision':scope['revision'],'source':source,
            'source_published_at':document['source_published_at'],
            'source_data_through_date':document['source_data_through_date']})
        return scope
    except Exception as error:
        store.set_checkpoint('dataset_scope_refresh',{'phase':'retained_prior_scope',
            'last_attempt':now.isoformat(),'players':len(prior['players']),'error_type':type(error).__name__})
        return prior
