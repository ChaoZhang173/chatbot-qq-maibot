"""Voice protocol v1 (device <-> gateway). Mirror of firmware main/pp_proto.[ch].

Control frames are flat JSON text; audio frames are binary with an 8-byte header:
[kind u8][flags u8][turn u32 BE][seq u16 BE] + PCM s16le. See docs/development/voice-protocol.md
in the firmware repository. Conformance vectors: tests/fixtures/protocol_vectors.txt.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass

VERSION = 1
SUBPROTOCOL = "dafeiyu.voice.v1"

UP_HZ = 16000
DOWN_HZ = 24000
MAX_UTTERANCE_MS = 20000
MAX_UP_BYTES = UP_HZ * 2 * MAX_UTTERANCE_MS // 1000
MIN_UP_BYTES = UP_HZ * 2 * 200 // 1000          # < 200 ms is treated as empty audio

AUDIO_HDR = struct.Struct(">BBIH")
AUDIO_HDR_LEN = AUDIO_HDR.size                  # 8
KIND_UP = 1
KIND_DOWN = 2
MAX_UP_PAYLOAD = 3200
MAX_DOWN_PAYLOAD = 2048
MAX_CTRL_UP = 512
MAX_CTRL_DOWN = 2048
MAX_REPLY_TEXT_BYTES = 600                      # device buffer is 640 incl. NUL

MOODS = ("neutral", "pleased", "gentle", "serious", "puzzled")

# Error codes the device understands (firmware pp_err_from_code).
ERROR_CODES = (
    "auth", "backend_unbound", "provider_unconfigured", "asr_empty", "too_long",
    "asr_timeout", "brain_timeout", "tts_timeout", "asr_failed", "tts_failed",
    "brain_failed", "queue_full", "busy", "bad_message", "out_of_order", "too_large",
    "internal",
)


class ProtocolError(Exception):
    """A device message violated the protocol; `code` is a wire error code."""

    def __init__(self, code: str, detail: str = ""):
        assert code in ERROR_CODES, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _utf8_prefix(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    cut = raw[:max_bytes]
    # Drop a possibly split trailing character.
    return cut.decode("utf-8", errors="ignore"), True


# ---- server -> device ------------------------------------------------------
def ready(max_ms: int = MAX_UTTERANCE_MS) -> str:
    return dumps({"v": VERSION, "type": "ready", "max_ms": max_ms, "hz": DOWN_HZ, "up_hz": UP_HZ})


def transcript(turn: int, text: str, final: bool) -> str:
    text, _ = _utf8_prefix(text, MAX_REPLY_TEXT_BYTES)
    return dumps({"v": VERSION, "type": "transcript", "turn": turn, "text": text, "final": final})


def reply_text(turn: int, text: str) -> str:
    text, trunc = _utf8_prefix(text, MAX_REPLY_TEXT_BYTES)
    return dumps({"v": VERSION, "type": "reply_text", "turn": turn, "text": text, "trunc": trunc})


def mood(turn: int, value: str) -> str:
    if value not in MOODS:
        value = "neutral"
    return dumps({"v": VERSION, "type": "mood", "turn": turn, "mood": value})


def audio_start(turn: int) -> str:
    return dumps({"v": VERSION, "type": "audio_start", "turn": turn, "hz": DOWN_HZ, "ch": 1,
                  "fmt": "pcm_s16le"})


def audio_end(turn: int, total_bytes: int) -> str:
    return dumps({"v": VERSION, "type": "audio_end", "turn": turn, "bytes": total_bytes})


def error(code: str, turn: int | None = None, msg: str = "") -> str:
    assert code in ERROR_CODES, code
    body: dict = {"v": VERSION, "type": "error"}
    if turn:
        body["turn"] = turn
    body["code"] = code
    if msg:
        body["msg"] = msg[:120]
    return dumps(body)


def down_frame(turn: int, seq: int, pcm: bytes) -> bytes:
    if not 0 < len(pcm) <= MAX_DOWN_PAYLOAD or len(pcm) % 2:
        raise ValueError("bad downlink payload size")
    return AUDIO_HDR.pack(KIND_DOWN, 0, turn, seq) + pcm


# ---- device -> server ------------------------------------------------------
@dataclass(frozen=True)
class Control:
    type: str
    turn: int = 0
    bytes: int = 0
    fw: str = ""


def parse_control(raw: str | bytes) -> Control:
    """Validate a device control frame. Raises ProtocolError on anything unexpected."""
    if isinstance(raw, bytes):
        raise ProtocolError("bad_message", "control frames must be text")
    if len(raw.encode("utf-8")) > MAX_CTRL_UP:
        raise ProtocolError("too_large", "control frame")
    try:
        msg = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("bad_message", "invalid json") from exc
    if not isinstance(msg, dict) or msg.get("v") != VERSION or type(msg.get("v")) is not int:
        raise ProtocolError("bad_message", "version")
    kind = msg.get("type")
    if kind == "hello":
        fw = msg.get("fw")
        if not isinstance(fw, str) or not 0 < len(fw) <= 32:
            raise ProtocolError("bad_message", "fw")
        return Control("hello", fw=fw)
    if kind in ("start", "end", "cancel"):
        turn = msg.get("turn")
        if type(turn) is not int or not 0 < turn <= 0xFFFFFFFF:
            raise ProtocolError("bad_message", "turn")
        if kind == "start":
            if msg.get("hz") != UP_HZ or msg.get("ch") != 1 or msg.get("fmt") != "pcm_s16le":
                raise ProtocolError("bad_message", "audio format")
            return Control("start", turn)
        if kind == "end":
            n = msg.get("bytes")
            if type(n) is not int or not 0 <= n <= MAX_UP_BYTES:
                raise ProtocolError("bad_message", "bytes")
            return Control("end", turn, n)
        return Control("cancel", turn)
    raise ProtocolError("bad_message", "unknown type")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


@dataclass(frozen=True)
class UpFrame:
    turn: int
    seq: int
    pcm: bytes


def parse_up_frame(frame: bytes) -> UpFrame:
    if len(frame) < AUDIO_HDR_LEN + 2:
        raise ProtocolError("bad_message", "short audio frame")
    kind, flags, turn, seq = AUDIO_HDR.unpack_from(frame)
    payload = len(frame) - AUDIO_HDR_LEN
    if kind != KIND_UP or flags != 0 or turn == 0:
        raise ProtocolError("bad_message", "audio header")
    if payload > MAX_UP_PAYLOAD:
        raise ProtocolError("too_large", "audio payload")
    if payload % 2:
        raise ProtocolError("bad_message", "odd payload")
    return UpFrame(turn, seq, bytes(frame[AUDIO_HDR_LEN:]))
