"""Telegram exposes bounded performance fields, never raw model diagnostics."""
import json
from pathlib import Path
import tempfile
import unittest

from telegram_bot import TelegramBot, safe_ai_request_details
from test_telegram import FakeStore, TOKEN, USER_ID, update


class DiagnosticFilteringTests(unittest.TestCase):
    def test_unknown_text_invalid_numbers_and_wrong_types_are_removed(self):
        raw = {
            'elapsed_seconds': float('nan'), 'model_load_seconds': float('inf'),
            'prompt_eval_seconds': -1, 'generation_seconds': True,
            'prompt_chars': 'PRIVATE TEXT', 'evidence_count': 1.5,
            'omitted_evidence_count': -2, 'prompt_tokens': True, 'generated_tokens': None,
            'cache_hit': 'PRIVATE TOKEN', 'phase': 'PRIVATE URL',
            'status': {'private': 'data'}, 'finish_reason': 'PRIVATE MODEL RESPONSE',
            'failure_category': 'model_http_500 PRIVATE URL',
            'prompt': 'PRIVATE PROMPT', 'response': 'PRIVATE RESPONSE', 'token': TOKEN,
        }
        self.assertEqual(safe_ai_request_details(raw), {})
        self.assertEqual(safe_ai_request_details('PRIVATE'), {})

    def test_http_category_is_code_only_and_counts_are_strict_integers(self):
        self.assertEqual(safe_ai_request_details({'failure_category': 'model_http_503',
                         'prompt_tokens': 12, 'generated_tokens': 0, 'cache_hit': False}),
                         {'failure_category': 'model_http_503', 'prompt_tokens': 12,
                          'generated_tokens': 0, 'cache_hit': False})
        self.assertEqual(safe_ai_request_details({'failure_category': 'model_http_999'}), {})

    def test_repair_diagnostics_use_strict_counts_and_fixed_reason_codes(self):
        self.assertEqual(safe_ai_request_details({
            'model_calls': 2, 'repair_attempts': 1, 'status': 'repairing',
            'validation_reason': 'numerical_prose', 'raw_reply': 'PRIVATE MODEL REPLY'}), {
            'model_calls': 2, 'repair_attempts': 1, 'status': 'repairing',
            'validation_reason': 'numerical_prose'})
        for reason in ('invalid_json', 'invalid_schema', 'invalid_evidence', 'analysis_too_long',
                       'retrieval_with_analysis', 'empty_reply', 'numerical_prose', 'unsupported_assignment', 'unsupported_scope'):
            self.assertEqual(safe_ai_request_details({'validation_reason': reason}),
                             {'validation_reason': reason})
        self.assertEqual(safe_ai_request_details({
            'model_calls': True, 'repair_attempts': -1, 'status': 'PRIVATE URL',
            'validation_reason': 'invalid_json PRIVATE RESPONSE'}), {})


class TelegramDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_status_delivers_safe_request_timings_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FakeStore(Path(directory) / 'telegram.sqlite')
            status_calls = []

            async def answer(*args, **kwargs):
                self.fail('/ai_status must not invoke inference')

            async def status():
                status_calls.append(True)
                return {'phase': 'ready', 'model': 'qwen2.5:3b', 'active': False,
                    'last_request': {'phase': 'evidence_reply', 'status': 'completed',
                        'elapsed_seconds': 7.1254, 'model_load_seconds': 1.25,
                        'prompt_eval_seconds': 2.5, 'generation_seconds': 3.3,
                        'prompt_chars': 1800, 'evidence_count': 4, 'omitted_evidence_count': 2,
                        'prompt_tokens': 450, 'generated_tokens': 50,
                        'cache_hit': False, 'finish_reason': 'stop',
                        'url': 'PRIVATE MODEL URL', 'response': 'PRIVATE MODEL RESPONSE'}}

            bot = TelegramBot(store, answer, token=TOKEN, allowed_user_ids=[USER_ID],
                              model_status_callback=status)
            await bot.process_update(update(1000, '/ai_status'))
            self.assertEqual(status_calls, [True])
            with bot._db() as db:
                text = db.execute('SELECT text FROM telegram_outbox').fetchone()['text']
            report = json.loads(text.split('\n', 1)[1])
            self.assertEqual(report['phase'], 'ready')
            self.assertEqual(report['last_request'], {
                'phase': 'evidence_reply', 'status': 'completed', 'elapsed_seconds': 7.125,
                'model_load_seconds': 1.25, 'prompt_eval_seconds': 2.5, 'generation_seconds': 3.3,
                'prompt_chars': 1800, 'evidence_count': 4, 'omitted_evidence_count': 2,
                'prompt_tokens': 450, 'generated_tokens': 50, 'cache_hit': False, 'finish_reason': 'stop'})
            self.assertNotIn('PRIVATE', text)


if __name__ == '__main__':
    unittest.main()
