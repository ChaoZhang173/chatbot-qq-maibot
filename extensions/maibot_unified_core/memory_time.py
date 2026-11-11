"""One clock for every model-facing context (Beijing time).

Absolute times are stored on facts/records; the relative wording ("今天早上 07:10") is
computed only when a context is rendered, so a cached retrieval can never carry a stale
"today" across midnight. Unknown times stay unknown: creation time is never substituted.
"""
from __future__ import annotations
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
WEEK = '一二三四五六日'

RULE = ('时间以now（北京时间）为准。记录里的when已按now换算好，直接照用，不要自己推算时间戳；'
        '今天的事不能说成昨天。没有when的记忆表示时间未知，只能当旧事或不确定的事，不能当成最近发生的。'
        '较早的记忆是当时的情况，不代表现在仍然如此。')


def stamp(value):
    """Epoch seconds from a number or ISO string; None when absent or invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec='seconds') if ts else None


def _part(hour):
    if hour < 5:
        return '凌晨'
    if hour < 9:
        return '早上'
    if hour < 12:
        return '上午'
    if hour < 14:
        return '中午'
    if hour < 18:
        return '下午'
    return '晚上'


def relative(value, now=None):
    """Calendar-day wording in Beijing time, e.g. 今天早上 07:10 / 昨天晚上 21:51 / 11月2日（7天前）."""
    ts = stamp(value)
    if ts is None:
        return None
    now = time.time() if now is None else now
    d, n = datetime.fromtimestamp(ts, TZ), datetime.fromtimestamp(now, TZ)
    days = (n.date() - d.date()).days
    clock = d.strftime('%H:%M')
    if days < 0:
        return f'{d.month}月{d.day}日 {clock}（未来时间）'
    if days == 0:
        return f'今天{_part(d.hour)} {clock}'
    if days == 1:
        return f'昨天{_part(d.hour)} {clock}'
    if days == 2:
        return f'前天{_part(d.hour)}'
    if d.year != n.year:
        return f'{d.year}年{d.month}月{d.day}日（{days}天前）'
    return f'{d.month}月{d.day}日（{days}天前）'


def now_line(now=None):
    n = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    return f'{n.year}年{n.month}月{n.day}日 星期{WEEK[n.weekday()]} {_part(n.hour)} {n.strftime("%H:%M")}（北京时间）'


def hit_time(hit):
    """Stored event time of a memory hit: (point, start, end) epoch seconds, any may be None.

    Order: Host time_meta (paragraph columns) -> metadata event_time* -> metadata.timestamp."""
    hit = hit if isinstance(hit, dict) else {}
    sources = [hit.get('time_meta'), hit.get('metadata'), hit]
    point = start = end = None
    for src in sources:
        if not isinstance(src, dict):
            continue
        point = point or stamp(src.get('event_time'))
        start = start or stamp(src.get('event_time_start'))
        end = end or stamp(src.get('event_time_end'))
    if point is None and start is None and end is None:
        meta = hit.get('metadata') if isinstance(hit.get('metadata'), dict) else {}
        point = stamp(meta.get('timestamp'))
    return point, start, end


def describe(point, start=None, end=None, now=None):
    if point:
        return relative(point, now)
    if start and end and relative(start, now) != relative(end, now):
        return f'{relative(start, now)} 至 {relative(end, now)}'
    if start or end:
        return relative(start or end, now)
    return None
