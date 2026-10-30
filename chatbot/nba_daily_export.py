"""Public NBA box-score snapshots for the user's GitHub data relay.

Runs on a GitHub-hosted runner, never calls a model or places entries. A refused
publisher is not hammered; another independently published feed may be tried.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import httpx

from feeds import ESPN, parse_game, parse_boxscore
from nba_cdn import SCHEDULE_URL, BOX_URL, parse_game as nba_game, parse_players, day_value

EASTERN=ZoneInfo('America/New_York')


def validate_final(game, rows):
    from feeds import canonical_player
    if game.get('status')!='final':
        raise ValueError('Only final box scores can be validated as final.')
    seen=set()
    for row in rows:
        key=row.get('player_id') or canonical_player(row.get('player'))
        if key in seen or not row.get('player') or row.get('game_id')!=game['id']:
            raise ValueError('Missing, duplicate or mismatched box-score player.')
        seen.add(key)
        if row.get('team') not in {game['home_team'],game['away_team']}:
            raise ValueError('Box score contains a different team.')
        if row.get('appearance_state')=='Played' and (row.get('pts') is None or row.get('minutes') is None or row['minutes']<=0):
            raise ValueError('Final played row is missing points/minutes.')
    for side in ('home','away'):
        played=[r for r in rows if r.get('team')==game[side+'_team'] and r.get('appearance_state')=='Played']
        if len(played)<5:
            raise ValueError('Final team box score has fewer than five participants.')
        score=game.get(side+'_score')
        if score is not None and abs(sum(float(r['pts']) for r in played)-float(score))>.01:
            raise ValueError('Final player points do not reconcile to the team score.')
    return rows


class PublicCollector:
    def __init__(self, client, now=None):
        self.client=client; self.now=now or dt.datetime.now(dt.timezone.utc)
        self.refused=set(); self.last_request=0; self.schedule=None

    async def request(self,url):
        host=httpx.URL(url).host
        if host in self.refused:
            raise RuntimeError('Publisher unavailable during this collection run.')
        await asyncio.sleep(max(0,.35-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        try:
            response=await self.client.get(url)
        except httpx.TimeoutException:
            self.refused.add(host)
            raise
        if response.status_code in {401,403,429}:
            self.refused.add(host)
        response.raise_for_status()
        if len(response.content)>8_000_000:
            raise ValueError('Publisher response too large.')
        return response.json()

    async def official(self,day):
        if self.schedule is None:
            self.schedule=await self.request(SCHEDULE_URL)
        league=self.schedule.get('leagueSchedule') or {}
        dates=league.get('gameDates')
        if not isinstance(dates,list) or not dates:
            raise ValueError('Official NBA schedule is unavailable.')
        season=league.get('seasonYear')
        games=[]
        for block in dates:
            if day_value(block.get('gameDate'))!=day:
                continue
            for raw in block.get('games',[]):
                game=nba_game(raw,SCHEDULE_URL,block.get('gameDate'),season)
                if game['date']==day:
                    games.append(game)
        if not games:
            # Establish an off day only within a recognized current season calendar.
            if season!=f'{int(day[:4])-(int(day[5:7])<7)}-{str(int(day[:4])-(int(day[5:7])<7)+1)[-2:]}':
                raise ValueError('Official NBA calendar is outside the requested season.')
        return await self.boxes(day,games,'nba')

    async def espn(self,day):
        document=await self.request(ESPN+'scoreboard?dates='+day.replace('-',''))
        if not isinstance(document.get('events'),list):
            raise ValueError('ESPN scoreboard is missing its event list.')
        games=[parse_game(raw) for raw in document['events']]
        if any(game['date']!=day for game in games):
            raise ValueError('Scoreboard event date differs from the requested day.')
        return await self.boxes(day,games,'espn')

    async def boxes(self,day,games,source):
        published=[]; observations=[]; pending=[]; errors=[]
        for game in games:
            if game['status']=='final':
                try:
                    if source=='nba':
                        document=await self.request(BOX_URL.format(game['provider_game_id']))
                        fresh=nba_game(document['game'],BOX_URL.format(game['provider_game_id']))
                        if fresh['id']!=game['id'] or fresh['status']!='final':
                            raise ValueError('Final box score identity/status mismatch.')
                        game={**game,**fresh}; rows=parse_players(document,game)
                    else:
                        document=await self.request(ESPN+'summary?event='+game['provider_game_id'])
                        rows=parse_boxscore(document,game)
                    validate_final(game,rows)
                    published.append(game); observations.extend(rows)
                except Exception as error:
                    pending.append(game['id'])
                    errors.append({'game_id':game['id'],'error_type':type(error).__name__})
            else:
                published.append(game)
                if game['status'] not in {'postponed','canceled'}:
                    pending.append(game['id'])
        return {'schema_version':1,'date':day,'published_at':self.now.isoformat(),
                'source':source,'expected_games':len(games),'games':published,'player_games':observations,
                'pending_game_ids':pending,'errors':errors,'complete':not pending and not errors}

    async def collect(self,day):
        failures=[]
        for name in ('official','espn'):
            try:
                result=await getattr(self,name)(day)
                if result['games'] or not result['expected_games']:
                    from dataset_refresh import load_scope, select_existing_rows
                    scope=load_scope()
                    result['player_games']=select_existing_rows(result['player_games'],scope)
                    result['scope_revision']=scope['revision']
                    result['scope_policy']='Existing workbook players and observation fields only; games are source evidence, not new named matchup populations.'
                    result['upstream_full_game_validated']=True
                    return result
                failures.append({'source':name,'error_type':'IncompleteBoxScores'})
            except Exception as error:
                failures.append({'source':name,'error_type':type(error).__name__,
                                 'http_status':getattr(getattr(error,'response',None),'status_code',None)})
        return {'schema_version':1,'date':day,'published_at':self.now.isoformat(),
                'source':None,'expected_games':None,'games':[],'player_games':[],
                'pending_game_ids':[],'errors':failures,'complete':False}


async def main_async(args):
    today=dt.datetime.now(EASTERN).date()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    failures=0
    async with httpx.AsyncClient(timeout=25,follow_redirects=True,
            headers={'User-Agent':'NBAPPBOT/3.0 personal research'}) as client:
        collector=PublicCollector(client)
        for offset in range(args.days-1,-1,-1):
            day=(today-dt.timedelta(days=offset)).isoformat()
            path=output/(day+'.json')
            old=json.loads(path.read_text()) if path.exists() else {}
            # Retry incomplete games and revisit the last two completed days for
            # stat corrections. Older complete snapshots avoid repeated requests.
            if old.get('complete') and offset>2:
                continue
            result=await collector.collect(day)
            if result['source'] is None:
                failures+=1
                print(day,'sources unavailable; preserving any prior successful snapshot',flush=True)
                print('Source checks:',json.dumps(result.get('errors',[])),flush=True)
                continue
            path.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
            print(day,result['source'],'games',len(result['games']),'players',len(result['player_games']),
                  'complete',result['complete'],flush=True)
        from advanced_refresh import collect_tracking
        advanced_ok=await collect_tracking(collector,output)
        print('Existing-statistics source collected:',advanced_ok,flush=True)
    if failures:
        print('Some daily dates could not be collected; no success was fabricated.',flush=True)
    return 1 if failures==args.days and not advanced_ok else 0


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='data/live')
    parser.add_argument('--days',type=int,default=7)
    args=parser.parse_args()
    if not 1<=args.days<=14:parser.error('--days must be 1–14')
    sys.exit(asyncio.run(main_async(args)))
