"""Deterministic provider fixtures. FOR TEST MODE ONLY (PASSPORT_TEST_MODE=1): they make no
network calls and are never selected in a normal run (see providers/__init__.py)."""
from __future__ import annotations

import asyncio
import math
import struct
from typing import AsyncIterator

from .base import ProviderError

SILENCE_THRESHOLD = 50     # peak amplitude below this = "silence"


class FakeAsrStream:
    def __init__(self, provider: "FakeAsr"):
        self._p = provider
        self.bytes_fed = 0
        self.peak = 0
        self.closed = False

    async def feed(self, pcm: bytes) -> None:
        if self._p.feed_delay:
            await asyncio.sleep(self._p.feed_delay)
        if self._p.fail_feed:
            raise ProviderError("asr_failed", "fixture feed failure")
        self.bytes_fed += len(pcm)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        self.peak = max(self.peak, max((abs(s) for s in samples), default=0))

    async def finish(self) -> str:
        if self._p.finish_delay:
            await asyncio.sleep(self._p.finish_delay)
        if self._p.fail_finish:
            raise ProviderError(self._p.fail_finish, "fixture finish failure")
        self._p.finished.append(self.bytes_fed)
        return "" if self.peak < SILENCE_THRESHOLD else self._p.transcript

    async def close(self) -> None:
        self.closed = True
        self._p.closed += 1


class FakeAsr:
    name = "fixture"
    model = "fixture-asr"

    def __init__(self, transcript: str = "你好呀"):
        self.transcript = transcript
        self.feed_delay = 0.0
        self.finish_delay = 0.0
        self.fail_feed = False
        self.fail_finish = ""
        self.finished: list[int] = []
        self.closed = 0
        self.opened = 0

    def diagnostics(self) -> dict:
        return {"provider": self.name, "model": self.model, "mode": "fixture"}

    async def open(self) -> FakeAsrStream:
        self.opened += 1
        return FakeAsrStream(self)


class FakeTts:
    name = "fixture"
    model = "fixture-tts"
    voice = "fixture-voice"

    def __init__(self, ms_per_char: int = 120):
        self.ms_per_char = ms_per_char
        self.chunk_delay = 0.0
        self.first_delay = 0.0
        self.fail = ""
        self.fail_after_chunks = -1
        self.gate: asyncio.Event | None = None      # when set, stream blocks after the first chunk
        self.calls: list[tuple[str, str]] = []
        self.streams_closed = 0
        self.chunks_produced = 0

    def diagnostics(self) -> dict:
        return {"provider": "fixture", "configured_model": self.model, "actual_model": self.model,
                "voice": self.voice, "instructions": False, "fallback_model": None, "fallback_count": 0}

    async def synthesize(self, text: str, mood: str) -> AsyncIterator[bytes]:
        self.calls.append((text, mood))
        if self.fail == "start":
            raise ProviderError("tts_failed", "fixture tts failure")
        if self.first_delay:
            await asyncio.sleep(self.first_delay)
        samples = 24 * self.ms_per_char * max(1, len(text))
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 24000)))
                       for i in range(samples))
        step = 4800                                  # 100 ms; the pipeline re-frames to <= 2048 B
        try:
            for count, offset in enumerate(range(0, len(pcm), step)):
                if self.fail_after_chunks >= 0 and count >= self.fail_after_chunks:
                    raise ProviderError("tts_failed", "fixture mid-stream failure")
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                if count == 1 and self.gate is not None:
                    await self.gate.wait()
                self.chunks_produced += 1
                yield pcm[offset:offset + step]
        finally:
            self.streams_closed += 1
