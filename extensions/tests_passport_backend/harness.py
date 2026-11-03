"""Backend test harness: the REAL maibot_unified_core store/private-access/service code with a FAKE Host
(ctx.llm / unified.memory / person). Simulation only - the live Host and its memory DB are not touched."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# media.py imports Pillow at module level; voice tests never touch media, so a stub keeps the test
# environment free of an image dependency. (The production environment has the real Pillow.)
try:
    import PIL  # noqa: F401
except ImportError:
    import types
    _pil = types.ModuleType("PIL")
    _pil.Image = types.ModuleType("PIL.Image")
    _pil.ImageOps = types.ModuleType("PIL.ImageOps")
    sys.modules.update({"PIL": _pil, "PIL.Image": _pil.Image, "PIL.ImageOps": _pil.ImageOps})

from maibot_unified_core.private_access import PrivateAccess          # noqa: E402
from maibot_unified_core.service import UnifiedService                # noqa: E402
from maibot_unified_core.store import State                           # noqa: E402

BOT = "20001"
GROUP_ID, GROUP_SESSION = "9001", "qq:group:9001"
OWNER_GROUP_ACTOR = "u-owner"
OWNER_PRIVATE_ID, OWNER_SESSION = "priv-owner", "qq:private:owner"
OTHER_PRIVATE_ID, OTHER_SESSION = "priv-other", "qq:private:other"
DEVICE = "dev-test1"
TURN_UID = "a" * 32


class FakeMemory:
    """Scoped by chat_id exactly like the Host capability: search only sees what was ingested there."""

    def __init__(self):
        self.items: list[dict] = []
        self.fail = False
        self.filtered = False

    def call(self, operation, **kw):
        if operation == "ingest_text":
            if self.fail:
                raise RuntimeError("host down")
            if self.filtered:
                return {"success": True, "detail": "chat_filtered", "skipped_ids": ["x"]}
            if any(i["external_id"] == kw["external_id"] for i in self.items):
                return {"success": True, "detail": "exists", "skipped_ids": [kw["external_id"]]}
            self.items.append(dict(kw))
            return {"success": True, "stored_ids": [kw["external_id"]]}
        if operation == "search":
            hits = [{"content": i["text"], "metadata": i["metadata"], "hash": "h:" + i["external_id"],
                     "source": "chat_summary:" + i["chat_id"]}
                    for i in self.items if i["chat_id"] == kw["chat_id"]]
            return {"success": True, "hits": hits}
        return {"success": True}


class FakeLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.reply = {"reply": "好呀，明天见。", "mood": "pleased"}
        self.raw = None
        self.fail = False

    async def generate(self, prompt, task_name, max_tokens, temperature):
        self.calls.append({"prompt": prompt, "task": task_name, "max_tokens": max_tokens})
        if self.fail:
            return {"success": False}
        return {"success": True, "response": self.raw if self.raw is not None else json.dumps(self.reply, ensure_ascii=False)}


class FakePerson:
    async def get_id(self, platform, actor):
        return "host-" + actor


class FakeVoiceHost:
    """The Host's `unified.voice` capability, faked at its RPC boundary (the real one is exercised by host_tests/)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.cancels: list[dict] = []
        self.result = {"success": True, "reply": "好呀，明天见。", "mood": "neutral", "delivery": "device_voice_buffered",
                       "tools": [{"name": "reply", "success": True}], "elapsed_ms": 1234}
        self.delay = 0.0
        self.raises: BaseException | None = None
        self.during = None                         # optional callable(kw) run while the Host "works"

    async def call(self, operation, **kw):
        if operation == "cancel":
            self.cancels.append(kw)
            return {"cancel_requested": True}
        assert operation == "run"
        self.calls.append(kw)
        if self.during:
            self.during(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return dict(self.result)


class FakeCtx:
    def __init__(self):
        self.memory = FakeMemory()
        self.llm = FakeLLM()
        self.person = FakePerson()
        self.voice_host = FakeVoiceHost()
        self.opened: list[dict] = []
        self.open_fail = False
        self.logger = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    async def call_capability(self, name, operation=None, timeout_ms=None, **kw):
        if name == "unified.voice":
            return await self.voice_host.call(operation, **kw)
        if name == "chat.open_session":
            # The Host's capability, faked at its RPC boundary (the real handler is exercised by host_tests/).
            self.opened.append(dict(kw))
            if self.open_fail:
                return {"success": False, "error": "boom"}
            return {"success": True, "stream": {"session_id": "pp-session:" + kw["user_id"], "platform": kw["platform"],
                    "user_id": kw["user_id"], "scope": kw["scope"] or None, "is_group_session": False}}
        assert name == "unified.memory"
        return self.memory.call(operation, **kw)


class BackendCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ctx = FakeCtx()
        self.policy = {
            "qq_bot_account": BOT, "qq_group_sessions": {GROUP_ID: GROUP_SESSION},
            "allowed_qq_sessions": [GROUP_SESSION], "qq_private_familiar_enabled": True,
            "qq_owner_bindings": {GROUP_SESSION: OWNER_GROUP_ACTOR}, "voice_uid": 4242,
            "decisions_enabled": True,
        }
        self.write_policy()
        self.svc = UnifiedService(self.ctx, root=self.root)
        self.svc.reload_policy()
        self.svc.state = State(self.root / "state.sqlite", self.svc.policy)
        self.svc.private_access = PrivateAccess(self.svc)
        self.svc.state.init_persona({"name": "大肥鱼", "core": "骄傲又好奇的小家伙"}, {})
        self.bind_private(OWNER_PRIVATE_ID, OWNER_SESSION, OWNER_GROUP_ACTOR)
        self.write_voice_bindings({DEVICE: {"enabled": True, "owner_private_id": OWNER_PRIVATE_ID,
                                            "session": OWNER_SESSION}})
        self.svc.reload_policy()

    async def asyncTearDown(self):
        self.svc.state.close()
        self.tmp.cleanup()

    def write_policy(self):
        (self.root / "policy.json").write_text(json.dumps(self.policy))

    def write_voice_bindings(self, devices):
        (self.root / "voice_bindings.json").write_text(json.dumps({"devices": devices}))

    def bind_private(self, private_id, session, group_actor):
        self.svc.state.db.execute(
            "INSERT OR REPLACE INTO qq_private_bindings(private_id,group_session,group_actor,verified,session) "
            "VALUES(?,?,?,?,?)", (private_id, GROUP_SESSION, group_actor, 1.0, session))

    def req(self, text="我明天要去医院复查", **kw):
        return {"op": "voice_turn", "device_id": DEVICE, "turn_uid": TURN_UID, "text": text, **kw}
