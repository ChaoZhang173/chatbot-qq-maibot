"""Scan publication files. Report paths/rule names, never matched secret values."""
import argparse
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_FIELDS = re.compile(r'(api_key|app_secret|password|access_token|client_secret)$', re.I)
RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'api-token': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'credential-url': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'personal-home': re.compile(r'/' + r'Users/[^/\s]+/'),
    'qq-openid': re.compile(r'\b[A-F0-9]{32}\b'),
}
FORBIDDEN_NAMES = {'webui.json', 'bot_config.toml', 'model_config.toml', 'adapter_policy.toml', '.env'}
FORBIDDEN_DIRS = {'data', 'logs', 'backups', 'screenshots', 'secrets', 'runtime', 'plugins'}


def scan_file(path, relative, text=None):
    failures = []
    if path.name in FORBIDDEN_NAMES or FORBIDDEN_DIRS.intersection(relative.parts):
        failures.append('live-file-or-data')
    if path.suffix.lower() in {'.key', '.pem', '.db', '.sqlite', '.log', '.png', '.jpg', '.zip', '.whl'}:
        failures.append('private-or-binary-artifact')
    if text is None:
        try:
            text = path.read_text()
        except (UnicodeError, OSError):
            return failures + ['non-text-file']
    for rule, pattern in RULES.items():
        if pattern.search(text):
            failures.append(rule)
    for address in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', text):
        if address not in {'127.0.0.1'}:
            failures.append('non-loopback-ipv4')
    if path.suffix == '.toml':
        try:
            def inspect(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if SECRET_FIELDS.search(key) and item not in ('', None):
                            failures.append('nonempty-credential-field')
                        if key in {'app_id', 'account_id'} and item and not str(item).startswith('REPLACE_WITH_'):
                            failures.append('real-account-identifier')
                        inspect(item)
                elif isinstance(value, list):
                    for item in value:
                        inspect(item)
            inspect(tomllib.loads(text))
        except tomllib.TOMLDecodeError:
            failures.append('invalid-toml')
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
