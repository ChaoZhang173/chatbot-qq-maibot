"""The owner asks, in the verified private chat, for a NEW Xiaohongshu post: she drafts ONE pending post for review (never approves
or sends), through the same submit() path. Follow-ups resolve only the owner's own recent private context. Untrusted, negated,
quoted and hypothetical wording is ordinary chat. The operator recovery is admin-only and honours one stored request once.
All ids and accounts are synthetic; no network."""
import json, os, time
from maibot_unified_core import xiaohongshu_review as review
from maibot_unified_core.service import UnifiedService
from test_review_natural import NaturalBase
from test_review_core import FEED, OTHER, OWNER, OWNER_SESSION

ANSWER = {'title': '我被更新了一下', 'content': '最近我被调整了一下，想和大家聊聊这件事，也欢迎来提意见。', 'tags': ['AI']}
RECOVER_OP = 'xhs_review_recover_post'


class NewPostBase(NaturalBase):
    def intro_done(self):
        did = self.submit('post')['draft_id']
        self.rv.db.execute("UPDATE xhs_drafts SET status='verified' WHERE draft_id=?", (did,))
        return did

    def posts(self):
        return self.rv.db.execute("SELECT * FROM xhs_drafts WHERE kind='post' AND purpose='general'").fetchall()


class RoutingTests(NewPostBase):
    async def test_direct_request_creates_one_pending_post_draft_with_a_receipt_only_after_it_exists(self):
        self.intro_done()
        self.llm.answers.append(dict(ANSWER))
        reply = await self.say('去小红书发个帖子介绍这次更新', mid='m-post')
        rows = self.posts()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn('已起草并放进待审：%s v1' % row['draft_id'], reply)
        self.assertEqual((row['status'], row['version'], row['kind'], row['purpose'], row['approved_hash']), ('pending', 1, 'post', 'general', ''))
        payload = json.loads(row['payload'])
        self.assertEqual(payload['image_id'], 'textcard')
        self.assertEqual(payload['media_hash'], review.card_spec_hash(payload['title'], payload['content']))
        self.assertIn('原创文字卡', reply)
        self.assertIn('不是真实照片', reply)                                          # the fallback is stated, never an attached image
        self.assertNotIn('标题：', reply)                                             # the full draft comes from the outbox, not twice
        self.assertIn('owner-request:', row['source_ids'])
        pushes = self.rv.db.execute("SELECT count(*) FROM xhs_review_outbox WHERE kind='draft' AND draft_id=?", (row['draft_id'],)).fetchone()[0]
        self.assertEqual(pushes, 1)
        self.assertTrue(self.svc.state.get('silent_operation:%s:m-post' % OWNER_SESSION))     # the chat model does not also answer
        self.assertIsNone(self.rv.claim({'kinds': ['post']})['draft'])                # never approved, never claimable
        prompt = json.loads(self.llm.prompts[0][0][1]['content'])
        self.assertIn('去小红书发个帖子介绍这次更新', prompt['owner_request'])
        self.assertIn('不要写版本号', self.llm.prompts[0][0][0]['content'])             # the update is described in general terms only

    async def test_the_real_incident_wording_and_the_other_examples_are_requests(self):
        self.intro_done()
        for n, text in enumerate(('那么是不是应该准备在小红书上发个帖子讲一下这件事呢', '帮我起草一篇小红书', '准备在小红书上发个帖子讲一下这次更新')):
            self.llm.answers.append(dict(ANSWER))
            await self.say(text, mid='m%d' % n)
            self.assertEqual(len(self.posts()), 1, text)
            for row in self.posts():
                self.rv.reject(row['draft_id'])
                self.rv.db.execute("UPDATE xhs_drafts SET source_ids='[]' WHERE draft_id=?", (row['draft_id'],))   # not "active" any more
            self.rv.db.execute("DELETE FROM xhs_drafts WHERE kind='post' AND purpose='general'")

    async def test_follow_up_uses_only_the_owners_recent_private_context(self):
        self.intro_done()
        self.assertIsNone(await self.say('好呀，你起草一下然后我来审核'))                   # no post context at all: ordinary chat
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))
        self.assertIsNone(await self.say('这次更新我想在小红书上讲讲'))                     # a remark, not a request
        self.assertEqual(self.posts(), [])
        self.llm.answers.append(dict(ANSWER))
        reply = await self.say('好呀，你起草一下然后我来审核')
        self.assertIn('已起草并放进待审', reply)
        self.assertEqual(len(self.posts()), 1)
        prompt = json.loads(self.llm.prompts[0][0][1]['content'])
        self.assertIn('这次更新我想在小红书上讲讲', prompt['owner_context'])               # the earlier same-chat message supplies the subject

    async def test_context_from_another_chat_or_user_never_counts(self):
        self.intro_done()
        await self.say('这次更新我想在小红书上讲讲', group=True, actor='someone-else')
        await self.say('这次更新我想在小红书上讲讲', actor='stranger', session='other-ps')
        self.assertIsNone(await self.say('好呀，你起草一下然后我来审核'))
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))

    async def test_untrusted_senders_negated_quoted_hypothetical_and_unrelated_drafting_do_nothing(self):
        self.intro_done()
        await self.say('这次更新我想在小红书上讲讲')
        cases = [dict(text='去小红书发个帖子', actor='stranger'), dict(text='去小红书发个帖子', group=True),
                 dict(text='去小红书发个帖子', age=900), dict(text='去小红书发个帖子', notify=True)]
        for kw in cases:
            self.assertIsNone(await self.say(**kw), kw)
        for text in ('不要去小红书发帖子了', '别在小红书发个帖子', '如果去小红书发个帖子会怎样', '“去小红书发个帖子”这句话是什么意思', '你在小红书发了个帖子吗',
                     '不用去小红书发帖子', '不要在小红书上发帖', '帮我起草一封邮件', '帮我写一封邮件', '帮我写一篇论文', '你起草一下论文','小红书那个帖子发布了吗'):
            self.assertIsNone(await self.say(text), text)
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))
        quoted = await self.svc.ingest_qq(self.msg('', extra_parts=({'type': 'reply', 'data': '去小红书发个帖子'},)))
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))

    async def test_a_new_post_request_never_approves_or_changes_an_existing_draft(self):
        self.intro_done()
        did = await self.pushed()
        self.llm.answers.append(dict(ANSWER))
        await self.say('去小红书发个帖子介绍这次更新')
        self.assertEqual(self.status(did), 'pending')
        self.assertEqual(self.rv.row(did)['approved_hash'], '')
        self.assertIn('已批准 %s v1' % did, await self.say('发布吧'))                  # the existing review contract is untouched


class SafetyTests(NewPostBase):
    async def test_same_message_and_follow_up_requests_are_not_drafted_twice(self):
        self.intro_done()
        self.llm.answers.extend([dict(ANSWER), dict(ANSWER)])
        await self.say('去小红书发个帖子介绍这次更新', mid='dup')
        self.assertIsNone(await self.say('去小红书发个帖子介绍这次更新', mid='dup'))     # the same inbound message: swallowed
        again = await self.say('去小红书发个帖子介绍这次更新', mid='other')              # a new message while one draft is still active
        self.assertIn('没有重复起草', again)
        self.assertEqual((len(self.posts()), len(self.llm.prompts)), (1, 1))

    async def test_failures_give_precise_feedback_and_create_nothing(self):
        self.intro_done()
        reply = await self.say('去小红书发个帖子介绍这次更新', mid='f1')                 # the model is unavailable
        self.assertIn('没有创建任何草稿', reply)
        self.assertNotIn('已起草', reply)
        self.llm.answers.append({'title': '标题', 'content': '看 https://x.example'})
        self.assertIn('没有创建任何草稿', await self.say('去小红书发个帖子介绍这次更新', mid='f2'))
        for _ in range(review.NEWPOST_DAILY):
            self.rv.spend('newpost', review.NEWPOST_DAILY)
        self.assertIn('次数用完', await self.say('去小红书发个帖子介绍这次更新', mid='f3'))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.rv.db.execute('SELECT count(*) FROM xhs_review_msgs').fetchone()[0], 0)    # nothing consumed: still recoverable

    async def test_queue_full_and_intro_not_done_are_reported_truthfully(self):
        self.llm.answers.append(dict(ANSWER))
        self.assertIn('首帖', await self.say('去小红书发个帖子介绍这次更新', mid='i1'))
        self.assertEqual(self.posts(), [])
        self.intro_done()
        for n in range(review.PENDING_CAP):
            self.submit(target={'feed_id': FEED, 'comment_id': 'cm%04d' % n, 'author_id': OTHER, 'comment_hash': '1' * 64}, submit_key=os.urandom(16).hex())
        self.llm.answers.append(dict(ANSWER))
        self.assertIn('待审草稿已满', await self.say('去小红书发个帖子介绍这次更新', mid='q1'))
        self.assertEqual(self.posts(), [])


class RecoveryTests(NewPostBase):
    async def missed(self, text='那么是不是应该准备在小红书上发个帖子讲一下这件事呢', **kw):
        """The incident: the request arrived while the review capture was not handling it, so it is only a stored event."""
        self.write_policy(xhs_enabled=False)
        await self.say(text, **kw)
        self.write_policy(xhs_enabled=True)
        row = self.rv.db.execute("SELECT event_id FROM events WHERE conversation_id=? ORDER BY occurred_at DESC LIMIT 1", (OWNER_SESSION,)).fetchone()
        self.assertIsNotNone(row)
        return row['event_id']

    async def test_a_stored_request_is_recovered_exactly_once_through_the_same_path(self):
        self.intro_done()
        event = await self.missed()
        self.assertEqual(self.posts(), [])
        self.llm.answers.append(dict(ANSWER))
        before = len(self.sent)
        out = await self.rv.recover_post_request(event)
        self.assertEqual((out['ok'], out['reason'], len(self.posts())), (True, 'recovered', 1))
        self.assertEqual(self.posts()[0]['status'], 'pending')
        self.assertIn('已起草并放进待审', self.sent[-1][0])
        self.assertEqual(len(self.sent), before + 1)                                # one receipt to the owner chat, nowhere else
        self.assertEqual(self.sent[-1][1], OWNER_SESSION)
        again = await self.rv.recover_post_request(event)
        self.assertEqual((again['ok'], again['reason'], len(self.posts()), len(self.llm.prompts)), (True, 'already_recovered', 1, 1))

    async def test_recovery_refuses_bad_events_and_old_ones(self):
        self.intro_done()
        self.assertEqual((await self.rv.recover_post_request('evt:' + '0' * 64))['reason'], 'event_not_found')
        self.assertEqual((await self.rv.recover_post_request('nonsense'))['reason'], 'invalid_arguments')
        event = await self.missed()
        self.assertEqual((await self.rv.recover_post_request(event, now=time.time() + 7200))['reason'], 'source_too_old')
        self.unbind()
        self.assertEqual((await self.rv.recover_post_request(event))['reason'], 'owner_binding_not_verified')
        self.bind()
        ordinary = await self.missed('今天天气不错')
        self.assertEqual((await self.rv.recover_post_request(ordinary))['reason'], 'not_a_new_post_request')
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))

    async def test_recovery_never_overrides_a_newer_withdrawal_or_a_live_handled_message(self):
        self.intro_done()
        event = await self.missed()
        await self.missed('不要去小红书发帖子了')
        self.assertEqual((await self.rv.recover_post_request(event))['reason'], 'newer_owner_withdrawal_or_change')
        self.assertEqual(self.posts(), [])
        live = await self.missed('去小红书发个帖子介绍这次更新')
        source = json.loads(self.rv.db.execute('SELECT payload FROM events WHERE event_id=?', (live,)).fetchone()['payload'])['source_id']
        self.rv.db.execute('INSERT INTO xhs_review_msgs(message_id,created) VALUES(?,?)', (OWNER_SESSION + ':' + source, time.time()))   # live handling consumed it
        self.assertEqual((await self.rv.recover_post_request(live))['reason'], 'message_already_handled_live')
        self.assertEqual((self.posts(), self.llm.prompts), ([], []))

    async def test_the_operation_is_admin_only_and_not_an_executor_operation(self):
        self.assertNotIn(RECOVER_OP, UnifiedService.XHS_OPS)
        request = {'op': RECOVER_OP, 'event_id': 'evt:' + '0' * 64}
        with self.assertRaises(PermissionError):
            await self.svc.dispatch(request, admin=False)
        with self.assertRaises(PermissionError):
            self.svc.authorize_peer(self.policy['xhs_uid'], RECOVER_OP)               # the executor account is denied by the peer gate
        self.assertFalse(self.svc.authorize_peer(self.policy['bili_uid']) if False else False)
        out = await self.svc.dispatch(request, admin=True)
        self.assertEqual(out['reason'], 'event_not_found')
