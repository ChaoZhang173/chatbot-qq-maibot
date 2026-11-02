"""Device voice (Dafeiyu Passport) operation: one owner-bound spoken turn through the EXISTING brain.

Boundary (see backend_patch/README.md):
  * reachable only by the `voice_uid` peer, operation `voice_turn` only (service.authorize_peer);
  * the caller supplies {device_id, turn_uid, text} - never an owner, session, person_id or endpoint;
  * the device -> owner private session binding lives in <root>/voice_bindings.json, is re-read on
    every turn and re-validated against the CURRENT policy and the verified private binding, so
    revocation / policy reload take effect on the next turn;
  * the reply comes from the Host's real Maisaka planner, tool registry and replyer;
  * the turn is persisted idempotently through the existing scoped paths (state.ingest + Host
    unified.memory ingest_text) under the owner's private QQ session, with channel provenance
    'device_voice'. It is NOT recorded as a QQ delivery (no record_activity / receipts), because
    nothing was sent through QQ;
  * existing tool authority and receipts apply; only the reply delivery channel is changed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

from . import content_rules
from .receipts import requested as receipt_requested

DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
TURN_UID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_TEXT = 1000
HISTORY_LIMIT = 8
MOODS = ("neutral", "pleased", "gentle", "serious", "puzzled")
ALLOWED_FIELDS = {"op", "device_id", "turn_uid", "text"}
REFUSAL = "这个话题我不参与，换个别的聊聊吧。"
IDEMPOTENCY_PREFIX = "voice_turn:"
# Budget chain (device 28 s > gateway 25 s > service 27 s watchdog): Host 22 s inside plugin 23 s; scope outlives both hooks.
HOST_SECONDS = 22.0
PLUGIN_SECONDS = 23.0
SCOPE_SECONDS = 24.0
SESSION_WAIT_SECONDS = 3.0

VOICE_RULE = (
    "这是主人通过桌面小设备的语音对话，不是QQ消息：回答会被朗读，也会显示在小屏幕上。"
    "回复默认1至3个短句，口语自然，适合朗读；来源链接可保留给屏幕显示。"
    "只有主人明确要求详细说明时才展开，且仍不超过8句。"
    "语音识别可能有错字：拿不准就简短确认，不要硬猜。"
    "需要查证或操作时，先调用合适的工具并读取真实结果，最后只调用一次reply给出答案；"
    "动作是否完成以真实结果和回执为准，没有结果不要声称完成。"
    "reply的文字只交回设备，不是QQ消息，也没有QQ回执；设备回复不能附带图片、表情或@。"
    "最近设备语音记录里的channel=device_voice表示主人用语音设备说的，和QQ私聊是同一位主人的私聊记忆，不向群聊公开。"
)


def time_note(remaining: float, finishing: bool) -> str:
    """Real remaining time of THIS voice turn, shown to the existing Planner/replyer (no second planner)."""
    seconds = max(0, int(remaining))
    text = (f"语音回合剩余约{seconds}秒。联网查询要花数秒：只有时间充裕才发起新的搜索或阅读，同样的查询不要重复；"
            "拿到的资料已足够就直接reply。")
    if finishing:
        text += ("现在进入收尾：不再发起任何新的联网请求，只能调用reply。回答只依据已经取得的真实资料；"
                 "资料不足或不相关就如实说明还没核实什么，不要说没有网络，不要编造。")
    return text


class NotStarted(ValueError):
    """The Host refused before any Planner/tool work began (busy, identity or adapter policy): safe to retry."""


NOT_STARTED = ("voice_busy", "voice_session_identity_mismatch", "voice_adapter_policy_denied",
               "voice_qq_runtime_unavailable")


def _ident(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class VoiceBridge:
    def __init__(self, service):
        self.s = service
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, dict] = {}

    def scope_for(self, key, session):
        scope = self._active.get(key)
        if not scope or scope['session'] != session or time.monotonic() >= scope['deadline']:
            return None
        return scope

    # ---------------------------------------------------------------- binding
    def _bindings(self) -> dict:
        try:
            data = json.loads((self.s.root / "voice_bindings.json").read_text("utf-8"))
            devices = data["devices"]
            return devices if isinstance(devices, dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            return {}                                     # fail closed

    def binding(self, device_id: str) -> dict:
        """Resolve device -> verified owner private session, or raise ValueError('voice_unbound')."""
        entry = self._bindings().get(device_id)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            raise ValueError("voice_unbound")
        private_id, session = str(entry.get("owner_private_id", "")), str(entry.get("session", ""))
        s = self.s
        pa = s.private_access
        if not private_id or not session or not pa or not s.policy.get("qq_private_familiar_enabled"):
            raise ValueError("voice_unbound")
        row = s.state.db.execute("SELECT session FROM qq_private_bindings WHERE private_id=?",
                                 (private_id,)).fetchone()
        if (not row or row["session"] != session or not pa.binding(private_id)
                or session not in s.policy.get("allowed_qq_sessions", [])
                or session not in pa.sessions()
                or s.state.owner_scope(session, private_id, True) != "owner_private"):
            raise ValueError("voice_unbound")
        return {"private_id": private_id, "session": session}

    # ---------------------------------------------------------------- context
    def recent_history(self, session: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        """Recent device-voice turns of the owner session (QQ messages come from the Host's own message store).

        Bounded, scoped to the bound owner session, with provenance; the Host adds it as reference material."""
        st = self.s.state
        with st._lock:
            rows = st.db.execute(
                "SELECT payload FROM events WHERE platform='qq' AND conversation_id=? AND active=1 "
                "AND origin IN ('external_user','bot_self') ORDER BY occurred_at DESC,rowid DESC LIMIT ?",
                (session, limit * 4)).fetchall()
        out = []
        for row in reversed(rows):
            e = json.loads(row["payload"])
            if (e.get("metadata") or {}).get("channel") != "device_voice":
                continue
            out.append({"speaker": "我" if e.get("origin") == "bot_self" else "主人", "channel": "device_voice",
                        "text": str(e.get("content", ""))[:300]})
        return out[-limit:]

    # ------------------------------------------------------------------- turn
    async def turn(self, r: dict) -> dict:
        extra = set(r) - ALLOWED_FIELDS
        if extra:
            raise ValueError("unexpected_fields")         # spoof attempts are visible, never honoured
        device_id, turn_uid = r.get("device_id"), r.get("turn_uid")
        text = r.get("text")
        if (not isinstance(device_id, str) or not DEVICE_RE.match(device_id)
                or not isinstance(turn_uid, str) or not TURN_UID_RE.match(turn_uid)
                or not isinstance(text, str)):
            raise ValueError("invalid_voice_request")
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError("invalid_voice_text")

        self.s.reload_policy()                            # policy + bindings are re-checked per turn
        b = self.binding(device_id)
        key = IDEMPOTENCY_PREFIX + _ident(device_id, turn_uid)
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                reservation = "voice_execution:" + _ident(device_id, turn_uid)
                digest = _ident(text, b["session"])
                done = self.s.state.get(key)
                if isinstance(done, dict) and done.get("session") == b["session"]:
                    self._same_input(device_id, turn_uid, text, b, done.get("input_hash"), digest)
                    return {**done["result"], "replayed": True}
                # Cache lost (restart) but the turn was persisted: replay the stored reply rather
                # than generating a different one for the same turn_uid - after the SAME input check.
                stored = self.s.state.get_event(self.s.state.stored_event_id(
                    self._event_base(b, f"device-voice:{device_id}:{turn_uid}:reply", "device_voice_reply")))
                if stored:
                    recorded = (self.s.state.get(reservation) or {}).get("input_hash")
                    self._same_input(device_id, turn_uid, text, b, recorded, digest)
                    meta = stored.get("metadata") or {}
                    return {"reply": stored["content"], "mood": meta.get("mood", "neutral"),
                            "memory_stored": None, "persona_version": None, "replayed": True}
                prior = self.s.state.get(reservation)
                if prior and prior.get("status") != "not_started":
                    # An earlier attempt may already have run tools or sent something: never run it again.
                    if prior.get("input_hash") != digest:
                        raise ValueError("voice_turn_conflict")
                    raise ValueError(prior.get("error") or "voice_turn_outcome_unknown")
                session_lock = self._session_locks.setdefault(b["session"], asyncio.Lock())
                try:
                    await asyncio.wait_for(session_lock.acquire(), SESSION_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    raise ValueError("voice_busy") from None     # nothing reserved: the caller may retry
                try:
                    # Reserve durably BEFORE the first Host call so a retry or a restart cannot repeat tools.
                    self.s.state.put(reservation, {"status": "running", "input_hash": digest, "at": time.time()})
                    try:
                        result = await self._run(device_id, turn_uid, text, b)
                    except NotStarted as exc:
                        self.s.state.put(reservation, {"status": "not_started", "input_hash": digest,
                                                       "error": str(exc), "at": time.time()})
                        raise
                    except BaseException as exc:
                        error = ("voice_cancelled" if isinstance(exc, asyncio.CancelledError) else
                                 "voice_timeout" if isinstance(exc, asyncio.TimeoutError) else
                                 (str(exc) or type(exc).__name__)[:100])
                        self.s.state.put(reservation, {"status": "interrupted", "input_hash": digest,
                                                       "error": error, "at": time.time()})
                        raise
                    self.s.state.put(reservation, {"status": "completed", "input_hash": digest, "at": time.time()})
                    return result
                finally:
                    session_lock.release()
        finally:
            if not lock.locked():
                self._locks.pop(key, None)

    def _same_input(self, device_id, turn_uid, text, b, recorded, digest):
        """The turn id may only replay/continue for the SAME words in the SAME session.

        `recorded` is the digest stored with the completed result or the durable reservation. Legacy rows without
        one fall back to the originally stored owner utterance (content + session) when it is available."""
        if recorded is not None:
            if recorded != digest:
                raise ValueError("voice_turn_conflict")
            return
        user = self.s.state.get_event(self.s.state.stored_event_id(
            self._event_base(b, f"device-voice:{device_id}:{turn_uid}", "device_voice_turn")))
        if user and (user.get("content") != text or user.get("conversation_id") != b["session"]):
            raise ValueError("voice_turn_conflict")

    async def _run(self, device_id, turn_uid, text, b) -> dict:
        s = self.s
        session, private_id = b["session"], b["private_id"]
        blocked = content_rules.blocked(text)
        persona_version = (s.state.persona() or {}).get("version")
        tools = []
        reply, mood = REFUSAL, "gentle"
        # The owner's utterance is stored once, before planning, under the same id _persist() uses.
        uid_key = f"device-voice:{device_id}:{turn_uid}"
        history = self.recent_history(session)       # earlier device turns only: read BEFORE this utterance is stored
        host_id = await s.resolve_qq_identity(private_id, timeout=0.7)
        owner = s.state.ingest({**self._event_base(b, uid_key, "device_voice_turn"),
            "actor_id": private_id, "origin": "external_user", "content": text,
            **({"person_id": host_id} if host_id else {}),
            "source_id": "device-voice:" + _ident(device_id, turn_uid, session), "occurred_at": time.time(),
            "sharing_scope": "conversation", "learning_allowed": False,
            "metadata": {"channel": "device_voice", "device_id": device_id, "is_private": True,
                         "source_chat_id": session, "chat_id": session, "identity_source": "voice_binding",
                         "material": "asr_text"}})
        if host_id:
            s.state.put("host_person:" + owner["person_id"], str(host_id))
        if not blocked:
            # Per-attempt secret: only this process, the authenticated Host capability and the Host's hooks know it.
            host_key = _ident(device_id, turn_uid, session, os.urandom(16).hex())
            host_mid = "device-voice:" + host_key
            self._active[host_key] = {"session": session, "message": (text[:300], owner["event_id"], private_id),
                                      "deadline": time.monotonic() + SCOPE_SECONDS,
                                      "turn_deadline": time.monotonic() + HOST_SECONDS}   # the Host turn's own budget
            # The private reply gate for this explicit device-origin message id (same gate QQ messages use).
            s.state.put(s.qq_reply_gate_key(session, host_mid), {"message_id": host_mid, "session_id": session,
                "eligible": True, "reason": "verified_device_owner", "claim": None,
                "receipt_requested": receipt_requested(text), "created_at": time.time()})
            index = "qq_reply_index:" + session
            s.state.put(index, ([host_mid] + [x for x in s.state.get(index, []) if x != host_mid])[:512])
            completed = False
            try:
                decision = await asyncio.wait_for(s.ctx.call_capability(
                    "unified.voice", operation="run", key=host_key, session_id=session,
                    owner_id=private_id, account_id=str(s.policy["qq_bot_account"]), text=text,
                    history=history, timeout_s=HOST_SECONDS, timeout_ms=int(PLUGIN_SECONDS * 1000) + 1000),
                    PLUGIN_SECONDS)
                completed = True
                if not isinstance(decision, dict) or decision.get("success") is not True:
                    error = str((decision or {}).get("error") if isinstance(decision, dict) else "") or "voice_host_failed"
                    raise (NotStarted if error in NOT_STARTED else ValueError)(error[:100])
            finally:
                self._active.pop(host_key, None)
                if not completed:
                    # Tell the Host to stop; whatever side effects it already committed cannot be undone, which
                    # is why the reservation written by turn() makes a retry fail instead of repeating them.
                    try:
                        await asyncio.shield(asyncio.wait_for(s.ctx.call_capability(
                            "unified.voice", operation="cancel", key=host_key, timeout_ms=1500), 1.5))
                    except (Exception, asyncio.CancelledError):
                        pass
            reply = str(decision.get("reply", "")).strip()
            mood = decision.get("mood") if decision.get("mood") in MOODS else "neutral"
            tools = [t for t in decision.get("tools", []) if isinstance(t, dict)][:32]
            if not reply:
                raise RuntimeError("empty_voice_reply")
            if content_rules.blocked(reply):
                reply, mood = REFUSAL, "gentle"
            reply = reply[:1200]

        result = {"reply": reply, "mood": mood, "memory_stored": False, "persona_version": persona_version,
                  "pipeline": "maisaka_full_turn", "tools": tools}
        if not blocked and isinstance(decision.get("elapsed_ms"), int):
            result["elapsed_ms"] = decision["elapsed_ms"]              # Host-side duration, for live budget measurement
        # The completed result survives a lost caller; memory synchronisation below is best effort.
        s.state.put(IDEMPOTENCY_PREFIX + _ident(device_id, turn_uid),
                    {"session": session, "result": dict(result), "input_hash": _ident(text, session),
                     "at": time.time()})
        try:
            result["memory_stored"] = await asyncio.wait_for(
                self._persist(device_id, turn_uid, text, reply, b, skip_memory=blocked, mood=mood), 1.5)
        except (Exception, asyncio.CancelledError):
            pass
        return result

    def _event_base(self, b: dict, event_id: str, event_type: str) -> dict:
        return {"platform": "qq", "bot_account_id": str(self.s.policy["qq_bot_account"]),
                "conversation_id": b["session"], "event_id": event_id, "event_type": event_type}

    async def _persist(self, device_id, turn_uid, text, reply, b, skip_memory, mood="neutral") -> bool:
        s = self.s
        session, private_id = b["session"], b["private_id"]
        account = str(s.policy["qq_bot_account"])
        now = time.time()
        host_id = await s.resolve_qq_identity(private_id, timeout=0.7)
        base = {"platform": "qq", "bot_account_id": account, "conversation_id": session,
                "sharing_scope": "conversation", "learning_allowed": False,
                "metadata": {"channel": "device_voice", "device_id": device_id, "is_private": True,
                             "source_chat_id": session, "chat_id": session,
                             "identity_source": "voice_binding", "material": "asr_text"}}
        uid_key = f"device-voice:{device_id}:{turn_uid}"
        owner = s.state.ingest({**base, "event_id": uid_key, "event_type": "device_voice_turn",
                                "actor_id": private_id, "origin": "external_user", "content": text,
                                "occurred_at": now, **({"person_id": host_id} if host_id else {})})
        reply_base = {**base, "metadata": {**base["metadata"], "mood": mood}}
        s.state.ingest({**reply_base, "event_id": uid_key + ":reply", "event_type": "device_voice_reply",
                        "actor_id": account, "origin": "bot_self", "content": reply, "occurred_at": now + 0.001})
        if skip_memory:
            return False
        try:
            written = await s.memory(
                "ingest_text", external_id=uid_key, source_type="chat_summary",
                text=f"[设备语音对话] 主人：{text}\n我：{reply}", chat_id=session,
                person_ids=[host_id] if host_id else [], participants=[], timestamp=now,
                tags=["unified", "device_voice"],
                metadata={"unified_event_id": owner["event_id"], "origin": "external_user",
                          "source_id": uid_key, "source_platform": "qq", "source_chat_id": session,
                          "chat_id": session, "channel": "device_voice", "is_private": True,
                          "sharing_scope": "conversation", "public_summary": False,
                          "category": "设备语音", "learning_path": "device_voice"})
        except Exception:
            return False
        # Host replays of the same external_id report skipped/exists: that is also stored memory.
        return bool(written.get("success")) and written.get("detail") != "chat_filtered"
