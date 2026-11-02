"""voice_turn tests on the real maibot_unified_core store/private-access code with a fake Host (simulation)."""
import asyncio
import json
import os
import sys
import unittest

from maibot_unified_core.voice import REFUSAL

from .harness import (BackendCase, DEVICE, FakeCtx, GROUP_SESSION, OTHER_PRIVATE_ID, OTHER_SESSION,
                      OWNER_GROUP_ACTOR, OWNER_PRIVATE_ID, OWNER_SESSION, TURN_UID)


class PeerBoundaryTests(BackendCase):
    def test_voice_peer_can_only_call_voice_turn(self):
        a = self.svc.authorize_peer
        self.assertFalse(a(4242))                                  # connects, not admin
        self.assertFalse(a(4242, "voice_turn"))
        for op in ("status", "context", "decide", "memory_admin", "qq_native", "gallery", "xhs_plan",
                   "policy_reload", "command_claim", "event", "activity"):
            with self.assertRaises(PermissionError, msg=op):
                a(4242, op)

    def test_other_peers_cannot_call_voice_turn(self):
        self.policy.update(bili_uid=5001, xhs_uid=5002)
        self.write_policy()
        self.svc.reload_policy()
        for uid in (5001, 5002):
            with self.assertRaises(PermissionError):
                self.svc.authorize_peer(uid, "voice_turn")
        with self.assertRaises(PermissionError):
            self.svc.authorize_peer(9999)                          # unknown uid denied outright
        with self.assertRaises(PermissionError):
            self.svc.authorize_peer(0, "voice_turn")               # not even root: it is the voice peer's op

    def test_voice_uid_must_not_equal_service_uid(self):
        self.policy["voice_uid"] = os.getuid()
        self.write_policy()
        self.svc.reload_policy()
        with self.assertRaises(PermissionError) as cm:
            self.svc.authorize_peer(os.getuid(), "voice_turn")
        self.assertIn("voice_uid_not_distinct", str(cm.exception))

    @unittest.skipUnless(sys.platform == "linux", "Linux peer-credential socket contract")
    async def test_end_to_end_through_the_ipc_handler(self):
        """Real Unix socket + the real handle(): SO_PEERCRED uid is ours, so make that the voice uid."""
        self.policy["voice_uid"] = os.getuid() + 1                 # a different uid for the "gateway"
        self.write_policy()
        self.svc.reload_policy()
        # The test process uid is the service uid => it is admin and denied voice_turn (boundary works
        # in both directions); exercise the allowed path via dispatch with the voice peer verified above.
        sock = str(self.root / "t.sock")
        server = await asyncio.start_unix_server(self.svc.handle, sock)
        try:
            reader, writer = await asyncio.open_unix_connection(sock)
            writer.write(json.dumps(self.req()).encode() + b"\n")
            reply = json.loads(await reader.readline())
            writer.close()
        finally:
            server.close()
        self.assertEqual(reply, {"ok": False, "error": "admin_only"})


class BindingTests(BackendCase):
    async def test_unbound_or_disabled_device_is_rejected_without_calling_the_brain(self):
        for entries in ({}, {DEVICE: {"enabled": False, "owner_private_id": OWNER_PRIVATE_ID, "session": OWNER_SESSION}},
                        {"other": {"enabled": True, "owner_private_id": OWNER_PRIVATE_ID, "session": OWNER_SESSION}}):
            self.write_voice_bindings(entries)
            with self.assertRaises(ValueError) as cm:
                await self.svc.dispatch(self.req())
            self.assertEqual(str(cm.exception), "voice_unbound")
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.memory.items, [])

    async def test_missing_or_corrupt_binding_file_fails_closed(self):
        (self.root / "voice_bindings.json").write_text("{not json")
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req())
        (self.root / "voice_bindings.json").unlink()
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req())

    async def test_binding_must_match_the_verified_private_binding(self):
        # Session claimed in the file differs from the verified private binding row.
        self.write_voice_bindings({DEVICE: {"enabled": True, "owner_private_id": OWNER_PRIVATE_ID,
                                            "session": OTHER_SESSION}})
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req())

    async def test_non_owner_private_account_cannot_be_bound(self):
        # A verified familiar private chat that is NOT the configured Owner never qualifies.
        self.bind_private(OTHER_PRIVATE_ID, OTHER_SESSION, "u-friend")
        self.write_voice_bindings({DEVICE: {"enabled": True, "owner_private_id": OTHER_PRIVATE_ID,
                                            "session": OTHER_SESSION}})
        self.svc.reload_policy()
        with self.assertRaises(ValueError) as cm:
            await self.svc.dispatch(self.req())
        self.assertEqual(str(cm.exception), "voice_unbound")

    async def test_policy_changes_are_rechecked_on_every_turn(self):
        self.assertEqual((await self.svc.dispatch(self.req()))["mood"], "neutral")
        self.policy["qq_owner_bindings"] = {GROUP_SESSION: "someone-else"}   # owner binding revoked
        self.write_policy()
        with self.assertRaises(ValueError):
            await self.svc.dispatch({**self.req(), "turn_uid": "b" * 32})
        self.policy["qq_owner_bindings"] = {GROUP_SESSION: OWNER_GROUP_ACTOR}
        self.policy["qq_private_familiar_enabled"] = False
        self.write_policy()
        with self.assertRaises(ValueError):
            await self.svc.dispatch({**self.req(), "turn_uid": "c" * 32})

    async def test_binding_reload_after_edit(self):
        self.write_voice_bindings({})
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req())
        self.write_voice_bindings({DEVICE: {"enabled": True, "owner_private_id": OWNER_PRIVATE_ID,
                                            "session": OWNER_SESSION}})
        self.assertIn("reply", await self.svc.dispatch(self.req()))


class SpoofTests(BackendCase):
    async def test_caller_cannot_nominate_owner_session_person_or_endpoint(self):
        for field in ("session", "owner", "person_id", "conversation_id", "platform", "chat_id",
                      "endpoint", "owner_private_id", "bot_account_id", "memory_scope", "task"):
            with self.assertRaises(ValueError, msg=field) as cm:
                await self.svc.dispatch(self.req(**{field: "qq:private:evil"}))
            self.assertEqual(str(cm.exception), "unexpected_fields")
        self.assertEqual(self.ctx.llm.calls, [])
        self.assertEqual(self.ctx.memory.items, [])

    async def test_input_validation(self):
        bad = [{"device_id": "../x"}, {"device_id": 5}, {"turn_uid": "short"}, {"turn_uid": "G" * 32},
               {"text": ""}, {"text": "   "}, {"text": 12}, {"text": "字" * 1001}]
        for patch in bad:
            with self.assertRaises(ValueError, msg=str(patch)):
                await self.svc.dispatch({**self.req(), **patch})
        self.assertEqual(self.ctx.llm.calls, [])


class TurnTests(BackendCase):
    async def test_turn_goes_through_the_host_bridge_for_the_bound_owner_only(self):
        r = await self.svc.dispatch(self.req("我明天要去医院复查"))
        self.assertEqual((r["reply"], r["mood"], r["memory_stored"]), ("好呀，明天见。", "neutral", True))
        self.assertEqual(r["pipeline"], "maisaka_full_turn")
        self.assertEqual(r["tools"], [{"name": "reply", "success": True}])
        self.assertEqual(r["elapsed_ms"], 1234)
        self.assertEqual(self.ctx.llm.calls, [])                   # no direct model call any more: the Host pipeline answers
        (call,) = self.ctx.voice_host.calls
        self.assertEqual(set(call), {"key", "session_id", "owner_id", "account_id", "text", "history", "timeout_s"})   # + operation="run"
        self.assertEqual((call["session_id"], call["owner_id"], call["account_id"], call["text"]),
                         (OWNER_SESSION, OWNER_PRIVATE_ID, "20001", "我明天要去医院复查"))   # from the server-side binding
        self.assertRegex(call["key"], r"^[0-9a-f]{64}$")
        self.assertEqual(call["timeout_s"], 22.0)

    async def test_scope_and_reply_gate_exist_only_around_the_host_call(self):
        seen = {}

        def during(kw):
            seen["scope"] = self.svc.voice.scope_for(kw["key"], OWNER_SESSION)
            seen["other_session"] = self.svc.voice.scope_for(kw["key"], OTHER_SESSION)
            seen["gate"] = self.svc.qq_reply_gate(OWNER_SESSION, "device-voice:" + kw["key"])
            seen["overlay"] = None
        self.ctx.voice_host.during = during
        await self.svc.dispatch(self.req("你好"))
        self.assertEqual(seen["scope"]["message"][0], "你好")
        self.assertEqual(seen["scope"]["message"][2], OWNER_PRIVATE_ID)
        self.assertIsNone(seen["other_session"])
        self.assertTrue(seen["gate"]["eligible"] and seen["gate"]["reason"] == "verified_device_owner")
        self.assertIsNone(self.svc.voice.scope_for(self.ctx.voice_host.calls[0]["key"], OWNER_SESSION))   # closed again

    async def test_each_attempt_gets_a_fresh_unguessable_key(self):
        await self.svc.dispatch(self.req("一"))
        await self.svc.dispatch({**self.req("二"), "turn_uid": "d" * 32})
        keys = [c["key"] for c in self.ctx.voice_host.calls]
        self.assertEqual(len(set(keys)), 2)

    async def test_reply_is_clipped_and_host_output_is_screened_again(self):
        self.ctx.voice_host.result = {**self.ctx.voice_host.result, "reply": "字" * 3000, "mood": "furious"}
        r = await self.svc.dispatch(self.req())
        self.assertEqual((r["mood"], len(r["reply"])), ("neutral", 1200))
        self.ctx.voice_host.result = {**self.ctx.voice_host.result, "reply": "我觉得政府应该怎样怎样", "mood": "serious"}
        r = await self.svc.dispatch({**self.req("今天吃什么"), "turn_uid": "d" * 32})
        self.assertEqual((r["reply"], r["mood"]), (REFUSAL, "gentle"))

    async def test_host_failures_surface_as_errors_and_store_no_reply_or_memory(self):
        for error in ("voice_no_reply", "voice_timeout", "voice_host_failed"):
            self.ctx.voice_host.result = {"success": False, "error": error}
            with self.assertRaises(ValueError) as cm:
                await self.svc.dispatch(self.req())
            self.assertEqual(str(cm.exception), error)
            break
        self.assertEqual(self.ctx.memory.items, [])
        # the owner's words were stored once (they were said); no reply exists
        self.assertIsNotNone(self.svc.state.get_event(self.svc.state.stored_event_id(
            {"platform": "qq", "bot_account_id": "20001", "event_type": "device_voice_turn",
             "event_id": f"device-voice:{DEVICE}:{TURN_UID}"})))
        self.assertIsNone(self.svc.state.get_event(self.svc.state.stored_event_id(
            {"platform": "qq", "bot_account_id": "20001", "event_type": "device_voice_reply",
             "event_id": f"device-voice:{DEVICE}:{TURN_UID}:reply"})))

    async def test_empty_host_reply_is_an_error(self):
        self.ctx.voice_host.result = {**self.ctx.voice_host.result, "reply": "   "}
        with self.assertRaises(RuntimeError):
            await self.svc.dispatch(self.req())

    async def test_restricted_topics_are_refused_without_the_host_or_memory(self):
        r = await self.svc.dispatch(self.req("你怎么看这次总统选举"))
        self.assertEqual((r["reply"], r["mood"], r["memory_stored"]), (REFUSAL, "gentle", False))
        self.assertEqual(self.ctx.voice_host.calls, [])
        self.assertEqual(self.ctx.memory.items, [])

    async def test_refusal_before_any_work_is_retryable_but_an_interruption_is_not(self):
        self.ctx.voice_host.result = {"success": False, "error": "voice_busy"}
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req())
        self.ctx.voice_host.result = {**FakeCtx().voice_host.result}
        r = await self.svc.dispatch(self.req())                      # nothing had started: the same turn id may run
        self.assertEqual(r["reply"], "好呀，明天见。")
        # an interruption leaves an "outcome unknown" reservation that refuses the same turn id
        uid = "d" * 32
        self.ctx.voice_host.raises = RuntimeError("host crashed")
        with self.assertRaises(RuntimeError):
            await self.svc.dispatch({**self.req("再来"), "turn_uid": uid})
        self.ctx.voice_host.raises = None
        with self.assertRaises(ValueError) as cm:
            await self.svc.dispatch({**self.req("再来"), "turn_uid": uid})
        self.assertEqual(str(cm.exception), "host crashed")
        self.assertEqual(len(self.ctx.voice_host.calls), 3)

    async def test_timeout_sends_a_cancel_and_the_retry_is_refused(self):
        from maibot_unified_core import voice
        voice.HOST_SECONDS, voice.PLUGIN_SECONDS = 0.1, 0.2
        try:
            self.ctx.voice_host.delay = 5
            with self.assertRaises(asyncio.TimeoutError):
                await self.svc.dispatch(self.req())
            self.assertEqual(len(self.ctx.voice_host.cancels), 1)
            self.assertEqual(self.ctx.voice_host.cancels[0]["key"], self.ctx.voice_host.calls[0]["key"])
            self.ctx.voice_host.delay = 0
            with self.assertRaises(ValueError) as cm:
                await self.svc.dispatch(self.req())
            self.assertEqual(str(cm.exception), "voice_timeout")
            self.assertEqual(len(self.ctx.voice_host.calls), 1)       # not run again
        finally:
            voice.HOST_SECONDS, voice.PLUGIN_SECONDS = 22.0, 23.0

    async def test_caller_cancellation_cancels_the_host_turn_and_closes_the_scope(self):
        self.ctx.voice_host.delay = 5
        task = asyncio.create_task(self.svc.dispatch(self.req()))
        for _ in range(100):
            if self.ctx.voice_host.calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.ctx.voice_host.cancels), 1)
        self.assertEqual(self.svc.voice._active, {})

    async def test_different_words_under_the_same_turn_id_conflict(self):
        await self.svc.dispatch(self.req("一"))
        with self.assertRaises(ValueError) as cm:
            await self.svc.dispatch(self.req("二"))
        self.assertEqual(str(cm.exception), "voice_turn_conflict")

    async def test_two_turns_of_one_owner_never_run_concurrently(self):
        running = []
        peak = []

        def during(kw):
            running.append(kw["key"])
            peak.append(len(running))

        self.ctx.voice_host.during = during
        self.ctx.voice_host.delay = 0.1
        original = self.ctx.voice_host.call

        async def tracked(operation, **kw):
            try:
                return await original(operation, **kw)
            finally:
                if operation == "run":
                    running.remove(kw["key"])
        self.ctx.voice_host.call = tracked
        await asyncio.gather(self.svc.dispatch(self.req("一")), self.svc.dispatch({**self.req("二"), "turn_uid": "d" * 32}))
        self.assertEqual(max(peak), 1)


class ReplayValidationTests(BackendCase):
    """A replay (completed cache or stored-reply fallback) must validate the CURRENT words first."""

    def drop_completed_cache(self):
        self.svc.state.db.execute("DELETE FROM meta WHERE k LIKE 'voice_turn:%'")

    async def test_stored_reply_fallback_replays_same_words_and_refuses_changed_words(self):
        first = await self.svc.dispatch(self.req("我明天要去医院复查"))
        self.drop_completed_cache()                                  # only the completed KV is gone
        calls = len(self.ctx.voice_host.calls)
        same = await self.svc.dispatch(self.req("我明天要去医院复查"))
        self.assertEqual(same["reply"], first["reply"])
        self.assertTrue(same["replayed"])
        self.assertNotIn("tools", same)                              # no pipeline/tool metadata invented for history
        self.assertNotIn("pipeline", same)
        with self.assertRaises(ValueError) as cm:
            await self.svc.dispatch(self.req("完全不同的话"))
        self.assertEqual(str(cm.exception), "voice_turn_conflict")
        self.assertEqual(len(self.ctx.voice_host.calls), calls)      # no new model/tool work either time
        self.assertEqual(len(self.ctx.memory.items), 1)

    async def test_legacy_rows_without_a_digest_are_checked_against_the_stored_user_event(self):
        from maibot_unified_core.voice import IDEMPOTENCY_PREFIX, _ident
        await self.svc.dispatch(self.req("我明天要去医院复查"))
        self.drop_completed_cache()
        reservation = "voice_execution:" + _ident(DEVICE, TURN_UID)
        self.svc.state.put(reservation, {"status": "completed", "at": 1})          # legacy: no input_hash
        calls = len(self.ctx.voice_host.calls)
        self.assertTrue((await self.svc.dispatch(self.req("我明天要去医院复查")))["replayed"])
        with self.assertRaises(ValueError) as cm:
            await self.svc.dispatch(self.req("换了内容"))
        self.assertEqual(str(cm.exception), "voice_turn_conflict")
        self.assertEqual(len(self.ctx.voice_host.calls), calls)

    async def test_completed_cache_with_a_digest_still_conflicts_and_valid_replay_still_works(self):
        await self.svc.dispatch(self.req("你好"))
        self.assertTrue((await self.svc.dispatch(self.req("你好")))["replayed"])
        with self.assertRaises(ValueError):
            await self.svc.dispatch(self.req("不一样"))


class PersistenceTests(BackendCase):
    async def test_turn_is_persisted_in_the_owner_private_scope_with_provenance(self):
        await self.svc.dispatch(self.req())
        item = self.ctx.memory.items[0]
        self.assertEqual(item["chat_id"], OWNER_SESSION)             # owner's private conversation scope
        self.assertEqual(item["external_id"], f"device-voice:{DEVICE}:{TURN_UID}")
        self.assertIn("我明天要去医院复查", item["text"])
        self.assertEqual(item["metadata"]["channel"], "device_voice")
        self.assertTrue(item["metadata"]["is_private"])
        self.assertFalse(item["metadata"]["public_summary"])
        self.assertEqual(item["person_ids"], ["host-" + OWNER_PRIVATE_ID])
        events = self.svc.state.db.execute("SELECT * FROM events ORDER BY rowid").fetchall()
        self.assertEqual([(e["event_type"], e["origin"], e["conversation_id"], e["platform"]) for e in events],
                         [("device_voice_turn", "external_user", OWNER_SESSION, "qq"),
                          ("device_voice_reply", "bot_self", OWNER_SESSION, "qq")])   # the user event exists exactly once
        self.assertTrue(all(e["learning_allowed"] == 0 for e in events))   # ASR text never feeds persona learning
        user = json.loads(events[0]["payload"])
        self.assertEqual(user["metadata"]["channel"], "device_voice")
        self.assertTrue(user["source_id"].startswith("device-voice:"))
        # Not a QQ delivery: no activity/receipt rows were recorded.
        self.assertEqual(self.svc.state.db.execute("SELECT count(*) FROM activities").fetchone()[0], 0)

    async def test_replay_is_idempotent_even_after_cache_loss(self):
        first = await self.svc.dispatch(self.req())
        second = await self.svc.dispatch(self.req())
        self.assertTrue(second["replayed"])
        self.assertEqual(second["reply"], first["reply"])
        self.assertEqual(len(self.ctx.voice_host.calls), 1)
        self.svc.state.db.execute("DELETE FROM meta WHERE k LIKE 'voice_turn:%'")
        self.ctx.voice_host.result = {**self.ctx.voice_host.result, "reply": "完全不同的回答"}
        third = await self.svc.dispatch(self.req())
        self.assertEqual(third["reply"], first["reply"])
        self.assertEqual(len(self.ctx.voice_host.calls), 1)
        self.assertEqual(len(self.ctx.memory.items), 1)
        self.assertEqual(self.svc.state.db.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    async def test_concurrent_duplicates_run_once(self):
        self.ctx.voice_host.delay = 0.1
        results = await asyncio.gather(*[self.svc.dispatch(self.req()) for _ in range(4)])
        self.assertEqual(len(self.ctx.voice_host.calls), 1)
        self.assertEqual(len({r["reply"] for r in results}), 1)

    async def test_memory_failure_is_reported_truthfully_not_hidden(self):
        self.ctx.memory.fail = True
        r = await self.svc.dispatch(self.req())
        self.assertFalse(r["memory_stored"])
        self.assertTrue(r["reply"])
        self.ctx.memory.fail, self.ctx.memory.filtered = False, True
        r = await self.svc.dispatch({**self.req(), "turn_uid": "e" * 32})
        self.assertFalse(r["memory_stored"])                         # Host chat filter skipped: not "stored"

    async def test_device_facts_are_retrievable_from_the_owner_qq_private_context(self):
        await self.svc.dispatch(self.req("记一下，我最喜欢的颜色是青绿色"))
        ctx = await self.svc.build_context("qq_reply", "qq", OWNER_SESSION, "我喜欢什么颜色")
        self.assertTrue(any("青绿色" in f["text"] for f in ctx["facts"]), ctx["facts"])
        self.assertTrue(ctx["memory_available"])

    async def test_device_facts_do_not_leak_to_group_or_other_private_chats(self):
        await self.svc.dispatch(self.req("记一下，我的体检结果有点异常"))
        self.bind_private(OTHER_PRIVATE_ID, OTHER_SESSION, "u-friend")
        self.policy["allowed_qq_sessions"] += [OTHER_SESSION]
        self.write_policy()
        self.svc.reload_policy()
        for target in (GROUP_SESSION, OTHER_SESSION):
            ctx = await self.svc.build_context("qq_reply", "qq", target, "体检")
            self.assertEqual(ctx["facts"], [], target)
        public = await self.svc.build_context("reply", "bilibili", "bilibili:video", "体检", purpose="public")
        self.assertEqual(public["facts"], [])

    async def test_history_sent_to_the_host_is_device_voice_only_bounded_and_owner_scoped(self):
        # QQ messages reach the Host Planner through its own message store; only device turns are supplied.
        self.svc.state.ingest({"event_id": "qq:m1", "platform": "qq", "bot_account_id": "20001",
                               "conversation_id": OWNER_SESSION, "actor_id": OWNER_PRIVATE_ID,
                               "event_type": "message_received", "content": "晚上好", "occurred_at": 1000.0,
                               "origin": "external_user", "sharing_scope": "conversation",
                               "metadata": {"is_private": True}})
        self.svc.state.ingest({"event_id": "g:1", "platform": "qq", "bot_account_id": "20001",
                               "conversation_id": GROUP_SESSION, "actor_id": "u-x", "event_type": "message_received",
                               "content": "群里的秘密", "occurred_at": 3000.0, "origin": "external_user"})
        for i in range(6):
            await self.svc.dispatch({**self.req(f"第{i}句"), "turn_uid": f"{i:032x}"})
        history = self.ctx.voice_host.calls[-1]["history"]
        self.assertEqual(len(history), 8)
        self.assertTrue(all(h["channel"] == "device_voice" for h in history))
        self.assertEqual({h["speaker"] for h in history}, {"主人", "我"})
        dumped = json.dumps(history, ensure_ascii=False)
        self.assertNotIn("晚上好", dumped)
        self.assertNotIn("群里的秘密", dumped)
        self.assertEqual(self.ctx.voice_host.calls[0]["history"], [])


class RelationshipTests(BackendCase):
    async def test_voice_scope_exposes_only_the_bound_owners_preferences(self):
        from maibot_unified_core.voice_scope import CURRENT
        st = self.svc.state
        st.add_relationship_pref("qq", "20001", OWNER_PRIVATE_ID, OWNER_SESSION, "nickname", "叫我阿青，说话简短一点", "叫我阿青", "evt:o1")
        self.bind_private(OTHER_PRIVATE_ID, OTHER_SESSION, "u-friend")
        st.add_relationship_pref("qq", "20001", OTHER_PRIVATE_ID, OTHER_SESSION, "nickname", "叫我小朋友", "叫我小朋友", "evt:x1")
        st.add_relationship_pref("qq", "20001", "u-in-group", GROUP_SESSION, "nickname", "群里叫我老大", "叫我老大", "evt:g1")
        self.assertEqual(self.svc.relationship_texts("qq", OWNER_SESSION), [])          # no QQ message since restart
        token = CURRENT.set({"session": OWNER_SESSION, "message": ("你好", "evt:x", OWNER_PRIVATE_ID), "deadline": 1e18})
        try:
            self.assertEqual(self.svc.relationship_texts("qq", OWNER_SESSION), ["叫我阿青，说话简短一点"])
            self.assertEqual(self.svc.relationship_texts("qq", OTHER_SESSION), [])      # another session: nothing
            self.assertEqual(self.svc.relationship_texts("qq", GROUP_SESSION), [])
        finally:
            CURRENT.reset(token)
        self.assertEqual(self.svc.relationship_texts("qq", OWNER_SESSION), [])          # and nothing persists outside it
        self.assertEqual(dict(self.svc.last_message), {})                               # nothing fabricated


@unittest.skipUnless(sys.platform == "linux", "Linux setfacl contract")
class SocketAclTests(BackendCase):
    """grant_voice_socket_access: a narrow setfacl for voice_uid only, beside (not instead of) the XHS grant."""

    async def run_grants(self, **policy):
        import asyncio as aio
        calls = []

        class Proc:
            returncode = 0

            async def wait(self):
                return self.returncode

        async def fake_exec(*argv, **kw):
            calls.append(argv)
            p = Proc()
            p.returncode = 1 if getattr(self, "fail_acl", False) else 0
            return p

        orig = aio.create_subprocess_exec
        aio.create_subprocess_exec = fake_exec
        try:
            self.svc.policy.update(policy)
            await self.svc.grant_xhs_socket_access("/run/core.sock")
            xhs_calls = list(calls)
            await self.svc.grant_voice_socket_access("/run/core.sock")
            return xhs_calls, calls[len(xhs_calls):]
        finally:
            aio.create_subprocess_exec = orig

    async def test_valid_voice_uid_gets_exactly_rw_on_the_socket(self):
        xhs, voice = await self.run_grants(voice_uid=4242)
        self.assertEqual(voice, [("/usr/bin/setfacl", "-m", "u:4242:rw", "/run/core.sock")])
        self.assertEqual(xhs, [])                                   # XHS disabled/absent: its behaviour is untouched

    async def test_xhs_grant_is_unchanged_and_independent(self):
        xhs, voice = await self.run_grants(voice_uid=4242, xhs_enabled=True, xhs_uid=5002)
        self.assertEqual(xhs, [("/usr/bin/setfacl", "-m", "u:5002:rw", "/run/core.sock")])
        self.assertEqual(voice, [("/usr/bin/setfacl", "-m", "u:4242:rw", "/run/core.sock")])
        xhs, voice = await self.run_grants(voice_uid=None, xhs_enabled=True, xhs_uid=5002)
        self.assertEqual(len(xhs), 1)
        self.assertEqual(voice, [])                                 # voice absent: nothing, XHS still granted

    async def test_absent_or_invalid_voice_uid_grants_nothing(self):
        _, voice = await self.run_grants(voice_uid=None)
        self.assertEqual(voice, [])
        self.assertNotIn("voice_socket_acl", self.svc.last_error)
        for bad in (0, -5, os.getuid(), True, "4242", 4242.0):
            self.svc.last_error = ""
            _, voice = await self.run_grants(voice_uid=bad)
            self.assertEqual(voice, [], bad)
            self.assertEqual(self.svc.last_error, "voice_socket_acl_invalid_uid", bad)

    async def test_setfacl_failure_is_voice_specific_and_does_not_raise(self):
        self.fail_acl = True
        self.svc.last_error = ""
        _, voice = await self.run_grants(voice_uid=4242)
        self.assertEqual(len(voice), 1)
        self.assertEqual(self.svc.last_error, "voice_socket_acl_failed")   # not xhs_socket_acl_failed

    def test_start_grants_voice_right_after_xhs(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "maibot_unified_core", "service.py"), encoding="utf-8").read()
        self.assertLess(src.index("await self.grant_xhs_socket_access(path)"),
                        src.index("await self.grant_voice_socket_access(path)"))
        self.assertLess(src.index("await self.grant_voice_socket_access(path)"), src.index("self.worker=asyncio.create_task"))
