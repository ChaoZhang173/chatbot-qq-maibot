"""Downlink flow control against the ESP playback queue budget (SIMULATED device queue).

The firmware queue is PP_DOWN_Q_BYTES = 16384 B (~341 ms of 24 kHz s16 mono). A real TTS stream
delivered a 3.24 s answer in ~0.43 s after first audio, i.e. ~7x faster than real time, so an unpaced
sender hands the device more than its queue holds. The simulated sink models the device exactly:
a leaky bucket that drains at the playback rate once the first frame arrives."""
import asyncio
import time

import websockets

from passport_gateway import protocol as P

from .harness import GatewayCase, tone

QUEUE_BYTES = 16384          # main/pp_audio.h PP_DOWN_Q_BYTES
PLAY_RATE = 48000            # bytes/s at 24 kHz s16 mono


class Sink:
    """Leaky-bucket model of the ESP downlink queue (arrival = frame received, drain = playback)."""

    def __init__(self, rate):
        self.rate, self.level, self.peak, self.last, self.frames, self.bytes = rate, 0.0, 0.0, None, 0, 0

    def arrive(self, n):
        now = time.monotonic()
        if self.last is not None:
            self.level = max(0.0, self.level - (now - self.last) * self.rate)
        self.last = now
        self.level += n
        self.peak = max(self.peak, self.level)
        self.frames += 1
        self.bytes += n


class PacingTests(GatewayCase):
    SCALE = 8                # run the clock 8x faster so a ~4 s answer takes ~0.5 s of wall time

    def setUp(self):
        self.cfg_overrides = {"pace_lead_ms": 240, "pace_rate_bytes_s": PLAY_RATE * self.SCALE}

    async def collect_audio(self, ws, sink):
        while True:
            raw = await self.recv(ws, 8)
            if isinstance(raw, bytes):
                sink.arrive(len(raw) - P.AUDIO_HDR_LEN)
            elif '"audio_end"' in raw or '"error"' in raw:
                return raw

    async def run_answer(self, ms_per_char=400):
        self.tts.ms_per_char = ms_per_char                 # fast producer: yields the whole answer instantly
        ws = await self.connect()
        sink = Sink(PLAY_RATE * self.SCALE)
        await self.speak(ws, 1, tone(800))
        final = await self.collect_audio(ws, sink)
        return sink, final

    async def test_ordinary_answer_stays_inside_the_queue_budget_with_pacing(self):
        sink, final = await self.run_answer()
        self.assertIn('"audio_end"', final)                 # not a queue_full result
        self.assertGreater(sink.bytes, 3 * PLAY_RATE)      # a >3 s answer
        budget = QUEUE_BYTES * self.SCALE
        self.assertLess(sink.peak, budget * 0.9, f"peak {sink.peak:.0f} of {budget}")
        self.assertGreater(sink.peak, budget * 0.4)        # and it really did run ahead (not trickling)

    async def test_negative_control_unpaced_stream_would_overflow(self):
        self.cfg.pace_lead_ms = 0                          # same source, pacing off
        sink, _ = await self.run_answer()
        self.assertGreater(sink.peak, QUEUE_BYTES * self.SCALE * 2, "the control must show the problem")

    async def test_press_flushes_immediately_while_flow_control_waits(self):
        self.cfg.pace_rate_bytes_s = PLAY_RATE             # real time: the sender is parked in _pace()
        self.tts.ms_per_char = 400
        ws = await self.connect()
        await self.speak(ws, 1, tone(800))
        frames_before = 0
        while True:
            raw = await self.recv(ws, 5)
            if isinstance(raw, bytes):
                frames_before += 1
                if frames_before >= 4:
                    break
        t_cancel = time.monotonic()
        await ws.send(self.cancel_msg(1))
        await self.speak(ws, 2, tone(800), end=False)      # the next press starts recording at once
        late = 0
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), 0.2)
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, bytes):
                late += 1
                self.assertEqual(P.AUDIO_HDR.unpack_from(raw)[2], 1)
        # Only frames already on the wire when cancel was processed may still arrive, never a stream.
        self.assertLessEqual(late, 3, f"{late} stale frames after cancel")
        self.assertGreaterEqual(self.tts.streams_closed, 1)
        self.assertLess(self.tts.chunks_produced * 4800, 4 * 400 * 24 * 10)   # generator was abandoned early
        self.assertLess(time.monotonic() - t_cancel, 1.5)
