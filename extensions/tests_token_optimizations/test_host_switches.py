"""Host 侧开关：默认关闭，配置损坏/非有限值回退关闭。"""
import json
import os
import tempfile
import unittest
from unittest import mock

import host_loader


class TestSwitches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.opt = host_loader.load("common.token_optimizations")

    def setUp(self):
        keys = ("MAIBOT_TOKEN_OPT_CONFIG", "MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP", "MAIBOT_TOKEN_OPT_REPLY_TERMINATION")
        env = {k: v for k, v in os.environ.items() if k not in keys}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.opt.reset_cache_for_tests)
        self.opt.reset_cache_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config_file(self, text):
        path = os.path.join(self.tmp.name, "opt.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.environ["MAIBOT_TOKEN_OPT_CONFIG"] = path
        self.opt.reset_cache_for_tests()

    def test_everything_defaults_to_off(self):
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])
        self.assertFalse(self.opt.get_settings("reply_termination")["enabled"])

    def test_corrupt_unreadable_or_wrong_typed_config_stays_off(self):
        for text in ("{not json", "[]", '"x"', '{"person_fact_dedup": "on"}',
                     '{"person_fact_dedup": {"enabled": "true"}}', '{"person_fact_dedup": {"enabled": 1}}'):
            self.config_file(text)
            self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"], text)
        os.environ["MAIBOT_TOKEN_OPT_CONFIG"] = os.path.join(self.tmp.name, "missing.json")
        self.opt.reset_cache_for_tests()
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])
        os.environ["MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP"] = "maybe"  # 无法解析的环境变量：关闭
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])

    def test_any_invalid_known_field_closes_the_whole_section(self):
        bad_fields = ('"ttl_seconds": NaN', '"ttl_seconds": Infinity', '"ttl_seconds": -1', '"ttl_seconds": 0',
                      '"ttl_seconds": true', '"ttl_seconds": "60"', '"max_entries": 0.5', '"max_entries": 2.5',
                      '"max_entries": -3', '"max_entries": 0', '"max_entries": true', '"empty_ttl_seconds": null',
                      '"partial_ttl_seconds": []')
        for field in bad_fields:
            self.config_file('{"person_fact_dedup": {"enabled": true, %s}}' % field)
            settings = self.opt.get_settings("person_fact_dedup")
            self.assertFalse(settings["enabled"], field)
            self.assertEqual(settings["ttl_seconds"], 900.0, field)  # 整个 section 回到默认
            self.assertEqual(settings["max_entries"], 512, field)
        self.config_file('{"reply_termination": {"enabled": true, "mode": ""}}')
        self.assertFalse(self.opt.get_settings("reply_termination")["enabled"])
        self.config_file('{"reply_termination": {"enabled": true, "mode": 5}}')
        self.assertFalse(self.opt.get_settings("reply_termination")["enabled"])

    def test_integral_float_for_integer_field_is_accepted_and_sections_are_independent(self):
        self.config_file('{"person_fact_dedup": {"enabled": true, "max_entries": 64.0}, '
                         '"reply_termination": {"enabled": true, "mode": 7}}')
        settings = self.opt.get_settings("person_fact_dedup")
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["max_entries"], 64)
        self.assertIsInstance(settings["max_entries"], int)
        self.assertFalse(self.opt.get_settings("reply_termination")["enabled"])  # 另一个 section 损坏不影响本 section

    def test_env_cannot_bypass_corrupt_config_or_invalid_values(self):
        os.environ["MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP"] = "1"
        for text in ("{not json", "[]", '{"person_fact_dedup": "x"}', '{"person_fact_dedup": {"max_entries": 0.5}}'):
            self.config_file(text)
            self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"], text)
        os.environ["MAIBOT_TOKEN_OPT_CONFIG"] = os.path.join(self.tmp.name, "missing.json")
        self.opt.reset_cache_for_tests()
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])
        self.config_file('{"person_fact_dedup": {"enabled": true}}')
        os.environ["MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP"] = "garbage"
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])  # 无效环境变量不保留已启用状态

    def test_valid_file_and_env_override(self):
        self.config_file(json.dumps({"person_fact_dedup": {"enabled": True, "ttl_seconds": 60},
                                     "reply_termination": {"enabled": True, "mode": "explicit"}}))
        self.assertTrue(self.opt.get_settings("person_fact_dedup")["enabled"])
        self.assertEqual(self.opt.get_settings("person_fact_dedup")["ttl_seconds"], 60.0)
        os.environ["MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP"] = "0"
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])

    def test_only_reply_termination_cannot_enable_dedup(self):
        os.environ["MAIBOT_TOKEN_OPT_REPLY_TERMINATION"] = "1"
        self.assertTrue(self.opt.get_settings("reply_termination")["enabled"])
        self.assertFalse(self.opt.get_settings("person_fact_dedup")["enabled"])


class TestReplyTerminationPure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = host_loader.load("maisaka.reply_termination")

    def decide(self, mode="explicit", final=True, enabled=True):
        rt = self.rt
        return rt.decide(
            {"enabled": enabled, "mode": mode}, rt.TurnFacts(0, ("m1",), sources_verified=True), ["reply"],
            [rt.ToolOutcome("reply", True, "m1", final, (True,), 1)], rt.RuntimeFacts(False, False, True))

    def test_only_explicit_true_stops(self):
        self.assertTrue(self.decide().stop)
        self.assertEqual(self.decide(mode="single_step").reason, "unknown_mode")
        self.assertEqual(self.decide(mode="").reason, "unknown_mode")
        self.assertEqual(self.decide(final=None).reason, "no_explicit_final_signal")
        self.assertEqual(self.decide(final=False).reason, "no_explicit_final_signal")
        self.assertEqual(self.decide(enabled=False).reason, "disabled")

    def test_unverified_sources_continue_by_default(self):
        rt = self.rt
        self.assertFalse(rt.TurnFacts().sources_verified)
        decision = rt.decide({"enabled": True, "mode": "explicit"}, rt.TurnFacts(0, ("m1",)), ["reply"],
                             [rt.ToolOutcome("reply", True, "m1", True, (True,), 1)], rt.RuntimeFacts(False, False, True))
        self.assertEqual(decision.reason, "batch_source_unverified_or_non_chat")

    def test_unknown_runtime_state_continues(self):
        rt = self.rt
        for facts in (rt.RuntimeFacts(None, False, True), rt.RuntimeFacts(False, None, True),
                      rt.RuntimeFacts(False, False, None)):
            decision = rt.decide({"enabled": True, "mode": "explicit"},
                                 rt.TurnFacts(0, ("m1",), sources_verified=True), ["reply"],
                                 [rt.ToolOutcome("reply", True, "m1", True, (True,), 1)], facts)
            self.assertEqual(decision.reason, "runtime_state_unknown")

    def test_extract_signal_only_accepts_booleans(self):
        rt = self.rt
        self.assertIs(rt.extract_final_reply_signal({"final_reply": True}), True)
        self.assertIsNone(rt.extract_final_reply_signal({"final_reply": "true"}))
        self.assertIsNone(rt.extract_final_reply_signal({"arguments": "{bad"}))
        self.assertIsNone(rt.extract_final_reply_signal(None))


if __name__ == "__main__":
    unittest.main()
