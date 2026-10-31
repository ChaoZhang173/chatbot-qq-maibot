"""P2/P3 实验：稳定规则 / 低频快照 / 即时内容分层，以及工具目录的位置与长度选项。

纯函数 + 小型进程内状态，不依赖 maibot_sdk，便于离线测试。默认全部关闭
（policy.json 中没有 ``token_optimizations.context_layers.enabled=true`` 时旧路径不变）。
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional

STABLE_MARKER = "[unified-core-stable-v1]"
SNAPSHOT_MARKER = "[unified-core-snapshot-v1]"
DYNAMIC_MARKER = "[unified-core-context-v1]"  # 与旧路径相同，旧块与新即时块可被同一规则替换
ALL_MARKERS = (STABLE_MARKER, SNAPSHOT_MARKER, DYNAMIC_MARKER)
ITEM_ID_PREFIX = "unified-context-"

CATALOG_HEAD = "<system-reminder>\n以下工具当前未直接暴露给你"
_CATALOG_LINE = re.compile(r"^(\d+)\. ([^:\n]+): (.*)$")
SHORT_NOTE = "（以上描述已缩略；工具名完整，需要完整说明与参数请先调用 tool_search。）"

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "refresh_messages": 20,
    "max_age_seconds": 1800.0,
    "prefix_position": "tail",  # tail | front
    "tool_catalog": "full",  # full | short
    "tool_catalog_position": "tail",  # tail | front
    "short_description_chars": 40,
}
_CHOICES = {
    "prefix_position": {"tail", "front"},
    "tool_catalog": {"full", "short"},
    "tool_catalog_position": {"tail", "front"},
}


class StaleContext(Exception):
    """await 期间授权/策略/人设/证据版本发生变化，本次构建产物作废。"""


def settings(policy: Any) -> Dict[str, Any]:
    """读取并校验设置；fail-closed：任何异常、类型错误或非法字段都整体回到默认（enabled=False）。"""

    try:
        raw = ((policy or {}).get("token_optimizations") or {}).get("context_layers") or {}
        if not isinstance(raw, dict):
            return dict(DEFAULTS)
        result = dict(DEFAULTS)
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                return dict(DEFAULTS)
            result["enabled"] = raw["enabled"]
        for key, allowed in _CHOICES.items():
            if key in raw:
                if raw[key] not in allowed:
                    return dict(DEFAULTS)
                result[key] = raw[key]
        for key in ("refresh_messages", "max_age_seconds", "short_description_chars"):
            if key in raw:
                value = raw[key]
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value <= 0):
                    return dict(DEFAULTS)
                if isinstance(DEFAULTS[key], int) and value != int(value):
                    return dict(DEFAULTS)  # 整数字段拒绝 0.5、2.5 等，不取整
                result[key] = type(DEFAULTS[key])(value)
    except Exception:
        return dict(DEFAULTS)
    return result


class SnapshotStore:
    """按会话隔离的低频快照；只统计外部消息，不统计机器人分段、工具和内部事件。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_sessions: int = 256) -> None:
        self._clock = clock
        self._max_sessions = max_sessions
        self._items: Dict[str, Dict[str, Any]] = {}
        self._counts: Dict[str, int] = {}

    def _bound(self) -> None:
        """快照与计数共用同一容量上限，淘汰最早的会话时两者一起清除（下次访问即重建）。"""
        while len(self._counts) > self._max_sessions:
            oldest = next(iter(self._counts))
            self._counts.pop(oldest, None)
            self._items.pop(oldest, None)
        while len(self._items) > self._max_sessions:
            oldest = next(iter(self._items))
            self._items.pop(oldest, None)
            self._counts.pop(oldest, None)

    def note_external(self, session: str) -> None:
        count = self._counts.pop(session, 0) + 1
        self._counts[session] = count  # 重新插入 = 最近使用
        self._bound()

    def count(self, session: str) -> int:
        return self._counts.get(session, 0)

    def get(self, session: str, key: Any, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """返回仍有效的快照；键（人设版本/修订/授权指纹）变化、满 N 条或超时均失效。"""

        entry = self._items.get(session)
        if entry is None:
            return None
        if entry["key"] != key:
            return None
        if self._counts.get(session, 0) - entry["count_at"] >= int(cfg["refresh_messages"]):
            return None
        if self._clock() - entry["built_at"] >= float(cfg["max_age_seconds"]):
            return None
        return entry

    def put(self, session: str, key: Any, text: str) -> Dict[str, Any]:
        entry = {"key": key, "text": text, "built_at": self._clock(),
                 "count_at": self._counts.get(session, 0)}
        self._items.pop(session, None)
        self._items[session] = entry
        self._counts.setdefault(session, 0)
        self._bound()
        return entry

    def invalidate(self, session: Optional[str] = None) -> None:
        """失效快照；计数保留（计数只表示“自上次快照以来”，由 count_at 差值决定）。"""
        if session is None:
            self._items.clear()
        else:
            self._items.pop(session, None)

    def sizes(self) -> Dict[str, int]:
        return {"snapshots": len(self._items), "counts": len(self._counts)}


def block_item(marker: str, text: str, session_id: str, *, timestamp: str, kind: str) -> dict:
    """与 plugin.context_item 同一 Context Item 结构；item_id、时间戳均由内容决定，字节稳定。"""

    body = marker + "\n" + text
    digest = hashlib.sha256((session_id + "\n" + body).encode()).hexdigest()[:24]
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": f"{ITEM_ID_PREFIX}{kind}-{digest}", "logical_turn_id": None, "timestamp": timestamp},
        "parts": [{"type": "text", "text": body}],
    }


def _texts(item: Any) -> List[str]:
    if not isinstance(item, dict):
        return []
    return [str(p.get("text", "")) for p in item.get("parts", []) if isinstance(p, dict) and p.get("type") == "text"]


def is_layer_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if not str((item.get("meta") or {}).get("item_id", "")).startswith(ITEM_ID_PREFIX):
        return False
    return any(text.startswith(marker) for text in _texts(item) for marker in ALL_MARKERS)


def is_catalog_item(item: Any) -> bool:
    return (isinstance(item, dict) and item.get("item_type") == "UserMessageItem"
            and any(text.startswith(CATALOG_HEAD) for text in _texts(item)))


def shorten_catalog(item: dict, chars: int) -> dict:
    """只缩短每个工具的描述；保留编号、工具名、头尾说明，其他字段原样。"""

    result = copy.deepcopy(item)
    for part in result["parts"]:
        if not (isinstance(part, dict) and part.get("type") == "text" and part["text"].startswith(CATALOG_HEAD)):
            continue
        lines = part["text"].split("\n")
        out: List[str] = []
        for line in lines:
            match = _CATALOG_LINE.match(line)
            if match and len(match.group(3)) > chars:
                line = f"{match.group(1)}. {match.group(2)}: {match.group(3)[:chars].rstrip()}…"
            out.append(line)
        if SHORT_NOTE not in out:
            closing = len(out) - 1 if out and out[-1] == "</system-reminder>" else len(out)
            out.insert(closing, SHORT_NOTE)
        part["text"] = "\n".join(out)
    return result


def _leading_system_count(items: List[Any]) -> int:
    count = 0
    for item in items:
        if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
            count += 1
        else:
            break
    return count


def apply_layout(items: List[Any], layers: Dict[str, str], session_id: str, cfg: Dict[str, Any],
                 snapshot_stamp: str) -> List[Any]:
    """返回新列表：先移除本插件旧块（重复注入替换），再按选项放置；其余项（含图片、工具调用/返回）原样。

    layers: stable / snapshot / dynamic 文本（可缺省）。工具目录仅在存在时处理，缺失则不凭空创建。
    """

    base = [item for item in items if not is_layer_item(item)]
    catalog_index = next((i for i, item in enumerate(base) if is_catalog_item(item)), None)
    catalog = None
    if catalog_index is not None:
        catalog = base[catalog_index]
        if cfg["tool_catalog"] == "short":
            catalog = shorten_catalog(catalog, int(cfg["short_description_chars"]))
        if cfg["tool_catalog_position"] == "front":
            del base[catalog_index]
            catalog_index = None
        else:
            base[catalog_index] = catalog
    stable = [block_item(STABLE_MARKER, layers["stable"], session_id, timestamp="1970-01-01T00:00:00+00:00",
                         kind="stable")] if layers.get("stable") else []
    snapshot = [block_item(SNAPSHOT_MARKER, layers["snapshot"], session_id, timestamp=snapshot_stamp,
                           kind="snapshot")] if layers.get("snapshot") else []
    dynamic = [block_item(DYNAMIC_MARKER, layers["dynamic"], session_id,
                          timestamp=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                          kind="dynamic")] if layers.get("dynamic") else []
    front: List[Any] = []
    tail: List[Any] = []
    if cfg["prefix_position"] == "front":
        front += stable + snapshot
    else:
        tail += stable + snapshot
    if catalog_index is None and catalog is not None:  # 目录前移
        front.append(catalog)
    tail += dynamic
    at = _leading_system_count(base)
    return base[:at] + front + base[at:] + tail
