"""DashScope (Alibaba Model Studio) realtime event schema used by the OPTIONAL DashScope adapters.

Checked 2026-11-02 against help.aliyun.com (qwen-tts-realtime interactive process; Qwen-ASR realtime
user guide):
  VERIFIED   endpoints/`model` query, `Authorization: Bearer`, ASR `OpenAI-Beta: realtime=v1`,
             session.update/input_audio_buffer.append|commit/session.finish (ASR),
             session.update(mode commit|server_commit, voice, language_type, response_format pcm,
             sample_rate 24000)/input_text_buffer.append|commit/session.finish (TTS),
             response.audio.delta{delta: base64 PCM}, response.done, session.finished,
             ASR final text in `transcript` of conversation.item.input_audio_transcription.completed.
  NOT DOCUMENTED on those pages: TTS `instructions`/`optimize_instructions` (instruct model), the
             `error` event payload shape, PCM rates other than 24000. These remain best-effort and
             are isolated here; this adapter is not the default provider and is untested live.
The ASR URL is workspace-specific (wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/...), so the base
URL is configuration, never hard-coded.
"""
from __future__ import annotations

import base64
import itertools
import json

_event_ids = itertools.count(1)


def _eid() -> str:
    return f"event_{next(_event_ids):06d}"


def endpoint(base_url: str, model: str) -> str:
    return f"{base_url}?model={model}"


def auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "OpenAI-Beta": "realtime=v1"}


# ---- ASR (qwen3-asr-flash-realtime): 16 kHz mono PCM s16le in, manual commit -----------------
def asr_session_update(sample_rate: int = 16000, language: str = "zh") -> str:
    return json.dumps({
        "event_id": _eid(), "type": "session.update",
        "session": {
            "input_audio_format": "pcm",
            "sample_rate": sample_rate,
            "input_audio_transcription": {"language": language},
            "turn_detection": None,                 # manual: the device decides when speech ends
        },
    })


def asr_append(pcm: bytes) -> str:
    return json.dumps({"event_id": _eid(), "type": "input_audio_buffer.append",
                       "audio": base64.b64encode(pcm).decode("ascii")})


def asr_commit() -> str:
    return json.dumps({"event_id": _eid(), "type": "input_audio_buffer.commit"})


def asr_finish() -> str:
    return json.dumps({"event_id": _eid(), "type": "session.finish"})


ASR_TRANSCRIPT_DONE = "conversation.item.input_audio_transcription.completed"
ASR_TRANSCRIPT_PARTIAL = "conversation.item.input_audio_transcription.text"


# ---- TTS (qwen3-tts-*-flash-realtime): text in, 24 kHz mono PCM s16le out --------------------
def tts_session_update(voice: str, sample_rate: int = 24000, instructions: str | None = None,
                       language_type: str = "Chinese") -> str:
    session = {
        "mode": "commit",                           # we send the whole answer, then commit
        "voice": voice,
        "language_type": language_type,
        "response_format": "pcm",
        "sample_rate": sample_rate,
    }
    if instructions:                                # only instruct models accept this field
        session["instructions"] = instructions
        session["optimize_instructions"] = False
    return json.dumps({"event_id": _eid(), "type": "session.update", "session": session},
                      ensure_ascii=False)


def tts_append(text: str) -> str:
    return json.dumps({"event_id": _eid(), "type": "input_text_buffer.append", "text": text},
                      ensure_ascii=False)


def tts_commit() -> str:
    return json.dumps({"event_id": _eid(), "type": "input_text_buffer.commit"})


def tts_finish() -> str:
    return json.dumps({"event_id": _eid(), "type": "session.finish"})


TTS_AUDIO_DELTA = "response.audio.delta"
TTS_RESPONSE_DONE = "response.done"


# ---- shared server events ---------------------------------------------------------------------
SESSION_CREATED = "session.created"
SESSION_UPDATED = "session.updated"
SESSION_FINISHED = "session.finished"
ERROR = "error"


def parse_event(raw: str | bytes) -> dict:
    event = json.loads(raw)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("not a realtime event")
    return event


def error_text(event: dict) -> str:
    err = event.get("error")
    if isinstance(err, dict):
        return f'{err.get("code", "")}: {err.get("message", "")}'[:200]
    return str(err or event.get("message", ""))[:200]


def error_is_unsupported(event: dict) -> bool:
    """Heuristic: the model/account does not accept `instructions` or this model name."""
    text = error_text(event).lower()
    return ("instruction" in text or "not support" in text or "unsupported" in text
            or "model" in text and ("not found" in text or "invalid" in text or "access" in text))
