"""Read independently published public NBA extracts on GitHub.

The upstream publication time and latest covered game date are retained. A file
retrieved tonight is never relabeled as tonight's results if it ends yesterday.
Parquet conversion runs on the data collector, not on the Akash chat service.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import email.utils
import hashlib
from pathlib import Path

import httpx

from dataset_refresh import load_scope,select_existing_rows
from feeds import canonical_game,canonical_player,canonical_team,numeric
from nba_daily_export import validate_final

REPO='sportsdataverse/sportsdataverse-data'
CSV_TAG='espn_nba_player_boxscores'
STATS_TAG='nba_stats_leaguedash'


def tracking_coverage(record, previous):
    """A different game's publication cannot date an unchanged tracking record.

    These Parquet extracts report cumulative statistics and a publication time,
    but no covered-game date. Preserve a prior date only for identical scoped
    statistics from the same source with an explicit coverage origin. Legacy
    dates inferred from a separate game CSV are not verified. Changed/new values retain an unknown
    coverage date instead of borrowing the independent game feed's latest date.
    """
    identity = ('entity', 'category', 'season', 'season_type', 'team', 'gp', 'fields', 'source')
    unchanged = isinstance(previous, dict) and all(record.get(k) == previous.get(k) for k in identity)
    trusted = {'source_reported', 'verified_game_inventory'}
    basis = previous.get('coverage_basis') if isinstance(previous, dict) else None
    origin = previous.get('coverage_origin') if isinstance(previous, dict) else None
    if basis in trusted:
        origin = basis
    proven = basis in trusted or (basis == 'prior_unchanged_source_statistics' and origin in trusted)
    prior_date = previous.get('data_through_date') if unchanged and proven else None
    try:
        valid_date = dt.date.fromisoformat(prior_date).isoformat() == prior_date
    except (ValueError, TypeError):
        valid_date = False
    record['data_through_date'] = prior_date if valid_date else None
    record['coverage_basis'] = ('prior_unchanged_source_statistics' if valid_date else
                                'not_reported_by_tracking_source')
    record['coverage_origin'] = origin if valid_date else None
    return record


def existing_game_snapshots(content,scope,published_at,source):
    grouped={}
    for raw in csv.DictReader(io.StringIO(content.decode('utf-8-sig'))):
        if raw.get('season_type')!='2':continue
        day=raw['game_date'][:10];home_side=raw.get('home_away')=='home'
        team=canonical_team(raw.get('team_abbreviation'));opponent=canonical_team(raw.get('opponent_team_abbreviation'))
        home,away=(team,opponent) if home_side else (opponent,team)
        game_id=canonical_game(day,home,away);name=raw.get('athlete_display_name')
        if not name:continue
        group=grouped.setdefault((day,game_id),{'game':{'id':game_id,'date':day,'home_team':home,'away_team':away,
            'home_score':numeric(raw.get('team_score') if home_side else raw.get('opponent_team_score')),
            'away_score':numeric(raw.get('opponent_team_score') if home_side else raw.get('team_score')),
            'provider_game_id':raw.get('game_id'),'start_time':raw.get('game_date_time'),'status':'final','source':source},'rows':[]})
        played=raw.get('did_not_play','').lower()=='false'
        minutes=numeric(raw.get('minutes'));points=numeric(raw.get('points'))
        state='Played' if played and minutes is not None and minutes>0 else 'DNP' if not played else 'Unknown'
        year=int(raw['season']);row={'game_id':game_id,'player_id':canonical_player(name),'player':name,
            'provider_player_id':raw.get('athlete_id'),'team':team,'opponent':opponent,'date':day,
            'season':f'{year-1}-{str(year)[-2:]}','season_type':'Regular Season','appearance_state':state,
            'minutes':minutes if state!='DNP' else None,'source':source,
            'minute_precision':'Publisher numeric minutes; zero/absent minutes with no DNP flag remain unknown.'}
        for column,field in [('points','pts'),('rebounds','reb'),('assists','ast'),('field_goals_made','fgm'),
            ('field_goals_attempted','fga'),('three_point_field_goals_made','tpm'),('three_point_field_goals_attempted','tpa'),
            ('free_throws_made','ftm'),('free_throws_attempted','fta'),('offensive_rebounds','oreb'),
            ('defensive_rebounds','dreb'),('steals','stl'),('blocks','blk'),('turnovers','tov'),('fouls','fouls'),('plus_minus','plus_minus')]:
            row[field]=numeric(raw.get(column)) if state!='DNP' else None
        group['rows'].append(row)
    days={}
    for (day,game_id),group in grouped.items():
        target=days.setdefault(day,{'schema_version':1,'date':day,'source':'espn','publication_source':'SportsDataverse public ESPN extract',
            'source_url':source,'published_at':published_at,'expected_games':0,'games':[],'player_games':[],
            'pending_game_ids':[],'errors':[],'scope_revision':scope['revision'],'upstream_full_game_validated':True,
            'coverage_note':'Inventory covers completed games in the public publication, not an independently queried live scoreboard.'})
        target['expected_games']+=1
        try:
            validate_final(group['game'],group['rows'])
            target['games'].append(group['game']);target['player_games'].extend(select_existing_rows(group['rows'],scope))
        except ValueError:
            target['pending_game_ids'].append(game_id);target['errors'].append({'game_id':game_id,'error_type':'SourceValidationError'})
    through=max(days) if days else None
    for day,target in days.items():
        target['complete']=not target['pending_game_ids'];target['source_data_through_date']=day
        target['publisher_file_data_through_date']=through
        target['inventory_scope']='Games present in the published extract; complete NBA schedule not independently verified.'
    return days


class PublishedNBAData:
    def __init__(self,client,now):
        self.client,self.now=client,now;self.scope=load_scope();self.assets={};self.days=None
        year=now.year-(now.month<7);self.season=f'{year}-{str(year+1)[-2:]}';self.end_year=year+1

    async def release(self,tag):
        if tag not in self.assets:
            url=f'https://api.github.com/repos/{REPO}/releases/tags/{tag}'
            response=await self.client.get(url);response.raise_for_status()
            self.assets[tag]={asset['name']:asset for asset in response.json().get('assets',[])}
        return self.assets[tag]

    async def asset(self,tag,name):
        url=f'https://github.com/{REPO}/releases/download/{tag}/{name}'
        response=await self.client.get(url);response.raise_for_status()
        if len(response.content)>8_000_000:raise ValueError('Published extract exceeds size limit.')
        modified=email.utils.parsedate_to_datetime(response.headers.get('last-modified',''))
        if modified is None or modified.tzinfo is None:
            raise ValueError('Published extract has no verifiable modification timestamp.')
        meta={'browser_download_url':url,'updated_at':modified.astimezone(dt.timezone.utc).isoformat()}
        return response.content,meta

    async def day(self,day):
        if self.days is None:
            content,meta=await self.asset(CSV_TAG,f'player_box_{self.end_year}.csv')
            self.days=existing_game_snapshots(content,self.scope,meta['updated_at'],meta['browser_download_url'])
        return self.days.get(day)

    async def tracking(self,output):
        import pyarrow.parquet as pq
        from advanced_refresh import normalize_records,preserve_sparse_fields
        from master import clear_unverified_tracking_coverage
        pair_scope=json.loads(Path(__file__).with_name('matchup_refresh_scope.json').read_text())
        assets=[('Player totals','player_stats_base'),('Team totals','team_stats_base'),('Team defense','team_stats_opponent'),
                ('Passing','player_tracking_passing'),('Rebounding','player_tracking_rebounding'),
                ('Possessions','player_tracking_possessions'),('Rim','player_tracking_defense')]
        target=Path(output)/'existing_tracking.json'
        old=json.loads(target.read_text()) if target.exists() else {}
        prior={(r.get('season'),r.get('entity'),r.get('category')):r for r in old.get('records',[]) if r.get('season')==self.season}
        records=[];coverage=[];published=[]
        for category,prefix in assets:
            try:
                content,meta=await self.asset(STATS_TAG,f'{prefix}_{self.end_year}.parquet')
                raw=pq.read_table(io.BytesIO(content)).to_pylist();converted=[]
                for row in raw:
                    if str(row.get('season'))!=str(self.end_year) or row.get('season_type')!='Regular Season' or row.get('per_mode')!='Totals':continue
                    item={key.upper().replace('FG3_M','FG3M').replace('FG3_A','FG3A'):value for key,value in row.items()}
                    converted.append(item)
                normalized=normalize_records(converted,category,self.scope,self.season,pair_scope['pairs'],pair_scope.get('team_ids'))
                if not normalized:raise ValueError('No existing metric values returned.')
                for record in normalized:
                    record.update(source=meta['browser_download_url'],published_at=meta['updated_at'],
                        source_published_at=meta['updated_at'],publication_time_kind='publisher_modified',
                        retrieved_at=self.now.isoformat(),publication_source='SportsDataverse published NBA Stats extract')
                    tracking_coverage(record,prior.get((record['season'],record['entity'],record['category'])))
                    record.update(preserve_sparse_fields(record,prior.get((record['season'],record['entity'],record['category']))))
                dates=[r.get('data_through_date') for r in normalized]
                through=min(dates) if dates and all(dates) else None
                records.extend(normalized);published.append(meta['updated_at'])
                coverage.append({'category':category,'status':'collected','records':len(normalized),
                                  'source_published_at':meta['updated_at'],'source_data_through_date':through,
                                  'source_content_sha256':hashlib.sha256(content).hexdigest(),
                                  'coverage_note':'Covered dates are retained only for unchanged source statistics; changed/new values have unknown covered-game dates.'})
            except Exception as error:
                coverage.append({'category':category,'status':'unavailable','error_type':type(error).__name__})
        # The current public mirror does not supply these populations. Prior dated
        # records remain unchanged; neither box production nor rim FG substitutes.
        coverage.extend({'category':category,'status':'unavailable','reason':'No current-season extract in this publication.'}
                        for category in ('Overall','Named pair','Less Than 6Ft','Hustle'))
        imported_keys={(r['season'],r['entity'],r['category']) for r in records}
        records.extend(clear_unverified_tracking_coverage(dict(r)) for r in old.get('records',[])
                       if r.get('season')==self.season and (r.get('season'),r.get('entity'),r.get('category')) not in imported_keys)
        if not published:return False
        known=[c.get('source_data_through_date') for c in coverage if c['status']=='collected']
        through=min(known) if known and all(known) else None
        document={'schema_version':1,'scope_revision':self.scope['revision'],'season':self.season,
            'last_attempt':self.now.isoformat(),'source_published_at':max(published),'records':records,'coverage':coverage,
            'direct_tracking_attempted_at':old.get('direct_tracking_attempted_at'),
            'source_data_through_date':through,'policy':'Only existing workbook entities/metrics; source publication lag is preserved.'}
        target.write_text(json.dumps(document,indent=2,sort_keys=True)+'\n')
        return True
