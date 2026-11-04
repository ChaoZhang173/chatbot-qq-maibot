"""Delivery on the existing authenticated socket, with app-owned reservation and playback receipts."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from . import protocol as P, speech_text

log = logging.getLogger("passport.reminders")
NOTIFY_FW = ("0.3.0-shunhuahua", "0.3.0", "0.3.1-shunhuahua", "0.3.1")


class ReminderMixin:
    async def reminder_seen(self):
        if (self.fw not in NOTIFY_FW or self.closing
                or not getattr(self.sv.backend, "supports_reminders", True)):
            return
        async with self.reminder_seen_lock:
            if time.monotonic() - self.reminder_seen_at < 30:
                return
            try:
                await asyncio.wait_for(self.sv.backend.reminder("seen", self.device_id), 2)
                self.reminder_seen_at = time.monotonic()
            except Exception as exc:
                log.warning("device=%s reminder connection check failed (%s)", self.device_id, type(exc).__name__)

    def reminder_tick(self):
        if (not self.ready or self.closing or
                not getattr(self.sv.backend, "supports_reminders", True) or
                self.fw not in NOTIFY_FW or
                self.reminder_task and not self.reminder_task.done()):
            return
        self.reminder_task = asyncio.create_task(self._reminder_delivery(), name="passport-reminder")

    def reminder_control(self, ctl):
        turn = self.active
        if not turn or not turn.reminder or turn.id != ctl.turn:
            return
        if ctl.type in ("notify_ready", "notify_busy") and not turn.accepted.done():
            turn.accepted.set_result(ctl.type == "notify_ready")
        elif ctl.type == "notify_done" and turn.state == "awaiting_playback" and not turn.played.done():
            turn.played.set_result(True)

    async def _reminder_delivery(self):
        from .session import Turn, StaleTurn
        job = turn = None
        armed = False
        accepted = False
        try:
            await self.reminder_seen()
            if self.closing or self.active:
                return
            response = await self.sv.backend.reminder("claim", self.device_id)
            job = response.get("job")
            if job is None:
                return
            reply = await self.sv.backend.reminder("prepare", self.device_id, id=job["id"], token=job["token"])
            if self.closing or self.active or time.time() > job["expires_at"] or not await self.check_binding():
                return
            # No await between availability check and reservation. Client turn sequence stays independent.
            if self.active or not self.sv.try_acquire_turn():
                return
            loop = asyncio.get_running_loop()
            turn = Turn(0x80000000 | secrets.randbits(31), state="offering", reminder=True,
                        accepted=loop.create_future(), played=loop.create_future())
            turn.pipeline = asyncio.current_task()
            self.active = turn
            self.sv.stats.turns += 1
            await self._send(turn, P.dumps({"v":1, "type":"notify", "turn":turn.id}))
            accepted = await asyncio.wait_for(asyncio.shield(turn.accepted), 5)
            if not accepted or not self._live(turn):
                return
            # Commit the no-replay boundary BEFORE any speech; cancellation/rescheduling may have won during prepare.
            await self.sv.backend.reminder("arm", self.device_id, id=job["id"], token=job["token"])
            armed = True
            if not self._live(turn):
                raise StaleTurn
            spoken = speech_text.prepare(reply["reply"], "")
            if not spoken:
                raise ValueError("empty reminder speech")
            await self._send(turn, P.reply_text(turn.id, reply["reply"]))
            await self._send(turn, P.mood(turn.id, reply["mood"]))
            turn.state = "processing"
            await self._speak(turn, spoken, reply["mood"])
            await asyncio.wait_for(asyncio.shield(turn.played), 8)
            if self._live(turn):
                turn.state = "done"
                self.active = None
                self.sv.release_turn()
                self.sv.stats.completed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No reminder content or account credentials in logs.
            log.warning("device=%s reminder delivery interrupted (%s)", self.device_id, type(exc).__name__)
            if turn and self._live(turn):
                await self._fail(turn, "brain_failed", from_task=asyncio.current_task())
        finally:
            if turn and self.active is turn:
                if accepted and not self.closing:
                    await self._send_raw(P.error("busy", turn.id))
                self._teardown(turn, keep=asyncio.current_task())
            if job:
                played = turn and turn.played.done() and not turn.played.cancelled() and turn.played.result()
                outcome = "delivered" if played else "unknown" if armed else "defer"
                # A lost arm response can also mean armed; defer is rejected then, and the ledger expires to unknown.
                for attempt in range(2):
                    try:
                        await asyncio.wait_for(self.sv.backend.reminder("report", self.device_id,
                            id=job["id"], token=job["token"], outcome=outcome), 4)
                        break
                    except Exception:
                        if attempt:
                            log.warning("device=%s reminder receipt unavailable", self.device_id)
