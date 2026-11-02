"""public_web_search/read with the operator-configured DeepSeek official Search/Fetch transport (mock HTTP only)."""
import asyncio
import json
import logging
import time
import unittest
from unittest import mock

import httpx

from maibot_unified_core import web_access
from maibot_unified_core.voice_scope import CURRENT as VOICE_SCOPE

from .harness import BackendCase, OWNER_SESSION

REAL_CHECKED_URL = web_access.checked_url        # captured before any test patches it
REAL_FETCH_BYTES = web_access.fetch_bytes
SECRET = "SYNTHETIC_SECRET_FIXTURE"
OK_SEARCH = {"results": [
    {"title": "Python 3.14 发布说明", "url": "https://docs.python.org/3/whatsnew/3.14.html",
     "snippet": "新特性概览", "summary": "官方发布说明", "date": "2026-10-07"},
    {"title": "旧页面", "url": "http://insecure.example.com/a", "snippet": "只有HTTP"},
    {"title": "带签名", "url": "https://x.example.com/a?token=abc", "snippet": "签名链接"},
    {"title": "无日期来源", "url": "https://peps.python.org/pep-0745/", "snippet": "PEP"},
]}


class WebCase(BackendCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.policy.update(web_enabled=True)
        self.write_policy()
        self.svc.reload_policy()
        self.ctx.llm.raw = json.dumps({"allowed": True})
        self.requests, self.respond = [], lambda request: httpx.Response(200, json=OK_SEARCH)

        def handler(request):
            self.requests.append(request)
            return self.respond(request)
        self.bing = mock.Mock(side_effect=AssertionError("must not fall back to the legacy engine"))
        patches = [mock.patch.object(web_access, "TRANSPORT", httpx.MockTransport(handler)),
                   mock.patch.object(web_access, "fetch_bytes", self.bing),
                   mock.patch.object(web_access, "checked_url", lambda url: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def configure(self, **kw):
        (self.root / "web_search.json").write_text(json.dumps({"provider": "deepseek", "api_key": SECRET, "timeout_s": 6, **kw}))

    async def search(self, query="Python 3.14 新特性"):
        return await web_access.run(self.svc, "search", query, OWNER_SESSION)

    async def fetch(self, url="https://docs.python.org/3/whatsnew/3.14.html"):
        return await web_access.run(self.svc, "fetch", url, OWNER_SESSION)


class DeepSeekSearchTests(WebCase):
    async def test_request_matches_the_official_schema_and_a_fixed_origin(self):
        self.configure()
        result = await self.search()
        (request,) = self.requests
        self.assertEqual((request.method, str(request.url)), ("POST", "https://api.deepseek.com/v1/tools/search"))
        self.assertEqual(json.loads(request.content), {"query": "Python 3.14 新特性", "count": 5, "need_content": False})
        self.assertEqual(request.headers["authorization"], "Bearer " + SECRET)        # only on the wire
        self.assertTrue(result["success"])
        self.bing.assert_not_called()

    async def test_results_are_normalized_with_https_only_and_explicit_date_provenance(self):
        self.configure()
        content = (await self.search())["content"]
        self.assertEqual(content["source"], "DeepSeek official Search API")
        urls = [r["url"] for r in content["results"]]
        self.assertEqual(urls, ["https://docs.python.org/3/whatsnew/3.14.html", "https://peps.python.org/pep-0745/"])
        first, second = content["results"]
        self.assertEqual(first["published"], {"value": "2026-10-07", "source": "DeepSeek Search API date field"})
        self.assertIsNone(second["published"])                                           # no invented date
        self.assertEqual(first["summary"], "官方发布说明")
        self.assertNotIn("search_index_time", json.dumps(content))

    async def test_tool_result_and_logs_never_contain_the_key(self):
        self.configure()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        logging.getLogger().addHandler(h := Capture())
        self.addCleanup(logging.getLogger().removeHandler, h)
        for respond in (lambda r: httpx.Response(401, json={"error": SECRET}),
                        lambda r: httpx.Response(200, json={"error": {"message": SECRET}}),
                        lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom " + SECRET + str(r.url)))):
            self.respond = respond
            self.svc.state.put("web_cache:x", None)
            result = await self.search(query="q" + str(len(records)))
            self.assertFalse(result["success"])
            self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(SECRET, " ".join(records))

    async def test_provider_failures_are_reported_and_never_fall_back_to_the_legacy_engine(self):
        self.configure()
        cases = {"http401": lambda r: httpx.Response(401, json={}), "http429": lambda r: httpx.Response(429, json={}),
                 "http500": lambda r: httpx.Response(500, text="x"), "badjson": lambda r: httpx.Response(200, text="<html>"),
                 "notobj": lambda r: httpx.Response(200, json=[1]), "error": lambda r: httpx.Response(200, json={"error": "x"}),
                 "noresults": lambda r: httpx.Response(200, json={"results": "nope"})}
        for name, respond in cases.items():
            self.respond = respond
            result = await self.search(query="failure " + name)
            self.assertFalse(result["success"], name)
            self.assertIn("没有取得可用资料", result["instruction"])
        self.bing.assert_not_called()

    async def test_timeout_and_oversize_are_bounded(self):
        self.configure(timeout_s=1)
        def slow(request):
            raise httpx.ReadTimeout("slow")
        self.respond = slow
        self.assertIn("超时", (await self.search(query="slow one"))["error"])
        self.respond = lambda r: httpx.Response(200, content=b"{" + b" " * (web_access.MAX_PROVIDER_BYTES + 10))
        self.assertIn("过大", (await self.search(query="big one"))["error"])

    async def test_query_length_and_entry_guards_are_unchanged(self):
        self.configure()
        self.assertFalse((await self.search("长" * 129))["success"])                       # DeepSeek limit: 128
        self.assertFalse((await self.search("我的手机号 13800000000"))["success"])          # privacy guard before any request
        self.assertFalse((await self.search("你怎么看总统选举"))["success"])                  # fixed topic guard
        self.assertFalse((await web_access.run(self.svc, "search", "python", "qq:private:stranger"))["success"])
        self.assertEqual(self.requests, [])

    async def test_screening_and_usage_still_apply(self):
        self.configure()
        self.ctx.llm.raw = json.dumps({"allowed": False})
        self.assertEqual((await self.search())["error"], "fixed_topic_restriction")
        self.ctx.llm.raw = json.dumps({"allowed": True})
        day = time.strftime("%Y-%m-%d")
        for key in [k for k in (self.svc.state.db.execute("SELECT k FROM meta WHERE k LIKE 'web_usage:%'").fetchall())]:
            self.svc.state.put(key["k"], 60)
        self.assertIn("60次", (await self.search(query="another"))["error"])

    async def test_cache_is_provider_aware_and_does_not_replay_old_engine_results(self):
        # An entry written by the old (provider-less) key format must never be served.
        import hashlib
        old_key = "web_cache:" + hashlib.sha256(("search\nPython 3.14 新特性").encode()).hexdigest()
        self.svc.state.put(old_key, {"at": time.time(), "result": {"success": True, "content": {"results": "BING JUNK"}}})
        self.configure()
        first = await self.search()
        self.assertNotIn("BING JUNK", json.dumps(first))
        again = await self.search()
        self.assertEqual(again, first)
        self.assertEqual(len(self.requests), 1)                                           # second answer from the new cache
        self.respond = lambda r: httpx.Response(500, text="x")
        legacy_key = "web_cache:" + hashlib.sha256(("bing-rss\nsearch\nPython 3.14 新特性").encode()).hexdigest()
        self.assertIsNone(self.svc.state.get(legacy_key))                                 # provider-specific namespaces


class ConfigTests(WebCase):
    async def test_missing_config_keeps_the_legacy_default(self):
        rss = '<rss><channel><item><title>t</title><link>https://example.org/x</link><description>d</description></item></channel></rss>'
        self.bing.side_effect = None
        self.bing.return_value = ("https://www.bing.com/search", rss)
        with mock.patch.object(web_access, "checked_url", lambda url: None):
            result = await self.search()
        self.assertTrue(result["success"])
        self.assertEqual(result["content"]["source"], "Bing public RSS")
        self.assertEqual(self.requests, [])

    async def test_configured_but_unusable_is_an_error_never_a_silent_bing(self):
        for body in ("{not json", json.dumps({"provider": "bing", "api_key": "x"}), json.dumps({"provider": "deepseek"}),
                     json.dumps({"provider": "deepseek", "api_key": "  "}), json.dumps([1])):
            (self.root / "web_search.json").write_text(body)
            result = await self.search(query="cfg " + body[:5])
            self.assertFalse(result["success"])
            self.assertIn("联网服务", result["error"])
        self.bing.assert_not_called()
        self.assertEqual(self.requests, [])

    async def test_timeout_is_clamped_and_nothing_model_supplied_selects_an_endpoint(self):
        self.configure(timeout_s=999)
        self.assertEqual(web_access.provider_config(self.svc)["timeout_s"], 10.0)
        self.configure(base_url="https://evil.example", endpoint="https://evil.example/x")
        await self.search(query="endpoint test")
        self.assertEqual(str(self.requests[-1].url), "https://api.deepseek.com/v1/tools/search")


PAGE = "<html><head><title>Welcome</title></head><body><h1>Welcome to Python.org</h1><p>" + "正文" * 6000 + "</p></body></html>"


class DirectReaderTests(WebCase):
    """public_web_read always uses the original guarded direct reader, whatever search provider is configured."""

    def page(self, body=PAGE, final="https://www.python.org/"):
        self.bing.side_effect = None
        self.bing.return_value = (final, body)

    async def test_configured_deepseek_does_not_touch_page_reads_and_no_credential_reaches_the_target(self):
        self.configure()
        self.page()
        result = await self.fetch("https://www.python.org/")
        self.assertEqual(self.requests, [])                                              # no DeepSeek request at all
        self.bing.assert_called_once_with("https://www.python.org/")                      # the direct reader, url only
        content = result["content"]
        self.assertEqual((content["url"], content["title"], content["truncated"]), ("https://www.python.org/", "Welcome", True))
        self.assertEqual(len(content["content"]), 8000)
        self.assertEqual(content["source"], "Direct public HTTPS reader (pinned public DNS)")   # truthful source
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))

    async def test_a_broken_search_config_does_not_break_page_reads_but_search_still_errors(self):
        (self.root / "web_search.json").write_text("{not json")
        self.page()
        self.assertTrue((await self.fetch("https://www.python.org/"))["success"])
        self.bing.side_effect = AssertionError("must not fall back to the legacy engine")
        self.assertFalse((await self.search(query="still broken"))["success"])

    async def test_cache_keys_distinguish_direct_fetch_deepseek_search_and_old_legacy_keys(self):
        import hashlib
        self.configure()
        url = "https://www.python.org/"
        old = "web_cache:" + hashlib.sha256(("fetch\n" + url).encode()).hexdigest()
        self.svc.state.put(old, {"at": time.time(), "result": {"success": True, "content": {"content": "OLD JUNK"}}})
        self.page()
        first = await self.fetch(url)
        self.assertNotIn("OLD JUNK", json.dumps(first))
        new = "web_cache:" + hashlib.sha256(("direct-public-https\nfetch\n" + url).encode()).hexdigest()
        self.assertEqual(self.svc.state.get(new)["result"], first)
        deepseek = "web_cache:" + hashlib.sha256(("deepseek\nfetch\n" + url).encode()).hexdigest()
        self.assertIsNone(self.svc.state.get(deepseek))
        self.bing.side_effect = AssertionError("served from cache")
        self.assertEqual(await self.fetch(url), first)

    async def test_url_policy_and_guards_still_apply(self):
        self.configure()
        with mock.patch.object(web_access, "checked_url", REAL_CHECKED_URL), \
                mock.patch.object(web_access, "fetch_bytes", web_access.__dict__.get("_ORIGINAL_FETCH_BYTES", None) or REAL_FETCH_BYTES):
            for url in ("https://127.0.0.1/admin", "http://docs.python.org/x", "https://u:p@docs.python.org/x",
                        "https://docs.python.org/x?token=1"):
                self.assertFalse((await self.fetch(url))["success"], url)
        self.assertFalse((await web_access.run(self.svc, "fetch", "https://www.python.org/", "qq:private:stranger"))["success"])
        self.assertEqual(self.requests, [])

    async def test_topic_block_and_semantic_screening_apply_to_page_text(self):
        self.configure()
        self.page("<html><body><h1>t</h1><p>讨论总统选举的文章</p></body></html>")
        self.assertEqual((await self.fetch("https://example.org/a"))["error"], "fixed_topic_restriction")
        self.page()
        self.ctx.llm.raw = json.dumps({"allowed": False})
        self.assertEqual((await self.fetch("https://www.python.org/"))["error"], "fixed_topic_restriction")


class ImageImportCompatibilityTests(unittest.TestCase):
    def test_media_import_still_uses_the_unchanged_fetch_helpers(self):
        from maibot_unified_core import media
        self.assertIs(media.fetch_bytes, web_access.fetch_bytes)


class VoiceBudgetTests(WebCase):
    def scope(self, remaining, **extra):
        return VOICE_SCOPE.set({"session": OWNER_SESSION, "message": ("t", "e", "p"), "deadline": time.monotonic() + 99,
                                "turn_deadline": time.monotonic() + remaining, **extra})

    async def run_in_scope(self, remaining, fn, **extra):
        token = self.scope(remaining, **extra)
        try:
            return await fn()
        finally:
            VOICE_SCOPE.reset(token)

    async def test_no_new_request_when_only_reply_time_remains(self):
        self.configure()
        result = await self.run_in_scope(9.0, self.search)                               # 9 s left - 8 s reserve < 2 s
        self.assertFalse(result["success"])
        self.assertIn("剩余时间不足", result["error"])
        self.assertIn("不要说没有网络", result["instruction"])
        self.assertEqual(self.requests, [])

    async def test_request_timeout_is_clamped_to_the_spare_time(self):
        self.configure(timeout_s=6)
        seen = {}
        real = web_access.deepseek_search

        async def spy(cfg, query, timeout):
            seen["timeout"] = timeout
            return await real(cfg, query, timeout)
        with mock.patch.object(web_access, "deepseek_search", spy):
            await self.run_in_scope(12.0, self.search)                                    # spare = 4 s
        self.assertLess(seen["timeout"], 4.01)
        self.assertGreater(seen["timeout"], 3.5)
        seen.clear()
        with mock.patch.object(web_access, "deepseek_search", spy):
            await self.run_in_scope(60.0, lambda: self.search("另一个问题"))               # plenty: configured 6 s
        self.assertEqual(seen["timeout"], 6.0)

    async def test_slow_provider_inside_the_budget_returns_a_useful_error(self):
        self.configure(timeout_s=6)
        async def hang(*a, **k):
            await asyncio.sleep(30)
        with mock.patch.object(web_access, "deepseek_search", hang):
            started = time.monotonic()
            result = await self.run_in_scope(10.5, self.search)                           # spare 2.5 s
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertFalse(result["success"])
        self.assertIn("超时", result["error"])
        self.assertIn("没查到", result["instruction"])

    async def test_repeat_is_served_from_the_provider_aware_cache_even_when_only_reply_time_remains(self):
        self.configure()
        first = await self.run_in_scope(60.0, lambda: self.search("A查询"))
        again = await self.run_in_scope(60.0, lambda: self.search("A查询"))
        self.assertTrue(first["success"])
        self.assertEqual(again, first)
        late = await self.run_in_scope(1.0, lambda: self.search("A查询"))                # finishing phase: cache needs no time
        self.assertEqual(late, first)
        self.assertEqual(len(self.requests), 1)
        fresh = await self.run_in_scope(1.0, lambda: self.search("B查询"))               # but a NEW lookup is still refused
        self.assertIn("剩余时间不足", fresh["error"])
        self.assertEqual(len(self.requests), 1)

    async def test_authorization_and_privacy_checks_come_before_the_cache(self):
        self.configure()
        await self.run_in_scope(60.0, lambda: self.search("A查询"))
        token = VOICE_SCOPE.set({"session": "qq:private:stranger", "message": ("t", "e", "p"), "deadline": time.monotonic() + 99,
                                 "turn_deadline": time.monotonic() + 60})
        try:
            denied = await web_access.run(self.svc, "search", "A查询", "qq:private:stranger")
        finally:
            VOICE_SCOPE.reset(token)
        self.assertFalse(denied["success"])
        self.assertFalse((await self.run_in_scope(60.0, lambda: self.search("手机号 13800000000")))["success"])

    async def test_many_distinct_lookups_are_allowed_while_time_remains(self):
        self.configure()
        for i in range(8):                                                               # no per-turn count cap
            self.assertTrue((await self.run_in_scope(60.0, lambda i=i: self.search(f"不同查询{i}")))["success"])
        self.assertEqual(len(self.requests), 8)

    async def test_slow_semantic_screening_returns_before_the_reply_reserve_and_fails_closed(self):
        self.configure()
        async def slow_screen(**kw):
            await asyncio.sleep(30)
            return {"success": True, "response": json.dumps({"allowed": True})}
        self.ctx.llm.generate = slow_screen
        started = time.monotonic()
        result = await self.run_in_scope(11.0, self.search)                              # spare = 3 s for network + screening
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.6)
        self.assertFalse(result["success"])
        self.assertIn("超时", result["error"])
        self.assertNotIn("content", result)                                              # unreviewed content never exposed
        self.assertNotIn("Python 3.14", json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(self.requests), 1)                                          # network happened, screen did not finish
        # nothing unscreened was cached: the same lookup is fresh again (and screened) once the screen works
        async def fast_screen(**kw):
            return {"success": True, "response": json.dumps({"allowed": True})}
        self.ctx.llm.generate = fast_screen
        self.assertTrue((await self.run_in_scope(60.0, self.search))["success"])
        self.assertEqual(len(self.requests), 2)

    async def test_network_plus_screening_share_one_absolute_deadline(self):
        self.configure()
        def slowish(request):
            time.sleep(0.0)
            return httpx.Response(200, json=OK_SEARCH)
        self.respond = slowish
        async def screen(**kw):
            await asyncio.sleep(1.2)
            return {"success": True, "response": json.dumps({"allowed": True})}
        self.ctx.llm.generate = screen
        real = web_access.deepseek_search

        async def network(cfg, query, timeout):
            await asyncio.sleep(1.2)                                                     # network 1.2 s + screen 1.2 s > 2.2 s spare
            return await real(cfg, query, timeout)
        with mock.patch.object(web_access, "deepseek_search", network):
            started = time.monotonic()
            result = await self.run_in_scope(10.2, self.search)                          # spare 2.2 s
        self.assertLess(time.monotonic() - started, 2.7)
        self.assertFalse(result["success"])

    async def test_slow_direct_page_read_is_bounded_by_the_voice_deadline(self):
        self.configure()
        def slow_reader(url, image=False):
            time.sleep(3.5)
            return url, PAGE
        self.bing.side_effect = slow_reader
        started = time.monotonic()
        result = await self.run_in_scope(10.5, self.fetch)                               # spare 2.5 s
        self.assertLess(time.monotonic() - started, 3.2)
        self.assertFalse(result["success"])
        self.assertIn("超时", result["error"])
        self.assertNotIn("content", result)
        self.assertEqual(self.requests, [])

    async def test_ordinary_qq_calls_have_no_voice_limits(self):
        self.configure()
        for _ in range(2):
            self.assertTrue((await self.search("同一个QQ问题"))["success"])
        for i in range(6):
            self.assertTrue((await self.search(f"QQ查询{i}"))["success"])
