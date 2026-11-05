"""Private self-test jobs acknowledge immediately and deliver durable final reports."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from master import MasterStore
from telegram_bot import TelegramBot, chunks, RESPONSIVE_COMMANDS


USER=123
REPORT='Self-test: 20 passed, 0 failed, 2 warnings.\nIsolated checks completed; live source gaps remain qualified.'


def update(number,text='/selftest',*,user=USER,kind='private'):
    return {'update_id':number,'message':{'from':{'id':user},'chat':{'id':user,'type':kind},'text':text}}


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():await asyncio.sleep(.002)


class SelfTestBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=MasterStore(Path(self.directory.name)/'fixture.sqlite')
        self.model=AsyncMock(side_effect=AssertionError('Self-test must not use AI'))
        self.bots=[]

    async def asyncTearDown(self):
        for bot in self.bots:await bot.shutdown()

    def bot(self,callback,*,status=None,users=(USER,)):
        bot=TelegramBot(self.store,self.model,token='123456:ISOLATED_TEST',allowed_user_ids=list(users),
            self_test_callback=callback,self_test_status_callback=status)
        self.bots.append(bot);return bot

    def rows(self,table):
        with self.store.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM '+table)]

    async def finish(self,bot):
        await asyncio.gather(*list(bot._self_test_tasks),return_exceptions=True)

    def results(self):
        return [row for row in self.rows('telegram_outbox') if row['event_key'].startswith('selftest-result:')]

    async def test_immediate_ack_virtual_long_job_status_responsiveness_and_one_final_report(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def checks():entered.set();await release.wait();return REPORT
        callback=AsyncMock(side_effect=checks)
        status=AsyncMock(return_value={'phase':'queued','stage':'Waiting for the shared worker pool','elapsed_seconds':0,'active':True})
        bot=self.bot(callback,status=status)
        await asyncio.wait_for(bot.process_update(update(1)),.5)
        await entered.wait()
        self.assertEqual(len(self.rows('telegram_selftest_jobs')),1)
        self.assertIn('started in the background',self.rows('telegram_outbox')[0]['text'])
        self.assertEqual(self.results(),[])
        # Advance the persisted job clock beyond the former60-second reply
        # limit without actually waiting a minute or altering its worker.
        with self.store.connect() as db:
            db.execute('UPDATE telegram_selftest_jobs SET created_at=?',
                       ((datetime.now(timezone.utc)-timedelta(seconds=75)).isoformat(),))
        await asyncio.wait_for(bot.process_update(update(2,'/selftest status')),.5)
        latest=self.rows('telegram_outbox')[-1]['text']
        self.assertIn('Waiting for the shared worker pool',latest)
        self.assertIn('Elapsed: 1m 15s',latest)
        await asyncio.wait_for(bot.process_update(update(3,'/help')),.5)
        help_text=''.join(row['text'] for row in self.rows('telegram_outbox') if row['event_key'].startswith('reply:3:'))
        self.assertIn('/selftest status',help_text)
        await bot.process_update(update(4))
        self.assertIn('already running',self.rows('telegram_outbox')[-1]['text'])
        callback.assert_awaited_once();self.model.assert_not_awaited()
        release.set();await self.finish(bot)
        self.assertEqual([row['text'] for row in self.results()],[REPORT])
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'completed')
        self.assertFalse(await bot.process_update(update(1)))
        self.assertEqual(len(self.results()),1)

    async def test_report_survives_reopen_and_status_never_starts_another_check(self):
        callback=AsyncMock(return_value=REPORT);bot=self.bot(callback)
        await bot.process_update(update(10));await self.finish(bot)
        reopened=self.bot(AsyncMock(side_effect=AssertionError('Status cannot run checks')))
        await reopened.process_update(update(11,'/selftest status'))
        self.assertIn(REPORT,self.rows('telegram_outbox')[-1]['text'])
        reopened._recover_interrupted();self.assertEqual(len(self.results()),1)
        callback.assert_awaited_once()

    async def test_private_allowlist_and_chat_filtering_protect_job_results(self):
        callback=AsyncMock(return_value=REPORT);bot=self.bot(callback,users=(USER,124))
        self.assertFalse(await bot.process_update(update(20,user=999)))
        self.assertFalse(await bot.process_update(update(21,kind='group')))
        self.assertEqual(self.rows('telegram_selftest_jobs'),[])
        callback.assert_not_awaited()
        await bot.process_update(update(22));await self.finish(bot)
        await bot.process_update(update(23,'/selftest status',user=124))
        self.assertNotIn(REPORT,self.rows('telegram_outbox')[-1]['text'])
        self.assertEqual({row['chat_id'] for row in self.results()},{USER})

    async def test_native_busy_finalization_retries_automatically_without_rerunning_checks(self):
        ready,release=asyncio.Event(),asyncio.Event()
        long_report=REPORT+'\n'+('Completed fixture record.\n'*220)
        async def checks():ready.set();await release.wait();return long_report
        callback=AsyncMock(side_effect=checks);bot=self.bot(callback)
        await bot.process_update(update(30));await ready.wait()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            release.set()
            await eventually(lambda:bool(bot._self_test_pending_results))
            await eventually(lambda:bot._database_waiters>0)
            self.assertEqual(self.results(),[])
            visible=await asyncio.wait_for(bot._self_test_status(USER),.5)
            self.assertIn(long_report,visible)
        await self.finish(bot)
        self.assertEqual([row['text'] for row in self.results()],list(chunks(long_report)))
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['result_text'],long_report)
        callback.assert_awaited_once()

    async def test_shutdown_during_finalization_preserves_completed_report_instead_of_interruption(self):
        ready,release=asyncio.Event(),asyncio.Event()
        async def checks():ready.set();await release.wait();return REPORT
        callback=AsyncMock(side_effect=checks);bot=self.bot(callback)
        await bot.process_update(update(40));await ready.wait()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');release.set()
            await eventually(lambda:bool(bot._self_test_pending_results))
            shutdown=asyncio.create_task(bot.shutdown())
            await asyncio.sleep(.02)
            self.assertFalse(shutdown.done())
        await asyncio.wait_for(shutdown,1)
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'completed')
        self.assertEqual([row['text'] for row in self.results()],[REPORT])
        callback.assert_awaited_once()

    async def test_shutdown_and_new_process_recovery_interrupt_once_without_replaying_checks(self):
        entered=asyncio.Event()
        async def pending():entered.set();await asyncio.Event().wait()
        callback=AsyncMock(side_effect=pending);bot=self.bot(callback)
        await bot.process_update(update(50));await entered.wait();await bot.shutdown()
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'interrupted')
        reopened=self.bot(AsyncMock(side_effect=AssertionError('Restart cannot replay checks')))
        reopened._recover_interrupted();reopened._recover_interrupted()
        self.assertEqual(len(self.results()),1)
        # Independently model an abrupt process exit leaving a reserved job.
        with self.store.connect() as db:
            db.execute('INSERT INTO telegram_selftest_jobs VALUES (?,?,?,?,?,?,?)',
                ('orphan',51,USER,'running',datetime.now(timezone.utc).isoformat(),None,None))
        reopened._recover_interrupted();reopened._recover_interrupted()
        self.assertEqual(len(self.results()),2)
        self.assertTrue(all('interrupt' in row['text'].lower() for row in self.results()))
        reopened.self_test_callback.assert_not_awaited()

    async def test_polling_cancellation_and_recovery_preserve_the_owned_check(self):
        entered,release,polling=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def checks():entered.set();await release.wait();return REPORT
        bot=self.bot(AsyncMock(side_effect=checks));bot.client=object()
        async def api(method,payload):
            if method=='getUpdates':polling.set();await asyncio.Event().wait()
            return {'message_id':1}
        bot._api=AsyncMock(side_effect=api)
        loop=asyncio.create_task(bot.run());await polling.wait()
        await bot.process_update(update(60));await entered.wait()
        loop.cancel()
        with self.assertRaises(asyncio.CancelledError):await loop
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'running')
        bot._recover_interrupted()
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'running')
        release.set();await self.finish(bot)
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'completed')
        self.assertEqual(len(self.results()),1)

    async def test_atomic_queue_failure_retries_full_report_once(self):
        bot=self.bot(AsyncMock(return_value=REPORT+'\n'+('Completed fixture check.\n'*220)))
        original=bot._queue_in_db;failed=[]
        def queue(db,chat,text,key,expires_at=None):
            original(db,chat,text,key,expires_at)
            if key.startswith('selftest-result:') and not failed:
                failed.append(True);raise RuntimeError('PRIVATE_TEMPORARY_QUEUE_FAILURE')
        with patch.object(bot,'_queue_in_db',queue):
            await bot.process_update(update(70));await self.finish(bot)
        self.assertEqual([row['text'] for row in self.results()],list(chunks(bot.self_test_callback.return_value)))
        bot.self_test_callback.assert_awaited_once()

    async def test_permanent_save_failure_keeps_readable_report_and_retry_does_not_rerun(self):
        callback=AsyncMock(return_value=REPORT);bot=self.bot(callback)
        error=sqlite3.OperationalError('PRIVATE_SQLITE_DETAILS');error.sqlite_errorcode=sqlite3.SQLITE_READONLY
        with patch.object(bot,'_finish_self_test',side_effect=error):
            await bot.process_update(update(80));await self.finish(bot)
            self.assertEqual(len(bot._self_test_pending_results),1)
            with self.store.connect() as db:db.execute("UPDATE telegram_updates SET status='processing' WHERE update_id=80")
            bot._recover_interrupted()
            self.assertEqual(self.rows('telegram_updates')[0]['status'],'processing')
            self.assertFalse(any(row['event_key'].startswith('interrupted:') for row in self.rows('telegram_outbox')))
            visible=await bot._self_test_status(USER)
            self.assertIn(REPORT,visible);self.assertNotIn('PRIVATE_',visible)
        await bot.process_update(update(81))
        callback.assert_awaited_once()
        self.assertEqual(len(self.rows('telegram_selftest_jobs')),1)
        self.assertEqual([row['text'] for row in self.results()],[REPORT])
        self.assertEqual(self.rows('telegram_selftest_jobs')[0]['status'],'completed')

    async def test_busy_auto_check_failed_report_and_safe_exception_are_honest(self):
        for number,value,expected in [(90,'A self-test is already running. Stage: automatic checks.','busy'),
            (91,'Self-test: 19 passed, 1 failed, 0 warnings.\nFAIL: Fixture assertion','failed')]:
            bot=self.bot(AsyncMock(return_value=value));await bot.process_update(update(number));await self.finish(bot)
            self.assertEqual(self.rows('telegram_selftest_jobs')[-1]['status'],expected)
            self.assertEqual(self.results()[-1]['text'],value)
        bot=self.bot(AsyncMock(side_effect=OSError('PRIVATE_ERROR_MESSAGE')))
        await bot.process_update(update(92));await self.finish(bot)
        self.assertEqual(self.rows('telegram_selftest_jobs')[-1]['status'],'failed')
        self.assertIn('OSError',self.results()[-1]['text']);self.assertNotIn('PRIVATE_',self.results()[-1]['text'])

    async def test_status_is_responsive_at_capacity_and_invalid_arguments_start_nothing(self):
        self.assertIn('/selftest',RESPONSIVE_COMMANDS)
        bot=self.bot(AsyncMock(return_value=REPORT))
        await bot.process_update(update(100,'/selftest extra'))
        await bot.process_update(update(101,'/selftest status'))
        self.assertEqual(self.rows('telegram_selftest_jobs'),[])
        bot.self_test_callback.assert_not_awaited()

    async def test_polling_at_full_command_capacity_still_dispatches_selftest_status(self):
        bot=self.bot(AsyncMock(return_value=REPORT));bot.client=object()
        answering,second_poll=asyncio.Event(),asyncio.Event()
        async def slow_answer(*args):answering.set();await asyncio.Event().wait()
        bot.answer_callback=slow_answer
        calls=[]
        async def api(method,payload):
            if method=='getUpdates':
                calls.append(True)
                if len(calls)==1:
                    return [update(110,'Tell me about basketball'),update(111,'/selftest status')]
                second_poll.set();await asyncio.Event().wait()
            return {'message_id':1}
        bot._api=AsyncMock(side_effect=api)
        with patch('telegram_bot.MAX_ACTIVE_COMMANDS',1):
            polling=asyncio.create_task(bot.run())
            try:
                await asyncio.wait_for(second_poll.wait(),1)
                await asyncio.wait_for(answering.wait(),1)
                status=''.join(row['text'] for row in self.rows('telegram_outbox') if row['event_key'].startswith('reply:111:'))
                self.assertIn('No manual self-test has run yet',status)
                self.assertNotIn('finishing earlier requests',status)
                self.assertEqual(self.rows('telegram_selftest_jobs'),[])
            finally:
                polling.cancel();await asyncio.gather(polling,return_exceptions=True)


if __name__=='__main__':unittest.main()
