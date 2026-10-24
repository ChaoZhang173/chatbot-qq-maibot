"""External API transport is mocked; no account activity occurs in these tests."""

import copy
import json
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from phase2.core import DEFAULTS, Denied, PlatformError, Store, Target, TZ, machine_id
from phase2.engagement import CAPS, EngagementExecutor, choices, policy, used


class API:
    def __init__(self):
        self.credentials = {"BILI_JCT": "synthetic-only"}
        self.nav = {"money": 10}
        self.reads, self.writes = [], []
        self.relation = {"like": False, "favorite": False, "coin": 0}
        self.attribute = 0
        self.folders = [{"id": 456, "mid": 100, "title": "默认收藏夹"}]
        self.write_failure = None
        self.hook = None

    def signed(self, params):
        return params

    def ensure_device(self):pass

    def request(self, path, params=None, data=None):
        if data is not None:
            self.writes.append((path, copy.deepcopy(data)))
            if self.write_failure:
                raise self.write_failure
            return {}
        self.reads.append((path, params))
        if self.hook:
            self.hook()
        if path.endswith("acc/relation"):
            return {"relation": {"attribute": self.attribute}}
        if path.endswith("archive/relation"):
            return copy.deepcopy(self.relation)
        if path.endswith("list-all"):
            return {"list": copy.deepcopy(self.folders)}
        raise AssertionError("unexpected API endpoint")


class EngagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.s.db.executescript("""
            CREATE TABLE unified_events(event_id TEXT PRIMARY KEY,payload TEXT);
            CREATE TABLE action_context(action_id TEXT PRIMARY KEY,event_json TEXT,
                persona_version TEXT,source_ids TEXT);
        """)
        self.c = copy.deepcopy(DEFAULTS)
        self.c.update(bot_uid="100", writer_machine=machine_id(), paused=False,
                      engagement={"enabled": True, "limits": dict(CAPS)})
        self.clock = datetime(2026, 10, 24, 12, tzinfo=TZ).timestamp()
        self.s.put("bound_uid", "100")
        self.s.put("baseline_at", self.clock - 10)
        self.api = API()
        self.identity_calls = 0
        self.identity_hook = None
        self.e = self.executor()

    def executor(self):
        return EngagementExecutor(self.s, lambda: copy.deepcopy(self.c), self.api, self.identity, lambda: self.clock)

    def identity(self, config):
        self.identity_calls += 1
        if self.identity_hook:
            self.identity_hook()
        return {"mid": "100"}

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def read(self, aid="1", author="300", **event_patch):
        target = Target("video", aid, 1, author, "实际取得的视频标题和简介", "https://www.bilibili.com/video/av" + aid,
                        [], "BVfixture" + aid, "title_description")
        event = {"event_id": "read:" + aid, "platform": "bilibili", "bot_account_id": "100",
                 "event_type": "content_read", "origin": "platform_content", "conversation_id": target.key,
                 "actor_id": author, "source_id": target.source_id, "material_evidence": target.evidence,
                 "content": target.text, "sharing_scope": "public", "occurred_at": self.clock, **event_patch}
        self.s.db.execute("INSERT OR REPLACE INTO unified_events VALUES(?,?)", (event["event_id"], json.dumps(event)))
        return target, event["event_id"]

    def decision(self, **actions):
        return {"interested": True, "interest_score": 80, "engagement": {
            "like": False, "favorite": False, "follow_author": False, "coin": 0, **actions}}

    def execute(self, target, event_id, decision=None):
        with self.s.lock():
            return self.e.execute(target, decision or self.decision(like=True), event_id, "persona-4", [event_id])

    def test_four_actions_use_exact_bounded_payloads_and_persist_provenance(self):
        target, eid = self.read()
        result = self.execute(target, eid, self.decision(like=True, favorite=True, follow_author=True, coin=1))
        self.assertEqual([r["status"] for r in result], ["success"] * 4)
        self.assertEqual(used(self.s, "100", self.clock), {k: 1 for k in CAPS})
        payloads = dict(self.api.writes)
        self.assertEqual(payloads["/x/web-interface/coin/add"], {"aid": "1", "multiply": 1, "select_like": 0, "csrf": "synthetic-only"})
        self.assertEqual(payloads["/x/relation/modify"]["act"], 1)
        self.assertEqual(payloads["/x/v3/fav/resource/deal"]["add_media_ids"], "456")
        self.assertEqual(payloads["/x/v3/fav/resource/deal"]["del_media_ids"], "")
        self.assertEqual(len(self.s.db.execute("SELECT * FROM engagement_actions").fetchall()), 4)
        contexts = self.s.db.execute("SELECT persona_version,source_ids FROM action_context").fetchall()
        self.assertTrue(all(r["persona_version"] == "persona-4" and json.loads(r["source_ids"]) == [eid] for r in contexts))

    def test_disabled_and_no_decision_are_noops(self):
        self.assertFalse(policy({})["enabled"])
        target, eid = self.read()
        self.c.pop("engagement")
        self.assertEqual(self.execute(target, eid)[0]["reason"], "engagement_disabled")
        self.assertEqual(self.execute(target, eid, {"interested": True}), [])
        self.assertEqual(self.api.reads + self.api.writes, [])

    def test_strict_decision_rejects_extra_targets_wrong_types_and_low_interest(self):
        cases = [self.decision(coin=True), self.decision(coin=2), self.decision(like="yes"),
                 self.decision(target="999"), {**self.decision(like=True), "interested": False},
                 {**self.decision(like=True), "interest_score": float("nan")},
                 {**self.decision(like=True), "interest_score": 64}]
        for decision in cases:
            with self.assertRaises(Denied):
                choices(decision)
        for limits in ({**CAPS, "coin": 3}, {**CAPS, "like": True}, {"like": 1}):
            with self.assertRaises(Denied):
                policy({"engagement": {"enabled": True, "limits": limits}})

    def test_exact_recent_read_evidence_is_required(self):
        for change in ({"origin": "bot_self"}, {"sharing_scope": "private"}, {"actor_id": "999"},
                       {"content": "different video"}, {"source_id": "forged"},
                       {"occurred_at": self.clock - 1801}, {"bot_account_id": "999"}):
            target, eid = self.read(**change)
            with self.assertRaisesRegex(Denied, "evidence"):
                self.execute(target, eid)
        target, eid = self.read(author="100")
        with self.assertRaisesRegex(Denied, "nonself"):
            self.execute(target, eid)
        self.assertEqual(self.api.writes, [])

    def test_writer_account_baseline_pause_machine_and_hours_guards(self):
        target, eid = self.read()
        result = self.e.execute(target, self.decision(like=True), eid, "4", [eid])
        self.assertEqual(result[0]["reason"], "engagement_single_writer_lock_required")
        for key, value, expected in (("paused", True, "paused"), ("writer_machine", "wrong", "wrong_writer_machine"),
                                     ("bot_uid", "999", "evidence")):
            old = self.c[key]; self.c[key] = value
            if key == "bot_uid":
                with self.assertRaises(Denied): self.execute(target, eid)
            else:
                self.assertEqual(self.execute(target, eid)[0]["reason"], expected)
            self.c[key] = old
        self.s.put("baseline_at", None)
        self.assertEqual(self.execute(target, eid)[0]["reason"], "baseline_required")
        self.s.put("baseline_at", self.clock)
        self.clock = datetime(2026, 10, 24, 23, tzinfo=TZ).timestamp()
        target, eid = self.read()
        self.assertEqual(self.execute(target, eid)[0]["reason"], "outside_active_hours")
        self.assertEqual(self.api.writes, [])

    def test_already_present_states_never_toggle_or_spend(self):
        self.api.relation = {"like": True, "favorite": True, "coin": 1}
        self.api.attribute = 6
        target, eid = self.read()
        result = self.execute(target, eid, self.decision(like=True, favorite=True, follow_author=True, coin=1))
        self.assertTrue(all(r["reason"] == "already_present" for r in result))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(sum(used(self.s, "100", self.clock).values()), 0)

    def test_coins_skip_insufficient_balance_and_favorite_requires_existing_default(self):
        target, eid = self.read()
        self.api.nav["money"] = 0.5
        self.assertIn("balance", self.execute(target, eid, self.decision(coin=1))[0]["reason"])
        self.api.folders = [{"id": 7, "mid": 100, "title": "other folder"}]
        self.assertIn("folder_unverified", self.execute(target, eid, self.decision(favorite=True))[0]["reason"])
        self.api.folders = [{"id": 7, "mid": 999, "title": "默认收藏夹"}]
        self.assertIn("folder_unverified", self.execute(target, eid, self.decision(favorite=True))[0]["reason"])
        self.assertEqual(self.api.writes, [])

    def test_unknown_never_resends_and_explicit_reconciliation_is_read_only(self):
        target, eid = self.read()
        self.api.write_failure = TimeoutError("synthetic")
        first = self.execute(target, eid)[0]
        self.assertEqual(first["status"], "unknown")
        self.s.db.close(); self.s = Store(self.tmp.name); self.e = self.executor()
        with self.s.lock(): self.e.recover()
        self.api.write_failure = None
        self.assertEqual(self.execute(target, eid)[0]["status"], "unknown")
        with self.s.lock():
            self.assertEqual(self.e.reconcile(first["action_id"])["status"], "unknown")
            self.api.relation["like"] = True
            self.assertEqual(self.e.reconcile(first["action_id"])["result_stage"], "account_state_reconciled")
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(used(self.s, "100", self.clock)["like"], 1)

    def test_restart_converts_sending_to_unknown_and_keeps_reserved_budget(self):
        target, eid = self.read()
        key = self.execute(target, eid, self.decision(coin=1))[0]["action_id"]
        self.s.update(key, status="sending", remote_id="")
        self.s.db.close(); self.s = Store(self.tmp.name); self.e = self.executor()
        with self.s.lock(): self.e.recover()
        self.assertEqual(self.execute(target, eid, self.decision(coin=1))[0]["status"], "unknown")
        self.assertEqual(used(self.s, "100", self.clock)["coin"], 1)
        self.assertEqual(len(self.api.writes), 1)

    def test_china_daily_quota_persists_but_resets_only_for_new_targets_next_day(self):
        self.c["engagement"]["limits"]["like"] = 1
        target, eid = self.read("1")
        self.execute(target, eid)
        target2, eid2 = self.read("2")
        self.assertEqual(self.execute(target2, eid2)[0]["reason"], "engagement_daily_limit")
        self.s.db.close(); self.s = Store(self.tmp.name); self.e = self.executor()
        self.assertEqual(self.execute(target2, eid2)[0]["reason"], "engagement_daily_limit")
        self.clock += 86400
        target2, eid2 = self.read("2")
        self.assertEqual(self.execute(target2, eid2)[0]["status"], "success")
        target, eid = self.read("1")
        self.assertEqual(self.execute(target, eid)[0]["reason"], "already_attempted")
        self.assertEqual(len(self.api.writes), 2)

    def test_follow_dedupes_by_author_across_videos(self):
        target, eid = self.read("1")
        self.execute(target, eid, self.decision(follow_author=True))
        target, eid = self.read("2")
        self.assertEqual(self.execute(target, eid, self.decision(follow_author=True))[0]["reason"], "already_attempted")
        self.assertEqual(len(self.api.writes), 1)

    def test_policy_revocation_and_date_change_during_read_block_dispatch(self):
        target, eid = self.read()
        self.api.hook = lambda: self.c["engagement"].update(enabled=False)
        self.assertEqual(self.execute(target, eid)[0]["reason"], "engagement_policy_or_day_changed")
        self.assertEqual(self.api.writes, [])
        self.assertEqual(used(self.s, "100", self.clock)["like"], 1)

    def test_policy_revoked_during_final_identity_is_rechecked(self):
        target, eid = self.read()
        def revoke():
            if self.identity_calls == 2: self.c["engagement"]["enabled"] = False
        self.identity_hook = revoke
        self.assertEqual(self.execute(target, eid)[0]["reason"], "engagement_policy_or_day_changed")
        self.assertEqual(self.api.writes, [])

    def test_risk_response_pauses_and_never_runs_remaining_actions(self):
        target, eid = self.read()
        self.api.write_failure = PlatformError(-412)
        result = self.execute(target, eid, self.decision(like=True, coin=1))
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.s.get("pause_reason"), "engagement_platform_-412")
        self.assertEqual(used(self.s, "100", self.clock)["like"], 1)


if __name__ == "__main__":
    unittest.main()
