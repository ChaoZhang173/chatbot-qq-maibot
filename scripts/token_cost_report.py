"""离线 token 费用估算入口（只读）。

示例：
  python scripts/token_cost_report.py --pricing config/token-pricing.example.json \
      --input usage.jsonl --kind jsonl
  python scripts/token_cost_report.py --pricing my-pricing.json --input usage.db --kind sqlite --since 2026-10-24
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))

from token_cost import report  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pricing", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--kind", choices=("jsonl", "json", "csv", "sqlite"), default="jsonl")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        pricing = report.load_pricing(json.loads(args.pricing.read_text(encoding="utf-8")))
        # since（含）/until（不含）对所有输入格式生效；无时区的时间按定价配置的时区解释。
        records = report.load_records(args.input, args.kind, args.since, args.until, pricing.timezone)
    except (report.PricingConfigError, ValueError, OSError) as exc:
        raise SystemExit(f"error: {exc}")
    result = report.build_report(records, pricing)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else report.render_text(result))


if __name__ == "__main__":
    main()
