"""No network or platform writes: exercise the real runner and ledger paths."""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from phase2.bridge import Bridge
from phase2.core import DEFAULTS, TZ, Denied, Target, machine_id
from phase2.runner import Engine
from phase2.unified import MAX_REQUEST, request


class API:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.direct = []
        self.failure = False

    def identity(self):
        return {"mid": "100", "name": "synthetic"}

    def notifications(self):
        return self.messages

    def private_messages(self, owner):
        return self.direct

    def video(self, oid, with_subtitle=False):
        return Target("video", str(oid), 1, "300", "天文光谱材料", "https://www.bilibili.com/video/av1",
                      [], "BVsynthetic", "title_description_subtitle_excerpt")

    def candidates(self, config, cycle):
        return [self.video("1")]

    def send(self, kind, target, text, **kwargs):
        self.calls.append((kind, target, text, kwargs))
        if self.failure:
            raise TimeoutError()
        return {"id": str(800 + len(self.calls)), "url": "https://t.bilibili.com/800"}

    def reconcile(self, *args, **kwargs):
        return None


class Core:
    def __init__(self):
        self.calls = []
        self.fail_decision = False
        self.fail_activity = False
        self.score = 80
        self.version = "v1"
        self.count = 0

    def __call__(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if payload["op"] == "activity" and self.fail_activity:
            raise Denied("fixture_core_offline")
        if payload["op"] != "decide":
            return {"ok": True}
        if self.fail_decision:
            raise Denied("fixture_core_offline")
        self.count += 1
        return {"ok": True, "persona_version": self.version, "source_ids": ["shared:topic"],
                "decision": {"interested": True, "text": "共同记忆测试想法" + str(self.count),
                             "interest_score": self.score, "reason": "synthetic"}}


class TestBiliUnified(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.c = copy.deepcopy(DEFAULTS)
        self.c.update(bot_uid="100", owner_uid="200", writer_machine=machine_id(), paused=False)
        self.c["enabled"] = {k: True for k in self.c["enabled"]}
        self.config.write_text(json.dumps(self.c))
        self.now = datetime(2026, 10, 24, 12, tzinfo=TZ).timestamp()
        self.api = API()
        self.core = Core()
        self.engine = self.new_engine()
        self.engine.s.put("bound_uid", "100")
        self.engine.s.put("baseline_at", self.now - 100)

    def new_engine(self):
        e = Engine(self.config, self.root / "state", self.root, api=self.api, now=lambda: self.now)
        e.unified_request = self.core
        return e

    def tearDown(self):
        self.engine.s.db.close()
        self.tmp.cleanup()

    def decide_calls(self):
        return [x for x in self.core.calls if x["op"] == "decide"]

    def activities(self):
        return [x["action"] for x in self.core.calls if x["op"] == "activity"]

    def test_all_active_generation_routes_reach_common_core_with_metadata(self):
        e = self.engine
        self.assertEqual(e.dynamic()["status"], "success")
        self.assertEqual(e.proactive()["status"], "success")
        self.now += 1801
        target = Target("dynamic", "44", 11, "301", "星空摄影", "https://t.bilibili.com/55", [], "55", "body_text")
        self.assertEqual(e.proactive(target)["status"], "success")
        self.api.messages = [{"id": "reply:99", "source": "99", "uid": "202", "time": self.now - 1,
                              "text": "星空话题", "thread": "11:44:98", "root": "98", "target": target}]
        self.assertEqual(e.replies()[0]["status"], "success")
        e.s.put("dm_baseline_owner", "200")
        e.s.put("dm_baseline_at", self.now - 100)
        self.api.direct = [{"id": "dm:9", "uid": "200", "time": self.now - 1,
                            "text": "私密合成材料", "thread": "dm:200"}]
        self.assertEqual(e.replies()[0]["status"], "success")
        draft = "一起聊聊星空"
        request_id = hashlib.sha256(("group-a\n" + draft).encode()).hexdigest()
        result = Bridge(e).deliver({"stream_id": "group-a", "expected_uid": "100", "draft": draft,
                                    "request_id": request_id, "created_at": self.now},
                                   {"enabled": True, "allowed_streams": ["group-a"]})
        self.assertEqual(result["status"], "success")
        calls = self.decide_calls()
        self.assertEqual([x["task"] for x in calls], ["dynamic", "proactive", "proactive", "reply", "dm", "dynamic"])
        self.assertEqual(calls[1]["event"]["source_id"], "BVsynthetic")
        self.assertEqual(calls[1]["event"]["material_evidence"], "title_description_subtitle_excerpt")
        self.assertEqual(calls[2]["event"]["target"]["oid"], "44")
        self.assertEqual(calls[3]["event"]["actor_id"], "202")
        self.assertEqual(calls[3]["event"]["conversation_id"], "11:44:98")
        self.assertEqual(calls[4]["event"]["sharing_scope"], "conversation")
        self.assertEqual(calls[5]["event"]["source_conversation_id"], "group-a")
        self.assertFalse(calls[5]["event"]["learning_allowed"])
        self.assertTrue(all(x["event"]["action_id"] for x in calls))
        self.assertTrue(all(x["permissions"]["reply_interest_min_score"] == 65 for x in calls))
        self.assertTrue(all(x["persona_version"] == "v1" for x in self.activities()))
        self.assertEqual(len(self.api.calls), 6)

    def test_core_failure_never_falls_back_to_direct_model_or_public_send(self):
        self.core.fail_decision = True
        with patch("phase2.api.requests.post") as direct:
            self.assertEqual(self.engine.dynamic()["status"], "failed")
        direct.assert_not_called()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.activities()[0]["status"], "failed")
        self.assertEqual(self.engine.s.db.execute("select count(*) from usage").fetchone()[0], 1)

    def test_receipt_sync_failure_replays_without_resending(self):
        self.core.fail_activity = True
        self.assertEqual(self.engine.dynamic()["status"], "success")
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(self.engine.s.db.execute(
            "select revision>synced_revision from unified_receipts").fetchone()[0])
        original = self.activities()[-1]["event_id"]
        self.core.fail_activity = False
        self.now += 61
        self.engine.s.db.close()
        self.engine = self.new_engine()
        self.engine.sync()
        self.assertEqual(self.activities()[-1]["event_id"], original)
        count = len(self.activities())
        self.engine.sync()
        self.assertEqual(len(self.activities()), count)
        with self.assertRaisesRegex(Denied, "already_attempted"):
            self.engine.dynamic()
        self.assertEqual(len(self.api.calls), 1)

    def test_unknown_and_later_reconciliation_sync_new_revision_only(self):
        self.api.failure = True
        self.assertEqual(self.engine.dynamic()["status"], "unknown")
        unknown = self.activities()[-1]
        self.assertEqual(unknown["status"], "unknown")
        self.assertFalse(unknown["learning_allowed"])
        self.engine.s.update(unknown["id"], status="success", remote_id="999", url="https://t.bilibili.com/999")
        self.engine.sync()
        success = self.activities()[-1]
        self.assertEqual(success["status"], "success")
        self.assertEqual(success["canonical_status"], "succeeded")
        self.assertNotEqual(success["event_id"], unknown["event_id"])
        self.assertEqual(len(self.api.calls), 1)

    def test_local_reply_threshold_rejects_invalid_core_score(self):
        for score in (64, None, "99", True, 101, float("nan")):
            with self.subTest(score=score):
                self.core.score = score
                n = str(len(self.decide_calls()) + 1)
                self.api.messages = [{"id": "reply:" + n, "source": n, "uid": "202", "time": self.now - 1,
                                      "text": "合成评论", "thread": "thread:" + n,
                                      "target": Target("reply", n, 11, "202")}]
                self.assertEqual(self.engine.replies()[0]["status"], "skipped")
        self.assertEqual(self.api.calls, [])

    def test_missing_persona_version_fails_closed(self):
        self.core.version = ""
        self.assertEqual(self.engine.dynamic()["status"], "failed")
        self.assertEqual(self.api.calls, [])

    def test_persona_version_is_fresh_for_each_decision(self):
        self.engine.dynamic()
        self.core.version = "v2"
        self.engine.proactive()
        self.assertEqual([x["persona_version"] for x in self.activities()], ["v1", "v2"])

    def test_actual_read_is_recorded_even_when_outbound_quota_denies(self):
        self.c["limits"]["proactive"] = 0
        self.config.write_text(json.dumps(self.c))
        self.assertEqual(self.engine.proactive()["status"], "skipped")
        events = [x["event"] for x in self.core.calls if x["op"] == "event"]
        self.assertEqual(events[0]["event_type"], "content_read")
        self.assertTrue(events[0]["learning_allowed"])
        self.assertEqual(self.decide_calls(), [])
        self.assertEqual(self.api.calls, [])

    def test_replayed_input_is_not_relearned(self):
        event = self.engine.event("proactive", "a", "合成材料", self.api.video("1"))
        self.engine.outbox.event(event)
        self.engine.outbox.event(event)
        self.engine.sync()
        self.engine.outbox.event(event)
        self.engine.sync()
        self.assertEqual(len([x for x in self.core.calls if x["op"] == "event"]), 1)

    def test_sender_receipt_commit_survives_optional_memory_failure(self):
        def fail(text):
            raise RuntimeError("synthetic learning failure")
        result = self.engine.sender.deliver("dynamic", None, "direct-test", lambda: "测试回执保留", memory=fail)
        self.assertEqual(result["status"], "success")
        self.engine.sync()
        self.assertEqual(self.activities()[0]["remote_id"], "801")
        self.assertEqual(len(self.api.calls), 1)

    def test_existing_ledger_backfill_is_idempotent(self):
        self.engine.sender.deliver("dynamic", None, "historical", lambda: "历史合成成功")
        self.engine.s.db.execute("delete from unified_receipts")
        self.engine.s.db.close()
        self.engine = self.new_engine()
        self.engine.sync()
        self.engine.s.db.close()
        self.engine = self.new_engine()
        self.engine.sync()
        self.assertEqual(len(self.activities()), 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_image_material_keeps_actual_input_and_no_credentials(self):
        target = Target("dynamic", "9", 11, "300", "合成图文", "https://t.bilibili.com/9",
                        ["https://i0.hdslb.com/synthetic.jpg"], "9", "body_and_image")
        with patch("phase2.unified.public_image", return_value="data:image/jpeg;base64,YQ=="):
            self.assertEqual(self.engine.proactive(target)["status"], "success")
        call = self.decide_calls()[0]
        self.assertEqual(call["images"], ["data:image/jpeg;base64,YQ=="])
        self.assertNotIn("api_key", json.dumps(call))
        self.assertFalse((self.root / "model.json").exists())


class TestBiliSocketProtocol(unittest.TestCase):
    def test_newline_protocol_handles_fragmented_reply(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.side_effect = [b'{"ok":', b'true}\n']
        with patch("phase2.unified.socket.socket", return_value=connection):
            self.assertEqual(request({"op": "event", "event": {}}, path="/synthetic.sock"), {"ok": True})
        self.assertEqual(connection.connect.call_args.args, ("/synthetic.sock",))
        self.assertTrue(connection.sendall.call_args.args[0].endswith(b"\n"))

    def test_rejected_and_incomplete_responses_fail_closed(self):
        for value in (b'{"ok":false,"error":"sensitive"}\n', b''):
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.recv.return_value = value
            with patch("phase2.unified.socket.socket", return_value=connection):
                with self.assertRaisesRegex(Denied, "unified_core_unavailable"):
                    request({"op": "event"})

    def test_payload_limit_precedes_socket_connection(self):
        with patch("phase2.unified.socket.socket") as client:
            with self.assertRaisesRegex(Denied, "unified_request_too_large"):
                request({"data": "x" * MAX_REQUEST})
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
