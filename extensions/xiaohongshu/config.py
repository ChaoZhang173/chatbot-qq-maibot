"""Strict configuration. Every switch defaults to off; secrets never live here."""
import json, os, re, stat
from pathlib import Path
from urllib.parse import urlsplit

SWITCHES = ('enabled', 'publish_enabled', 'comment_enabled', 'reply_enabled', 'browse_enabled', 'autonomy_enabled')
DEFAULTS = {
    'enabled': False, 'publish_enabled': False, 'comment_enabled': False, 'reply_enabled': False,
    'browse_enabled': False, 'autonomy_enabled': False,
    'dry_run': True,
    'bound_uid': '',
    'service_url': 'http://127.0.0.1:18060',
    'token_env': 'XHS_MCP_AUTH_TOKEN', 'token_file': '',
    'core_socket': '/srv/maibot/unified/core.sock',
    'state_dir': '/srv/maibot-xiaohongshu/state',
    'media_dir': '/srv/maibot-xiaohongshu/publish-media',
    'visibility': '公开可见',
    'text_card': {'enabled': False, 'font_path': ''},
    'limits': {
        'daily_posts': 1, 'daily_comments': 4, 'daily_replies': 8, 'daily_model_calls': 16, 'daily_browses': 6,
        'per_thread_daily': 2, 'per_author_daily': 2,
        'min_gap_post': 21600, 'min_gap_comment': 1800, 'min_gap_reply': 300,
        'notification_poll_seconds': 600, 'autonomy_seconds': 3600, 'tick_seconds': 30,
        'max_notifications': 20, 'max_comments_read': 30, 'active_start_hour': 9, 'active_end_hour': 23,
        'identity_ttl_seconds': 300, 'http_timeout': 120, 'write_timeout': 400, 'max_response_bytes': 2097152,
        'max_post_images': 1, 'image_reuse_days': 30, 'interests': ['游戏', '动漫', '美食', 'AI'],
    },
}
LIMIT_BOUNDS = {
    'daily_posts': (0, 3), 'daily_comments': (0, 20), 'daily_replies': (0, 30), 'daily_model_calls': (0, 60),
    'daily_browses': (0, 24), 'per_thread_daily': (1, 5), 'per_author_daily': (1, 10),
    'min_gap_post': (3600, 604800), 'min_gap_comment': (300, 86400), 'min_gap_reply': (60, 86400),
    'notification_poll_seconds': (120, 7200), 'autonomy_seconds': (600, 86400), 'tick_seconds': (5, 300),
    'max_notifications': (1, 50), 'max_comments_read': (5, 100), 'active_start_hour': (0, 23), 'active_end_hour': (1, 24),
    'identity_ttl_seconds': (30, 3600), 'http_timeout': (10, 600), 'write_timeout': (30, 900),
    'max_response_bytes': (65536, 8 * 1024 * 1024), 'max_post_images': (1, 1), 'image_reuse_days': (0, 365),
}
VISIBILITIES = ('公开可见', '仅自己可见')
LOOPBACK ={'127.0.0.1', '::1', 'localhost'}
UID = re.compile(r'[A-Za-z0-9]{6,64}')


class ConfigError(ValueError):
    pass


def validate_url(url):
    """Only an explicit loopback HTTP service; no userinfo, path, query or other host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ConfigError('invalid_service_url') from None
    if (parts.scheme != 'http' or parts.hostname not in LOOPBACK or port is None or parts.username or parts.password
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ConfigError('service_url_must_be_loopback_http_with_port')
    return parts.hostname, port


def validate(raw):
    if not isinstance(raw, dict):
        raise ConfigError('config_must_be_object')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError('unknown_config_keys:' + ','.join(sorted(unknown))[:80])
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    limits = raw.get('limits', {})
    if not isinstance(limits, dict) or set(limits) - set(DEFAULTS['limits']):
        raise ConfigError('invalid_limits')
    card = raw.get('text_card', {})
    if not isinstance(card, dict) or set(card) - set(DEFAULTS['text_card']):
        raise ConfigError('invalid_text_card')
    cfg.update({k: v for k, v in raw.items() if k not in ('limits', 'text_card')})
    cfg['text_card'] = {**DEFAULTS['text_card'], **card}
    if type(cfg['text_card']['enabled']) is not bool or not isinstance(cfg['text_card']['font_path'], str):
        raise ConfigError('invalid_text_card')
    if cfg['text_card']['enabled'] and not os.path.isabs(cfg['text_card']['font_path']):
        raise ConfigError('text_card_font_path_required')
    if cfg['visibility'] not in VISIBILITIES:
        raise ConfigError('invalid_visibility')
    cfg['limits'] = {**DEFAULTS['limits'], **limits}
    for key in (*SWITCHES, 'dry_run'):
        if type(cfg[key]) is not bool:
            raise ConfigError('invalid_switch:' + key)
    for key, (lo, hi) in LIMIT_BOUNDS.items():
        value = cfg['limits'][key]
        if type(value) is not int or not lo <= value <= hi:
            raise ConfigError('invalid_limit:' + key)
    if cfg['limits']['active_end_hour'] <= cfg['limits']['active_start_hour']:
        raise ConfigError('invalid_active_hours')
    interests = cfg['limits']['interests']
    if not isinstance(interests, list) or not 1 <= len(interests) <= 8 or any(not isinstance(x, str) or not 1 <= len(x) <= 12 for x in interests):
        raise ConfigError('invalid_interests')
    for key in ('bound_uid', 'service_url', 'token_env', 'token_file', 'core_socket', 'state_dir', 'media_dir'):
        if not isinstance(cfg[key], str):
            raise ConfigError('invalid_string:' + key)
    if cfg['bound_uid'] and not UID.fullmatch(cfg['bound_uid']):
        raise ConfigError('invalid_bound_uid')
    if cfg['token_env'] and not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', cfg['token_env']):
        raise ConfigError('invalid_token_env')
    validate_url(cfg['service_url'])
    for key in ('core_socket', 'state_dir', 'media_dir'):
        if not os.path.isabs(cfg[key]):
            raise ConfigError('absolute_path_required:' + key)
    if cfg['token_file'] and not os.path.isabs(cfg['token_file']):
        raise ConfigError('absolute_path_required:token_file')
    if cfg['enabled'] and not cfg['bound_uid']:
        raise ConfigError('bound_uid_required_when_enabled')
    return cfg


def load(path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ConfigError('config_unreadable') from None
    return validate(raw)


def load_token(cfg, environ=None):
    """Bearer token from the runtime environment or a private file; never logged."""
    environ = os.environ if environ is None else environ
    if cfg['token_file']:
        path = Path(cfg['token_file'])
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ConfigError('token_file_must_be_private_regular_file')
        token = path.read_text(encoding='utf-8').strip()
    else:
        token = str(environ.get(cfg['token_env'], '')).strip()
    if not 16 <= len(token) <= 512 or re.search(r'\s', token):
        raise ConfigError('service_token_missing_or_invalid')
    return token
