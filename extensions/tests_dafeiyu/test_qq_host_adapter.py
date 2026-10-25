"""Boundary tests; SDK/Host integration is validated separately on the server."""

import asyncio
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "maibot_unified_core"
import os
HOST_SOURCE = Path(os.environ.get("MAIBOT_SOURCE_ROOT", "/nonexistent/maibot-source"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    sdk = types.ModuleType("maibot_sdk")
    sdk.MaiBotPlugin = object
    sdk.HookHandler = sdk.Tool = lambda *args, **kwargs: lambda function: function
    enums = types.ModuleType("maibot_sdk.types")
    enums.HookMode = types.SimpleNamespace(BLOCKING="blocking")
    enums.HookOrder = types.SimpleNamespace(LATE="late")
    service = types.ModuleType("qq_boundary_test.service")
    service.UnifiedService = object
    with patch.dict(sys.modules, {"maibot_sdk": sdk, "maibot_sdk.types": enums,
                                  "qq_boundary_test.service": service,
                                  "qq_boundary_test.content_rules": load("boundary_content_rules", PACKAGE / "content_rules.py")}):
        return load("maibot_unified_core._boundary_plugin", PACKAGE / "plugin.py")


class CoreHooks(unittest.IsolatedAsyncioTestCase):
    async def test_retry_replaces_context_and_preserves_image_items(self):
        plugin = load_plugin()
        obj = plugin.UnifiedCorePlugin()
        class Service:
            async def qq_context(self, sid):
                return "new authorized version"
        obj.service = Service()
        image = {"item_type": "UserMessageItem", "meta": {"item_id": "original"},
                 "parts": [{"type": "image", "image_format": "png", "image_base64": "AA=="}]}
        old = plugin.context_item("revoked old version", "session-a")
        result = await obj._inject(session_id="session-a", items=[image, old], item_schema_version=1,
                                   tool_definitions=[{"name": "reply"}], selected_history_count=3)
        items = result["modified_kwargs"]["items"]
        self.assertIs(items[0], image)
        self.assertEqual(sum(map(plugin.is_core_context, items)), 1)
        self.assertNotIn("revoked", str(items))
        self.assertEqual(result["modified_kwargs"]["item_schema_version"], 1)
        self.assertEqual(result["modified_kwargs"]["tool_definitions"], [{"name": "reply"}])
        self.assertEqual(result["modified_kwargs"]["session_id"], "session-a")

    async def test_empty_scope_removes_stale_injection(self):
        plugin = load_plugin()
        obj = plugin.UnifiedCorePlugin()
        class Service:
            async def qq_context(self, sid):
                return ""
        obj.service = Service()
        result = await obj._inject(session_id="scope-revoked", items=[plugin.context_item("old", "scope-revoked")])
        self.assertEqual(result["modified_kwargs"]["items"], [])

    async def test_tool_cannot_read_without_host_session(self):
        plugin = load_plugin()
        obj = plugin.UnifiedCorePlugin()
        result = await obj._read_tool("memory", query="anything")
        self.assertFalse(result["success"])


class MemoryBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_plugin_cannot_reach_host_memory(self):
        adapter = load("audit_memory", PACKAGE / "host_adapter.py")
        with self.assertRaises(PermissionError):
            await adapter.unified_memory("other.plugin", "unified.memory", {"operation": "status"})

    async def test_scope_cannot_be_disabled_and_profiles_omit_global_prose(self):
        adapter = load("audit_memory", PACKAGE / "host_adapter.py")
        fake = types.ModuleType("src.services.memory_service")
        fake.memory_service = object()
        with patch.dict(sys.modules, {"src.services.memory_service": fake}):
            with self.assertRaises(ValueError):
                await adapter.unified_memory(adapter.PLUGIN_ID, "unified.memory", {
                    "operation": "search", "query": "x", "chat_id": "a", "respect_filter": False})
        result = adapter._evidence_only({"success": True, "profile_text": "private summary",
            "raw_profile": {"secret": True}, "evidence": [{"source": "chat:a", "raw": {"private": True}}]})
        self.assertNotIn("private", str(result))
        self.assertTrue(result["requires_scope_filter"])


class SurgicalPatch(unittest.TestCase):
    @unittest.skipUnless(HOST_SOURCE.exists(), "local locked source not present")
    def test_apply_is_idempotent_and_drift_is_rejected(self):
        module = load("audit_patch", PACKAGE / "host_patch.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "src"
            folder = root / "plugin_runtime/capabilities"
            folder.mkdir(parents=True)
            registry = folder / "registry.py"
            shutil.copy2(HOST_SOURCE / "plugin_runtime/capabilities/registry.py", registry)
            adapter = PACKAGE / "host_adapter.py"
            self.assertFalse(module.patch(root, adapter)["applied"])
            self.assertTrue(module.patch(root, adapter, True)["applied"])
            self.assertTrue(module.patch(root, adapter, True)["already_installed"])
            registry.write_text(registry.read_text() + "# unrelated user change\n")
            with self.assertRaises(ValueError):
                module.patch(root, adapter, True)


if __name__ == "__main__":
    unittest.main()
