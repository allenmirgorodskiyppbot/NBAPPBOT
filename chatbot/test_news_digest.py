import datetime as dt
import json
import unittest

from news_digest import compact_news_context, is_promotion, news_summary


class FakeStore:
    def __init__(self, reports=None, feeds=None):
        self.reports = reports or []
        self.feeds = feeds or []
        self.requested_limit = None

    def recent_news(self, limit=10):
        self.requested_limit = limit
        return self.reports[:limit]

    def status(self):
        return {'feeds': self.feeds, 'counts': {'irrelevant_private_ledger': 999}}

    def record_news(self, *args, **kwargs):
        raise AssertionError('The digest must never mutate collected news.')


class NewsDigestTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        self.stamp = self.now.isoformat()

    def report(self, title='Jalen Brunson questionable with ankle soreness', **changes):
        record = {'id': 'article-1', 'title': title,
                  'body': 'The team said he will be evaluated again before the game.',
                  'source': 'Example NBA desk', 'published_at': self.stamp,
                  'updated_at': self.stamp, 'url': 'https://example.com/nba/brunson-update'}
        record.update(changes)
        return record

    def healthy_feed(self):
        return {'name': 'rss_1', 'last_success': self.stamp, 'error': None}

    def test_promotion_code_trade_text_is_not_a_player_trade(self):
        promo = self.report('OG PromoCode Trade25 Get50: Signup bonus offer',
                            body='Use code Trade25 for bonus bets on injury news.',
                            url='https://example.com/promos/og-code')
        trade = self.report('Knicks trade for guard with contract bonus', id='real-trade',
                            body='The guard is recovering from knee surgery. '
                                 'This article contains affiliate links. We may earn a commission.',
                            url='https://example.com/nba/trade-reaction')
        self.assertTrue(is_promotion(promo))
        self.assertFalse(is_promotion(trade))
        context = compact_news_context(FakeStore([promo, trade], [self.healthy_feed()]))
        self.assertEqual(context['excluded_promotions'], 1)
        self.assertEqual([r['id'] for r in context['reports']], ['real-trade'])
        self.assertIn('trade/roster', context['reports'][0]['categories'])
        self.assertIn('injury', context['reports'][0]['categories'])
        self.assertNotIn('affiliate', context['reports'][0]['excerpt'])
        self.assertTrue(is_promotion(self.report('OG Promo: Trade $25, Get $50',
                                                 url='https://example.com/nba/news')))

    def test_real_medical_and_trade_reports_survive_betting_mentions_and_bonus_words(self):
        for report in [
            self.report('Brunson injury shifts betting outlook', body='The guard is questionable.',
                        url='https://example.com/betting/nba/injury-update'),
            self.report('Knicks trade bonus clause explained', body='A contract bonus follows the trade.'),
            self.report('Towns to return to the lineup after illness'),
        ]:
            with self.subTest(title=report['title']):
                self.assertFalse(is_promotion(report))
                self.assertEqual(len(compact_news_context(FakeStore([report]))['reports']), 1)

    def test_source_publication_timestamp_and_url_are_preserved_without_guessing_timezone(self):
        published = '2026-10-29T10:30:00'
        report = self.report(published_at=published)
        summary = news_summary(FakeStore([report], [self.healthy_feed()]))
        self.assertIn('Source: Example NBA desk', summary)
        self.assertIn('Published: ' + published + ' (timezone unconfirmed)', summary)
        self.assertIn(report['url'], summary)
        self.assertNotIn(published + 'Z', summary)
        self.assertNotIn('EDT', summary)
        self.assertNotIn(' EST', summary)
        context = compact_news_context(FakeStore([report]))
        self.assertEqual(context['reports'][0]['published_at'], published)

    def test_no_player_status_is_invented_from_possible_injury_wording(self):
        report = self.report('Towns may miss Friday with an ankle injury',
                            body='A decision has not been announced. He might return next week.')
        context = compact_news_context(FakeStore([report]))
        evidence = context['reports'][0]
        self.assertEqual(evidence['title'], report['title'])
        self.assertEqual(evidence['verification'], 'unconfirmed report/rumor')
        self.assertNotIn('status', evidence)
        self.assertNotIn('player_status', evidence)
        summary = news_summary(FakeStore([report]))
        self.assertNotIn('Towns is out', summary)
        self.assertNotIn('ruled out', summary)
        self.assertIn('may miss', summary)

    def test_generic_out_and_trade_code_do_not_create_false_material_reports(self):
        reports = [self.report('Brunson outsmarts the defense', body='New York wins again.'),
                   self.report('Trade25 is today’s code', body='Basketball is on tonight.')]
        context = compact_news_context(FakeStore(reports))
        self.assertEqual(context['reports'], [])

    def test_empty_collection_is_not_claimed_to_mean_everyone_is_healthy(self):
        summary = news_summary(FakeStore())
        self.assertIn('No matching reports', summary)
        self.assertIn('does not establish', summary)
        self.assertIn('not a complete official NBA injury report', summary)
        self.assertNotIn('No injuries today', summary)

    def test_stale_report_and_stale_source_are_clearly_labeled(self):
        old = (self.now - dt.timedelta(days=3)).isoformat()
        store = FakeStore([self.report(published_at=old)],
                          [{'name': 'rss_1', 'last_success': old, 'error': None}])
        context = compact_news_context(store)
        self.assertIn('more than 24 hours', context['reports'][0]['age_note'])
        self.assertTrue(any('30 minutes' in warning for warning in context['warnings']))
        summary = news_summary(store)
        self.assertIn('not a fresh status update', summary)
        self.assertIn('30 minutes', summary)

    def test_recent_collection_does_not_make_old_publication_new(self):
        old = (self.now - dt.timedelta(days=4)).isoformat()
        context = compact_news_context(FakeStore([self.report(published_at=old)], [self.healthy_feed()]))
        self.assertIn('not a fresh status update', context['reports'][0]['age_note'])
        self.assertEqual(context['reports'][0]['published_at'], old)

    def test_manual_notes_remain_unverified_and_do_not_mask_failed_rss(self):
        manual = self.report(source='Manual user upload', published_at=None,
                             metadata_json=json.dumps({'manual_unverified': True}))
        feeds = [{'name': 'manual_news', 'last_success': self.stamp, 'error': None},
                 {'name': 'rss_1', 'last_success': None, 'error': 'request failed'}]
        context = compact_news_context(FakeStore([manual], feeds))
        self.assertEqual(context['reports'][0]['verification'], 'manual note; unverified')
        self.assertIn('not supplied', context['reports'][0]['published_label'])
        self.assertTrue(any('30 minutes' in warning for warning in context['warnings']))

    def test_rss_sources_are_not_hidden_by_disabled_legacy_feed_metadata(self):
        feeds = [{'name': name, 'last_success': None, 'error': 'Disabled'} for name in
                 ['availability_coverage', 'espn_news', 'injuries_configured', 'manual_news', 'rss_1', 'rss_2']]
        feeds.append({'name': 'rss_3', 'last_success': self.stamp, 'error': None})
        context = compact_news_context(FakeStore([self.report()], feeds))
        self.assertEqual([f['name'] for f in context['feed_status'][:3]], ['rss_1', 'rss_2', 'rss_3'])
        self.assertFalse(any('No selected collection source' in warning for warning in context['warnings']))
        self.assertLessEqual(len(json.dumps(context)), 6000)

    def test_trades_topic_filters_injuries_and_dates_sort_only_with_known_timezone(self):
        older = (self.now - dt.timedelta(hours=2)).isoformat()
        newest = self.report('Knicks trade talks reportedly ongoing', id='new',
                             url='https://example.com/trade', published_at=self.stamp)
        prior = self.report('Celtics trade for guard', id='old', published_at=older)
        store = FakeStore([prior, self.report(id='injury'), newest], [self.healthy_feed()])
        context = compact_news_context(store, topic='trades')
        self.assertEqual([r['id'] for r in context['reports']], ['new', 'old'])
        self.assertEqual(store.requested_limit, 100)

    def test_payloads_are_bounded_and_links_are_safe(self):
        reports = [self.report('🩺' * 250 + ' injury update', id=str(index),
                               body='Very long injury detail ' * 500,
                               url='https://example.com/' + 'a' * 600) for index in range(30)]
        context = compact_news_context(FakeStore(reports), limit=8)
        self.assertLessEqual(len(json.dumps(context)), 6000)
        self.assertLessEqual(len(news_summary(FakeStore(reports), limit=8)), 3500)
        malicious = self.report(url='javascript:alert(1)')
        self.assertIsNone(compact_news_context(FakeStore([malicious]))['reports'][0]['url'])
        self.assertNotIn('javascript:', news_summary(FakeStore([malicious])))

    def test_unavailable_storage_and_bad_topics_have_clear_behavior(self):
        class FailingStore(FakeStore):
            def recent_news(self, limit=10):
                raise RuntimeError('internal database error')

        summary = news_summary(FailingStore())
        self.assertIn('could not be read', summary)
        self.assertNotIn('internal database error', summary)
        with self.assertRaises(ValueError):
            news_summary(FakeStore(), topic='execute sql')


if __name__ == '__main__':
    unittest.main()
