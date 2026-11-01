"""P0：离线费用估算（token_cost.report + scripts/token_cost_report.py 入口）。"""
import contextlib
import csv
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from token_cost import report

REPO = Path(__file__).resolve().parents[2]
M = 1_000_000


def config(**calendar):
    return {
        "currency": "CNY", "timezone": "Asia/Shanghai",
        "calendar": {"known_from": "2026-10-01", "known_to": "2026-12-31",
                     "holidays": ["2026-10-02"], "makeup_workdays": ["2026-11-07"], **calendar},
        "models": {"m": {"rules": [
            {"day_type": "workday", "start": "08:00", "end": "20:00", "price_in": 2, "price_out": 8, "cache_price_in": 0.2},
            {"day_type": "workday", "start": "20:00", "end": "08:00", "price_in": 1, "price_out": 4, "cache_price_in": 0.1},
            {"day_type": "offday", "price_in": 1, "price_out": 4, "cache_price_in": 0.1}]}}}


def rec(ts="2026-10-28T10:00:00", **kw):
    base = {"model_name": "m", "request_type": "t", "timestamp": ts, "prompt_tokens": M,
            "completion_tokens": 100_000, "prompt_cache_hit_tokens": 400_000, "prompt_cache_miss_tokens": 600_000, "cost": 0}
    base.update(kw)
    return base


class TestEstimate(unittest.TestCase):
    def setUp(self):
        self.pricing = report.load_pricing(config())

    def cost(self, record):
        return report.estimate_call(record, self.pricing)

    def assertExact(self, record, value):
        result = self.cost(record)
        self.assertTrue(result.exact, result.reasons)
        self.assertAlmostEqual(result.low, value, places=6)
        self.assertAlmostEqual(result.high, value, places=6)

    def test_cached_uncached_and_output_prices_per_call(self):
        self.assertExact(rec(), 0.4 * 0.2 + 0.6 * 2 + 0.1 * 8)

    def test_peak_offpeak_and_overnight_boundary(self):
        self.assertExact(rec("2026-10-28T19:59:59"), 2.08)
        self.assertExact(rec("2026-10-28T20:00:00"), 1.04)
        self.assertExact(rec("2026-10-29T07:59:59"), 1.04)  # 跨午夜时段
        self.assertExact(rec("2026-10-29T08:00:00"), 2.08)

    def test_weekend_explicit_holiday_and_makeup_workday(self):
        self.assertExact(rec("2026-10-31T10:00:00"), 1.04)  # 周六
        self.assertExact(rec("2026-10-02T10:00:00"), 1.04)  # 配置中显式声明的“节假日”（周五）
        self.assertExact(rec("2026-11-07T10:00:00"), 2.08)  # 配置中的调休工作日（周六）

    def test_unknown_calendar_gives_a_range_not_exact(self):
        result = self.cost(rec("2027-01-05T10:00:00"))
        self.assertFalse(result.exact)
        self.assertAlmostEqual(result.low, 1.04, places=6)
        self.assertAlmostEqual(result.high, 2.08, places=6)
        self.assertIn("calendar_unknown", result.reasons)
        self.assertFalse(report.load_pricing({**config(), "calendar": {}}).calendar.day_types(
            __import__("datetime").date(2026, 10, 28))[1])

    def test_timezone_conversion_and_naive_timestamps(self):
        self.assertExact(rec("2026-10-28T02:00:00+00:00"), 2.08)  # = 10:00 北京时间
        self.assertExact(rec("2026-10-28T12:30:00+00:00"), 1.04)  # = 20:30 北京时间

    def test_missing_cache_record_is_a_range(self):
        result = self.cost(rec(prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None))
        self.assertFalse(result.exact)
        self.assertIn("cache_record_missing", result.reasons)
        self.assertAlmostEqual(result.low, 1.0, places=6)
        self.assertAlmostEqual(result.high, 2.8, places=6)
        absent = rec()
        del absent["prompt_cache_hit_tokens"], absent["prompt_cache_miss_tokens"]
        self.assertFalse(self.cost(absent).exact)

    def test_hit_only_record_follows_host_definition(self):
        self.assertExact(rec(prompt_cache_miss_tokens=0), 2.08)  # Host：miss = prompt - hit
        self.assertExact(rec(prompt_tokens="1000000", completion_tokens="100000",
                             prompt_cache_hit_tokens="400000", prompt_cache_miss_tokens="600000"), 2.08)  # CSV 字符串

    def test_missing_price_is_never_free(self):
        result = self.cost(rec(model_name="unknown", cost=0))
        self.assertIsNone(result.low)
        self.assertEqual(result.reasons, ("no_price_for_model",))

    def test_invalid_usage_is_not_priced_as_exact_or_zero(self):
        cases = {
            "missing prompt": {"prompt_tokens": None}, "missing output": {"completion_tokens": None},
            "negative": {"prompt_tokens": -5, "completion_tokens": 0, "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None},
            "bool": {"prompt_tokens": True}, "fraction": {"completion_tokens": 1.5},
            "nan": {"prompt_tokens": float("nan")}, "inf": {"completion_tokens": float("inf")},
            "text": {"prompt_tokens": "abc"}, "list": {"prompt_tokens": [1]},
            "mismatch": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 10},
            "hit over total": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 0},
            "negative hit": {"prompt_cache_hit_tokens": -1},
        }
        for name, patch in cases.items():
            result = self.cost(rec(**patch))
            self.assertIsNone(result.low, name)
            self.assertFalse(result.exact, name)
            self.assertTrue(result.reasons[0].startswith("invalid_usage:"), (name, result.reasons))
        absent = rec()
        del absent["prompt_tokens"], absent["completion_tokens"]
        self.assertIsNone(self.cost(absent).low)

    def test_invalid_timestamp_and_missing_time_rule(self):
        self.assertEqual(self.cost(rec("not-a-time")).reasons, ("invalid_timestamp",))
        partial = report.load_pricing({**config(), "models": {"m": {"rules": [
            {"day_type": "workday", "start": "08:00", "end": "20:00", "price_in": 1, "price_out": 1, "cache_price_in": 1}]}}})
        self.assertIsNone(report.estimate_call(rec("2026-10-28T21:00:00"), partial).low)

    def test_pricing_config_rejects_bad_values(self):
        for bad in ({"price_in": -1}, {"price_out": float("nan")}, {"cache_price_in": True}, {"price_in": None}):
            cfg = config()
            cfg["models"]["m"]["rules"][0].update(bad)
            with self.assertRaises(report.PricingConfigError):
                report.load_pricing(cfg)
        with self.assertRaises(report.PricingConfigError):
            report.load_pricing({**config(), "timezone": ""})


class TestReport(unittest.TestCase):
    def test_aggregation_keeps_recorded_cost_and_flags_incomplete_totals(self):
        pricing = report.load_pricing(config())
        result = report.build_report([rec(), rec(model_name="other", cost=7), rec(prompt_tokens=-1)], pricing)
        total = result["total"]
        self.assertEqual((total["calls"], total["priced_calls"], total["unpriced_calls"]), (3, 1, 2))
        self.assertFalse(total["complete"])
        self.assertFalse(total["exact"])
        self.assertAlmostEqual(total["low"], 2.08, places=6)
        self.assertEqual(result["groups"]["t"]["recorded_cost_sum"], 7.0)  # 原始值不改写
        self.assertIn("lower bound", report.render_text(result))

    def test_all_unpriced_is_unknown_not_zero(self):
        pricing = report.load_pricing({**config(), "models": {}})
        result = report.build_report([rec(), rec()], pricing)
        self.assertIsNone(result["total"]["low"])
        self.assertIsNone(result["groups"]["t"]["high"])
        self.assertIn("UNKNOWN", report.render_text(result))
        self.assertNotIn("cost=0.0", report.render_text(result))

    def test_example_pricing_file_is_not_free(self):
        example = json.loads((REPO / "config/token-pricing.example.json").read_text(encoding="utf-8"))
        result = report.build_report([rec()], report.load_pricing(example))
        self.assertIsNone(result["total"]["low"])


class TestFiltersAndCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.rows = [rec("2026-10-27T10:00:00"), rec("2026-10-28T10:00:00"), rec("2026-10-29T10:00:00"),
                     rec("2026-10-28T02:00:00+00:00")]
        spec = importlib.util.spec_from_file_location("token_cost_cli", REPO / "scripts/token_cost_report.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.pricing_path = self.dir / "pricing.json"
        self.pricing_path.write_text(json.dumps(config()))
        self.tz = report.load_pricing(config()).timezone

    def write(self, kind):
        path = self.dir / ("usage." + kind)
        if kind == "jsonl":
            path.write_text("\n".join(json.dumps(r) for r in self.rows))
        elif kind == "json":
            path.write_text(json.dumps(self.rows))
        elif kind == "csv":
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self.rows[0]))
                writer.writeheader()
                writer.writerows(self.rows)
        else:
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE llm_usage (model_name, request_type, timestamp, prompt_tokens, "
                               "completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, cost)")
            for r in self.rows:
                connection.execute("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?)", tuple(r[k] for k in (
                    "model_name", "request_type", "timestamp", "prompt_tokens", "completion_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cost")))
            connection.commit()
            connection.close()
        return path

    def test_since_until_apply_to_every_format_in_the_given_timezone(self):
        for kind in ("jsonl", "json", "csv", "sqlite"):
            path = self.write(kind)
            self.assertEqual(len(report.load_records(path, kind)), 4, kind)
            # 北京时间 10-28 当天：10:00 本地 + 02:00 UTC（=10:00 本地）两条
            day = report.load_records(path, kind, "2026-10-28", "2026-10-29", self.tz)
            self.assertEqual(len(day), 2, kind)
            self.assertEqual(len(report.load_records(path, kind, since="2026-10-29", tz=self.tz)), 1, kind)
            self.assertEqual(len(report.load_records(path, kind, until="2026-10-28T10:00:00", tz=self.tz)), 1, kind)
            with self.assertRaises(report.PricingConfigError):
                report.load_records(path, kind, since="2026-10-28")  # 没有明确时区就拒绝

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli.main(["--pricing", str(self.pricing_path), *args])
        return out.getvalue()

    def test_cli_entry_filters_and_reports(self):
        path = self.write("jsonl")
        full = json.loads(self.run_cli("--input", str(path), "--kind", "jsonl", "--format", "json"))
        day = json.loads(self.run_cli("--input", str(path), "--kind", "jsonl", "--format", "json",
                                      "--since", "2026-10-28", "--until", "2026-10-29"))
        self.assertEqual(full["total"]["calls"], 4)
        self.assertEqual(day["total"]["calls"], 2)
        self.assertAlmostEqual(day["total"]["low"], 4.16, places=6)
        self.assertIn("TOTAL", self.run_cli("--input", str(path), "--kind", "jsonl"))

    def test_cli_rejects_bad_filter_and_missing_input(self):
        path = self.write("csv")
        with self.assertRaises(SystemExit):
            self.run_cli("--input", str(path), "--kind", "csv", "--since", "yesterday")
        with self.assertRaises(SystemExit):
            self.run_cli("--input", str(self.dir / "absent.jsonl"), "--kind", "jsonl")


if __name__ == "__main__":
    unittest.main()
