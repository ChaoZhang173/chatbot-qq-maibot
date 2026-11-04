"""Gateway configuration: environment + a server-side binding file. Secrets are env-only."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# DashScope-only candidate instruction (design brief; audition only, not an accepted voice).
DEFAULT_DS_TTS_INSTRUCTIONS = (
    "明亮自然的年轻成年女声，普通话清晰，像熟人轻松交谈。轻松时有一点小骄傲和俏皮，"
    "认真回答或安慰时温柔平稳。语速适中，不夹嗓、不喊叫，不刻意拖长尾音，"
    "不擅自添加台词、笑声或语气词。"
)

PROVIDERS = ("siliconflow", "dashscope")


class ConfigError(Exception):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Bindings:
    """Device token bindings. Re-read whenever the file changes, so revocation is live.

    File format: {"devices": {"<device_id>": {"token_sha256": "<hex>", "enabled": true}}}
    It deliberately holds NO owner / QQ session / person_id: the backend owns that mapping.
    """

    def __init__(self, path: str | os.PathLike | None):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._devices: dict[str, dict] = {}

    def _load(self) -> None:
        if self._path is None:
            self._devices = {}
            return
        try:
            st = self._path.stat()
        except OSError:
            self._stamp, self._devices = None, {}
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            devices = data["devices"]
            clean: dict[str, dict] = {}
            for dev, entry in devices.items():
                if (DEVICE_ID_RE.match(dev) and isinstance(entry, dict)
                        and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("token_sha256", "")))):
                    clean[dev] = {"token_sha256": entry["token_sha256"],
                                  "enabled": entry.get("enabled", True) is True}
            self._devices = clean
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            self._devices = {}            # fail closed on a corrupt file
        self._stamp = stamp

    def verify(self, device_id: str, token: str) -> bool:
        """Constant-time token check; unknown/disabled devices are indistinguishable from bad tokens."""
        with self._lock:
            self._load()
            entry = self._devices.get(device_id) if DEVICE_ID_RE.match(device_id or "") else None
        expected = entry["token_sha256"] if entry else "0" * 64
        ok = hmac.compare_digest(token_hash(token or ""), expected)
        return bool(entry and entry["enabled"] and ok)

    def is_enabled(self, device_id: str) -> bool:
        """Binding still present and enabled (re-checked at every turn start)."""
        with self._lock:
            self._load()
            entry = self._devices.get(device_id)
        return bool(entry and entry["enabled"])

    def session_valid(self, device_id: str, digest: str) -> bool:
        """Is this established connection still backed by the CURRENT binding? Compares the credential
        digest captured at the handshake with the current token_sha256 (constant time), so rotating or
        disabling a token invalidates already-open connections at their next turn."""
        with self._lock:
            self._load()
            entry = self._devices.get(device_id)
        return bool(entry and entry["enabled"] and hmac.compare_digest(digest, entry["token_sha256"]))

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._devices)


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8443
    tls_cert: str = ""
    tls_key: str = ""
    allow_plaintext_loopback: bool = False
    bindings_path: str = ""
    backend_socket: str = "/srv/maibot/unified/core.sock"
    backend_mode: str = "unix"
    backend_url: str = ""
    backend_api_key: str = field(default="", repr=False)
    http_reminders: bool = False
    test_mode: bool = False
    provider: str = "siliconflow"
    # --- SiliconFlow (initial default): turn-end ASR + streaming CosyVoice2 TTS --------------
    sf_base_url: str = "https://api.siliconflow.cn/v1"
    sf_api_key: str = field(default="", repr=False)
    sf_asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    sf_tts_model: str = "FunAudioLLM/CosyVoice2-0.5B"
    sf_tts_voice: str = "FunAudioLLM/CosyVoice2-0.5B:diana"     # provisional preset, not accepted
    # Delivery style, sent as "<style><|endofprompt|>text". User-accepted provisional default (2026-11-02);
    # PASSPORT_TTS_STYLE / PASSPORT_TTS_STYLE_FILE override it, an explicit empty value disables it.
    sf_tts_style: str = "请用活泼俏皮的动漫角色语气说话"
    # Softer delivery for gentle/serious replies (design intent, not separately auditioned). Same voice id.
    sf_tts_style_soft: str = "请用温柔平稳的动漫角色语气说话，语速稍慢"
    sf_tts_speed: float = 1.0
    sf_tts_gain: float = 0.0
    # --- DashScope (optional) ---------------------------------------------------------------
    ds_asr_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    ds_tts_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    ds_api_key: str = field(default="", repr=False)
    ds_asr_model: str = "qwen3-asr-flash-realtime"
    ds_tts_model: str = "qwen3-tts-instruct-flash-realtime"
    ds_tts_voice: str = "Cherry"
    ds_tts_instructions: str = DEFAULT_DS_TTS_INSTRUCTIONS
    ds_tts_fallback: bool = True
    ds_tts_fallback_model: str = "qwen3-tts-flash-realtime"
    max_sessions: int = 4
    max_active_turns: int = 3
    # ASR budget: live use showed SenseVoice requests that outlast 12 s; 25 s total for the turn-end upload + recognition.
    # Coupled with the firmware stage limits (docs/development/voice-protocol.md, tests/test_deadlines.py). No automatic retry.
    asr_timeout_s: float = 25.0
    brain_timeout_s: float = 25.0
    tts_first_audio_timeout_s: float = 15.0
    tts_total_timeout_s: float = 60.0
    send_timeout_s: float = 10.0
    upload_grace_ms: int = 3000
    # Downlink pacing: audio is released at most `pace_lead_ms` ahead of the playback clock so the
    # device's 16 KiB (~340 ms) queue can never be overrun by a fast TTS source. 0 disables pacing.
    pace_lead_ms: int = 240
    pace_rate_bytes_s: int = 48000        # 24 kHz mono s16le in real time (tests scale it)

    def validate(self) -> None:
        if self.backend_mode not in ("unix", "http"):
            raise ConfigError("PASSPORT_BACKEND must be unix or http")
        if self.backend_mode == "http":
            from .http_backend import validate_endpoint
            validate_endpoint(self.backend_url)
            if not self.backend_api_key or any(c.isspace() for c in self.backend_api_key):
                raise ConfigError("PASSPORT_BACKEND_API_KEY must be a nonempty token without whitespace")
        if self.tls_cert or self.tls_key:
            if not (self.tls_cert and self.tls_key):
                raise ConfigError("PASSPORT_TLS_CERT and PASSPORT_TLS_KEY must be set together")
        elif not (self.allow_plaintext_loopback and is_loopback(self.host)):
            raise ConfigError(
                "TLS is required. Plain ws:// is only allowed on a loopback bind with "
                "PASSPORT_ALLOW_PLAINTEXT_LOOPBACK=1 (private development only).")
        if self.provider not in PROVIDERS:
            raise ConfigError(f"PASSPORT_PROVIDER must be one of {PROVIDERS}")
        if not self.test_mode and self.provider == "siliconflow" and \
                not self.sf_base_url.startswith("https://"):
            raise ConfigError("PASSPORT_SF_BASE_URL must be https:// (credentials are sent to it)")
        if not self.bindings_path:
            raise ConfigError("PASSPORT_BINDINGS is required (device token binding file)")
        if not 0 <= self.port <= 65535:       # 0 = OS-assigned (tests)
            raise ConfigError("invalid port")

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        d = cls()
        style = e.get("PASSPORT_TTS_STYLE", d.sf_tts_style)
        if e.get("PASSPORT_TTS_STYLE_FILE"):
            style = Path(e["PASSPORT_TTS_STYLE_FILE"]).read_text("utf-8").strip()
        soft = e.get("PASSPORT_TTS_STYLE_SOFT", d.sf_tts_style_soft)
        c = cls(
            host=e.get("PASSPORT_HOST", "127.0.0.1"),
            port=int(e.get("PASSPORT_PORT", "8443")),
            tls_cert=e.get("PASSPORT_TLS_CERT", ""),
            tls_key=e.get("PASSPORT_TLS_KEY", ""),
            allow_plaintext_loopback=_truthy(e.get("PASSPORT_ALLOW_PLAINTEXT_LOOPBACK")),
            bindings_path=e.get("PASSPORT_BINDINGS", ""),
            backend_socket=e.get("PASSPORT_BACKEND_SOCKET", d.backend_socket),
            backend_mode=e.get("PASSPORT_BACKEND", d.backend_mode),
            backend_url=e.get("PASSPORT_BACKEND_URL", ""),
            backend_api_key=e.get("PASSPORT_BACKEND_API_KEY", ""),
            http_reminders=_truthy(e.get("PASSPORT_HTTP_REMINDERS")),
            test_mode=_truthy(e.get("PASSPORT_TEST_MODE")),
            provider=e.get("PASSPORT_PROVIDER", d.provider),
            sf_base_url=e.get("PASSPORT_SF_BASE_URL", d.sf_base_url),
            sf_api_key=e.get("SILICONFLOW_API_KEY", ""),
            sf_asr_model=e.get("PASSPORT_SF_ASR_MODEL", d.sf_asr_model),
            sf_tts_model=e.get("PASSPORT_SF_TTS_MODEL", d.sf_tts_model),
            sf_tts_voice=e.get("PASSPORT_SF_TTS_VOICE", d.sf_tts_voice),
            sf_tts_style=style,
            sf_tts_style_soft=soft,
            sf_tts_speed=float(e.get("PASSPORT_SF_TTS_SPEED", d.sf_tts_speed)),
            sf_tts_gain=float(e.get("PASSPORT_SF_TTS_GAIN", d.sf_tts_gain)),
            ds_asr_url=e.get("PASSPORT_DS_ASR_URL", d.ds_asr_url),
            ds_tts_url=e.get("PASSPORT_DS_TTS_URL", d.ds_tts_url),
            ds_api_key=e.get("DASHSCOPE_API_KEY", ""),
            ds_asr_model=e.get("PASSPORT_DS_ASR_MODEL", d.ds_asr_model),
            ds_tts_model=e.get("PASSPORT_DS_TTS_MODEL", d.ds_tts_model),
            ds_tts_voice=e.get("PASSPORT_DS_TTS_VOICE", d.ds_tts_voice),
            ds_tts_fallback=_truthy(e.get("PASSPORT_DS_TTS_FALLBACK"), True),
            ds_tts_fallback_model=e.get("PASSPORT_DS_TTS_FALLBACK_MODEL", d.ds_tts_fallback_model),
        )
        return c
