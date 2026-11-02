"""End-to-end gateway tests over a real loopback WebSocket with FIXTURE providers/backend.

SIMULATED: these prove protocol/lifecycle behaviour only. They say nothing about the live
DashScope services, the live unified core, or a physical device."""
import asyncio
import json
import logging

import websockets

from passport_gateway import protocol as P
from passport_gateway.backend import BackendError
from passport_gateway.config import Config
from passport_gateway.providers import UnconfiguredAsr, UnconfiguredTts
from passport_gateway.providers.base import ProviderError
from passport_gateway.server import GatewayServer

from .harness import DEVICE, TOKEN, GatewayCase, silence, tone


def is_end(msg):
    return msg["type"] in ("audio_end", "error")


class AuthTests(GatewayCase):
    async def rejected(self, **kw):
        with self.assertRaises(websockets.InvalidStatus) as cm:
            await self.connect(**kw)
        return cm.exception.response.status_code

    async def test_bad_credentials_get_identical_401(self):
        self.assertEqual(await self.rejected(token="wrong"), 401)
        self.assertEqual(await self.rejected(device="nobody"), 401)
        self.assertEqual(await self.rejected(token=""), 401)
        self.assertEqual(await self.rejected(device="bad id!"), 401)
        self.write_bindings({DEVICE: TOKEN}, enabled=False)
        self.assertEqual(await self.rejected(), 401)

    async def test_missing_subprotocol_is_rejected(self):
        with self.assertRaises(websockets.InvalidStatus) as cm:
            await websockets.connect(self.url, additional_headers={"X-Device-Id": DEVICE,
                                     "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(cm.exception.response.status_code, 400)

    async def test_repeated_failures_are_throttled(self):
        codes = [await self.rejected(token="x") for _ in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)
        self.assertEqual(await self.rejected(), 429)         # even the right token waits out the window

    async def test_owner_and_identity_spoofing_is_ignored(self):
        """Clients cannot nominate an owner/session/person_id/device: the gateway forwards only the
        device id bound to the authenticated token, plus the transcript."""
        ws = await self.connect(headers={"X-Owner": "qq:12345", "X-Session": "evil", "X-Person-Id": "p1"})
        await ws.send(P.dumps({"v": 1, "type": "start", "turn": 1, "hz": 16000, "ch": 1, "fmt": "pcm_s16le",
                               "device_id": "someone-else", "owner": "qq:999", "session": "s", "person_id": "x",
                               "endpoint": "wss://evil.example/v1"}))
        pcm = tone(800)
        for seq, off in enumerate(range(0, len(pcm), 3200)):
            await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, seq) + pcm[off:off + 3200])
        await ws.send(self.end_msg(1, len(pcm)))
        await self.collect(ws, is_end)
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(set(self.backend.requests[0]), {"device_id", "turn_uid", "text"})
        self.assertEqual(self.backend.requests[0]["device_id"], DEVICE)

    async def test_binding_revocation_takes_effect_mid_session(self):
        ws = await self.connect()
        self.write_bindings({})
        await ws.send(self.start_msg(1))
        msg = await self.recv_json(ws)
        self.assertEqual((msg["type"], msg["code"]), ("error", "auth"))
        with self.assertRaises(websockets.ConnectionClosed) as cm:
            await ws.recv()
        self.assertEqual(cm.exception.rcvd.code, 4401)

    async def test_token_never_logged(self):
        # Server-side loggers only (the test client's own library logs are not under our control).
        with self.assertLogs("passport", level=logging.DEBUG) as logs:
            logging.getLogger("passport.test").info("sentinel")
            with self.assertRaises(websockets.InvalidStatus):
                await self.connect(token="secret-token-value-xyz")
            ws = await self.connect()
            await ws.close()
            await asyncio.sleep(0.05)
        text = "\n".join(logs.output)
        self.assertNotIn("secret-token-value-xyz", text)
        self.assertNotIn(TOKEN, text)

    async def test_token_rotation_revokes_an_established_connection(self):
        """Regression: the handshake verified the old token, but an open connection used to keep working
        after token_sha256 was rotated. It must be refused at the next turn AND dropped by the watcher."""
        ws = await self.connect()
        self.write_bindings({DEVICE: "a-brand-new-rotated-token-0123456789"})
        await ws.send(self.start_msg(1))
        msg = await self.recv_json(ws)
        self.assertEqual((msg["type"], msg["code"]), ("error", "auth"))
        with self.assertRaises(websockets.ConnectionClosed) as cm:
            await ws.recv()
        self.assertEqual(cm.exception.rcvd.code, 4401)
        self.assertEqual(self.backend.requests, [])
        # The old token no longer connects; the new one does and gets a fresh, working session.
        with self.assertRaises(websockets.InvalidStatus):
            await self.connect()
        fresh = await self.connect(token="a-brand-new-rotated-token-0123456789")
        await self.speak(fresh, 1, tone(600))
        texts, _ = await self.collect(fresh, is_end)
        self.assertEqual(texts[-1]["type"], "audio_end")

    async def test_watcher_drops_an_idle_connection_after_rotation(self):
        ws = await self.connect()
        self.write_bindings({DEVICE: "another-rotated-token-abcdef012345"})
        msg = await self.recv_json(ws)                           # no message sent: the watcher acts alone
        self.assertEqual((msg["type"], msg["code"]), ("error", "auth"))
        with self.assertRaises(websockets.ConnectionClosed) as cm:
            await asyncio.wait_for(ws.recv(), 3)
        self.assertEqual(cm.exception.rcvd.code, 4401)

    async def test_rotation_during_a_turn_cancels_it(self):
        self.backend.gate = asyncio.Event()
        ws = await self.connect()
        await self.speak(ws, 1, tone(600))
        first = await self.recv_json(ws)
        self.assertEqual(first["type"], "transcript")
        self.write_bindings({DEVICE: "rotated-while-thinking-0123456789"})
        with self.assertRaises(websockets.ConnectionClosed):
            while True:
                msg = await asyncio.wait_for(ws.recv(), 3)
                self.assertNotEqual(json.loads(msg)["type"], "reply_text")
        self.backend.gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_same_device_reconnect_replaces_old_connection(self):
        first = await self.connect()
        second = await self.connect()
        with self.assertRaises(websockets.ConnectionClosed) as cm:
            await first.recv()
        self.assertEqual(cm.exception.rcvd.code, 4409)
        await second.send(self.start_msg(1))                  # newest connection still works

    async def test_session_limit(self):
        self.write_bindings({f"d{i}": TOKEN for i in range(6)})
        for i in range(4):
            await self.connect(device=f"d{i}")
        with self.assertRaises(websockets.InvalidStatus) as cm:
            await self.connect(device="d4")
        self.assertEqual(cm.exception.response.status_code, 503)


class TurnTests(GatewayCase):
    async def test_happy_path_streams_audio_in_order(self):
        ws = await self.connect()
        pcm = tone(1200)
        await self.speak(ws, 1, pcm)
        texts, frames = await self.collect(ws, is_end)
        self.assertEqual([t["type"] for t in texts],
                         ["transcript", "reply_text", "mood", "audio_start", "audio_end"])
        self.assertTrue(all(t["turn"] == 1 for t in texts))
        self.assertEqual(texts[0]["text"], "今天天气怎么样")
        self.assertTrue(texts[0]["final"])
        self.assertEqual(texts[2]["mood"], "pleased")
        # Frames are well-formed, in sequence, for turn 1, and add up to audio_end.bytes.
        total = 0
        for seq, frame in enumerate(frames):
            kind, flags, turn, fseq = P.AUDIO_HDR.unpack_from(frame)
            self.assertEqual((kind, flags, turn, fseq), (P.KIND_DOWN, 0, 1, seq))
            self.assertLessEqual(len(frame) - 8, P.MAX_DOWN_PAYLOAD)
            self.assertEqual((len(frame) - 8) % 2, 0)
            total += len(frame) - 8
        self.assertEqual(total, texts[-1]["bytes"])
        self.assertGreater(len(frames), 3)
        # The backend got the transcript once; original reply kept for subtitles, speech normalized.
        self.assertEqual(self.backend.requests[0]["text"], "今天天气怎么样")
        self.assertIn("**今天**", texts[1]["text"])
        spoken, mood = self.tts.calls[0]
        self.assertNotIn("**", spoken)
        self.assertNotIn("http", spoken)
        self.assertEqual(mood, "pleased")
        await asyncio.sleep(0.05)                              # completion is counted right after audio_end
        self.assertEqual(self.gw.sv.stats.completed, 1)

    async def test_multiple_turns_on_one_connection(self):
        ws = await self.connect()
        for turn in (1, 2, 3):
            await self.speak(ws, turn, tone(600))
            texts, _ = await self.collect(ws, is_end)
            self.assertEqual(texts[-1]["type"], "audio_end")
            self.assertEqual(texts[-1]["turn"], turn)
        self.assertEqual(len({r["turn_uid"] for r in self.backend.requests}), 3)

    async def test_silence_and_too_short_report_asr_empty(self):
        ws = await self.connect()
        await self.speak(ws, 1, silence(900))
        texts, _ = await self.collect(ws, is_end)
        self.assertEqual((texts[-1]["code"], texts[-1]["turn"]), ("asr_empty", 1))
        self.assertEqual(self.backend.requests, [])           # nothing reaches the brain
        await self.speak(ws, 2, tone(100))                    # < 200 ms
        texts, _ = await self.collect(ws, is_end)
        self.assertEqual(texts[-1]["code"], "asr_empty")

    async def test_cancel_during_tts_stops_audio_and_closes_provider(self):
        self.tts.gate = asyncio.Event()
        ws = await self.connect()
        await self.speak(ws, 1, tone(800))
        seen = []
        while True:                                            # until the first audio frame arrives
            raw = await self.recv(ws)
            seen.append(raw)
            if isinstance(raw, bytes):
                break
        await ws.send(self.cancel_msg(1))
        await asyncio.sleep(0.1)
        self.tts.gate.set()                                    # a non-cancelled stream would continue now
        await asyncio.sleep(0.3)
        with self.assertRaises(asyncio.TimeoutError):          # nothing more for the cancelled turn
            while True:
                late = await self.recv(ws, 0.4)
                self.assertIsInstance(late, bytes)             # allowed only: frames already in flight
                self.assertLess(len(seen), 40)
                seen.append(late)
        self.assertFalse(any(isinstance(m, str) and json.loads(m)["type"] == "audio_end" for m in seen))
        self.assertGreaterEqual(self.tts.streams_closed, 1)
        self.assertLess(self.tts.chunks_produced, 8)           # the generator did not run to completion

    async def test_new_start_supersedes_and_old_results_never_arrive(self):
        self.backend.gate = asyncio.Event()                    # turn 1 stuck in the brain
        ws = await self.connect()
        await self.speak(ws, 1, tone(800))
        first = await self.recv_json(ws)                       # turn 1's transcript, sent pre-cancel
        self.assertEqual((first["type"], first["turn"]), ("transcript", 1))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.backend.requests), 1)
        await ws.send(self.cancel_msg(1))                      # interrupt, then press again
        await self.speak(ws, 2, tone(800))
        await asyncio.sleep(0.2)
        self.backend.gate.set()                                # both brain calls now complete
        texts, _ = await self.collect(ws, lambda m: m["type"] == "audio_end", timeout=6)
        self.assertTrue(all(t["turn"] == 2 for t in texts), texts)   # no turn-1 transcript/reply/audio

    async def test_start_without_cancel_also_supersedes(self):
        self.backend.gate = asyncio.Event()
        ws = await self.connect()
        await self.speak(ws, 1, tone(800))
        first = await self.recv_json(ws)
        self.assertEqual((first["type"], first["turn"]), ("transcript", 1))
        await self.speak(ws, 2, tone(800))
        await asyncio.sleep(0.1)
        self.backend.gate.set()
        texts, _ = await self.collect(ws, lambda m: m["type"] == "audio_end", timeout=6)
        self.assertTrue(all(t["turn"] == 2 for t in texts))

    async def test_disconnect_cancels_everything(self):
        self.tts.gate = asyncio.Event()
        ws = await self.connect()
        await self.speak(ws, 1, tone(800))
        while not isinstance(await self.recv(ws), bytes):
            pass
        await ws.close()
        await asyncio.sleep(0.2)
        self.assertEqual(self.gw.sv._active_turns, 0)
        self.assertEqual(self.gw.sessions, {})
        self.assertGreaterEqual(self.tts.streams_closed, 1)


class ProtocolViolationTests(GatewayCase):
    async def expect_error(self, ws, code):
        msg = await self.recv_json(ws)
        self.assertEqual((msg["type"], msg["code"]), ("error", code), msg)
        return msg

    async def test_messages_before_hello_are_rejected(self):
        ws = await self.connect(hello=False)
        await ws.send(self.start_msg(1))
        await self.expect_error(ws, "out_of_order")

    async def test_audio_without_start_is_out_of_order_but_stale_audio_is_silent(self):
        ws = await self.connect()
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 5, 0) + b"\0\0")
        await self.expect_error(ws, "out_of_order")
        # Trailing frames of an already cancelled turn are dropped without an error.
        await ws.send(self.start_msg(1))
        await ws.send(self.cancel_msg(1))
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 0) + b"\0\0")
        await asyncio.sleep(0.1)
        self.assertEqual(self.gw.sv.stats.stale_audio_dropped, 1)
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv(ws, 0.2)

    async def test_turn_ids_must_increase(self):
        ws = await self.connect()
        await ws.send(self.start_msg(5))
        await ws.send(self.cancel_msg(5))
        await ws.send(self.start_msg(5))
        await self.expect_error(ws, "out_of_order")
        await ws.send(self.start_msg(4))
        await self.expect_error(ws, "out_of_order")

    async def test_sequence_gap_fails_the_turn(self):
        ws = await self.connect()
        await ws.send(self.start_msg(1))
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 0) + b"\0\0")
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 2) + b"\0\0")
        msg = await self.expect_error(ws, "out_of_order")
        self.assertEqual(msg["turn"], 1)

    async def test_end_byte_count_mismatch(self):
        ws = await self.connect()
        await ws.send(self.start_msg(1))
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 0) + tone(300)[:3200])
        await ws.send(self.end_msg(1, 999))
        await self.expect_error(ws, "bad_message")

    async def test_duplicate_end_and_audio_after_end(self):
        ws = await self.connect()
        self.backend.gate = asyncio.Event()
        pcm = tone(500)
        await self.speak(ws, 1, pcm)
        await ws.send(self.end_msg(1, len(pcm)))
        await self.expect_error(ws, "out_of_order")
        self.backend.gate.set()

    async def test_oversize_frames_are_rejected(self):
        ws = await self.connect()
        await ws.send(P.dumps({"v": 1, "type": "hello", "fw": "x" * 600}))
        await self.expect_error(ws, "too_large")
        await ws.send(self.start_msg(1))
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 0) + b"\0" * (P.MAX_UP_PAYLOAD + 2))
        await self.expect_error(ws, "too_large")
        # Larger than the transport limit: the server drops the connection.
        await ws.send(b"\0" * 9000)
        with self.assertRaises(websockets.ConnectionClosed):
            await ws.recv()

    async def test_malformed_and_unknown_control_frames(self):
        ws = await self.connect()
        for bad in ("not json", '{"v":1,"type":"launch_missiles"}', '{"v":1,"type":"start","turn":"x"}'):
            await ws.send(bad)
            await self.expect_error(ws, "bad_message")
        await ws.send(b"\x09\x00\x00\x00\x00\x00\x00\x00\x00\x00")           # unknown binary kind
        await self.expect_error(ws, "bad_message")

    async def test_repeated_violations_close_the_connection(self):
        ws = await self.connect()
        for _ in range(5):
            await ws.send("garbage")
        with self.assertRaises(websockets.ConnectionClosed) as cm:
            for _ in range(10):
                await ws.recv()
        self.assertEqual(cm.exception.rcvd.code, 1008)


class LimitTests(GatewayCase):
    async def test_twenty_second_limit_enforced_server_side(self):
        ws = await self.connect()
        await ws.send(self.start_msg(1))
        chunk = b"\x10\x00" * (P.MAX_UP_PAYLOAD // 2)
        code = None
        for seq in range(P.MAX_UP_BYTES // P.MAX_UP_PAYLOAD + 2):
            await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, seq) + chunk)
            await asyncio.sleep(0)
        while code is None:
            msg = await self.recv_json(ws)
            if msg["type"] == "error":
                code = msg["code"]
        self.assertEqual(code, "too_long")
        self.assertEqual(self.backend.requests, [])
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_stalled_upload_hits_wall_clock_deadline(self):
        P_orig = P.MAX_UTTERANCE_MS
        P.MAX_UTTERANCE_MS = 0
        self.cfg.upload_grace_ms = 150
        try:
            ws = await self.connect()
            await ws.send(self.start_msg(1))
            msg = await self.recv_json(ws)
            self.assertEqual((msg["code"], msg["turn"]), ("too_long", 1))
        finally:
            P.MAX_UTTERANCE_MS = P_orig

    async def test_audio_queue_overflow_is_reported(self):
        self.asr.feed_delay = 5.0                              # provider cannot keep up
        ws = await self.connect()
        await ws.send(self.start_msg(1))
        for seq in range(80):                                  # > FEED_QUEUE_DEPTH (64)
            await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, seq) + b"\x10\x00" * 160)
        code = None
        while code is None:
            msg = await self.recv_json(ws)
            code = msg.get("code")
        self.assertEqual(code, "queue_full")
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_global_active_turn_cap_returns_busy(self):
        self.cfg.max_active_turns = 1
        self.gw.sv.max_active_turns = 1
        self.write_bindings({DEVICE: TOKEN, "dev-two": TOKEN})
        self.backend.gate = asyncio.Event()
        a, b = await self.connect(), await self.connect(device="dev-two")
        await self.speak(a, 1, tone(500))
        await asyncio.sleep(0.2)
        await b.send(self.start_msg(1))
        msg = await self.recv_json(b)
        self.assertEqual((msg["type"], msg["code"]), ("error", "busy"))
        self.backend.gate.set()


class ProviderFailureTests(GatewayCase):
    async def run_turn(self, ws, turn=1):
        await self.speak(ws, turn, tone(600))
        return await self.collect(ws, is_end)

    async def test_asr_failure_and_timeout(self):
        ws = await self.connect()
        self.asr.fail_finish = "asr_failed"
        texts, _ = await self.run_turn(ws, 1)
        self.assertEqual(texts[-1]["code"], "asr_failed")
        self.asr.fail_finish = ""
        self.asr.finish_delay = 5
        self.cfg.asr_timeout_s = 0.2
        texts, _ = await self.run_turn(ws, 2)
        self.assertEqual(texts[-1]["code"], "asr_timeout")

    async def test_asr_feed_failure_is_reported_early(self):
        self.asr.fail_feed = True
        ws = await self.connect()
        await ws.send(self.start_msg(1))
        await ws.send(P.AUDIO_HDR.pack(P.KIND_UP, 0, 1, 0) + b"\x10\x00" * 160)
        msg = await self.recv_json(ws)
        self.assertEqual((msg["type"], msg["code"], msg["turn"]), ("error", "asr_failed", 1))

    async def test_backend_errors_map_to_wire_codes(self):
        ws = await self.connect()
        for turn, (err, code) in enumerate([
                (BackendError("backend_unbound", "voice_unbound"), "backend_unbound"),
                (BackendError("brain_failed", "x"), "brain_failed"),
                (BackendError("brain_timeout"), "brain_timeout")], start=1):
            self.backend.error = err
            texts, _ = await self.run_turn(ws, turn)
            self.assertEqual(texts[-1]["code"], code)
            self.assertNotIn("reply_text", [t["type"] for t in texts])      # no fake reply

    async def test_brain_timeout(self):
        self.cfg.brain_timeout_s = 0.2
        self.backend.gate = asyncio.Event()
        ws = await self.connect()
        texts, _ = await self.run_turn(ws)
        self.assertEqual(texts[-1]["code"], "brain_timeout")

    async def test_tts_failure_keeps_text_but_reports_error(self):
        self.tts.fail = "start"
        ws = await self.connect()
        texts, frames = await self.run_turn(ws)
        self.assertEqual([t["type"] for t in texts], ["transcript", "reply_text", "mood", "error"])
        self.assertEqual(texts[-1]["code"], "tts_failed")
        self.assertEqual(frames, [])

    async def test_tts_midstream_failure_and_first_audio_timeout(self):
        self.tts.fail_after_chunks = 2
        ws = await self.connect()
        texts, frames = await self.run_turn(ws, 1)
        self.assertEqual(texts[-1]["code"], "tts_failed")
        self.assertNotIn("audio_end", [t["type"] for t in texts])       # never claims completion
        self.tts.fail_after_chunks = -1
        self.tts.first_delay = 5
        self.cfg.tts_first_audio_timeout_s = 0.2
        texts, _ = await self.run_turn(ws, 2)
        self.assertEqual(texts[-1]["code"], "tts_timeout")

    async def test_unconfigured_providers_fail_clearly_without_fixtures(self):
        cfg = Config(host="127.0.0.1", port=0, allow_plaintext_loopback=True, test_mode=False,
                     bindings_path=str(self.bindings_path), sf_api_key="")
        gw = GatewayServer(cfg, backend=self.backend)
        self.assertIsInstance(gw.sv.asr, UnconfiguredAsr)
        self.assertIsInstance(gw.sv.tts, UnconfiguredTts)
        await gw.start()
        try:
            ws = await websockets.connect(f"ws://127.0.0.1:{gw.port}/v1/voice",
                                          additional_headers={"X-Device-Id": DEVICE, "Authorization": f"Bearer {TOKEN}"},
                                          subprotocols=[P.SUBPROTOCOL])
            await ws.send(P.dumps({"v": 1, "type": "hello", "fw": "0.1.0"}))
            await ws.recv()
            await ws.send(self.start_msg(1))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
            self.assertEqual((msg["type"], msg["code"], msg["turn"]), ("error", "provider_unconfigured", 1))
            self.assertEqual(self.backend.requests, [])
            await ws.close()
        finally:
            await gw.stop()

    async def test_diagnostics_endpoint_reports_actual_providers_on_loopback(self):
        import urllib.request
        loop = asyncio.get_running_loop()
        body = await loop.run_in_executor(None, lambda: urllib.request.urlopen(
            f"http://127.0.0.1:{self.gw.port}/diagnostics", timeout=3).read())
        diag = json.loads(body)
        self.assertEqual(diag["asr"]["model"], "fixture-asr")
        self.assertEqual(diag["asr"]["mode"], "fixture")
        self.assertEqual(diag["tts"]["actual_model"], "fixture-tts")
        self.assertTrue(diag["test_mode"])
        self.assertNotIn("api_key", body.decode().lower())
