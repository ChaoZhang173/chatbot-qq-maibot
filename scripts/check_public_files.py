"""Scan publication files. Report paths/rule names, never matched secret values."""
import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_FIELDS = re.compile(r'^(api_?key|app_secret|password|passwd|access_token|refresh_token|client_secret|secret_key|private_key|public_key|authorization|cookie|sessdata|bili_jct)$', re.I)
RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    'public-key-or-certificate': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |TRUSTED )?(?:PUBLIC KEY|CERTIFICATE)-----|-{4} BEGIN SSH2 PUBLIC KEY -{4}|\b(?:ssh-rsa|ssh-dss|ssh-ed25519|ecdsa-sha2-\S+)\s+[A-Za-z0-9+/]{40,}={0,3}'),
    'api-token': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'credential-url': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'personal-home': re.compile(r'/' + r'Users/[^/\s]+/'),
    'qq-openid': re.compile(r'\b[A-F0-9]{32}\b'),
}
FORBIDDEN_NAMES = {'webui.json', 'bot_config.toml', 'model_config.toml', 'adapter_policy.toml', '.env'}
FORBIDDEN_DIRS = {'data', 'logs', 'backups', 'screenshots', 'secrets', 'runtime', 'plugins', 'private', '.ssh', 'gallery'}

# Exact, deliberately invalid URL fixtures used to exercise credential rejection.
# This is not a blanket exemption for tests or for credentials in URLs.
SYNTHETIC_URLS = {
    'extensions/tests_phase2/test_api.py': {'https://u:p@www.bilibili.com/opus/123', 'https://u:p@i0.hdslb.com/a'},
    'extensions/tests_phase25/test_commands_web_topics.py': {'https://u:p@example.com'},
}


def scan_file(path, relative, text=None):
    failures = []
    if path.name in FORBIDDEN_NAMES or FORBIDDEN_DIRS.intersection(relative.parts):
        failures.append('live-file-or-data')
    if path.suffix.lower() in {'.key', '.pub', '.pem', '.crt', '.cer', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3', '.log', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.zip', '.gz', '.tar', '.whl'} or path.name.startswith(('._', '.env', 'id_rsa', 'id_ed25519')):
        failures.append('private-or-binary-artifact')
    if text is None:
        try:
            text = path.read_text()
        except (UnicodeError, OSError):
            return failures + ['non-text-file']
    for rule, pattern in RULES.items():
        checked = text
        if rule == 'credential-url':
            fixtures = (set().union(*SYNTHETIC_URLS.values()) if relative.as_posix() == 'scripts/check_public_files.py'
                        else SYNTHETIC_URLS.get(relative.as_posix(), set()))
            for fixture in fixtures:
                checked = checked.replace(fixture, '')
        if pattern.search(checked):
            failures.append(rule)
    for address in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', text):
        synthetic_metadata = (relative.as_posix() in {'extensions/tests_phase25/test_commands_web_topics.py', 'scripts/check_public_files.py'} and address == '169.254.169.254')
        if address not in {'127.0.0.1'} and not synthetic_metadata:
            failures.append('non-loopback-ipv4')
    if path.suffix in {'.toml', '.json'}:
        try:
            def inspect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if SECRET_FIELDS.search(key) and item not in ('', None):
                            failures.append('nonempty-credential-field')
                        if key in {'app_id', 'account_id'} and item and not (str(item).startswith('REPLACE_WITH_') or item == 'REPLACE_PRIVATELY'):
                            failures.append('real-account-identifier')
                        inspect(item)
                elif isinstance(value, list):
                    for item in value:
                        inspect(item)
            inspect(tomllib.loads(text) if path.suffix == '.toml' else json.loads(text))
        except (tomllib.TOMLDecodeError, json.JSONDecodeError):
            failures.append('invalid-configuration')
    return sorted(set(failures))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tracked', action='store_true', help='Scan git-index files only')
    args = parser.parse_args()
    if args.tracked:
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        paths = [ROOT / name for name in names if name]
    else:
        paths = [p for p in ROOT.rglob('*') if p.is_file() and not {'.git', '.venv', '__pycache__'}.intersection(p.relative_to(ROOT).parts)]
    failures = []
    for path in paths:
        rel = path.relative_to(ROOT)
        text = None
        if args.tracked:
            try:
                text = subprocess.check_output(['git', 'show', f':{rel.as_posix()}'], cwd=ROOT).decode()
            except UnicodeError:
                failures.append(f'{rel}: non-text-file')
                continue
        failures.extend(f'{rel}: {rule}' for rule in scan_file(path, rel, text))
    if failures:
        print('\n'.join(failures))
        raise SystemExit(1)
    print(f'PASS: {len(paths)} publication files scanned; no configured secret patterns found.')
    print('This check supplements manual review; it cannot detect every possible secret.')


if __name__ == '__main__':
    main()
