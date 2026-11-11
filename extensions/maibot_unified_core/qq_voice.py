"""Model-chosen QQ voice replies, using the Passport TTS provider and native Host sender.

No scheduler or new recipient selection lives here. Callers retain the existing
reply claim / proactive budget. Private speech settings stay in a local file.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
import wave

from .content_rules import blocked
from .voice_scope import CURRENT as VOICE_SCOPE
from .voice_style import RULE as STYLE_RULE, normalize as normalize_style

RULE = (
    "你可以自主选择qq_voice_reply，用Passport同一个声音发送QQ语音条。"
    "根据你自己的表达偏好、当下心情和内容，自由决定用语音还是文字，也可以连续选择语音。"
    "不要求以文字为主，不设语音次数或间隔。链接、代码、需要复制的内容可以选择文字。"
    "对方不方便听或要求文字时尊重对方。msg_id使用当前合格消息。"
    "qq_voice_reply替代本次reply；工具已发送文字或语音后不要重复发送或播报操作回执。"
) + STYLE_RULE
MOODS = {"neutral", "pleased", "gentle", "serious", "puzzled"}


def get(service):
    bridge = getattr(service, "qq_voice", None)
    if bridge is None:
        bridge = service.qq_voice = QQVoice(service)
    return bridge


class QQVoice:
    def __init__(self, service, *, tts=None, clock=time.time):
        self.s, self.tts, self.clock = service, tts, clock

    def allowed(self, session):
        native = self.s.policy.get("qq_native") or {}
        return bool(not VOICE_SCOPE.get() and session
                    and session in self.s.policy.get("allowed_qq_sessions", [])
                    and native.get("enabled") is True
                    and str(native.get("account")) == str(self.s.policy.get("qq_bot_account")))

    def enabled(self, session):
        return self.allowed(session) and (self.s.policy.get("qq_voice") or {}).get("enabled") is True

    def context(self, session):
        return RULE if self.enabled(session) else ""

    def observe(self, session, text):
        if re.search(r"(?:不要|别|停止).{0,4}(?:发|用|回)?.{0,2}语音|(?:只|请|改|继续)用文字|不方便听", text):
            self.s.state.put("qq_voice_quiet:" + session, True)
        elif re.search(r"(?:恢复|允许|可以).{0,4}(?:发|用|回)?.{0,2}语音", text):
            self.s.state.put("qq_voice_quiet:" + session, False)

    def eligible(self, session, text):
        if not self.enabled(session) or self.s.state.get("qq_voice_quiet:" + session, False):
            return False
        return isinstance(text, str) and bool(text.strip())

    def _provider(self):
        if self.tts is not None:
            return self.tts
        # Populate this file from the verified live Passport TTS configuration at
        # deployment, never from model arguments. Do not guess the accepted voice.
        from passport_gateway.config import Config
        from passport_gateway.providers import build_providers
        env = json.loads((self.s.root / "qq_voice_speech.json").read_text("utf-8"))
        provider = env.get("PASSPORT_PROVIDER")
        voice_key = {"siliconflow": "PASSPORT_SF_TTS_VOICE", "dashscope": "PASSPORT_DS_TTS_VOICE"}.get(provider)
        if not voice_key or not env.get(voice_key):
            raise ValueError("verified_passport_voice_required")
        cfg = Config.from_env(env)
        if cfg.test_mode:
            raise ValueError("verified_passport_voice_required")
        if provider == "siliconflow" and not cfg.sf_base_url.startswith("https://"):
            raise ValueError("https_provider_required")
        return build_providers(cfg)[1]

    async def _wav(self, text, mood, voice_style=""):
        chunks = bytearray()
        style_args = {"voice_style": voice_style} if voice_style else {}
        stream = self._provider().synthesize(text, mood, **style_args)
        try:
            async for chunk in stream:
                chunks.extend(chunk)
        finally:
            await stream.aclose()
        if not chunks or len(chunks) % 2:
            raise ValueError("invalid_pcm")
        result = io.BytesIO()
        with wave.open(result, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(chunks)
        return base64.b64encode(result.getvalue()).decode("ascii")

    async def _send(self, session, text, audio=None):
        # Re-check after synthesis: a revoked session must receive nothing.
        self.s.reload_policy()
        if not self.allowed(session):
            return {"sent": False, "status": "cancelled", "delivery": "none"}
        delivery = "voice" if audio else "text"
        if audio and (not self.enabled(session) or self.s.state.get("qq_voice_quiet:" + session, False)):
            audio, delivery = None, "text"
        args = dict(stream_id=session, return_details=True, storage_message=True,
                    sync_to_maisaka_history=True, maisaka_source_kind="qq_voice_reply")
        if audio:
            args.update(message_type="voice", content=audio, processed_plain_text=text)
        else:
            args.update(text=text)
        try:
            result = await asyncio.wait_for(self.s.ctx.call_capability(
                "send.custom" if audio else "send.text", timeout_ms=18000, **args), 19)
        except Exception:
            result = {}
        # An uncertain delivery can already be on QQ. Never retry or text-fallback
        # after the first outbound invocation.
        confirmed = isinstance(result, dict) and result.get("sent") is True
        return {"sent": confirmed, "status": "sent" if confirmed else "unknown",
                "delivery": delivery,
                "message_id": str(result.get("message_id") or "") if isinstance(result, dict) else ""}

    async def deliver(self, session, text, mood="neutral", *, voice_style="", still_allowed=None):
        self.s.reload_policy()
        if not isinstance(text, str) or not text.strip() or blocked(text) or not self.allowed(session):
            return {"sent": False, "status": "cancelled", "delivery": "none"}
        text = text.strip()
        if still_allowed is not None and not still_allowed():
            return {"sent": False, "status": "cancelled", "delivery": "none"}
        audio = None
        if self.eligible(session, text):
            try:
                audio = await asyncio.wait_for(self._wav(text, mood if mood in MOODS else "neutral", normalize_style(voice_style)), 60)
            except Exception as exc:
                self.s.state.put("qq_voice_last_error", {"type": type(exc).__name__, "at": self.clock()})
        self.s.reload_policy()
        if still_allowed is not None and not still_allowed():
            return {"sent": False, "status": "cancelled", "delivery": "none"}
        return await self._send(session, text, audio)

    async def reply(self, session, msg_id, text, mood="neutral", token="", *, voice_style=""):
        self.s.reload_policy()
        if not self.allowed(session) or not isinstance(text, str) or not text.strip() or blocked(text):
            return {"success": False, "internal_only": True, "error": "voice_reply_not_allowed"}
        if not self.s.guard_qq_reply(session, msg_id, {"_unified_reply_token": token}):
            return {"success": False, "internal_only": True, "error": "reply_already_claimed_or_not_eligible"}
        result = await self.deliver(session, text, mood, voice_style=voice_style)
        return {"success": result["sent"], "internal_only": True, "content": result,
                "instruction": "本轮回复已处理。不要重复reply或qq_voice_reply；unknown表示发送未确认，不能声称已送达，也不要自动补发。"}
