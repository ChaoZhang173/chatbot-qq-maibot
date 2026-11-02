"""Nested-reply readback: exact author+text+time found in the SAME comment thread by this account (`same_thread`).

It is stronger than the platform tool's bare acceptance and weaker than an exact own-page match: it does not prove the exact
reply target, other-account visibility or notification delivery. It is reported to the core as `readback_same_thread`
(core status stays `delivered`, evidence recorded once, ONE QQ update), upgrades only monotonically and never re-sends.
Stub browser and in-memory core only; no network, no real database.
"""
import hashlib, time
from test_review_natural import NaturalBase
from test_review_core import UID
from xhs_support import Env, comment, TOKEN, OTHER, FEED
from xiaohongshu.core_client import CoreClient
from maibot_unified_core.xiaohongshu_review import SAME_THREAD_TEXT

TEXT = 'original message'


class ThreadReadbackTests(NaturalBase):
    async def prepare(self, deliver=True):
        env = Env(self, strict=True, bound_uid=UID)
        env.clock.t = time.time()
        self.policy['xhs_bot_account'] = env.cfg['bound_uid']
        env.service.comments = [comment('cm0001', TEXT, user=OTHER, when=env.clock())]
        did = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0001', 'author_id': OTHER,
                                        'comment_hash': hashlib.sha256(TEXT.encode()).hexdigest()})
        env.store.put_token(FEED, TOKEN)

        def core(payload):
            op = payload['op']
            if op.startswith('xhs_draft_'):
                return {'ok': True, **getattr(self.rv, op[len('xhs_draft_'):])(payload)}
            return {'ok': True}
        env.ex.core = CoreClient(transport=core)
        env.ex.identity = lambda force=False: {'ok': True}
        await self.say('发布吧')
        # The incident shape: the target is a sub-comment of a root, invisible to a shallow read (so the post-write readback
        # cannot verify it and the first report is the tool's bare acceptance).
        root = comment('root01', 'a root comment', user=OTHER, when=env.clock(), subs=[env.service.comments[0]])
        env.service.comments = [root]
        env.service.hidden_subs_until_deep = {'cm0001'}
        if deliver:
            self.assertEqual(env.ex.deliver_approved()['reported'], 'accepted')     # the first report: tool acceptance only
        return env, did

    def results(self):
        return [r['text'] for r in self.rv.db.execute("SELECT text FROM xhs_review_outbox WHERE kind='result' ORDER BY id")]

    def result_of(self, did):
        import json
        return json.loads(self.rv.row(did)['result'])

    @staticmethod
    def same_thread(env):
        action = env.store.actions(limit=1)[0]
        env.store.finish(action['id'], 'accepted', detail='same_thread_readback', remote_id='newreply01', verification='same_thread')
        return action['id']

    async def test_first_report_as_same_thread_is_delivered_with_limited_evidence_and_one_update(self):
        env, did = await self.prepare(deliver=False)
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        report = {'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': 'readback_same_thread', 'detail': 'same_thread_readback'}
        self.rv.report(report)
        self.assertEqual(self.status(did), 'delivered')
        self.assertEqual(self.result_of(did)['outcome'], 'readback_same_thread')
        texts = self.results()
        self.assertEqual(len(texts), 1)
        self.assertIn(SAME_THREAD_TEXT, texts[0])
        self.rv.report(report)                                                     # a repeat is acknowledged, not applied twice
        self.assertEqual(self.results(), texts)
        self.assertEqual(self.status(did), 'delivered')

    async def test_accepted_is_upgraded_by_report_pending_once_and_not_repeated_every_tick_or_restart(self):
        env, did = await self.prepare()
        before = self.results()
        self.assertEqual(self.result_of(did)['outcome'], 'accepted')
        writes = len(env.service.writes())
        action_id = self.same_thread(env)
        self.assertEqual([a['id'] for a in env.store.unreported()], [action_id])  # picked up although it was already reported once
        env.ex.report_pending()
        self.assertEqual(self.status(did), 'delivered')
        self.assertEqual(self.result_of(did)['outcome'], 'readback_same_thread')
        after = self.results()
        self.assertEqual(len(after), len(before) + 1)                              # exactly one new QQ update
        self.assertIn(SAME_THREAD_TEXT, after[-1])
        self.assertEqual(env.store.unreported(), [])
        for _ in range(3):                                                         # later ticks / a restart: nothing more is sent
            env.ex.report_pending()
        self.assertEqual(self.results(), after)
        # even if the executor told the core again (e.g. lost local marker), the core applies and announces it once
        env.store.mark_reported(action_id, 1)                                      # a late/stale lower marker cannot downgrade level 3
        self.assertEqual(env.store.action(action_id)['reported'], 3)
        env.store.db.execute('UPDATE actions SET reported=1 WHERE id=?', (action_id,))   # simulate a lost marker
        env.ex.report_pending()
        self.assertEqual(self.results(), after)
        self.assertEqual(len(env.service.writes()), writes)                        # never a second platform write

    async def test_wording_never_claims_exact_target_or_public_success(self):
        env, did = await self.prepare()
        self.same_thread(env)
        env.ex.report_pending()
        text = self.results()[-1]
        line = self.rv.status_line(self.rv.row(did))
        for shown in (text, line):
            self.assertIn('同一评论楼层', shown)
            self.assertIn('具体回复对象、其他账号是否可见及通知送达尚未核验', shown)
            self.assertNotIn('读回确认', shown)                                    # the exact-match ('verified') wording
            self.assertNotIn('发布成功', shown)
            self.assertNotIn('正常显示', shown)
            self.assertNotIn('没有发', shown)                                      # nothing that sounds like a failure

    async def test_exact_match_still_upgrades_and_a_late_same_thread_report_never_downgrades(self):
        env, did = await self.prepare()
        action_id = self.same_thread(env)
        env.ex.report_pending()
        env.store.finish(action_id, 'accepted', detail='own_page_exact_readback', remote_id='newreply01', verification='own_page_exact')
        env.ex.report_pending()
        self.assertEqual(self.status(did), 'verified')
        sent = self.results()
        self.assertEqual(env.store.unreported(), [])
        token = self.rv.row(did)['claim_token']
        self.rv.report({'draft_id': did, 'claim_token': token, 'outcome': 'readback_same_thread', 'detail': ''})   # stale evidence
        self.assertEqual(self.status(did), 'verified')
        self.assertEqual(self.results(), sent)

    async def test_mismatched_claims_are_refused(self):
        env, did = await self.prepare()
        token = self.rv.row(did)['claim_token']
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': did, 'claim_token': 'x' * 32, 'outcome': 'readback_same_thread'})
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': did, 'claim_token': '', 'outcome': 'readback_same_thread'})
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': 'ZZZZZZ', 'claim_token': token, 'outcome': 'readback_same_thread'})
        with self.assertRaises(ValueError):
            self.rv.report({'draft_id': did, 'claim_token': token, 'outcome': 'same_thread'})        # not a valid outcome name
        self.assertEqual(self.result_of(did)['outcome'], 'accepted')

    async def test_same_thread_evidence_never_allows_a_second_send(self):
        env, did = await self.prepare()
        self.same_thread(env)
        env.ex.report_pending()
        self.assertEqual(len(env.service.writes()), 1)
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        env.ex.tick()
        self.assertEqual(len(env.service.writes()), 1)
        self.assertEqual(len(env.store.actions()), 1)
        # the owner asking for a retry cannot get past the delivered+same_thread record: no second action is ever reserved
        action = env.store.actions(limit=1)[0]
        again, reason = env.store.reserve('reply', feed_id=FEED, comment_id='cm0001', author=OTHER, content=action['content'],
                                          limits=env.ex.limits, draft_id=did, claim_token='fresh' * 6, retry_of=action['id'])
        self.assertIsNone(again)
        self.assertEqual(reason, 'retry_not_authorized')
