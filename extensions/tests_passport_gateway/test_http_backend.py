"""Portable-backend contracts, cancellation, and full WebSocket/HTTP integration."""
import asyncio
import json
import unittest

import httpx

from passport_gateway.backend import BackendClient, BackendError, MAX_PACKET
from passport_gateway.config import Config, ConfigError
from passport_gateway.http_backend import HttpBackendClient, build_backend
from passport_gateway.smoke import run_smoke
from passport_gateway import protocol as P
from .harness import DEVICE, TOKEN, GatewayCase, tone

URL = "https://backend.example/v1/voice"
KEY = "integration-backend-token"


class ConfigTests(unittest.TestCase):
    def test_existing_default_and_http_selection(self):
        self.assertIsInstance(build_backend(Config()), BackendClient)
        cfg = Config.from_env({"PASSPORT_BACKEND": "http", "PASSPORT_BACKEND_URL": URL,
                               "PASSPORT_BACKEND_API_KEY": KEY, "PASSPORT_BINDINGS": "bindings.json",
                               "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK": "1"})
        cfg.validate()
        self.assertIsInstance(build_backend(cfg), HttpBackendClient)
        self.assertFalse(build_backend(cfg).supports_reminders)
        self.assertNotIn(KEY, repr(cfg))

    def test_endpoint_and_token_validation(self):
        for url in ("", "http://backend.example/voice", "file:///tmp/backend", URL + "?key=private",
                    URL + "#fragment", "https://user@backend.example/voice", "https://backend.example:bad"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                HttpBackendClient(url, KEY)
        for token in ("", "has space", "line\nbreak"):
            with self.subTest(token=token), self.assertRaises(ConfigError):
                HttpBackendClient(URL, token)
        for url in (URL, "http://127.0.0.1:8766/v1/voice", "http://[::1]:8766/v1/voice"):
            HttpBackendClient(url, KEY)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, **kwargs):
        return HttpBackendClient(URL, KEY, transport=httpx.MockTransport(handler), **kwargs)

    async def test_exact_wire_contract_and_device_scoped_idempotency(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "reply": "你好", "mood": "gentle", "memory_stored": False})
        client = self.client(handler)
        reply = await client.voice_turn("device-a", "a" * 32, "hello")
        await client.voice_turn("device-a", "a" * 32, "hello")
        await client.voice_turn("device-b", "a" * 32, "hello")
        self.assertEqual(reply.text, "你好")
        self.assertFalse(reply.memory_stored)
        self.assertEqual(json.loads(seen[0].content), {"op": "voice_turn", "device_id": "device-a",
                                                       "turn_uid": "a" * 32, "text": "hello"})
        self.assertEqual(seen[0].headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(seen[0].headers["X-Passport-Protocol"], "1")
        self.assertEqual(seen[0].headers["Idempotency-Key"], seen[1].headers["Idempotency-Key"])
        self.assertNotEqual(seen[0].headers["Idempotency-Key"], seen[2].headers["Idempotency-Key"])

    async def test_http_errors_are_bounded_and_never_retried_or_redirected(self):
        for status, code in ((302, "brain_failed"), (401, "internal"), (403, "internal"),
                             (429, "busy"), (503, "busy"), (504, "brain_timeout"), (500, "brain_failed")):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(status, headers={"Location": "https://elsewhere.example/"},
                                      text="private diagnostic content")
            with self.subTest(status=status), self.assertRaises(BackendError) as cm:
                await self.client(handler).voice_turn(DEVICE, "a" * 32, "hello")
            self.assertEqual(cm.exception.code, code)
            self.assertNotIn("private", str(cm.exception))
            self.assertEqual(len(seen), 1)

    async def test_malformed_or_oversized_replies_and_private_errors_are_not_forwarded(self):
        bodies = [b"not JSON", b"[]", b"{}", b"\xff", b"x" * (MAX_PACKET + 1),
                  json.dumps({"ok": True, "reply": ""}).encode(),
                  json.dumps({"ok": False, "error": "private diagnostic content"}).encode()]
        for body in bodies:
            with self.subTest(size=len(body)), self.assertRaises(BackendError) as cm:
                await self.client(lambda _: httpx.Response(200, content=body)).voice_turn(DEVICE, "a" * 32, "hello")
            self.assertEqual(cm.exception.code, "brain_failed")
            self.assertNotIn("private", str(cm.exception))

    async def test_timeout_and_cancel_close_response_without_retry(self):
        for cancel in (False, True):
            closed, reading = asyncio.Event(), asyncio.Event()
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    reading.set()
                    await asyncio.Event().wait()
                    yield b"never"
                async def aclose(self):
                    closed.set()
            client = self.client(lambda _: httpx.Response(200, stream=Stream()), timeout_s=1 if cancel else 0.03)
            task = asyncio.create_task(client.voice_turn(DEVICE, "a" * 32, "hello"))
            await reading.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                with self.assertRaises(BackendError) as cm:
                    await task
                self.assertEqual(cm.exception.code, "brain_timeout")
            self.assertTrue(closed.is_set())

    async def test_optional_reminder_operations_use_same_authenticated_transport(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "job": None})
        client = self.client(handler, reminders=True)
        self.assertTrue(client.supports_reminders)
        self.assertEqual(await client.reminder("claim", DEVICE), {"ok": True, "job": None})
        self.assertEqual(seen, [{"op": "reminder_claim", "device_id": DEVICE}])

    async def test_real_http_example_and_websocket_round_trip(self):
        result = await run_smoke()
        self.assertTrue(result["ok"])
        self.assertGreater(result["audio_bytes"], 0)


class GatewayHttpTests(GatewayCase):
    async def test_compatible_firmware_speaks_without_reminder_polling_or_identity_leak(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "reply": "hello", "mood": "neutral"})
        self.gw.sv.backend = HttpBackendClient(URL, KEY, transport=httpx.MockTransport(handler))
        ws = await self.connect(hello=False, headers={"X-Role": "owner", "X-Session": "private"})
        await ws.send(P.dumps({"v": 1, "type": "hello", "fw": "0.3.1"}))
        self.assertEqual((await self.recv_json(ws))["type"], "ready")
        await asyncio.sleep(0.15)
        self.assertEqual(seen, [])
        await self.speak(ws, 1, tone(500))
        messages, audio = await self.collect(ws, lambda m: m["type"] in ("audio_end", "error"))
        self.assertEqual(messages[-1]["type"], "audio_end")
        self.assertTrue(audio)
        self.assertEqual(len(seen), 1)
        self.assertNotIn(TOKEN, str(seen[0].headers))
        self.assertEqual(set(json.loads(seen[0].content)), {"op", "device_id", "turn_uid", "text"})
