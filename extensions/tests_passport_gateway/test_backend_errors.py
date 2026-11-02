"""Backend (unified core voice_turn) error mapping and the end-to-end brain budget chain."""
import json
import os
import re
import unittest
from pathlib import Path

from passport_gateway.backend import BackendError, parse_response
from passport_gateway.config import Config

BACKEND = Path(os.environ.get("PASSPORT_BACKEND_PATCH",
               Path(__file__).resolve().parents[2] / "backend_patch" / "unified_core"))
HEADER = Path(os.environ.get("PASSPORT_FIRMWARE_SESSION_H",
              Path(__file__).resolve().parents[2].parent / "ai-passport" / "main" / "pp_session.h"))


def reply(**kw):
    return json.dumps({"ok": True, "reply": "好。", "mood": "neutral", **kw}).encode()


def failure(error):
    return json.dumps({"ok": False, "error": error}).encode()


class ErrorMappingTests(unittest.TestCase):
    def code(self, error):
        with self.assertRaises(BackendError) as cm:
            parse_response(failure(error))
        return cm.exception.code

    def test_full_pipeline_errors_map_to_distinct_device_states(self):
        self.assertEqual(self.code("voice_timeout"), "brain_timeout")
        self.assertEqual(self.code("voice_busy"), "busy")
        self.assertEqual(self.code("voice_unbound"), "backend_unbound")
        self.assertEqual(self.code("voice_no_reply"), "brain_failed")
        self.assertEqual(self.code("voice_turn_outcome_unknown"), "brain_failed")
        self.assertEqual(self.code("voice_turn_conflict"), "brain_failed")
        self.assertEqual(self.code("admin_only"), "internal")

    def test_extra_pipeline_fields_do_not_disturb_the_reply(self):
        parsed = parse_response(reply(pipeline="maisaka_full_turn", tools=[{"name": "reply", "success": True}],
                                      elapsed_ms=4200, replayed=True))
        self.assertEqual((parsed.text, parsed.mood), ("好。", "neutral"))


@unittest.skipUnless((BACKEND / "voice.py").exists(), "backend patch checkout not found")
class BudgetChainTests(unittest.TestCase):
    """Host 22 s < plugin 23 s < gateway 25 s < core watchdog 27 s < device 28 s: each layer gives up after the
    one inside it, so a late Host answer can never be spoken after the device has already failed the turn."""

    def constants(self):
        voice = (BACKEND / "voice.py").read_text()
        values = {k: float(v) for k, v in re.findall(r"^(HOST_SECONDS|PLUGIN_SECONDS|SCOPE_SECONDS) = ([\d.]+)", voice, re.M)}
        watchdog = re.search(r"asyncio\.wait\(\(work,disconnected\),timeout=(\d+)", (BACKEND / "service.py").read_text())
        values["WATCHDOG"] = float(watchdog.group(1))
        return values

    def test_chain_is_strictly_ordered(self):
        c, cfg = self.constants(), Config()
        self.assertLess(c["HOST_SECONDS"], c["PLUGIN_SECONDS"])
        self.assertLess(c["PLUGIN_SECONDS"], cfg.brain_timeout_s)
        self.assertLessEqual(c["PLUGIN_SECONDS"], c["SCOPE_SECONDS"])
        self.assertLess(cfg.brain_timeout_s, c["WATCHDOG"])
        if HEADER.exists():
            device = int(re.search(r"#define\s+PP_THINK_BRAIN_MS\s+(\d+)", HEADER.read_text()).group(1)) / 1000
            self.assertLess(c["WATCHDOG"], device)
        self.assertEqual((c["HOST_SECONDS"], c["PLUGIN_SECONDS"], c["WATCHDOG"]), (22.0, 23.0, 27.0))

    def test_host_capability_deadline_limit_matches_the_bridge(self):
        cap = Path(os.environ.get("PASSPORT_HOST_PATCH", Path(__file__).resolve().parents[2] / "host_patch")) \
            / "src/plugin_runtime/capabilities/unified_voice.py"
        if not cap.exists():
            self.skipTest("host patch checkout not found")
        limit = float(re.search(r"0 < timeout <= ([\d.]+)", cap.read_text()).group(1))
        self.assertGreaterEqual(limit, self.constants()["HOST_SECONDS"])


if __name__ == "__main__":
    unittest.main()
