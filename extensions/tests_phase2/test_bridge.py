import copy, hashlib, json, os, tempfile, unittest
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from phase2.core import DEFAULTS, TZ, Denied, machine_id
from phase2.runner import Engine
from phase2.bridge import Bridge, public_snapshot
from maibot_bilibili_bridge.client import enqueue, receipt


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.timeout = False

    def identity(self):
        return {"mid": "100", "name": "fixture"}

    def send(self, kind, target, text, **kwargs):
        self.calls.append(text)
        if self.timeout:
            raise TimeoutError()
        return {"id": str(1000 + len(self.calls)), "url": "https://t.bilibili.com/1000"}

    def reconcile(self, *a, **kw):
        return None


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ts = datetime(2026, 10, 24, 12, tzinfo=TZ).timestamp()
        c = copy.deepcopy(DEFAULTS)
        c.update(
            bot_uid="100", owner_uid="200", writer_machine=machine_id(), paused=False
        )
        c["enabled"] = {k: True for k in c["enabled"]}
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(c))
        self.api = FakeAPI()
        self.e = Engine(
            self.config,
            self.root / "state",
            self.root,
            api=self.api,
            now=lambda: self.ts,
        )
        self.e.s.put("bound_uid", "100")
        self.e.s.put("baseline_at", self.ts - 1)
        self.e.model = lambda: SimpleNamespace(
            generate=lambda kind, material, **kw: material["public_draft"]
        )
        self.p = {
            "enabled": True,
            "root": str(self.root / "bridge"),
            "allowed_streams": ["group-a"],
            "client_uid": os.getuid(),
        }
        self.policy = self.root / "bridge.json"
        self.policy.write_text(json.dumps(self.p))
        for name in ("requests", "results"):
            (self.root / "bridge" / name).mkdir(parents=True)
        self.b = Bridge(self.e)
        self.b.export(force=True)

    def tearDown(self):
        self.e.s.db.close()
        self.tmp.cleanup()

    def request(self, draft="今天想吃草莓布丁", **kw):
        r = {
            "stream_id": "group-a",
            "expected_uid": "100",
            "draft": draft,
            "created_at": self.ts,
        }
        r.update(kw)
        r["request_id"] = hashlib.sha256(
            (r["stream_id"] + "\n" + r["draft"]).encode()
        ).hexdigest()
        return r

    def test_publication_uses_single_writer_and_durable_receipt(self):
        r = self.request()
        self.assertEqual(self.b.deliver(r, self.p)["status"], "success")
        self.assertEqual(self.b.deliver(r, self.p)["status"], "success")
        self.assertEqual(len(self.api.calls), 1)

    def test_qq_and_scheduled_dynamic_share_ten_per_day(self):
        for i in range(9):
            self.e.sender.deliver(
                "dynamic", None, "schedule:" + str(i), lambda i=i: "公开想法" + str(i)
            )
        self.b.deliver(self.request(), self.p)
        with self.assertRaisesRegex(Denied, "daily_limit"):
            self.b.deliver(self.request("另一份公开想法"), self.p)
        self.assertEqual(len(self.api.calls), 10)

    def test_wrong_group_uid_or_expired_request_never_calls_model(self):
        for change in (
            {"stream_id": "other"},
            {"expected_uid": "200"},
            {"created_at": self.ts - 601},
        ):
            with self.assertRaises(Denied):
                self.b.deliver(self.request(**change), self.p)
        self.assertFalse(self.api.calls)

    def test_private_drafts_and_links_rejected(self):
        for text in (
            "手机号13812345678",
            "群号123456",
            "邮箱hello@example.com",
            "请看https://example.com",
            "我的密码是秘密",
        ):
            with self.assertRaises(Denied):
                self.b.deliver(self.request(text), self.p)
        self.assertFalse(self.api.calls)

    def test_pause_stops_qq_publication(self):
        (self.e.s.root / "PAUSE").touch()
        with self.assertRaisesRegex(Denied, "paused"):
            self.b.deliver(self.request(), self.p)
        self.assertFalse(self.api.calls)

    def test_unknown_qq_send_is_not_replayed(self):
        self.api.timeout = True
        r = self.request()
        self.assertEqual(self.b.deliver(r, self.p)["status"], "unknown")
        self.e.s.recover()
        self.assertEqual(self.b.deliver(r, self.p)["status"], "unknown")
        self.assertEqual(len(self.api.calls), 1)

    def test_queue_end_to_end_and_repeat_submission(self):
        from unittest.mock import patch

        with patch("maibot_bilibili_bridge.client.time.time", return_value=self.ts):
            rid = enqueue("今天想吃草莓布丁", "group-a", self.root / "bridge")
            self.assertEqual(
                enqueue("今天想吃草莓布丁", "group-a", self.root / "bridge"), rid
            )
        self.assertEqual(self.b.process_one()["status"], "success")
        self.assertEqual(receipt(rid, self.root / "bridge")["status"], "success")
        self.assertFalse(list((self.root / "bridge" / "requests").glob("*.json")))

    def test_private_messages_excluded_from_public_memory(self):
        self.e.sender.deliver(
            "dm", None, "private", lambda: "仅限私信的内容", recipient="200"
        )
        self.b.deliver(self.request(), self.p)
        data = public_snapshot(self.e, self.p)
        self.assertNotIn("仅限私信", json.dumps(data, ensure_ascii=False))
        self.assertEqual(len(data["public_activities"]), 1)

    def test_request_file_symlink_cannot_read_private_files(self):
        name = "a" * 64 + ".json"
        (self.root / "bridge" / "requests" / name).symlink_to(self.config)
        result = self.b.process_one()
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(self.api.calls)
        self.assertTrue(self.config.exists())

    def test_disabled_bridge_rejects(self):
        p = dict(self.p, enabled=False)
        with self.assertRaisesRegex(Denied, "bridge_disabled"):
            self.b.deliver(self.request(), p)


if __name__ == "__main__":
    unittest.main()
