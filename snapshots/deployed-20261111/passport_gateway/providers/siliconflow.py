"""SiliconFlow adapters (initial default provider).

ASR  POST {base}/audio/transcriptions   multipart file=<WAV> + model=FunAudioLLM/SenseVoiceSmall -> {"text": ...}
     This is TURN-END recognition: the device streams PCM continuously, the SERVER alone buffers
     (<= 20 s / 640000 bytes) and uploads once when the turn ends. There are no interim transcripts.
TTS  POST {base}/audio/speech           JSON {model, input, voice, response_format: pcm,
     sample_rate: 24000, stream: true, speed, gain} -> raw PCM s16le/24k/mono byte stream.
Schemas checked against docs.siliconflow.cn on 2026-11-02. API key comes from the environment only.
"""
from __future__ import annotations

import re
import sys
from array import array
from typing import AsyncIterator

import httpx

from .. import protocol as P
from ..wav import pcm_to_wav
from .base import ProviderError, ProviderUnconfigured
from .audio_format import pcm_chunks

SILENCE_PEAK = 100                       # |sample| below this everywhere = silence; skip the paid call
_TAGS = re.compile(r"<\|[^|>]*\|>")      # SenseVoice meta tags such as <|zh|><|NEUTRAL|>
_CJK_GAP = re.compile(r"(?<=[㐀-鿿　-〿＀-￯])\s+(?=[㐀-鿿　-〿＀-￯])")


_STYLE_QUOTES = re.compile(r"""“[^”]*”|‘[^’]*’|"[^"]*"|'[^']*'""")
_STYLE_ACTIONS = re.compile(
    r"前半句|后半句|转折前|停半拍|短停顿|局部拖腔|重读|重音|"
    r"句尾(?:稍微|稍|微)?(?:拉长|拖长)|尾音(?:上扬|下收|下落)")


def normalize_style(value: str) -> str:
    """Keep a short overall emotion, omitting unsupported per-word stage directions."""
    if not isinstance(value, str):
        return ""
    value = _TAGS.sub("", value).strip()
    parts = []
    for part in re.split(r"[，,。；;\n]+", value):
        if _STYLE_ACTIONS.search(part):
            continue
        part = _STYLE_QUOTES.sub("", part).strip()
        if part:
            parts.append(part)
    if not parts:
        return ""
    style = "，".join(parts)
    if len(style) > 35:
        style = parts[0]
    style = style[:34].rstrip(" 。.!！?？；;，,")
    return style + "。" if style else ""


def _timeouts(read: float) -> httpx.Timeout:
    return httpx.Timeout(connect=5.0, read=read, write=10.0, pool=5.0)


def _http_detail(response: httpx.Response, body: bytes = b"") -> str:
    # Status + the provider's short message only; never request headers (they carry the key).
    text = (body or response.content or b"")[:200].decode("utf-8", errors="replace")
    return f"HTTP {response.status_code} {text}".strip()


class BufferedAsrStream:
    """Server-side bounded buffer; one HTTP call at finish()."""

    def __init__(self, provider: "SiliconFlowAsr"):
        self._p = provider
        self._buf = bytearray()
        self._peak = 0
        self._closed = False

    async def feed(self, pcm: bytes) -> None:
        if len(self._buf) + len(pcm) > P.MAX_UP_BYTES:
            raise ProviderError("too_long", "utterance exceeds 20 s")
        self._buf += pcm
        samples = array("h", pcm[:len(pcm) & ~1])
        if sys.byteorder == "big":
            samples.byteswap()
        if samples:
            self._peak = max(self._peak, max(samples), -min(samples))

    async def finish(self) -> str:
        if self._peak < SILENCE_PEAK:
            return ""                                    # truthful silence; no provider call
        wav = pcm_to_wav(bytes(self._buf), P.UP_HZ)
        self._buf.clear()
        try:
            async with self._p.client() as client:
                r = await client.post(
                    f"{self._p.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._p.api_key}"},
                    data={"model": self._p.model},
                    files={"file": ("turn.wav", wav, "audio/wav")},
                    timeout=_timeouts(self._p.timeout))
        except httpx.TimeoutException as exc:
            raise ProviderError("asr_timeout", "ASR request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("asr_failed", f"ASR transport error: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ProviderError("asr_timeout" if r.status_code == 504 else "asr_failed", _http_detail(r))
        try:
            text = r.json()["text"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("asr_failed", "malformed ASR response") from exc
        if not isinstance(text, str):
            raise ProviderError("asr_failed", "ASR text is not a string")
        return _TAGS.sub("", text).strip()

    async def close(self) -> None:
        self._closed = True
        self._buf.clear()


class SiliconFlowAsr:
    name = "siliconflow"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 25.0, transport=None):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.timeout = timeout
        self._transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport)

    async def open(self) -> BufferedAsrStream:
        if not self.api_key:
            raise ProviderUnconfigured("SILICONFLOW_API_KEY is not set")
        return BufferedAsrStream(self)

    def diagnostics(self) -> dict:
        return {"provider": self.name, "model": self.model, "mode": "turn_end", "interim": False}


class SiliconFlowTts:
    name = "siliconflow"

    SOFT_MOODS = ("gentle", "serious")

    def __init__(self, base_url: str, model: str, voice: str, api_key: str, style: str = "",
                 speed: float = 1.0, gain: float = 0.0, first_byte_timeout: float = 15.0, transport=None,
                 style_soft: str = ""):
        self.base_url, self.model, self.voice, self.api_key = base_url.rstrip("/"), model, voice, api_key
        self.style, self.speed, self.gain = style.strip(), speed, gain
        self.style_soft = style_soft.strip() or self.style
        self.first_byte_timeout = first_byte_timeout
        self._transport = transport
        self.requests = 0
        self.last_status: int | None = None

    def diagnostics(self) -> dict:
        return {"provider": self.name, "configured_model": self.model, "actual_model": self.model,
                "voice": self.voice, "stream": True, "format": "pcm", "sample_rate": P.DOWN_HZ,
                "dynamic_style": True, "style_configured": bool(self.style), "style_soft_configured": self.style_soft != self.style,
                "speed": self.speed, "gain": self.gain,
                "requests": self.requests, "last_status": self.last_status}

    def style_for(self, mood: str, voice_style: str = "") -> str:
        """The reply chooses its delivery; configured styles cover replies without a direction."""
        style = normalize_style(voice_style)
        return style or normalize_style(self.style_soft if mood in self.SOFT_MOODS else self.style)

    def build_input(self, text: str, mood: str = "neutral", voice_style: str = "") -> str:
        # Docs: CosyVoice input should not contain spaces; natural-language style goes before
        # <|endofprompt|>. The voice id never changes with mood (no timbre switching).
        body = _CJK_GAP.sub("", text)
        style = self.style_for(mood, voice_style)
        return f"{style}<|endofprompt|>{body}" if style else body

    def payload(self, text: str, mood: str = "neutral", voice_style: str = "") -> dict:
        return {"model": self.model, "input": self.build_input(text, mood, voice_style), "voice": self.voice,
                "response_format": "pcm", "sample_rate": P.DOWN_HZ, "stream": True,
                "speed": self.speed, "gain": self.gain}

    async def synthesize(self, text: str, mood: str = "neutral", *, voice_style: str = "") -> AsyncIterator[bytes]:
        if not self.api_key:
            raise ProviderUnconfigured("SILICONFLOW_API_KEY is not set")
        self.requests += 1
        async with httpx.AsyncClient(transport=self._transport) as client:
            try:
                async with client.stream(
                        "POST", f"{self.base_url}/audio/speech",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=self.payload(text, mood, voice_style), timeout=_timeouts(self.first_byte_timeout)) as r:
                    self.last_status = r.status_code
                    if r.status_code != 200:
                        body = await r.aread()
                        raise ProviderError("tts_timeout" if r.status_code == 504 else "tts_failed",
                                            _http_detail(r, body))
                    async for chunk in pcm_chunks(r):
                        yield chunk
            except httpx.TimeoutException as exc:
                raise ProviderError("tts_timeout", "TTS stream timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("tts_failed", f"TTS transport error: {type(exc).__name__}") from exc
