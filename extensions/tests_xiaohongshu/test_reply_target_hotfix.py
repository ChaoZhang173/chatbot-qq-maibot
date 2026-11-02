"""Hotfix: an approved reply whose target is missing from the shallow load (a nested comment) gets exactly ONE bounded deep read.

Guards are unchanged: exact comment ID + author + SHA256 text hash, approval hash/version/account, unknown-retry duplicate
protection. A target that is still absent is `target_not_loaded` (never "changed"); an author/text difference stays
`target_changed`. Nothing is ever written in either refusal. Stub browser only; no network, no real database.
"""
import hashlib, time, unittest
from test_review_natural import NaturalBase
from test_review_core import UID
from xhs_support import Env, comment, TOKEN, OTHER, FEED
from xiaohongshu.core_client import CoreClient

TEXT = 'original message'


class ReplyTargetTests(NaturalBase):
    @staticmethod
    def target_of(env):
        """The target comment dict wherever it sits (top level or inside a root's subComments)."""
        for c in env.service.comments:
            if c['id'] == 'cm0001':
                return c
            for sub in c['subComments']:
                if sub['id'] == 'cm0001':
                    return sub

    async def prepare(self, hidden=False, nested=False):
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
        if hidden:
            env.service.hidden_until_deep = {'cm0001'}      # absent from the shallow load, present in a deep one
        if nested:
            # The incident shape: the target is a sub-comment INSIDE a root comment's subComments, and a shallow read
            # returns the root without it; only a deep read (scroll + expand replies) loads it.
            target = env.service.comments[0]
            root = comment('root01', 'a root comment', user=OTHER, when=env.clock(), subs=[target])
            env.service.comments = [root]
            env.service.hidden_subs_until_deep = {'cm0001'}
        return env, did

    @staticmethod
    def reads_before_first_write(env):
        """(shallow, deep) detail reads issued before the first write attempt."""
        shallow = deep = 0
        for method, path, data in env.service.calls:
            if method == 'POST' and path.startswith('/api/v1/feeds/comment'):
                break
            if path == '/api/v1/feeds/detail':
                deep += bool(data.get('load_all_comments'))
                shallow += not data.get('load_all_comments')
        return shallow, deep

    def last_result_text(self):
        return self.rv.db.execute("SELECT text FROM xhs_review_outbox WHERE kind='result' ORDER BY id DESC LIMIT 1").fetchone()['text']

    async def test_hidden_nested_target_is_found_by_one_deep_read_and_written_exactly_once(self):
        env, did = await self.prepare(hidden=True)
        outcome = env.ex.deliver_approved()
        # accepted, not "verified": the post-write shallow readback cannot see the hidden thread, and that is reported honestly
        self.assertEqual(outcome['reported'], 'accepted')
        self.assertEqual(len(env.service.writes()), 1)                            # exactly one platform write
        self.assertEqual(self.reads_before_first_write(env), (1, 1))               # 1 shallow + exactly 1 deep, never more
        self.assertEqual(len(env.store.actions()), 1)
        self.assertEqual(self.status(did), 'delivered')
        # nothing else may follow: a second delivery finds no approved draft and writes nothing
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        self.assertEqual(len(env.service.writes()), 1)

    async def test_visible_target_needs_no_deep_read(self):
        env, did = await self.prepare()
        env.ex.deliver_approved()
        self.assertEqual(len(env.service.writes()), 1)
        self.assertEqual(self.reads_before_first_write(env), (1, 0))

    async def test_still_missing_target_is_not_loaded_not_changed_and_nothing_is_written(self):
        env, did = await self.prepare()
        env.service.comments = []                                                  # absent from shallow AND deep
        outcome = env.ex.deliver_approved()
        self.assertEqual(outcome['status'], 'refused:target_not_loaded')
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])                                  # no executor action row, nothing consumed
        self.assertEqual(self.reads_before_first_write(env), (1, 1))               # bounded: one shallow, one deep, no loop
        self.assertEqual(self.status(did), 'pending')                              # back to review: needs a fresh approval
        text = self.last_result_text()
        self.assertIn('没能读到', text)
        self.assertIn('target_not_loaded', text)
        self.assertNotIn('一致性核验', text)                                       # never claims the target changed
        self.assertNotIn('凭据', text)
        self.assertIn('并不表示它被改动或删除', text)

    async def test_nested_subcomment_target_is_found_by_one_deep_read_with_exact_author_and_hash(self):
        env, did = await self.prepare(nested=True)
        before = [c for c in env.service.calls if c[1] == '/api/v1/feeds/detail']
        self.assertEqual(before, [])
        outcome = env.ex.deliver_approved()
        self.assertEqual(outcome['reported'], 'accepted')                          # post-write shallow readback cannot see the thread
        self.assertEqual(len(env.service.writes()), 1)                             # exactly one write
        self.assertEqual(self.reads_before_first_write(env), (1, 1))               # 1 shallow (root only) + exactly 1 deep
        self.assertEqual(len(env.store.actions()), 1)
        written = env.service.writes()[0]
        self.assertEqual(written[2]['comment_id'], 'cm0001')                       # replied to the exact nested comment ID
        self.assertEqual(len(env.service.comments[0]['subComments']), 2)           # the target plus our one reply, nowhere else
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        self.assertEqual(len(env.service.writes()), 1)

    async def test_nested_target_absent_even_deep_is_not_loaded_and_nothing_is_written(self):
        env, did = await self.prepare(nested=True)
        env.service.comments[0]['subComments'] = []
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:target_not_loaded')
        self.assertEqual((env.service.writes(), env.store.actions()), ([], []))
        self.assertEqual(self.reads_before_first_write(env), (1, 1))

    async def test_nested_target_mismatch_stays_target_changed_and_never_writes(self):
        await self.mismatch(lambda c: c['userInfo'].update(userId='u' + 'z' * 11), False, (1, 1), nested=True)

    async def test_nested_target_text_mismatch_stays_target_changed_and_never_writes(self):
        await self.mismatch(lambda c: c.update(content='the text was edited'), False, (1, 1), nested=True)

    async def mismatch(self, mutate, hidden, reads, nested=False):
        env, did = await self.prepare(hidden=hidden, nested=nested)
        mutate(self.target_of(env))
        outcome = env.ex.deliver_approved()
        self.assertEqual(outcome['status'], 'refused:target_changed')
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])
        self.assertEqual(self.reads_before_first_write(env), reads)
        self.assertIn('发送前未能通过目标作者或原文的一致性核验', self.last_result_text())
        self.assertNotIn('没能读到', self.last_result_text())
        self.assertNotIn('token', self.last_result_text().replace('target_changed', ''))      # no unrelated credential wording

    async def test_different_author_found_at_once_never_writes(self):
        await self.mismatch(lambda c: c['userInfo'].update(userId='u' + 'z' * 11), False, (1, 0))

    async def test_different_text_found_at_once_never_writes(self):
        await self.mismatch(lambda c: c.update(content='the text was edited'), False, (1, 0))

    async def test_different_author_found_only_deep_never_writes(self):
        await self.mismatch(lambda c: c['userInfo'].update(userId='u' + 'z' * 11), True, (1, 1))

    async def test_different_text_found_only_deep_never_writes(self):
        await self.mismatch(lambda c: c.update(content='the text was edited'), True, (1, 1))

    async def test_replying_to_our_own_comment_is_still_refused(self):
        env, did = await self.prepare(hidden=True)
        env.service.comments[0]['userInfo']['userId'] = UID                        # the target would now be the bot itself
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:target_changed')
        self.assertEqual(env.service.writes(), [])

    async def test_unknown_retry_keeps_duplicate_protection_and_adds_no_extra_deep_read(self):
        env, did = await self.prepare(hidden=True)
        env.service.fail = 'ambiguous_lost_before'
        env.ex.deliver_approved()                                                   # hidden target found deep, write outcome unknown
        self.assertEqual(self.status(did), 'unknown')
        first = env.store.actions()[0]
        env.service.fail = ''
        env.clock.t += 61
        await self.say('草稿%s v1发送成功了吗？我怎么没看到，能重发一下吗' % did)
        before = len(env.service.calls)
        outcome = env.ex.deliver_approved()
        self.assertEqual(outcome['reported'], 'accepted')                          # honest: the hidden thread is not read back yet
        writes_at = next(i for i, c in enumerate(env.service.calls[before:]) if c[0] == 'POST' and c[1].startswith('/api/v1/feeds/comment'))
        pre_write = [c for c in env.service.calls[before:before + writes_at] if c[1] == '/api/v1/feeds/detail']
        self.assertEqual(sum(bool(c[2].get('load_all_comments')) for c in pre_write), 1)   # the preflight readback IS the deep read
        self.assertEqual(len(pre_write), 1)                                                # and no extra deep reread
        rows = env.store.actions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['retry_of'], first['id'])
        self.assertEqual(len(env.service.comments[0]['subComments']), 1)           # exactly one visible reply: no duplicate
        # a further delivery attempt cannot send again
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        self.assertEqual(len(env.service.comments[0]['subComments']), 1)


if __name__ == '__main__':
    unittest.main()
