"""Undated published aggregates stay distinct from dated partial observations."""
import json
from pathlib import Path
import tempfile
import unittest

from basketball_research import research_context
from feeds import canonical_player
from grounded_reply import evidence_cards, prompt_evidence, render_reply, required_evidence
from master import MasterStore


class EmptyWorkbook:
    sheets = {}


class PublishedResearchSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.master = MasterStore(Path(directory.name) / 'master.sqlite')
        self.master.upsert_game({'id':'sample-game','date':'2026-10-20','home_team':'NYK',
                                 'away_team':'PHI','status':'final'})
        self.master.upsert_player_game({'game_id':'sample-game','date':'2026-10-20',
            'player':'Jalen Brunson','team':'NYK','opponent':'PHI','season':'2026-27',
            'season_type':'Regular Season','appearance_state':'Played','pts':21,'reb':2,'ast':5,
            'source':'https://example.com/observed-game'})
        self.publication = {'entity':'Jalen Brunson','player_id':canonical_player('Jalen Brunson'),
            'category':'Player totals','season':'2026-27','season_type':'Regular Season','gp':5,
            'fields':{'pts':129,'reb':12,'ast':25},'data_through_date':None,
            'published_at':'2026-10-29T14:18:20+00:00',
            'source':'https://example.com/published-player-totals'}
        self.save_publication()
        self.question = 'How does Jalen Brunson match up against Philadelphia and what could affect his scoring?'

    def save_publication(self):
        with self.master.connect() as db:
            db.execute('INSERT OR REPLACE INTO refreshed_tracking VALUES (?,?,?,?)',
                ('2026-27','Jalen Brunson','Player totals',json.dumps(self.publication)))

    def research(self):
        return research_context(EmptyWorkbook(), self.master, {
            'players':['Jalen Brunson'],'teams':['PHI'],'question':self.question,
            'original_question':self.question})

    def test_unknown_coverage_preserves_publication_and_dated_partial_samples_separately(self):
        with self.master.connect() as db:
            before = dict(db.execute("SELECT * FROM season_averages WHERE player='Jalen Brunson'").fetchone())
        self.assertEqual(before['ppg'], 21)
        context = self.research()
        self.assertEqual(context['current_player_statistics'][0]['ppg'], 21)
        publication = next(r for r in context['current_tracking'] if r['category']=='Player totals')
        self.assertEqual(publication['fields']['pts'], 129)
        self.assertIsNone(publication['data_through_date'])
        cards = evidence_cards(context, self.question)
        published = next(c for c in cards if c['kind']=='player_all_opponent_averages')
        observed = next(c for c in cards if c['kind']=='player_observed_sample')
        opponent = next(c for c in cards if c['kind']=='player_opponent_sample')
        self.assertIn('25.8 PPG', published['text'])
        self.assertIn('5 games', published['text'])
        self.assertIn('covered-game date is unknown', published['text'])
        self.assertIn('published 2026-10-29', published['text'])
        self.assertIsNone(published['data_through_date'])
        self.assertIn('21 PPG', observed['text'])
        self.assertIn('Stored sample; coverage may be incomplete', observed['text'])
        self.assertIn('2026-10-20', observed['text'])
        self.assertIn('21 PPG', opponent['text'])
        descriptions = {c['kind']:c['text'] for c in prompt_evidence(cards)}
        self.assertIn('below the published all-opponent snapshot', descriptions['player_opponent_sample'])
        self.assertIn('freshness is unknown', descriptions['player_opponent_sample'])
        self.assertIn('covered-game date is unknown', descriptions['player_all_opponent_averages'])
        required = required_evidence(self.question, cards)
        self.assertIn(published['id'], required)
        self.assertIn(opponent['id'], required)
        result = render_reply({'evidence_ids':[],'analysis':'The scopes differ and current coverage is uncertain.',
                               'needs_more_data':False}, cards, required)
        self.assertIn('25.8 PPG', result['answer'])
        self.assertIn('21 PPG', result['answer'])
        with self.master.connect() as db:
            after = dict(db.execute("SELECT * FROM season_averages WHERE player='Jalen Brunson'").fetchone())
        self.assertEqual(before, after)

    def test_known_older_publication_does_not_displace_newer_observed_sample(self):
        self.publication['data_through_date'] = '2026-10-19'
        self.save_publication()
        context = self.research()
        self.assertEqual(context['current_player_statistics'][0]['ppg'], 21)
        self.assertNotIn('Player totals', [r['category'] for r in context['current_tracking']])
        cards = evidence_cards(context, self.question)
        self.assertNotIn('25.8 PPG', '\n'.join(c['text'] for c in cards))

    def test_known_current_publication_remains_a_single_coverage_labeled_baseline(self):
        self.publication['data_through_date'] = '2026-10-28'
        self.save_publication()
        context = self.research()
        self.assertEqual(context['current_player_statistics'][0]['ppg'], 25.8)
        self.assertNotIn('Player totals', [r['category'] for r in context['current_tracking']])
        cards = evidence_cards(context, self.question)
        baselines = [c for c in cards if c['kind']=='player_all_opponent_averages']
        self.assertEqual(len(baselines), 1)
        self.assertIn('Coverage through 2026-10-28', baselines[0]['text'])
        self.assertNotIn('covered-game date is unknown', baselines[0]['text'])


if __name__ == '__main__':
    unittest.main()
