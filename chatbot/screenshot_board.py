"""Read individual player cards from screenshots without mixing their columns.

This is conservative OCR, not image understanding. Names must match a supplied
roster and a market/threshold must agree across two OCR views. Unclear cards are
returned for review. Visible More/Less buttons describe available alternatives,
never a placed wager or a selected side.
"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import warnings

READER_VERSION = 'card-ocr-v1'
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_OCR_SECONDS = 38
MAX_WORDS = 6000
MIN_WORD_CONFIDENCE = 70.0

MARKETS = {
    'points': 'pts', 'pts': 'pts', 'rebounds': 'reb', 'rebs': 'reb',
    'assists': 'ast', 'asts': 'ast', 'turnovers': 'tov',
    '3ptmade': 'tpm', '3ptm': 'tpm', '3pm': 'tpm', '3pointersmade': 'tpm',
    'threepointersmade': 'tpm', 'threesmade': 'tpm',
    'fgattempted': 'fga', 'fgattempts': 'fga', 'fieldgoalattempts': 'fga',
    '3ptattempted': 'tpa', '3ptattempts': 'tpa', '3pa': 'tpa',
    'steals': 'stl', 'blocks': 'blk', 'stealsblocks': 'stocks',
    'pra': 'pra', 'ptsrebsasts': 'pra', 'pointsreboundsassists': 'pra',
    'ptsrebs': 'pr', 'pointsrebounds': 'pr',
    'ptsasts': 'pa', 'pointsassists': 'pa',
    'rebsasts': 'ra', 'reboundsassists': 'ra',
    'offensiverebounds': 'oreb', 'defensiverebounds': 'dreb',
}
_SPECIAL = re.compile(
    r'\b(?:[1-4](?:st|nd|rd|th)?\s*(?:q(?:tr)?|quarter)|q[1-4]|[12]h|'
    r'(?:first|second|third|fourth|1st|2nd|3rd|4th)\s*(?:half|quarter|[1-9]\s*min(?:ute)?s?)|'
    r'half|quarter|fantasy|live|in[- ]game|first\s*basket|combo|h2h)\b', re.I)


def _normalized(text):
    value = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode().casefold()
    return re.sub(r'[^a-z0-9]+', '', value)


def parse_tsv(text, *, x_offset=0):
    """Keep bounded, finite word boxes; malformed TSV is not usable evidence."""
    words = []
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE)
        for row in reader:
            if len(words) >= MAX_WORDS:
                break
            value = str(row.get('text') or '').strip()
            if not value or len(value) > 120:
                continue
            try:
                confidence = float(row['conf'])
                left, top, width, height = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
            except (KeyError, ValueError, TypeError):
                continue
            if not math.isfinite(confidence) or not 0 <= confidence <= 100:
                continue
            if min(left, top) < 0 or not 0 < width <= 5000 or not 0 < height <= 5000:
                continue
            words.append({'text': value, 'confidence': confidence, 'x': left + x_offset,
                          'y': top, 'w': width, 'h': height})
    except (csv.Error, TypeError):
        return []
    return words


def _rows(words):
    """Reconstruct spatial rows rather than trusting sparse-text reading order."""
    rows = []
    for word in sorted(words, key=lambda w: (w['y'] + w['h'] / 2, w['x'])):
        center = word['y'] + word['h'] / 2
        row = next((r for r in reversed(rows[-4:])
                    if abs(center - r['center']) <= max(5, min(word['h'], r['height']) * .55)), None)
        if row is None:
            row = {'center': center, 'height': word['h'], 'words': []}
            rows.append(row)
        row['words'].append(word)
    for row in rows:
        row['words'].sort(key=lambda w: w['x'])
    return rows


def _roster(players):
    by_name = {}
    for player in players[:1500]:
        name = str(player.get('name') or player.get('player') or '').strip()
        team = str(player.get('team') or '').strip().upper()
        key = _normalized(name)
        if len(key) < 5 or not team:
            continue
        by_name.setdefault(key, set()).add((name, team))
    # Conflicting stored team/name identities are not resolved by guessing.
    return {key: next(iter(values)) for key, values in by_name.items() if len(values) == 1}


def _anchors(words, roster):
    anchors = []
    for row in _rows(words):
        items = row['words']
        for start in range(len(items)):
            for end in range(start + 1, min(start + 5, len(items)) + 1):
                part = items[start:end]
                if any(b['x'] - (a['x'] + a['w']) > max(a['h'], b['h']) * 2.2
                       for a, b in zip(part, part[1:])):
                    break
                key = _normalized(' '.join(w['text'] for w in part))
                if key not in roster or min(w['confidence'] for w in part) < MIN_WORD_CONFIDENCE:
                    continue
                name, team = roster[key]
                x = min(w['x'] for w in part)
                y = min(w['y'] for w in part)
                right = max(w['x'] + w['w'] for w in part)
                bottom = max(w['y'] + w['h'] for w in part)
                anchors.append({'player': name, 'team': team, 'x': x, 'y': y,
                                'w': right - x, 'h': bottom - y,
                                'confidence': min(w['confidence'] for w in part)})
    return anchors


def _market(text):
    """Accept exactly one threshold followed by a known complete market label."""
    match = re.fullmatch(r'\s*(\d{1,3}(?:\.\d{1,2})?)\s+(.+?)\s*', text)
    if not match:
        # OCR sometimes joins the threshold to its adjacent label.
        match = re.fullmatch(r'\s*(\d{1,3}(?:\.\d{1,2})?)([A-Za-z].+?)\s*', text)
    if not match:
        return None
    line, label = float(match[1]), _normalized(match[2])
    stat = MARKETS.get(label)
    if stat is None or not 0 <= line <= 300:
        return None
    # Missing decimal points in low-resolution screenshots must not produce
    # implausible thresholds for small-count markets.
    ceilings = {'tpm': 20, 'tpa': 40, 'stl': 15, 'blk': 15, 'stocks': 20,
                'tov': 20, 'oreb': 30, 'dreb': 50, 'ast': 50, 'reb': 60}
    if line > ceilings.get(stat, 200):
        return None
    return stat, line


def read_cards(words, width, height, players):
    """Parse spatial OCR fixtures or one OCR view; acceptance requires consensus."""
    roster = _roster(players)
    anchors = _anchors(words, roster)
    first_name_y = min((a['y'] for a in anchors), default=height)
    header_text = ' '.join(w['text'] for r in _rows([w for w in words if w['y'] < first_name_y - 3])
                           for w in r['words'])
    global_special = bool(_SPECIAL.search(header_text))
    middle = width / 2
    left = [a for a in anchors if a['x'] + a['w'] <= middle]
    right = [a for a in anchors if a['x'] >= middle]
    two_columns = bool(left and right)
    cards, unresolved = [], []
    for anchor in anchors[:100]:
        if two_columns and anchor['x'] < middle < anchor['x'] + anchor['w']:
            unresolved.append(anchor['player'] + ': player card crosses the column boundary.')
            continue
        region = (0, middle) if two_columns and anchor['x'] < middle else (
            (middle, width) if two_columns else (0, width))
        # A known name on only one side does not prove the image is one column:
        # the other column may contain an unrecognized/garbled player. Bound the
        # readable area to this name's local card even when grid detection fails.
        # Ambiguous centered/full-width layouts can lose a side and need review;
        # borrowing the other player's threshold is never an acceptable fallback.
        local_left = max(region[0], anchor['x'] - anchor['h'] * 2)
        local_right = min(region[1], anchor['x'] + max(anchor['w'] + anchor['h'] * 2, width * .45))
        next_top = min((other['y'] for other in anchors
                        if other['y'] > anchor['y'] + anchor['h']
                        and region[0] <= other['x'] < region[1]), default=height)
        # An unrecognized player farther down the image must not lend their
        # threshold/buttons to a known name whose own card is incomplete.
        next_top = min(next_top, anchor['y'] + anchor['h'] + max(100, anchor['h'] * 8))
        # Restrict to the card below this name, ending before the next name.
        section = [w for w in words if local_left <= w['x'] and w['x'] + w['w'] <= local_right
                   and w['y'] >= anchor['y'] + anchor['h'] - 3 and w['y'] < next_top - 3]
        rows = _rows(section)
        side_rows = [r for r in rows if any(_normalized(w['text']) in {'more', 'less'}
                                          and w['confidence'] >= MIN_WORD_CONFIDENCE for w in r['words'])]
        if not side_rows:
            unresolved.append(anchor['player'] + ': More/Less buttons or lower card edge are unreadable.')
            continue
        first_side = min(r['center'] for r in side_rows)
        sides = sorted({_normalized(w['text']) for r in side_rows
                        if abs(r['center'] - first_side) <= max(12, r['height'])
                        for w in r['words'] if _normalized(w['text']) in {'more', 'less'}
                        and w['confidence'] >= MIN_WORD_CONFIDENCE})
        uncertain_buttons = not two_columns and any(
            _normalized(w['text']) in {'more', 'less'} and w['confidence'] >= MIN_WORD_CONFIDENCE
            and abs(w['y'] + w['h'] / 2 - first_side) <= max(12, w['h'])
            and not (local_left <= w['x'] and w['x'] + w['w'] <= local_right)
            for w in words)
        if uncertain_buttons:
            unresolved.append(anchor['player'] + ': additional More/Less text could not be assigned to this card; only clearly matched sides are included.')
        market_rows = [r for r in rows if r['center'] < first_side - 3]
        # A region containing a period/live/special definition cannot be passed
        # off as a full-game market merely because one row says "Points".
        name_row = [w for w in words if region[0] <= w['x'] and w['x'] + w['w'] <= region[1]
                    and abs(w['y'] - anchor['y']) <= max(3, anchor['h'] / 2)]
        content = (' '.join(w['text'] for w in name_row) + ' ' +
                   ' '.join(w['text'] for r in market_rows for w in r['words']))
        if global_special or _SPECIAL.search(content):
            unresolved.append(anchor['player'] + ': partial-game or special market needs review.')
            continue
        # The complete text between this name and its buttons must be the
        # market, on one or two adjacent rows. Searching for a parseable row
        # anywhere in a larger block can silently borrow the next unknown
        # player's line while ignoring that player's unrecognized name.
        matches = []
        selected_words = [w for r in market_rows for w in r['words']]
        contiguous = (len(market_rows) == 1 or len(market_rows) == 2 and
                      market_rows[1]['center'] - market_rows[0]['center'] <=
                      max(market_rows[0]['height'], market_rows[1]['height']) * 2.2)
        if contiguous and min((w['confidence'] for w in selected_words), default=0) >= MIN_WORD_CONFIDENCE:
            parsed = _market(' '.join(w['text'] for w in selected_words))
            if parsed:
                matches.append((*parsed, min(w['confidence'] for w in selected_words)))
        unique = {(stat, line) for stat, line, _ in matches}
        if len(unique) != 1:
            unresolved.append(anchor['player'] + ': statistic or line is unclear; resend that card.')
            continue
        stat, line = next(iter(unique))
        confidence = min(anchor['confidence'], max(c for s, n, c in matches if (s, n) == (stat, line)))
        cards.append({'player': anchor['player'], 'team': anchor['team'], 'stat': stat,
                      'line': line, 'sides': sides, 'confidence': round(confidence, 1),
                      'sides_complete': not uncertain_buttons,
                      'source': 'screenshot_ocr',
                      'source_box': [round(region[0]), anchor['y'], round(region[1]), round(first_side)]})
    return {'cards': cards, 'unresolved': list(dict.fromkeys(unresolved)),
            'known_player_cards': len(anchors), 'two_columns': two_columns}


def reconcile_views(views):
    """Accept matching market values across views; any conflicting value vetoes."""
    groups = []
    unresolved = []
    for view_index, view in enumerate(views):
        unresolved.extend(view.get('unresolved', []))
        for card in view.get('cards', []):
            group = next((g for g in groups if g[0][1]['player'] == card['player']
                          and g[0][1]['team'] == card['team']
                          and abs(g[0][1]['source_box'][1] - card['source_box'][1]) < 35
                          and abs(g[0][1]['source_box'][0] - card['source_box'][0]) < 35), None)
            if group is None:
                group = []
                groups.append(group)
            group.append((view_index, card))
    accepted = []
    for group in groups:
        card = dict(group[0][1])
        readings = {(r['stat'], r['line']) for _, r in group}
        if len(readings) != 1:
            unresolved.append(card['player'] + ': OCR readings disagree about the line or statistic.')
            continue
        if len({index for index, _ in group}) < 2:
            unresolved.append(card['player'] + ': line needs a clearer second reading.')
            continue
        # Only alternatives visible in two separate OCR passes are admitted.
        # This is consistency checking, not independent visual verification.
        sides = [side for side in ('less', 'more') if len(
            {index for index, r in group if side in r['sides']}) >= 2]
        if not sides:
            unresolved.append(card['player'] + ': available More/Less sides need review.')
            continue
        card['sides'] = sides
        card['sides_complete'] = all(r.get('sides_complete', True) for _, r in group)
        card['confidence'] = min(r['confidence'] for _, r in group)
        card['corroborating_views'] = len({index for index, _ in group})
        if not any((r['player'], r['team'], r['stat'], r['line'], r['sides']) ==
                   (card['player'], card['team'], card['stat'], card['line'], card['sides']) for r in accepted):
            accepted.append(card)
    # Do not suppress failed other cards for an accepted player: the same player
    # can have another market lower in the screenshot that still needs review.
    return accepted, list(dict.fromkeys(unresolved))[:12]


def extract_screenshot(payload, players):
    """Bounded local OCR; no network, model, master-data or entry mutations."""
    result = {'cards': [], 'unresolved': [], 'raw_text': '', 'reader_version': READER_VERSION,
              'verification': 'User-uploaded OCR; not visually verified.',
              'review_note': 'Only readable cards are included. Compare the listed lines with your screenshot; OCR can miss cards or decimal points.'}
    executable = shutil.which('tesseract')
    if not executable:
        result['unresolved'] = ['Screenshot reader is unavailable; paste player | statistic | line instead.']
        return result
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError('Send a screenshot smaller than 8 MB.')
    try:
        from PIL import Image, ImageOps, ImageStat
    except ImportError:
        result['unresolved'] = ['Screenshot image support is unavailable; paste the lines instead.']
        return result
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            original = Image.open(io.BytesIO(payload))
            if original.width * original.height > MAX_IMAGE_PIXELS or min(original.size) < 100:
                raise ValueError('Screenshot dimensions are unsupported; crop the player cards and resend.')
            original = ImageOps.exif_transpose(original).convert('L')
            original.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError):
            raise ValueError('This image could not be read; send a PNG or JPEG screenshot.') from None
    scale = min(2, 2400 / max(original.size))
    if scale != 1:
        original = original.resize((max(1, round(original.width * scale)), max(1, round(original.height * scale))),
                                   Image.Resampling.LANCZOS)
    if ImageStat.Stat(original).median[0] < 128:
        original = ImageOps.invert(original)
    prepared = ImageOps.autocontrast(original)
    width, height = prepared.size
    deadline = time.monotonic() + MAX_OCR_SECONDS
    views = []
    all_words = []
    with tempfile.TemporaryDirectory(prefix='knicks-card-ocr-') as directory:
        def run(image, label, psm=11, x_offset=0):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                return []
            path = Path(directory) / (label + '.png')
            image.save(path)
            try:
                output = subprocess.run([executable, str(path), 'stdout', '--psm', str(psm), 'tsv'],
                                        capture_output=True, text=True, check=True,
                                        timeout=min(14, remaining))
            except (subprocess.SubprocessError, OSError):
                return []
            # Size bound also protects CSV parsing from unexpected subprocess output.
            return parse_tsv(output.stdout[:1_500_000], x_offset=x_offset)

        full = run(prepared, 'full')
        all_words = full
        views.append(read_cards(full, width, height, players))
        # Column views restore name/market reading order on the two-card grid.
        split = width // 2
        left = run(prepared.crop((0, 0, split, height)), 'left')
        right = run(prepared.crop((split, 0, width, height)), 'right', x_offset=split)
        split_words = left + right
        split_view = read_cards(split_words, width, height, players)
        if split_view['two_columns']:
            views.append(split_view)
        else:
            # Cropping a single card down its center can remove market qualifiers;
            # never accept those partial crops. Re-read its entire width instead.
            second = run(ImageOps.posterize(prepared, 4), 'full-second', psm=6)
            views.append(read_cards(second, width, height, players))
        result['raw_text'] = '\n'.join(' '.join(w['text'] for w in r['words']) for r in _rows(all_words))[:16000]
    cards, unresolved = reconcile_views(views)
    result['cards'], result['unresolved'] = cards, unresolved
    if not cards and not unresolved:
        result['unresolved'] = ['No complete supported player cards were readable; send closer crops with the name, line, market and buttons.']
    result['known_player_cards'] = max((view['known_player_cards'] for view in views), default=0)
    return result
