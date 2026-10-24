"""Actual notification parser + runner contract, synthetic APIs and no network."""
import copy
import json
from unittest import TestCase

import test_bili_unified as fixtures

from phase2.api import Bili


def notice(source, stamp, *, feed="at", root=0, typ=1, oid=700, text="@机器人 这个光谱怎么理解？"):
    return {"id": 900000 + source, "user": {"mid": 300}, feed + "_time": stamp,
            "item": {"type": "reply", "business": "评论", "business_id": typ,
                     "subject_id": oid, "source_id": source, "root_id": root, "target_id": 123,
                     "source_content": text, "hide_reply_button": False,
                     "uri": "https://www.bilibili.com/video/av700?tracking=removed",
                     "at_details": [{"mid": 100}]}}


class IncomingMentionTests(TestCase):
    setUp = fixtures.TestBiliUnified.setUp
    tearDown = fixtures.TestBiliUnified.tearDown
    new_engine = fixtures.TestBiliUnified.new_engine

    def install(self, replies=(), mentions=()):
        self.reader = Bili.__new__(Bili)
        self.reader.credentials, self.reader.nav = {"DEDE_USER_ID": "100"}, {"mid": 100}
        feeds = {"reply": list(replies), "at": list(mentions)}
        self.reads = []
        def request(path, params=None):
            self.reads.append((path, params))
            return {"items": copy.deepcopy(feeds[path.rsplit("/", 1)[-1]]), "cursor": {"is_end": True}}
        self.reader.request = request
        self.api.notifications = self.reader.notifications

    def test_at_root_and_nested_comments_use_native_reply_identity(self):
        self.install(replies=[notice(33, self.now - 1, feed="reply", root=22)],
                     mentions=[notice(33, self.now - 1, root=22), notice(44, self.now - 1)])
        messages = self.reader.notifications()
        self.assertEqual([row["id"] for row in messages], ["reply:33", "reply:44"])
        self.assertEqual([(row["source"], row["root"], row["target"].key) for row in messages],
                         [("33", "22", "1:700"), ("44", "44", "1:700")])
        self.assertEqual(messages[1]["notification_kind"], "comment_mention")
        self.assertEqual(messages[1]["target"].url, "https://www.bilibili.com/video/av700")
        self.assertEqual([path for path, params in self.reads], ["/x/msgfeed/reply", "/x/msgfeed/at"])

    def test_dynamic_body_danmu_wrong_account_and_malformed_notifications_rejected(self):
        valid = notice(44, self.now - 1)
        invalid = []
        for field, value in (("type", "dynamic"), ("type", "album"), ("type", "danmu"),
                             ("business_id", 2), ("business_id", "1"), ("source_id", 0),
                             ("subject_id", 0), ("root_id", "bad"), ("at_details", []),
                             ("at_details", [{"mid": 200}]), ("hide_reply_button", True)):
            item = copy.deepcopy(valid)
            item["item"][field] = value
            invalid.append(item)
        for field in ("type", "root_id"):
            missing = copy.deepcopy(valid)
            del missing["item"][field]
            invalid.append(missing)
        invalid.append({**valid, "at_time": float("nan")})
        invalid.append({**valid, "user": {"mid": ""}})
        self.install(mentions=invalid)
        self.assertEqual(self.reader.notifications(), [])
        self.assertEqual(self.engine.replies(), [])
        self.assertFalse(self.api.calls)
        self.assertEqual(self.core.count, 0)

    def test_bounded_pagination_maps_response_time_to_feed_parameter(self):
        self.install()
        def request(path, params=None):
            self.reads.append((path, params))
            feed = path.rsplit("/", 1)[-1]
            index = len([p for p, values in self.reads if p == path])
            return {"items": [notice(40 + index, self.now - index, feed=feed)],
                    "cursor": {"is_end": False, "id": 100 + index, "time": 1000 - index}}
        self.reader.request = request
        self.reader.notifications()
        self.assertEqual(len(self.reads), 6)
        self.assertEqual(self.reads[1][1], {"ps": 20, "id": "101", "reply_time": "999"})
        self.assertEqual(self.reads[4][1], {"ps": 20, "id": "101", "at_time": "999"})

    def test_same_comment_in_both_feeds_sends_once_then_uses_existing_reply_quota(self):
        self.c["limits"]["reply"] = 1
        self.config.write_text(json.dumps(self.c))
        self.install(replies=[notice(33, self.now - 2, feed="reply", root=22)],
                     mentions=[notice(33, self.now - 2, root=22), notice(44, self.now - 1)])
        self.assertEqual(self.engine.replies()[0]["status"], "success")
        self.assertEqual(self.engine.replies(), [])
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0][0], "reply")
        self.assertEqual(self.api.calls[0][3]["root"], "22")
        self.assertEqual(self.api.calls[0][3]["parent"], "33")
        self.assertEqual(self.core.count, 1)
        self.assertEqual(self.engine.s.db.execute("SELECT kind FROM actions").fetchone()[0], "reply")
        self.assertEqual(self.engine.s.db.execute("SELECT status FROM seen WHERE id='reply:44'").fetchone()[0],
                         "skip_daily_limit")

    def test_existing_baseline_and_seen_comment_are_preserved(self):
        baseline = self.engine.s.get("baseline_at")
        self.install(mentions=[notice(44, baseline - 1), notice(55, self.now - 1)])
        self.engine.s.db.execute("INSERT INTO seen VALUES(?,?,?)", ("reply:55", self.now - 1, "success"))
        (self.root / "account.json").write_text(json.dumps({"user_selected_uid": "100"}))
        self.assertEqual(self.engine.baseline(), {"status": "existing_baseline_preserved"})
        self.assertEqual(self.engine.s.get("baseline_at"), baseline)
        self.assertEqual(self.engine.replies(), [])
        self.assertEqual(self.engine.s.db.execute("SELECT status FROM seen WHERE id='reply:44'").fetchone()[0],
                         "old_or_self")
        self.assertFalse(self.api.calls)

    def test_mention_uses_existing_interest_threshold_pause_and_stop_guards(self):
        self.install(mentions=[notice(44, self.now - 1)])
        self.core.score = 64
        self.assertEqual(self.engine.replies()[0]["status"], "skipped")
        self.assertFalse(self.api.calls)
        self.assertEqual(self.core.count, 1)
        self.install(mentions=[notice(55, self.now - 1, text="@机器人 不要再回复我")])
        self.assertEqual(self.engine.replies(), [])
        self.assertEqual(self.core.count, 1)
        self.assertTrue(self.engine.s.db.execute("SELECT 1 FROM stopped WHERE thread='1:700:55'").fetchone())
        self.install(mentions=[notice(66, self.now - 1)])
        self.engine.s.put("pause_reason", "synthetic_pause")
        self.assertEqual(self.engine.replies(), [])
        self.assertEqual(self.core.count, 1)
        self.assertFalse(self.api.calls)
