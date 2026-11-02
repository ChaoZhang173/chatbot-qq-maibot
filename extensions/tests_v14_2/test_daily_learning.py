"""Phase 2C/3B: daily selective learning - sampling, activation watermark, crash safety, scope privacy, interest gates."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from maibot_unified_core.daily_learning import DailyLearning, is_trivial, scope_of
from maibot_unified_core.store import CHINA, State
from v14_2_support import BOT, Crash, Harness, HOUR

DAY = 86400.0


def qq(h, eid, text, session='a', actor='u1', private=False):
    return h.state.ingest({'event_id': eid, 'platform': 'qq', 'bot_account_id': BOT, 'conversation_id': session, 'actor_id': actor,
                           'origin': 'external_user', 'event_type': 'message_received', 'content': text, 'source_id': eid,
                           'sharing_scope': 'conversation' if private else 'policy', 'learning_allowed': True, 'occurred_at': h.clock(),
                           'metadata': {'is_private': private}})['event_id']


def xhs(h, eid, text):
    return h.state.ingest({'event_id': eid, 'platform': 'xiaohongshu', 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:abcdef123456',
                           'actor_id': 'author1', 'origin': 'platform_content', 'event_type': 'content_read', 'content': text, 'source_id': eid,
                           'sharing_scope': 'public', 'learning_allowed': True, 'occurred_at': h.clock()})['event_id']


def prompts(h):
    return [json.loads(c['prompt'][1]['content']) for c in h.llm.calls]


def learned(h):
    return {r['event_id']: r['status'] for r in h.state.db.execute('SELECT event_id,status FROM learning')}


class SelectiveSamplingTests(unittest.IsolatedAsyncioTestCase):
    def test_local_trivial_filter(self):
        for text in ('哈哈哈哈', '好的', '😂😂😂', '666666', '晚安', '[链接]', '。。。。', '啊啊啊啊啊'):
            self.assertTrue(is_trivial(text), text)
        for text in ('最近在玩一款像素风肉鸽游戏', '这个番的反转太刺激了'):
            self.assertFalse(is_trivial(text), text)

    async def test_recent_sample_only_with_4000_legacy_events_under_three_calls(self):
        h = Harness(self)
        base = h.clock()
        old = h.bulk_events(4010, 'old', 'a', base - 8 * DAY)                         # 8 days of legacy QQ history
        recent_old = h.bulk_events(40, 'recent-old', 'b', base - 3 * HOUR, step=60)   # recent, but BEFORE the cut-over
        h.clock.advance(10)
        cutover = h.activate()
        self.assertEqual(cutover, h.clock())
        h.clock.advance(HOUR)
        fresh = [qq(h, 'qa%d' % i, text) for i, text in enumerate([
            '哈哈哈哈', '好的', '😂😂😂', '666666', '最近在玩一款像素风肉鸽游戏，节奏很爽', '最近在玩一款像素风肉鸽游戏，节奏很爽',
            '这个番的反转太刺激了真的', '想试试本地小模型的量化推理', '今天吃了抹茶布丁味道很不错', '有人了解过最新的AI科技进展吗', '周末想找点新游戏玩玩'])]
        fresh += [qq(h, 'qb%d' % i, text, session='b', actor='u2') for i, text in enumerate(['群里在聊游戏推荐的话题', '大家讨论了新出的动漫', '分享一个美食小店'])]
        fresh += [h.state.ingest(h.bili_read(700 + i, '像素风肉鸽游戏介绍%d' % i, author=str(900 + i)))['event_id'] for i in range(2)]
        fresh.append(xhs(h, 'x1', '一篇关于游戏的笔记内容'))
        private = qq(h, 'qp1', '私下聊的游戏秘密话题内容', session='p-user', actor='user-private', private=True)
        h.clock.advance(HOUR)
        h.llm.queue.extend([h.events_batch_answer()] * 5)

        result = await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 3)                                   # the normal 3-call limit, five candidate scopes
        self.assertEqual(h.state.quota_used('background'), 3)
        seen_ids = {e['event_id'] for p in prompts(h) for e in p['events']}
        self.assertTrue(seen_ids <= set(fresh + [private]))
        self.assertTrue(seen_ids.isdisjoint(old) and seen_ids.isdisjoint(recent_old))     # history and pre-cut-over material never chosen
        texts = [e['text'] for p in prompts(h) for e in p['events']]
        for trivial in ('哈哈哈哈', '好的', '😂😂😂', '666666'):
            self.assertNotIn(trivial, texts)
        self.assertEqual(sum(t == '最近在玩一款像素风肉鸽游戏，节奏很爽' for t in texts) + 0 <= 1, True)   # duplicates collapsed
        # a private message never shares a batch with other scopes and is not exposed to a public/other-scope batch
        for payload in prompts(h):
            if any('秘密话题' in e['text'] for e in payload['events']):
                self.assertTrue(payload['scope'].startswith('private:'))
                self.assertEqual({e['platform'] for e in payload['events']}, {'qq'})
        # only the SELECTED events carry a learning outcome; history is neither learned nor failed
        states = learned(h)
        self.assertEqual(set(states), seen_ids)
        self.assertEqual(set(states.values()), {'skipped'})
        for event_id in old[:50] + old[-50:] + recent_old:
            self.assertNotIn(event_id, states)
        status = h.service.daily.status()
        self.assertEqual(status['historical_raw'], 4050)                        # retained history, reported apart from the sample
        self.assertEqual(status['selected'], len(seen_ids))
        self.assertEqual(status['processed'], len(seen_ids))
        self.assertGreater(status['recent_unselected'], 0)
        self.assertEqual(status['selected_batches_open'], 0)
        self.assertTrue(status['done'])
        self.assertEqual(result['summary']['historical_raw'], 4050)
        # the same day never replays or drains history
        again = await h.service.daily.run()
        self.assertEqual(again['skipped'], 'already_done')
        self.assertEqual(len(h.llm.calls), 3)
        self.assertEqual(await h.service.learning_tick(), {'skipped': 'daily_batch_active'})   # legacy half-hour extraction is cut over

    async def test_activation_watermark_survives_restart_and_excludes_everything_before_it(self):
        h = Harness(self)
        qq(h, 'pre1', '切换前两小时的一条普通聊天内容很有价值')
        h.clock.advance(2 * HOUR)
        cutover = h.activate()
        h.clock.advance(5)
        h.state.close()
        h.state = h.service.state = State(h.root / 'state.sqlite', h.service.policy, now=h.clock)
        self.addCleanup(h.state.close)
        restarted = DailyLearning(h.service)
        h.clock.advance(HOUR)
        self.assertEqual(restarted.activate(), cutover)                        # a restart does not reset the watermark
        qq(h, 'post1', '切换后收到的一条普通聊天内容也很有价值')
        h.clock.advance(HOUR)
        self.assertEqual([e['event_id'] for e in restarted.pending_events()], [h.state.stored_event_id(
            {'platform': 'qq', 'bot_account_id': BOT, 'event_id': 'post1'})])
        # even 30h later the pre-cut-over message is not a candidate; the post-cut-over one has aged out of the 24h window too
        h.clock.advance(30 * HOUR)
        self.assertEqual(restarted.pending_events(), [])
        self.assertEqual(restarted.status()['historical_raw'], 2)
        self.assertEqual(learned(h), {})                                       # nothing was marked learned or failed

    async def test_not_activated_means_no_candidates(self):
        h = Harness(self)
        qq(h, 'e1', '一条完全正常的聊天内容可以学习吗')
        self.assertEqual(h.service.daily.pending_events(), [])
        h.set_policy(v14_2={'daily_learning': {'enabled': False}})
        self.assertEqual((await h.service.daily.run())['skipped'], 'daily_learning_disabled')
        self.assertIsNone(h.service.daily.activation_at())


class CrashSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, count=2):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        ids = [qq(h, 'c%d' % i, '最近在玩的游戏很有意思第%d个话题值得记' % i) for i in range(count)]
        h.clock.advance(HOUR)
        return h, ids

    async def test_transient_error_before_model_response_retries_within_budget(self):
        h, ids = self.setup()
        h.llm.queue.extend([RuntimeError('timeout'), h.events_batch_answer(True)])
        first = await h.service.daily.run()
        [batch] = h.service.daily.batches()
        self.assertEqual((batch['status'], batch['attempts']), ('pending', 1))
        self.assertEqual(h.memory.writes(), [])
        h.clock.advance(601)
        await h.service.daily.run()
        [batch] = h.service.daily.batches()
        self.assertEqual(batch['status'], 'completed')
        self.assertEqual(len(h.llm.calls), 2)
        self.assertEqual(h.state.quota_used('daily_retry'), 1)
        self.assertEqual(h.state.quota_used('background'), 2)                 # every request, failed ones included, is counted
        self.assertEqual(set(learned(h).values()), {'stored'})
        # retries are bounded: the third failure ends the batch, the events are kept (not failed yet) and no fourth call is made
        h2, _ = self.setup()
        h2.llm.queue.extend([RuntimeError('a'), RuntimeError('b'), RuntimeError('c'), h2.events_batch_answer(True)])
        for _ in range(4):
            await h2.service.daily.run()
            h2.clock.advance(1900)
        [batch] = h2.service.daily.batches()
        self.assertEqual(batch['status'], 'failed')
        self.assertEqual(len(h2.llm.calls), 3)                                 # the same material is not re-sent the same day
        self.assertEqual(h2.state.quota_used('daily_retry'), 2)
        self.assertEqual(learned(h2), {})                                      # kept, traceable, not falsely failed after one bad night

    async def test_output_is_persisted_before_memory_and_replayed_without_new_model_call(self):
        h, ids = self.setup()
        h.memory.fail = 1
        h.llm.queue.append(h.events_batch_answer(True))
        await h.service.daily.run()
        [batch] = h.service.daily.batches()
        self.assertEqual(batch['status'], 'pending')
        self.assertTrue(json.loads(batch['output'])['items'])                  # saved before the failing write
        self.assertEqual(len(h.llm.calls), 1)
        h.clock.advance(61)
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 1)                                  # no second model call
        self.assertEqual(h.service.daily.batches()[0]['status'], 'completed')
        self.assertEqual(set(learned(h).values()), {'stored'})
        self.assertEqual(len(h.memory.written), 2)

    async def test_memory_written_then_crash_before_commit_marker_is_idempotent_after_restart(self):
        h, ids = self.setup()
        h.llm.queue.append(h.events_batch_answer(True))
        daily = h.service.daily
        real = daily.commit_key
        calls = {'n': 0}

        def crash_once(key, item):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Crash()
            return real(key, item)

        with patch.object(daily, 'commit_key', crash_once):
            with self.assertRaises(Crash):
                await daily.run()
        [batch] = daily.batches()
        self.assertEqual((batch['status'], json.loads(batch['committed'])), ('running', []))
        self.assertEqual(len(h.memory.written), 1)                             # memory succeeded, the marker did not
        h.clock.advance(700)                                                   # lease expired: a restarted process takes over
        await DailyLearning(h.service).run()
        self.assertEqual(len(h.llm.calls), 1)
        self.assertEqual(daily.batches()[0]['status'], 'completed')
        self.assertEqual(len(h.memory.written), 2)
        self.assertEqual(len(h.memory.writes()), 2)                            # the first event was NOT written twice (learning row)
        self.assertEqual(set(learned(h).values()), {'stored'})

    async def test_crash_between_memory_write_and_learning_mark_rewrites_same_external_id(self):
        h, ids = self.setup()
        h.llm.queue.append(h.events_batch_answer(True))
        real = h.state.mark_learned
        calls = {'n': 0}

        def crash_once(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Crash()
            return real(*args, **kwargs)

        with patch.object(h.state, 'mark_learned', crash_once):
            with self.assertRaises(Crash):
                await h.service.daily.run()
        h.clock.advance(700)
        await DailyLearning(h.service).run()
        self.assertEqual(len(h.memory.written), 2)                             # stable external ids: the replay overwrote, not duplicated
        self.assertEqual(len(h.memory.writes()), 3)
        self.assertEqual(set(learned(h).values()), {'stored'})
        self.assertEqual(len(h.llm.calls), 1)

    async def test_saved_batch_is_recovered_across_the_next_window_and_the_24h_cutoff(self):
        h, ids = self.setup()
        h.llm.queue.append(h.events_batch_answer(True))
        daily = h.service.daily
        real = daily.commit_key
        calls = {'n': 0}

        def crash_once(key, item):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Crash()
            return real(key, item)

        with patch.object(daily, 'commit_key', crash_once):
            with self.assertRaises(Crash):
                await daily.run()
        h.clock.advance(30 * HOUR)                                             # next 03:00 window; the events left the 24h window
        self.assertEqual(daily.pending_events(), [])
        await DailyLearning(h.service).run()
        self.assertEqual(daily.batches()[0]['status'], 'completed')            # adopted independently of fresh selection
        self.assertEqual(len(h.llm.calls), 1)                                  # saved output, no new model call
        self.assertEqual(set(learned(h).values()), {'stored'})

    async def test_retry_after_midnight_is_charged_to_the_real_current_quota_day(self):
        h, ids = self.setup()
        h.llm.queue.extend([RuntimeError('night'), h.events_batch_answer(True)])
        await h.service.daily.run()
        h.clock.advance(26 * HOUR)
        await h.service.daily.run()
        day_now = datetime.fromtimestamp(h.clock(), CHINA).date().isoformat()
        rows = h.state.db.execute("SELECT day FROM quotas WHERE kind='daily_retry'").fetchall()
        self.assertEqual([r[0] for r in rows], [day_now])
        self.assertEqual(h.service.daily.batches()[0]['status'], 'completed')

    async def test_reserved_background_positions_belong_to_the_daily_batch(self):
        h = Harness(self)
        self.assertEqual(sum(h.state.reserve_quota('legacy%d' % i) for i in range(20)), 15)
        self.assertTrue(h.state.reserve_quota('daily-1', protected=True))
        self.assertEqual(h.state.quota_used('background'), 16)

    async def test_legacy_tick_stops_at_its_cap_without_marking_pending_events_failed(self):
        h = Harness(self, policy={'v14_2': {'daily_learning': {'enabled': False}}})
        for i in range(22):
            qq(h, 'l%d' % i, '最近在玩的游戏很有意思第%d个话题值得记' % i)
            h.clock.advance(1)
        h.llm.queue.extend([{'remember': False, 'feedback': None}] * 30)
        for _ in range(6):
            await h.service.learning_tick(force=True)
        self.assertEqual(len(h.llm.calls), 15)                                   # the last 5 positions stay for the daily batch
        self.assertEqual(h.state.quota_used('background'), 15)
        self.assertEqual(sorted(set(learned(h).values())), ['skipped'])          # nothing was falsely marked failed
        self.assertEqual(len(learned(h)), 15)


class BudgetRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Quota-denied batches wait durably for the next Beijing quota day; the actual-day caps are atomic and shared."""

    def setup(self, hour=10, count=2, session='a'):
        h = Harness(self, hour=hour)
        h.activate()
        h.clock.advance(HOUR)
        ids = [qq(h, '%s%d' % (session, i), '最近在玩的游戏很有意思%s第%d个话题值得记' % (session, i), session=session) for i in range(count)]
        h.clock.advance(HOUR)
        return h, ids

    @staticmethod
    def select_then_exhaust(h):
        """The batch is SELECTED while a slot exists; the day's ordinary calls are then spent elsewhere before it runs."""
        from maibot_unified_core.daily_learning import window_day
        assert h.service.daily.plan_next(window_day(h.clock(), 3))
        for i in range(3):
            assert h.state.reserve_quota('earlier-batch-%d' % i, kind='daily_ordinary', limit=3)

    async def test_exhausted_budget_then_next_day_is_processed_exactly_once(self):
        h, ids = self.setup()
        self.select_then_exhaust(h)
        h.llm.queue.extend([h.events_batch_answer(True)] * 3)
        result = await h.service.daily.run()
        self.assertEqual(h.llm.calls, [])                                       # no model call without an ordinary slot
        [batch] = h.service.daily.batches()
        self.assertEqual(batch['status'], 'pending')                            # durably pending, NOT a stranded 'deferred'
        self.assertEqual(batch['attempts'], 0)
        self.assertEqual(sorted(json.loads(batch['manifest'])), sorted(ids))     # the selection is preserved
        self.assertGreater(batch['next_at'], h.clock())
        self.assertFalse(result['done'])
        self.assertIsNone(h.state.get('daily_done:' + result['day']))            # not reported as a successful no-change day
        self.assertEqual(h.service.daily.status()['selected_batches_open'], 1)
        self.assertEqual(learned(h), {})
        for _ in range(3):                                                      # no hot loop while the budget is spent
            await h.service.daily.run()
            h.clock.advance(60)
        self.assertEqual((len(h.llm.calls), len(h.service.daily.batches())), (0, 1))
        # next Beijing quota day: the SAME batch is executed, once
        h.clock.advance(15 * HOUR)
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 1)
        self.assertEqual(h.service.daily.batches()[0]['status'], 'completed')
        self.assertEqual(set(learned(h).values()), {'stored'})
        self.assertEqual(len(h.memory.written), 2)
        self.assertEqual(h.state.quota_used('daily_ordinary'), 1)
        await h.service.daily.run()
        await h.service.daily.run()
        self.assertEqual((len(h.llm.calls), len(h.memory.writes())), (1, 2))      # once-only

    async def test_recovered_old_batch_and_new_day_batches_share_the_actual_day_cap(self):
        h, ids = self.setup(hour=10)
        h.set_policy(allowed_qq_sessions=['a', 'b', 'c', 'd', 'p-owner', 'p-user'])
        self.select_then_exhaust(h)
        await h.service.daily.run()                                              # batch A is deferred to the next quota day
        h.clock.advance(17.5 * HOUR)                                             # next day 03:30; a new window day starts at 03:00
        for session in ('b', 'c', 'd'):
            qq(h, 'n%s' % session, '今天新的游戏话题很有意思值得学习%s' % session, session=session)
        h.clock.advance(30 * 60)
        h.llm.queue.extend([h.events_batch_answer(True)] * 6)
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 3)                                    # recovered A + two new ones, never four
        self.assertEqual(h.state.quota_used('daily_ordinary'), 3)
        self.assertEqual(h.state.quota_used('background'), 3)
        self.assertEqual(len(h.service.daily.batches()), 3)
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 3)

    async def test_partial_denial_burns_no_slot(self):
        h, ids = self.setup()
        for i in range(20):
            self.assertTrue(h.state.reserve_quota('bg%d' % i, protected=True))     # the shared background cap is full
        await h.service.daily.run()
        self.assertEqual(h.state.quota_used('daily_ordinary'), 0)                  # the purpose slot was not spent
        self.assertEqual(h.service.daily.batches()[0]['attempts'], 0)
        # a retry denied by the shared cap does not burn retry quota either
        h2, _ = self.setup()
        h2.llm.queue.append(RuntimeError('timeout'))
        await h2.service.daily.run()
        for i in range(19):
            self.assertTrue(h2.state.reserve_quota('bg%d' % i, protected=True))
        h2.clock.advance(601)
        await h2.service.daily.run()
        self.assertEqual(h2.state.quota_used('daily_retry'), 0)
        [batch] = h2.service.daily.batches()
        self.assertEqual((batch['status'], batch['attempts']), ('pending', 1))
        self.assertGreater(batch['next_at'], h2.clock())
        self.assertEqual(len(h2.llm.calls), 1)

    async def test_crash_at_the_final_request_boundary_ends_in_terminal_failure_not_endless_deferral(self):
        h, ids = self.setup()
        h.llm.queue.extend([RuntimeError('t1'), RuntimeError('t2'), Crash(), h.events_batch_answer(True)])
        await h.service.daily.run()                      # attempt 1 fails (transient)
        h.clock.advance(601)
        await h.service.daily.run()                      # attempt 2 (retry 1) fails
        h.clock.advance(1801)
        with self.assertRaises(Crash):
            await h.service.daily.run()                  # attempt 3 (retry 2): the process dies right at the request
        [batch] = h.service.daily.batches()
        self.assertEqual((batch['status'], batch['attempts'], batch['output']), ('running', 3, ''))
        self.assertEqual(len(h.llm.calls), 3)
        self.assertEqual((h.state.quota_used('daily_ordinary'), h.state.quota_used('daily_retry'), h.state.quota_used('background')), (1, 2, 3))
        # restart after the lease expires, on this and later days: terminal failure, never a 4th request, never endless deferral
        for _ in range(3):
            h.clock.advance(26 * HOUR)
            await DailyLearning(h.service).run()
        [batch] = h.service.daily.batches()
        self.assertEqual((batch['status'], batch['detail']), ('failed', 'attempts_exhausted'))
        self.assertEqual(len(h.llm.calls), 3)            # the scripted success answer was never consumed
        self.assertEqual(learned(h), {})                 # one bad night does not falsely fail the events
        self.assertEqual(h.state.get('daily_fail:' + ids[0]), 1)

    async def test_denial_never_creates_a_phantom_attempt(self):
        h, ids = self.setup()
        h.llm.queue.append(RuntimeError('timeout'))
        await h.service.daily.run()                      # attempt 1 consumed
        for day in range(4):                             # repeated recoveries, each day under a full background cap
            h.clock.advance(26 * HOUR)
            while h.state.quota_used('background') < 20:
                self.assertTrue(h.state.reserve_quota('bg%d-%d' % (day, h.state.quota_used('background')), protected=True))
            await h.service.daily.run()
        [batch] = h.service.daily.batches()
        self.assertEqual((batch['status'], batch['attempts']), ('pending', 1))   # still retryable, nothing burnt

    async def test_empty_eligible_manifest_spends_nothing_after_recovery(self):
        h, ids = self.setup()
        self.select_then_exhaust(h)
        await h.service.daily.run()
        for event_id in ids:
            h.state.invalidate_event(event_id, 'user removed it')
        h.clock.advance(15 * HOUR)
        await h.service.daily.run()
        self.assertEqual(h.llm.calls, [])
        self.assertEqual(h.service.daily.batches()[0]['status'], 'no_change')
        self.assertEqual((h.state.quota_used('daily_ordinary'), h.state.quota_used('background')), (0, 0))

    async def test_host_memory_semantics_chat_filter_and_external_id_replay(self):
        h, ids = self.setup()
        h.llm.queue.append(h.events_batch_answer(True))

        async def filtered(operation, **kw):
            return {'success': True, 'stored_ids': [], 'skipped_ids': [kw['external_id']], 'detail': 'chat_filtered'}
        h.service.memory = filtered
        await h.service.daily.run()
        rows = {r['event_id']: r['status'] for r in h.state.db.execute('SELECT event_id,status FROM learning')}
        self.assertEqual(set(rows.values()), {'skipped'})                          # a Host-filtered write is not "stored memory"
        # a replay of an already-written external_id (Host: skipped_ids + reason exists) counts as stored, no duplicate
        h2, ids2 = self.setup()
        h2.llm.queue.append(h2.events_batch_answer(True))
        seen = []

        async def replay(operation, **kw):
            seen.append(kw['external_id'])
            return {'success': True, 'stored_ids': [], 'skipped_ids': ['paragraph-hash'], 'detail': '', 'reason': 'exists'}
        h2.service.memory = replay
        await h2.service.daily.run()
        self.assertEqual(set(learned(h2).values()), {'stored'})
        self.assertEqual(sorted(seen), sorted('unified:' + e for e in ids2))       # the stable Host identity is used


class ScopeAndGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_bilibili_and_xiaohongshu_material_never_groups_with_public(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        h.state.ingest(h.bili_read(801, '公开的游戏介绍内容很长很长很长', author='1'))
        h.state.ingest(h.bili_read(802, '私下聊的游戏秘密话题内容很私密', author='2', scope='conversation'))
        h.state.ingest({'event_id': 'xp', 'platform': 'xiaohongshu', 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:privateprivate',
                        'actor_id': 'a', 'origin': 'platform_content', 'event_type': 'content_read', 'content': '仅自己可见的小红书私有笔记内容',
                        'source_id': 'xp', 'sharing_scope': 'conversation', 'learning_allowed': True, 'occurred_at': h.clock(),
                        'metadata': {'is_private': True}})
        self.assertTrue(scope_of(h.state.get_event(h.state.stored_event_id({'platform': 'bilibili', 'bot_account_id': '1000',
                                                                            'event_type': 'content_read', 'event_id': 'bilibili:1000:content_read:802'})))
                        .startswith('private:'))
        h.clock.advance(HOUR)
        h.llm.queue.extend([h.events_batch_answer()] * 3)
        await h.service.daily.run()
        for payload in prompts(h):
            private = any('秘密' in e['text'] or '私有' in e['text'] for e in payload['events'])
            if private:
                self.assertTrue(payload['scope'].startswith('private:'), payload['scope'])
                self.assertTrue(all('公开' not in e['text'] for e in payload['events']))
            else:
                self.assertTrue(payload['scope'].endswith(':public'))

    async def test_batch_proposals_cannot_bypass_the_interest_gate_but_expression_growth_works(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        one = h.state.ingest(h.bili_read(810, '像素风肉鸽游戏介绍一', author='11'))['event_id']
        two = h.state.ingest(h.bili_read(811, '像素风肉鸽游戏介绍二', author='12'))['event_id']
        h.clock.advance(HOUR)

        def answer(prompt):
            events = json.loads(prompt[1]['content'])['events']
            base = h.events_batch_answer()(prompt)
            base['proposals'] = [{'field': 'gaming_preferences', 'value': ['像素风肉鸽'], 'evidence_ids': [e['event_id'] for e in events], 'reason': 'two sources'},
                                 {'field': 'expression_habits', 'value': '喜欢用游戏里的小梗接话但不重复', 'evidence_ids': [e['event_id'] for e in events], 'reason': 'seen twice'}]
            return base

        h.llm.queue.append(answer)
        await h.service.daily.run()
        dynamic = h.state.persona()['dynamic']
        self.assertEqual(dynamic['gaming_preferences'], [])                    # two distinct external sources are not enough any more
        self.assertEqual(dynamic['expression_habits'], '喜欢用游戏里的小梗接话但不重复')
        reasons = [json.loads(r[0])['reason'] for r in h.state.db.execute('SELECT payload FROM persona_proposals')]
        self.assertIn('interest_requires_contact_evidence', reasons)

    async def test_missing_or_conflicting_outcomes_never_hot_loop(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        ids = [qq(h, 'm%d' % i, '最近在玩的游戏很有意思第%d个话题值得记' % i) for i in range(3)]
        h.clock.advance(HOUR)
        h.llm.queue.extend([{'items': []}, h.events_batch_answer()])
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 1)                                  # an empty answer is not re-offered immediately
        for event_id in ids:
            self.assertEqual(h.state.get('daily_defer:' + event_id)['count'], 1)
        self.assertEqual(learned(h), {})                                       # deferred, not marked learned or failed
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls), 1)
        # conflicting duplicate outcomes are rejected as an invalid answer
        h2 = Harness(self)
        h2.activate()
        h2.clock.advance(HOUR)
        eid = qq(h2, 'd1', '最近在玩的游戏很有意思这个话题值得记')
        h2.clock.advance(HOUR)
        h2.llm.queue.append({'items': [{'event_id': eid, 'remember': True, 'summary': '一', 'category': '游戏'},
                                       {'event_id': eid, 'remember': False}]})
        await h2.service.daily.run()
        self.assertEqual(h2.service.daily.batches()[0]['status'], 'failed')
        self.assertEqual(learned(h2)[eid], 'failed')

    async def test_removed_or_blocked_material_is_not_sent_or_committed_even_from_saved_output(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        eid = qq(h, 'r1', '最近在玩的游戏很有意思这个话题值得记')
        h.clock.advance(HOUR)
        h.llm.queue.append(h.events_batch_answer(True))
        daily = h.service.daily
        real = daily.commit_key

        def crash(key, item):
            raise Crash()

        with patch.object(daily, 'commit_key', crash):
            with patch.object(daily, 'store_item', side_effect=Crash()):
                with self.assertRaises(Crash):
                    await daily.run()
        h.state.invalidate_event(eid, 'user removed it')                      # authorization changes while the output is saved
        h.clock.advance(700)
        await DailyLearning(h.service).run()
        self.assertEqual(h.memory.writes(), [])                                # re-checked before commit, also for resumed output
        self.assertEqual(len(h.llm.calls), 1)


class NightlyDirectionTests(unittest.IsolatedAsyncioTestCase):
    """Unknown direction is resolved only from the robot's recorded reaction, never from the material's topic."""

    async def test_direction_needs_recorded_reaction_evidence(self):
        import time
        h = Harness(self)
        h.set_policy(xhs_enabled=True, xhs_bot_account='xhsbot')
        h.activate()
        h.clock.advance(HOUR)
        # (a) a bare XHS read: no action, no reason -> may stay unknown
        await h.service.dispatch({'op': 'xhs_event', 'opportunity': 'a' * 64, 'event': {
            'event_id': 'xhs:read:abcdef123456:d1', 'platform': 'xiaohongshu', 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:feed:abcdef123456',
            'actor_id': 'author1', 'occurred_at': time.time(), 'event_type': 'content_read', 'content': '一篇关于游戏的笔记很长很长',
            'source_id': 'abcdef123456', 'origin': 'platform_content'}}, admin=True)
        # (b) a Bili contact where the robot recorded its own reason (neutral stance, saved for later)
        archival = h.bili_read(180, '像素风肉鸽游戏介绍很长很长很长', author='41')
        h.llm.queue.append({'interested': True, 'text': '', 'interest_score': 80, 'reason': '先留着以后查', 'stance': 'neutral'})
        await h.service.dispatch({'op': 'decide', 'event': archival, 'task': 'proactive', 'material': {'text': archival['content'], 'evidence': 'title_description'},
                                  'permissions': {}}, admin=True)
        h.clock.advance(HOUR)

        def answer(prompt):
            payload = json.loads(prompt[1]['content'])
            keys = [c['impression_key'] for e in payload['events'] for c in e['contacts']]
            base = h.events_batch_answer()(prompt)
            base['entities'] = [{'impression_keys': keys, 'topic': '游戏内容', 'direction': 'positive'}]
            return base
        h.llm.queue.extend([answer, answer])
        await h.service.daily.run()
        by_platform = {r['platform']: dict(r) for r in h.state.db.execute('SELECT * FROM impressions')}
        self.assertEqual((by_platform['xiaohongshu']['topic'], by_platform['xiaohongshu']['direction']), ('游戏内容', 'unknown'))
        self.assertEqual((by_platform['bilibili']['topic'], by_platform['bilibili']['direction']), ('游戏内容', 'positive'))


class InterestPipelineTests(unittest.IsolatedAsyncioTestCase):
    GOOD = {'interested': True, 'text': '', 'interest_score': 85, 'reason': '喜欢', 'stance': 'positive'}

    async def contacts(self, h):
        first, second = h.bili_read(820, '像素风肉鸽游戏介绍'), h.bili_read(820, '像素风肉鸽游戏介绍再看', suffix=':again')
        await h.decide(first, self.GOOD)
        await h.receipt(first, 'favorite')
        h.clock.advance(3 * HOUR)
        second['occurred_at'] = h.clock()
        await h.decide(second, self.GOOD)
        await h.receipt(second, 'like')
        h.clock.advance(HOUR)
        return first, second

    def entity_then_verdict(self, h, verdict='promote'):
        def events_answer(prompt):
            payload = json.loads(prompt[1]['content'])
            keys = [c['impression_key'] for e in payload['events'] for c in e['contacts']]
            self.assertTrue(keys)                                              # the unresolved contacts are actually offered to the model
            base = h.events_batch_answer()(prompt)
            base['entities'] = [{'impression_keys': keys, 'topic': '像素风肉鸽', 'direction': 'positive'}]
            return base

        def interest_answer(prompt):
            payload = json.loads(prompt[1]['content'])
            [candidate] = payload['candidates']
            self.assertIn('像素风肉鸽游戏介绍', json.dumps(candidate['evidence'], ensure_ascii=False))     # bounded original material
            self.assertTrue(candidate['reactions'])                                                      # and the recorded reaction
            return {'verdicts': [{'candidate_key': candidate['candidate_key'], 'verdict': verdict, 'field': 'gaming_preferences',
                                  'value': ['像素风肉鸽'], 'reason': '两次主动接触'}]}
        return [events_answer, interest_answer]

    async def test_contacts_to_nightly_resolution_to_promotion_end_to_end(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        await self.contacts(h)
        before = len(h.llm.calls)                                              # the two browse decisions above
        h.llm.queue.extend(self.entity_then_verdict(h))
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls) - before, 2)                         # one material batch, one interest review
        self.assertEqual(h.state.persona()['dynamic']['gaming_preferences'], ['像素风肉鸽'])
        row = h.state.db.execute('SELECT status,field FROM interest_candidates').fetchone()
        self.assertEqual((row['status'], row['field']), ('promoted', 'gaming_preferences'))
        # a promoted interest is not cleared by the candidate window and is not reviewed again
        h.clock.advance(20 * DAY)
        h.llm.queue.append(RuntimeError('must not be called'))
        await h.service.daily.run()
        self.assertEqual(len(h.llm.calls) - before, 2)
        self.assertEqual(h.state.persona()['dynamic']['gaming_preferences'], ['像素风肉鸽'])

    async def test_continue_observe_keeps_points_and_only_reoffers_on_changed_evidence(self):
        h = Harness(self)
        h.activate()
        h.clock.advance(HOUR)
        await self.contacts(h)
        h.llm.queue.extend(self.entity_then_verdict(h, 'continue_observe'))
        await h.service.daily.run()
        self.assertEqual(h.state.db.execute('SELECT status FROM interest_candidates').fetchone()[0], 'observing')
        self.assertEqual(h.state.persona()['dynamic']['gaming_preferences'], [])
        calls = len(h.llm.calls)
        h.llm.queue.clear()
        h.clock.advance(DAY + HOUR)
        await h.service.daily.run()                                            # same evidence: no model spend
        self.assertEqual(len(h.llm.calls), calls)
        third = h.bili_read(820, '像素风肉鸽游戏介绍第三次', suffix=':third')
        third['occurred_at'] = h.clock()
        await h.decide(third, self.GOOD)
        h.clock.advance(DAY + HOUR)
        h.llm.queue.append(lambda prompt: {'verdicts': [{'candidate_key': json.loads(prompt[1]['content'])['candidates'][0]['candidate_key'],
                                                         'verdict': 'continue_observe'}]})
        h.llm.queue.insert(0, h.events_batch_answer())
        await h.service.daily.run()
        self.assertGreater(len(h.llm.calls), calls)                            # new contact => changed fingerprint => reviewed again
