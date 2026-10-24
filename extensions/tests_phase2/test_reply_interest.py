import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from phase2.api import Model
from phase2.core import DEFAULTS, InterestSkip, Target, load_config, machine_id
from phase2.runner import Engine


class Response:
    headers = {}

    def __init__(self, answer):
        self.body = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }
        ).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


class API:
    def __init__(self, now):
        self.calls = []
        self.message = {
            "id": "reply:500",
            "time": now - 1,
            "uid": "200",
            "text": "无明确话题的评论",
            "thread": "11:700",
            "source": "500",
            "root": "500",
            "target": Target("reply", "700", 11, "200"),
        }

    def identity(self):
        return {"mid": "100", "name": "fixture"}

    def notifications(self):
        return [self.message]

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"id": "800", "url": "https://t.bilibili.com/900#reply800"}


class TestReplyInterest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = copy.deepcopy(DEFAULTS)
        self.c.update(bot_uid="100", writer_machine=machine_id(), paused=False)
        self.c["enabled"]["reply"] = True
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(self.c))
        (self.root / "model.json").write_text(json.dumps({"api_key": "synthetic_key"}))
        self.now = time.time()
        self.api = API(self.now)
        self.e = Engine(
            self.path,
            self.root / "state",
            self.root,
            api=self.api,
            now=lambda: self.now,
        )
        self.e.s.put("bound_uid", "100")
        self.e.s.put("baseline_at", self.now - 100)
        self.model = Model(self.root / "model.json", self.e.s, self.c, "合成人设")
        self.e.model = lambda: self.model

    def tearDown(self):
        self.e.s.db.close()
        self.tmp.cleanup()

    def test_low_missing_or_invalid_score_blocks_even_with_reply_text(self):
        for score in (0, 64, None, "99", True, 101, float("nan")):
            with (
                self.subTest(score=score),
                patch(
                    "phase2.api.requests.post",
                    return_value=Response(
                        {
                            "interested": True,
                            "interest_score": score,
                            "text": "不应发送",
                        }
                    ),
                ),
            ):
                with self.assertRaises(InterestSkip):
                    self.model.generate("reply", "无意义内容")
        self.assertIsNone(self.e.s.get("model_cooldown"))

    def test_uninterested_decision_blocks_even_with_high_score(self):
        with patch(
            "phase2.api.requests.post",
            return_value=Response(
                {"interested": False, "interest_score": 100, "text": "不应发送"}
            ),
        ):
            self.assertEqual(self.e.replies()[0]["status"], "skipped")
        self.assertEqual(self.api.calls, [])

    def test_threshold_boundary_replies_and_prompt_has_current_interests(self):
        with patch(
            "phase2.api.requests.post",
            return_value=Response(
                {
                    "interested": True,
                    "interest_score": 65,
                    "text": "这个游戏音乐话题很有趣。",
                }
            ),
        ) as post:
            self.assertEqual(self.e.replies()[0]["status"], "success")
            prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
            self.assertIn("、".join(self.c["interests"]), prompt)
            self.assertIn("低于65分", prompt)
        self.assertEqual(len(self.api.calls), 1)

    def test_skipped_comment_is_durable_and_never_rejudged_after_restart(self):
        with patch(
            "phase2.api.requests.post",
            return_value=Response(
                {"interested": True, "interest_score": 20, "text": "不应强行追问"}
            ),
        ) as post:
            self.assertEqual(self.e.replies()[0]["status"], "skipped")
            self.e.s.recover()
            self.assertEqual(self.e.replies(), [])
            self.assertEqual(post.call_count, 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(
            self.e.s.db.execute("select status from seen").fetchone()[0], "skipped"
        )
        self.assertEqual(
            self.e.s.db.execute("select status from actions").fetchone()[0], "skipped"
        )
        self.assertEqual(
            self.e.s.db.execute("select status from usage").fetchone()[0],
            "skipped_interest",
        )

    def test_invalid_threshold_config_is_rejected(self):
        for score in (0, 101, True, "65"):
            self.c["reply_interest_min_score"] = score
            self.path.write_text(json.dumps(self.c))
            with self.assertRaisesRegex(ValueError, "invalid_reply_interest_threshold"):
                load_config(self.path)


if __name__ == "__main__":
    unittest.main()
