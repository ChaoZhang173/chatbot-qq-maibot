"""离线费用估算：按每次调用的实际 hit/miss/output 计算，配置驱动，只读。

原则：
- 不修改、不追写历史用量或账单；输出是“按给定价格配置回算的估算”。
- 价格缺失 => 该调用费用为 None（未定价），绝不当作 0 元。
- 峰谷价依赖工作日/节假日日历：日历只来自配置，不内置节假日；日期不在配置声明的
  已核实范围内时，按“工作日”和“非工作日”两种口径给出区间 (low, high)，标记为估算。
- 缺少 hit/miss 记录而有输入 token 时，按“全命中”与“全未命中”给出区间。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import csv
import json
import math
import sqlite3

DAY_ANY = "any"
DAY_WORKDAY = "workday"
DAY_OFFDAY = "offday"
_FIELDS = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


class PricingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    day_type: str
    start: dtime
    end: dtime
    price_in: float
    price_out: float
    cache_price_in: float

    def matches_time(self, moment: dtime) -> bool:
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # 跨午夜


@dataclass(frozen=True)
class Calendar:
    known_from: Optional[date]
    known_to: Optional[date]
    holidays: frozenset
    makeup_workdays: frozenset

    def day_types(self, day: date) -> Tuple[frozenset, bool]:
        """返回 (可能的日类型集合, 是否确定)。日期在已核实范围内才确定。"""

        if self.known_from and self.known_to and self.known_from <= day <= self.known_to:
            if day in self.holidays:
                return frozenset({DAY_OFFDAY}), True
            if day in self.makeup_workdays:
                return frozenset({DAY_WORKDAY}), True
            return frozenset({DAY_OFFDAY if day.weekday() >= 5 else DAY_WORKDAY}), True
        return frozenset({DAY_WORKDAY, DAY_OFFDAY}), False


@dataclass(frozen=True)
class Pricing:
    timezone: ZoneInfo
    calendar: Calendar
    models: Mapping[str, Tuple[Rule, ...]]
    currency: str = "CNY"


def _parse_time(text: Any) -> dtime:
    try:
        hour, minute = str(text).split(":")
        return dtime(int(hour), int(minute))
    except (TypeError, ValueError) as exc:
        raise PricingConfigError(f"invalid time {text!r}") from exc


def _parse_date(text: Any) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError as exc:
        raise PricingConfigError(f"invalid date {text!r}") from exc


def _price(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise PricingConfigError(f"{name} must be a finite number >= 0")
    return float(value)


def load_pricing(config: Mapping[str, Any]) -> Pricing:
    if not isinstance(config, Mapping):
        raise PricingConfigError("pricing config must be an object")
    try:
        zone = ZoneInfo(str(config.get("timezone") or ""))
    except Exception as exc:
        raise PricingConfigError("timezone is required (IANA name)") from exc
    cal = config.get("calendar") or {}
    calendar = Calendar(
        known_from=_parse_date(cal["known_from"]) if cal.get("known_from") else None,
        known_to=_parse_date(cal["known_to"]) if cal.get("known_to") else None,
        holidays=frozenset(_parse_date(x) for x in cal.get("holidays", [])),
        makeup_workdays=frozenset(_parse_date(x) for x in cal.get("makeup_workdays", [])),
    )
    models: Dict[str, Tuple[Rule, ...]] = {}
    for name, entry in (config.get("models") or {}).items():
        rules = []
        for raw in (entry or {}).get("rules", []):
            day_type = raw.get("day_type", DAY_ANY)
            if day_type not in {DAY_ANY, DAY_WORKDAY, DAY_OFFDAY}:
                raise PricingConfigError(f"unknown day_type {day_type!r}")
            start, end = _parse_time(raw.get("start", "00:00")), _parse_time(raw.get("end", "00:00"))
            if "start" not in raw and "end" not in raw:
                start, end = dtime(0, 0), dtime(0, 0)  # 全天
            rules.append(Rule(day_type, start, end, _price(raw.get("price_in"), "price_in"),
                              _price(raw.get("price_out"), "price_out"),
                              _price(raw.get("cache_price_in"), "cache_price_in")))
        if rules:
            models[str(name)] = tuple(rules)
    return Pricing(zone, calendar, models, str(config.get("currency") or "CNY"))


def _rule_for(rules: Iterable[Rule], day_type: str, moment: dtime) -> Optional[Rule]:
    for rule in rules:
        if rule.day_type not in (DAY_ANY, day_type):
            continue
        if rule.start == rule.end == dtime(0, 0) or rule.matches_time(moment):
            return rule
    return None


def _to_local(value: Any, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 无时区的时间戳按配置时区解释（Host 数据库保存服务器本地时间）。
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


class InvalidUsage(ValueError):
    pass


def _strict_count(record: Mapping[str, Any], name: str, *, required: bool) -> Optional[int]:
    """严格解析 token 计数：缺失、bool、负数、非整数、非有限值都是无效用量，绝不静默当作 0。

    缺失的可选字段（hit/miss）返回 None，表示“没有这条缓存记录”。CSV 导出的数字字符串可以接受。
    """

    value = record.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise InvalidUsage(f"{name}_missing")
        return None
    if isinstance(value, bool):
        raise InvalidUsage(f"{name}_not_integer")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise InvalidUsage(f"{name}_not_integer") from exc
    if not isinstance(value, (int, float)):
        raise InvalidUsage(f"{name}_not_integer")
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        raise InvalidUsage(f"{name}_not_integer")
    number = int(value)
    if number < 0:
        raise InvalidUsage(f"{name}_negative")
    return number


def _count(record: Mapping[str, Any], name: str) -> int:
    """仅用于展示 token 合计；无效值计 0 且该调用已在费用中标为未定价。"""
    try:
        return _strict_count(record, name, required=False) or 0
    except InvalidUsage:
        return 0


@dataclass
class CallCost:
    low: Optional[float]
    high: Optional[float]
    exact: bool
    reasons: Tuple[str, ...]


def estimate_call(record: Mapping[str, Any], pricing: Pricing) -> CallCost:
    reasons: List[str] = []
    model = str(record.get("model_name") or record.get("model") or "")
    rules = pricing.models.get(model)
    if not rules:
        return CallCost(None, None, False, ("no_price_for_model",))
    try:
        local = _to_local(record.get("timestamp"), pricing.timezone)
    except (TypeError, ValueError):
        return CallCost(None, None, False, ("invalid_timestamp",))
    try:
        prompt = _strict_count(record, "prompt_tokens", required=True)
        out = _strict_count(record, "completion_tokens", required=True)
        hit = _strict_count(record, "prompt_cache_hit_tokens", required=False)
        miss = _strict_count(record, "prompt_cache_miss_tokens", required=False)
    except InvalidUsage as exc:
        return CallCost(None, None, False, (f"invalid_usage:{exc}",))
    # Host 记录：prompt_tokens 是输入总数；有缓存记录时 hit+miss 必须等于它
    # （只上报 hit 时 Host 以 prompt-hit 作为 miss）。不一致说明数据有误，不猜测。
    hit, miss = hit or 0, miss or 0
    if miss == 0 and hit > 0:
        if hit > prompt:
            return CallCost(None, None, False, ("invalid_usage:cache_total_mismatch",))
        miss = prompt - hit
    if hit + miss > 0 and hit + miss != prompt:
        return CallCost(None, None, False, ("invalid_usage:cache_total_mismatch",))
    cache_unknown = hit + miss == 0 and prompt > 0
    if cache_unknown:
        reasons.append("cache_record_missing")
    day_types, certain = pricing.calendar.day_types(local.date())
    if not certain:
        reasons.append("calendar_unknown")
    totals: List[float] = []
    for day_type in sorted(day_types):
        rule = _rule_for(rules, day_type, local.time())
        if rule is None:
            return CallCost(None, None, False, tuple(reasons + ["no_price_for_time"]))
        in_candidates = ([prompt * rule.cache_price_in, prompt * rule.price_in] if cache_unknown
                         else [hit * rule.cache_price_in + miss * rule.price_in])
        for value in in_candidates:
            totals.append((value + out * rule.price_out) / 1_000_000)
    return CallCost(min(totals), max(totals), len(set(round(t, 12) for t in totals)) == 1, tuple(reasons))


def _parse_bound(text: str, zone: ZoneInfo) -> datetime:
    """过滤边界：无时区的按给定时区解释；纯日期表示当天 00:00。"""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingConfigError(f"invalid since/until {text!r}") from exc
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment


def load_records(path: Path, kind: str, since: Optional[str] = None, until: Optional[str] = None,
                 tz: Optional[ZoneInfo] = None) -> List[Dict[str, Any]]:
    """读取用量记录。since（含）/until（不含）对所有格式统一生效，必须提供 tz。

    时间戳无法解析的记录在过滤时保留（随后会被标为 invalid_timestamp 未定价），不静默丢弃。
    """
    if (since or until) and tz is None:
        raise PricingConfigError("since/until require an explicit timezone")
    rows = _read_rows(path, kind)
    if not (since or until):
        return rows
    low = _parse_bound(since, tz) if since else None
    high = _parse_bound(until, tz) if until else None
    kept = []
    for row in rows:
        try:
            moment = _to_local(row.get("timestamp"), tz)
        except (TypeError, ValueError):
            kept.append(row)
            continue
        if (low is None or moment >= low) and (high is None or moment < high):
            kept.append(row)
    return kept


def _read_rows(path: Path, kind: str) -> List[Dict[str, Any]]:
    since = until = None
    if kind == "jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif kind == "json":
        rows = json.loads(path.read_text(encoding="utf-8"))
    elif kind == "csv":
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif kind == "sqlite":
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # 只读
        connection.row_factory = sqlite3.Row
        try:
            query = ("SELECT model_name, request_type, timestamp, prompt_tokens, completion_tokens, "
                     "prompt_cache_hit_tokens, prompt_cache_miss_tokens, cost FROM llm_usage WHERE 1=1")
            args: List[Any] = []
            if since:
                query += " AND timestamp >= ?"
                args.append(since)
            if until:
                query += " AND timestamp < ?"
                args.append(until)
            rows = [dict(row) for row in connection.execute(query, args)]
        finally:
            connection.close()
    else:
        raise PricingConfigError(f"unknown input kind {kind!r}")
    if not isinstance(rows, list):
        raise PricingConfigError("usage input must be a list of records")
    return [row for row in rows if isinstance(row, dict)]


def build_report(records: Iterable[Mapping[str, Any]], pricing: Pricing) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "priced_calls": 0, "unpriced_calls": 0, "estimated_calls": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "hit_tokens": 0, "miss_tokens": 0,
        "low": 0.0, "high": 0.0, "recorded_cost_sum": 0.0, "reasons": defaultdict(int)})
    for record in records:
        key = str(record.get("request_type") or "unknown")
        group = groups[key]
        cost = estimate_call(record, pricing)
        group["calls"] += 1
        for field, name in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                            ("hit_tokens", "prompt_cache_hit_tokens"), ("miss_tokens", "prompt_cache_miss_tokens")):
            group[field] += _count(record, name)
        try:
            group["recorded_cost_sum"] += float(record.get("cost") or 0)
        except (TypeError, ValueError):
            pass
        for reason in cost.reasons:
            group["reasons"][reason] += 1
        if cost.low is None:
            group["unpriced_calls"] += 1
            continue
        group["priced_calls"] += 1
        group["low"] += cost.low
        group["high"] += cost.high
        if not cost.exact:
            group["estimated_calls"] += 1
    out = {"currency": pricing.currency, "groups": {}, "note": (
        "按给定价格配置回算的估算，不是账单；recorded_cost_sum 为数据库原始值，未改写。"
        "unpriced_calls>0 时 low/high 只是已定价调用的合计下限，不含未定价调用。")}
    total = {"calls": 0, "priced_calls": 0, "unpriced_calls": 0, "estimated_calls": 0, "low": 0.0, "high": 0.0}
    for key in sorted(groups):
        group = groups[key]
        group["reasons"] = dict(group["reasons"])
        # 没有任何已定价调用时费用未知（None），不呈现为 0 元。
        group["low"], group["high"] = ((round(group["low"], 6), round(group["high"], 6))
                                       if group["priced_calls"] else (None, None))
        group["complete"] = group["unpriced_calls"] == 0
        group["exact"] = group["complete"] and group["estimated_calls"] == 0
        out["groups"][key] = group
        for name in ("calls", "priced_calls", "unpriced_calls", "estimated_calls"):
            total[name] += group[name]
        if group["low"] is not None:
            total["low"] += group["low"]
            total["high"] += group["high"]
    total["low"], total["high"] = ((round(total["low"], 6), round(total["high"], 6))
                                   if total["priced_calls"] else (None, None))
    total["complete"] = total["unpriced_calls"] == 0
    total["exact"] = total["complete"] and total["estimated_calls"] == 0
    out["total"] = total
    return out


def _cost_text(item: Mapping[str, Any]) -> str:
    if item["low"] is None:
        return "UNKNOWN (no priced calls; not zero)"
    text = f"{item['low']}..{item['high']}"
    return text if item["complete"] else f">={text} (lower bound; {item['unpriced_calls']} unpriced/invalid calls excluded)"


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"currency={report['currency']}"]
    for key, group in report["groups"].items():
        lines.append(f"{key}: calls={group['calls']} unpriced={group['unpriced_calls']} "
                     f"cost={_cost_text(group)} exact={group['exact']}")
    total = report["total"]
    lines.append(f"TOTAL calls={total['calls']} unpriced={total['unpriced_calls']} "
                 f"cost={_cost_text(total)} complete={total['complete']} exact={total['exact']}")
    lines.append(report["note"])
    return "\n".join(lines)
