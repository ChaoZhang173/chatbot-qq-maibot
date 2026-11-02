"""One authenticated device connection: protocol enforcement + the per-turn pipeline.

Invariants (see docs/development/voice-protocol.md):
  * exactly one active turn per connection; every outbound message belongs to that turn and is
    only sent while `session.active is turn and not turn.cancelled` (checked right before send);
  * providers are pulled from inside the turn's own task, so cancelling the task is sufficient
    to stop them; there are no provider callbacks that could resurrect a cancelled turn;
  * all queues/timeouts are bounded and a violation fails the turn with a wire error code.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from . import protocol as P
from . import speech_text
from .backend import BackendError
from .providers.base import ProviderError

log = logging.getLogger("passport.session")
FEED_QUEUE_DEPTH = 64          # 64 x <=100 ms chunks; overflow = queue_full, never unbounded growth
MAX_STRIKES = 5                # protocol violations tolerated before the connection is dropped


class StaleTurn(Exception):
    """Raised inside a turn task when its turn is no longer the live one."""


class TurnFailure(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass(eq=False)
class Turn:
    id: int
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "recording"            # recording -> processing -> done | cancelled
    bytes: int = 0
    next_seq: int = 0
    cancelled: bool = False
    started: float = field(default_factory=time.monotonic)
    feed_q: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(FEED_QUEUE_DEPTH))
    asr: object = None
    feeder: asyncio.Task | None = None
    pipeline: asyncio.Task | None = None
    deadline: asyncio.TimerHandle | None = None


class Stats:
    def __init__(self):
        self.turns = 0
        self.completed = 0
        self.cancelled = 0
        self.failed: dict[str, int] = {}
        self.stale_audio_dropped = 0
        self.protocol_errors = 0

    def snapshot(self) -> dict:
        return {"turns": self.turns, "completed": self.completed, "cancelled": self.cancelled,
                "failed": dict(self.failed), "stale_audio_dropped": self.stale_audio_dropped,
                "protocol_errors": self.protocol_errors}


class DeviceSession:
    def __init__(self, ws, device_id: str, services, cred_digest: str):
        self.ws = ws
        self.device_id = device_id
        self.cred = cred_digest          # sha256 of the token presented at the handshake (never the token)
        self.sv = services
        self.cfg = services.cfg
        self.ready = False
        self.last_turn = 0
        self.active: Turn | None = None
        self.strikes = 0
        self.closing = False

    # ---------------------------------------------------------------- main loop
    async def run(self) -> None:
        try:
            async for message in self.ws:
                try:
                    if isinstance(message, str):
                        await self._control(P.parse_control(message))
                    else:
                        await self._audio(P.parse_up_frame(message))
                except P.ProtocolError as exc:
                    await self._protocol_violation(exc)
                if self.closing:
                    break
        finally:
            await self._cancel_active("disconnect")

    async def _protocol_violation(self, exc: P.ProtocolError) -> None:
        self.sv.stats.protocol_errors += 1
        self.strikes += 1
        turn = self.active
        log.warning("device=%s protocol violation: %s", self.device_id, exc)
        if turn and exc.code != "out_of_order":
            await self._fail(turn, exc.code, exc.detail)
        else:
            await self._send_raw(P.error(exc.code, turn.id if turn else None, exc.detail))
        if self.strikes >= MAX_STRIKES:
            self.closing = True
            await self.ws.close(code=1008, reason="too many protocol violations")

    # ------------------------------------------------------------------ control
    async def _control(self, ctl: P.Control) -> None:
        if ctl.type == "hello":
            if self.ready:
                raise P.ProtocolError("out_of_order", "duplicate hello")
            self.ready = True
            await self._send_raw(P.ready())
            return
        if not self.ready:
            raise P.ProtocolError("out_of_order", "hello required first")
        if not await self.check_binding():
            return

        if ctl.type == "start":
            await self._start(ctl.turn)
        elif ctl.type == "end":
            await self._end(ctl)
        elif ctl.type == "cancel":
            turn = self.active
            if turn and turn.id == ctl.turn:
                self.sv.stats.cancelled += 1
                await self._cancel_active("device cancel")
            # Cancel for an unknown/finished turn is idempotent and silent.

    async def check_binding(self) -> bool:
        """Still backed by the CURRENT binding (enabled AND same token)? Otherwise drop the connection.
        Called on every control frame and by the server's periodic watcher, so a rotated or disabled
        token cannot keep an established connection alive."""
        if self.closing:
            return False
        if self.sv.bindings.session_valid(self.device_id, self.cred):
            return True
        self.closing = True
        log.info("device=%s binding revoked or token rotated; closing", self.device_id)
        await self._cancel_active("binding revoked")
        try:
            await asyncio.wait_for(self.ws.send(P.error("auth")), self.cfg.send_timeout_s)
        except Exception:
            pass
        await self.ws.close(code=4401, reason="binding revoked")
        return False

    async def _start(self, turn_id: int) -> None:
        if turn_id <= self.last_turn:
            raise P.ProtocolError("out_of_order", "turn id must increase")
        if self.active:                               # a new press supersedes the old turn
            await self._cancel_active("superseded")
        if not self.sv.try_acquire_turn():
            self.last_turn = turn_id
            await self._send_raw(P.error("busy", turn_id))
            return
        self.last_turn = turn_id
        turn = Turn(turn_id)
        self.active = turn
        self.sv.stats.turns += 1
        turn.feeder = asyncio.create_task(self._feeder(turn), name=f"feeder-{turn_id}")
        limit = (P.MAX_UTTERANCE_MS + self.cfg.upload_grace_ms) / 1000
        turn.deadline = asyncio.get_running_loop().call_later(
            limit, lambda: asyncio.create_task(self._upload_deadline(turn)))

    async def _upload_deadline(self, turn: Turn) -> None:
        if self._live(turn) and turn.state == "recording":
            await self._fail(turn, "too_long", "upload never finished")

    async def _audio(self, frame: P.UpFrame) -> None:
        turn = self.active
        if not self.ready:
            raise P.ProtocolError("out_of_order", "hello required first")
        if turn is None or frame.turn != turn.id:
            if frame.turn <= self.last_turn:          # trailing frames of a cancelled/finished turn
                self.sv.stats.stale_audio_dropped += 1
                return
            raise P.ProtocolError("out_of_order", "audio before start")
        if turn.state != "recording":
            raise P.ProtocolError("out_of_order", "audio after end")
        if frame.seq != turn.next_seq:
            raise P.ProtocolError("out_of_order", "audio sequence")
        if turn.bytes + len(frame.pcm) > P.MAX_UP_BYTES:
            await self._fail(turn, "too_long", "utterance exceeds 20 s")
            return
        turn.next_seq += 1
        turn.bytes += len(frame.pcm)
        try:
            turn.feed_q.put_nowait(frame.pcm)
        except asyncio.QueueFull:
            await self._fail(turn, "queue_full", "audio queue overflow")

    async def _end(self, ctl: P.Control) -> None:
        turn = self.active
        if turn is None or turn.id != ctl.turn:
            if ctl.turn <= self.last_turn:
                return                                 # end for a cancelled turn: ignore
            raise P.ProtocolError("out_of_order", "end before start")
        if turn.state != "recording":
            raise P.ProtocolError("out_of_order", "duplicate end")
        if ctl.bytes != turn.bytes:
            raise P.ProtocolError("bad_message", "byte count mismatch")
        if turn.deadline:
            turn.deadline.cancel()
        if turn.bytes < P.MIN_UP_BYTES:
            await self._fail(turn, "asr_empty", "audio too short")
            return
        if turn.feed_q.full():                         # no room for the end-of-audio sentinel
            await self._fail(turn, "queue_full", "audio queue overflow")
            return
        turn.feed_q.put_nowait(None)
        turn.state = "processing"
        turn.pipeline = asyncio.create_task(self._pipeline(turn), name=f"turn-{turn.id}")

    # ------------------------------------------------------------------ pipeline
    def _live(self, turn: Turn) -> bool:
        return not turn.cancelled and self.active is turn

    async def _feeder(self, turn: Turn) -> None:
        try:
            turn.asr = await self.sv.asr.open()
            while True:
                chunk = await turn.feed_q.get()
                if chunk is None:
                    return
                await turn.asr.feed(chunk)
        except ProviderError as exc:
            if self._live(turn):
                await self._fail(turn, exc.code, exc.detail, from_task=turn.feeder)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("device=%s asr feeder crashed", self.device_id)
            if self._live(turn):
                await self._fail(turn, "internal", from_task=turn.feeder)

    async def _pipeline(self, turn: Turn) -> None:
        cfg = self.cfg
        try:
            await turn.feeder                          # all audio delivered to the ASR stream
            if not self._live(turn):
                return
            try:
                text = await asyncio.wait_for(turn.asr.finish(), cfg.asr_timeout_s)
            except asyncio.TimeoutError as exc:
                raise TurnFailure("asr_timeout") from exc
            if not self._live(turn):
                return
            if not text.strip():
                raise TurnFailure("asr_empty")
            await self._send(turn, P.transcript(turn.id, text, True))

            try:
                reply = await asyncio.wait_for(
                    self.sv.backend.voice_turn(self.device_id, turn.uid, text), cfg.brain_timeout_s)
            except asyncio.TimeoutError as exc:
                raise TurnFailure("brain_timeout") from exc
            except BackendError as exc:
                raise TurnFailure(exc.code, str(exc)) from exc
            if not self._live(turn):
                return
            mood = reply.mood if reply.mood in P.MOODS else "neutral"
            await self._send(turn, P.reply_text(turn.id, reply.text))
            await self._send(turn, P.mood(turn.id, mood))
            spoken = speech_text.prepare(reply.text, text)
            if not spoken:
                raise TurnFailure("tts_failed", "nothing speakable")
            await self._speak(turn, spoken, mood)
            if self._live(turn):
                turn.state = "done"
                self.active = None
                self.sv.stats.completed += 1
                self.sv.release_turn()
        except StaleTurn:
            return                                     # cancelled/superseded: nothing more to say
        except TurnFailure as exc:
            if self._live(turn):
                await self._fail(turn, exc.code, exc.detail, from_task=asyncio.current_task())
        except ProviderError as exc:
            if self._live(turn):
                await self._fail(turn, exc.code, exc.detail, from_task=asyncio.current_task())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("device=%s turn %s crashed", self.device_id, turn.id)
            if self._live(turn):
                await self._fail(turn, "internal", from_task=asyncio.current_task())
        finally:
            await self._close_asr(turn)

    async def _speak(self, turn: Turn, spoken: str, mood: str) -> None:
        cfg = self.cfg
        stream = self.sv.tts.synthesize(spoken, mood)
        started = False
        seq = total = 0
        buf = bytearray()
        t0 = time.monotonic()
        t_audio = 0.0
        iterator = stream.__aiter__()
        try:
            while True:
                limit = cfg.tts_total_timeout_s - (time.monotonic() - t0)
                if not started:
                    limit = min(limit, cfg.tts_first_audio_timeout_s)
                if limit <= 0:
                    raise TurnFailure("tts_timeout")
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), limit)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise TurnFailure("tts_timeout") from exc
                if not self._live(turn):
                    return
                buf += chunk
                if not started and len(buf) >= 2:      # first audio goes out immediately (latency)
                    first = bytes(buf[:min(len(buf) & ~1, P.MAX_DOWN_PAYLOAD)])
                    del buf[:len(first)]
                    await self._send(turn, P.audio_start(turn.id))
                    started = True
                    t_audio = time.monotonic()
                    await self._send_bin(turn, P.down_frame(turn.id, seq, first))
                    seq += 1
                    total += len(first)
                while len(buf) >= P.MAX_DOWN_PAYLOAD:
                    piece = bytes(buf[:P.MAX_DOWN_PAYLOAD])
                    del buf[:P.MAX_DOWN_PAYLOAD]
                    await self._pace(t_audio, total, len(piece))
                    await self._send_bin(turn, P.down_frame(turn.id, seq, piece))
                    seq += 1
                    total += len(piece)
            if len(buf) % 2:
                buf = buf[:-1]
            if buf:
                if not started:
                    await self._send(turn, P.audio_start(turn.id))
                    started = True
                    t_audio = time.monotonic()
                await self._pace(t_audio, total, len(buf))
                await self._send_bin(turn, P.down_frame(turn.id, seq, bytes(buf)))
                seq += 1
                total += len(buf)
            if not started or total == 0:
                raise TurnFailure("tts_failed", "provider returned no audio")
            if self._live(turn):
                await self._send(turn, P.audio_end(turn.id, total))
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose:
                await asyncio.shield(aclose())

    async def _pace(self, t_audio: float, sent: int, n: int) -> None:
        """Flow control against the device queue (16 KiB ~ 340 ms): never run more than `pace_lead_ms`
        ahead of the playback clock, however fast the TTS source yields. The sleep lives in the turn's own
        task, so a cancel/new press interrupts it immediately."""
        lead = self.cfg.pace_lead_ms
        if lead <= 0:
            return
        rate = self.cfg.pace_rate_bytes_s
        allowed = (time.monotonic() - t_audio) * rate + rate * lead / 1000.0
        if sent + n > allowed:
            await asyncio.sleep((sent + n - allowed) / rate)

    # ------------------------------------------------------------------ sending
    async def _send_raw(self, data: str | bytes) -> None:
        try:
            await asyncio.wait_for(self.ws.send(data), self.cfg.send_timeout_s)
        except asyncio.TimeoutError:
            # A client that cannot drain control frames is dead weight: drop the connection.
            self.closing = True
            await self.ws.close(code=1011, reason="send timeout")
        except Exception:
            self.closing = True

    async def _send(self, turn: Turn, text: str) -> None:
        if not self._live(turn):
            raise StaleTurn
        await self._send_raw(text)

    async def _send_bin(self, turn: Turn, frame: bytes) -> None:
        if not self._live(turn):
            raise StaleTurn
        await self._send_raw(frame)

    # --------------------------------------------------------------- termination
    async def _fail(self, turn: Turn, code: str, detail: str = "", from_task=None) -> None:
        if not self._live(turn):
            return
        self.sv.stats.failed[code] = self.sv.stats.failed.get(code, 0) + 1
        log.info("device=%s turn=%s failed: %s %s", self.device_id, turn.id, code, detail)
        self._teardown(turn, keep=from_task)
        await self._send_raw(P.error(code, turn.id, detail))
        await self._close_asr(turn)

    async def _cancel_active(self, why: str) -> None:
        turn = self.active
        if turn is None:
            return
        log.info("device=%s turn=%s cancelled (%s)", self.device_id, turn.id, why)
        self._teardown(turn)
        tasks = [t for t in (turn.feeder, turn.pipeline) if t and not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_asr(turn)

    @staticmethod
    async def _close_asr(turn: Turn) -> None:
        if turn.asr is not None:
            try:
                await asyncio.shield(turn.asr.close())
            except Exception:          # closing a provider stream must never mask the turn result
                pass

    def _teardown(self, turn: Turn, keep=None) -> None:
        if turn.state != "done":
            self.sv.release_turn()
        turn.cancelled = True
        turn.state = "cancelled"
        if self.active is turn:
            self.active = None
        if turn.deadline:
            turn.deadline.cancel()
        for task in (turn.feeder, turn.pipeline):
            if task and task is not keep and not task.done():
                task.cancel()
