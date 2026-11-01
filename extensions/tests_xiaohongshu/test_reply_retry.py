"""Real owner message routing and one-shot retries over a stub browser API."""
import hashlib, json, unittest
from unittest.mock import AsyncMock, patch
from test_review_natural import NaturalBase
from test_review_core import OWNER, OWNER_SESSION, UID
from xhs_support import Env, comment, TOKEN
from xiaohongshu.core_client import CoreClient
from xiaohongshu.http_client import McpClient, NotSent, Ambiguous


class OwnerRetryTests(NaturalBase):
    async def test_core_socket_recreation_restores_only_executor_access(self):
        proc = type('Proc', (), {'wait': AsyncMock(return_value=0)})()
        with patch('sys.platform', 'linux'), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=proc)) as spawn:
            await self.svc.grant_xhs_socket_access(self.root / 'core.sock')
            self.assertEqual(spawn.call_args.args, ('/usr/bin/setfacl','-m','u:41001:rw',str(self.root/'core.sock')))
            self.svc.policy['xhs_enabled'] = False
            await self.svc.grant_xhs_socket_access(self.root / 'core.sock')
            self.assertEqual(spawn.call_count, 1)

    async def unknown(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.claim(did, 'unknown')
        return did

    async def test_id_status_routes_to_ledger_and_real_readback_job(self):
        did = await self.unknown()
        answer = await self.say('草稿%s v1发送成功了吗？我怎么没看到' % did)
        self.assertIn('结果未知', answer)
        self.assertIn('已安排一次实际页面复查', answer)
        job = self.rv.check_claim({})['check']
        self.assertEqual(job['draft_id'], did)
        self.assertIsNone(self.rv.check_claim({})['check'])
        with self.assertRaises(ValueError):
            self.rv.check_report({**job, 'token': 'wrong', 'result': 'found'})
        self.rv.check_report({**job, 'result': 'not_found'})
        row = self.rv.db.execute("SELECT text FROM xhs_review_outbox WHERE okey=?", ('readback:' + job['request_id'],)).fetchone()
        self.assertIn('复查已结束', row['text'])
        self.assertEqual(self.status(did), 'unknown')

    async def test_exact_incident_phrase_requests_one_retry_and_replay_is_harmless(self):
        did = await self.unknown()
        text = '草稿%s v1发送成功了吗？我怎么没看到，能重发一下吗' % did
        answer = await self.say(text, mid='retry-message')
        self.assertIn('按原文补发一次', answer)
        self.assertEqual(self.rv.row(did)['delivery_attempt'], 1)
        await self.say(text, mid='retry-message')
        await self.say(text, mid='another-same-request')
        self.assertEqual(self.rv.row(did)['delivery_attempt'], 1)
        self.assertEqual(self.status(did), 'approved')
        self.assertTrue(self.svc.state.get('silent_operation:%s:retry-message' % OWNER_SESSION))
        self.assertEqual(self.llm.prompts, [])

    async def test_status_and_retry_bind_explicit_id_when_other_drafts_exist(self):
        did = await self.unknown()
        other = self.submit(target={'feed_id': 'z'*24, 'comment_id': 'cm9876', 'author_id': 'u'+'b'*11, 'comment_hash': '1'*64})['draft_id']
        self.assertIn('按原文补发一次', await self.say('帮我补发草稿%s v1' % did))
        self.assertEqual(self.status(other), 'pending')
        self.assertIn('没有重复', await self.say('草稿%s再发一次' % did))

    async def test_negative_hypothetical_old_version_other_user_cannot_retry(self):
        did = await self.unknown()
        for phrase in ('不要重发草稿%s', '如果没成功就补发草稿%s', '草稿%s重发了吗'):
            await self.say(phrase % did)
            self.assertEqual(self.status(did), 'unknown')
        self.assertIn('版本不同', await self.say('补发草稿%s v2' % did))
        await self.say('补发草稿%s v1' % did, actor='stranger')
        self.assertEqual(self.status(did), 'unknown')
        self.assertEqual(self.rv.row(did)['delivery_attempt'], 0)

    async def test_verified_draft_never_requeues(self):
        did = await self.unknown()
        self.rv.db.execute("UPDATE xhs_drafts SET status='verified' WHERE draft_id=?", (did,))
        self.assertIn('没有重复', await self.say('重发草稿%s' % did))
        self.assertEqual(self.status(did), 'verified')

    async def test_numeric_subject_selects_right_draft_instead_of_unrelated_pending(self):
        did = await self.pushed(content='3天120w token？真能吃呀')
        await self.say('发布吧')
        self.claim(did, 'unknown')
        other = await self.pushed(target={'feed_id': 'z'*24, 'comment_id': 'cm9876', 'author_id': 'u'+'b'*11, 'comment_hash': '1'*64})
        self.assertIn(did, await self.say('把“3天120万”那条回复补发一次'))
        self.assertEqual(self.status(did), 'approved')
        self.assertEqual(self.status(other), 'pending')


class CompleteRetryFlowTests(NaturalBase):
    async def prepare(self):
        env = Env(self, strict=True, bound_uid=UID)
        env.clock.t = __import__('time').time()
        from xhs_support import OTHER, FEED
        self.policy['xhs_bot_account'] = env.cfg['bound_uid']
        env.service.comments = [comment('cm0001', 'original message', user=OTHER, when=env.clock())]
        did = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0001', 'author_id': OTHER,
                                      'comment_hash': hashlib.sha256(b'original message').hexdigest()})
        env.store.put_token(FEED, TOKEN)
        def core(payload):
            op = payload['op']
            if op.startswith('xhs_draft_'):
                return {'ok': True, **getattr(self.rv, op[len('xhs_draft_'):])(payload)}
            return {'ok': True}
        env.ex.core = CoreClient(transport=core)
        env.ex.identity = lambda force=False: {'ok': True}
        await self.say('发布吧')
        return env, did

    async def test_owner_chat_to_actual_retry_and_restart_no_duplicate(self):
        env, did = await self.prepare()
        env.service.fail = 'ambiguous_lost_before'
        env.ex.deliver_approved()
        self.assertEqual(self.status(did), 'unknown')
        old = env.store.actions()[0]
        env.service.fail = ''
        env.clock.t += 61
        answer = await self.say('草稿%s v1发送成功了吗？我怎么没看到，能重发一下吗' % did)
        self.assertIn('按原文补发一次', answer)
        outcome = env.ex.deliver_approved()
        self.assertEqual(outcome['reported'], 'verified')
        self.assertEqual(self.status(did), 'verified')
        rows = env.store.actions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['retry_of'], old['id'])
        self.assertEqual(len(env.service.comments[0]['subComments']), 1)
        core = env.ex.core
        env.restart()
        env.ex.core, env.ex.identity = core, lambda force=False: {'ok': True}
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        self.assertEqual(len(env.service.comments[0]['subComments']), 1)

    async def test_existing_comment_is_read_back_without_a_second_write(self):
        env, did = await self.prepare()
        env.service.fail = 'ambiguous_lost_before'
        env.ex.deliver_approved()
        # The platform finishes late, after the first readback: retry preflight must find it.
        env.service.comments[0]['subComments'].append(comment('cmfound1', self.rv.row(did) and '谢谢你的留言', user=UID, when=env.clock()))
        env.clock.t += 61
        env.service.fail = ''
        await self.say('补发草稿%s' % did)
        writes = len(env.service.writes())
        result = env.ex.deliver_approved()
        self.assertEqual(result['status'], 'already_present')
        self.assertEqual(len(env.service.writes()), writes)
        self.assertEqual(self.status(did), 'verified')

    async def test_incomplete_readback_does_not_send(self):
        env, did = await self.prepare()
        env.service.fail = 'ambiguous_lost_before'
        env.ex.deliver_approved()
        env.service.comments[0]['subCommentHasMore'] = True
        env.service.fail = ''
        env.clock.t += 61
        await self.say('补发草稿%s' % did)
        writes = len(env.service.writes())
        self.assertEqual(env.ex.deliver_approved()['status'], 'retry_readback_incomplete')
        self.assertEqual(len(env.service.writes()), writes)
        self.assertEqual(self.status(did), 'unknown')


class StageReceiptTests(unittest.TestCase):
    def test_only_explicit_presubmit_error_is_known_not_sent(self):
        def client(details):
            return McpClient('http://127.0.0.1:18060', 'test', transport=lambda *a: (500, json.dumps({'success': False, 'details': details}).encode()))
        with self.assertRaises(NotSent) as caught:
            client('XHS_NOT_SENT:reply_button: timeout').reply('f'*24, TOKEN, 'cm0001', 'hi')
        self.assertEqual(caught.exception.kind, 'reply_not_sent:reply_button')
        for details in ('timeout', 'XHS_NOT_SENT:submit: timeout', 'XHS_NOT_SENT:readback: timeout'):
            with self.assertRaises(Ambiguous):
                client(details).reply('f'*24, TOKEN, 'cm0001', 'hi')

    def test_interrupted_retry_keeps_original_duplicate_guard(self):
        env = Env(self)
        args = dict(feed_id='f'*24, comment_id='cm0001', content='same text', draft_id='ABC234', manual=True,
                    limits={**env.cfg['limits'], 'per_thread_daily': 3})
        old, reason = env.store.reserve('reply', claim_token='old', **args)
        self.assertFalse(reason)
        env.store.finish(old, 'unknown')
        for finish in ('discard', 'failed_not_sent'):
            new, reason = env.store.reserve('reply', claim_token='new', retry_of=old, **args)
            self.assertFalse(reason)
            if finish == 'discard':
                env.store.discard(new)
            else:
                env.store.finish(new, finish)
            self.assertEqual(env.store.action(old)['dedupe_key'], 'reply:cm0001')
        # A second explicit retry after a pre-submit failure follows the chain,
        # while the older unknown attempt remains protected against generic sends.
        again, reason = env.store.reserve('reply', claim_token='third', retry_of=new, **args)
        self.assertFalse(reason)
        env.store.discard(again)
        self.assertEqual(env.store.action(old)['dedupe_key'], 'reply:cm0001')


if __name__ == '__main__':
    unittest.main()
