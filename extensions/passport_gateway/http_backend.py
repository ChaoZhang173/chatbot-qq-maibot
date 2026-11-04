"""Portable backend protocol v1: bounded JSON requests over authenticated HTTP(S)."""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

import httpx

from .backend import BackendClient, BackendError, MAX_PACKET
from .config import ConfigError, is_loopback


def validate_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ConfigError("Invalid PASSPORT_BACKEND_URL") from None
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port == 0
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback(host)))):
        raise ConfigError("PASSPORT_BACKEND_URL requires HTTPS (HTTP only on loopback), "
                          "with no embedded credentials, query or fragment")


class HttpBackendClient(BackendClient):
    """Same operations as the local backend; the operator chooses a trusted endpoint.

    Device tokens are never sent to the backend. The backend maps authenticated device IDs
    to its own users/sessions. No automatic retry: a timed-out request may have performed work.
    Reminder polling is opt-in, since most chatbot backends implement only voice_turn.
    """

    def __init__(self, url: str, api_key: str, timeout_s: float = 25.0, *,
                 reminders: bool = False, transport: httpx.AsyncBaseTransport | None = None):
        validate_endpoint(url)
        if not api_key or any(c.isspace() for c in api_key):
            raise ConfigError("PASSPORT_BACKEND_API_KEY must be a nonempty token without whitespace")
        self.url = url
        self._api_key = api_key
        self.timeout_s = timeout_s
        self.supports_reminders = reminders
        self._transport = transport

    async def _request(self, request: dict) -> bytes:
        headers = {"Authorization": "Bearer " + self._api_key,
                   "X-Passport-Protocol": "1", "Accept": "application/json"}
        if request.get("op") == "voice_turn":
            identity = request["device_id"] + "\0" + request["turn_uid"]
            headers["Idempotency-Key"] = hashlib.sha256(identity.encode()).hexdigest()
        try:
            async with asyncio.timeout(self.timeout_s):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout_s, connect=min(3.0, self.timeout_s)),
                    follow_redirects=False, trust_env=False, transport=self._transport,
                ) as client:
                    async with client.stream("POST", self.url, headers=headers, json=request) as response:
                        if response.status_code in (408, 504):
                            raise BackendError("brain_timeout")
                        if response.status_code in (429, 503):
                            raise BackendError("busy")
                        if response.status_code in (401, 403):
                            raise BackendError("internal", "backend authentication failed")
                        if response.status_code != 200:
                            raise BackendError("brain_failed", "backend HTTP error")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_PACKET:
                                raise BackendError("brain_failed", "backend reply too large")
                            body.extend(chunk)
                        return bytes(body)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise BackendError("brain_timeout") from exc
        except httpx.HTTPError as exc:
            raise BackendError("brain_failed", "backend unavailable") from exc


def build_backend(cfg):
    if cfg.backend_mode == "http":
        return HttpBackendClient(cfg.backend_url, cfg.backend_api_key, cfg.brain_timeout_s,
                                 reminders=cfg.http_reminders)
    return BackendClient(cfg.backend_socket, cfg.brain_timeout_s)
