import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from maibot_unified_core.store import State


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.clock = [datetime(2026, 10, 24, 15, 59, tzinfo=timezone.utc).timestamp()]
        self.policy = {
            "allowed_qq_sessions": ["group-a", "group-b", "private-a"],
            "shared_groups": [["group-a", "group-b"]],
            "internal_cross_platform": ["group-a"],
            "public_qq_sessions": ["group-a"],
            "public_topics": ["AI", "游戏", "美食"],
            "bot_accounts": {"qq": "bot-qq", "bilibili": "bot-bili"},
        }
        self.state = State(self.path, self.policy, now=lambda: self.clock[0])

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def event(self, native_id="message-1", **kwargs):
        return {
            "platform": "qq", "bot_account_id": "bot-qq", "conversation_id": "group-a",
            "actor_id": "person-a", "event_type": "message_received", "native_event_id": native_id,
            "content": "我喜欢草莓甜点", "origin": "external_user", "learning_allowed": True,
            "sharing_scope": "policy", **kwargs,
        }

    def ingest(self, *args, **kwargs):
        result = self.state.ingest(self.event(*args, **kwargs))
        return self.state.get_event(result["event_id"])

    def restart(self):
        self.state.close()
        self.state = State(self.path, now=lambda: self.clock[0])

    def test_native_replay_is_idempotent_but_equal_text_from_new_event_is_not(self):
        first = self.state.ingest(self.event())
        self.clock[0] += 30
        replay = self.state.ingest(self.event(received_at=self.clock[0], nickname="new nickname"))
        second = self.state.ingest(self.event("message-2"))
        self.assertTrue(first["is_new"])
        self.assertFalse(replay["is_new"])
        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(self.state.learning_candidates()), 2)
        with self.assertRaisesRegex(ValueError, "payload_conflict"):
            self.state.ingest(self.event(content="mutated under an existing ID"))
        with self.assertRaisesRegex(ValueError, "stable_event_id"):
            self.state.ingest(self.event(native_id=""))

    def test_identity_is_namespaced_and_confirmed_merge_retains_old_ids(self):
        a = self.ingest(nickname="same name", person_id="maibot-known-person")
        b = self.ingest("another-group", conversation_id="group-b", nickname="changed name")
        other = self.ingest("bili-message", platform="bilibili", bot_account_id="bot-bili",
                            actor_id="same-uid", conversation_id="thread-1", nickname="same name")
        self.assertEqual(a["person_id"], "maibot-known-person")
        self.assertEqual(a["person_id"], b["person_id"])
        self.assertNotEqual(a["person_id"], other["person_id"])
        p = {"platform": "qq", "bot_account_id": "bot-qq", "actor_id": "person-a"}
        q = {"platform": "bilibili", "bot_account_id": "bot-bili", "actor_id": "same-uid"}
        with self.assertRaisesRegex(ValueError, "confirmed_admin"):
            self.state.bind_identities(p, q, "", "comment claimed to be owner")
        merge = self.state.bind_identities(p, q, "verified-local-admin", "user confirmed identity")
        self.assertEqual(merge["host_memory_reconciliation"], "pending")
        self.assertEqual(self.state.get_event(other["event_id"])["person_id"], a["person_id"])
        self.assertIn(other["person_id"], self.state.person_ids(a["person_id"]))
        self.restart()
        self.assertEqual(self.state.resolve_identity(**q), a["person_id"])
        self.assertFalse(self.state.bind_identities(p, q, "admin", "already verified")["is_new"])
        q["actor_id"] = "bot-bili"
        with self.assertRaisesRegex(ValueError, "cannot_bind_bot"):
            self.state.bind_identities(p, q, "admin", "wrong target")

    def test_scope_private_unknown_and_public_topic_summary_filters(self):
        group = self.ingest()
        private = self.ingest("private", conversation_id="private-a", conversation_type="private")
        self.assertTrue(self.state.allowed(group, "qq", "group-b"))
        self.assertFalse(self.state.allowed(group, "qq", "unapproved-group"))
        self.assertTrue(self.state.allowed(private, "qq", "private-a"))
        self.assertFalse(self.state.allowed(private, "qq", "group-a"))
        self.assertFalse(self.state.allowed(group, "bilibili", "self", "internal"))
        self.assertFalse(self.state.allowed(group, "bilibili", "self", "public"))
        summary = {"metadata": {"platform": "qq", "chat_id": "group-a", "public_summary": True,
                                "category": "AI", "source_event_ids": [group["event_id"]]},
                   "content": "讨论了 AI 的有限记忆", "sharing_scope": "public"}
        self.assertTrue(self.state.allowed(summary, "bilibili", "self", "public"))
        self.assertTrue(self.state.allowed(summary, "bilibili", "self", "internal"))
        for patch in ({"category": "private relationship"}, {"public_summary": False},
                      {"personal_details": True}, {"source_type": "person_fact"},
                      {"conversation_type": "private"}):
            bad = {**summary, "metadata": {**summary["metadata"], **patch}}
            self.assertFalse(self.state.allowed(bad, "bilibili", "self", "public"), patch)
        self.assertFalse(self.state.allowed({"content": "unknown provenance"}, "qq", "group-a"))

    def test_revocation_and_correction_invalidate_stale_context_sources_across_restart(self):
        event = self.ingest()
        summary = {**event, "public_summary": True, "category": "AI", "summary_content": "模型评估需要区分记忆训练资料与泛化能力。", "source_event_ids": [event["event_id"]]}
        self.assertTrue(self.state.allowed(summary, "bilibili", "self", "public"))
        initial_revision = self.state.get("context_revision")
        policy = {**self.policy, "shared_groups": [], "public_qq_sessions": []}
        self.state.set_policy(policy)
        self.assertFalse(self.state.allowed(event, "qq", "group-b"))
        self.assertFalse(self.state.allowed(summary, "bilibili", "self", "public"))
        self.assertGreater(self.state.get("context_revision"), initial_revision)
        self.state.set_policy(self.policy)
        self.state.invalidate_event(event["event_id"], "preference corrected")
        self.restart()
        self.assertFalse(self.state.allowed(event, "qq", "group-a"))
        self.assertFalse(self.state.allowed(summary, "bilibili", "self", "public"))
        self.assertEqual(self.state.learning_candidates(), [])

    def test_public_label_cannot_authorize_personal_narratives(self):
        base = {"platform": "qq", "conversation_id": "group-a", "public_summary": True,
                "category": "AI", "sharing_scope": "policy"}
        for text in ("有用户会纠正AI身份，强调自己是DeepSeek而非其他称呼。",
                     "用户喜欢游戏和草莓。", "我最近在群里说了自己的经历。", "", "联系账号242616讨论AI。"):
            self.assertFalse(self.state.allowed({**base, "summary_content": text}, "bilibili", "self", "public"))
        self.assertTrue(self.state.allowed({**base, "summary_content": "评估模型能力时需区分记忆训练资料与解决新问题的能力。"},
                                           "bilibili", "self", "public"))

    def test_learning_excludes_bot_echoes_and_resumes_only_pending_new_evidence(self):
        self.ingest("bot", actor_id="bot-qq", origin="external_user")
        self.ingest("system", origin="system_task")
        self.ingest("other-group", conversation_id="unapproved-group")
        events = [self.ingest(f"user-{i}", content=f"new content {i}") for i in range(7)]
        self.assertEqual(len(self.state.learning_candidates(99)), 5)
        first = self.state.learning_candidates()[0]
        self.state.mark_learned(first["event_id"], "stored", ["native-memory-hash"])
        self.assertFalse(self.state.mark_learned(first["event_id"], "failed"))
        second = self.state.learning_candidates()[0]
        self.state.mark_learned(second["event_id"], "deferred", detail="quota reached")
        self.restart()
        self.assertNotIn(first["event_id"], [e["event_id"] for e in self.state.learning_candidates()])
        self.assertIn(second["event_id"], [e["event_id"] for e in self.state.learning_candidates()])
        for _ in range(3):
            self.state.mark_learned(second["event_id"], "failed", detail="bounded error")
        self.assertNotIn(second["event_id"], [e["event_id"] for e in self.state.learning_candidates()])
        self.assertIn(first["event_id"], [e["event_id"] for e in self.state.recent_events()])
        self.state.set_policy({**self.policy, "learning_enabled": False})
        self.assertEqual(self.state.learning_candidates(), [])
        self.assertEqual(len(events), 7)

    def test_explicit_qq_feedback_precedes_more_than_fifty_old_reads_without_resetting_budget(self):
        old = []
        for i in range(60):
            old.append(self.ingest(f"old-bili-{i}", platform="bilibili", bot_account_id="bot-bili",
                                   actor_id="video-author", event_type="content_read", origin="platform_content",
                                   conversation_id=f"video-{i}", content=f"actual video material {i}"))
            self.clock[0] += 1
        # A long ordinary QQ prefix must not hide the real feedback either.
        for i in range(60):
            self.ingest(f"ordinary-qq-{i}", content=f"ordinary discussion {i}")
            self.clock[0] += 1
        feedback = self.ingest("new-explicit-feedback", content="请少用哼这个口头禅，回复短一些")
        self.state.put("learning_cursor", {"last_seen": "keep-existing-cursor"})
        for i in range(18):
            self.assertTrue(self.state.reserve_quota(f"existing-background-{i}"))
        selected = self.state.learning_candidates(99)
        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0]["event_id"], feedback["event_id"])
        self.assertEqual([e["event_id"] for e in selected[1:]], [e["event_id"] for e in old[:4]])
        self.assertEqual(self.state.quota_used(), 18)
        self.assertEqual(self.state.get("learning_cursor"), {"last_seen": "keep-existing-cursor"})
        self.assertEqual(self.state.learning_candidates(1)[0]["event_id"], feedback["event_id"])
        self.assertEqual(self.state.learning_candidates(0), [])
        self.assertFalse(self.state.ingest(self.event("new-explicit-feedback", content="请少用哼这个口头禅，回复短一些"))["is_new"])
        self.state.mark_learned(feedback["event_id"], "stored")
        self.restart()
        self.assertEqual(self.state.learning_candidates()[0]["event_id"], old[0]["event_id"])
        self.assertNotIn(feedback["event_id"], [e["event_id"] for e in self.state.learning_candidates()])
        self.assertEqual(self.state.quota_used(), 18)
        self.assertEqual(self.state.get("learning_cursor"), {"last_seen": "keep-existing-cursor"})

    def test_feedback_priority_keeps_active_scope_origin_and_pending_filters(self):
        oldest = self.ingest("oldest-normal", content="普通讨论")
        self.clock[0] += 1
        self.ingest("bot-feedback", actor_id="bot-qq", event_type="user_feedback", content="请少用口头禅")
        self.ingest("unapproved-feedback", conversation_id="unapproved-group", event_type="user_feedback")
        inactive = self.ingest("withdrawn-feedback", event_type="user_feedback")
        self.state.invalidate_event(inactive["event_id"], "withdrawn")
        failed = self.ingest("failed-feedback", event_type="user_feedback")
        self.state.mark_learned(failed["event_id"], "failed")
        self.ingest("platform-labelled-feedback", origin="platform_content", event_type="user_feedback")
        feedback = self.ingest("valid-feedback", content="请少用这个口头禅")
        self.state.mark_learned(feedback["event_id"], "deferred")
        rows = self.state.learning_candidates()
        self.assertEqual(rows[0]["event_id"], feedback["event_id"])
        self.assertEqual(rows[1]["event_id"], oldest["event_id"])
        ids = {e["event_id"] for e in rows}
        self.assertNotIn(inactive["event_id"], ids)
        self.assertNotIn(failed["event_id"], ids)
        self.state.set_policy({**self.policy, "allowed_qq_sessions": ["group-b"]})
        self.assertEqual(self.state.learning_candidates(), [])

    def test_activity_unknown_requires_reconciliation_success_survives_learning_failure(self):
        base = {"action_id": "action-1", "platform": "bilibili", "bot_account_id": "bot-bili", "conversation_id": "self",
                "sharing_scope": "public", "kind": "dynamic", "text": "confirmed external activity"}
        self.state.record_activity({**base, "status": "executing"})
        self.state.record_activity({**base, "status": "unknown", "error": "timeout"})
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.state.record_activity({**base, "status": "executing"})
        with self.assertRaisesRegex(ValueError, "executor_receipt"):
            self.state.record_activity({**base, "status": "succeeded"})
        success = self.state.record_activity({**base, "status": "success", "canonical_status": "succeeded",
                                              "remote_id": "verified-dynamic-123", "url": "https://t.bilibili.com/123"})
        self.state.put("last_learning_error", "embedding_failed")
        self.state.record_activity({"action_id": "action-2", "platform": "bilibili", "bot_account_id": "bot-bili", "status": "unknown",
                                    "remote_id": "unverified-id", "sharing_scope": "public"})
        self.restart()
        records = self.state.recent_activity("bilibili", context={"platform": "qq", "conversation_id": "group-b"})
        self.assertEqual({r["status"] for r in records}, {"succeeded", "unknown"})
        self.assertEqual(success["original_status"], "success")
        self.assertFalse(success["learning_allowed"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.state.record_activity({**base, "status": "planned"})
        self.assertEqual(self.state.recent_activity(since=self.clock[0] + 1), [])

    def test_restart_recovers_inflight_send_as_unknown_without_replaying(self):
        self.state.record_activity({"action_id": "crashed-send", "platform": "qq", "status": "executing"})
        self.restart()
        result = self.state.recent_activity()[0]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "process_interrupted")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            self.state.record_activity({"action_id": "crashed-send", "platform": "qq", "status": "executing"})

    def test_confirmed_activity_visibility_can_change_without_changing_delivery(self):
        base = {"action_id": "visible-check", "platform": "bilibili", "status": "success",
                "remote_id": "123", "content": "original content", "target": "1:99",
                "persona_version": "4", "source_ids": ["read-99"], "bot_account_id": "bot-bili",
                "public_visibility": "not_independently_checked"}
        original = self.state.record_activity(base)
        checked_at = self.clock[0] + 5
        annotation = {"public_visibility": "account_only_or_not_publicly_verified",
                      "visibility_checked_at": checked_at,
                      "visibility_evidence": {"authenticated_exact_match": True, "guest_exact_match": False,
                          "authenticated_detail_code": 0, "guest_detail_code": 12006,
                          "authenticated_state": 0, "method": "bounded_comment_api", "Cookie": "do-not-store",
                          "text": "private content must not enter the visibility annotation"}}
        self.clock[0] += 10
        result = self.state.record_activity({**base, **annotation, "event_id": "new-receipt-digest"})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["remote_id"], original["remote_id"])
        self.assertEqual(result["original_status"], "success")
        self.assertEqual(result["created_at"], original["created_at"])
        self.assertNotIn("Cookie", result["visibility_evidence"])
        self.assertNotIn("text", result["visibility_evidence"])
        self.assertNotIn("event_id", result)
        self.assertEqual(self.state.record_activity(base), result)
        self.assertEqual(self.state.record_activity({**base, **annotation}), result)
        self.restart()
        self.assertEqual(self.state.recent_activity()[0], result)
        later = {**annotation, "visibility_checked_at": checked_at + 20,
                 "public_visibility": "guest_api_visible",
                 "visibility_evidence": {"guest_exact_match": True, "method": "bounded_comment_api"}}
        result2 = self.state.record_activity({**base, **later})
        self.assertEqual(result2["public_visibility"], "guest_api_visible")
        self.assertEqual(self.state.record_activity({**base, **annotation}), result2)
        for patch in ({"status": "executing"}, {"status": "unknown"}, {"remote_id": "999"},
                      {"content": "replacement"}, {"target": "1:111"}, {"persona_version": "5"},
                      {"source_ids": ["different-read"]}, {"bot_account_id": "different-bot"}):
            with self.assertRaises(ValueError):
                self.state.record_activity({**base, **later, **patch})
        self.assertEqual(self.state.recent_activity()[0], result2)

    def test_visibility_annotations_reject_unbounded_or_unproven_claims(self):
        base = {"action_id": "bad-visibility", "platform": "bilibili", "status": "success", "remote_id": "123"}
        self.state.record_activity(base)
        for patch in ({"public_visibility": "everyone_saw_it"},
                      {"public_visibility": "guest_api_visible"},
                      {"visibility_checked_at": float("nan")},
                      {"visibility_evidence": {"guest_detail_code": "12006"}},
                      {"visibility_evidence": {"authenticated_exact_match": 1}}):
            with self.assertRaises(ValueError):
                self.state.record_activity({**base, **patch})

    def test_single_writer_rejects_duplicate_before_recovering_live_actions(self):
        self.state.record_activity({"action_id": "live-send", "platform": "qq", "status": "executing"})
        with self.assertRaisesRegex(RuntimeError, "writer_already_running"):
            State(self.path)
        self.assertEqual(self.state.recent_activity()[0]["status"], "executing")

    def test_crash_after_learning_reservation_does_not_block_next_event_or_repeat_call(self):
        first = self.ingest("learning-before-crash")
        second = self.ingest("learning-after-crash")
        self.assertTrue(self.state.reserve_quota("learn:" + first["event_id"]))
        self.restart()
        self.assertFalse(self.state.reserve_quota("learn:" + first["event_id"]))
        candidates = self.state.learning_candidates()
        self.assertNotIn(first["event_id"], [e["event_id"] for e in candidates])
        self.assertIn(second["event_id"], [e["event_id"] for e in candidates])
        row = self.state.db.execute("SELECT status,detail FROM learning WHERE event_id=?", (first["event_id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("unverified", row["detail"])

    def test_personality_constraints_then_feedback_growth_and_append_only_rollback(self):
        self.state.init_persona({"identity": "大小姐", "relationship": "喜欢 Claude"}, {"avoid_phrases": []})
        feedback = self.ingest("feedback", event_type="user_feedback", content="请少用这个口头禅")
        self_echo = self.ingest("echo", actor_id="bot-qq", content="我应该改人设")
        self.assertFalse(self.state.propose_changes({"identity": "new identity"}, [feedback["event_id"]], "bad")["accepted"])
        self.assertFalse(self.state.propose_changes({"avoid_phrases": ["old phrase"]}, [self_echo["event_id"]], "self evidence")["accepted"])
        self.assertFalse(self.state.propose_changes({"interests": ["AI"]}, [feedback["event_id"]], "one anecdote")["accepted"])
        update = self.state.propose_changes({"avoid_phrases": ["old phrase"]}, [feedback["event_id"]], "explicit feedback")
        self.assertTrue(update["accepted"])
        self.assertEqual(update["version"], 2)
        self.assertTrue(self.state.reserve_quota("test-reflection"))
        self.state.record_activity({"action_id": "published", "platform": "bilibili", "status": "success", "remote_id": "123"})
        rollback = self.state.rollback_persona(1, "verified rollback")
        self.assertEqual(rollback["version"], 3)
        self.assertEqual(rollback["rollback_of"], 1)
        self.assertEqual(rollback["dynamic"]["avoid_phrases"], [])
        self.restart()
        self.assertEqual(self.state.persona()["version"], 3)
        self.assertEqual(self.state.persona()["stable"]["identity"], "大小姐")
        self.assertEqual(self.state.quota_used(), 1)
        self.assertEqual(self.state.recent_activity()[0]["status"], "succeeded")
        self.assertEqual(self.state.persona(2)["dynamic"]["avoid_phrases"], ["old phrase"])

    def test_growth_counts_distinct_original_sources_and_max_two_fields(self):
        self.state.init_persona({"identity": "大小姐"})
        a = self.ingest("read-a", platform="bilibili", bot_account_id="bot-bili", source_id="same-video")
        b = self.ingest("read-b", platform="bilibili", bot_account_id="bot-bili", source_id="same-video")
        self.assertFalse(self.state.propose_changes({"interests": ["AI"]}, [a["event_id"], b["event_id"]], "repeated read")["accepted"])
        c = self.ingest("read-c", platform="bilibili", bot_account_id="bot-bili", source_id="second-video")
        self.assertTrue(self.state.propose_changes({"interests": ["AI"]}, [a["event_id"], c["event_id"]], "two sources")["accepted"])
        self.assertFalse(self.state.propose_changes({"tone": "soft", "verbosity": "short", "curiosity": 0.2}, [a["event_id"], c["event_id"]], "too broad")["accepted"])

    def test_actual_feedback_message_supports_structured_list_but_not_platform_content(self):
        self.state.init_persona({"identity": "大小姐"})
        event = self.ingest("direct-feedback", content="请少用口头禅，回复短一些")
        changes = [{"field": "response_length", "value": "简短一些", "evidence_ids": [event["event_id"]], "reason": "明确反馈"}]
        result = self.state.propose_changes(changes, [event["event_id"]], "user feedback")
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["previous_values"]["response_length"])
        external = self.ingest("video-feedback", origin="platform_content", content="请少用口头禅")
        rejected = self.state.propose_changes({"avoid_phrases": ["口头禅"]}, [external["event_id"]], "not user feedback")
        self.assertFalse(rejected["accepted"])
        unrelated = self.ingest("unrelated", content="我喜欢 AI")
        mixed = [{"field": "interests", "value": ["AI"], "evidence_ids": [unrelated["event_id"]]}]
        self.assertFalse(self.state.propose_changes(mixed, [event["event_id"], unrelated["event_id"]], "cannot borrow evidence")["accepted"])

    def test_private_feedback_stays_local_and_correction_removes_growth_evidence(self):
        self.state.init_persona({"identity": "大小姐"})
        private = self.ingest("private-feedback", conversation_id="private-a", sharing_scope="private",
                              content="请少用口头禅，回答短一些", event_type="user_feedback")
        result = self.state.propose_changes({"response_length": "更短"}, [private["event_id"]], "private feedback")
        self.assertFalse(result["accepted"])
        self.assertFalse(self.state.growth_evidence_allowed(private))
        group = self.ingest("group-feedback", content="请少用口头禅", event_type="user_feedback")
        self.assertTrue(self.state.growth_evidence_allowed(group))
        self.state.invalidate_event(group["event_id"], "user withdrew feedback")
        self.assertFalse(self.state.propose_changes({"avoid_phrases": ["口头禅"]}, [group["event_id"]], "withdrawn feedback")["accepted"])
        self.assertNotIn(group["event_id"], [e["event_id"] for e in self.state.recent_events()])

    def test_public_bili_reading_and_feedback_reach_qq_only_for_authorized_bot(self):
        for origin in ("bot_self", "platform_content", "external_user"):
            source = {"platform": "bilibili", "bot_account_id": "bot-bili", "conversation_id": "video:123",
                      "origin": origin, "sharing_scope": "public", "content": "实际读到标题和简介"}
            self.assertTrue(self.state.allowed(source, "qq", "group-b"))
            self.assertFalse(self.state.allowed({**source, "bot_account_id": "another-bot"}, "qq", "group-b"))
            self.assertFalse(self.state.allowed({**source, "sharing_scope": "conversation"}, "qq", "group-b"))

    def test_quota_reservations_persist_failed_requests_and_china_midnight(self):
        for i in range(20):
            self.assertTrue(self.state.reserve_quota(f"request-{i}"))
        self.assertFalse(self.state.reserve_quota("request-20", limit=999))
        self.assertFalse(self.state.reserve_quota("request-0"))
        self.restart()
        self.assertEqual(self.state.quota_used(), 20)
        self.clock[0] += 61  # UTC 16:00 is China midnight.
        self.assertEqual(self.state.quota_used(), 0)
        self.assertFalse(self.state.reserve_quota("request-0"))
        self.assertTrue(self.state.reserve_quota("next-day-task"))

    def test_audit_excludes_prompts_and_backup_preserves_committed_state(self):
        event = self.ingest(source_url="https://example.com/article?signature=SECRET",
                            attachment_refs=["asset:123", "https://example.com/image?signed=SECRET"])
        self.assertEqual(event["source_url"], "https://example.com/article")
        self.assertEqual(event["attachment_refs"], ["asset:123"])
        activity = self.state.record_activity({"action_id": "reply-link", "platform": "bilibili", "status": "success",
                                               "remote_id": "123", "url": "https://www.bilibili.com/video/BV1?signature=SECRET#reply123"})
        self.assertEqual(activity["url"], "https://www.bilibili.com/video/BV1#reply123")
        audit_id = self.state.audit_context("reply", "qq", "group-a", [event["event_id"]], 1, ["reply"], "replyer")
        row = self.state.db.execute("SELECT payload FROM context_audits WHERE audit_id=?", (audit_id,)).fetchone()
        payload = json.loads(row[0])
        self.assertNotIn("content", payload)
        self.assertNotIn("prompt", payload)
        backup_path = Path(self.temp.name) / "backup.sqlite"
        self.state.backup(backup_path)
        backup = State(backup_path)
        try:
            self.assertEqual(backup.get_event(event["event_id"])["content"], event["content"])
        finally:
            backup.close()


if __name__ == "__main__":
    unittest.main()
