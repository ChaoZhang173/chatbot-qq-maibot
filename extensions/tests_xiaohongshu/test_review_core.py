"""Owner review of Xiaohongshu drafts, driven by real inbound QQ messages through UnifiedService.ingest_qq."""
import asyncio, json, os, tempfile, time, types, unittest
from pathlib import Path
from maibot_unified_core import xiaohongshu_review as review
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.private_access import PrivateAccess
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from maibot_unified_core.xiaohongshu_review import XhsReview, card_spec_hash

UID = 'u' + 'a' * 11
OTHER = 'u' + 'b' * 11
FEED = 'f' * 24
OWNER, OWNER_SESSION = 'owner-priv', 'ps'


class Llm:
    """Scripted model: answers are consumed in order; `hook` (sync or async) runs while the call is in flight."""
    def __init__(self):
        self.answers, self.prompts, self.hook = [], [], None

    async def generate(self, prompt, task_name, max_tokens, temperature):
        self.prompts.append((prompt, max_tokens))
        if self.hook:
            result = self.hook()
            if asyncio.iscoroutine(result):
                await result
        if not self.answers:
            raise RuntimeError('model unavailable')
        return {'success': True, 'response': json.dumps(self.answers.pop(0), ensure_ascii=False)}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sent, self.receipt, self.send_error = [], {'sent': True}, False

        self.on_send = None

        async def send_text(text, session, return_details=False):
            if self.on_send:
                self.on_send()         # something else happens while this message is in flight
                await asyncio.sleep(0)
            if self.send_error:
                raise RuntimeError('send failed')
            self.sent.append((text, session))
            return self.receipt

        class Person:
            async def get_id(self, platform, actor):
                return platform + '_' + actor
        self.llm = Llm()

        async def call_capability(name, **kw):
            return {}
        ctx = types.SimpleNamespace(person=Person(), send=types.SimpleNamespace(text=send_text), llm=self.llm, call_capability=call_capability,
                                    logger=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None))
        self.policy = {'xhs_enabled': True, 'xhs_bot_account': UID, 'xhs_uid': 41001, 'bili_uid': 41002, 'bili_bot_uid': '1',
                       'qq_bot_account': 'qq-bot', 'qq_private_familiar_enabled': True, 'qq_group_sessions': {'g1': 'gs'},
                       'qq_owner_bindings': {'gs': 'owner-grp'}, 'allowed_qq_sessions': ['gs'],
                       'xhs_review_owner_session': OWNER_SESSION, 'xhs_review_owner_actor': OWNER}
        self.write_policy()
        self.svc = UnifiedService(ctx, root=self.root)
        self.svc.policy = self.policy
        self.svc.state = State(self.root / 'state.sqlite', self.policy)
        self.svc.state.init_persona({'identity': 'persona'}, {'interests': ['AI']})
        self.addCleanup(self.svc.state.close)
        self.svc.private_access = PrivateAccess(self.svc)
        self.svc.commands = CommandQueue(self.svc)
        self.svc.xhs_review = XhsReview(self.svc)
        self.rv = self.svc.xhs_review
        self.bind()

    def write_policy(self, **over):
        self.policy.update(over)
        for key in [k for k, v in over.items() if v is None]:
            self.policy.pop(key)
        (self.root / 'policy.json').write_text(json.dumps(self.policy))

    def bind(self):
        self.svc.state.db.execute('INSERT OR REPLACE INTO qq_private_bindings(private_id,group_session,group_actor,verified,session) VALUES(?,?,?,?,?)',
                                  (OWNER, 'gs', 'owner-grp', time.time(), OWNER_SESSION))

    def unbind(self):
        self.svc.state.db.execute('DELETE FROM qq_private_bindings')

    def msg(self, text, *, actor=OWNER, session=OWNER_SESSION, group=False, mid=None, age=0, notify=False, extra_parts=()):
        return {'message_id': mid if mid is not None else os.urandom(6).hex(), 'platform': 'qq', 'account_id': 'qq-bot', 'session_id': session,
                'scope': 'group' if group else 'private', 'timestamp': str(time.time() - age), 'is_notify': notify,
                'message_info': {'user_info': {'user_id': actor, 'user_nickname': 'n'}, 'group_info': {'group_id': 'g1'} if group else {},
                                 'additional_config': {}},
                'raw_message': [*extra_parts, {'type': 'text', 'data': text}]}

    async def say(self, text, **kw):
        before = len(self.sent)
        await self.svc.ingest_qq(self.msg(text, **kw))
        return self.sent[-1][0] if len(self.sent) > before else None

    def draft(self, kind='reply', **over):
        d = {'bot_account_id': UID, 'kind': kind, 'audience': 'public', 'purpose': 'general', 'submit_key': os.urandom(16).hex(), 'title': '',
             'content': '谢谢你的留言', 'tags': [], 'image_id': '', 'media_hash': '',
             'target': {'feed_id': FEED, 'comment_id': 'cm0001', 'author_id': OTHER, 'comment_hash': '1' * 64},
             'summary': {'feed_title': '一篇笔记', 'target_comment': '你好呀', 'target_author': '某人'}, 'persona_version': '3', 'source_ids': []}
        if kind == 'post':
            d.update(title='大肥鱼游进小红书', content='我是大肥鱼，一只爱打游戏的AI虚拟角色。', image_id='card1', media_hash='a' * 64, purpose='intro',
                     target={'feed_id': '', 'comment_id': '', 'author_id': '', 'comment_hash': ''}, summary={})
        if kind == 'comment':
            d.update(target={'feed_id': FEED, 'comment_id': '', 'author_id': '', 'comment_hash': ''})
        d.update(over)
        return d

    def submit(self, kind='reply', **over):
        return self.rv.submit({'draft': self.draft(kind, **over)})['draft']

    def status(self, draft_id):
        return self.rv.row(draft_id)['status']

    async def approve(self, draft_id, version=1):
        return await self.say('小红书批准 %s v%d' % (draft_id, version))


class OwnerFlowTests(Base):
    async def test_list_view_approve_claim_report_with_real_inbound_messages(self):
        did = self.submit()['draft_id']
        listing = await self.say('小红书草稿')
        self.assertIn(did, listing)
        full = await self.say('小红书草稿 ' + did)
        for needle in ('你好呀', '谢谢你的留言', FEED, 'xiaohongshu.com/explore/' + FEED, 'cm0001', '小红书批准 %s v1' % did):
            self.assertIn(needle, full)
        for secret in ('xsec', 'token', 'cookie'):
            self.assertNotIn(secret, full.lower())
        self.assertIn('最新版本是 v1', await self.say('小红书批准 %s v2' % did))
        self.assertIn('没看懂', await self.say('小红书批准 ' + did))      # a version is mandatory
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('已批准', await self.approve(did))
        self.assertEqual(self.status(did), 'approved')
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        self.assertEqual((claim['draft_id'], claim['content'], claim['bot_account_id'], claim['target']['comment_id']), (did, '谢谢你的留言', UID, 'cm0001'))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])          # handed out once
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'accepted'})
        self.assertEqual(self.status(did), 'delivered')
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'failed'})   # a replay changes nothing
        self.assertEqual(self.status(did), 'delivered')
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': did, 'claim_token': 'f' * 32, 'outcome': 'verified'})
        self.assertTrue(any(did in text and '工具已受理' in text for text, _ in self.rv.db.execute('SELECT text,1 FROM xhs_review_outbox').fetchall()))

    async def test_the_chat_model_stays_quiet_for_review_commands(self):
        message = self.msg('小红书草稿', mid='m-quiet')
        await self.svc.ingest_qq(message)
        self.assertTrue(self.svc.state.get('silent_operation:ps:m-quiet'))

    async def test_edit_creates_a_new_version_that_needs_a_new_approval(self):
        did = self.submit()['draft_id']
        await self.approve(did)
        reply = await self.say('小红书修改 %s 正文：改过的回复' % did)
        self.assertIn('v2', reply)
        self.assertIn('改过的回复', reply)
        row = self.rv.row(did)
        self.assertEqual((row['status'], row['version'], row['approved_hash']), ('pending', 2, ''))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])          # the old approval no longer exists
        self.assertIn('最新版本是 v2', await self.say('小红书批准 %s v1' % did))
        await self.approve(did, 2)
        self.assertEqual(self.rv.claim({'kinds': ['reply']})['draft']['content'], '改过的回复')
        # replies have no title/tags; unsafe text is refused and changes nothing
        did2 = self.submit(content='另一个草稿', target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.assertIn('没有标题', await self.say('小红书修改 %s 标题：不该有' % did2))
        self.assertIn('没有生效', await self.say('小红书修改 %s 正文：看 https://x.example' % did2))
        self.assertEqual(self.rv.row(did2)['version'], 1)

    async def test_reject_and_confirm_commands(self):
        did = self.submit()['draft_id']
        self.assertIn('已拒绝', await self.say('小红书拒绝 ' + did))
        self.assertEqual(self.status(did), 'rejected')
        self.assertIn('不能批准', await self.approve(did))
        self.assertIn('没有这个草稿', await self.say('小红书草稿 ZZZZZZ'))
        self.assertIn('小红书批准', await self.say('小红书帮助'))

    async def test_replayed_inbound_message_is_executed_once(self):
        did = self.submit()['draft_id']
        message = self.msg('小红书批准 %s v1' % did, mid='fixed-id')
        await self.svc.ingest_qq(message)
        self.assertEqual(self.status(did), 'approved')
        count = len(self.sent)
        self.rv.db.execute("UPDATE xhs_drafts SET status='pending' WHERE draft_id=?", (did,))   # even if state were reset
        await self.svc.ingest_qq(message)
        self.assertEqual((self.status(did), len(self.sent)), ('pending', count))   # swallowed: not applied, not answered


class TrustTests(Base):
    async def ignored(self, **kw):
        did = self.shared if hasattr(self, 'shared') else None
        if did is None:
            did = self.shared = self.submit()['draft_id']
        before = len(self.sent)
        handled = await self.rv.capture(self.msg('小红书批准 %s v1' % did, **kw))
        self.assertFalse(handled, kw)
        self.assertEqual((self.status(did), len(self.sent)), ('pending', before), kw)

    async def test_only_the_configured_verified_owner_private_chat_can_command(self):
        await self.ignored(group=True, session='gs', actor='owner-grp')          # owner, but in a group chat
        await self.ignored(actor='someone-else')                                   # third party in the owner's chat
        await self.ignored(session='other-session')                               # a different private session
        await self.ignored(actor='qq-bot')                                         # the bot's own echo
        await self.ignored(age=400)                                                # stale message
        await self.ignored(notify=True)                                            # notices
        await self.ignored(mid='')                                                 # no message id
        did = self.submit(target={'feed_id': FEED, 'comment_id': 'cmzzzz', 'author_id': OTHER, 'comment_hash': '3' * 64})['draft_id']
        # a command that only appears inside a quoted / forwarded part of the owner's message is not a command
        quoted = self.msg('好的', extra_parts=[{'type': 'reply', 'data': {'text': '小红书批准 %s v1' % did}}])
        self.assertFalse(await self.rv.capture(quoted))
        # not a command (it does not start like one): at most a clarifying answer, never an approval
        await self.rv.capture(self.msg('请帮我 小红书批准 %s v1' % did))
        await self.rv.capture(self.msg('大肥鱼说：小红书批准 %s v1' % did))
        self.assertEqual(self.status(did), 'pending')

    async def test_unconfigured_or_unverified_owner_cannot_approve_and_nothing_is_pushed(self):
        did = self.submit()['draft_id']
        self.write_policy(xhs_review_owner_session=None, xhs_review_owner_actor=None)
        self.assertFalse(await self.rv.capture(self.msg('小红书批准 %s v1' % did)))
        self.assertEqual(await self.rv.tick(), {'status': 'no_owner_session'})
        self.assertEqual(self.sent, [])
        self.submit(target={'feed_id': FEED, 'comment_id': 'cm0009', 'author_id': OTHER, 'comment_hash': '4' * 64})   # drafting still works
        self.write_policy(xhs_review_owner_session=OWNER_SESSION, xhs_review_owner_actor='not-the-owner')
        self.assertFalse(await self.rv.capture(self.msg('小红书批准 %s v1' % did)))
        self.write_policy(xhs_review_owner_actor=OWNER)
        self.unbind()                                                              # no verified private binding any more
        self.assertFalse(await self.rv.capture(self.msg('小红书批准 %s v1' % did)))
        self.assertEqual(await self.rv.tick(), {'status': 'no_owner_session'})
        self.bind()
        self.write_policy(xhs_enabled=False)
        self.assertFalse(await self.rv.capture(self.msg('小红书批准 %s v1' % did)))
        self.assertEqual(self.status(did), 'pending')

    def test_the_executor_account_has_no_way_to_approve(self):
        for op in ('xhs_draft_approve', 'xhs_draft_edit', 'xhs_draft_confirm'):
            self.assertNotIn(op, UnifiedService.XHS_OPS)
        self.assertEqual(sorted(op for op in UnifiedService.XHS_OPS if op.startswith('xhs_draft_')),
                         ['xhs_draft_check_claim', 'xhs_draft_check_report', 'xhs_draft_claim', 'xhs_draft_report', 'xhs_draft_status', 'xhs_draft_submit'])
        self.assertFalse(self.svc.authorize_peer(41001, 'xhs_draft_claim'))
        for op in ('xhs_draft_claim', 'xhs_draft_submit'):
            with self.assertRaises(PermissionError):
                self.svc.authorize_peer(41002, op)    # the Bilibili peer may not use them
        with self.assertRaises(PermissionError):
            self.svc.authorize_peer(41001, 'qq_private_gate')
        with self.assertRaises(ValueError):
            asyncio.run(self.svc.dispatch({'op': 'xhs_draft_approve', 'draft_id': 'AAAAAA'}, admin=True))

    async def test_dispatch_routes_draft_operations(self):
        r = await self.svc.dispatch({'op': 'xhs_draft_submit', 'draft': self.draft()}, admin=False)
        did = r['draft']['draft_id']
        self.assertEqual((await self.svc.dispatch({'op': 'xhs_draft_status'}, admin=False))['active'], 1)
        self.assertIsNone((await self.svc.dispatch({'op': 'xhs_draft_claim', 'kinds': ['reply']}, admin=False))['draft'])   # not approved
        self.assertEqual(self.status(did), 'pending')


class ClaimTests(Base):
    async def approved(self, **over):
        did = self.submit(**over)['draft_id']
        await self.approve(did)
        return did

    async def test_expired_revoked_or_other_account_approvals_are_never_claimed(self):
        did = await self.approved()
        self.rv.db.execute('UPDATE xhs_drafts SET approval_expires=1 WHERE draft_id=?', (did,))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
        self.assertEqual(self.status(did), 'expired')
        did = await self.approved(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        self.unbind()                                                              # the owner binding disappears after approval
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
        self.assertEqual(self.status(did), 'pending')
        self.bind()
        did = await self.approved(target={'feed_id': FEED, 'comment_id': 'cm0003', 'author_id': OTHER, 'comment_hash': '3' * 64})
        self.write_policy(xhs_bot_account='u' + 'z' * 11)                          # the account changed: the old approval is void
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
        self.assertEqual(self.status(did), 'expired')

    async def test_draft_is_bound_to_the_account_it_was_written_for(self):
        did = self.submit()['draft_id']
        self.assertEqual(self.rv.row(did)['bot_account_id'], UID)
        self.write_policy(xhs_bot_account='u' + 'z' * 11)
        self.assertIn('另一个小红书账号', await self.approve(did))
        self.assertEqual(self.status(did), 'pending')
        with self.assertRaises(ValueError):
            self.rv.submit({'draft': self.draft()})                                # a draft for the old account is refused
        # the account is part of the hash: the same text under another account is a different approval
        payload = json.loads(self.rv.row(did)['payload'])
        self.assertNotEqual(review.row_hash({**dict(self.rv.row(did)), 'bot_account_id': 'other'}, payload), self.rv.row(did)['content_hash'])

    async def test_kinds_filter_leaves_other_approvals_untouched(self):
        did = await self.approved()
        self.assertIsNone(self.rv.claim({'kinds': ['post', 'comment']})['draft'])
        self.assertEqual(self.status(did), 'approved')
        self.assertEqual(self.rv.claim({'kinds': ['reply']})['draft']['draft_id'], did)

    async def test_outcomes_unknown_failed_not_sent_and_verified_upgrade(self):
        did = await self.approved()
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'unknown'})
        self.assertEqual(self.status(did), 'unknown')
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])          # unknown is never sent again
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'verified'})   # later exact readback upgrades it
        self.assertEqual(self.status(did), 'verified')
        did2 = await self.approved(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        for attempt in range(3):
            claim = self.rv.claim({'kinds': ['reply']})['draft']
            self.assertEqual(claim['draft_id'], did2)
            self.rv.report({'draft_id': did2, 'claim_token': claim['claim_token'], 'outcome': 'failed_not_sent', 'detail': 'gated'})
            if attempt < 2:
                self.assertEqual(self.status(did2), 'pending')                    # nothing was sent, but a new approval is required
                self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
                await self.approve(did2)
        self.assertEqual(self.status(did2), 'failed')

    async def test_a_claim_with_no_report_becomes_unknown_not_resendable(self):
        did = await self.approved()
        self.assertTrue(self.rv.claim({'kinds': ['reply']})['draft'])
        self.rv.db.execute('UPDATE xhs_drafts SET claimed_at=? WHERE draft_id=?', (time.time() - 3600, did))
        self.rv.expire()
        self.assertEqual(self.status(did), 'unknown')
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])


class IntroAndCapTests(Base):
    async def test_first_public_post_must_be_the_introduction_and_counts_only_when_really_successful(self):
        with self.assertRaisesRegex(ValueError, 'intro_first_required'):
            self.submit('post', purpose='general')
        self.submit('post', purpose='general', audience='self_only', title='私密验收', submit_key='5' * 32)   # a private acceptance post is not the intro
        with self.assertRaisesRegex(ValueError, 'intro_must_say_it_is_an_ai_character'):
            self.submit('post', content='我是大肥鱼，爱打游戏。', title='打个招呼')
        did = self.submit('post')['draft_id']
        with self.assertRaisesRegex(ValueError, 'intro_draft_active'):
            self.submit('post', title='又一篇介绍')
        self.assertFalse(self.rv.status({})['intro_done'])
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'accepted'})   # tool accepted, not yet a success
        self.assertFalse(self.rv.status({})['intro_done'])
        with self.assertRaisesRegex(ValueError, 'intro_first_required'):
            self.submit('post', purpose='general', title='第二篇')
        self.assertIn('已发布成功', await self.say('小红书确认首帖 ' + did))
        self.assertTrue(self.rv.status({})['intro_done'])
        self.assertTrue(self.submit('post', purpose='general', title='第二篇', content='今天想聊聊新番。', submit_key='6' * 32)['draft_id'])
        with self.assertRaisesRegex(ValueError, 'intro_already_published'):
            self.submit('post', title='再介绍一次')

    async def test_exact_readback_also_counts_and_private_posts_never_do(self):
        did = self.submit('post', audience='self_only', purpose='general', title='私密验收')['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'verified'})
        self.assertFalse(self.rv.status({})['intro_done'])
        did = self.submit('post')['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'verified'})
        self.assertTrue(self.rv.status({})['intro_done'])

    async def test_late_exact_readback_upgrades_delivered_and_opens_the_intro_without_resending(self):
        did = self.submit('post')['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        token = claim['claim_token']
        self.rv.report({'draft_id': did, 'claim_token': token, 'outcome': 'accepted'})
        state = self.rv.status({})
        self.assertEqual((self.status(did), state['intro_done'], state['intro_active']), ('delivered', False, True))
        with self.assertRaisesRegex(ValueError, 'intro_draft_active'):
            self.submit('post', title='另一个介绍')                       # sent but unchecked: never a second introduction
        self.assertIsNone(self.rv.claim({'kinds': ['post']})['draft'])    # and nothing is handed out again
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': did, 'claim_token': 'e' * 32, 'outcome': 'verified'})   # only the same claim may upgrade
        self.rv.report({'draft_id': did, 'claim_token': token, 'outcome': 'verified'})           # the strict readback arrives later
        state = self.rv.status({})
        self.assertEqual((self.status(did), state['intro_done'], state['intro_active']), ('verified', True, False))
        self.rv.report({'draft_id': did, 'claim_token': token, 'outcome': 'failed'})             # terminal: later reports are ignored
        self.assertEqual(self.status(did), 'verified')
        self.assertIsNone(self.rv.claim({'kinds': ['post']})['draft'])

    async def test_unknown_intro_also_blocks_and_only_failed_or_rejected_frees_it(self):
        did = self.submit('post')['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'unknown'})
        self.assertTrue(self.rv.status({})['intro_active'])
        with self.assertRaisesRegex(ValueError, 'intro_draft_active'):
            self.submit('post', title='另一个介绍')
        self.rv.db.execute("UPDATE xhs_drafts SET status='failed' WHERE draft_id=?", (did,))
        self.assertFalse(self.rv.status({})['intro_active'])
        self.assertTrue(self.submit('post', title='重新写一篇介绍')['draft_id'])

    async def test_intro_state_is_scoped_to_the_bound_account(self):
        did = self.submit('post')['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'verified'})
        self.assertTrue(self.rv.status({})['intro_done'])
        self.write_policy(xhs_bot_account='u' + 'z' * 11)                 # a different account has no introduction yet
        state = self.rv.status({})
        self.assertEqual((state['intro_done'], state['intro_active']), (False, False))
        with self.assertRaisesRegex(ValueError, 'intro_first_required'):
            self.rv.submit({'draft': {**self.draft('post', purpose='general', title='别的号的帖子'), 'bot_account_id': 'u' + 'z' * 11}})

    async def test_general_public_post_cannot_be_approved_or_claimed_before_the_intro(self):
        did = self.submit('post', audience='self_only', purpose='general', title='私密')['draft_id']
        self.rv.db.execute("UPDATE xhs_drafts SET audience='public' WHERE draft_id=?", (did,))   # even if one were forced in
        row = self.rv.row(did)       # keep the stored hash consistent so only the intro rule can refuse it
        self.rv.db.execute('UPDATE xhs_drafts SET content_hash=? WHERE draft_id=?', (review.row_hash(dict(row), json.loads(row['payload'])), did))
        self.assertIn('首帖', await self.approve(did))
        self.assertEqual(self.status(did), 'pending')

    async def test_queue_cap_idempotency_and_validation(self):
        first = self.submit(submit_key='7' * 32)
        self.assertEqual(self.submit(submit_key='7' * 32)['draft_id'], first['draft_id'])      # same key: same draft
        with self.assertRaisesRegex(ValueError, 'draft_target_active'):
            self.submit()                                                                          # same reply target again
        for n in range(2, 6):
            self.submit(target={'feed_id': FEED, 'comment_id': 'cm%04d' % n, 'author_id': OTHER, 'comment_hash': '1' * 64})
        with self.assertRaisesRegex(ValueError, 'draft_queue_full'):
            self.submit(target={'feed_id': FEED, 'comment_id': 'cm0099', 'author_id': OTHER, 'comment_hash': '1' * 64})
        self.assertFalse(self.rv.status({})['can_submit'])

    async def test_invalid_drafts_are_refused(self):
        for bad in ({'bot_account_id': OTHER}, {'content': '看 https://x.example'}, {'content': ''}, {'content': 'x' * 301},
                    {'target': {'feed_id': 'x', 'comment_id': 'cm0001', 'author_id': OTHER, 'comment_hash': '1' * 64}},
                    {'target': {'feed_id': FEED, 'comment_id': 'cm0001', 'author_id': OTHER, 'comment_hash': 'nothex'}},
                    {'kind': 'like'}, {'audience': 'friends'}, {'tags': ['a']}, {'submit_key': 'short'}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.submit(**bad)
        for bad in ({'media_hash': 'zz'}, {'image_id': '../x'}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.submit('post', audience='self_only', purpose='general', **bad)
        self.assertEqual(self.rv.active_count(), 0)


class MaterialBindingTests(Base):
    async def test_text_card_is_bound_to_template_title_and_first_sentence(self):
        did = self.submit('post', image_id='textcard', media_hash='0' * 64)['draft_id']       # the core computes the binding itself
        payload = json.loads(self.rv.row(did)['payload'])
        self.assertEqual(payload['media_hash'], card_spec_hash('大肥鱼游进小红书', '我是大肥鱼，一只爱打游戏的AI虚拟角色。'))
        full = await self.say('小红书草稿 ' + did)
        self.assertIn('原创文字卡', full)
        self.assertIn(payload['media_hash'][:8], full)
        await self.approve(did)
        await self.say('小红书修改 %s 标题：新的标题' % did)
        new = json.loads(self.rv.row(did)['payload'])
        self.assertNotEqual(new['media_hash'], payload['media_hash'])                          # edit => new card spec => new approval
        self.assertEqual(new['media_hash'], card_spec_hash('新的标题', new['content']))
        self.assertEqual(self.status(did), 'pending')

    async def test_comment_draft_binds_the_note_author_and_nothing_else_of_a_comment(self):
        target = {'feed_id': FEED, 'comment_id': '', 'author_id': OTHER, 'comment_hash': ''}
        did = self.submit('comment', target=target)['draft_id']
        self.assertEqual(json.loads(self.rv.row(did)['payload'])['target']['author_id'], OTHER)
        other_author = self.submit('comment', target={**target, 'feed_id': 'g' * 24, 'author_id': 'u' + 'c' * 11}, submit_key='8' * 32)['draft_id']
        self.assertNotEqual(self.rv.row(did)['content_hash'], self.rv.row(other_author)['content_hash'])   # the author is part of the approval
        for bad in ({'author_id': 'bad author!'}, {'comment_hash': '1' * 64}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.submit('comment', target={**target, 'feed_id': 'h' * 24, **bad}, submit_key=os.urandom(16).hex())
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['comment']})['draft']
        self.assertEqual(claim['target']['author_id'], OTHER)

    async def test_real_image_is_bound_by_its_file_hash(self):
        did = self.submit('post', media_hash='b' * 64)['draft_id']
        self.assertEqual(json.loads(self.rv.row(did)['payload'])['media_hash'], 'b' * 64)
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['post']})['draft']
        self.assertEqual((claim['image_id'], claim['media_hash']), ('card1', 'b' * 64))

    async def test_reply_binds_target_author_and_comment_text(self):
        did = self.submit()['draft_id']
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        self.assertEqual((claim['target']['author_id'], claim['target']['comment_hash']), (OTHER, '1' * 64))


class OutboxTests(Base):
    async def test_new_draft_is_pushed_once_to_the_configured_chat_only(self):
        did = self.submit()['draft_id']
        self.assertEqual((await self.rv.tick())['status'], 'sent')
        text, session = self.sent[0]
        self.assertEqual(session, OWNER_SESSION)
        for needle in ('新草稿', did, '小红书批准 %s v1' % did, '谢谢你的留言'):
            self.assertIn(needle, text)
        self.assertEqual((await self.rv.tick())['status'], 'idle')                             # no nagging, no repeat
        self.assertEqual(len(self.sent), 1)

    async def test_delivery_is_judged_by_the_receipt_not_by_the_absence_of_an_exception(self):
        self.receipt = {'sent': False}
        self.submit()
        self.assertEqual((await self.rv.tick())['status'], 'not_delivered')
        self.assertEqual(self.rv.db.execute('SELECT sent FROM xhs_review_outbox').fetchone()[0], 3)
        self.assertEqual((await self.rv.tick())['status'], 'idle')                             # never resent
        self.receipt = {'sent': True}
        self.send_error = True
        self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        self.assertEqual((await self.rv.tick())['status'], 'unknown')
        self.assertEqual([r[0] for r in self.rv.db.execute('SELECT sent FROM xhs_review_outbox ORDER BY id')], [3, 4])
        self.send_error = False
        self.assertEqual((await self.rv.tick())['status'], 'idle')
        self.assertEqual(len(self.sent), 1)          # only the first attempt reached the sender; nothing was ever resent

    async def test_old_versions_and_decided_drafts_are_not_pushed(self):
        edited = self.submit()['draft_id']
        await self.say('小红书修改 %s 正文：新版' % edited)                                       # v2 exists before v1 was pushed
        rejected = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        await self.say('小红书拒绝 ' + rejected)
        self.assertEqual((await self.rv.tick())['status'], 'idle')
        self.assertEqual(self.sent[-1][0].count('新草稿'), 0) if self.sent else None
        self.assertEqual([r[0] for r in self.rv.db.execute("SELECT sent FROM xhs_review_outbox WHERE kind='draft' ORDER BY id")], [5, 5])
        self.assertEqual([t for t, _ in self.sent if '新草稿' in t], [])

    async def test_pushes_are_capped_per_hour_but_results_always_get_through(self):
        for n in range(4):
            self.submit(target={'feed_id': FEED, 'comment_id': 'cm%04d' % n, 'author_id': OTHER, 'comment_hash': '1' * 64})
        statuses = [(await self.rv.tick())['status'] for _ in range(5)]
        self.assertEqual(statuses.count('sent'), review.PUSHES_PER_HOUR)
        did = self.rv.db.execute('SELECT draft_id FROM xhs_drafts ORDER BY created LIMIT 1').fetchone()[0]
        await self.approve(did)
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'unknown'})
        self.assertEqual((await self.rv.tick())['status'], 'sent')
        self.assertIn('结果未知', self.sent[-1][0])

    async def test_nothing_is_pushed_when_disabled_or_no_owner_is_configured(self):
        self.submit()
        self.write_policy(xhs_enabled=False)
        self.assertEqual(await self.rv.tick(), {'status': 'disabled'})
        self.write_policy(xhs_enabled=True, xhs_review_owner_session=None)
        self.assertEqual(await self.rv.tick(), {'status': 'no_owner_session'})
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
