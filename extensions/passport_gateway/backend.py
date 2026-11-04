"""Client for the narrow `voice_turn` operation of the unified core local IPC.

Transport: newline-delimited JSON over the Unix socket; the core authenticates the peer UID
(SO_PEERCRED) and allows this peer the `voice_turn` operation ONLY. The gateway sends just
{device_id, turn_uid, text}: owner/session/person_id are resolved server-side by the core.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

MAX_PACKET = 64 * 1024


class BackendError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code          # wire error code understood by the device


@dataclass(frozen=True)
class VoiceReply:
    text: str
    mood: str
    memory_stored: bool | None = None
    persona_version: int | None = None


class BackendClient:
    supports_reminders = True

    def __init__(self, socket_path: str, timeout_s: float = 25.0):
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    async def voice_turn(self, device_id: str, turn_uid: str, text: str) -> VoiceReply:
        request = {"op": "voice_turn", "device_id": device_id, "turn_uid": turn_uid, "text": text}
        return parse_response(await self._request(request))

    async def reminder(self, op: str, device_id: str, **fields) -> dict:
        raw = await self._request({"op": "reminder_" + op, "device_id": device_id, **fields})
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BackendError("brain_failed", "malformed reminder reply") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise BackendError("brain_failed", "reminder unavailable")
        return data

    async def _request(self, request: dict) -> bytes:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path, limit=MAX_PACKET), 3.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BackendError("brain_failed", "backend unreachable") from exc
        try:
            writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), self.timeout_s)
        except asyncio.TimeoutError as exc:
            raise BackendError("brain_timeout") from exc
        except (OSError, ValueError) as exc:
            raise BackendError("brain_failed", "backend io") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        return raw


def parse_response(raw: bytes) -> VoiceReply:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BackendError("brain_failed", "malformed backend reply") from exc
    if not isinstance(data, dict):
        raise BackendError("brain_failed", "malformed backend reply")
    if data.get("ok") is not True:
        error = str(data.get("error", ""))
        if error == "voice_timeout":
            raise BackendError("brain_timeout", error)
        if error == "voice_busy":
            raise BackendError("busy", error)
        if error in ("voice_unbound", "voice_disabled", "voice_binding_changed"):
            # not bound / bound to someone else now / authority changed during the turn: all look the same to the device
            raise BackendError("backend_unbound", error)
        if error in ("peer_denied", "admin_only", "unsupported_operation"):
            raise BackendError("internal", error)
        # Never send arbitrary backend diagnostics (which can contain secrets) to the device.
        raise BackendError("brain_failed")
    text = data.get("reply")
    if not isinstance(text, str) or not text.strip():
        raise BackendError("brain_failed", "empty reply")
    mood = data.get("mood") if isinstance(data.get("mood"), str) else "neutral"
    stored = data.get("memory_stored") if isinstance(data.get("memory_stored"), bool) else None
    version = data.get("persona_version") if isinstance(data.get("persona_version"), int) else None
    return VoiceReply(text.strip(), mood, stored, version)
