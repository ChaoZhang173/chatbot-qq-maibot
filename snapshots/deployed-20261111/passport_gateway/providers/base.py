"""Provider interfaces. Providers are pulled by the turn pipeline (no push callbacks), so a
cancelled turn can never be resurrected by a late provider event."""
from __future__ import annotations

from typing import AsyncIterator, Protocol


class ProviderError(Exception):
    """`code` is the wire error code sent to the device."""

    def __init__(self, code: str, detail: str = "", unsupported: bool = False):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.unsupported = unsupported      # e.g. account/model lacks `instructions` support


class ProviderUnconfigured(ProviderError):
    def __init__(self, detail: str = "provider credentials are not configured"):
        super().__init__("provider_unconfigured", detail)


class AsrStream(Protocol):
    async def feed(self, pcm: bytes) -> None: ...
    async def finish(self) -> str: ...
    async def close(self) -> None: ...


class AsrProvider(Protocol):
    name: str
    model: str

    async def open(self) -> AsrStream: ...


class TtsProvider(Protocol):
    name: str
    model: str
    voice: str

    def synthesize(self, text: str, mood: str, *, voice_style: str = "") -> AsyncIterator[bytes]: ...

    def diagnostics(self) -> dict: ...
