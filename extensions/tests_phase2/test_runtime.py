import copy, json, tempfile, unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock
from phase2.core import (
    DEFAULTS,
    TZ,
    Store,
    Sender,
    Target,
    Denied,
    PlatformError,
    machine_id,
    future,
    active,
    dynamic_target,
    safe_text,
)
from phase2.runner import Engine, interest


class FakeAPI:
    def __init__(self):
        self.uid = "100"
        self.calls = []
        self.failure = None
        self.found = None
        self.inbox = []

    def identity(self):
        return {"mid": self.uid, "name": "test"}

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure
        return {"id": str(len(self.calls) + 8000), "url": "https://t.bilibili.com/8001"}

    def reconcile(self, *args, **kwargs):
        return self.found

    def notifications(self):
        return self.inbox

    def private_messages(self, owner):
        return []


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.c = copy.deepcopy(DEFAULTS)
        self.c.update(
            bot_uid="100", owner_uid="200", writer_machine=machine_id(), paused=False
        )
        self.c["enabled"] = {k: True for k in self.c["enabled"]}
        self.save()
        self.ts = datetime(2026, 10, 24, 12, tzinfo=TZ).timestamp()
        self.s = Store(self.root / "state")
        self.s.put("bound_uid", "100")
        self.s.put("baseline_at", self.ts - 10)
        self.api = FakeAPI()
        self.sender = Sender(self.s, self.path, self.api, lambda: self.ts)
        self.t = Target(
            "video", "1", 1, "300", "大模型示例", "https://www.bilibili.com/video/av1"
        )

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.c))

    def send(self, key="a", kind="proactive", target=None, **kw):
        return self.sender.deliver(
            kind, target or self.t, key, lambda: "这个例子很清楚。", **kw
        )

    def test_success_committed_before_memory_failure_and_restart(self):
        def fail(text):
            raise RuntimeError("optional embedding is absent")

        self.assertEqual(self.send(memory=fail)["status"], "success")
        self.s.recover()
        with self.assertRaises(Denied):
            self.send()
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(
            self.s.db.execute("select remote_id from actions").fetchone()[0], "8001"
        )

    def test_unknown_delivery_never_retried(self):
        self.api.failure = TimeoutError()
        self.assertEqual(self.send()["status"], "unknown")
        self.s.recover()
        with self.assertRaises(Denied):
            self.send()
        self.assertEqual(len(self.api.calls), 1)

    def test_unknown_reconciled(self):
        self.api.failure = TimeoutError()
        self.api.found = {"id": "999", "url": "https://t.bilibili.com/999"}
        self.assertEqual(self.send()["status"], "success")
        self.assertEqual(
            self.s.db.execute("select remote_id from actions").fetchone()[0], "999"
        )

    def test_uid_mismatch_stops_before_generation(self):
        self.api.uid = "999"
        g = Mock(return_value="text")
        with self.assertRaises(Denied):
            self.sender.deliver("proactive", self.t, "a", g)
        g.assert_not_called()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.s.get("pause_reason"), "uid_mismatch")

    def test_platform_rejection_pauses(self):
        self.api.failure = PlatformError(-412)
        self.assertEqual(self.send()["status"], "paused")
        self.assertTrue(self.s.get("pause_reason"))

    def test_duplicate_video_shared_dynamic_same_target(self):
        self.send()
        self.ts += 2000
        t = Target("dynamic", "1", 1, "300")
        with self.assertRaisesRegex(Denied, "duplicate_target"):
            self.send("b", target=t)

    def test_spacing(self):
        self.send()
        t = Target("video", "2", 1, "400")
        with self.assertRaisesRegex(Denied, "minimum_gap"):
            self.send("b", target=t)

    def test_author_daily_and_persistence(self):
        for i in range(2):
            self.ts += 2000
            self.s.reserve(
                self.c, "proactive", Target("video", str(i), 1, "300"), self.ts, str(i)
            )
        self.ts += 2000
        self.s.recover()
        with self.assertRaisesRegex(Denied, "author_limit"):
            self.s.reserve(
                self.c, "proactive", Target("dynamic", "3", 17, "300"), self.ts, "c"
            )

    def test_shared_proactive_quota(self):
        for i in range(6):
            self.ts += 2000
            self.s.reserve(
                self.c,
                "proactive",
                Target(
                    "video" if i % 2 else "dynamic",
                    str(i + 1),
                    1 if i % 2 else 17,
                    str(i + 400),
                ),
                self.ts,
                str(i),
            )
        self.ts += 2000
        with self.assertRaisesRegex(Denied, "daily_limit"):
            self.s.reserve(self.c, "proactive", self.t, self.ts, "g")

    def test_thread_limit(self):
        for i in range(3):
            self.s.reserve(self.c, "reply", self.t, self.ts, str(i), "thread")
        with self.assertRaisesRegex(Denied, "thread_limit"):
            self.s.reserve(self.c, "reply", self.t, self.ts, "d", "thread")

    def test_dm_owner_only(self):
        with self.assertRaisesRegex(Denied, "owner_only"):
            self.send("d", kind="dm", recipient="555")
        self.assertEqual(
            self.send("e", kind="dm", recipient="200")["status"], "success"
        )

    def test_pause_file(self):
        (self.s.root / "PAUSE").touch()
        with self.assertRaisesRegex(Denied, "paused"):
            self.send()

    def test_pause_during_model(self):
        def gen():
            (self.s.root / "PAUSE").touch()
            return "你好"

        self.assertEqual(
            self.sender.deliver("proactive", self.t, "a", gen)["status"], "paused"
        )
        self.assertFalse(self.api.calls)

    def test_feature_disabled_and_foreign_host(self):
        self.c["enabled"]["proactive"] = False
        self.save()
        with self.assertRaisesRegex(Denied, "feature_disabled"):
            self.send()
        self.c["enabled"]["proactive"] = True
        self.c["writer_machine"] = "elsewhere"
        self.save()
        with self.assertRaisesRegex(Denied, "wrong_writer"):
            self.send()

    def test_night_no_send(self):
        self.ts = datetime(2026, 10, 24, 23, 1, tzinfo=TZ).timestamp()
        with self.assertRaisesRegex(Denied, "outside_active"):
            self.send()

    def test_interrupted_sending_unknown_pending_failed(self):
        self.s.reserve(self.c, "proactive", self.t, self.ts, "a")
        self.s.update("a", status="sending")
        self.s.recover()
        self.assertEqual(
            self.s.db.execute("select status from actions").fetchone()[0], "unknown"
        )

    def test_single_instance(self):
        other = Store(self.s.root)
        with self.s.lock():
            with self.assertRaises(Denied):
                with other.lock():
                    pass
        other.db.close()

    def test_stop_thread(self):
        self.s.stop_thread("thread")
        with self.assertRaisesRegex(Denied, "thread_stopped"):
            self.send(kind="reply", thread="thread")

    def test_no_cross_target_identical_text(self):
        self.send()
        self.ts += 2000
        self.assertEqual(
            self.send("b", target=Target("video", "2", 1, "400"))["status"], "skipped"
        )
        self.assertEqual(len(self.api.calls), 1)

    def test_plans_future_after_missed_restart(self):
        e = Engine(self.path, self.s.root, self.root, api=self.api, now=lambda: self.ts)
        e.s.put("next_proactive", self.ts - 5000)
        e.s.put("next_dynamic", self.ts - 5000)
        e.initialize_plans()
        self.assertGreater(e.s.get("next_proactive"), self.ts)
        self.assertGreater(e.s.get("next_dynamic"), self.ts)
        self.assertFalse(self.api.calls)
        e.s.db.close()

    def test_baseline_old_messages_not_replied(self):
        (self.root / "account.json").write_text(
            json.dumps({"user_selected_uid": "100"})
        )
        self.s.db.execute("delete from meta where k='baseline_at'")
        self.api.inbox = [{"id": "reply:1", "time": self.ts - 1}]
        e = Engine(self.path, self.s.root, self.root, api=self.api, now=lambda: self.ts)
        self.assertEqual(e.baseline()["count"], 1)
        self.assertEqual(e.baseline()["status"], "existing_baseline_preserved")
        self.assertFalse(self.api.calls)
        e.s.db.close()

    def test_futures_stay_inside_activity(self):
        for h in (0, 8, 9, 22, 23):
            ts = datetime(2026, 10, 24, h, 59, tzinfo=TZ).timestamp()
            for kind in ("dynamic", "proactive"):
                for _ in range(20):
                    f = future(self.c, ts, kind)
                    self.assertGreater(f, ts)
                    self.assertTrue(active(self.c, f))


class TestTargets(unittest.TestCase):
    def item(self, typ=11, oid="678", id="123", kind="DYNAMIC_TYPE_DRAW"):
        return {
            "id_str": id,
            "type": kind,
            "basic": {"comment_type": typ, "comment_id_str": oid, "rid_str": "999999"},
            "modules": {
                "module_author": {"mid": 50},
                "module_dynamic": {
                    "desc": {"text": "草莓布丁"},
                    "major": {
                        "draw": {"items": [{"src": "https://i0.hdslb.com/a.jpg"}]}
                    },
                },
                "module_stat": {"comment": {"forbidden": False}},
            },
        }

    def test_draw_uses_platform_comment_object_not_dynamic_id(self):
        t = dynamic_target(self.item())
        self.assertEqual(t.key, "11:678")
        self.assertEqual(t.images, ["https://i0.hdslb.com/a.jpg"])
        self.assertTrue(interest(DEFAULTS, t.text))

    def test_video_share_canonicalizes_to_video(self):
        t = dynamic_target(self.item(1, "678", kind="DYNAMIC_TYPE_AV"))
        self.assertEqual(t.kind, "video")
        self.assertEqual(t.key, Target("video", "678", 1, "50").key)

    def test_image_only_game_dynamic_keeps_public_topic(self):
        i = self.item()
        i["modules"]["module_dynamic"]["desc"] = None
        i["modules"]["module_dynamic"]["additional"] = {
            "common": {"sub_type": "game", "title": "原神"}
        }
        t = dynamic_target(i)
        self.assertTrue(interest(DEFAULTS, t.text))
        self.assertEqual(t.key, "11:678")

    def test_forward_keeps_own_comment_target(self):
        i = self.item(17, "123", kind="DYNAMIC_TYPE_FORWARD")
        i["orig"] = self.item()
        t = dynamic_target(i)
        self.assertEqual(t.key, "17:123")
        self.assertIn("转发的公开原文", t.text)

    def test_missing_and_closed_targets_skip(self):
        i = self.item()
        i["basic"].pop("comment_id_str")
        self.assertIsNone(dynamic_target(i))
        i = self.item()
        i["modules"]["module_stat"]["comment"]["forbidden"] = True
        self.assertIsNone(dynamic_target(i))

    def test_uninterested_skip(self):
        self.assertFalse(interest(DEFAULTS, "水泥价格报价单"))

    def test_generated_links_and_mentions_rejected(self):
        for s in ("@所有人", "访问https://example.com", ""):
            with self.assertRaises(Denied):
                safe_text(s)


if __name__ == "__main__":
    unittest.main()
