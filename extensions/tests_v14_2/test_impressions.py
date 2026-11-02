"""Phase 3A: the local impression ledger driven through the real decide/activity entry points, and the interest gate."""
import unittest

from maibot_unified_core import impressions
from v14_2_support import Harness, HOUR

DAY = 86400.0
GOOD = {'interested': True, 'text': '', 'interest_score': 80, 'reason': '喜欢这类', 'stance': 'positive'}


def rows(h):
    return [dict(r) for r in h.state.db.execute('SELECT * FROM impressions ORDER BY occurred_at,impression_key')]


FEED = 'abcdef123456'


class XhsCommentSignalTests(unittest.IsolatedAsyncioTestCase):
    """Xiaohongshu: the sign comes from the stance in the existing comment JSON, never from the act of commenting."""

    async def run_flow(self, answer, receipt='pending_visibility'):
        import time
        h = Harness(self)
        h.set_policy(xhs_enabled=True, xhs_bot_account='xhsbot')
        await h.service.dispatch({'op': 'xhs_event', 'opportunity': 'a' * 64, 'event': {
            'event_id': 'xhs:read:%s:d1' % FEED, 'platform': 'xiaohongshu', 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:' + FEED,
            'actor_id': 'author1', 'occurred_at': time.time() - 5, 'event_type': 'content_read', 'content': '一篇关于游戏的笔记',
            'source_id': FEED, 'origin': 'platform_content'}}, admin=True)
        h.llm.queue.append(answer)
        plan = await h.service.dispatch({'op': 'xhs_plan', 'task': 'comment', 'cycle': 'b' * 64, 'opportunity': 'a' * 64,
                                         'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:' + FEED, 'history': [],
                                         'material': {'feed_title': 't', 'feed_text': 'x'}, 'candidates': {}, 'budget': {}}, admin=True)
        if receipt:
            await h.service.dispatch({'op': 'xhs_activity', 'action': {
                'action_id': 'xhsact1', 'platform': 'xiaohongshu', 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:' + FEED,
                'kind': 'comment', 'status': receipt, 'audience': 'self_only', 'content': '评论', 'created': time.time(),
                'target': {'feed_id': FEED, 'comment_id': ''}}}, admin=True)
        return h, plan['decision']

    @staticmethod
    def summary(h):
        return {(r['action'], r['direction'], r['execution'], r['weight']) for r in rows(h)}

    async def test_opposing_comment_never_becomes_a_like(self):
        h, decision = await self.run_flow({'willing': True, 'text': '我不同意这个说法', 'stance': 'negative', 'reason': '想纠正'})
        self.assertEqual((decision['stance'], decision['reason']), ('negative', '想纠正'))
        self.assertEqual(self.summary(h), {('comment', 'negative', 'submitted_pending_moderation', 3)})   # receipt keeps the sign
        self.assertEqual(impressions.Ledger(h.state).rows(), [])
        self.assertEqual(h.state.get('opp_reason:xhs:read:%s:d1' % FEED), '想纠正')

    async def test_positive_stance_counts_and_the_receipt_is_distinct_from_publication(self):
        h, _ = await self.run_flow({'willing': True, 'text': '好喜欢这个', 'stance': 'positive', 'reason': '真心喜欢'})
        self.assertEqual(self.summary(h), {('comment', 'positive', 'submitted_pending_moderation', 3)})
        self.assertEqual(len(impressions.Ledger(h.state).rows()), 1)

    async def test_missing_neutral_or_invalid_stance_stays_unknown_and_declined_stance_is_kept(self):
        for answer in ({'willing': True, 'text': '看看'}, {'willing': True, 'text': '看看', 'stance': 'neutral'},
                       {'willing': True, 'text': '看看', 'stance': 'love'}):
            h, decision = await self.run_flow(answer)
            self.assertEqual({d for _, d, _, _ in self.summary(h)}, {'unknown'}, answer)
            self.assertEqual(impressions.Ledger(h.state).rows(), [])
            self.assertNotEqual(decision.get('stance'), 'love')
        # declined to comment but disliked it: no comment row, the read row carries the negative sign
        h, _ = await self.run_flow({'willing': False, 'stance': 'negative', 'reason': '不太认同'}, receipt='')
        self.assertEqual(self.summary(h), {('read', 'negative', 'read', 1)})


class ImpressionLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_failed_favorite_then_successful_like_never_overstates_weight(self):
        h = Harness(self)
        event = h.bili_read(123)
        await h.decide(event, GOOD)
        [row] = rows(h)
        self.assertEqual((row['action'], row['weight'], row['execution'], row['direction'], row['mode']), ('read', 1, 'read', 'positive', 'autonomous'))
        await h.receipt(event, 'favorite', status='failed')            # a failed favorite adds nothing
        [row] = rows(h)
        self.assertEqual((row['weight'], row['execution']), (1, 'read'))
        await h.receipt(event, 'like', status='succeeded')
        [row] = rows(h)
        self.assertEqual((row['action'], row['weight'], row['execution']), ('like', 3, 'executed'))
        await h.receipt(event, 'like', status='succeeded')             # replayed receipt: still one contact, same weight
        await h.decide(event, GOOD)                                    # the same opportunity decided again (retry)
        self.assertEqual([(r['weight']) for r in rows(h)], [3])
        await h.receipt(event, 'favorite', status='succeeded', attempt=':retry')   # a later, separate successful attempt
        self.assertEqual([(r['action'], r['weight']) for r in rows(h)], [('favorite', 5)])
        self.assertEqual(len(rows(h)), 1)

    async def test_follow_scores_the_author_not_the_content_and_commanded_actions_do_not_count(self):
        h = Harness(self)
        event = h.bili_read(124, author='555')
        await h.decide(event, GOOD)
        await h.receipt(event, 'follow')
        by_type = {r['target_type']: r for r in rows(h)}
        self.assertEqual(by_type['author']['target_ref'], '555')
        self.assertEqual(by_type['author']['weight'], 5)
        self.assertEqual(by_type['content']['weight'], 1)               # the author's 5 points did not land on the content
        # an action the owner commanded (no autonomous opportunity was ever decided) is not recorded as autonomous liking
        commanded = h.bili_read(125, author='556')
        await h.receipt(commanded, 'favorite')
        self.assertEqual(len(rows(h)), 2)
        # unknown / negative direction and un-executed intents are distinct and not counted as positive points
        h.state.put('opp:' + commanded['event_id'], 'opp-x')
        ledger = impressions.Ledger(h.state)
        evidence = h.state.ingest(commanded)['event_id']
        ledger.record(platform='bilibili', opportunity_id='neg', content_id='1:125', target_type='content', target_ref='1:125',
                      direction='negative', mode='autonomous', execution='executed', action='favorite', occurred_at=h.clock(),
                      evidence_event_ids=[evidence])
        ledger.record(platform='bilibili', opportunity_id='cmd', content_id='1:125', target_type='content', target_ref='1:125',
                      direction='positive', mode='commanded', execution='executed', action='favorite', occurred_at=h.clock(),
                      evidence_event_ids=[evidence])
        counted = {r['opportunity_id'] for r in ledger.rows()}
        self.assertNotIn('neg', counted)          # negative direction does not add to positive preference
        self.assertNotIn('cmd', counted)          # a commanded action does not prove autonomous liking
        self.assertTrue(all(o.startswith('bili:') for o in counted))

    async def test_opposed_comment_and_quota_blocked_willingness_through_decide(self):
        h = Harness(self)
        ledger = impressions.Ledger(h.state)
        blocked = {'comment_allowed': False}             # the comment quota/permission is exhausted
        # 1. an opposing/corrective comment the bot wanted to write. The prompt makes a compliant model leave text EMPTY when
        #    comments are blocked, so willingness arrives as the explicit boolean: kept, direction NEGATIVE, never a positive point
        opposed = h.bili_read(160)
        decision = await h.decide(opposed, {'interested': True, 'text': '', 'comment_willing': True, 'interest_score': 85,
                                            'reason': '纠错', 'stance': 'negative'}, permissions=blocked)
        self.assertEqual(decision['decision']['text'], '')            # blocked text stays empty; willingness authorizes nothing
        self.assertNotIn('comment_willing', decision['decision'])      # the optional field never reaches the executor
        [row] = rows(h)
        self.assertEqual((row['action'], row['execution'], row['direction'], row['weight']), ('comment', 'intent_not_executed', 'negative', 3))
        self.assertEqual(ledger.rows(), [])
        await h.receipt(opposed, 'favorite')                         # even a saved item does not turn negative into liking
        self.assertEqual([r['direction'] for r in rows(h)], ['negative'])
        self.assertEqual(ledger.rows(), [])
        # 2. quota-blocked genuine liking: the willingness is a counted positive intent, not a published comment
        liked = h.bili_read(161)
        await h.decide(liked, {'interested': True, 'text': '', 'comment_willing': True, 'interest_score': 85, 'reason': '喜欢',
                               'stance': 'positive'}, permissions=blocked)
        [row] = [r for r in rows(h) if r['content_id'] == '1:161']
        self.assertEqual((row['action'], row['execution'], row['direction'], row['weight']), ('comment', 'intent_not_executed', 'positive', 3))
        self.assertEqual([r['content_id'] for r in ledger.rows()], ['1:161'])
        # 3. interest without an explicit stance (archival / curiosity): unknown, resolved at night, not counted
        archival = h.bili_read(162)
        await h.decide(archival, {'interested': True, 'text': '', 'interest_score': 90, 'reason': '先留着以后查'}, permissions=blocked)
        await h.receipt(archival, 'favorite')
        [row] = [r for r in rows(h) if r['content_id'] == '1:162']
        self.assertEqual((row['direction'], row['action'], row['weight']), ('unknown', 'favorite', 5))
        self.assertNotIn('1:162', [r['content_id'] for r in ledger.rows()])
        self.assertEqual(h.state.get('opp_reason:' + archival['event_id']), '先留着以后查')
        # 4. explicit false / invalid / missing with blocked empty text: no willingness is invented; explicit false beats stray text;
        #    a missing field with real text falls back to inference (older models)
        def comment_rows(oid):
            return [r for r in rows(h) if r['content_id'] == '1:%d' % oid and r['action'] == 'comment']
        cases = [(170, {'comment_willing': False, 'text': ''}, 0), (171, {'comment_willing': 'yes', 'text': ''}, 0),
                 (172, {'text': ''}, 0), (173, {'comment_willing': False, 'text': '想说一句'}, 0),
                 (174, {'text': '想说一句'}, 1), (175, {'comment_willing': True, 'text': ''}, 1)]
        for oid, extra, expected in cases:
            await h.decide(h.bili_read(oid), {'interested': True, 'interest_score': 85, 'reason': 'r', 'stance': 'positive', **extra},
                           permissions=blocked)
            self.assertEqual(len(comment_rows(oid)), expected, (oid, extra))
        # willingness never needs a positive stance and a low interest score cancels it
        await h.decide(h.bili_read(176), {'interested': True, 'interest_score': 40, 'comment_willing': True, 'text': '', 'reason': 'r'},
                       permissions=blocked)
        self.assertEqual(comment_rows(176), [])

    async def test_pending_moderation_and_unexecuted_intent_are_distinct_states(self):
        h = Harness(self)
        evidence = h.state.ingest(h.bili_read(130))['event_id']
        ledger = impressions.Ledger(h.state)
        common = dict(platform='xiaohongshu', opportunity_id='o', content_id='n1', target_type='content', target_ref='n1',
                      direction='positive', mode='autonomous', action='comment', occurred_at=h.clock(), evidence_event_ids=[evidence])
        ledger.record(execution='intent_not_executed', **common)
        self.assertEqual(rows(h)[0]['execution'], 'intent_not_executed')
        self.assertEqual(rows(h)[0]['weight'], 3)
        ledger.record(execution='submitted_pending_moderation', **common)
        self.assertEqual(rows(h)[0]['execution'], 'submitted_pending_moderation')   # not "executed" / public
        ledger.record(execution='failed', **common)                                 # a later failure report cannot downgrade or inflate
        self.assertEqual((rows(h)[0]['execution'], rows(h)[0]['weight']), ('submitted_pending_moderation', 3))

    async def test_window_points_independence_and_expiry(self):
        h = Harness(self)
        ledger = impressions.Ledger(h.state)
        first, second = h.bili_read(140), h.bili_read(140, suffix=':again')
        await h.decide(first, GOOD)
        await h.receipt(first, 'favorite')                              # one contact of 5 points: not enough
        self.assertEqual(ledger.candidates(), [])
        h.clock.advance(1 * HOUR)
        await h.decide(second, GOOD)                                    # genuinely revisited, but only 1h apart
        await h.receipt(second, 'like')
        self.assertEqual(ledger.candidates(), [])                       # 5+3 points but not two choices >= 2h apart
        h.clock.advance(2 * HOUR)
        third = h.bili_read(140, suffix=':third')
        await h.decide(third, GOOD)
        [candidate] = ledger.candidates()
        self.assertGreaterEqual(candidate['points'], 6)
        self.assertGreaterEqual(candidate['independent'], 2)
        h.clock.advance(15 * DAY)                                       # the 14-day window moves on: recomputed from real rows
        self.assertEqual(ledger.candidates(), [])

    async def test_interest_promotion_requires_looked_up_contact_evidence(self):
        h = Harness(self)
        ledger = impressions.Ledger(h.state)
        events = [h.bili_read(150), h.bili_read(150, suffix=':b')]
        await h.decide(events[0], GOOD)
        await h.receipt(events[0], 'favorite')
        h.clock.advance(3 * HOUR)
        events[1]['occurred_at'] = h.clock()
        await h.decide(events[1], GOOD)
        await h.receipt(events[1], 'favorite')
        keys = [r['impression_key'] for r in rows(h)]
        evidence = [r['evidence_ids'] for r in rows(h)]
        material = [h.state.stored_event_id(e) for e in events]
        change = {'gaming_preferences': ['像素风肉鸽']}
        other = [h.state.ingest(h.bili_read(i, '另一条游戏介绍%d' % i, author=str(i)))['event_id'] for i in (901, 902)]
        # two ordinary distinct external sources no longer promote a stable interest
        self.assertEqual(h.state.propose_changes(change, other, 'x', automatic=True)['reason'], 'interest_requires_contact_evidence')
        # forged / unknown keys, keys of a still-unresolved work, or evidence not cited by the contacts do not qualify
        self.assertEqual(h.state.propose_changes(change, other, 'x', automatic=True, contact_keys=['imp:forged'])['reason'],
                         'interest_requires_contact_evidence')
        self.assertEqual(h.state.propose_changes(change, material, 'x', automatic=True, contact_keys=keys)['reason'],
                         'sustained_evidence_or_explicit_feedback_required')    # unresolved label: contacts do not qualify yet
        ledger.resolve(keys, topic='像素风肉鸽')
        self.assertEqual(h.state.propose_changes(change, other, 'x', automatic=True, contact_keys=keys)['reason'],
                         'interest_requires_contact_evidence')                   # evidence ids must come from those contacts
        # the proposed value must name the contacts' own topic
        self.assertEqual(h.state.propose_changes({'gaming_preferences': ['美食']}, material, 'x', automatic=True, contact_keys=keys)['reason'],
                         'sustained_evidence_or_explicit_feedback_required')
        accepted = h.state.propose_changes(change, material, 'x', automatic=True, contact_keys=keys)
        self.assertTrue(accepted['accepted'], accepted)
        self.assertEqual(h.state.persona()['dynamic']['gaming_preferences'], ['像素风肉鸽'])
        # the self reaction is never external material evidence
        bot = h.state.ingest({'event_id': 'self:1', 'platform': 'bilibili', 'bot_account_id': '1000', 'conversation_id': 'x', 'actor_id': '1000',
                              'origin': 'bot_self', 'event_type': 'comment', 'content': '我的评论', 'occurred_at': h.clock()})
        ledger.record(platform='bilibili', opportunity_id='selfonly', content_id='1:9', target_type='content', target_ref='1:9', direction='positive',
                      mode='autonomous', execution='executed', action='favorite', occurred_at=h.clock(), evidence_event_ids=[bot['event_id']], topic='自言自语')
        self.assertEqual([r['evidence_ids'] for r in rows(h) if r['opportunity_id'] == 'selfonly'], ['[]'])
