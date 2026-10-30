"""Layout, uncertainty and local synthetic-image checks for screenshot cards."""
import io
from pathlib import Path
import shutil
import unittest

from screenshot_board import extract_screenshot, parse_tsv, read_cards, reconcile_views, _visual_words, _offer_variant

PLAYERS = [{'name': 'Jalen Brunson', 'team': 'NYK'}, {'name': 'Tyrese Maxey', 'team': 'PHI'},
           {'name': 'Karl-Anthony Towns', 'team': 'NYK'}]


def word(text, x, y, *, confidence=95, width=None):
    return {'text': text, 'x': x, 'y': y, 'w': width or len(text) * 10,
            'h': 22, 'confidence': confidence}


def card(name, x, y, market=('24.5', 'Points'), sides=('Less', 'More')):
    words = []
    cursor = x
    for item in name.split():
        words.append(word(item, cursor, y))
        cursor += len(item) * 10 + 10
    cursor = x
    for item in market:
        words.append(word(item, cursor, y + 40))
        cursor += len(item) * 10 + 12
    for index, side in enumerate(sides):
        words.append(word(side, x + index * 140, y + 80))
    return words


class LayoutReaderTests(unittest.TestCase):
    def test_columns_keep_player_threshold_and_sides_together(self):
        words = card('Jalen Brunson', 30, 60) + card('Tyrese Maxey', 530, 60, ('27.5', 'Points'), ('More',))
        view = read_cards(words, 1000, 800, PLAYERS)
        self.assertTrue(view['two_columns'])
        self.assertEqual([(r['player'], r['line'], r['sides']) for r in view['cards']],
                         [('Jalen Brunson', 24.5, ['less', 'more']), ('Tyrese Maxey', 27.5, ['more'])])
        accepted, _ = reconcile_views([view, view])
        self.assertEqual(len(accepted), 2)

    def test_bad_left_card_does_not_borrow_right_market(self):
        words = card('Jalen Brunson', 30, 60, ('3.5', 'atrsw3+', 'Pts')) + card(
            'Tyrese Maxey', 530, 60, ('17.5', 'FG', 'Attempted'))
        view = read_cards(words, 1000, 800, PLAYERS)
        self.assertEqual([(r['player'], r['stat'], r['line']) for r in view['cards']],
                         [('Tyrese Maxey', 'fga', 17.5)])
        self.assertIn('Jalen Brunson', ' '.join(view['unresolved']))

    def test_repeated_player_markets_remain_separate(self):
        words = card('Jalen Brunson', 30, 60) + card('Jalen Brunson', 30, 300, ('36.5', 'PRA'))
        view = read_cards(words, 500, 700, PLAYERS)
        accepted, _ = reconcile_views([view, view])
        self.assertEqual([(r['stat'], r['line']) for r in accepted], [('pts', 24.5), ('pra', 36.5)])

    def test_partial_market_and_unsupported_two_point_attempts_abstain(self):
        for market in [('1.5', '2-PT', 'Attempted'), ('4.5', '1Q', 'Points'), ('4.5', 'Fantasy', 'Score')]:
            with self.subTest(market=market):
                self.assertEqual(read_cards(card('Jalen Brunson', 30, 60, market), 500, 500, PLAYERS)['cards'], [])
        words = card('Jalen Brunson', 30, 60)
        words.append(word('First Half', 30, 90))
        self.assertEqual(read_cards(words, 500, 500, PLAYERS)['cards'], [])

    def test_global_period_header_and_same_name_row_qualifier_abstain(self):
        for header in ['First Half', '1st 3 min', 'First 3 Minutes', '1Q Points', 'Live']:
            with self.subTest(header=header):
                words = card('Jalen Brunson', 30, 100) + [word(header, 30, 20)]
                self.assertFalse(read_cards(words, 500, 500, PLAYERS)['cards'])
        words = card('Jalen Brunson', 30, 60) + [word('1Q', 260, 60)]
        self.assertFalse(read_cards(words, 500, 500, PLAYERS)['cards'])
        words = card('Jalen Brunson', 30, 100) + [word('Starts in 51:37', 30, 20)]
        self.assertTrue(read_cards(words, 500, 500, PLAYERS)['cards'])

    def test_header_digits_cannot_combine_with_search_icon_on_another_row(self):
        words = card('Jalen Brunson', 30, 100) + [word('4', 400, 10), word('Q', 30, 45)]
        self.assertTrue(read_cards(words, 500, 500, PLAYERS)['cards'])

    def test_visible_period_veto_survives_cropped_views_without_header(self):
        ordinary = read_cards(card('Jalen Brunson', 30, 100), 500, 500, PLAYERS)
        partial = read_cards(card('Jalen Brunson', 30, 100) + [word('First Half', 30, 20)], 500, 500, PLAYERS)
        self.assertFalse(reconcile_views([ordinary, ordinary, partial])[0])

    def test_ligature_corroboration_still_requires_one_clear_market_reading(self):
        strong_words = card('Karl-Anthony Towns', 30, 60, ('1.5', '3P™'), ('More',))
        weak_words = [dict(w) for w in strong_words]
        next(w for w in weak_words if w['text'] == '3P™')['confidence'] = 60
        strong = read_cards(strong_words, 550, 500, PLAYERS)
        weak = read_cards(weak_words, 550, 500, PLAYERS)
        self.assertFalse(reconcile_views([weak, weak])[0])
        accepted = reconcile_views([strong, weak])[0]
        self.assertEqual([(c['stat'], c['line']) for c in accepted], [('tpm', 1.5)])

    def test_circle_pixels_required_before_removing_a_numeric_control_glyph(self):
        from PIL import Image, ImageDraw
        words = [word('2', 40, 62, width=12), word('19.5', 80, 55, width=50)]
        words[0]['h'], words[1]['h'] = 12, 22
        plain = Image.new('L', (200, 150), 245)
        self.assertEqual(len(_visual_words(words, plain)), 2)
        image = plain.copy(); draw = ImageDraw.Draw(image)
        draw.ellipse((26, 48, 66, 88), fill=210)
        draw.rectangle((42, 64, 49, 71), fill=0)
        self.assertEqual([w['text'] for w in _visual_words(words, image)], ['19.5'])

    def test_decimal_is_recovered_only_when_dot_pixels_exist(self):
        from PIL import Image, ImageDraw
        words = [word('17', 20, 20, width=30), word('5', 64, 20, width=15)]
        for w in words: w['h'] = 30
        image = Image.new('L', (150, 80), 245)
        self.assertEqual(len(_visual_words(words, image)), 2)
        ImageDraw.Draw(image).rectangle((55, 44, 60, 49), fill=0)
        self.assertEqual([w['text'] for w in _visual_words(words, image)], ['17.5'])

    def test_button_offer_icons_preserve_side_variant_without_inventing_payout(self):
        from PIL import Image, ImageDraw
        button = word('More', 10, 30, width=45); button['h'] = 20
        for color, expected in [((230, 20, 10), 'demon'), ((10, 210, 20), 'goblin'), (None, 'standard')]:
            image = Image.new('RGB', (200, 100), (20, 20, 30))
            if color:
                ImageDraw.Draw(image).rectangle((65, 28, 85, 48), fill=color)
            self.assertEqual(_offer_variant(image, button, 180), expected)

    def test_unknown_player_conflicting_roster_and_missing_sides_abstain(self):
        self.assertFalse(read_cards(card('Unknown Person', 30, 60), 500, 500, PLAYERS)['cards'])
        ambiguous = PLAYERS + [{'name': 'Jalen Brunson', 'team': 'PHI'}]
        self.assertFalse(read_cards(card('Jalen Brunson', 30, 60), 500, 500, ambiguous)['cards'])
        self.assertFalse(read_cards(card('Jalen Brunson', 30, 60, sides=()), 500, 500, PLAYERS)['cards'])

    def test_weak_numbers_and_extra_numbers_abstain(self):
        words = card('Jalen Brunson', 30, 60)
        words[2]['confidence'] = 39
        self.assertFalse(read_cards(words, 500, 500, PLAYERS)['cards'])

    def test_missing_card_cannot_borrow_an_unknown_players_lower_card(self):
        words = card('Jalen Brunson', 30, 60, sides=())[:2]
        words += card('Unrecognized Person', 30, 350, ('18.5', 'Points'))
        self.assertFalse(read_cards(words, 500, 700, PLAYERS)['cards'])

    def test_missing_card_cannot_borrow_unknown_player_in_other_column(self):
        words = card('Jalen Brunson', 30, 60, sides=())[:2]
        words += card('Unknown Person', 530, 60, ('17.5', 'FG', 'Attempted'))
        self.assertFalse(read_cards(words, 1000, 800, PLAYERS)['cards'])

    def test_missing_buttons_cannot_borrow_unknown_other_column_buttons(self):
        words = card('Jalen Brunson', 30, 60, sides=())
        words += card('Unknown Person', 530, 60, ('17.5', 'FG', 'Attempted'))
        self.assertFalse(read_cards(words, 1000, 800, PLAYERS)['cards'])

    def test_unknown_player_inside_vertical_window_still_breaks_card_identity(self):
        for include_line in (False, True):
            words = card('Jalen Brunson', 30, 60, sides=())
            if not include_line:
                words = words[:2]
            words += card('Unknown Person', 30, 140, ('17.5', 'FG', 'Attempted'))
            view = read_cards(words, 500, 600, PLAYERS)
            self.assertFalse(view['cards'])
            self.assertFalse(reconcile_views([view, view])[0])
        words = card('Jalen Brunson', 30, 60, ('2', '19.5', 'Points'))
        self.assertFalse(read_cards(words, 500, 500, PLAYERS)['cards'])

    def test_consensus_requires_two_views_and_conflict_veto(self):
        first = read_cards(card('Jalen Brunson', 30, 60), 500, 500, PLAYERS)
        self.assertEqual(reconcile_views([first])[0], [])
        second = read_cards(card('Jalen Brunson', 30, 60, ('29.5', 'Points')), 500, 500, PLAYERS)
        accepted, unresolved = reconcile_views([first, second])
        self.assertEqual(accepted, [])
        self.assertIn('disagree', ' '.join(unresolved))

    def test_only_twice_observed_sides_pass_consensus(self):
        first = read_cards(card('Jalen Brunson', 30, 60), 500, 500, PLAYERS)
        second = read_cards(card('Jalen Brunson', 30, 60, sides=('More',)), 500, 500, PLAYERS)
        self.assertEqual(reconcile_views([first, second])[0][0]['sides'], ['more'])

    def test_distant_single_column_side_is_flagged_instead_of_silently_lost(self):
        words = card('Jalen Brunson', 30, 60, sides=())
        words += [word('Less', 30, 140), word('More', 300, 140)]
        view = read_cards(words, 500, 500, PLAYERS)
        self.assertEqual(view['cards'][0]['sides'], ['less'])
        self.assertFalse(view['cards'][0]['sides_complete'])
        self.assertIn('additional More/Less', ' '.join(view['unresolved']))

    def test_bounded_tsv_skips_invalid_boxes_and_confidence(self):
        header = 'left\ttop\twidth\theight\tconf\ttext\n'
        rows = '1\t2\t20\t20\t95\tMore\n1\t2\t20\t20\tnan\tLess\n-1\t2\t20\t20\t95\tBad\n'
        parsed = parse_tsv(header + rows, x_offset=500)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['x'], 501)

    def test_bad_image_and_oversize_bytes_are_rejected(self):
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract unavailable')
        with self.assertRaises(ValueError):
            extract_screenshot(b'not an image', PLAYERS)
        with self.assertRaises(ValueError):
            extract_screenshot(b'x' * (8 * 1024 * 1024 + 1), PLAYERS)

    def test_real_ocr_synthetic_two_column_image(self):
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract unavailable')
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.skipTest('Pillow unavailable')
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if not font_path.exists():
            self.skipTest('Synthetic fixture font unavailable')
        image = Image.new('RGB', (1000, 700), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(font_path), 28)
        draw.text((30, 30), 'NBA player lines', font=font, fill='black')
        for x, name, value in [(30, 'Jalen Brunson', '24.5'), (530, 'Tyrese Maxey', '27.5')]:
            draw.rounded_rectangle((x - 10, 140, x + 440, 430), radius=16, outline='gray', width=2)
            draw.text((x, 200), name, font=font, fill='black')
            draw.text((x, 260), value + ' Points', font=font, fill='black')
            draw.text((x, 330), 'Less', font=font, fill='black')
            draw.text((x + 240, 330), 'More', font=font, fill='black')
        payload = io.BytesIO()
        image.save(payload, format='PNG')
        result = extract_screenshot(payload.getvalue(), PLAYERS)
        self.assertEqual([(c['player'], c['stat'], c['line']) for c in result['cards']],
                         [('Jalen Brunson', 'pts', 24.5), ('Tyrese Maxey', 'pts', 27.5)], result)
        self.assertTrue(all(c['sides'] == ['less', 'more'] for c in result['cards']))


if __name__ == '__main__':
    unittest.main()
