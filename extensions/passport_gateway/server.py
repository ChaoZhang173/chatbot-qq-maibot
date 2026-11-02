"""WSS server: handshake authentication, connection limits, diagnostics, graceful shutdown."""
from __future__ import annotations

import asyncio
import http
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass

import websockets
from websockets.asyncio.server import ServerConnection, serve

from . import __version__
from . import protocol as P
from .backend import BackendClient
from .config import Bindings, Config, DEVICE_ID_RE, is_loopback, token_hash
from .providers import build_providers
from .session import DeviceSession, Stats

log = logging.getLogger("passport.server")


class _Redact(logging.Filter):
    """websockets logs request headers at DEBUG; never let a bearer token reach a log record."""
    _PAT = re.compile(r"(?i)(authorization:?\s*)(bearer\s+)?\S+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "uthorization" in message:
            record.msg, record.args = self._PAT.sub(r"\1[redacted]", message), None
        return True


ws_log = logging.getLogger("passport.ws")
ws_log.addFilter(_Redact())
AUTH_FAIL_LIMIT = 10          # per source address per window
AUTH_FAIL_WINDOW_S = 60
MAX_TRACKED_ADDRESSES = 1024
MAX_WS_MESSAGE = 4096         # > largest legal frame (8 + 3200); bounds memory per message


@dataclass
class Services:
    cfg: Config
    bindings: Bindings
    asr: object
    tts: object
    backend: object
    stats: Stats
    max_active_turns: int
    _active_turns: int = 0

    def try_acquire_turn(self) -> bool:
        if self._active_turns >= self.max_active_turns:
            return False
        self._active_turns += 1
        return True

    def release_turn(self) -> None:
        self._active_turns = max(0, self._active_turns - 1)


class GatewayServer:
    def __init__(self, cfg: Config, *, asr=None, tts=None, backend=None, bindings: Bindings | None = None):
        cfg.validate()
        default_asr, default_tts = build_providers(cfg)
        self.cfg = cfg
        self.sv = Services(
            cfg=cfg,
            bindings=bindings or Bindings(cfg.bindings_path),
            asr=asr or default_asr,
            tts=tts or default_tts,
            backend=backend or BackendClient(cfg.backend_socket, cfg.brain_timeout_s),
            stats=Stats(),
            max_active_turns=cfg.max_active_turns,
        )
        self.sessions: dict[str, DeviceSession] = {}
        self._auth_failures: dict[str, tuple[int, float]] = {}
        self._server = None
        self._watcher = None
        self.started = time.time()
        self.binding_poll_s = 2.0

    # ------------------------------------------------------------- handshake
    def _throttled(self, addr: str) -> bool:
        count, since = self._auth_failures.get(addr, (0, 0.0))
        return count >= AUTH_FAIL_LIMIT and time.monotonic() - since < AUTH_FAIL_WINDOW_S

    def _auth_failed(self, addr: str) -> None:
        now = time.monotonic()
        count, since = self._auth_failures.get(addr, (0, now))
        if now - since >= AUTH_FAIL_WINDOW_S:
            count, since = 0, now
        if len(self._auth_failures) >= MAX_TRACKED_ADDRESSES:
            self._auth_failures.pop(next(iter(self._auth_failures)))
        self._auth_failures[addr] = (count + 1, since)

    @staticmethod
    def _credentials(request) -> tuple[str, str]:
        device_id = request.headers.get("X-Device-Id", "")
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        return device_id, token

    def diagnostics(self) -> dict:
        return {
            "version": __version__,
            "uptime_s": int(time.time() - self.started),
            "test_mode": self.cfg.test_mode,
            "asr": self.sv.asr.diagnostics(),
            "tts": self.sv.tts.diagnostics(),
            "limits": {"max_sessions": self.cfg.max_sessions, "max_active_turns": self.cfg.max_active_turns,
                       "max_utterance_ms": P.MAX_UTTERANCE_MS},
            "sessions": len(self.sessions),
            "bound_devices": self.sv.bindings.count(),
            "stats": self.sv.stats.snapshot(),
        }

    async def _process_request(self, connection: ServerConnection, request):
        peer = connection.remote_address
        addr = peer[0] if peer else "?"
        if request.path == "/diagnostics":
            if not is_loopback(addr):
                return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
            body = json.dumps(self.diagnostics(), ensure_ascii=False) + "\n"
            response = connection.respond(http.HTTPStatus.OK, body)
            response.headers["Content-Type"] = "application/json"
            return response
        if request.path != "/v1/voice":
            return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
        if self._throttled(addr):
            return connection.respond(http.HTTPStatus.TOO_MANY_REQUESTS, "slow down\n")
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")]
        if P.SUBPROTOCOL not in offered:
            return connection.respond(http.HTTPStatus.BAD_REQUEST, "unsupported subprotocol\n")
        device_id, token = self._credentials(request)
        if not self.sv.bindings.verify(device_id, token):
            self._auth_failed(addr)
            # Same answer for unknown device, wrong token and disabled binding.
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        if len(self.sessions) >= self.cfg.max_sessions and device_id not in self.sessions:
            return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "busy\n")
        return None

    async def _handler(self, connection: ServerConnection) -> None:
        device_id, token = self._credentials(connection.request)
        # Re-verify after the upgrade (the binding file may have changed in between).
        if not self.sv.bindings.verify(device_id, token) or not DEVICE_ID_RE.match(device_id):
            await connection.close(code=4401, reason="unauthorized")
            return
        previous = self.sessions.get(device_id)
        if previous is not None:                      # one connection per device: newest wins
            previous.closing = True
            await previous.ws.close(code=4409, reason="replaced by a newer connection")
        session = DeviceSession(connection, device_id, self.sv, token_hash(token))
        self.sessions[device_id] = session
        log.info("device=%s connected (%d sessions)", device_id, len(self.sessions))
        try:
            await session.run()
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("device=%s session crashed", device_id)
        finally:
            if self.sessions.get(device_id) is session:
                del self.sessions[device_id]
            log.info("device=%s disconnected", device_id)

    # ------------------------------------------------------------- lifecycle
    def _ssl(self) -> ssl.SSLContext | None:
        if not self.cfg.tls_cert:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cfg.tls_cert, self.cfg.tls_key)
        return ctx

    async def _watch_bindings(self) -> None:
        """Close established connections whose binding was disabled or whose token was rotated."""
        while True:
            await asyncio.sleep(self.binding_poll_s)
            for session in list(self.sessions.values()):
                try:
                    await session.check_binding()
                except Exception:
                    log.exception("binding watcher failed for %s", session.device_id)

    async def start(self):
        self._server = await serve(
            self._handler, self.cfg.host, self.cfg.port, ssl=self._ssl(),
            subprotocols=[P.SUBPROTOCOL], process_request=self._process_request,
            max_size=MAX_WS_MESSAGE, max_queue=16, compression=None, logger=ws_log,
            ping_interval=20, ping_timeout=20, open_timeout=8, close_timeout=3)
        self._watcher = asyncio.create_task(self._watch_bindings(), name="binding-watcher")
        log.info("gateway listening on %s:%s tls=%s test_mode=%s asr=%s tts=%s",
                 self.cfg.host, self.port, bool(self.cfg.tls_cert), self.cfg.test_mode,
                 self.sv.asr.diagnostics(), self.sv.tts.diagnostics())
        return self

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._watcher:
            self._watcher.cancel()
        if self._server:
            self._server.close()
            for session in list(self.sessions.values()):
                session.closing = True
                await session.ws.close(code=1001, reason="server shutting down")
            await self._server.wait_closed()
