"""Protocol conformance: gateway builders/parsers must agree with the shared vector file that
the firmware C tests also consume (tests/fixtures/protocol_vectors.txt in the firmware repo)."""
import hashlib
import os
import unittest
from pathlib import Path

from passport_gateway import protocol as P

HERE = Path(__file__).resolve().parent
VENDORED = HERE / "protocol_vectors.txt"
FIRMWARE = Path(os.environ.get(
    "PASSPORT_FIRMWARE_VECTORS",
    HERE.parents[2] / "ai-passport" / "tests" / "fixtures" / "protocol_vectors.txt"))


def vectors():
    out = []
    for line in VENDORED.read_text("utf-8").splitlines():
        if line and not line.startswith("#"):
            out.append(line.split(" ", 3 if line.startswith(("SRV", "AUD")) else 2))
    return out


class VectorTests(unittest.TestCase):
    def test_vendored_copy_matches_firmware_repo(self):
        if not FIRMWARE.exists():
            self.skipTest("firmware checkout not found next to services")
        self.assertEqual(hashlib.sha256(FIRMWARE.read_bytes()).hexdigest(),
                         hashlib.sha256(VENDORED.read_bytes()).hexdigest(),
                         "vendored protocol_vectors.txt drifted from the firmware repo")

    def test_server_messages_are_byte_identical_to_vectors(self):
        built = {
            "ready": P.ready(),
            "transcript": P.transcript(7, "今天天气怎么样", True),
            "transcript_partial": P.transcript(7, "今天", False),
            "reply_text": P.reply_text(7, "天气不错,出门记得带伞。"),
            "mood": P.mood(7, "pleased"),
            "audio_start": P.audio_start(7),
            "audio_end": P.audio_end(7, 96000),
            "error_conn": P.error("auth"),
        }
        seen = set()
        for kind, status, name, payload in [v for v in vectors() if v[0] == "SRV"]:
            if name in built:
                self.assertEqual(built[name], payload, name)
                self.assertEqual(status, "OK")
                seen.add(name)
        self.assertEqual(seen, set(built))

    def test_error_with_turn(self):
        self.assertEqual(P.error("asr_empty", 7, "no speech detected, a fairly long unknown string field "
                                                  "is skipped safely"),
                         [v[3] for v in vectors() if v[0] == "SRV" and v[2] == "error_turn"][0])

    def test_client_messages(self):
        for _, name, payload in [v for v in vectors() if v[0] == "CLI"]:
            ctl = P.parse_control(payload)
            self.assertEqual(ctl.type, name)
        self.assertEqual(P.parse_control(
            [v[2] for v in vectors() if v[0] == "CLI" and v[1] == "end"][0]).bytes, 64000)

    def test_audio_frame_vectors(self):
        for _, ok, kind, hexframe in [v for v in vectors() if v[0] == "AUD"]:
            frame = bytes.fromhex(hexframe)
            if int(kind) == P.KIND_UP:
                if ok == "OK":
                    self.assertEqual(P.parse_up_frame(frame).turn, 7)
                else:
                    with self.assertRaises(P.ProtocolError):
                        P.parse_up_frame(frame)
            elif ok == "OK":
                # Downlink OK vector: the gateway's own builder must produce the same bytes.
                self.assertEqual(P.down_frame(7, 1, frame[8:]), frame)

    def test_error_code_sets_match(self):
        listed = {v[1] for v in vectors() if v[0] == "ERR" and v[1] != "never_heard_of_it"}
        self.assertEqual(listed, set(P.ERROR_CODES))


class ParserTests(unittest.TestCase):
    def check(self, raw, code):
        with self.assertRaises(P.ProtocolError) as cm:
            P.parse_control(raw)
        self.assertEqual(cm.exception.code, code, raw)

    def test_rejections(self):
        self.check(b"binary", "bad_message")
        self.check("{", "bad_message")
        self.check('{"v":1,"type":"start","turn":1,"turn":2,"hz":16000,"ch":1,"fmt":"pcm_s16le"}', "bad_message")
        self.check('{"v":2,"type":"cancel","turn":1}', "bad_message")
        self.check('{"v":true,"type":"cancel","turn":1}', "bad_message")
        self.check('{"v":1,"type":"cancel","turn":0}', "bad_message")
        self.check('{"v":1,"type":"cancel","turn":"1"}', "bad_message")
        self.check('{"v":1,"type":"cancel","turn":true}', "bad_message")
        self.check('{"v":1,"type":"start","turn":1,"hz":8000,"ch":1,"fmt":"pcm_s16le"}', "bad_message")
        self.check('{"v":1,"type":"end","turn":1,"bytes":-1}', "bad_message")
        self.check('{"v":1,"type":"end","turn":1,"bytes":%d}' % (P.MAX_UP_BYTES + 1), "bad_message")
        self.check('{"v":1,"type":"mystery"}', "bad_message")
        self.check('{"v":1,"type":"hello","fw":"' + "x" * 600 + '"}', "too_large")
        self.check("[]", "bad_message")

    def test_up_frame_limits(self):
        hdr = P.AUDIO_HDR
        with self.assertRaises(P.ProtocolError):
            P.parse_up_frame(hdr.pack(P.KIND_UP, 0, 1, 0) + b"\0" * (P.MAX_UP_PAYLOAD + 2))
        with self.assertRaises(P.ProtocolError):
            P.parse_up_frame(hdr.pack(P.KIND_UP, 1, 1, 0) + b"\0\0")
        with self.assertRaises(P.ProtocolError):
            P.parse_up_frame(hdr.pack(P.KIND_UP, 0, 0, 0) + b"\0\0")
        self.assertEqual(P.parse_up_frame(hdr.pack(P.KIND_UP, 0, 5, 9) + b"\1\2\3\4").seq, 9)

    def test_reply_text_is_truncated_on_a_character_boundary(self):
        import json
        text = "你" * 400                                      # 1200 bytes
        msg = json.loads(P.reply_text(1, text))
        self.assertTrue(msg["trunc"])
        self.assertLessEqual(len(msg["text"].encode()), P.MAX_REPLY_TEXT_BYTES)
        self.assertEqual(msg["text"], "你" * (P.MAX_REPLY_TEXT_BYTES // 3))
        self.assertFalse(json.loads(P.reply_text(1, "短"))["trunc"])

    def test_unknown_mood_degrades_to_neutral(self):
        import json
        self.assertEqual(json.loads(P.mood(1, "rage"))["mood"], "neutral")

    def test_down_frame_validation(self):
        with self.assertRaises(ValueError):
            P.down_frame(1, 0, b"\0" * 3)
        with self.assertRaises(ValueError):
            P.down_frame(1, 0, b"")
        with self.assertRaises(ValueError):
            P.down_frame(1, 0, b"\0" * (P.MAX_DOWN_PAYLOAD + 2))


if __name__ == "__main__":
    unittest.main()
