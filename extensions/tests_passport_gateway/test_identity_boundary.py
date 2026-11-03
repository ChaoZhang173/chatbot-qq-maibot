"""Gateway side of per-person identity: the gateway authenticates a DEVICE credential and forwards only
{device_id, turn_uid, text}. It never carries, accepts or stores a profile / QQ / session / role."""
import asyncio
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from passport_gateway import tokens
from passport_gateway.backend import BackendClient, BackendError, parse_response
from passport_gateway.config import Bindings, token_hash

from .harness import DEVICE, TOKEN, GatewayCase, tone
from . import harness as H


def is_end(msg):
    return msg["type"] in ("error",) or (msg["type"] == "audio_end")


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_backend_client_sends_exactly_op_device_turn_text(self):
        """The real IPC client: nothing about a person, QQ account, session or role can even be expressed."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sock, seen = str(Path(tmp.name) / "core.sock"), []

        async def handle(reader, writer):
            seen.append(json.loads(await reader.readline()))
            writer.write(json.dumps({"ok": True, "reply": "好。", "mood": "neutral"}).encode() + b"\n")
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handle, sock)
        self.addCleanup(server.close)
        reply = await BackendClient(sock, 2.0).voice_turn(DEVICE, "a" * 32, "你好")
        self.assertEqual(reply.text, "好。")
        self.assertEqual(seen, [{"op": "voice_turn", "device_id": DEVICE, "turn_uid": "a" * 32, "text": "你好"}])

    def test_binding_changed_and_unbound_look_the_same_to_the_device(self):
        for error in ("voice_unbound", "voice_disabled", "voice_binding_changed"):
            with self.assertRaises(BackendError) as cm:
                parse_response(json.dumps({"ok": False, "error": error}).encode())
            self.assertEqual(cm.exception.code, "backend_unbound", error)


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bindings.json"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tokens.main(["--bindings", str(self.path), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_issue_never_replaces_an_existing_credential_and_rotation_is_explicit(self):
        code, out, _ = self.run_cli("--device-id", "dev-a")
        self.assertEqual(code, 0)
        token = out.strip().splitlines()[-1]
        before = json.loads(self.path.read_text())["devices"]["dev-a"]["token_sha256"]
        self.assertEqual(before, token_hash(token))
        code, out, err = self.run_cli("--device-id", "dev-a")                 # accidental second run
        self.assertEqual(code, 2)
        self.assertNotIn(token, out + err)
        self.assertEqual(json.loads(self.path.read_text())["devices"]["dev-a"]["token_sha256"], before)
        code, out, _ = self.run_cli("--device-id", "dev-a", "--rotate")
        self.assertEqual(code, 0)
        self.assertNotEqual(json.loads(self.path.read_text())["devices"]["dev-a"]["token_sha256"], before)
        self.assertNotEqual(out.strip().splitlines()[-1], token)               # a fresh secret, never the old one
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_enable_disable_touches_only_the_flag_and_issues_no_secret(self):
        self.run_cli("--device-id", "dev-a")
        digest = json.loads(self.path.read_text())["devices"]["dev-a"]["token_sha256"]
        code, out, _ = self.run_cli("--device-id", "dev-a", "--disable")
        self.assertEqual(code, 0)
        entry = json.loads(self.path.read_text())["devices"]["dev-a"]
        self.assertEqual((entry["enabled"], entry["token_sha256"]), (False, digest))
        self.assertNotIn(digest, out)
        bindings = Bindings(self.path)
        self.assertFalse(bindings.is_enabled("dev-a"))
        self.assertEqual(self.run_cli("--device-id", "dev-b", "--disable")[0], 2)      # no credential: nothing invented
        self.assertEqual(self.run_cli("--device-id", "dev-a", "--enable")[0], 0)
        self.assertTrue(Bindings(self.path).is_enabled("dev-a"))

    def test_list_shows_state_never_hashes_and_a_malformed_file_is_never_overwritten(self):
        self.run_cli("--device-id", "dev-a")
        self.run_cli("--device-id", "dev-b")
        self.run_cli("--device-id", "dev-b", "--disable")
        digest = json.loads(self.path.read_text())["devices"]["dev-a"]["token_sha256"]
        code, out, _ = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertIn("dev-a  enabled", out)
        self.assertIn("dev-b  disabled", out)
        self.assertNotIn(digest, out)
        self.path.write_text("{broken")
        with self.assertRaises(SystemExit):
            self.run_cli("--device-id", "dev-c")
        self.assertEqual(self.path.read_text(), "{broken")

    def test_the_credential_file_cannot_carry_identity_and_extra_fields_are_dropped(self):
        digest = token_hash("t")
        self.path.write_text(json.dumps({"devices": {"dev-a": {
            "token_sha256": digest, "enabled": True, "profile_id": "pf_x", "role": "owner", "owner": "qq:1",
            "session": "evil", "qq": "10001"}}}))
        bindings = Bindings(self.path)
        self.assertTrue(bindings.verify("dev-a", "t"))
        bindings.count()
        self.assertEqual(set(bindings._devices["dev-a"]), {"token_sha256", "enabled"})        # nothing else survives
        # a non-boolean 'enabled' is not "enabled"
        self.path.write_text(json.dumps({"devices": {"dev-a": {"token_sha256": digest, "enabled": "yes"}}}))
        self.assertFalse(Bindings(self.path).verify("dev-a", "t"))


class GatewayIdentityBoundaryTests(GatewayCase):
    async def test_profile_and_qq_claims_in_any_device_frame_or_header_never_reach_the_backend(self):
        ws = await self.connect(headers={"X-Profile-Id": "pf_x", "X-Qq": "10001", "X-Role": "owner"})
        await ws.send(H.P.dumps({"v": 1, "type": "start", "turn": 1, "hz": 16000, "ch": 1, "fmt": "pcm_s16le",
                                 "profile_id": "pf_0000000000000000", "qq": "10001", "role": "owner",
                                 "preferred_address": "主人", "display_name": "主人", "tool_allowlist": ["x"]}))
        pcm = tone(800)
        for seq, off in enumerate(range(0, len(pcm), 3200)):
            await ws.send(H.P.AUDIO_HDR.pack(H.P.KIND_UP, 0, 1, seq) + pcm[off:off + 3200])
        await ws.send(self.end_msg(1, len(pcm)))
        await self.collect(ws, is_end)
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(set(self.backend.requests[0]), {"device_id", "turn_uid", "text"})
        self.assertEqual(self.backend.requests[0]["device_id"], DEVICE)

    async def test_device_credential_disabled_during_a_turn_cancels_it_and_no_reply_is_delivered(self):
        self.backend.gate = asyncio.Event()
        ws = await self.connect()
        await self.speak(ws, 1, tone(600))
        self.assertEqual((await self.recv_json(ws))["type"], "transcript")
        self.write_bindings({DEVICE: TOKEN}, enabled=False)                     # credential switched off mid-turn
        with self.assertRaises(Exception):
            while True:
                msg = await asyncio.wait_for(ws.recv(), 3)
                self.assertNotIn(json.loads(msg)["type"], ("reply_text", "audio_start"))
        self.backend.gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(self.gw.sv._active_turns, 0)

    async def test_backend_binding_changed_error_reaches_the_device_as_unbound_without_a_reply(self):
        self.backend.error = BackendError("backend_unbound", "voice_binding_changed")
        ws = await self.connect()
        await self.speak(ws, 1, tone(600))
        texts, _ = await self.collect(ws, lambda m: m["type"] == "error")
        self.assertEqual(texts[-1]["code"], "backend_unbound")
        self.assertNotIn("reply_text", [t["type"] for t in texts])


if __name__ == "__main__":
    unittest.main()
