"""Optional DashScope adapter against a local FIXTURE websocket server that speaks the documented
event schema. Verifies adapter logic only (flow, fallback reporting); not the live service."""
import base64
import json
import unittest

import websockets

from passport_gateway.providers import dashscope_schema as S
from passport_gateway.providers.base import ProviderError
from passport_gateway.providers.dashscope import DashScopeAsr, DashScopeTts


class FakeDashScope:
    def __init__(self, reject_instructions=False, bad_audio=False):
        self.reject_instructions = reject_instructions
        self.bad_audio = bad_audio
        self.models, self.headers, self.sessions = [], [], []

    async def handler(self, ws):
        self.models.append(ws.request.path)
        self.headers.append(ws.request.headers)
        await ws.send(json.dumps({"type": S.SESSION_CREATED, "session": {"id": "s"}}))
        appended = 0
        async for raw in ws:
            ev = json.loads(raw)
            t = ev["type"]
            if t == "session.update":
                self.sessions.append(ev["session"])
                if self.reject_instructions and "instructions" in ev["session"]:
                    await ws.send(json.dumps({"type": "error", "error": {"code": "invalid_parameter",
                                  "message": "instructions is not supported by this model"}}))
                    continue
                await ws.send(json.dumps({"type": S.SESSION_UPDATED}))
            elif t == "input_audio_buffer.append":
                appended += len(base64.b64decode(ev["audio"]))
            elif t == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": S.ASR_TRANSCRIPT_DONE, "transcript": f"heard {appended}"}))
            elif t == "input_text_buffer.commit":
                for _ in range(3):
                    await ws.send(json.dumps({"type": S.TTS_AUDIO_DELTA,
                                  "delta": "!!!" if self.bad_audio else base64.b64encode(b"\x01\x00" * 50).decode()}))
                await ws.send(json.dumps({"type": S.TTS_RESPONSE_DONE}))
            elif t == "session.finish":
                await ws.send(json.dumps({"type": S.SESSION_FINISHED}))


class DashScopeTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, **kw):
        self.fake = FakeDashScope(**kw)
        self.server = await websockets.serve(self.fake.handler, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close)
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/api-ws/v1/realtime"

    async def _close(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_asr_flow_and_headers(self):
        url = await self.serve()
        stream = await DashScopeAsr(url, "qwen3-asr-flash-realtime", "k-secret").open()
        await stream.feed(b"\x01\x00" * 160)
        await stream.feed(b"\x01\x00" * 160)
        self.assertEqual(await stream.finish(), "heard 640")
        await stream.close()
        self.assertIn("model=qwen3-asr-flash-realtime", self.fake.models[0])
        self.assertEqual(self.fake.headers[0]["Authorization"], "Bearer k-secret")
        self.assertEqual(self.fake.headers[0]["OpenAI-Beta"], "realtime=v1")
        self.assertEqual(self.fake.sessions[0]["turn_detection"], None)
        self.assertEqual(self.fake.sessions[0]["sample_rate"], 16000)

    async def test_tts_streams_and_reports_actual_model(self):
        url = await self.serve()
        tts = DashScopeTts(url, "qwen3-tts-instruct-flash-realtime", "Cherry", "k", "基础指令", "qwen3-tts-flash-realtime")
        out = [c async for c in tts.synthesize("你好", "gentle")]
        self.assertEqual(len(b"".join(out)), 300)
        self.assertIn("instructions", self.fake.sessions[0])
        self.assertIn("温柔", self.fake.sessions[0]["instructions"])
        self.assertEqual(tts.diagnostics()["actual_model"], "qwen3-tts-instruct-flash-realtime")

    async def test_instruct_rejection_falls_back_visibly_not_silently(self):
        url = await self.serve(reject_instructions=True)
        tts = DashScopeTts(url, "qwen3-tts-instruct-flash-realtime", "Cherry", "k", "基础指令", "qwen3-tts-flash-realtime")
        out = [c async for c in tts.synthesize("你好", "neutral")]
        self.assertTrue(out)
        d = tts.diagnostics()
        self.assertEqual((d["configured_model"], d["actual_model"], d["fallback_count"]),
                         ("qwen3-tts-instruct-flash-realtime", "qwen3-tts-flash-realtime", 1))
        self.assertNotIn("instructions", self.fake.sessions[-1])

    async def test_no_fallback_when_disabled_and_bad_audio_is_an_error(self):
        url = await self.serve(reject_instructions=True)
        tts = DashScopeTts(url, "qwen3-tts-instruct-flash-realtime", "Cherry", "k", "x", "qwen3-tts-flash-realtime", False)
        with self.assertRaises(ProviderError):
            [c async for c in tts.synthesize("你好", "neutral")]
        url = await self.serve(bad_audio=True)
        tts = DashScopeTts(url, "qwen3-tts-flash-realtime", "Cherry", "k", "", "")
        with self.assertRaises(ProviderError) as cm:
            [c async for c in tts.synthesize("你好", "neutral")]
        self.assertEqual(cm.exception.code, "tts_failed")

    async def test_missing_key(self):
        from passport_gateway.providers.base import ProviderUnconfigured
        with self.assertRaises(ProviderUnconfigured):
            await DashScopeAsr("ws://127.0.0.1:1/x", "m", "").open()


if __name__ == "__main__":
    unittest.main()
