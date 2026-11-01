"""P4：真实 PersonFactWritebackService._handle_message 路径上的去重测试（桩掉模型与存储）。"""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import host_loader

BOT = "bot"
RULES_HEAD = "你要从用户原始发言中提取“关于目标人物的稳定事实”"
LEGACY_HEAD = "你要从用户原始发言中提取“关于Alice的稳定事实”"
FACT_A = "Alice 喜欢打游戏"
FACT_B = "Alice 对青霉素过敏"


def message(mid, uid, text, ts, sid="s1"):
    return SimpleNamespace(
        message_id=mid, platform="qq", user_id=uid, processed_plain_text=text, timestamp=ts, sid=sid,
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id=uid, user_cardname="", user_nickname=uid)))


def reply(text, ts, reply_to="u1", sid="s1", group="g1"):
    return SimpleNamespace(
        processed_plain_text=text, session_id=sid, reply_to=reply_to, timestamp=ts, platform="qq",
        session=SimpleNamespace(platform="qq", user_id="" if group else "u", group_id=group, session_id=sid))


class FakeExtractor:
    def __init__(self, responses):
        self.responses, self.prompts, self.delay = list(responses), [], 0

    async def generate_response(self, prompt):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(response=item)


def facts_json(*facts):
    return json.dumps([{"fact": f, "evidence_message_id": "u1", "evidence_quote": "x"} for f in facts], ensure_ascii=False)


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = host_loader.load("services.memory_flow_service", ("services.person_fact_dedup",))

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.module_opt = self.module._get_token_opt_settings
        self.db = [message("u1", "u", "我喜欢打游戏，也对青霉素过敏", 100.0)]
        self.policy = {"filter": {"mode": "blacklist", "chats": []}}
        self.stores = []
        self.store_results = [True]

        class Cfg:
            def __init__(inner):
                pass

            def model_dump(inner):
                return json.loads(json.dumps(self.policy))

        def find_messages(message_id=None, session_id=None, before_time=None, limit=None, limit_mode=None,
                          filter_bot=False, **_):
            if message_id:
                return [m for m in self.db if m.message_id == message_id][:1]
            items = [m for m in self.db if m.sid == session_id and (before_time is None or m.timestamp < before_time)]
            if filter_bot:
                items = [m for m in items if m.user_id != BOT]
            items.sort(key=lambda m: m.timestamp)
            return items[-limit:] if limit else items

        async def store(person_name, fact, session_id, **kwargs):
            self.stores.append((fact, session_id))
            result = self.store_results.pop(0) if len(self.store_results) > 1 else self.store_results[0]
            if isinstance(result, Exception):
                raise result
            return result

        patches = [
            mock.patch.object(self.module, "find_messages", find_messages),
            mock.patch.object(self.module, "is_bot_self", lambda platform, uid: uid == BOT),
            mock.patch.object(self.module, "get_person_id", lambda platform, uid: "p1"),
            mock.patch.object(self.module, "store_person_memory_from_answer", store),
            mock.patch.object(self.module, "global_config", SimpleNamespace(a_memorix=Cfg())),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.person = SimpleNamespace(person_id="p1", person_name="Alice", nickname="Alice", is_known=True)
        self.svc = self.module.PersonFactWritebackService()
        self.svc._resolve_target_person = lambda m: self.person

    def extractor(self, *responses):
        self.svc._extractor = FakeExtractor(responses)
        return self.svc._extractor

    def segment(self, n, ts, **kw):
        """机器人分段：先写入历史（作为发送后的消息），再触发写回。"""
        text = f"原来如此，这一点我记住了，第{n}段回复内容"
        self.db.append(message(f"b{n}", BOT, text, ts, sid=kw.get("sid", "s1")))
        return reply(text, ts, **kw)

    async def handle_segments(self, count=3, start=101.0, **kw):
        for n in range(1, count + 1):
            await self.svc._handle_message(self.segment(n + len(self.db), start + n, **kw))


class TestSegments(Base):
    async def test_multi_segment_extracts_and_writes_once(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(3)
        self.assertEqual(len(ex.prompts), 1)
        self.assertEqual(self.stores, [(FACT_A, "s1")])

    async def test_default_off_keeps_legacy_prompt_and_per_segment_calls(self):
        with mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP": "0"}):
            ex = self.extractor(facts_json(FACT_A))
            await self.handle_segments(3)
        self.assertEqual(len(ex.prompts), 3)
        self.assertTrue(all(p.startswith(LEGACY_HEAD) for p in ex.prompts))
        self.assertEqual(len(self.stores), 3)

    async def test_rules_first_prompt_prefix_is_person_independent(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.assertTrue(ex.prompts[0].startswith(RULES_HEAD))
        rules = self.module.PersonFactWritebackService._extraction_rules_text()
        other = self.module.PersonFactWritebackService._build_rules_first_prompt("Bob", "r", "e")
        self.assertEqual(ex.prompts[0][: len(rules)], other[: len(rules)])
        self.assertIn("事实值必须能被“目标用户原始发言”直接支持", rules)

    async def test_edited_evidence_recomputes(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.db[0].processed_plain_text = "我其实不喜欢打游戏了"  # 同 ID 编辑
        await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)

    async def test_retracted_evidence_changes_key_and_no_evidence_skips(self):
        self.db.append(message("u2", "u", "我住在上海", 100.5))
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.db[:] = [m for m in self.db if m.message_id != "u2"]  # 撤回
        await self.handle_segments(1, start=110.0, reply_to="")
        self.assertEqual(len(ex.prompts), 2)

    async def test_new_user_evidence_recomputes_without_reply_to(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(2, reply_to="")
        self.assertEqual(len(ex.prompts), 1)
        self.db.append(message("u9", "u", "更正：我养了一只猫", 120.0))
        await self.handle_segments(1, start=121.0, reply_to="")
        self.assertEqual(len(ex.prompts), 2)

    async def test_earlier_bot_context_change_recomputes(self):
        # 目标发言之前的机器人提问影响短答的解释，必须进入键。
        self.db.insert(0, message("q0", BOT, "你对什么过敏？", 90.0))
        self.db[1].processed_plain_text = "青霉素"
        ex = self.extractor(facts_json(FACT_B))
        await self.handle_segments(2)
        self.assertEqual(len(ex.prompts), 1)
        self.db[0].processed_plain_text = "你喜欢吃什么？"
        await self.handle_segments(1, start=130.0)
        self.assertEqual(len(ex.prompts), 2)

    async def test_session_and_private_group_scope_do_not_share(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.db.append(message("u1", "u", self.db[0].processed_plain_text, 100.0, sid="s2"))
        await self.svc._handle_message(self.segment(50, 150.0, sid="s2", group="g2"))
        await self.svc._handle_message(self.segment(51, 151.0, sid="s2", group=""))  # 同会话号的私聊形态
        self.assertEqual(len(ex.prompts), 3)

    async def test_policy_change_in_same_session_recomputes(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.policy["filter"] = {"mode": "blacklist", "chats": ["group:g1"]}  # 同会话记忆策略变化
        await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)

    async def test_unreadable_policy_disables_reuse(self):
        ex = self.extractor(facts_json(FACT_A))
        self.module.global_config.a_memorix.model_dump = mock.Mock(side_effect=RuntimeError("boom"))
        await self.handle_segments(2)
        self.assertEqual(len(ex.prompts), 2)

    async def test_extractor_version_participates_in_key(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        with mock.patch.object(self.module, "_FACT_EXTRACTION_VERSION", "pf-extract-next"):
            await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)


class TestFailures(Base):
    async def test_model_exception_is_not_cached_then_retried(self):
        ex = self.extractor(RuntimeError("timeout"), facts_json(FACT_A))
        await self.handle_segments(1)
        self.assertEqual(self.stores, [])
        await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual(len(self.stores), 1)
        await self.handle_segments(1, start=120.0)
        self.assertEqual(len(ex.prompts), 2)

    async def test_true_empty_array_is_cached_but_invalid_array_is_not(self):
        ex = self.extractor("[]")
        await self.handle_segments(2)
        self.assertEqual(len(ex.prompts), 1)
        self.db[0].processed_plain_text = "我现在住在北京"
        ex = self.extractor("[{}]")
        await self.handle_segments(2, start=110.0)
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual(self.stores, [])

    async def test_garbage_and_blank_output_not_cached(self):
        for raw in ("", "not json at all", '{"fact": "x"}'):
            ex = self.extractor(raw)
            await self.handle_segments(2, start=200.0 + len(raw))
            self.assertEqual(len(ex.prompts), 2, raw)

    async def test_repairable_truncated_json_keeps_old_write_behaviour_but_is_never_cached(self):
        for index, raw in enumerate(('[{"fact":"他喜欢打游戏"', '[{"fact":"他喜欢打游戏', '```json\n[{"fact":"他喜欢打游戏"\n```')):
            self.stores.clear()
            self.db[0].processed_plain_text = f"我喜欢打游戏 {index}"
            ex = self.extractor(raw)
            await self.handle_segments(2, start=300.0 + index * 10)
            self.assertEqual(len(ex.prompts), 2, raw)  # 每个分段都重新抽取：没有缓存为完成
            self.assertEqual(self.stores[0][0], "他喜欢打游戏", raw)  # 修复结果仍按旧行为写入

    async def test_non_string_or_malformed_items_are_not_complete(self):
        for index, raw in enumerate(("[12345]", '[{"fact": 12345}]', '[{"fact": null}]', '["abc"]', '[true]')):
            self.db[0].processed_plain_text = f"另一条证据 {index}"
            ex = self.extractor(raw)
            await self.handle_segments(2, start=500.0 + index * 10)
            self.assertEqual(len(ex.prompts), 2, raw)

    async def test_complete_valid_json_and_complete_fence_are_cached(self):
        for index, raw in enumerate((facts_json(FACT_A), "```json\n" + facts_json(FACT_A) + "\n```", '["他喜欢打游戏"]')):
            self.db[0].processed_plain_text = f"完整证据 {index}"
            ex = self.extractor(raw)
            await self.handle_segments(3, start=700.0 + index * 10)
            self.assertEqual(len(ex.prompts), 1, raw)

    async def test_partially_invalid_array_writes_valid_items_but_not_cached(self):
        raw = json.dumps([{"fact": FACT_A}, {}])
        ex = self.extractor(raw)
        await self.handle_segments(2)
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual(self.stores[0][0], FACT_A)

    async def test_partial_write_failure_keeps_missing_candidate(self):
        # 第一次抽取 A/B：A 写成功、B 写失败；第二次模型只返回 A，B 仍必须被补写。
        ex = self.extractor(facts_json(FACT_A, FACT_B), facts_json(FACT_A))
        self.store_results = [True, False, True]
        await self.handle_segments(1)
        self.assertEqual([f for f, _ in self.stores], [FACT_A, FACT_B])
        await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual([f for f, _ in self.stores], [FACT_A, FACT_B, FACT_B])
        await self.handle_segments(1, start=120.0)  # 现已完成：跳过
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual(len(self.stores), 3)

    async def test_write_exception_keeps_unprocessed_candidates(self):
        ex = self.extractor(facts_json(FACT_A, FACT_B), facts_json(FACT_A))
        self.store_results = [RuntimeError("db"), True]
        with self.assertRaises(RuntimeError):
            await self.svc._handle_message(self.segment(1, 101.0))
        await self.svc._handle_message(self.segment(2, 102.0))
        self.assertEqual(sorted(f for f, _ in self.stores[1:]), sorted([FACT_A, FACT_B]))
        self.assertEqual(len(ex.prompts), 2)

    async def test_unconfirmed_write_result_none_not_cached(self):
        ex = self.extractor(facts_json(FACT_A))
        self.store_results = [None, True]
        await self.handle_segments(2)
        self.assertEqual(len(ex.prompts), 2)
        self.assertEqual(len(self.stores), 2)


class TestConcurrencyAndBounds(Base):
    async def test_concurrent_same_evidence_writes_once(self):
        ex = self.extractor(facts_json(FACT_A))
        ex.delay = 0.05
        await asyncio.gather(*(self.svc._handle_message(self.segment(n, 101.0 + n)) for n in range(1, 5)))
        self.assertEqual(len(ex.prompts), 1)
        self.assertEqual(len(self.stores), 1)

    async def test_cache_capacity_and_ttl_are_bounded(self):
        from importlib import import_module  # noqa: F401  (确认使用真实模块)
        now = [0.0]
        cache = self.module.PersonFactDedupCache(ttl_seconds=10, empty_ttl_seconds=5, partial_ttl_seconds=3,
                                                 max_entries=3, clock=lambda: now[0])
        for index in range(10):
            cache.record_success(f"k{index}", fact_count=1)
        self.assertEqual(len(cache), 3)
        self.assertIsNone(cache.lookup("k0"))
        self.assertIsNotNone(cache.lookup("k9"))
        now[0] = 11.0
        self.assertIsNone(cache.lookup("k9"))
        cache.record_success("e", fact_count=0)
        now[0] = 17.0
        self.assertIsNone(cache.lookup("e"))  # 空结果 TTL 更短
        cache.record_partial("p", written=["x"], pending=["cand"])
        self.assertEqual(cache.lookup("p").pending, ("cand",))
        now[0] = 21.0
        self.assertIsNone(cache.lookup("p"))

    async def test_restart_falls_back_to_original_behaviour(self):
        ex = self.extractor(facts_json(FACT_A))
        await self.handle_segments(1)
        self.svc = self.module.PersonFactWritebackService()  # 进程重启：缓存为空
        self.svc._resolve_target_person = lambda m: self.person
        self.svc._extractor = ex
        await self.handle_segments(1, start=110.0)
        self.assertEqual(len(ex.prompts), 2)


if __name__ == "__main__":
    unittest.main()
