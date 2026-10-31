"""Bounded, fresh observations; media acquisition never sends a chat message."""
from __future__ import annotations
import json, math, re, time
from pathlib import Path

DEFAULT = {'enabled': False, 'interval_seconds': 30, 'sample_seconds': 12,
           'daily_samples': 120, 'night_samples': 30, 'max_age_seconds': 60}
OBSERVATION_PATH = Path('/srv/bilibot/bridge/live-observation.json')

def policy(config):
    raw = config.get('live_media', DEFAULT)
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT) or type(raw['enabled']) is not bool:
        raise ValueError('invalid_live_media_policy')
    limits = {'interval_seconds': (30, 300), 'sample_seconds': (5, 15),
              'daily_samples': (0, 240), 'night_samples': (0, 60), 'max_age_seconds': (20, 90)}
    for key, (low, high) in limits.items():
        if type(raw[key]) is not int or not low <= raw[key] <= high:
            raise ValueError('invalid_live_media_policy')
    return dict(raw)

def fresh(value, room_id, visit_id, now=None, max_age=60):
    now = time.time() if now is None else now
    if not isinstance(value, dict) or value.get('status') != 'observed': return None
    if value.get('room_id') != str(room_id) or value.get('visit_id') != str(visit_id): return None
    if not re.fullmatch(r'[a-f0-9]{64}', str(value.get('sample_id', ''))): return None
    start, end = value.get('started_at'), value.get('ended_at')
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end)): return None
    if not 0 <= end - start <= 25 or not -5 <= now - end <= max_age: return None
    if value.get('audio_status') not in ('ok', 'silent', 'unclear', 'unavailable', 'failed'): return None
    if value.get('visual_status') not in ('ok', 'unavailable', 'failed'): return None
    result = {k: value[k] for k in ('room_id', 'visit_id', 'sample_id', 'started_at', 'ended_at', 'audio_status', 'visual_status')}
    for key, limit in (('audio_text', 1800), ('visual_summary', 1000), ('uncertainty', 500)):
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > limit: return None
        result[key] = text
    if result['audio_status'] != 'ok': result['audio_text'] = ''
    if result['visual_status'] != 'ok': result['visual_summary'] = ''
    if not result['audio_text'].strip() and not result['visual_summary'].strip(): return None
    return result

def read_observation(config, visit, now=None, path=OBSERVATION_PATH):
    p = policy(config)
    if not p['enabled']: return None
    try:
        path = Path(path)
        if path.stat().st_size > 16000: return None
        return fresh(json.loads(path.read_text()), visit['room']['room_id'], visit['id'], now, p['max_age_seconds'])
    except (OSError, ValueError, TypeError, KeyError): return None
