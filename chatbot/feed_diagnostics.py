"""One-shot public feed checks. Never prints secrets, proxy settings or private URLs."""
from __future__ import annotations
import asyncio
import html
import json
import re

import httpx

PUBLIC_CHECKS = [
    ('espn_scoreboard', 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard'),
    ('nba_cdn_scoreboard', 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'),
    ('cbs_nba_rss', 'https://www.cbssports.com/rss/headlines/nba'),
    ('rotowire_nba_rss', 'https://www.rotowire.com/rss/news.php?sport=NBA'),
    ('sny_knicks_rss', 'https://sny.tv/knicks-feed'),
    ('esny_knicks_rss', 'https://elitesportsny.com/category/nyc-teams/e-new-york-knicks/feed'),
]
SAFE_HEADERS = ('content-type', 'server', 'via', 'x-cache', 'retry-after',
                'x-openai-block-reason', 'x-blocked-reason')


def summarize(name, response, body):
    status = response.status_code
    report = {'source': name, 'host': response.request.url.host, 'status': status,
              'headers': {k: response.headers[k][:160] for k in SAFE_HEADERS if k in response.headers}}
    content_type = response.headers.get('content-type', '').lower()
    preview = body.decode('utf-8', 'replace')
    if status >= 400:
        title = re.search(r'<title[^>]*>(.*?)</title>', preview, re.I | re.S)
        message = title.group(1) if title else preview[:200]
        message = html.unescape(re.sub(r'<[^>]*>', ' ', message))
        report['error_summary'] = re.sub(r'\s+', ' ', message).strip()[:180]
        report['meaning'] = ('Access refused by a responding server or proxy; this does not identify the cause.'
                             if status == 403 else 'Request failed; inspect status and headers.')
    elif 'json' in content_type:
        report['body_kind'] = 'JSON'
    elif 'xml' in content_type or preview.lstrip().startswith(('<?xml', '<rss', '<feed')):
        report['body_kind'] = 'XML feed'
    else:
        report['body_kind'] = 'Unexpected content type; do not count this as usable feed data.'
    return report


async def diagnose_feeds(client=None):
    async def check(session, name, url):
        try:
            async with session.stream('GET', url) as response:
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part[:max(0, 4096 - len(body))])
                    if len(body) >= 4096:
                        break
                return summarize(name, response, bytes(body))
        except Exception as error:
            return {'source': name, 'error': type(error).__name__,
                    'meaning': 'Connection failed. No environment variables or credentials are included in this report.'}
    if client is not None:
        return await asyncio.gather(*(check(client, name, url) for name, url in PUBLIC_CHECKS))
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={'User-Agent': 'NBAPPBOT/2.0 personal research'}) as session:
        return await asyncio.gather(*(check(session, name, url) for name, url in PUBLIC_CHECKS))


if __name__ == '__main__':
    print(json.dumps(asyncio.run(diagnose_feeds()), indent=2))
