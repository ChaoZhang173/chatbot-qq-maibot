"""Self readback must not erase the owner's independent visibility feedback."""
import json, time
from maibot_unified_core import xiaohongshu as xhs
from test_review_natural import NaturalBase
from test_review_core import OWNER, OWNER_SESSION, UID


class VisibilityFeedbackTests(NaturalBase):
    async def prepare_activity(self, outcome='verified'):
        did = await self.pushed(content='3天120w token？真能吃呀')
        await self.say('发布吧')
        self.claim(did, outcome)
        row = self.rv.row(did)
        payload = json.loads(row['payload'])
        action = {'action_id': 'xhs-visibility-test', 'platform': 'xiaohongshu',
                  'bot_account_id': row['bot_account_id'], 'conversation_id': 'xhs:feed:' + payload['target']['feed_id'],
                  'kind': 'reply', 'status': 'succeeded' if outcome == 'verified' else 'pending_visibility',
                  'content': payload['content'], 'created': time.time(), 'draft_id': did,
                  'verification': 'own_page_exact' if outcome == 'verified' else 'pending',
                  'remote_id': 'cm100001' if outcome == 'verified' else '', 'target': payload['target']}
        stored = xhs.activity(self.svc, {'action': action})['activity']
        return did, action, stored

    def report_hidden(self, stored):
        stamp = time.time()
        return self.svc.state.record_activity({**stored, 'public_visibility': 'not_publicly_verified',
             'visibility_checked_at': stamp, 'visibility_evidence': {'observer_source': 'user_report',
             'observer_list_seen': False, 'observer_recorded_at': stamp}})

    async def test_status_exposes_owner_report_and_keeps_duplicate_guard(self):
        did, action, stored = await self.prepare_activity()
        self.report_hidden(stored)
        answer = await self.say('草稿%s发送成功了吗' % did)
        self.assertIn('你反馈其他账号在评论区看不到', answer)
        self.assertIn('对外可见性异常', self.rv.render(self.rv.row(did), short=True))
        answer = await self.say('把3天120万那条回复补发一次')
        self.assertIn('没有重复', answer)
        self.assertEqual((self.status(did), self.rv.row(did)['delivery_attempt']), ('verified', 0))
        self.assertIsNone(self.rv.claim({})['draft'])

    async def test_accepted_to_self_readback_and_replay_preserve_feedback(self):
        did, action, stored = await self.prepare_activity('accepted')
        report = self.report_hidden(stored)
        action.update(status='succeeded', verification='own_page_exact', remote_id='cm100001')
        for _ in range(2):
            result = xhs.activity(self.svc, {'action': action})['activity']
            self.assertEqual(result['visibility_evidence'], report['visibility_evidence'])
            self.assertEqual(result['public_visibility'], 'not_publicly_verified')
        self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': 'verified'})
        self.assertIn('对外可见性异常', self.rv.status_line(self.rv.row(did)))
        notice = self.rv.db.execute("SELECT text FROM xhs_review_outbox WHERE okey=?", ('result:%s:1:upgraded' % did,)).fetchone()['text']
        self.assertIn('对外可见性异常', notice)

    async def test_feedback_does_not_apply_to_different_content_or_account(self):
        did, action, stored = await self.prepare_activity()
        self.report_hidden(stored)
        row = dict(self.rv.row(did))
        self.assertTrue(self.rv.observer_not_seen(row))
        payload = json.loads(row['payload']); payload['content'] = 'a different draft'
        self.assertFalse(self.rv.observer_not_seen({**row, 'payload': json.dumps(payload)}))
        self.assertFalse(self.rv.observer_not_seen({**row, 'bot_account_id': 'another-account'}))
        self.assertFalse(self.rv.observer_not_seen({**row, 'status': 'pending'}))

    async def test_delivered_hidden_comment_is_not_treated_as_unsent(self):
        did, action, stored = await self.prepare_activity('accepted')
        self.report_hidden(stored)
        answer = await self.say('把3天120万那条回复补发一次')
        self.assertIn('没有重复加入发送队列', answer)
        self.assertEqual((self.status(did), self.rv.row(did)['delivery_attempt']), ('delivered', 0))
        self.assertIsNone(self.rv.claim({})['draft'])

    async def test_pending_moderation_survives_readback_and_blocks_retry(self):
        did, action, stored = await self.prepare_activity('accepted')
        self.rv.queue_readback(self.rv.row(did))
        check = self.rv.check_claim({})['check']
        stamp = time.time()
        report = self.svc.state.record_activity({**stored, 'visibility_checked_at': stamp,
            'visibility_evidence': {'moderation_source': 'user_report', 'moderation_state': 'pending_review', 'moderation_recorded_at': stamp}})
        for question in ('草稿%s审核通过了吗', '草稿%s审核是不是没通过'):
            self.assertIn('平台审核中', await self.say(question % did))
        self.assertEqual(self.rv.db.execute('SELECT count(*) FROM xhs_readback_requests').fetchone()[0], 1)
        answer = await self.say('把3天120万那条回复补发一次')
        self.assertIn('审核或对外不可见不能当成没有提交', answer)
        self.assertEqual((self.status(did), self.rv.row(did)['delivery_attempt']), ('delivered', 0))
        action.update(status='succeeded', verification='own_page_exact', remote_id='cm100001')
        refreshed = xhs.activity(self.svc, {'action': action})['activity']
        self.assertEqual(refreshed['visibility_evidence'], report['visibility_evidence'])
        self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': 'verified'})
        self.rv.check_report({**check, 'result': 'found'})
        self.assertIn('平台审核中', self.rv.render(self.rv.row(did), short=True))
        notice = self.rv.db.execute('SELECT text FROM xhs_review_outbox WHERE okey=?', ('readback:' + check['request_id'],)).fetchone()['text']
        self.assertIn('本账号页面复查不能证明审核通过', notice)
        self.assertIsNone(self.rv.claim({})['draft'])
        row = dict(self.rv.row(did)); payload = json.loads(row['payload']); payload['content'] = 'another draft'
        self.assertFalse(self.rv.moderation_pending({**row, 'payload': json.dumps(payload)}))
        self.assertFalse(self.rv.moderation_pending({**row, 'bot_account_id': 'another-account'}))

    async def test_account_hold_keeps_drafts_and_blocks_approval_claim_and_retry(self):
        did, action, stored = await self.prepare_activity('accepted')
        pending = self.submit(kind='comment', content='新的待审评论')['draft_id']
        approved = self.submit(content='已批准但尚未领取的回复')['draft_id']
        await self.approve(approved)
        self.write_policy(xhs_interaction_hold={'enabled': True, 'bot_account_id': UID})
        answer = await self.approve(pending)
        self.assertIn('发送已暂停', answer)
        self.assertEqual(self.status(pending), 'pending')
        self.assertIsNone(self.rv.claim({'kinds': ['comment', 'reply']})['draft'])
        self.assertEqual(self.status(approved), 'approved')
        self.assertIn('发送已暂停', self.rv.status_line(self.rv.row(approved)))
        self.assertIn('发送已暂停', self.rv.retry(self.rv.row(did), OWNER_SESSION, OWNER, 'test'))
        self.assertEqual(self.rv.row(did)['delivery_attempt'], 0)
        self.assertIn('发送已暂停', await self.say('发出去了吗'))
        self.assertTrue(self.rv.status({})['can_submit'])
        self.assertTrue(self.rv.status({})['interaction_sending_paused'])
        # A note still requires normal owner approval and can be claimed.
        post = self.submit(kind='post')['draft_id']
        self.assertIn('已批准', await self.approve(post))
        claim = self.rv.claim({})['draft']
        self.assertEqual(claim['draft_id'], post)
        self.rv.report({'draft_id': post, 'claim_token': claim['claim_token'], 'outcome': 'verified'})
        self.write_policy(xhs_interaction_hold={'enabled': True, 'bot_account_id': 'another-account'})
        self.svc.reload_policy()
        self.assertFalse(self.rv.interaction_hold())
        self.assertEqual(self.rv.claim({'kinds': ['reply']})['draft']['draft_id'], approved)

    async def test_moderation_report_requires_bounded_provenance(self):
        did, action, stored = await self.prepare_activity()
        stamp = time.time()
        evidence = {'moderation_source': 'user_report', 'moderation_state': 'pending_review', 'moderation_recorded_at': stamp}
        for change in ({'moderation_source': 'executor'}, {'moderation_state': 'unknown'},
                       {'moderation_recorded_at': stamp + 1}, {'moderation_recorded_at': float('nan')}):
            with self.assertRaisesRegex(ValueError, 'invalid_moderation_evidence'):
                self.svc.state.record_activity({**stored, 'visibility_checked_at': stamp, 'visibility_evidence': {**evidence, **change}})
        # The executor's activity API cannot forge a platform moderation report.
        xhs.activity(self.svc, {'action': {**action, 'visibility_checked_at': stamp, 'visibility_evidence': evidence}})
        self.assertFalse(self.rv.moderation_pending(self.rv.row(did)))
