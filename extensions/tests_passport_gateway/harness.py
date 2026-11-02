"""Shared test harness: a real loopback WebSocket gateway with FIXTURE providers/backend.

Everything here is simulation: no network egress, no paid APIs. Live-provider behaviour is not
covered by these tests (see IMPLEMENTATION_REPORT 'Live speech')."""
from __future__ import annotations

import asyncio
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

import websockets

from passport_gateway import protocol as P
from passport_gateway.backend import BackendError, VoiceReply
from passport_gateway.config import Bindings, Config, token_hash
from passport_gateway.providers.fake import FakeAsr, FakeTts
from passport_gateway.server import GatewayServer

TOKEN = "unit-test-token-0123456789abcdef"
DEVICE = "dev-test1"


def tone(ms: int, amp: int = 6000) -> bytes:
    n = 16 * ms
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 16000))) for i in range(n))


def silence(ms: int) -> bytes:
    return bytes(32 * ms)


class FakeBackend:
    """Stands in for the unified-core voice_turn IPC. Records exactly what the gateway sends."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = VoiceReply("好的，我记住了。**今天**不错 https://example.com/x", "pleased", True, 3)
        self.error: BackendError | None = None
        self.gate: asyncio.Event | None = None
        self.completed = 0

    async def voice_turn(self, device_id, turn_uid, text):
        self.requests.append({"device_id": device_id, "turn_uid": turn_uid, "text": text})
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        self.completed += 1
        return self.reply


class GatewayCase(unittest.IsolatedAsyncioTestCase):
    cfg_overrides: dict = {}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bindings_path = Path(self.tmp.name) / "bindings.json"
        self.write_bindings({DEVICE: TOKEN})
        self.asr, self.tts, self.backend = FakeAsr("今天天气怎么样"), FakeTts(), FakeBackend()
        cfg = Config(host="127.0.0.1", port=0, allow_plaintext_loopback=True, test_mode=True,
                     bindings_path=str(self.bindings_path), max_sessions=4, max_active_turns=3,
                     asr_timeout_s=2.0, brain_timeout_s=2.0, tts_first_audio_timeout_s=2.0,
                     tts_total_timeout_s=10.0, send_timeout_s=2.0, upload_grace_ms=3000,
                     pace_lead_ms=0)          # pacing is exercised explicitly in test_pacing.py
        for k, v in self.cfg_overrides.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.gw = GatewayServer(cfg, asr=self.asr, tts=self.tts, backend=self.backend)
        self.gw.binding_poll_s = 0.1
        await self.gw.start()
        self.url = f"ws://127.0.0.1:{self.gw.port}/v1/voice"
        self.clients: list = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        await self.gw.stop()
        self.tmp.cleanup()

    def write_bindings(self, devices: dict[str, str], enabled: bool = True):
        data = {"devices": {d: {"token_sha256": token_hash(t), "enabled": enabled} for d, t in devices.items()}}
        self.bindings_path.write_text(json.dumps(data))

    async def connect(self, device=DEVICE, token=TOKEN, hello=True, headers=None):
        h = {"X-Device-Id": device, "Authorization": f"Bearer {token}"}
        h.update(headers or {})
        ws = await websockets.connect(self.url, additional_headers=h, subprotocols=[P.SUBPROTOCOL],
                                      max_size=None, compression=None)
        self.clients.append(ws)
        if hello:
            await ws.send(P.dumps({"v": 1, "type": "hello", "fw": "0.1.0"}))
            msg = await self.recv_json(ws)
            assert msg["type"] == "ready", msg
        return ws

    @staticmethod
    async def recv(ws, timeout=3.0):
        return await asyncio.wait_for(ws.recv(), timeout)

    async def recv_json(self, ws, timeout=3.0):
        raw = await self.recv(ws, timeout)
        assert isinstance(raw, str), "expected a text frame"
        return json.loads(raw)

    @staticmethod
    def start_msg(turn):
        return P.dumps({"v": 1, "type": "start", "turn": turn, "hz": 16000, "ch": 1, "fmt": "pcm_s16le"})

    @staticmethod
    def end_msg(turn, n):
        return P.dumps({"v": 1, "type": "end", "turn": turn, "bytes": n})

    @staticmethod
    def cancel_msg(turn):
        return P.dumps({"v": 1, "type": "cancel", "turn": turn})

    async def speak(self, ws, turn, pcm: bytes, end=True, chunk=3200):
        await ws.send(self.start_msg(turn))
        for seq, off in enumerate(range(0, len(pcm), chunk)):
            await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, turn, seq) + pcm[off:off + chunk])
        if end:
            await ws.send(self.end_msg(turn, len(pcm)))

    async def collect(self, ws, until, timeout=5.0):
        """Collect messages until `until(msg)`; returns (texts, binary_frames)."""
        texts, frames = [], []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            raw = await self.recv(ws, max(0.05, deadline - asyncio.get_running_loop().time()))
            if isinstance(raw, bytes):
                frames.append(raw)
                continue
            msg = json.loads(raw)
            texts.append(msg)
            if until(msg):
                return texts, frames
