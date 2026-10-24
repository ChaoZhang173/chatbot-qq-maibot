import copy, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from phase2.api import Bili, Model, public_image, post_url
from phase2.core import DEFAULTS, Store, Denied, Target


class Response:
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield json.dumps(self.payload).encode()


class APIPaths(unittest.TestCase):
    def setUp(self):
        self.b = Bili.__new__(Bili)
        self.b.credentials = {"BILI_JCT": "synthetic_csrf"}
        self.b.nav = {"mid": 100}
        self.calls = []

    def request(self, path, params=None, **kw):
        self.calls.append((path, params, kw))
        return {"rpid_str": "700", "dyn_id_str": "900", "msg_key": "500"}

    def test_reply_uses_root_parent_and_actual_object(self):
        self.b.request = self.request
        t = Target("reply", "678", 11, "200")
        self.assertEqual(
            self.b.send("reply", t, "你好", root="22", parent="33")["id"], "700"
        )
        form = self.calls[0][2]["data"]
        self.assertEqual(
            (form["oid"], form["type"], form["root"], form["parent"]),
            ("678", 11, "22", "33"),
        )

    def test_dynamic_receipt_retained(self):
        self.b.request = self.request
        r = self.b.send("dynamic", None, "今天吃布丁")
        self.assertEqual(r["url"], "https://t.bilibili.com/900")
        self.assertEqual(self.calls[0][2]["body"]["dyn_req"]["scene"], 1)

    def test_opus_dynamic_reads_body_and_images_with_actual_comment_target(self):
        def req(path, params=None, **kw):
            self.assertEqual(params["features"], "itemOpusStyle")
            return {
                "item": {
                    "id_str": "900",
                    "type": "DYNAMIC_TYPE_DRAW",
                    "basic": {"comment_id_str": "678", "comment_type": 11},
                    "modules": {
                        "module_author": {"mid": "200"},
                        "module_dynamic": {
                            "desc": None,
                            "major": {
                                "opus": {
                                    "summary": {"text": "游戏音乐已更新"},
                                    "pics": [
                                        {"url": "https://i0.hdslb.com/fixture.png"}
                                    ],
                                }
                            },
                        },
                    },
                }
            }

        self.b.request = req
        t = self.b.dynamic("900")
        self.assertEqual(t.text, "游戏音乐已更新")
        self.assertEqual(t.images, ["https://i0.hdslb.com/fixture.png"])
        self.assertEqual(t.key, "11:678")

    def test_video_fetch_is_metadata_only_without_interest(self):
        def req(path, params=None, **kw):
            self.calls.append(path)
            return {
                "aid": 123,
                "bvid": "BV1test00000",
                "cid": 999,
                "owner": {"mid": 200},
                "title": "人工智能",
                "desc": "介绍上下文",
            }

        self.b.request = req
        t = self.b.video("123")
        self.assertEqual(self.calls, ["/x/web-interface/view"])
        self.assertEqual(t.evidence, "title_description")

    def test_notification_reply_target_fields(self):
        self.b.request = lambda *a, **k: {
            "items": [
                {
                    "reply_time": 1000,
                    "user": {"mid": 200},
                    "item": {
                        "source_id": 33,
                        "subject_id": 678,
                        "business_id": 11,
                        "root_id": 22,
                        "source_content": "测试",
                        "uri": "https://www.bilibili.com/opus/123#reply33",
                    },
                }
            ],
            "cursor": {"is_end": True},
        }
        m = self.b.notifications()[0]
        self.assertEqual(
            (m["root"], m["source"], m["target"].key), ("22", "33", "11:678")
        )
        self.assertEqual(m["target"].url, "https://www.bilibili.com/opus/123")

    def test_reply_links_do_not_retain_untrusted_hosts_or_query_values(self):
        self.assertEqual(
            post_url("https://t.bilibili.com/123?tracking=x#reply4"),
            "https://t.bilibili.com/123",
        )
        for uri in (
            "https://bilibili.com.attacker.test/opus/123",
            "https://u:p@www.bilibili.com/opus/123",
            "http://t.bilibili.com/123",
            "https://www.bilibili.com/account/123",
        ):
            self.assertEqual(post_url(uri), "")

    def test_untrusted_image_hosts_never_requested(self):
        with patch("phase2.api.requests.get") as get:
            for u in [
                "http://example.com/a",
                "https://127.0.0.1/a",
                "https://hdslb.com.attacker.test/a",
                "https://u:p@i0.hdslb.com/a",
            ]:
                with self.assertRaises(ValueError):
                    public_image(u)
            get.assert_not_called()

    def test_platform_http_cdn_link_is_upgraded_before_network(self):
        with patch("phase2.api.requests.get", side_effect=ValueError("fixture")) as get:
            with self.assertRaises(ValueError):
                public_image("http://i0.hdslb.com/a.png")
            self.assertEqual(get.call_args.args[0], "https://i0.hdslb.com/a.png")

    def test_model_receives_image_and_uses_only_official_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(json.dumps({"api_key": "synthetic_key"}))
            s = Store(root / "state")
            c = copy.deepcopy(DEFAULTS)
            model = Model(root / "model.json", s, c, "合成人设")
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"interested": True, "text": "布丁的颜色好漂亮。"}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 22, "completion_tokens": 8},
            }
            with (
                patch(
                    "phase2.api.public_image",
                    return_value="data:image/jpeg;base64,c3ludGhldGlj",
                ),
                patch(
                    "phase2.api.requests.post", return_value=Response(payload)
                ) as post,
            ):
                self.assertIn(
                    "布丁",
                    model.generate("proactive", "草莓布丁", ["https://i0.hdslb.com/a"]),
                )
                args = post.call_args
                self.assertEqual(
                    args.args[0], "https://api.deepseek.com/v1/chat/completions"
                )
                content = args.kwargs["json"]["messages"][1]["content"]
                self.assertEqual(content[1]["type"], "image_url")
                self.assertNotIn("synthetic_key", json.dumps(args.kwargs["json"]))
            self.assertEqual(s.summary()["usage"]["calls"], 1)
            s.db.close()

    def test_model_failure_cools_down_no_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "model.json").write_text(json.dumps({"api_key": "synthetic"}))
            s = Store(root / "state")
            m = Model(root / "model.json", s, copy.deepcopy(DEFAULTS), "test")
            with patch("phase2.api.requests.post", side_effect=TimeoutError()) as post:
                for _ in range(2):
                    with self.assertRaises(Denied):
                        m.generate("reply", "hello")
                self.assertEqual(post.call_count, 1)
            s.db.close()


if __name__ == "__main__":
    unittest.main()
