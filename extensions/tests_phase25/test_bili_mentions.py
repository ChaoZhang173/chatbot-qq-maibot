"""Real sender/model/serialization guards, with synthetic profiles and no network."""
import copy
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import test_bili_unified as fixtures

from phase2.api import Bili
from phase2.core import Denied, safe_text
from phase2.mentions import (
    MentionText,
    comment_fields,
    dynamic_contents,
    policy,
    registry,
    select,
)


class MentionTests(TestCase):
    setUp = fixtures.TestBiliUnified.setUp
    tearDown = fixtures.TestBiliUnified.tearDown
    new_engine = fixtures.TestBiliUnified.new_engine

    def enable(self):
        self.c["mentions"] = {"mode": "mutual_and_manual", "max_per_action": 1,
                              "manual_uids": [], "cache_seconds": 300}
        self.config.write_text(json.dumps(self.c))
        self.people = [{"uid": "400", "name": "熟人", "source": "mutual"}]
        self.reads = []
        def mutuals(uid):
            self.reads.append(("mutuals", uid))
            return copy.deepcopy(self.people)
        def profile(uid):
            self.reads.append(("profile", uid))
            row = next(row for row in self.people if row["uid"] == uid)
            return {"uid": uid, "name": row["name"]}
        self.api.mutuals, self.api.profile = mutuals, profile

    def test_mutual_registry_cache_and_lookup_failure_never_broadens_contacts(self):
        self.enable()
        contacts = registry(self.engine.s, self.c, self.api, self.now)
        self.assertEqual(contacts, self.people)
        self.assertEqual(registry(self.engine.s, self.c, self.api, self.now + 60), contacts)
        self.assertEqual(len(self.reads), 1)
        self.api.mutuals = lambda uid: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(registry(self.engine.s, self.c, self.api, self.now + 301), [])
        self.assertEqual(self.engine.s.get("mention_registry")["status"], "read_failed")

    def test_unknown_uid_raw_at_and_more_than_one_are_rejected(self):
        self.enable()
        for bad in (["999"], ["400", "400"], [400], "400"):
            with self.assertRaises(Denied):
                select("一起聊聊", bad, self.people, "dynamic")
        for text in ("@熟人 一起聊聊", "＠熟人 一起聊聊"):
            with self.assertRaises(Denied):
                safe_text(text)
        with self.assertRaises(Denied):
            select("你好", ["400"], self.people, "dm")
        self.c["mentions"]["manual_uids"] = ["熟人昵称"]
        with self.assertRaises(ValueError):
            policy(self.c)

    def test_real_payloads_have_exact_uid_entities_and_same_display_text(self):
        self.enable()
        text = MentionText("一起聊聊这个公开话题", self.people)
        self.assertEqual(safe_text(text), "@熟人 一起聊聊这个公开话题")
        self.assertEqual(json.loads(comment_fields(text)["at_name_to_mid"]), {"熟人": 400})
        nodes = dynamic_contents(text)
        self.assertEqual(nodes[0], {"raw_text": "@熟人 ", "type": 2, "biz_id": "400"})
        self.assertEqual("".join(node["raw_text"] for node in nodes), str(text))
        api = Bili.__new__(Bili)
        api.credentials = {"BILI_JCT": "synthetic"}
        captured = []
        def request(path, **kwargs):
            captured.append((path, kwargs))
            return {"rpid_str": "900", "dyn_id_str": "901"}
        api.request = request
        api.send("proactive", self.api.video("1"), text)
        api.send("dynamic", None, text)
        self.assertEqual(json.loads(captured[0][1]["data"]["at_name_to_mid"]), {"熟人": 400})
        self.assertEqual(captured[1][1]["body"]["dyn_req"]["content"]["contents"], nodes)

    def test_sender_rechecks_mutual_and_profile_before_one_structured_send(self):
        self.enable()
        original = self.core
        def core(payload):
            result = original(payload)
            if payload["op"] == "decide":
                self.assertEqual(payload["permissions"]["mention_candidates"], self.people)
                result["decision"]["mention_uids"] = ["400"]
            return result
        self.engine.unified_request = core
        result = self.engine.dynamic()
        self.assertEqual(result["status"], "success")
        self.assertIsInstance(self.api.calls[0][2], MentionText)
        self.assertEqual(self.api.calls[0][2].mentions, self.people)
        self.assertEqual(sum(name == "mutuals" for name, uid in self.reads), 2)
        self.assertIn(("profile", "400"), self.reads)
        self.assertEqual(self.core.count, 1)

    def test_relation_changes_after_generation_stop_send(self):
        self.enable()
        text = MentionText("这段材料很有趣", self.people)
        self.people = []
        result = self.engine.sender.deliver("dynamic", None, "synthetic-dynamic", lambda: text)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.api.calls)

    def test_manual_uid_requires_exact_live_uid_and_name(self):
        self.enable()
        self.c["mentions"].update(mode="manual", manual_uids=["400"])
        values = registry(self.engine.s, self.c, self.api, self.now)
        self.assertEqual(values, [{"uid": "400", "name": "熟人", "source": "manual"}])
        self.assertNotIn(("mutuals", "100"), self.reads)

    def test_visibility_annotation_replays_only_receipt_and_keeps_success(self):
        result = self.engine.proactive()
        self.assertEqual(result["status"], "success")
        row = self.engine.s.db.execute("SELECT * FROM actions").fetchone()
        before = dict(row)
        self.engine.outbox.visibility(row["id"], row["remote_id"], self.now,
            "account_only_or_not_publicly_verified", {
                "authenticated_exact_match": True, "guest_exact_match": False,
                "authenticated_detail_code": 0, "guest_detail_code": 12006, "authenticated_state": 17})
        self.engine.sync()
        self.assertEqual(dict(self.engine.s.db.execute("SELECT * FROM actions").fetchone()), before)
        self.assertEqual(len(self.api.calls), 1)
        receipt = [call["action"] for call in self.core.calls if call["op"] == "activity"][-1]
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["public_visibility"], "account_only_or_not_publicly_verified")
        self.assertEqual(receipt["visibility_checked_at"], self.now)
        self.assertEqual(receipt["visibility_evidence"]["authenticated_state"], 17)

    def test_same_proactive_decision_drives_bounded_engagement_under_writer_lock(self):
        self.c["engagement"] = {"enabled": True, "limits": {"like": 10, "favorite": 5, "follow": 2, "coin": 2}}
        self.config.write_text(json.dumps(self.c))
        self.api.credentials = {"BILI_JCT": "synthetic"}
        self.api.nav = {"money": 2}
        requests = []
        def request(path, params=None, data=None):
            requests.append((path, params, data))
            if path == "/x/web-interface/archive/relation":
                return {"like": False, "favorite": False, "coin": 0}
            if path == "/x/web-interface/archive/like":
                return {}
            raise AssertionError("unexpected engagement request")
        self.api.request = request
        original = self.core
        def core(payload):
            result = original(payload)
            if payload["op"] == "decide":
                permissions = payload["permissions"]["engagement"]
                self.assertTrue(permissions["actual_read_video"])
                self.assertEqual(permissions["limits"], self.c["engagement"]["limits"])
                self.assertEqual(permissions["used_today"]["like"], 0)
                result["decision"]["engagement"] = {"like": True, "favorite": False, "follow_author": False, "coin": 0}
            return result
        self.engine.unified_request = core
        with self.engine.s.lock():
            self.engine.recover()
            result = self.engine.proactive(self.api.video("1"))
        self.assertFalse(self.engine.s._lock_held)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["engagement"][0]["status"], "success")
        self.assertEqual(self.core.count, 1)
        self.assertEqual([path for path, params, data in requests if data is not None], ["/x/web-interface/archive/like"])
        receipt = next(call["action"] for call in self.core.calls if call["op"] == "activity" and call["action"]["kind"] == "like")
        self.assertEqual(receipt["result_stage"], "platform_api_receipt")
        self.assertEqual(receipt["remote_id_type"], "operation_receipt_not_content_id")

    def test_optional_engagement_failure_preserves_committed_comment_success(self):
        self.c["engagement"] = {"enabled": True, "limits": {"like": 10, "favorite": 5, "follow": 2, "coin": 2}}
        self.config.write_text(json.dumps(self.c))
        executor = Mock()
        executor.execute.side_effect = RuntimeError("synthetic ledger failure")
        with patch.object(self.engine, "engagement", return_value=executor):
            result = self.engine.proactive(self.api.video("1"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["engagement"][0]["status"], "failed")
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.engine.s.db.execute("SELECT status FROM actions").fetchone()[0], "success")

    def test_empty_comment_keeps_same_decision_engagement_and_original_browse_quota(self):
        self.c["engagement"] = {"enabled": True, "limits": {"like": 10, "favorite": 5, "follow": 2, "coin": 2}}
        self.c["limits"]["proactive"] = 1
        self.config.write_text(json.dumps(self.c))
        self.api.credentials = {"BILI_JCT": "synthetic"}
        self.api.nav = {"money": 2}
        self.api.signed = lambda values: values
        self.api.ensure_device = lambda: None
        writes = []
        def request(path, params=None, data=None):
            if data is not None:
                writes.append((path, data))
                return {}
            if path == "/x/web-interface/archive/relation":
                return {"like": False, "favorite": False, "coin": 0}
            if path == "/x/v3/fav/folder/created/list-all":
                return {"list": [{"id": 999, "mid": "100", "title": "默认收藏夹"}]}
            if path == "/x/space/wbi/acc/relation":
                return {"relation": {"attribute": 0}}
            raise AssertionError("unexpected engagement request")
        self.api.request = request
        original = self.core
        def core(payload):
            result = original(payload)
            if payload["op"] == "decide":
                result["decision"].update(text="", engagement={
                    "like": True, "favorite": True, "follow_author": True, "coin": 1})
            return result
        self.engine.unified_request = core
        with self.engine.s.lock():
            result = self.engine.proactive(self.api.video("1"))
            self.assertEqual(result["status"], "skipped")
            self.now += self.c["proactive_gap"] + 1
            blocked = self.engine.proactive(self.api.video("2"))
        self.assertFalse(self.api.calls, "Empty comment text must never call the public text sender")
        self.assertEqual(self.core.count, 1, "Engagement must reuse one common-core decision")
        self.assertEqual(blocked, {"status": "skipped", "reasons": ["daily_limit"]})
        self.assertEqual([path for path, data in writes], [
            "/x/web-interface/archive/like", "/x/v3/fav/resource/deal",
            "/x/relation/modify", "/x/web-interface/coin/add"])
        rows = {row["kind"]: row["status"] for row in self.engine.s.db.execute("SELECT kind,status FROM actions")}
        self.assertEqual(rows, {"proactive": "skipped", "like": "success", "favorite": "success",
                               "follow": "success", "coin": "success"})
        receipts = [call["action"] for call in self.core.calls if call["op"] == "activity"]
        self.assertEqual({row["kind"] for row in receipts if row["status"] == "success"},
                         {"like", "favorite", "follow", "coin"})
