"""P2/P3：真实 UnifiedService + 插件 _inject 路径（桩掉检索与模型，无网络）。"""
import copy
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from maibot_unified_core import context_layers
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from test_qq_host_adapter import load_plugin

BOT = "bot-account"


def message(mid, actor, text="聊聊游戏", session="a", stamp="1000"):
    return {"message_id": mid, "platform": "qq", "account_id": BOT, "session_id": session, "scope": "group",
            "timestamp": stamp, "message_info": {"user_info": {"user_id": actor, "user_nickname": actor},
                                                  "group_info": {"group_id": "group-" + session}, "additional_config": {}},
            "raw_message": [{"type": "text", "data": text}]}


def text_item(item_type, item_id, text):
    return {"item_type": item_type, "meta": {"item_id": item_id}, "parts": [{"type": "text", "text": text}]}


LONG = "这是一个很长很长的工具描述，" * 8
CATALOG = ("<system-reminder>\n以下工具当前未直接暴露给你，但可以通过 tool_search 工具发现并在后续轮次中使用：\n"
           f"1. alpha_tool: {LONG}\n2. beta_tool: 短描述\n\n如需其中某个工具，请先调用 tool_search。\n</system-reminder>")


def host_items():
    return [
        text_item("SystemMessageItem", "sys", "system prompt"),
        text_item("UserMessageItem", "h1", "历史消息"),
        {"item_type": "UserMessageItem", "meta": {"item_id": "img"},
         "parts": [{"type": "image", "image_format": "png", "image_base64": "AA=="}]},
        {"item_type": "FunctionCallItem", "meta": {"item_id": "fc"}, "tool_call": {"call_id": "c1", "func_name": "x"}},
        {"item_type": "FunctionCallOutputItem", "meta": {"item_id": "fo"}, "call_id": "c1",
         "parts": [{"type": "text", "text": "ok"}]},
        text_item("UserMessageItem", "cat", CATALOG),
        text_item("UserMessageItem", "tail", "当前时间"),
    ]


class Fixture(unittest.IsolatedAsyncioTestCase):
    layers_cfg = {"enabled": True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base_policy = {"qq_bot_account": BOT, "allowed_qq_sessions": ["a", "b"],
                            "qq_group_sessions": {"group-a": "a", "group-b": "b"}, "shared_groups": [["a", "b"]]}
        self.write_policy()

        class Person:
            async def get_id(inner, platform, actor):
                return hashlib.md5((platform + "_" + actor).encode()).hexdigest()
        self.s = UnifiedService(types.SimpleNamespace(person=Person()), root=self.root)
        self.s.policy = copy.deepcopy(self.policy)
        self.s.state = State(self.root / "state.sqlite", self.s.policy)
        self.s.state.init_persona({"identity": "fixed"}, {"interests": ["游戏"]})
        self.now = [0.0]
        self.s.layer_snapshots = context_layers.SnapshotStore(clock=lambda: self.now[0])
        self.s.build_context = AsyncMock(side_effect=self.fake_context)
        self.s.native_learning_context = AsyncMock(return_value={"expressions": [{"situation": "闲聊", "style": "轻松", "source_id": "e1"}], "jargon": []})
        self.s.native_profile_context = AsyncMock(side_effect=self.fake_profiles)
        self.plugin = load_plugin().UnifiedCorePlugin()
        self.plugin.service = self.s
        self.mid = 0

    def tearDown(self):
        self.s.state.close()
        self.tmp.cleanup()

    def write_policy(self, layers="default", **override):
        self.policy = {**copy.deepcopy(self.base_policy), **override}
        cfg = self.layers_cfg if layers == "default" else layers
        if cfg is not None:
            self.policy["token_optimizations"] = {"context_layers": cfg}
        (self.root / "policy.json").write_text(json.dumps(self.policy))

    async def fake_context(self, kind, platform, session, query):
        return {"persona": self.s.state.persona(), "facts": [{"text": "事实:" + query, "source_id": "s1"}],
                "activities": [], "source_ids": ["s1"], "memory_available": True}

    async def fake_profiles(self, session):
        speaker = self.s.last_message[session][2]
        return [{"source_id": "p-" + speaker, "text": "资料:" + speaker}]

    async def say(self, actor, text="聊聊游戏", session="a"):
        self.mid += 1
        return await self.s.ingest_qq(message(f"m{self.mid}", actor, text, session, str(1000 + self.mid)))


class TestSnapshotCadence(Fixture):
    async def test_default_off_returns_none_and_plugin_uses_legacy_single_block(self):
        self.write_policy(layers=None)
        await self.say("u1")
        self.assertIsNone(await self.s.qq_context_layers("a"))
        result = await self.plugin._inject(session_id="a", items=host_items())
        items = result["modified_kwargs"]["items"]
        blocks = [i for i in items if i["meta"]["item_id"].startswith("unified-context-")]
        self.assertEqual(len(blocks), 1)
        self.assertIs(items[-1], blocks[0])
        self.assertEqual([i for i in items if i not in blocks], host_items())

    async def test_twenty_external_messages_refresh_and_bot_events_do_not_count(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 1)
        for index in range(19):  # 快照之后又来 19 条外部消息，同时穿插机器人自身分段
            await self.say("u%d" % (index % 3))
            await self.s.ingest_qq(message(f"bot{index}", BOT, "我是机器人分段", "a", str(5000 + index)))
            await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_snapshots.count("a"), 20)
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 1)
        await self.say("u9")  # 快照之后的第 20 条外部消息
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 2)

    async def test_commands_and_empty_events_do_not_count(self):
        from maibot_unified_core.command_schema import PREFIX
        await self.say("u1")
        before = self.s.layer_snapshots.count("a")
        await self.say("u1", PREFIX + "状态")
        self.assertEqual(self.s.layer_snapshots.count("a"), before)

    async def test_time_cap_refreshes(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")
        self.now[0] = 1799
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 1)
        self.now[0] = 1801
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 2)

    async def test_sessions_are_isolated(self):
        await self.say("u1", session="a")
        await self.say("u1", session="b")
        for _ in range(25):
            await self.say("u2", session="a")
        await self.s.qq_context_layers("b")
        await self.s.qq_context_layers("b")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 1)
        self.assertEqual(self.s.layer_snapshots.count("b"), 1)

    async def test_ordinary_new_message_does_not_invalidate_snapshot(self):
        await self.say("u1")
        first = await self.s.qq_context_layers("a")
        await self.say("u2", "换个话题")
        second = await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 1)
        self.assertEqual(first["snapshot"], second["snapshot"])
        self.assertEqual(first["stable"], second["stable"])


class TestFreshness(Fixture):
    async def test_speaker_switch_never_reuses_previous_speaker_profile(self):
        await self.say("alice")
        first = await self.s.qq_context_layers("a")
        await self.say("bob")
        second = await self.s.qq_context_layers("a")
        self.assertIn("资料:alice", first["dynamic"])
        self.assertIn("资料:bob", second["dynamic"])
        self.assertNotIn("alice", second["dynamic"] + second["snapshot"] + second["stable"])

    async def test_persona_version_change_refreshes_immediately(self):
        await self.say("u1")
        first = await self.s.qq_context_layers("a")
        self.s.state.rollback_persona(1)
        second = await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 2)
        self.assertNotEqual(first["snapshot"], second["snapshot"])

    async def test_memory_correction_revision_refreshes(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")
        self.s.state._bump_revision()
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 2)

    async def test_session_authorization_revoked_injects_nothing_and_removes_old_blocks(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")
        self.write_policy(allowed_qq_sessions=["b"], shared_groups=[])
        self.assertEqual(await self.s.qq_context_layers("a"), {})
        stale = [context_layers.block_item(context_layers.SNAPSHOT_MARKER, "old", "a", timestamp="t", kind="snapshot")]
        result = await self.plugin._inject(session_id="a", items=[host_items()[0], *stale])
        self.assertEqual(result["modified_kwargs"]["items"], [host_items()[0]])

    async def test_shared_group_revocation_refreshes_snapshot(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")
        self.write_policy(shared_groups=[])
        await self.s.qq_context_layers("a")
        self.assertEqual(self.s.layer_stats["snapshot_refreshes"], 2)

    async def test_revoking_shared_groups_during_snapshot_await_discards_result(self):
        await self.say("u1")

        calls = []

        async def revoke_during_await(session):
            if calls:
                return {"expressions": [], "jargon": []}
            calls.append(1)
            self.write_policy(shared_groups=[])  # 本 session 仍在 allowed 内，只撤共享授权
            return {"expressions": [{"situation": "旧共享表达", "style": "x"}], "jargon": []}
        self.s.native_learning_context = AsyncMock(side_effect=revoke_during_await)
        self.assertIsNone(await self.s.qq_context_layers("a"))
        self.assertEqual(self.s.layer_snapshots.sizes()["snapshots"], 0)
        self.assertEqual(self.s.layer_stats["stale"], 1)
        result = await self.plugin._inject(session_id="a", items=host_items())
        self.assertNotIn("旧共享表达", json.dumps(result, ensure_ascii=False))
        self.assertIn("[unified-core-context-v1]", json.dumps(result, ensure_ascii=False))  # 实时旧路径

    async def test_revoking_shared_groups_during_dynamic_await_discards_cached_snapshot(self):
        await self.say("u1")
        await self.s.qq_context_layers("a")  # 快照已缓存

        async def revoke_during_await(kind, platform, session, query):
            self.write_policy(shared_groups=[])
            return await self.fake_context(kind, platform, session, query)
        self.s.build_context = AsyncMock(side_effect=revoke_during_await)
        await self.say("u2", "新问题")
        result = await self.plugin._inject(session_id="a", items=host_items())
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("unified-core-snapshot", text)
        self.assertEqual(self.s.layer_stats["stale"], 1)
        self.assertEqual(self.s.layer_snapshots.sizes()["snapshots"], 0)
        self.assertEqual(self.s.layer_dynamic_cache, {})

    async def test_failure_falls_back_to_legacy_path(self):
        await self.say("u1")
        self.s.native_learning_context = AsyncMock(side_effect=RuntimeError("down"))
        self.assertIsNone(await self.s.qq_context_layers("a"))
        self.assertEqual(self.s.layer_stats["fallbacks"], 1)
        result = await self.plugin._inject(session_id="a", items=host_items())
        self.assertNotIn("unified-core-stable", json.dumps(result, ensure_ascii=False))


class TestLayoutAndCatalog(Fixture):
    async def inject(self, items, **cfg):
        self.write_policy(layers={"enabled": True, **cfg})
        return (await self.plugin._inject(session_id="a", items=items))["modified_kwargs"]["items"]

    def ids(self, items):
        return [i["meta"]["item_id"].split("-")[0] if i["meta"]["item_id"].startswith("unified-context") else i["meta"]["item_id"]
                for i in items]

    async def test_front_layout_and_preserved_images_and_tool_chain(self):
        await self.say("u1")
        items = await self.inject(host_items(), prefix_position="front")
        self.assertEqual(items[0]["meta"]["item_id"], "sys")
        self.assertTrue(items[1]["meta"]["item_id"].startswith("unified-context-stable-"))
        self.assertTrue(items[2]["meta"]["item_id"].startswith("unified-context-snapshot-"))
        self.assertTrue(items[-1]["meta"]["item_id"].startswith("unified-context-dynamic-"))
        original = host_items()
        self.assertEqual([i for i in items if not i["meta"]["item_id"].startswith("unified-context")], original)
        positions = {i["meta"]["item_id"]: n for n, i in enumerate(items)}
        self.assertEqual(positions["fo"], positions["fc"] + 1)  # 调用/返回配对不被拆开

    async def test_reinjection_replaces_and_prefix_is_byte_identical(self):
        await self.say("u1")
        first = await self.inject(host_items(), prefix_position="front")
        again = await self.inject(first)
        self.assertEqual(sum(i["meta"]["item_id"].startswith("unified-context") for i in again), 3)
        await self.say("u2", "不同的新问题")
        later = await self.inject(host_items(), prefix_position="front")
        self.assertEqual(json.dumps(first[:3], sort_keys=True), json.dumps(later[:3], sort_keys=True))
        self.assertNotEqual(first[-1]["parts"], later[-1]["parts"])  # 即时层每轮更新

    async def test_tail_default_layout_keeps_host_order(self):
        await self.say("u1")
        items = await self.inject(host_items())
        self.assertEqual([i["meta"]["item_id"] for i in items[:7]], [i["meta"]["item_id"] for i in host_items()])

    async def test_catalog_length_and_position_are_independent(self):
        await self.say("u1")
        names = ["alpha_tool", "beta_tool"]
        for position in ("tail", "front"):
            for catalog in ("full", "short"):
                items = await self.inject(host_items(), tool_catalog=catalog, tool_catalog_position=position,
                                          prefix_position="front")
                cats = [i for i in items if context_layers.is_catalog_item(i)]
                self.assertEqual(len(cats), 1)
                text = cats[0]["parts"][0]["text"]
                for name in names:
                    self.assertIn(name, text)
                self.assertIn("tool_search", text)
                self.assertEqual("…" in text, catalog == "short")
                self.assertEqual(LONG in text, catalog == "full")
                index = [i["meta"]["item_id"] for i in items].index("cat")
                self.assertEqual(index < 4, position == "front")

    async def test_short_catalog_is_idempotent_and_absent_catalog_is_not_invented(self):
        item = text_item("UserMessageItem", "cat", CATALOG)
        once = context_layers.shorten_catalog(item, 10)
        self.assertEqual(context_layers.shorten_catalog(once, 10), once)
        self.assertEqual(item["parts"][0]["text"], CATALOG)  # 原对象不被修改
        await self.say("u1")
        items = await self.inject([host_items()[0]], tool_catalog="short", tool_catalog_position="front")
        self.assertFalse(any(context_layers.is_catalog_item(i) for i in items))


class TestSettingsFailClosed(unittest.TestCase):
    def test_invalid_fields_disable_everything(self):
        for bad in ({"enabled": True, "prefix_position": "middle"}, {"enabled": True, "max_age_seconds": float("nan")},
                    {"enabled": True, "refresh_messages": -1}, {"enabled": True, "refresh_messages": True},
                    {"enabled": "yes"}, {"enabled": True, "tool_catalog": "tiny"},
                    {"enabled": True, "refresh_messages": 0.5}, {"enabled": True, "refresh_messages": 20.5},
                    {"enabled": True, "short_description_chars": 2.5}):
            self.assertFalse(context_layers.settings({"token_optimizations": {"context_layers": bad}})["enabled"], bad)
        self.assertFalse(context_layers.settings({"token_optimizations": "oops"})["enabled"])
        self.assertTrue(context_layers.settings({"token_optimizations": {"context_layers": {"enabled": True}}})["enabled"])

    def test_store_capacity_is_shared_by_snapshots_and_counts(self):
        store = context_layers.SnapshotStore(max_sessions=3)
        for index in range(10):
            store.note_external(f"s{index}")
            store.put(f"s{index}", ("k",), "t")
        self.assertEqual(store.sizes(), {"snapshots": 3, "counts": 3})
        for index in range(10, 20):
            store.note_external(f"s{index}")
        self.assertLessEqual(store.sizes()["counts"], 3)
        self.assertLessEqual(store.sizes()["snapshots"], 3)


if __name__ == "__main__":
    unittest.main()
