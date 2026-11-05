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
from zoneinfo import ZoneInfo

import httpx

from dataset_refresh import load_scope,select_existing_rows,expand_current_player_scope,validate_published_scope
from feeds import canonical_game,canonical_player,canonical_team,numeric
from nba_daily_export import validate_final
from game_phase import regular_phase_exclusion

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


def existing_game_snapshots(content,scope,published_at,source,discovered_scope=None):
    grouped={}
    for raw in csv.DictReader(io.StringIO(content.decode('utf-8-sig'))):
        if str(raw.get('season_type'))!='2' or regular_phase_exclusion(raw):continue
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
            'provider_player_id':raw.get('athlete_id'),'espn_player_id':raw.get('athlete_id'),'team':team,'opponent':opponent,'date':day,
            'season':f'{year-1}-{str(year)[-2:]}','season_type':'Regular Season','appearance_state':state,
            'minutes':minutes if state!='DNP' else None,'source':source,
            'minute_precision':'Publisher numeric minutes; zero/absent minutes with no DNP flag remain unknown.'}
        for column,field in [('points','pts'),('rebounds','reb'),('assists','ast'),('field_goals_made','fgm'),
            ('field_goals_attempted','fga'),('three_point_field_goals_made','tpm'),('three_point_field_goals_attempted','tpa'),
            ('free_throws_made','ftm'),('free_throws_attempted','fta'),('offensive_rebounds','oreb'),
            ('defensive_rebounds','dreb'),('steals','stl'),('blocks','blk'),('turnovers','tov'),('fouls','fouls'),('plus_minus','plus_minus')]:
            row[field]=numeric(raw.get(column)) if state!='DNP' else None
        starter=str(raw.get('starter','')).lower()
        if starter in {'true','false'}:row['starter']=int(starter=='true')
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
            target['games'].append(group['game']);target['player_games'].extend(group['rows'])
        except ValueError:
            target['pending_game_ids'].append(game_id);target['errors'].append({'game_id':game_id,'error_type':'SourceValidationError'})
    if discovered_scope is not None:
        scope=expand_current_player_scope(scope,[row for target in days.values() for row in target['player_games']])
        discovered_scope.update(scope)
    through=max(days) if days else None
    for day,target in days.items():
        target['player_games']=select_existing_rows(target['player_games'],scope)
        target['scope_revision']=scope['revision']
        target['complete']=not target['pending_game_ids'];target['source_data_through_date']=day
        target['publisher_file_data_through_date']=through
        target['inventory_scope']='Games present in the published extract; complete NBA schedule not independently verified.'
    return days


class PublishedNBAData:
    def __init__(self,client,now):
        self.client,self.now=client,now;self.scope=load_scope();self.bundled_scope=self.scope;self.assets={};self.days=None;self.scope_document=None
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
            # Full-season CSV files grow beyond the bounded response size.
            # Parquet is already a collector dependency and keeps transfer small.
            content,meta=await self.asset(CSV_TAG,f'player_box_{self.end_year}.parquet')
            import pyarrow.parquet as pq
            table=pq.read_table(io.BytesIO(content))
            if table.num_rows>100_000:
                raise ValueError('Published season extract exceeds its row bound.')
            raw_rows=table.to_pylist()
            today=self.now.astimezone(ZoneInfo('America/New_York')).date()
            if any(str(row.get('season'))!=str(self.end_year) or
                   not dt.date(self.end_year-1,7,1)<=dt.date.fromisoformat(str(row.get('game_date'))[:10])<=today
                   for row in raw_rows):
                raise ValueError('Published current-season extract has an out-of-season or future observation.')
            buffer=io.StringIO()
            writer=csv.DictWriter(buffer,fieldnames=table.column_names);writer.writeheader()
            writer.writerows(raw_rows)
            discovered={}
            days=existing_game_snapshots(buffer.getvalue().encode(),self.scope,meta['updated_at'],meta['browser_download_url'],discovered)
            effective=discovered or self.scope
            scope_document={'schema_version':1,'base_revision':self.bundled_scope['revision'],
                'season':self.season,'scope':effective,'upstream_full_game_validated':True,
                'source_url':meta['browser_download_url'],'source_published_at':meta['updated_at'],
                'source_checked_at':self.now.isoformat(),'source_data_through_date':max(days) if days else None,
                'source_content_sha256':hashlib.sha256(content).hexdigest(),
                'validated_player_rows':sum(len(item['player_games']) for item in days.values()),
                'rejected_game_ids':sorted({identity for item in days.values() for identity in item.get('pending_game_ids',[])}),
                'coverage_note':'Published current-season game inventory; roster additions use validated complete boxes, not a claim of complete league history.'}
            validate_published_scope(scope_document,self.bundled_scope,self.scope,self.now)
            self.scope,self.days,self.scope_document=effective,days,scope_document
        return self.days.get(day)

    def configure_scope(self, output):
        target=Path(output)/'existing_dataset_scope.json'
        if target.exists():
            try:
                self.scope=validate_published_scope(json.loads(target.read_text()),self.bundled_scope,
                                                    self.bundled_scope,self.now)
            except (ValueError,TypeError,KeyError):
                # A changed release/season starts from its bundled authorized scope.
                self.scope=self.bundled_scope

    def publish_scope(self, output):
        if self.scope_document is None:return
        target=Path(output)/'existing_dataset_scope.json'
        temporary=target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.scope_document,indent=2,sort_keys=True)+'\n')
        temporary.replace(target)

    async def tracking(self,output):
        import pyarrow.parquet as pq
        from advanced_refresh import normalize_records,preserve_sparse_fields,direct_tracking_status
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
        # Separate public releases carry actual hustle and player-vs-player
        # tracking. Do not substitute team defense or ordinary box production.
        try:
            from tracking_publications import normalize_hustle
            content,meta=await self.asset('nba_stats_hustle',
                f'leaguehustlestatsplayer_regular-season_totals_{self.end_year}.parquet')
            raw=pq.read_table(io.BytesIO(content)).to_pylist()
            normalized=normalize_hustle(raw,self.scope,self.season)
            if not normalized:raise ValueError('No authorized cumulative hustle values returned.')
            for record in normalized:
                record.update(source=meta['browser_download_url'],published_at=meta['updated_at'],
                    source_published_at=meta['updated_at'],publication_time_kind='publisher_modified',
                    retrieved_at=self.now.isoformat(),publication_source='SportsDataverse published NBA hustle extract',
                    source_content_sha256=hashlib.sha256(content).hexdigest())
                tracking_coverage(record,prior.get((record['season'],record['entity'],record['category'])))
                record.update(preserve_sparse_fields(record,prior.get((record['season'],record['entity'],record['category']))))
            dates=[r.get('data_through_date') for r in normalized]
            records.extend(normalized);published.append(meta['updated_at'])
            coverage.append({'category':'Hustle','status':'collected','records':len(normalized),
                'source_published_at':meta['updated_at'],
                'source_data_through_date':min(dates) if dates and all(dates) else None,
                'source_content_sha256':hashlib.sha256(content).hexdigest(),
                'fields':sorted(set().union(*(set(r['fields']) for r in normalized))),
                'coverage_note':'Cumulative hustle fields use source G; covered-game dates are not reported.'})
        except Exception as error:
            coverage.append({'category':'Hustle','status':'unavailable','error_type':type(error).__name__})
        try:
            from tracking_publications import game_date_inventory,measured_matchups
            source_content,source_meta=await self.asset('nba_stats_game_matchups',f'game_matchups_{self.end_year}.parquet')
            schedule_content,schedule_meta=await self.asset('nba_stats_schedules',f'nba_schedule_{self.end_year}.parquet')
            log_content,log_meta=await self.asset('nba_stats_player_game_logs',f'player_game_logs_{self.end_year}.parquet')
            mapping=[{'source':meta['browser_download_url'],'source_published_at':meta['updated_at'],
                      'source_content_sha256':hashlib.sha256(content).hexdigest()}
                     for content,meta in [(schedule_content,schedule_meta),(log_content,log_meta)]]
            inventory,excluded=game_date_inventory(pq.read_table(io.BytesIO(schedule_content)).to_pylist(),
                pq.read_table(io.BytesIO(log_content)).to_pylist(),self.season,mapping_sources=mapping)
            alias_path=Path(__file__).with_name('matchup_history_targets.json')
            aliases=json.loads(alias_path.read_text()).get('players',[]) if alias_path.exists() else []
            normalized,measured,report=measured_matchups(pq.read_table(io.BytesIO(source_content)).to_pylist(),
                inventory,self.scope,pair_scope,self.season,source=source_meta['browser_download_url'],
                published_at=source_meta['updated_at'],source_hash=hashlib.sha256(source_content).hexdigest(),
                now=self.now,alias_rows=aliases)
            if not normalized:raise ValueError('No authorized measured player-pair observations returned.')
            for record in normalized:
                record.update(preserve_sparse_fields(record,prior.get((record['season'],record['entity'],record['category']))))
            records.extend(normalized);published.append(source_meta['updated_at'])
            dates=[r['data_through_date'] for r in normalized]
            coverage.append({'category':'Named pair','status':'collected','records':len(normalized),
                'source_published_at':source_meta['updated_at'],'source_data_through_date':min(dates),
                'source_content_sha256':hashlib.sha256(source_content).hexdigest(),
                'measured_pair_games':len(measured),'first_observed_game_date':report['first_observed_game_date'],
                'last_observed_game_date':report['last_observed_game_date'],
                'date_mapping_exclusions':excluded,'held_rows':report['held_rows'],
                'coverage_note':'Actual named pair observations. Each row date is its last observed measured game; full season/lifetime pair coverage is not asserted.'})
            # The historical relay builder can reuse this exact current-season
            # source without downloading it again during the same collection.
            self.current_measured_tracking={'source_content':source_content,'source_meta':source_meta,
                'schedule_content':schedule_content,'schedule_meta':schedule_meta,
                'team_log_content':log_content,'team_log_meta':log_meta,
                'records':normalized,'measured':measured,'report':report}
        except Exception as error:
            coverage.append({'category':'Named pair','status':'unavailable','error_type':type(error).__name__})
        # These two closest-defender populations are not contained in the
        # publications above. Measured-matchup FGA is a different population.
        coverage.extend({'category':category,'status':'unavailable',
                         'reason':'No current-season closest-defender extract in the supported publications.'}
                        for category in ('Overall','Less Than 6Ft'))
        imported_keys={(r['season'],r['entity'],r['category']) for r in records}
        records.extend(clear_unverified_tracking_coverage(dict(r)) for r in old.get('records',[])
                       if r.get('season')==self.season and (r.get('season'),r.get('entity'),r.get('category')) not in imported_keys)
        if not published:return False
        known=[c.get('source_data_through_date') for c in coverage if c['status']=='collected']
        through=min(known) if known and all(known) else None
        document={'schema_version':1,'scope_revision':self.scope['revision'],'season':self.season,
            'last_attempt':self.now.isoformat(),'source_published_at':max(published),'records':records,'coverage':coverage,
            'direct_tracking_attempted_at':old.get('direct_tracking_attempted_at'),
            'direct_tracking':direct_tracking_status(old,self.now),
            'source_data_through_date':through,'policy':'Authorized NBA player/team populations and supported metrics; source publication lag is preserved.'}
        target.write_text(json.dumps(document,indent=2,sort_keys=True)+'\n')
        try:
            from historical_matchup_refresh import collect_historical_matchups
            history=await collect_historical_matchups(self,Path(output)/'historical_matchups',now=self.now,
                current_data=getattr(self,'current_measured_tracking',None),
                cache_directory=Path(output).parent/'.nba-tracking-cache')
        except Exception as error:
            # Existing measured archives and current working categories survive
            # an unavailable historical source or an unsupported old schema.
            history={'phase':'unavailable','error_type':type(error).__name__,
                     'previous_publication_retained':(Path(output)/'historical_matchups'/'historical_matchups_manifest.json').exists()}
        document['historical_pair_history']=history
        target.write_text(json.dumps(document,indent=2,sort_keys=True)+'\n')
        print('Actual measured-pair archive:',json.dumps(history),flush=True)
        return True
