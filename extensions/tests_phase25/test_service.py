"""Service integration with real durable State and synthetic Host RPC/LLM.

No network, external model, account, or platform sender is used. The fake Host
deliberately returns mixed-scope search hits to test the final disclosure gate.
"""

import asyncio
import copy
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from phase2.core import DEFAULTS, TZ, machine_id
from phase2.runner import Engine


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.answers = []

    async def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        answer = self.answers.pop(0) if self.answers else {
            "interested": True, "text": "这段公开材料值得继续聊聊。", "interest_score": 80, "reason": "synthetic"
        }
        if isinstance(answer, Exception):
            raise answer
        return {"success": True, "response": json.dumps(answer, ensure_ascii=False)}


class FakeHost:
    def __init__(self):
        self.llm = FakeLLM()
        self.calls = []
        self.hits = []
        self.writes = {}
        self.fail_writes = False
        self.logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)

    async def call_capability(self, capability, **kwargs):
        if capability != "unified.memory":
            raise AssertionError("unexpected Host capability")
        self.calls.append(copy.deepcopy(kwargs))
        op = kwargs["operation"]
        if op == "search":
            if not kwargs.get("chat_id"):
                raise AssertionError("unscoped memory search")
            return {"success": True, "hits": copy.deepcopy(self.hits)}
        if op == "status":
            return {"success": True, "semantic_retrieval": False}
        if op == "ingest_text":
            if self.fail_writes:
                return {"success": False, "error": "synthetic_host_failure"}
            key = kwargs["external_id"]
            self.writes.setdefault(key, copy.deepcopy(kwargs))
            return {"success": True, "stored_ids": ["paragraph:" + key]}
        if op in ("expression_list", "jargon_list", "profile_evidence"):
            return {"success": True, "items": [], "evidence": []}
        raise AssertionError("unexpected memory operation " + str(op))


class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = {
            "bili_uid": 8888, "bili_bot_uid": "100", "owner_uid": "200", "qq_bot_account": "qq-bot",
            "bot_accounts": {"bilibili": "100", "qq": "qq-bot"},
            "allowed_qq_sessions": ["group-a", "group-b", "private-a"],
            "shared_groups": [["group-a", "group-b"]],
            "internal_cross_platform": ["group-a"], "public_qq_sessions": ["group-a"],
            "public_topics": ["AI", "游戏", "美食"],
            "learning_enabled": True, "reflection_enabled": True,
        }
        self.write_policy()
        self.host = FakeHost()
        self.service = UnifiedService(self.host, self.root)
        self.service.policy = copy.deepcopy(self.policy)
        self.state = State(self.root / "state.sqlite", self.policy)
        self.service.state = self.state
        self.state.init_persona({"identity": "用户设定的大小姐"}, {"interests": ["AI"]})

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def write_policy(self):
        (self.root / "policy.json").write_text(json.dumps(self.policy))

    def event(self, key="source-1", **updates):
        value = {
            "event_id": key, "platform": "bilibili", "bot_account_id": "100",
            "conversation_id": "video:1", "actor_id": "300", "origin": "platform_content",
            "event_type": "content_read", "content": "公开介绍了游戏音轨的制作方式。",
            "source_id": key, "source_url": "https://www.bilibili.com/video/av1",
            "sharing_scope": "public", "learning_allowed": True, "occurred_at": time.time(),
            "action_id": "action:" + key, "material_evidence": "title_description_subtitle_excerpt",
        }
        value.update(updates)
        return value

    def ingest(self, event):
        result = self.state.ingest(event)
        return self.state.get_event(result["event_id"])

    def hit(self, event, text, key, **metadata):
        return {"hash": key, "content": text, "source": "unified-source",
                "metadata": {"unified_event_id": event["event_id"], **metadata}}

    def learning_answer(self, **updates):
        value = {"remember": True, "summary": "游戏音轨会使用多层配器。", "category": "游戏",
                 "public_summary": False, "knowledge_type": "fact", "feedback": None}
        value.update(updates)
        return value

    def decision_request(self, **updates):
        request = {"op": "decide", "event": self.event(), "task": "proactive",
                   "material": {"text": "实际标题与字幕节选", "evidence": "title_description_subtitle_excerpt"},
                   "images": [], "history": [],
                   "permissions": {"enabled": True, "paused": False, "max_model_tokens": 500,
                                   "reply_interest_min_score": 65, "daily_limit": 6}}
        request.update(updates)
        return request

    async def test_public_context_rechecks_raw_private_unknown_and_revoked_sources(self):
        raw = self.ingest(self.event("qq-raw", platform="qq", bot_account_id="qq-bot", conversation_id="group-a",
                                     origin="external_user", sharing_scope="policy", content="个人原文不可用于公开表达"))
        allowed = self.ingest(self.event("qq-topic", platform="qq", bot_account_id="qq-bot", conversation_id="group-a",
                                         origin="external_user", sharing_scope="policy", content="讨论人工智能的有限记忆"))
        private = self.ingest(self.event("private", platform="qq", bot_account_id="qq-bot", conversation_id="private-a",
                                         origin="external_user", sharing_scope="conversation", content="PRIVATE_SENTINEL"))
        revoked = self.ingest(self.event("revoked"))
        self.state.invalidate_event(revoked["event_id"], "source corrected")
        self.host.hits = [
            self.hit(raw, "RAW_SENTINEL", "raw"),
            self.hit(allowed, "讨论人工智能有限记忆的抽象主题。", "safe", public_summary=True, category="AI"),
            self.hit(allowed, "有用户会纠正AI身份，强调自己是DeepSeek而非其他称呼。", "bad-narrative", public_summary=True, category="AI"),
            self.hit(private, "PRIVATE_SENTINEL", "private", public_summary=True, category="AI"),
            self.hit(revoked, "REVOKED_SENTINEL", "revoked"),
            {"hash": "unknown", "content": "UNKNOWN_SENTINEL", "metadata": {}},
        ]
        result = await self.service.build_context("dynamic", "bilibili", "self:100", "最近", "public")
        self.assertEqual([x["source_id"] for x in result["facts"]], ["safe"])
        text = json.dumps(result, ensure_ascii=False)
        for sentinel in ("RAW_SENTINEL", "PRIVATE_SENTINEL", "REVOKED_SENTINEL", "UNKNOWN_SENTINEL", "bad-narrative", "有用户"):
            self.assertNotIn(sentinel, text)
        self.policy["public_qq_sessions"] = []
        self.write_policy()
        result = await self.service.build_context("dynamic", "bilibili", "self:100", "最近", "public")
        self.assertEqual(result["facts"], [])

    async def test_private_memory_only_appears_in_its_original_conversation(self):
        private = self.ingest(self.event("dm-1", conversation_id="dm:200", actor_id="200", origin="external_user",
                                         event_type="message_received", sharing_scope="conversation", content="PRIVATE_SENTINEL"))
        self.host.hits = [self.hit(private, "PRIVATE_SENTINEL", "private")]
        same = await self.service.build_context("dm", "bilibili", "dm:200", "私聊", "internal")
        public = await self.service.build_context("dynamic", "bilibili", "self:100", "私聊", "public")
        other = await self.service.build_context("dm", "bilibili", "dm:201", "私聊", "internal")
        self.assertEqual(len(same["facts"]), 1)
        self.assertEqual(public["facts"], [])
        self.assertEqual(other["facts"], [])

    async def test_public_bili_read_knowledge_is_available_to_qq_internal_context(self):
        source = self.ingest(self.event("public-video-knowledge"))
        self.host.hits = [self.hit(source, "实际读取的公开视频知识摘要。", "public-video-fact")]
        result = await self.service.build_context("qq_reply", "qq", "group-a", "刚才读了什么", "internal")
        self.assertEqual([x["source_id"] for x in result["facts"]], ["public-video-fact"])

    async def test_recent_read_query_is_immediate_scoped_and_preserves_evidence_type(self):
        self.ingest(self.event("read-now", content="PUBLIC_READ_SENTINEL", material_evidence="title_description"))
        self.ingest(self.event("private-now", conversation_id="dm:200", actor_id="200", origin="external_user",
                               event_type="message_received", sharing_scope="conversation", content="PRIVATE_READ_SENTINEL"))
        # No Host memory writes or embedding/learning are needed for immediate recall.
        result = await self.service.tool("activity", {"session_id": "group-a", "platform": "bilibili", "query": "刚才读了什么"})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("PUBLIC_READ_SENTINEL", serialized)
        self.assertIn("title_description", serialized)
        self.assertIn("https://www.bilibili.com/video/av1", serialized)
        self.assertNotIn("PRIVATE_READ_SENTINEL", serialized)
        self.assertEqual(self.host.writes, {})

    async def test_today_read_query_uses_event_time_not_import_time(self):
        now = time.time()
        self.ingest(self.event("today", content="TODAY_READ_SENTINEL", occurred_at=now, received_at=now))
        self.ingest(self.event("old-import", content="OLD_READ_SENTINEL", occurred_at=now - 2 * 86400, received_at=now))
        result = await self.service.tool("activity", {"session_id": "group-a", "platform": "bilibili", "query": "今天读了什么"})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertIn("TODAY_READ_SENTINEL", serialized)
        self.assertNotIn("OLD_READ_SENTINEL", serialized)

    async def test_bili_event_replay_uses_canonical_id_without_relearning(self):
        event = self.event()
        first = await self.service.dispatch({"op": "event", "event": event})
        repeated = {**event, "received_at": time.time() + 60}
        second = await self.service.dispatch({"op": "event", "event": repeated})
        self.assertTrue(first["event"]["is_new"])
        self.assertFalse(second["event"]["is_new"])
        self.assertEqual(first["event"]["event_id"], second["event"]["event_id"])
        self.host.llm.answers = [self.learning_answer()]
        await self.service.learning_tick(force=True)
        await self.service.dispatch({"op": "event", "event": repeated})
        await self.service.learning_tick(force=True)
        self.assertEqual(len(self.host.writes), 1)
        self.assertEqual(len(self.host.llm.calls), 1)
        written = next(iter(self.host.writes.values()))
        self.assertEqual(written["external_id"], "unified:" + first["event"]["event_id"])
        self.assertEqual(written["metadata"]["unified_event_id"], first["event"]["event_id"])
        self.assertIsNotNone(self.state.get_event(written["metadata"]["unified_event_id"]))

    async def test_learning_preserves_actual_material_boundary(self):
        event = self.ingest(self.event())
        self.host.llm.answers = [self.learning_answer()]
        await self.service.learning_tick(force=True)
        written = next(iter(self.host.writes.values()))
        self.assertEqual(written["metadata"]["actual_material"], event["material_evidence"])
        self.assertEqual(written["metadata"]["source_id"], event["source_id"])

    async def test_background_budget_leaves_pending_sources_unprocessed(self):
        for index in range(25):
            self.ingest(self.event("source-" + str(index)))
        self.host.llm.answers = [self.learning_answer() for _ in range(25)]
        for _ in range(6):
            await self.service.learning_tick(force=True)
        self.assertEqual(self.state.quota_used(), 20)
        self.assertEqual(len(self.host.llm.calls), 20)
        self.assertEqual(len(self.host.writes), 20)
        self.assertEqual(len(self.state.learning_candidates()), 5)

    async def test_failed_learning_does_not_starve_later_sources(self):
        self.ingest(self.event("first"))
        self.host.llm.answers = [RuntimeError("synthetic first failure")]
        await self.service.learning_tick(force=True)
        self.ingest(self.event("later"))
        self.host.llm.answers = [self.learning_answer(), self.learning_answer()]
        await self.service.learning_tick(force=True)
        self.assertTrue(any(x["metadata"]["source_id"] == "later" for x in self.host.writes.values()))
        self.assertLessEqual(len(self.host.llm.calls), 3)

    async def test_decision_obeys_500_tokens_and_never_claims_delivery_until_receipt(self):
        request = self.decision_request()
        result = await self.service.dispatch(request)
        self.assertEqual(result["persona_version"], self.state.persona()["version"])
        self.assertLessEqual(self.host.llm.calls[-1]["max_tokens"], 500)
        self.assertEqual(self.state.recent_activity(), [])
        action = {"action_id": request["event"]["action_id"], "platform": "bilibili", "bot_account_id": "100",
                  "conversation_id": "video:1", "kind": "proactive", "status": "success", "canonical_status": "succeeded",
                  "remote_id": "900", "url": "https://www.bilibili.com/video/av1#reply900",
                  "text": result["decision"]["text"], "sharing_scope": "public", "receipt_verified": True,
                  "persona_version": result["persona_version"], "input_event": request["event"]}
        first = await self.service.dispatch({"op": "activity", "action": action})
        await self.service.dispatch({"op": "activity", "action": action})
        self.assertEqual(first["activity"]["status"], "succeeded")
        self.assertEqual(len(self.state.recent_activity()), 1)
        self.assertFalse(self.state.recent_activity()[0]["learning_allowed"])

    async def test_real_bili_runner_roundtrip_and_outbox_replay_share_one_event(self):
        config = copy.deepcopy(DEFAULTS)
        config.update(bot_uid="100", writer_machine=machine_id(), paused=False)
        config["enabled"]["dynamic"] = True
        path = self.root / "bili-config.json"
        path.write_text(json.dumps(config))
        loop = asyncio.get_running_loop()

        class SyntheticPlatform:
            def __init__(self):
                self.sent = []
            def identity(self):
                return {"mid": "100", "name": "synthetic"}
            def send(self, kind, target, text, **kwargs):
                self.sent.append(text)
                return {"id": "777", "url": "https://t.bilibili.com/777"}

        api = SyntheticPlatform()

        async def dispatch(payload):
            return {"ok": True, **await self.service.dispatch(payload)}

        def run():
            now = datetime(2026, 10, 24, 12, tzinfo=TZ).timestamp()
            engine = Engine(path, self.root / "bili-state", self.root, api=api, now=lambda: now)
            engine.unified_request = lambda payload: asyncio.run_coroutine_threadsafe(dispatch(payload), loop).result(5)
            engine.s.put("bound_uid", "100")
            engine.s.put("baseline_at", now - 1)
            try:
                result = engine.dynamic()
                engine.sync()
                return result, engine.s.summary()
            finally:
                engine.s.db.close()

        result, summary = await asyncio.to_thread(run)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(summary["unified"]["pending_events"], 0)
        self.assertEqual(summary["unified"]["pending_receipts"], 0)
        self.assertEqual(self.state.db.execute("select count(*) from events").fetchone()[0], 1)
        activity = self.state.recent_activity()[0]
        self.assertEqual(activity["remote_id"], "777")
        self.assertEqual(activity["status"], "succeeded")
        self.assertIsNotNone(self.state.get_event(activity["trigger_event_id"]))
        event = self.state.db.execute("select actor_id,learning_allowed from events").fetchone()
        self.assertEqual(event["actor_id"], "100")
        self.assertEqual(event["learning_allowed"], 0)

    async def test_low_interest_reply_decision_has_no_content(self):
        self.host.llm.answers = [{"interested": True, "text": "不应外发", "interest_score": 64, "reason": "low"}]
        result = await self.service.dispatch(self.decision_request(task="reply"))
        self.assertFalse(result["decision"]["interested"])
        self.assertEqual(result["decision"]["text"], "")

    async def test_system_actor_is_nonblank_and_cannot_be_learned_as_user(self):
        event = self.event("due", actor_id="100", origin="system_task", event_type="opportunity", learning_allowed=True)
        accepted = await self.service.dispatch({"op": "event", "event": event})
        stored = self.state.get_event(accepted["event"]["event_id"])
        self.assertEqual(stored["actor_id"], "100")
        self.assertEqual(stored["origin"], "bot_self")
        self.assertFalse(stored["learning_allowed"])
        with self.assertRaises((ValueError, PermissionError)):
            await self.service.dispatch({"op": "event", "event": self.event("blank", actor_id="")})

    async def test_bili_dm_cannot_upgrade_scope_to_public(self):
        event = self.event("private", conversation_id="dm:200", actor_id="200", origin="external_user",
                           event_type="message_received", sharing_scope="public")
        try:
            accepted = await self.service.dispatch({"op": "event", "event": event})
        except (ValueError, PermissionError):
            return
        stored = self.state.get_event(accepted["event"]["event_id"])
        self.assertFalse(self.state.allowed(stored, "bilibili", "self:100", "public"))

    async def test_private_receipt_cannot_upgrade_scope_to_public(self):
        action = {"action_id": "dm-private", "platform": "bilibili", "bot_account_id": "100",
                  "conversation_id": "dm:200", "kind": "dm", "status": "success", "remote_id": "901",
                  "text": "PRIVATE_SENTINEL", "sharing_scope": "public"}
        try:
            accepted = await self.service.dispatch({"op": "activity", "action": action})
        except (ValueError, PermissionError):
            return
        self.assertFalse(self.state.allowed(accepted["activity"], "qq", "group-a", "internal"))
        self.assertFalse(self.state.allowed(accepted["activity"], "bilibili", "self:100", "public"))

    async def test_reflection_applies_supported_changes_from_two_sources(self):
        one = self.ingest(self.event("one"))
        two = self.ingest(self.event("two"))
        self.host.llm.answers = [{"changes": [{"field": "interests", "value": ["AI", "游戏"],
                                                "evidence_ids": [one["event_id"], two["event_id"]], "reason": "两份独立游戏材料"}]}]
        result = await self.service.reflect(force=True)
        self.assertTrue(result.get("result", {}).get("accepted"), result)
        self.assertEqual(self.state.persona()["dynamic"]["interests"], ["AI", "游戏"])
        self.assertEqual(self.state.persona()["stable"], {"identity": "用户设定的大小姐"})

    async def test_reflection_can_use_already_learned_evidence(self):
        one = self.ingest(self.event("one"))
        two = self.ingest(self.event("two"))
        self.state.mark_learned(one["event_id"], "stored", ["fact-one"])
        self.state.mark_learned(two["event_id"], "stored", ["fact-two"])
        self.host.llm.answers = [{"changes": [{"field": "interests", "value": ["AI", "游戏"],
                                                "evidence_ids": [one["event_id"], two["event_id"]], "reason": "共同经历"}]}]
        result = await self.service.reflect(force=True)
        self.assertTrue(result.get("result", {}).get("accepted"), result)

    async def test_private_material_cannot_shape_public_shared_persona(self):
        one = self.ingest(self.event("dm-one", conversation_id="dm:200", actor_id="200", origin="external_user",
                                     sharing_scope="conversation", content="PRIVATE_SENTINEL_ONE"))
        two = self.ingest(self.event("dm-two", conversation_id="dm:200", actor_id="200", origin="external_user",
                                     sharing_scope="conversation", content="PRIVATE_SENTINEL_TWO"))
        self.host.llm.answers = [{"changes": [{"field": "interests", "value": ["PRIVATE_SENTINEL_ONE"],
                                                "evidence_ids": [one["event_id"], two["event_id"]], "reason": "应被阻止的私聊推断"}]}]
        before = self.state.persona()["version"]
        await self.service.reflect(force=True)
        self.assertEqual(self.state.persona()["version"], before)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.state.persona()))
        if self.host.llm.calls:
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.host.llm.calls))

    async def test_non_admin_socket_peer_cannot_invoke_administration(self):
        class Reader:
            async def readline(self):
                return b'{"op":"rollback","version":1}\n'

        class Writer:
            def __init__(self):
                self.data = b""
            def get_extra_info(self, name):
                return SimpleNamespace(getsockopt=lambda *a: struct.pack("3i", 123, 8888, 8888))
            def write(self, data):
                self.data += data
            async def drain(self):
                pass
            def close(self):
                pass
            async def wait_closed(self):
                pass

        writer = Writer()
        with patch("maibot_unified_core.service.os.getuid", return_value=7777), patch(
            "maibot_unified_core.service.socket.SO_PEERCRED", 17, create=True
        ):
            await self.service.handle(Reader(), writer)
        response = json.loads(writer.data)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "admin_only")
        self.assertEqual(self.state.persona()["version"], 1)


if __name__ == "__main__":
    unittest.main()
