"""Historical nonempty day inventory corroborated by independent NBA results.

The calendar supplies expected games, never final status or current tipoff times.
Every expected game must also have coherent two-team NBA results and an exact
independently validated ESPN full box. This cannot verify an off day or a live
schedule, and does not certify unannounced Cup/TBD games.
"""
from __future__ import annotations

from collections import defaultdict
import datetime as dt
import hashlib
import io
import math
import re
from zoneinfo import ZoneInfo

from feeds import TEAM_WORDS, canonical_game, canonical_team

ROOT = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
EASTERN = ZoneInfo('America/New_York')


def _instant(value):
    stamp = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Published inventory timestamps need a timezone.')
    return stamp


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0 and float(value).is_integer()


def _source(meta, tag, filename, checked_at):
    expected = ROOT + tag + '/' + filename
    if (not isinstance(meta, dict) or meta.get('source') != expected
            or not re.fullmatch(r'[0-9a-f]{64}', str(meta.get('source_content_sha256') or ''))
            or _instant(meta.get('source_published_at')) > checked_at):
        raise ValueError('Historical inventory source identity or publication is invalid.')
    return dict(meta)


def historical_inventory(calendar, results, snapshot, day, *, checked_at, calendar_source, results_source):
    """Return an independently corroborated historical inventory or fail closed."""
    target = dt.date.fromisoformat(day)
    if checked_at.tzinfo is None or day != target.isoformat() or target >= checked_at.astimezone(EASTERN).date():
        raise ValueError('Published inventory is limited to dates strictly before today Eastern.')
    year = target.year - (target.month < 7)
    end = year + 1
    calendar_source = _source(calendar_source, 'nba_stats_schedules', f'nba_schedule_{end}.parquet', checked_at)
    results_source = _source(results_source, 'nba_stats_player_game_logs', f'player_game_logs_{end}.parquet', checked_at)
    if not isinstance(calendar, list) or not isinstance(results, list) or len(calendar) > 5000 or len(results) > 10000:
        raise ValueError('Historical NBA inventory source exceeds its row bound.')
    dated = [r for r in calendar if str(r.get('game_date')) == day]
    if not 1 <= len(dated) <= 15:
        raise ValueError('An explicit nonempty historical calendar date is required; empty/TBD days are not verified.')
    expected = {}
    for row in dated:
        provider = str(row.get('game_id') or '')
        home, away = canonical_team(row.get('home_team_abbreviation')), canonical_team(row.get('away_team_abbreviation'))
        if (str(row.get('season')) != str(end) or row.get('season_type') != 'regular-season'
                or not re.fullmatch('002' + str(year)[-2:] + r'\d{5}', provider)
                or home not in TEAM_WORDS or away not in TEAM_WORDS or home == away or provider in expected
                or str(row.get('home_team_id')) == str(row.get('away_team_id'))
                or not str(row.get('home_team_id') or '').isdigit() or not str(row.get('away_team_id') or '').isdigit()):
            raise ValueError('Historical calendar has duplicate, unsupported phase, TBD or invalid game identities.')
        expected[provider] = {'id': canonical_game(day,home,away), 'date':day,
            'home_team':home,'away_team':away,'home_team_id':str(row['home_team_id']),
            'away_team_id':str(row['away_team_id']), 'provider_game_id':provider}
    groups = defaultdict(list)
    for row in results:
        if str(row.get('game_date')) != day:
            continue
        if row.get('season_type') != 'regular-season':
            # An explicitly nonregular event cannot establish this statistical
            # day's complete nonempty NBA inventory, including Cup/TBD days.
            raise ValueError('Historical result date contains an unsupported statistical phase.')
        groups[str(row.get('game_id') or '')].append(row)
    if set(groups) != set(expected):
        raise ValueError('Historical NBA final results do not exactly match the published calendar inventory.')
    games = []
    for provider, game in sorted(expected.items()):
        rows = groups[provider]
        if len(rows) != 2:
            raise ValueError('Each NBA final needs exactly two unique team result views.')
        sides = {}
        for row in rows:
            own = canonical_team(row.get('team_abbreviation'))
            match = re.fullmatch(r'([A-Z]{2,3})\s+(vs\.?|@)\s+([A-Z]{2,3})', str(row.get('matchup') or ''))
            if not match or canonical_team(match[1]) != own or str(row.get('season')) != str(end):
                raise ValueError('NBA result date, season or matchup header is invalid.')
            side = 'away' if match[2] == '@' else 'home'
            opposite = 'home' if side == 'away' else 'away'
            if (side in sides or own != game[side+'_team'] or canonical_team(match[3]) != game[opposite+'_team']
                    or str(row.get('team_id')) != game[side+'_team_id']):
                raise ValueError('NBA result participant, ID or home/away header conflicts with the calendar.')
            counts = ('pts','fgm','fga','fg3m','fg3a','ftm','fta')
            if (not all(_number(row.get(k)) for k in counts)
                    or row['fgm'] > row['fga'] or row['fg3m'] > row['fg3a'] or row['fg3m'] > row['fgm']
                    or row['ftm'] > row['fta'] or row['pts'] != 2*row['fgm']+row['fg3m']+row['ftm']):
                raise ValueError('NBA team points and scoring components do not establish a coherent final.')
            sides[side] = row
        home, away = sides['home'], sides['away']
        hs, aws = home['pts'], away['pts']
        if (hs == aws or (home.get('wl'),away.get('wl')) != (('W','L') if hs>aws else ('L','W'))
                or any(r.get('plus_minus') is not None and r['plus_minus'] != score
                       for r,score in [(home,hs-aws),(away,aws-hs)])):
            raise ValueError('NBA final outcomes or team plus-minus conflict.')
        if _instant(results_source['source_published_at']).date() < target:
            raise ValueError('NBA final results claim a game after their publication date.')
        games.append({k:v for k,v in {**game,'status':'final','home_score':hs,'away_score':aws,
            'source':results_source['source']}.items() if k not in {'home_team_id','away_team_id'}})
    if (not isinstance(snapshot,dict) or snapshot.get('date') != day or snapshot.get('source') != 'espn'
            or snapshot.get('upstream_full_game_validated') is not True or snapshot.get('complete') is not True
            or snapshot.get('pending_game_ids') or snapshot.get('errors') or snapshot.get('expected_games') != len(games)):
        raise ValueError('Independent ESPN full boxes are incomplete or unvalidated.')
    box_source = _source({'source':snapshot.get('source_url'),
        'source_published_at':snapshot.get('published_at'),'source_content_sha256':snapshot.get('source_content_sha256')},
        'espn_nba_player_boxscores',f'player_box_{end}.parquet',checked_at)
    actual = {g['id']:g for g in snapshot.get('games',[])}
    if len(actual) != len(snapshot.get('games',[])) or set(actual) != {g['id'] for g in games}:
        raise ValueError('ESPN full box events do not exactly match the independent NBA inventory.')
    for game in games:
        other = actual[game['id']]
        if any(other.get(k) != game[k] for k in ('date','home_team','away_team','status','home_score','away_score')):
            raise ValueError('ESPN and NBA final identities or scores disagree.')
        if _instant(other.get('start_time')).astimezone(EASTERN).date() != target:
            raise ValueError('ESPN event clock does not match the NBA calendar date in Eastern time.')
    return {'inventory_verified':True,'inventory_verified_at':checked_at.isoformat(),
        'inventory_verification_source':results_source['source'],
        'inventory_game_ids':sorted(g['id'] for g in games),'inventory_off_day_verified':False,
        'inventory_sources':[calendar_source,results_source,box_source],
        'inventory_scope':'Nonempty historical NBA calendar date, corroborated by exact two-team NBA final results and independent complete ESPN boxes; no live schedule, off-day or unannounced Cup/TBD certification.',
        'inventory_date':day,'games':games}


def _parquet_rows(content):
    import pyarrow.parquet as pq
    table=pq.read_table(io.BytesIO(content))
    if table.num_rows>10000:
        raise ValueError('Published NBA inventory source exceeds its row bound.')
    return table.to_pylist()


async def fetch_historical_inventory(mirror, day, snapshot, *, checked_at=None):
    """Fetch fixed independent public NBA assets, verifying release SHA256."""
    checked_at = checked_at or dt.datetime.now(dt.timezone.utc)
    target = dt.date.fromisoformat(day)
    if target >= checked_at.astimezone(EASTERN).date():
        raise ValueError('Published historical inventory cannot verify today or a future day.')
    end = target.year + (target.month >= 7)
    if end != mirror.end_year:
        raise ValueError('Historical fallback is outside the collected current season.')
    cache = getattr(mirror,'_published_inventory_assets',{})
    values=[]
    for tag,name in [('nba_stats_schedules',f'nba_schedule_{end}.parquet'),
                     ('nba_stats_player_game_logs',f'player_game_logs_{end}.parquet')]:
        if name not in cache:
            asset = (await mirror.release(tag)).get(name)
            url = ROOT+tag+'/'+name
            if (not isinstance(asset,dict) or asset.get('browser_download_url') != url
                    or type(asset.get('size')) is not int or not 0 < asset['size'] <= 8_000_000
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}',str(asset.get('digest') or ''))):
                raise ValueError('Published NBA inventory release asset is invalid.')
            content,meta = await mirror.asset(tag,name)
            digest = hashlib.sha256(content).hexdigest()
            if len(content) != asset['size'] or digest != asset['digest'][7:] or meta.get('browser_download_url') != url:
                raise ValueError('Published NBA inventory source checksum or identity differs.')
            cache[name]=(_parquet_rows(content),{'source':url,'source_published_at':asset['updated_at'],
                                          'source_content_sha256':digest})
        values.append(cache[name])
    mirror._published_inventory_assets=cache
    return historical_inventory(values[0][0],values[1][0],snapshot,day,checked_at=checked_at,
        calendar_source=values[0][1],results_source=values[1][1])
