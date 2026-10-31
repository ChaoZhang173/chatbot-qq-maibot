"""Validate the room, visit, time and modality of supplied live evidence."""
import math,re,time

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
