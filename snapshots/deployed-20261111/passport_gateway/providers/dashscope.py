"""DashScope realtime ASR/TTS adapters over `websockets`. API key is env-only and never logged."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import AsyncIterator

import websockets

from . import dashscope_schema as S
from .base import ProviderError, ProviderUnconfigured

log = logging.getLogger("passport.dashscope")
CONNECT_TIMEOUT = 8.0


async def _connect(url: str, api_key: str):
    if not api_key:
        raise ProviderUnconfigured("DASHSCOPE_API_KEY is not set")
    return await asyncio.wait_for(
        websockets.connect(url, additional_headers=S.auth_headers(api_key), max_size=2 ** 22,
                           open_timeout=CONNECT_TIMEOUT, ping_interval=20, compression=None),
        CONNECT_TIMEOUT + 2)


async def _expect(ws, wanted: str, code: str, timeout: float = 8.0) -> dict:
    """Read events until `wanted`; an `error` event or a close raises ProviderError(code)."""
    async def loop():
        async for raw in ws:
            event = S.parse_event(raw)
            if event["type"] == wanted:
                return event
            if event["type"] == S.ERROR:
                raise ProviderError(code, S.error_text(event), unsupported=S.error_is_unsupported(event))
        raise ProviderError(code, "provider closed the connection")
    try:
        return await asyncio.wait_for(loop(), timeout)
    except asyncio.TimeoutError as exc:
        raise ProviderError(code.replace("failed", "timeout"), f"timeout waiting for {wanted}") from exc
    except ValueError as exc:
        raise ProviderError(code, "malformed provider event") from exc
    except websockets.ConnectionClosed as exc:
        raise ProviderError(code, "provider connection closed") from exc


class DashScopeAsrStream:
    def __init__(self, ws, timeout: float):
        self._ws = ws
        self._timeout = timeout
        self._closed = False

    async def feed(self, pcm: bytes) -> None:
        try:
            await self._ws.send(S.asr_append(pcm))
        except websockets.ConnectionClosed as exc:
            raise ProviderError("asr_failed", "provider connection closed") from exc

    async def finish(self) -> str:
        try:
            await self._ws.send(S.asr_commit())
        except websockets.ConnectionClosed as exc:
            raise ProviderError("asr_failed", "provider connection closed") from exc
        event = await _expect(self._ws, S.ASR_TRANSCRIPT_DONE, "asr_failed", self._timeout)
        text = event.get("transcript", event.get("text", ""))
        if not isinstance(text, str):
            raise ProviderError("asr_failed", "transcript is not text")
        try:
            await self._ws.send(S.asr_finish())
        except websockets.ConnectionClosed:
            pass
        return text.strip()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._ws.close()


class DashScopeAsr:
    name = "dashscope"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 25.0):
        self.base_url, self.model, self._key, self._timeout = base_url, model, api_key, timeout

    async def open(self) -> DashScopeAsrStream:
        ws = await _connect(S.endpoint(self.base_url, self.model), self._key)
        try:
            await _expect(ws, S.SESSION_CREATED, "asr_failed")
            await ws.send(S.asr_session_update())
            await _expect(ws, S.SESSION_UPDATED, "asr_failed")
        except BaseException:
            await ws.close()
            raise
        return DashScopeAsrStream(ws, self._timeout)


class DashScopeTts:
    """Primary model with an optional, reported fallback (never silent)."""
    name = "dashscope"

    def __init__(self, base_url: str, model: str, voice: str, api_key: str, base_instructions: str,
                 fallback_model: str = "", fallback: bool = True):
        self.base_url, self.model, self.voice, self._key = base_url, model, voice, api_key
        self._instructions = base_instructions
        self._fallback_model = fallback_model if fallback else ""
        self.actual_model = model
        self.fallback_count = 0
        self.instruct_supported = "instruct" in model

    def diagnostics(self) -> dict:
        return {"provider": self.name, "configured_model": self.model, "actual_model": self.actual_model,
                "voice": self.voice, "instructions": self.instruct_supported,
                "fallback_model": self._fallback_model or None, "fallback_count": self.fallback_count}

    async def synthesize(self, text: str, mood: str, *, voice_style: str = "") -> AsyncIterator[bytes]:
        from ..speech_text import instructions_for
        attempts = [(self.model, (voice_style or instructions_for(self._instructions, mood)) if self.instruct_supported else None)]
        if self._fallback_model and self._fallback_model != self.model:
            attempts.append((self._fallback_model, None))
        last: ProviderError | None = None
        for index, (model, instructions) in enumerate(attempts):
            produced = False
            try:
                async for chunk in self._one(model, instructions, text):
                    produced = True
                    yield chunk
                self.actual_model = model
                return
            except ProviderError as exc:
                last = exc
                # Fall back only on a capability error before any audio was produced.
                if produced or not exc.unsupported or index + 1 >= len(attempts):
                    raise
                self.fallback_count += 1
                log.warning("TTS model %s unavailable (%s); falling back to %s without instructions",
                            model, exc.detail, attempts[index + 1][0])
        if last:
            raise last

    async def _one(self, model: str, instructions: str | None, text: str) -> AsyncIterator[bytes]:
        ws = await _connect(S.endpoint(self.base_url, model), self._key)
        try:
            await _expect(ws, S.SESSION_CREATED, "tts_failed")
            await ws.send(S.tts_session_update(self.voice, instructions=instructions))
            await _expect(ws, S.SESSION_UPDATED, "tts_failed")
            await ws.send(S.tts_append(text))
            await ws.send(S.tts_commit())
            while True:
                try:
                    raw = await ws.recv()
                except websockets.ConnectionClosed as exc:
                    raise ProviderError("tts_failed", "provider connection closed") from exc
                try:
                    event = S.parse_event(raw)
                except ValueError as exc:
                    raise ProviderError("tts_failed", "malformed provider event") from exc
                kind = event["type"]
                if kind == S.TTS_AUDIO_DELTA:
                    try:
                        pcm = base64.b64decode(event.get("delta", ""), validate=True)
                    except ValueError as exc:
                        raise ProviderError("tts_failed", "bad audio payload") from exc
                    if pcm:
                        yield pcm
                elif kind == S.TTS_RESPONSE_DONE:
                    break
                elif kind == S.ERROR:
                    raise ProviderError("tts_failed", S.error_text(event),
                                        unsupported=S.error_is_unsupported(event))
            try:
                await ws.send(S.tts_finish())
            except websockets.ConnectionClosed:
                pass
        finally:
            await ws.close()
