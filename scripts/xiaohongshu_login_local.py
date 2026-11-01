"""Local helper: let the account owner scan the login QR in their own Chrome, then export that login.

What it does, and only this:
  1. starts the user's installed Chrome (Playwright, headless=False) on a SEPARATE persistent profile that this
     script creates and marks; it refuses to use an existing/personal Chrome profile;
  2. takes NO screenshots by default. Only if --qr-out is given, it saves the QR image shown on the page
     (.login-container .qrcode-img) to that path (mode 0600) ONCE per run: never every poll, never on QR refresh;
  3. waits (default and maximum 10 minutes) until the page's own state says user.userInfo.guest === false with a
     valid userId. The person scans and solves any verification themselves; nothing is automated or disguised;
  4. exports only xiaohongshu.com cookies of that one browser context to --cookies-out (atomic, 0600) and writes
     the user id / nickname to --identity-out (atomic, 0600). Cookies and tokens are never printed or put in
     the identity file.
It never publishes, comments or connects to a server. No automation-hiding or fingerprint options are used.
Exit codes: 0 ok, 2 bad arguments, 3 timed out, 4 identity rejected, 5 browser/Playwright unavailable.
"""
import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

MAX_TIMEOUT = 600
MARKER = '.xhs-login-profile'
MARKER_TEXT = 'dafeiyu xiaohongshu login profile; created by xiaohongshu_login_local.py\n'
PERSONAL_PROFILE_HINTS = ('library/application support/google/chrome', '.config/google-chrome', '.config/chromium',
                          'appdata/local/google/chrome', 'chrome/user data')
USER_ID = re.compile(r'[A-Za-z0-9]{6,64}')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
START_URL = 'https://www.xiaohongshu.com/explore'
QR_SELECTOR = '.login-container .qrcode-img'
# Same selection the page code uses: the userInfo ref may expose .value or ._value, or be a plain object.
USER_INFO_JS = '''() => {
  const s = window.__INITIAL_STATE__;
  const u = s && s.user && s.user.userInfo;
  if (!u) return null;
  const v = (u.value !== undefined) ? u.value : ((u._value !== undefined) ? u._value : u);
  if (!v || typeof v !== 'object') return null;
  return {guest: v.guest, userId: v.userId, nickname: v.nickname};
}'''


class LoginError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


# ---- pure helpers (offline-testable) -------------------------------------------------------------------
def normalize_user_info(raw):
    """-> {'guest': True|False|None, 'user_id': str, 'nickname': str} or None when there is nothing usable."""
    if not isinstance(raw, dict):
        return None
    for key in ('value', '_value'):          # unwrap a Vue-style ref if the whole wrapper was passed
        if isinstance(raw.get(key), dict):
            raw = raw[key]
            break
    guest = raw.get('guest')
    user_id = raw.get('userId', raw.get('user_id'))
    nickname = raw.get('nickname')
    return {'guest': guest if isinstance(guest, bool) else None,
            'user_id': user_id if isinstance(user_id, str) else '',
            'nickname': re.sub(r'[\x00-\x1f\x7f]', '', nickname)[:64] if isinstance(nickname, str) else ''}


def judge(info):
    """('wait', None) | ('ok', identity) | raises LoginError(4). Only guest === false with a valid id is a login."""
    if info is None or info['guest'] is not False:
        return 'wait', None
    if not USER_ID.fullmatch(info['user_id']):
        raise LoginError(4, 'invalid_user_id')
    return 'ok', {'user_id': info['user_id'], 'nickname': info['nickname']}


def domain_ok(domain):
    domain = str(domain or '').lstrip('.').lower()
    return domain == 'xiaohongshu.com' or domain.endswith('.xiaohongshu.com')


def filter_cookies(cookies):
    """Keep only xiaohongshu.com cookies, with the fields the cookie file needs and nothing else."""
    out = []
    for c in cookies or []:
        if not isinstance(c, dict) or not domain_ok(c.get('domain')) or not isinstance(c.get('name'), str) or not isinstance(c.get('value'), str):
            continue
        expires = c.get('expires', -1)
        out.append({'name': c['name'], 'value': c['value'], 'domain': c['domain'], 'path': c.get('path') or '/',
                    'expires': expires if isinstance(expires, (int, float)) else -1,
                    'httpOnly': bool(c.get('httpOnly')), 'secure': bool(c.get('secure')),
                    'sameSite': c.get('sameSite') if c.get('sameSite') in ('Strict', 'Lax', 'None') else 'Lax',
                    'session': expires in (-1, None)})
    return out


def write_private_atomic(path, data):
    """Create a 0600 file next to the target, fsync, then rename over it. Never follows a symlink."""
    path = Path(path)
    tmp = path.with_name('.%s.tmp-%s' % (path.name, os.urandom(6).hex()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prepare_profile(profile_dir):
    """A dedicated, marked profile only: never adopt an existing or personal Chrome profile."""
    path = Path(profile_dir)
    if not path.is_absolute():
        raise LoginError(2, 'profile_dir_must_be_absolute')
    lowered = str(path).replace('\\', '/').lower()
    if any(hint in lowered for hint in PERSONAL_PROFILE_HINTS):
        raise LoginError(2, 'profile_dir_looks_like_a_personal_chrome_profile')
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise LoginError(2, 'profile_dir_must_be_a_real_directory')
        entries = [p.name for p in path.iterdir()]
        if entries and MARKER not in entries:
            raise LoginError(2, 'profile_dir_exists_and_is_not_a_dedicated_login_profile')
        if (path / 'Default').exists() and MARKER not in entries:
            raise LoginError(2, 'profile_dir_looks_like_a_chrome_user_data_dir')
    else:
        if not path.parent.is_dir():
            raise LoginError(2, 'profile_parent_missing')
        path.mkdir(mode=0o700)
    marker = path / MARKER
    if not marker.exists():
        write_private_atomic(marker, MARKER_TEXT.encode())
    return path


def check_output_path(raw, profile, label):
    path = Path(raw)
    if not path.is_absolute():
        raise LoginError(2, label + '_must_be_absolute')
    parent = path.parent.resolve()
    if not parent.is_dir():
        raise LoginError(2, label + '_parent_missing')
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(os.lstat(path).st_mode)):
        raise LoginError(2, label + '_must_be_a_regular_file_path')
    resolved = parent / path.name
    profile_resolved = Path(profile).resolve()
    if resolved == profile_resolved or profile_resolved in resolved.parents:
        raise LoginError(2, label + '_must_be_outside_the_profile')
    return resolved


def parse_args(argv):
    p = argparse.ArgumentParser(description='Scan-to-login helper for the account owner (own Chrome, dedicated profile).')
    p.add_argument('--profile-dir', required=True, help='absolute path of a NEW dedicated profile directory (never a personal profile)')
    p.add_argument('--qr-out', default='', help='OPTIONAL absolute path for one QR screenshot (PNG, mode 0600); without it no screenshot is ever taken')
    p.add_argument('--cookies-out', required=True, help='absolute path for the exported xiaohongshu.com cookies (JSON, mode 0600)')
    p.add_argument('--identity-out', required=True, help='absolute path for the user id / nickname metadata (JSON, mode 0600)')
    p.add_argument('--channel', default='chrome', help='Playwright browser channel (default: chrome)')
    p.add_argument('--executable-path', default='', help='explicit Chrome executable instead of --channel')
    p.add_argument('--timeout', type=int, default=MAX_TIMEOUT, help='seconds to wait for the scan (30..600, default 600)')
    p.add_argument('--startup-timeout', type=int, default=60, help='seconds allowed for the browser launch and for the first page load (10..120, default 60)')
    p.add_argument('--poll-interval', type=float, default=2.0, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def validate(args):
    if not 30 <= args.timeout <= MAX_TIMEOUT:
        raise LoginError(2, 'timeout_must_be_between_30_and_600_seconds')
    if not 10 <= args.startup_timeout <= 120:
        raise LoginError(2, 'startup_timeout_must_be_between_10_and_120_seconds')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', args.channel):
        raise LoginError(2, 'invalid_channel')
    if args.executable_path and not os.path.isabs(args.executable_path):
        raise LoginError(2, 'executable_path_must_be_absolute')
    profile = prepare_profile(args.profile_dir)
    outputs = {label: check_output_path(getattr(args, label), profile, label)
               for label in ('qr_out', 'cookies_out', 'identity_out') if getattr(args, label)}
    if len(set(outputs.values())) != len(outputs):
        raise LoginError(2, 'output_paths_must_be_distinct')
    return profile, outputs


def export_login(outputs, identity, cookies, now):
    selected = filter_cookies(cookies)
    if not selected:
        raise LoginError(4, 'no_xiaohongshu_cookies_in_context')
    write_private_atomic(outputs['cookies_out'], json.dumps(selected, ensure_ascii=False).encode('utf-8'))
    meta = {'user_id': identity['user_id'], 'nickname': identity['nickname'], 'exported_at': int(now()), 'cookie_count': len(selected)}
    try:
        write_private_atomic(outputs['identity_out'], json.dumps(meta, ensure_ascii=False).encode('utf-8'))
    except BaseException:
        try:
            os.unlink(outputs['cookies_out'])  # never leave cookies without their identity record
        except OSError:
            pass
        raise
    return len(selected)


def run(outputs, driver, timeout, poll, now=time.time, sleep=time.sleep, out=print):
    deadline = now() + timeout
    qr_out, qr_taken = outputs.get('qr_out'), False
    out(json.dumps({'status': 'waiting_for_scan'}))
    while now() < deadline:
        verdict, identity = judge(normalize_user_info(driver.user_info()))
        if verdict == 'ok':
            count = export_login(outputs, identity, driver.cookies(), now)
            out(json.dumps({'status': 'logged_in', 'cookie_count': count}))
            return 0
        if qr_out and not qr_taken:
            # At most one screenshot per run, only when --qr-out was given. The driver returns None (and takes no
            # screenshot) while the QR is not visible yet; the first image it does return ends the attempts.
            qr_taken = True  # marked BEFORE the call: one attempt per run, even if the QR is not there yet or the shot fails
            png = driver.qr_png()
            if png is not None and png.startswith(PNG_MAGIC):
                write_private_atomic(qr_out, png)
                out(json.dumps({'status': 'qr_saved'}))
        sleep(poll)
    raise LoginError(3, 'timed_out_waiting_for_scan')


EXCEPTION_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,60}')
NET_ERROR = re.compile(r'net::ERR_[A-Z0-9_]{1,60}')
# Fixed codes derived from the error text. The text itself is never printed (it can hold paths or URLs).
CODE_KEYWORDS = (("executable doesn't exist", 'executable_missing'), ('singletonlock', 'profile_in_use'),
                 ('already in use', 'profile_in_use'), ('profile appears to be in use', 'profile_in_use'),
                 ('has been closed', 'target_closed'), ('target closed', 'target_closed'), ('crashed', 'browser_crashed'),
                 ('permission denied', 'permission_denied'), ('operation not permitted', 'permission_denied'),
                 ('timeout', 'timeout'))


def classify(exc):
    """-> (exception class name, whitelisted code). Safe to print: no message, URL, path or credential."""
    name = type(exc).__name__
    name = name if EXCEPTION_NAME.fullmatch(name) else 'Exception'
    text = str(exc)
    net = NET_ERROR.search(text)
    if net:
        return name, net.group(0)
    if 'Timeout' in name:
        return name, 'TimeoutError'
    lowered = text.lower()
    for keyword, code in CODE_KEYWORDS:
        if keyword in lowered:
            return name, code
    return name, 'unclassified'


class StartupError(Exception):
    """Browser did not come up. Carries only a stage and whitelisted identifiers."""
    def __init__(self, stage, exc):
        super().__init__(stage)
        self.stage = stage
        self.exception, self.code = classify(exc)


class PlaywrightDriver:
    """The only browser-touching code. Default Playwright launch options; nothing hidden or spoofed.

    Two separate stages: launch (bounded timeout, one retry; everything started here is closed on failure) and
    navigation (one retry for a network error). Once the browser is up it is never relaunched: a navigation
    problem is reported and the window stays open so the person can open the site normally.
    """
    def __init__(self, profile, channel, executable_path, startup_timeout=60, report=None, sleep=None):
        from playwright.sync_api import sync_playwright   # ImportError -> playwright_not_installed
        report = report or (lambda event: None)
        sleep = sleep or time.sleep
        millis = int(startup_timeout * 1000)
        self.context = self._pw = None
        options = {'user_data_dir': str(profile), 'headless': False, 'timeout': millis}
        if executable_path:
            options['executable_path'] = executable_path
        else:
            options['channel'] = channel
        try:
            self._pw = sync_playwright().start()
            last = None
            for attempt in (1, 2):
                try:
                    self.context = self._pw.chromium.launch_persistent_context(**options)
                    break
                except Exception as exc:
                    last = exc
                    if attempt == 1:
                        report({'status': 'launch_retry', 'stage': 'launch', 'attempt': 1, **dict(zip(('exception', 'code'), classify(exc)))})
                        sleep(3)
            if self.context is None:
                raise last
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except Exception as exc:
            self.close()  # only what this task started
            raise StartupError('launch', exc) from None
        self._navigate(millis, report, sleep)

    def _navigate(self, millis, report, sleep):
        for attempt in (1, 2):
            try:
                self.page.goto(START_URL, wait_until='domcontentloaded', timeout=millis)
                return
            except Exception as exc:
                name, code = classify(exc)
                if code == 'target_closed' or code == 'browser_crashed':
                    self.close()
                    raise StartupError('navigate', exc) from None
                if code in ('timeout', 'TimeoutError'):
                    # Browser is up: keep the window, let the person open the site themselves, keep waiting.
                    report({'status': 'navigation_timeout', 'stage': 'navigate', 'exception': name, 'code': code})
                    return
                if attempt == 1:
                    sleep(2)
                    continue
                report({'status': 'navigation_failed', 'stage': 'navigate', 'exception': name, 'code': code})

    def user_info(self):
        try:
            return self.page.evaluate(USER_INFO_JS)
        except Exception:  # page is navigating or not ready yet
            return None

    def qr_png(self):
        if getattr(self, '_qr_shot', False):
            return None  # the one allowed screenshot was already attempted
        try:
            qr = self.page.locator(QR_SELECTOR).first
            if not qr.is_visible():  # a visibility check is not a screenshot
                return None
            self._qr_shot = True      # set before shooting: even a failed attempt is not repeated
            return qr.screenshot(timeout=3000)
        except Exception:
            return None

    def cookies(self):
        return self.context.cookies()

    def close(self):
        for owner, name in ((self.context, 'close'), (self._pw, 'stop')):
            if owner is None:
                continue
            try:
                getattr(owner, name)()
            except Exception:
                pass


def main(argv=None, driver_factory=None, now=time.time, sleep=time.sleep, out=print):
    try:
        args = parse_args(argv)
        profile, outputs = validate(args)
        try:
            driver = (driver_factory or PlaywrightDriver)(profile, args.channel, args.executable_path,
                                                          startup_timeout=args.startup_timeout, report=lambda e: out(json.dumps(e)))
        except ImportError:
            raise LoginError(5, 'playwright_not_installed') from None
        except StartupError as exc:
            out(json.dumps({'status': 'error', 'reason': 'browser_launch_failed' if exc.stage == 'launch' else 'browser_closed_during_navigation',
                            'stage': exc.stage, 'exception': exc.exception, 'code': exc.code}))
            return 5
        except Exception as exc:
            name, code = classify(exc)
            out(json.dumps({'status': 'error', 'reason': 'browser_launch_failed', 'stage': 'launch', 'exception': name, 'code': code}))
            return 5
        try:
            return run(outputs, driver, args.timeout, args.poll_interval, now, sleep, out)
        finally:
            driver.close()
    except LoginError as exc:
        out(json.dumps({'status': 'error', 'reason': exc.reason}))
        return exc.code


if __name__ == '__main__':
    sys.exit(main())
