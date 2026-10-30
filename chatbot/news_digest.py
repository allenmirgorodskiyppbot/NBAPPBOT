"""Fast, read-only summaries of collected NBA reports without model inference.

Categories describe the article, not a player's medical/playing status. Reported
wording and source timestamps stay evidence; nothing here confirms an injury,
transaction, participation decision, or complete official coverage.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import re
from urllib.parse import urlsplit

MAX_CONTEXT_CHARS = 6000
MAX_MESSAGE_CHARS = 3500
COVERAGE = ('Collected reports only; publisher-reported or manual and not independently verified. '
            'This is not a complete official NBA injury report. Missing reports do not mean a player is healthy or available.')

_INJURY = re.compile(r'\b(injur\w*|surgery|surgical|concussion\w*|fractur\w*|sprain\w*|'
                     r'soreness|illness|rehab(?:ilitation)?|torn|tore|tendon|ligament)\b', re.I)
_AVAILABILITY = re.compile(
    r'\b(questionable|doubtful|probable|availability|inactive|suspend(?:ed|sion)?|'
    r'ruled\s+out|out\s+(?:with|for|indefinitely)|day[- ]to[- ]day|game[- ]time\s+decision|'
    r'minutes?\s+(?:restriction|limit)|load\s+management|starting\s+lineup|benched|'
    r'return(?:s|ing|ed)?\s+(?:from|to\s+(?:action|the\s+lineup))|will\s+(?:rest|miss))\b|'
    r':\s*(?:out|available|active|inactive)\b', re.I)
_TRADE = re.compile(r'\b(trad(?:e|es|ed|ing)|waiv(?:e|ed|er|ers)|acquir(?:e|es|ed)|'
                    r'roster\s+(?:move|change)|free[- ]agent|sign(?:s|ed|ing)\s+(?:with|a\s+contract)|'
                    r'contract\s+(?:extension|agreement)|released\s+by)\b', re.I)
# Deliberately avoid bare "bonus", "betting", "return" or "out": those can
# appear in genuine contract/medical reports. Explicit offers and promotional
# landing pages are excluded, while article-body affiliate boilerplate is removed.
_PROMOTION = re.compile(
    r'\bpromo(?:tional)?[\s_-]*codes?\b|\bcoupon(?:[\s_-]*codes?)?\b|'
    r'\b(?:sportsbook|casino|betting|deposit|welcome|sign[- ]?up)[\s_-]+(?:bonus|offer|promotion)s?\b|'
    r'\b(?:bonus|free)[\s_-]+bets?\b|\bbet[\s_-]+credits?\b|\bno[- ]deposit\b|'
    r'\brisk[- ]free[\s_-]+bets?\b|\b(?:claim|unlock)[\s_-]+(?:your[\s_-]+)?bonus\b|'
    r'\buse[\s_-]+(?:the[\s_-]+)?code\b', re.I)
_PROMO_PATH = re.compile(r'(?:^|[/_-])(?:promo(?:code|codes|s)?|coupon(?:s)?|'
                         r'best[-_]sportsbooks?|sportsbook[-_]reviews?|affiliate[-_]offers?)(?:[/_.-]|$)', re.I)
_AFFILIATE_BOILERPLATE = re.compile(r'\baffiliate\s+(?:links?|disclosure)|'
                                  r'\b(?:earn|receive)\s+(?:a\s+)?commission\b', re.I)
_UNCERTAIN = re.compile(r'\b(rumou?rs?|unconfirmed|reportedly|speculat\w*|could|might|'
                        r'trade\s+(?:interest|target|proposal))\b|'
                        r'\bmay\s+(?:miss|return|play|be|not|sit|trade|sign|rest|undergo)\b', re.I)


def _clean(value, limit=1000):
    text = html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))
    text = ''.join(character if ord(character) >= 32 else ' ' for character in text)
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def _url(value):
    if not isinstance(value, str) or len(value) > 800 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
            return value.strip()
    except ValueError:
        pass
    return None


def _metadata(item):
    raw = item.get('metadata_json')
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _is_promotion(item):
    title = _clean(item.get('title'), 1000)
    url = _url(item.get('url'))
    path = urlsplit(url).path if url else ''
    priced_promo = (re.search(r'\bpromo(?:tion|tional)?\b', title, re.I)
                    and re.search(r'\$\s*\d|\bget[\s_$]*\d|\b(?:bonus|deposit|offer|coupon)\b', title, re.I))
    return bool(_PROMOTION.search(title) or _PROMO_PATH.search(path) or priced_promo)


def is_promotion(item):
    """Reusable commercial-offer gate; safe to call before an alert is sent."""
    return isinstance(item, dict) and _is_promotion(item)


def material_categories(item):
    """Exclude routine box-score recaps from material news, even with minutes boilerplate."""
    if not isinstance(item, dict) or _is_promotion(item):
        return []
    title = _clean(item.get('title'), 500)
    title_categories = _categories(title, '')
    recap = re.search(r'\b(drops?|scores?|scored|pours?|tallies|nets|posts?|finishes|double[- ]double|'
                      r'triple[- ]double)\b.*\b(points?|win|loss|victory|defeat)\b|'
                      r'\b\d+\s+points?\s+in\s+(?:a\s+)?(?:win|loss)\b', title, re.I)
    if recap and not title_categories:
        return []
    return _categories(title, _excerpt(item.get('body')))


def _excerpt(value):
    text = _clean(value, 5000)
    clauses = re.split(r'(?<=[.!?])\s+|\s*[|•]\s*', text)
    return ' '.join(clause for clause in clauses if not _PROMOTION.search(clause)
                    and not _AFFILIATE_BOILERPLATE.search(clause))[:160]


def _categories(title, excerpt):
    text = title + ' ' + excerpt
    categories = []
    if _INJURY.search(text):
        categories.append('injury')
    if _AVAILABILITY.search(text):
        categories.append('availability')
    if _TRADE.search(text):
        categories.append('trade/roster')
    return categories


def _aware(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        instant = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            instant = email.utils.parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    try:
        return instant.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None


def _publisher_clock(value):
    """Sortable wall-clock value within one publisher, never an inferred timezone."""
    try:
        instant = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return instant.toordinal() * 86400 + instant.hour * 3600 + instant.minute * 60 + instant.second
    except (TypeError, ValueError, OverflowError):
        return 0


def _publication_label(value):
    if value is None or not str(value).strip():
        return 'not supplied (timezone unknown)'
    text = _clean(value, 120)
    if _aware(str(value)):
        return text
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text + ' (time/timezone not supplied)'
    return text + ' (timezone unconfirmed)'


def _verification(item, metadata, title, excerpt):
    note = ' '.join(str(metadata.get(field) or '') for field in
                    ('verification', 'verification_status', 'source'))
    if metadata.get('manual_unverified') or 'manual' in (str(item.get('source', '')) + note).casefold():
        return 'manual note; unverified'
    if re.search(r'rumor|rumour|unconfirmed', note, re.I) or _UNCERTAIN.search(title + ' ' + excerpt):
        return 'unconfirmed report/rumor'
    return 'publisher-reported; unverified'


def _feed_status(store):
    method = getattr(store, 'status', None)
    if not callable(method):
        return []
    try:
        feeds = method().get('feeds', [])
    except Exception:
        return []
    result = []
    for feed in feeds:
        name = str(feed.get('name', ''))
        if not (name.startswith('rss_') or name in {'espn_news', 'injuries_configured',
                                                   'availability_coverage', 'manual_news'}):
            continue
        result.append({'name': _clean(name, 60), 'last_success': _clean(feed.get('last_success'), 65) or None,
                       'error': 'Latest attempt failed or source disabled; see /status.' if feed.get('error') else None})
    # Current deployments may have disabled legacy ESPN/injury sources before
    # RSS entries alphabetically. Prioritize actual headline collectors so a
    # healthy third RSS feed cannot disappear behind disabled-source metadata.
    result.sort(key=lambda feed: (0 if feed['name'].startswith('rss_') else
                                  1 if feed['name'] == 'espn_news' else 2, feed['name']))
    return result[:8]


def _topic(value):
    topic = str(value).strip().casefold()
    aliases = {'availability': 'injuries', 'injury': 'injuries', 'trade': 'trades', 'all': 'news'}
    topic = aliases.get(topic, topic)
    if topic not in {'injuries', 'trades', 'news'}:
        raise ValueError('Summary topic must be injuries, trades or news.')
    return topic


def compact_news_context(store, limit=8, topic='injuries'):
    """Return <=6000 serialized characters of untrusted, cited collected evidence.

    The default injury/availability desk also includes material roster trades,
    because those reports can change a player's role. No player status is inferred.
    """
    topic = _topic(topic)
    if isinstance(limit, bool):
        raise ValueError('Summary limit must be a number.')
    try:
        limit = max(1, min(10, int(limit)))
    except (TypeError, ValueError):
        raise ValueError('Summary limit must be a number.') from None
    now = dt.datetime.now(dt.timezone.utc)
    context = {'topic': topic, 'coverage': COVERAGE, 'reports': [],
               'feed_status': _feed_status(store), 'warnings': [],
               'matching_count': 0, 'excluded_promotions': 0, 'generated_at': now.isoformat()}
    try:
        rows = store.recent_news(limit=100)
    except Exception:
        context['warnings'] = ['Collected reports could not be read; check /status.']
        context['retrieval_error'] = True
        return context
    selected = []
    for index, item in enumerate(rows[:100]):
        if not isinstance(item, dict):
            continue
        if _is_promotion(item):
            context['excluded_promotions'] += 1
            continue
        title = _clean(item.get('title'), 180)
        excerpt = _excerpt(item.get('body'))
        categories = material_categories(item)
        if not categories or (topic == 'trades' and 'trade/roster' not in categories):
            continue
        metadata = _metadata(item)
        published = str(item['published_at'])[:120] if item.get('published_at') is not None else None
        collected = str(item['updated_at'])[:80] if item.get('updated_at') is not None else None
        instant = _aware(published)
        observed = _aware(collected)
        age_note = None
        if instant and (now - instant).total_seconds() > 86400:
            age_note = 'Published more than 24 hours ago; not a fresh status update.'
        elif instant and (instant - now).total_seconds() > 300:
            age_note = 'Publisher timestamp is in the future; verify it.'
        elif not instant and observed and (now - observed).total_seconds() > 86400:
            age_note = 'Collected more than 24 hours ago; publisher time is not timezone-confirmed.'
        elif not instant and published:
            try:
                clock = dt.datetime.fromisoformat(published)
                # This is a conservative age bound, not a timezone assignment.
                # Even UTC-14 cannot make this old publisher clock current.
                latest_possible = clock.replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=14)
                if (now - latest_possible).total_seconds() > 86400:
                    age_note = 'Publisher date is old despite its unknown timezone; not a fresh status update.'
            except (ValueError, OverflowError):
                pass
        report = {'id': _clean(item.get('id'), 80), 'title': title or 'Untitled collected report',
                  'excerpt': excerpt, 'categories': categories,
                  'source': _clean(item.get('source'), 80) or 'not supplied',
                  'published_at': published, 'published_label': _publication_label(published),
                  'collected_at': collected, 'verification': _verification(item, metadata, title, excerpt),
                  'url': _url(item.get('url')), 'age_note': age_note}
        # Never let the order of a backfill make a 10:06 publisher item appear
        # newer than its 11:57 update. Unknown timezones stay grouped by source;
        # their wall clocks are not compared as real UTC instants across sources.
        rank = 3 if age_note and 'future' in age_note else 2 if age_note else 0 if instant else 1
        group = '' if instant else report['source'].casefold()
        sort_clock = instant.timestamp() if instant else _publisher_clock(published)
        selected.append(((rank, group, -sort_clock, index), report))
    selected.sort(key=lambda value: value[0])
    context['matching_count'] = len(selected)
    context['reports'] = [value[1] for value in selected[:limit]]
    fresh_sources = [feed for feed in context['feed_status'] if feed['name'] != 'manual_news' and not feed['error']
                     and _aware(feed['last_success'])
                     and -60 <= (now - _aware(feed['last_success'])).total_seconds() <= 1800]
    if not context['feed_status']:
        context['warnings'].append('Collection-source freshness is unavailable; check /status.')
    elif not fresh_sources:
        context['warnings'].append('No selected collection source has a clean successful update within 30 minutes; check /status.')
    if any(report['age_note'] for report in context['reports']):
        context['warnings'].append('Some reports are old or have uncertain timestamps; read each date before treating them as current.')
    if any(not _aware(report['published_at']) for report in context['reports']):
        context['warnings'].append('Some publisher times have no confirmed timezone; none has been guessed.')
    # Bound the entire evidence object, including escaped Unicode under default
    # json.dumps settings, so the small-model path cannot grow back to workbook size.
    while context['reports'] and len(json.dumps(context, ensure_ascii=True)) > MAX_CONTEXT_CHARS:
        context['reports'].pop()
    return context


def summary_limit(question, default=3):
    words = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}
    match = re.search(r'\b(one|two|three|four|five|\d{1,2})\s+(?:short\s+)?'
                      r'(?:bullets?|reports?|items?|updates?)\b', str(question), re.I)
    if not match:
        return default
    token = match.group(1).lower()
    return max(1, min(5, words[token] if token in words else int(token)))


def cited_news_summary(context, selection=None, limit=3):
    """Render cited source wording. AI can select reports, never rewrite provenance."""
    reports = context.get('reports', [])
    if selection is None:
        selection = list(range(1, min(limit, len(reports)) + 1))
    if (not isinstance(selection, list) or any(type(number) is not int
            or not 1 <= number <= len(reports) for number in selection)
            or len(set(selection)) != len(selection)):
        raise ValueError('Summary selection must contain distinct report numbers.')
    chosen = [reports[number - 1] for number in selection[:limit]]
    if not chosen:
        return 'No matching collected reports are available. ' + COVERAGE
    blocks = []
    for report in chosen:
        lines = ['- ' + report['title']]
        if report.get('excerpt'):
            lines.append('  Source excerpt: ' + report['excerpt'])
        lines.extend(['  Source: ' + report['source'],
                      '  Published: ' + report['published_label'],
                      '  ' + report['verification']])
        if report.get('age_note'):
            lines.append('  ' + report['age_note'])
        if report.get('url'):
            lines.append('  ' + report['url'])
        block = '\n'.join(lines)
        # Reserve room for the mandatory coverage note and freshness warning.
        if len('\n\n'.join(blocks + [block])) + len(COVERAGE) + 400 > MAX_MESSAGE_CHARS:
            break
        blocks.append(block)
    footer = [COVERAGE]
    if context.get('warnings'):
        footer.append(context['warnings'][0])
    return '\n\n'.join(blocks + footer)


def news_summary(store, limit=5, topic='injuries'):
    """Format the collected evidence deterministically for one Telegram message."""
    context = compact_news_context(store, limit=limit, topic=topic)
    heading = ('Collected NBA trade / roster reports' if context['topic'] == 'trades'
               else 'Collected NBA injury / availability / trade reports')
    lines = [heading, COVERAGE]
    if context.get('retrieval_error'):
        return '\n\n'.join(lines + context['warnings'])[:MAX_MESSAGE_CHARS]
    if not context['reports']:
        message = ('Matching reports exceeded the safe display limit; use /news to inspect them.'
                   if context['matching_count'] else
                   'No matching reports were found among the latest 100 collected items. '
                   'This does not establish that there are no injuries, trades, or availability changes.')
        return '\n\n'.join(lines + [message]
                             + context['warnings'])[:MAX_MESSAGE_CHARS]
    blocks = []
    for report in context['reports']:
        block = [f"{len(blocks) + 1}. {report['title']}",
                 '[' + report['verification'] + ']',
                 'Source: ' + report['source'],
                 'Published: ' + report['published_label']]
        if report['age_note']:
            block.append(report['age_note'])
        if report['url']:
            block.append(report['url'])
        else:
            block.append('URL: not supplied or not a usable public HTTP/HTTPS link')
        joined = '\n'.join(block)
        if len('\n\n'.join(lines + blocks + [joined])) + 230 > MAX_MESSAGE_CHARS:
            break
        blocks.append(joined)
    suffix = context['warnings'][:1]
    if len(blocks) < context['matching_count']:
        suffix.append(f"Showing {len(blocks)} of {context['matching_count']} matching collected reports; /news shows the feed.")
    suffix.append('Dates and wording are publisher-reported. Check /status for collection freshness.')
    return '\n\n'.join(lines + blocks + suffix)[:MAX_MESSAGE_CHARS]
