"""Merge the exported profile into an existing MaiBot 1.3.14 instance.

Dry-run by default. Never updates providers, QQ credentials, or group policies.
Run with the instance's Python so MaiBot's own validator is available.
"""
import argparse
import copy
import importlib.metadata
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
from datetime import datetime, timezone
import tomlkit

REPO = Path(__file__).resolve().parents[1]
TASKS = ('planner', 'replyer', 'utils')
FORBIDDEN = {'api_key', 'app_secret', 'password', 'access_token', 'app_id', 'account_id'}


def merge_existing(target, overlay, prefix=''):
    """Reject missing/version-incompatible keys; preserve unrelated live values."""
    for key, value in overlay.items():
        path = f'{prefix}.{key}'.strip('.')
        if key.lower() in FORBIDDEN:
            raise ValueError(f'Credential/identity field forbidden in overlay: {path}')
        if key not in target:
            raise ValueError(f'Missing existing field: {path}; check MaiBot version')
        if isinstance(value, dict):
            merge_existing(target[key], value, path)
        else:
            target[key] = copy.deepcopy(value)


def prepare(bot_raw, model_raw):
    bot = tomlkit.parse(bot_raw.decode())
    model = tomlkit.parse(model_raw.decode())
    overlay = tomllib.loads((REPO / 'config/bot.profile.toml').read_text())
    example = tomllib.loads((REPO / 'config/model_config.example.toml').read_text())
    flash = [x for x in model['models'] if x['name'] == 'deepseek-flash']
    if len(flash) != 1 or flash[0]['model_identifier'] != 'deepseek-flash':
        raise ValueError('Expected one existing deepseek-flash model; configure it privately first')
    if flash[0].get('extra_params', {}).get('thinking', {}).get('type') != 'disabled':
        raise ValueError('Configure thinking=disabled on the existing Flash model first')
    merge_existing(bot, overlay)
    for name in TASKS:
        merge_existing(model['model_task_config'][name], example['model_task_config'][name])
    return tomlkit.dumps(bot), tomlkit.dumps(model)


def validate_with_maibot(instance, bot_text, model_text):
    if importlib.metadata.version('maibot') != '1.3.14':
        raise ValueError('This snapshot targets MaiBot 1.3.14; review before using another version')
    os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)
    os.chdir(instance)
    from src.config.config import Config, ModelConfig, _normalize_loaded_bot_config_dict
    from src.config.config_base import AttributeData
    b, m = tomllib.loads(bot_text), tomllib.loads(model_text)
    b.pop('inner', None)
    m.pop('inner', None)
    Config.from_dict(AttributeData(), _normalize_loaded_bot_config_dict(b))
    ModelConfig.from_dict(AttributeData(), m)


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix='.profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Write after validation and private backup')
    args = parser.parse_args()
    instance = args.instance.resolve()
    paths = [instance / 'config/bot_config.toml', instance / 'config/model_config.toml']
    originals = [p.read_bytes() for p in paths]
    texts = prepare(*originals)
    validate_with_maibot(instance, *texts)
    print('Validated: bot profile and planner/replyer/utils task settings.')
    print('Providers, credentials, adapter policy, and other model tasks are preserved.')
    if not args.apply:
        print('Dry-run complete; no configuration files changed.')
        return
    if any(p.read_bytes() != raw for p, raw in zip(paths, originals)):
        raise ValueError('Concurrent edit detected; no files written')
    backup = instance / 'backups' / ('before-repo-profile-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    for p in paths:
        shutil.copy2(p, backup / p.name)
        (backup / p.name).chmod(0o600)
    try:
        for p, text in zip(paths, texts):
            atomic_write(p, text.encode())
    except Exception:
        for p, raw in zip(paths, originals):
            atomic_write(p, raw)
        raise
    print('Applied. Private backup:', backup)
    print('Verify MaiBot hot reload and one manual QQ message; no API test was sent.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # A validation exception can include live secret values; never print it.
        print('Stopped:', type(exc).__name__, '(details suppressed to protect credentials)')
        raise SystemExit(1) from None
