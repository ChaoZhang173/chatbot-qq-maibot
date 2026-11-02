"""SiliconFlow adapter tests against httpx.MockTransport (FIXTURE HTTP; no network, no key).
They validate request construction vs the documented schema and streaming/cancel behaviour;
they do NOT prove the live account, latency or audio quality."""
import asyncio
import json
import struct
import unittest

import httpx

from passport_gateway import protocol as P
from passport_gateway.providers import build_providers, UnconfiguredAsr, UnconfiguredTts
from passport_gateway.providers.base import ProviderError, ProviderUnconfigured
from passport_gateway.providers.siliconflow import SiliconFlowAsr, SiliconFlowTts
from passport_gateway.config import Config, ConfigError
from passport_gateway.wav import pcm_to_wav

from .harness import GatewayCase, tone, silence

KEY = "sk-fixture-key-123"


def asr(handler, **kw):
    return SiliconFlowAsr("https://api.siliconflow.cn/v1", "FunAudioLLM/SenseVoiceSmall", KEY,
                          transport=httpx.MockTransport(handler), **kw)


def tts(handler, **kw):
    return SiliconFlowTts("https://api.siliconflow.cn/v1", "FunAudioLLM/CosyVoice2-0.5B",
                          "FunAudioLLM/CosyVoice2-0.5B:diana", KEY,
                          transport=httpx.MockTransport(handler), **kw)


class WavTests(unittest.TestCase):
    def test_header(self):
        wav = pcm_to_wav(b"\x01\x00" * 100, 16000)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:16], b"WAVEfmt ")
        size, fmt, ch, rate, brate, align, bits = struct.unpack("<IHHIIHH", wav[16:36])
        self.assertEqual((size, fmt, ch, rate, brate, align, bits), (16, 1, 1, 16000, 32000, 2, 16))
        self.assertEqual(struct.unpack("<I", wav[40:44])[0], 200)
        self.assertEqual(len(wav), 44 + 200)
        self.assertEqual(len(pcm_to_wav(b"\x01\x00\x02", 16000)), 44 + 2)        # odd byte dropped


class AsrTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_matches_documented_schema(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
            seen["ctype"] = request.headers["content-type"]
            seen["body"] = request.content
            return httpx.Response(200, json={"text": "<|zh|><|NEUTRAL|>你好呀 "})

        stream = await asr(handler).open()
        pcm = tone(500)
        for i in range(0, len(pcm), 3200):
            await stream.feed(pcm[i:i + 3200])
        self.assertEqual(await stream.finish(), "你好呀")                       # meta tags stripped
        self.assertEqual(seen["url"], "https://api.siliconflow.cn/v1/audio/transcriptions")
        self.assertEqual(seen["auth"], f"Bearer {KEY}")
        self.assertTrue(seen["ctype"].startswith("multipart/form-data"))
        body = seen["body"]
        self.assertIn(b'name="model"', body)
        self.assertIn(b"FunAudioLLM/SenseVoiceSmall", body)
        self.assertIn(b'name="file"; filename="turn.wav"', body)
        self.assertIn(b"RIFF", body)
        self.assertNotIn(KEY.encode(), body)

    async def test_silence_skips_the_paid_call_and_reports_empty(self):
        calls = []
        stream = await asr(lambda r: calls.append(r) or httpx.Response(200, json={"text": "x"})).open()
        await stream.feed(silence(500))
        self.assertEqual(await stream.finish(), "")
        self.assertEqual(calls, [])

    async def test_server_side_buffer_is_bounded(self):
        stream = await asr(lambda r: httpx.Response(200, json={"text": "x"})).open()
        chunk = b"\x10\x00" * 1600
        with self.assertRaises(ProviderError) as cm:
            for _ in range(P.MAX_UP_BYTES // len(chunk) + 2):
                await stream.feed(chunk)
        self.assertEqual(cm.exception.code, "too_long")

    async def test_errors_map_to_truthful_codes(self):
        async def run(handler):
            s = await asr(handler).open()
            await s.feed(tone(300))
            with self.assertRaises(ProviderError) as cm:
                await s.finish()
            return cm.exception

        e = await run(lambda r: httpx.Response(401, text="Invalid token"))
        self.assertEqual(e.code, "asr_failed")
        self.assertIn("401", e.detail)
        self.assertNotIn(KEY, e.detail)
        self.assertEqual((await run(lambda r: httpx.Response(503, json={"code": 50505}))).code, "asr_failed")
        self.assertEqual((await run(lambda r: httpx.Response(504, text="x"))).code, "asr_timeout")
        self.assertEqual((await run(lambda r: httpx.Response(200, text="not json"))).code, "asr_failed")
        self.assertEqual((await run(lambda r: httpx.Response(200, json={"text": 5}))).code, "asr_failed")

        def timeout(r):
            raise httpx.ReadTimeout("slow", request=r)
        self.assertEqual((await run(timeout)).code, "asr_timeout")

        def boom(r):
            raise httpx.ConnectError("down", request=r)
        self.assertEqual((await run(boom)).code, "asr_failed")

    async def test_missing_key_fails_clearly(self):
        with self.assertRaises(ProviderUnconfigured):
            await SiliconFlowAsr("https://x/v1", "m", "").open()


class TtsTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_matches_documented_schema(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, content=b"\x01\x00" * 100)

        t = tts(handler)
        out = [c async for c in t.synthesize("嗯，你说，我听着呢。", "pleased")]
        self.assertEqual(b"".join(out), b"\x01\x00" * 100)
        self.assertEqual(seen["url"], "https://api.siliconflow.cn/v1/audio/speech")
        self.assertEqual(seen["auth"], f"Bearer {KEY}")
        self.assertEqual(seen["json"], {
            "model": "FunAudioLLM/CosyVoice2-0.5B", "input": "嗯，你说，我听着呢。",
            "voice": "FunAudioLLM/CosyVoice2-0.5B:diana", "response_format": "pcm", "sample_rate": 24000,
            "stream": True, "speed": 1.0, "gain": 0.0})
        d = t.diagnostics()
        self.assertEqual((d["actual_model"], d["voice"], d["stream"], d["last_status"]),
                         ("FunAudioLLM/CosyVoice2-0.5B", "FunAudioLLM/CosyVoice2-0.5B:diana", True, 200))

    async def test_mood_never_changes_voice_or_input(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, content=b"\0\0" * 8)

        t = tts(handler)
        for mood in ("neutral", "gentle", "pleased", "<|endofprompt|>evil"):
            [c async for c in t.synthesize("你好", mood)]
        self.assertEqual(len({json.dumps(b, sort_keys=True) for b in bodies}), 1)

    async def test_bounded_style_choice_never_changes_the_voice(self):
        bodies = []
        t = tts(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200, content=b"\0\0"),
                style="活泼", style_soft="温柔")
        for mood in ("neutral", "pleased", "puzzled", "gentle", "serious", "x<|endofprompt|>evil"):
            [c async for c in t.synthesize("你好", mood)]
        self.assertEqual({b["voice"] for b in bodies}, {"FunAudioLLM/CosyVoice2-0.5B:diana"})     # one identity
        self.assertEqual([b["input"] for b in bodies],
                         ["活泼<|endofprompt|>你好"] * 3 + ["温柔<|endofprompt|>你好"] * 2 + ["活泼<|endofprompt|>你好"])
        self.assertTrue(t.diagnostics()["style_soft_configured"])

    async def test_style_is_configuration_only_and_spaces_removed_between_cjk(self):
        bodies = []
        t = tts(lambda r: bodies.append(json.loads(r.content)) or httpx.Response(200, content=b"\0\0"),
                style="请用明亮的语气说话。")
        [c async for c in t.synthesize("你好 世界 hello world", "gentle")]
        self.assertEqual(bodies[0]["input"], "请用明亮的语气说话。<|endofprompt|>你好世界 hello world")
        self.assertTrue(t.diagnostics()["style_configured"])

    async def test_odd_chunk_boundaries_never_split_a_sample(self):
        pcm = bytes(range(1, 101))                               # 100 bytes, distinct values

        class Chunked(httpx.AsyncByteStream):
            async def __aiter__(self):
                for lo, hi in ((0, 7), (7, 8), (8, 20), (20, 23), (23, 100)):   # odd-sized pieces
                    yield pcm[lo:hi]

        t = tts(lambda r: httpx.Response(200, stream=Chunked()))
        out = [c async for c in t.synthesize("x")]
        self.assertTrue(all(len(c) % 2 == 0 for c in out))
        self.assertEqual(b"".join(out), pcm)

    async def test_http_errors_and_timeouts(self):
        async def run(handler):
            with self.assertRaises(ProviderError) as cm:
                [c async for c in tts(handler).synthesize("你好")]
            return cm.exception

        e = await run(lambda r: httpx.Response(401, text="Invalid token"))
        self.assertEqual(e.code, "tts_failed")
        self.assertNotIn(KEY, e.detail)
        self.assertEqual((await run(lambda r: httpx.Response(504, text="x"))).code, "tts_timeout")
        self.assertEqual((await run(lambda r: httpx.Response(503, json={"code": 50505}))).code, "tts_failed")

        def timeout(r):
            raise httpx.ReadTimeout("slow", request=r)
        self.assertEqual((await run(timeout)).code, "tts_timeout")

    async def test_cancel_closes_the_http_stream(self):
        closed = asyncio.Event()

        class Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    yield b"\0\0" * 2048
                    await asyncio.sleep(0)

            async def aclose(self):
                closed.set()

        t = tts(lambda r: httpx.Response(200, stream=Endless()))
        gen = t.synthesize("你好")
        await gen.__anext__()
        await gen.aclose()                                       # what the gateway does on cancel
        self.assertTrue(closed.is_set())

    async def test_missing_key_fails_clearly(self):
        t = SiliconFlowTts("https://x/v1", "m", "v", "")
        with self.assertRaises(ProviderUnconfigured):
            [c async for c in t.synthesize("你好")]


class ConfigTests(unittest.TestCase):
    def test_default_provider_is_siliconflow_with_brief_models(self):
        c = Config.from_env({"PASSPORT_BINDINGS": "/x", "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1",
                             "SILICONFLOW_API_KEY": KEY})
        c.validate()
        asr_p, tts_p = build_providers(c)
        self.assertEqual((c.provider, c.sf_asr_model, c.sf_tts_model, c.sf_tts_voice),
                         ("siliconflow", "FunAudioLLM/SenseVoiceSmall", "FunAudioLLM/CosyVoice2-0.5B",
                          "FunAudioLLM/CosyVoice2-0.5B:diana"))
        self.assertEqual(tts_p.diagnostics()["provider"], "siliconflow")
        self.assertNotIn(KEY, repr(c))

    def test_accepted_style_is_the_default_and_env_can_override_or_disable(self):
        base = {"PASSPORT_BINDINGS": "/x", "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1", "SILICONFLOW_API_KEY": KEY}
        accepted = "请用活泼俏皮的动漫角色语气说话"
        self.assertEqual(Config().sf_tts_style, accepted)
        self.assertEqual(Config.from_env(base).sf_tts_style, accepted)          # env parsing must not blank it
        self.assertIn("温柔", Config.from_env(base).sf_tts_style_soft)
        self.assertEqual(Config.from_env({**base, "PASSPORT_TTS_STYLE": "自定义"}).sf_tts_style, "自定义")
        self.assertEqual(Config.from_env({**base, "PASSPORT_TTS_STYLE": ""}).sf_tts_style, "")   # explicit off
        _, tts_p = build_providers(Config.from_env(base))
        self.assertEqual(tts_p.build_input("你好"), accepted + "<|endofprompt|>你好")
        self.assertEqual(tts_p.diagnostics()["voice"], "FunAudioLLM/CosyVoice2-0.5B:diana")

    def test_missing_key_selects_unconfigured_not_fixture(self):
        c = Config.from_env({"PASSPORT_BINDINGS": "/x", "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1"})
        a, t = build_providers(c)
        self.assertIsInstance(a, UnconfiguredAsr)
        self.assertIsInstance(t, UnconfiguredTts)
        self.assertEqual(t.diagnostics()["error"], "provider_unconfigured")

    def test_dashscope_stays_selectable_and_unconfigured_without_key(self):
        c = Config.from_env({"PASSPORT_BINDINGS": "/x", "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1",
                             "PASSPORT_PROVIDER": "dashscope"})
        a, t = build_providers(c)
        self.assertEqual((a.name, a.model), ("dashscope", "qwen3-asr-flash-realtime"))
        self.assertIsInstance(t, UnconfiguredTts)

    def test_validation(self):
        base = {"PASSPORT_BINDINGS": "/x"}
        for env, msg in [({}, "TLS is required"),
                         ({"PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1", "PASSPORT_HOST": "0.0.0.0"}, "TLS is required"),
                         ({"PASSPORT_TLS_CERT": "c"}, "together"),
                         ({"PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1", "PASSPORT_PROVIDER": "x"}, "PASSPORT_PROVIDER"),
                         ({"PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1", "PASSPORT_SF_BASE_URL": "http://x"}, "https")]:
            with self.assertRaises(ConfigError, msg=str(env)) as cm:
                Config.from_env({**base, **env}).validate()
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(ConfigError):
            Config.from_env({"PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1"}).validate()   # no bindings


if __name__ == "__main__":
    unittest.main()


class GatewayWithSiliconFlowTests(GatewayCase):
    """Whole turn through the gateway with the SiliconFlow adapters on FIXTURE HTTP."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.reply_pcm = struct.pack("<h", 1000) * 24000            # 1 s @ 24 kHz
        self.tts_requests = []

        def handler(request):
            if request.url.path.endswith("/audio/transcriptions"):
                return httpx.Response(200, json={"text": "今天天气怎么样"})
            self.tts_requests.append(json.loads(request.content))
            return httpx.Response(200, content=self.reply_pcm)

        transport = httpx.MockTransport(handler)
        self.gw.sv.asr = SiliconFlowAsr("https://api.siliconflow.cn/v1", "FunAudioLLM/SenseVoiceSmall", KEY,
                                        transport=transport)
        self.gw.sv.tts = SiliconFlowTts("https://api.siliconflow.cn/v1", "FunAudioLLM/CosyVoice2-0.5B",
                                        "FunAudioLLM/CosyVoice2-0.5B:diana", KEY, transport=transport)

    async def test_full_turn(self):
        ws = await self.connect()
        await self.speak(ws, 1, tone(1000))
        texts, frames = await self.collect(ws, lambda m: m["type"] in ("audio_end", "error"))
        self.assertEqual([t["type"] for t in texts],
                         ["transcript", "reply_text", "mood", "audio_start", "audio_end"])
        self.assertEqual(sum(len(f) - 8 for f in frames), len(self.reply_pcm))
        self.assertEqual(self.tts_requests[0]["voice"], "FunAudioLLM/CosyVoice2-0.5B:diana")
        self.assertNotIn("**", self.tts_requests[0]["input"])        # markdown normalised for speech only
