"""Opt-in experience features and versioned language assets; no live defaults."""
import hashlib
import json
from pathlib import Path

ASSETS = Path(__file__).with_name('text_assets')


def enabled(service, feature):
    return (getattr(service, 'policy', {}).get('experience_features') or {}).get(feature) is True


def version(service):
    # Authorization changes invalidate drafts even when the human version label was not bumped.
    data = {k: service.policy.get(k) for k in ('experience_features', 'experience_sharing',
        'allowed_qq_sessions', 'qq_group_sessions', 'qq_bot_account', 'bili_bot_uid', 'internal_cross_platform',
        'public_qq_sessions', 'public_topics', 'shared_groups', 'qq_group_recall_shared_enabled', 'daily_diary')}
    data['assets'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ASSETS.glob('*.txt')}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def prompt(name):
    return (ASSETS / (name + '.txt')).read_text(encoding='utf-8').strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
