"""Real loopback WebSocket delivery with simulated providers and speaker receipts."""
import asyncio
import time
from passport_gateway import protocol as P
from .harness import DEVICE, GatewayCase, tone


class ReminderDeliveryTests(GatewayCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pending = True
        self.calls = []
        self.reported = asyncio.Event()
        self.prepare_gate = None
        async def reminder(op, device, **kw):
            self.calls.append((op, device, kw))
            if op == "claim":
                if not self.pending:
                    return {"job":None}
                self.pending = False
                return {"job":{"id":"test-reminder", "token":"lease-token", "expires_at":time.time()+300}}
            if op == "prepare":
                if self.prepare_gate:
                    await self.prepare_gate.wait()
                return {"reply":"小花，先伸个懒腰，我来叫你起床啦。", "mood":"gentle"}
            if op == "report":
                self.reported.set()
            return {"status":"ok"}
        self.backend.reminder = reminder

    async def connect_new(self):
        ws = await self.connect(hello=False)
        await ws.send(P.dumps({"v":1, "type":"hello", "fw":"0.3.1-shunhuahua"}))
        self.assertEqual((await self.recv_json(ws))["type"], "ready")
        return ws

    def outcome(self):
        return [kw["outcome"] for op, _, kw in self.calls if op == "report"]

    async def ready(self, ws, offer):
        await ws.send(P.dumps({"v":1,"type":"notify_ready","turn":offer["turn"]}))

    async def test_no_audio_before_accept_no_delivery_before_playback_receipt(self):
        ws = await self.connect_new()
        offer = await self.recv_json(ws)
        self.assertEqual(offer["type"], "notify")
        self.assertGreaterEqual(offer["turn"], 0x80000000)
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv(ws, 0.05)
        self.assertNotIn("arm", [c[0] for c in self.calls])
        await self.ready(ws, offer)
        messages, audio = await self.collect(ws, lambda m:m["type"]=="audio_end")
        self.assertTrue(audio)
        self.assertIn("reply_text", [m["type"] for m in messages])
        self.assertFalse(self.reported.is_set())
        await ws.send(P.dumps({"v":1,"type":"notify_done","turn":offer["turn"]}))
        await asyncio.wait_for(self.reported.wait(), 1)
        self.assertEqual(self.outcome(), ["delivered"])
        self.assertEqual(self.gw.sv._active_turns, 0)
        await self.speak(ws, 1, tone(500))
        messages, _ = await self.collect(ws, lambda m:m["type"] in ("audio_end","error"))
        self.assertEqual(messages[-1]["type"], "audio_end")
        self.assertEqual(len(self.backend.requests), 1)

    async def test_busy_defers_without_audio_or_arming(self):
        ws = await self.connect_new()
        offer = await self.recv_json(ws)
        await ws.send(P.dumps({"v":1,"type":"notify_busy","turn":offer["turn"]}))
        await asyncio.wait_for(self.reported.wait(), 1)
        self.assertEqual(self.outcome(), ["defer"])
        self.assertNotIn("arm", [c[0] for c in self.calls])
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_disconnect_after_speech_is_unknown_and_no_replay(self):
        ws = await self.connect_new()
        offer = await self.recv_json(ws)
        await self.ready(ws, offer)
        await self.collect(ws, lambda m:m["type"]=="audio_end")
        await ws.close()
        await asyncio.wait_for(self.reported.wait(), 2)
        self.assertEqual(self.outcome(), ["unknown"])
        ws2 = await self.connect_new()
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv(ws2, 0.2)
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_button_preempts_offer_and_continues_normal_voice(self):
        ws = await self.connect_new()
        await self.recv_json(ws)
        await self.speak(ws, 1, tone(500))
        messages, _ = await self.collect(ws, lambda m:m["type"] in ("audio_end","error"))
        self.assertEqual(messages[-1]["type"], "audio_end")
        await asyncio.wait_for(self.reported.wait(), 1)
        self.assertEqual(self.outcome(), ["defer"])
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_user_turn_wins_while_persona_reply_is_generating(self):
        self.prepare_gate = asyncio.Event()
        ws = await self.connect_new()
        for _ in range(20):
            if any(c[0] == "prepare" for c in self.calls):
                break
            await asyncio.sleep(0.02)
        await ws.send(self.start_msg(1))
        await asyncio.sleep(0.03)
        self.prepare_gate.set()
        await asyncio.wait_for(self.reported.wait(), 1)
        self.assertEqual(self.outcome(), ["defer"])
        self.assertEqual(self.gw.sessions[DEVICE].active.id, 1)
        await ws.send(self.cancel_msg(1))

    async def test_old_firmware_never_receives_extension(self):
        ws = await self.connect()
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv(ws, 0.2)
        self.assertEqual(self.calls, [])

    async def test_standard_first_voice_turn_registers_support_before_planner(self):
        self.pending = False
        ws = await self.connect(hello=False)
        await ws.send(P.dumps({"v":1, "type":"hello", "fw":"0.3.1"}))
        self.assertEqual((await self.recv_json(ws))["type"], "ready")
        self.assertEqual(self.calls[0][0], "seen")
        self.assertFalse(self.backend.requests)
        await self.speak(ws, 1, tone(500))
        messages, _ = await self.collect(ws, lambda m:m["type"] in ("audio_end", "error"))
        self.assertEqual(messages[-1]["type"], "audio_end")
        self.assertEqual(len(self.backend.requests), 1)

    async def test_busy_recording_refreshes_support_without_claiming_job(self):
        self.pending = False
        ws = await self.connect_new()
        await ws.send(self.start_msg(1))
        for _ in range(30):
            session = self.gw.sessions[DEVICE]
            if session.active:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(session.active.state, "recording")
        self.calls.clear()
        session.reminder_seen_at = float("-inf")
        await session._reminder_delivery()
        self.assertEqual([op for op, _, _ in self.calls], ["seen"])
        await ws.send(self.cancel_msg(1))
