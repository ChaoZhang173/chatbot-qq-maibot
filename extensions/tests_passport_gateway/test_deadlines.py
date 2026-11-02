"""Device and gateway deadlines must be consistent: valid work inside the server's per-stage budgets must
never be abandoned by the device first. Reads the firmware constants from the sibling checkout."""
import os
import re
import unittest
from pathlib import Path

from passport_gateway.config import Config

HEADER = Path(os.environ.get("PASSPORT_FIRMWARE_SESSION_H",
              Path(__file__).resolve().parents[2].parent / "ai-passport" / "main" / "pp_session.h"))


class DeadlineTests(unittest.TestCase):
    def test_device_stage_deadlines_exceed_server_budgets(self):
        if not HEADER.exists():
            self.skipTest("firmware checkout not found")
        d = {k: int(v) for k, v in re.findall(r"#define\s+(PP_THINK_\w+)\s+(\d+)", HEADER.read_text())}
        cfg = Config()
        margin = 2000
        self.assertGreaterEqual(d["PP_THINK_ASR_MS"], cfg.asr_timeout_s * 1000 + margin)
        self.assertGreaterEqual(d["PP_THINK_BRAIN_MS"], cfg.brain_timeout_s * 1000 + margin)
        self.assertGreaterEqual(d["PP_THINK_TTS_MS"], cfg.tts_first_audio_timeout_s * 1000 + margin)
        server_total = (cfg.asr_timeout_s + cfg.brain_timeout_s + cfg.tts_first_audio_timeout_s) * 1000
        self.assertGreater(d["PP_THINK_TOTAL_MS"], server_total)
        self.assertEqual(server_total, 65000)          # the figure documented in voice-protocol.md (25 + 25 + 15 s)
        self.assertGreaterEqual(d["PP_THINK_TOTAL_MS"], d["PP_THINK_ASR_MS"] + d["PP_THINK_BRAIN_MS"] + d["PP_THINK_TTS_MS"])

    def test_default_budgets_and_the_provider_standalone_defaults_agree(self):
        import inspect

        from passport_gateway.providers.dashscope import DashScopeAsr
        from passport_gateway.providers.siliconflow import SiliconFlowAsr
        cfg = Config()
        self.assertEqual((cfg.asr_timeout_s, cfg.brain_timeout_s, cfg.tts_first_audio_timeout_s), (25.0, 25.0, 15.0))
        for cls in (SiliconFlowAsr, DashScopeAsr):
            self.assertEqual(inspect.signature(cls.__init__).parameters["timeout"].default, cfg.asr_timeout_s, cls.__name__)

    def test_downlink_pacing_fits_the_device_queue(self):
        cfg = Config()
        queue_ms = 16384 / cfg.pace_rate_bytes_s * 1000          # PP_DOWN_Q_BYTES / playback rate
        frame_ms = 2048 / cfg.pace_rate_bytes_s * 1000
        self.assertLess(cfg.pace_lead_ms + frame_ms, queue_ms)


if __name__ == "__main__":
    unittest.main()
