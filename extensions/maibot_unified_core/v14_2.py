"""v14.2 switches and parameters. Each feature has an independent switch under policy['v14_2'].

Invalid or out-of-range values fall back to the safe default; nothing here flips any unrelated
token-optimization flag.
"""
from __future__ import annotations

DEFAULTS = {
    'feedback': {'enabled': True, 'parse_daily_limit': 12},
    'impressions': {'enabled': True, 'window_days': 14, 'promotion_points': 6, 'min_gap_seconds': 7200},
    'daily_learning': {'enabled': False, 'hour': 3, 'max_batch_calls': 3, 'max_retries': 2,
                       'batch_events': 10, 'excerpt_chars': 600,
                       # Selective sampling: only material received within the last `fresh_hours` is ever considered, at most
                       # `scope_cap` locally preselected candidates per scope, then <= max_batch_calls x batch_events events a day.
                       # Older raw events are HISTORY (retained, never replayed, never marked learned or failed).
                       'fresh_hours': 24, 'scope_cap': 20},
    'exploration': {'enabled': False, 'daily_opportunities': 3, 'cooldown_seconds': 3600,
                    'dedup_seconds': 86400, 'platforms': ['bilibili', 'xiaohongshu', 'web']},
}
BOUNDS = {
    ('feedback', 'parse_daily_limit'): (0, 50),
    ('impressions', 'window_days'): (1, 60), ('impressions', 'promotion_points'): (1, 50),
    ('impressions', 'min_gap_seconds'): (0, 86400),
    ('daily_learning', 'hour'): (0, 23), ('daily_learning', 'max_batch_calls'): (1, 3),
    ('daily_learning', 'max_retries'): (0, 2), ('daily_learning', 'batch_events'): (1, 10),
    ('daily_learning', 'excerpt_chars'): (50, 600),
    ('daily_learning', 'fresh_hours'): (12, 48), ('daily_learning', 'scope_cap'): (5, 50),
    ('exploration', 'daily_opportunities'): (0, 3), ('exploration', 'cooldown_seconds'): (3600, 86400),
    ('exploration', 'dedup_seconds'): (86400, 7 * 86400),
}


def section(policy, name):
    base = dict(DEFAULTS[name])
    raw = ((policy or {}).get('v14_2') or {}).get(name)
    if not isinstance(raw, dict):
        return base
    for key, default in DEFAULTS[name].items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(default, bool):
            if type(value) is bool:
                base[key] = value
        elif isinstance(default, int):
            low, high = BOUNDS.get((name, key), (0, 10**9))
            if type(value) is int and low <= value <= high:
                base[key] = value
        elif isinstance(default, list):
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                base[key] = [x for x in value if x in default]
    return base


def enabled(policy, name):
    return section(policy, name)['enabled'] is True
