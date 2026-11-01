"""Offline tests for scripts/xiaohongshu_login_local.py. No browser, no network, no real login data."""
import importlib.util, json, os, stat, sys, tempfile, types, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('xiaohongshu_login_local', ROOT / 'scripts' / 'xiaohongshu_login_local.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

UID = 'u' + 'a' * 11
PNG1 = helper.PNG_MAGIC + b'qr-one'
PNG2 = helper.PNG_MAGIC + b'qr-two'
SECRET = 'cookie-secret-value-' + 'z' * 12
NICK = '测试昵称'


def cookie(domain, name='web_session', value=SECRET, **extra):
    return {'name': name, 'value': value, 'domain': domain, 'path': '/', 'expires': 1900000000, 'httpOnly': True, 'secure': True,
            'sameSite': 'Lax', **extra}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeDriver:
    def __init__(self, infos, qrs=(PNG1,), cookies=None):
        self.infos, self.qrs, self._cookies, self.closed = list(infos), list(qrs), cookies, False
        self.qr_calls = self.shots = 0   # shots = calls that actually returned an image (i.e. took a screenshot)

    def user_info(self):
        return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

    def qr_png(self):
        self.qr_calls += 1
        image = self.qrs.pop(0) if len(self.qrs) > 1 else (self.qrs[0] if self.qrs else None)
        self.shots += image is not None
        return image

    def cookies(self):
        return self._cookies if self._cookies is not None else [cookie('.xiaohongshu.com'), cookie('www.xiaohongshu.com', 'a1', 'x' + SECRET),
                                                              cookie('.google.com', 'NID', 'other-site'), cookie('xiaohongshu.com.evil.example', 'bad', 'evil')]

    def close(self):
        self.closed = True


def guest():
    return {'guest': True, 'userId': '', 'nickname': ''}


def logged_in(user_id=UID):
    return {'guest': False, 'userId': user_id, 'nickname': NICK}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.paths = {'profile': self.base / 'profile', 'qr': self.base / 'qr.png', 'cookies': self.base / 'cookies.json',
                      'identity': self.base / 'identity.json'}
        self.clock, self.lines = Clock(), []

    def argv(self, no_qr=False, **over):
        extra = over.pop('extra', [])
        p = {**self.paths, **{k: str(v) for k, v in over.items()}}
        qr = [] if no_qr else ['--qr-out', str(p['qr'])]
        return ['--profile-dir', str(p['profile']), *qr, '--cookies-out', str(p['cookies']),
                '--identity-out', str(p['identity']), '--poll-interval', '2', *extra]

    def run_main(self, driver, argv=None, **kw):
        factory = lambda profile, channel, exe, **kw: driver
        return helper.main(argv or self.argv(), driver_factory=factory, now=self.clock.now, sleep=self.clock.sleep,
                           out=self.lines.append, **kw)

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def leftovers(self):
        return [p.name for p in self.base.iterdir() if '.tmp-' in p.name]


class FlowTests(Base):
    def test_scan_then_export_with_private_files_and_clean_stdout(self):
        driver = FakeDriver([guest(), guest(), logged_in()], qrs=[PNG1, PNG1, PNG2])
        code = self.run_main(driver)
        self.assertEqual(code, 0)
        self.assertTrue(driver.closed)
        self.assertEqual(self.paths['qr'].read_bytes(), PNG1)         # the first QR was saved...
        cookies = json.loads(self.paths['cookies'].read_text())
        self.assertEqual({c['domain'] for c in cookies}, {'.xiaohongshu.com', 'www.xiaohongshu.com'})
        self.assertEqual({c['name'] for c in cookies}, {'web_session', 'a1'})
        identity = json.loads(self.paths['identity'].read_text())
        self.assertEqual((identity['user_id'], identity['nickname'], identity['cookie_count']), (UID, NICK, 2))
        for path in (self.paths['qr'], self.paths['cookies'], self.paths['identity']):
            self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.leftovers(), [])
        text = '\n'.join(self.lines)
        for hidden in (SECRET, 'other-site', UID, NICK, 'evil'):
            self.assertNotIn(hidden, text)
        self.assertNotIn(SECRET, self.paths['identity'].read_text())
        self.assertEqual({json.loads(l)['status'] for l in self.lines}, {'waiting_for_scan', 'qr_saved', 'logged_in'})

    def test_at_most_one_screenshot_even_if_the_qr_refreshes_or_polling_goes_on(self):
        driver = FakeDriver([guest()] * 6 + [logged_in()], qrs=[PNG1, PNG2, PNG2])
        self.run_main(driver)
        self.assertEqual(self.paths['qr'].read_bytes(), PNG1)         # never replaced by the refreshed QR
        self.assertEqual((driver.qr_calls, driver.shots), (1, 1))     # not asked again on later polls
        self.assertEqual([json.loads(l)['status'] for l in self.lines].count('qr_saved'), 1)

    def test_one_attempt_even_if_the_qr_is_not_there_yet_or_the_shot_fails(self):
        # the driver returns None both for "not visible yet" and for a swallowed screenshot error
        driver = FakeDriver([guest()] * 5 + [logged_in()], qrs=[None, None, PNG1])
        self.assertEqual(self.run_main(driver), 0)          # login by scanning in the window does not need the screenshot
        self.assertEqual(driver.qr_calls, 1)                # attempted once, marked before the call, never retried
        self.assertEqual(driver.shots, 0)
        self.assertFalse(self.paths['qr'].exists())
        self.assertNotIn('qr_saved', '\n'.join(self.lines))

    def test_no_qr_out_means_no_screenshot_at_all(self):
        driver = FakeDriver([guest()] * 3 + [logged_in()])
        code = self.run_main(driver, self.argv(no_qr=True))
        self.assertEqual(code, 0)
        self.assertEqual((driver.qr_calls, driver.shots), (0, 0))
        self.assertFalse(self.paths['qr'].exists())
        self.assertNotIn('qr_saved', '\n'.join(self.lines))
        self.assertTrue(self.paths['cookies'].exists() and self.paths['identity'].exists())   # login itself is unaffected

    def test_non_png_qr_data_is_ignored_and_not_retried(self):
        driver = FakeDriver([guest(), guest(), logged_in()], qrs=[b'<html>not a png'])
        self.run_main(driver)
        self.assertFalse(self.paths['qr'].exists())
        self.assertEqual(driver.qr_calls, 1)

    def test_times_out_after_ten_minutes_without_exporting(self):
        driver = FakeDriver([guest()])
        code = self.run_main(driver)
        self.assertEqual(code, 3)
        self.assertGreaterEqual(self.clock.t, 1000 + 600)
        self.assertLess(self.clock.t, 1000 + 600 + 5)
        self.assertFalse(self.paths['cookies'].exists() or self.paths['identity'].exists())
        self.assertTrue(driver.closed)
        self.assertIn('timed_out', self.lines[-1])

    def test_identity_rejections_export_nothing(self):
        for bad in ('', 'a b', 'short', 'x' * 65, 'id/../x'):
            self.setUp()
            code = self.run_main(FakeDriver([logged_in(bad)]))
            self.assertEqual(code, 4, bad)
            self.assertFalse(self.paths['cookies'].exists() or self.paths['identity'].exists())
        self.setUp()
        # guest missing / not a boolean is "not ready", never a login
        self.assertEqual(self.run_main(FakeDriver([{'guest': 'false', 'userId': UID}, None, {}])), 3)
        self.assertFalse(self.paths['cookies'].exists())

    def test_login_without_any_xiaohongshu_cookie_is_rejected(self):
        driver = FakeDriver([logged_in()], cookies=[cookie('.google.com'), cookie('xiaohongshu.com.evil.example')])
        self.assertEqual(self.run_main(driver), 4)
        self.assertFalse(self.paths['cookies'].exists() or self.paths['identity'].exists())

    def test_identity_failure_never_leaves_cookies_behind(self):
        outputs = {'cookies_out': self.paths['cookies'], 'identity_out': self.paths['identity']}
        real = helper.write_private_atomic

        def flaky(path, data):
            if Path(path) == self.paths['identity']:
                raise OSError('disk full')
            return real(path, data)
        with mock.patch.object(helper, 'write_private_atomic', flaky), self.assertRaises(OSError):
            helper.export_login(outputs, {'user_id': UID, 'nickname': NICK}, [cookie('.xiaohongshu.com')], self.clock.now)
        self.assertFalse(self.paths['cookies'].exists())

    def test_browser_unavailable_exit_code(self):
        def missing(profile, channel, exe, **kw):
            raise ImportError('no playwright')

        def broken(profile, channel, exe, **kw):
            raise RuntimeError('no chrome at /secret/place')
        self.assertEqual(helper.main(self.argv(), driver_factory=missing, out=self.lines.append), 5)
        self.assertIn('playwright_not_installed', self.lines[-1])
        self.assertEqual(helper.main(self.argv(), driver_factory=broken, out=self.lines.append), 5)
        failure = json.loads(self.lines[-1])
        self.assertEqual((failure['reason'], failure['stage'], failure['exception'], failure['code']),
                         ('browser_launch_failed', 'launch', 'RuntimeError', 'unclassified'))
        self.assertNotIn('/secret/place', self.lines[-1])


class PureHelperTests(unittest.TestCase):
    def test_user_info_shapes(self):
        plain = {'guest': False, 'userId': UID, 'nickname': NICK}
        for raw in (plain, {'value': plain}, {'_value': plain}, {'value': None, '_value': plain}):
            info = helper.normalize_user_info(raw)
            self.assertEqual((info['guest'], info['user_id'], info['nickname']), (False, UID, NICK), raw)
        self.assertIsNone(helper.normalize_user_info(None))
        self.assertIsNone(helper.normalize_user_info('x'))
        self.assertIsNone(helper.normalize_user_info({'guest': 'false'})['guest'])
        self.assertEqual(helper.normalize_user_info({'guest': True, 'nickname': 'a\x00b\nc'})['nickname'], 'abc')

    def test_judge(self):
        self.assertEqual(helper.judge(None), ('wait', None))
        self.assertEqual(helper.judge(helper.normalize_user_info({'guest': True, 'userId': UID})), ('wait', None))
        self.assertEqual(helper.judge(helper.normalize_user_info({'guest': False, 'userId': UID, 'nickname': 'n'}))[0], 'ok')
        with self.assertRaises(helper.LoginError):
            helper.judge(helper.normalize_user_info({'guest': False, 'userId': ''}))

    def test_cookie_domain_filter_and_field_whitelist(self):
        cookies = [cookie('.xiaohongshu.com', 'a'), cookie('xiaohongshu.com', 'b'), cookie('edith.xiaohongshu.com', 'c'),
                   cookie('.XiaoHongShu.com', 'd'), cookie('notxiaohongshu.com', 'e'), cookie('xiaohongshu.com.evil.example', 'f'),
                   cookie('.google.com', 'g'), cookie('', 'h'), {'name': 'x'}, 'junk']
        cookies[0]['partitionKey'] = 'secret-partition'
        kept = helper.filter_cookies(cookies)
        self.assertEqual([c['name'] for c in kept], ['a', 'b', 'c', 'd'])
        self.assertNotIn('partitionKey', kept[0])
        self.assertEqual(set(kept[0]), {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'session'})
        odd = helper.filter_cookies([cookie('.xiaohongshu.com', sameSite='weird', expires=-1)])[0]
        self.assertEqual((odd['sameSite'], odd['session']), ('Lax', True))

    def test_atomic_private_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            target = base / 'out.json'
            target.write_text('old')
            target.chmod(0o644)
            helper.write_private_atomic(target, b'new')
            self.assertEqual((target.read_bytes(), stat.S_IMODE(os.lstat(target).st_mode)), (b'new', 0o600))
            self.assertEqual([p.name for p in base.iterdir()], ['out.json'])
            outside = base / 'victim'
            outside.write_text('keep')
            link = base / 'link.json'
            link.symlink_to(outside)
            helper.write_private_atomic(link, b'x')  # replaces the link itself; the link target is untouched
            self.assertEqual(outside.read_text(), 'keep')
            with self.assertRaises(FileNotFoundError):
                helper.write_private_atomic(base / 'missing-dir' / 'f', b'x')
            self.assertEqual(sorted(p.name for p in base.iterdir()), ['link.json', 'out.json', 'victim'])


class ArgumentTests(Base):
    def code(self, argv=None):
        return helper.main(argv or self.argv(), driver_factory=lambda *a, **kw: FakeDriver([logged_in()]), now=self.clock.now,
                           sleep=self.clock.sleep, out=self.lines.append)

    def test_required_arguments_and_timeout_bounds(self):
        with mock.patch.object(sys, 'stderr', __import__('io').StringIO()), self.assertRaises(SystemExit) as ctx:
            helper.main(['--profile-dir', str(self.paths['profile'])])
        self.assertEqual(ctx.exception.code, 2)
        for timeout in ('10', '601', '-1'):
            self.assertEqual(self.code(self.argv(extra=['--timeout', timeout])), 2, timeout)
        self.assertEqual(self.code(self.argv(extra=['--timeout', '600'])), 0)

    def test_help_documents_the_four_required_options(self):
        self.assertIn('Exit codes', helper.__doc__)
        buf = __import__('io').StringIO()
        with mock.patch.object(sys, 'stdout', buf), self.assertRaises(SystemExit) as ctx:
            helper.parse_args(['--help'])
        self.assertEqual(ctx.exception.code, 0)
        for option in ('--profile-dir', '--qr-out', '--cookies-out', '--identity-out', '--channel'):
            self.assertIn(option, buf.getvalue())

    def test_profile_is_dedicated_marked_and_private(self):
        self.assertEqual(self.code(), 0)
        profile = self.paths['profile']
        self.assertEqual(self.mode(profile), 0o700)
        self.assertTrue((profile / helper.MARKER).exists())
        self.assertEqual(self.code(), 0)  # a profile this script marked may be reused

    def test_existing_or_personal_profiles_are_refused(self):
        existing = self.base / 'someone-elses'
        (existing / 'Default').mkdir(parents=True)
        (existing / 'Cookies').write_text('personal')
        self.assertEqual(self.code(self.argv(profile=existing)), 2)
        self.assertEqual(sorted(p.name for p in existing.iterdir()), ['Cookies', 'Default'])   # untouched, no marker added
        personal = self.base / 'Library' / 'Application Support' / 'Google' / 'Chrome'
        personal.mkdir(parents=True)
        self.assertEqual(self.code(self.argv(profile=personal)), 2)
        self.assertEqual(list(personal.iterdir()), [])
        linked = self.base / 'linked'
        (self.base / 'real').mkdir()
        linked.symlink_to(self.base / 'real')
        self.assertEqual(self.code(self.argv(profile=linked)), 2)
        self.assertEqual(self.code(self.argv(profile='relative/profile')), 2)
        self.assertEqual(self.code(self.argv(profile=self.base / 'no-parent' / 'profile')), 2)

    def test_output_paths_are_validated(self):
        inside = self.paths['profile'] / 'cookies.json'
        self.assertEqual(self.code(self.argv(cookies=inside)), 2)
        self.assertEqual(self.code(self.argv(qr=self.paths['cookies'])), 2)           # not distinct
        self.assertEqual(self.code(self.argv(cookies='relative.json')), 2)
        self.assertEqual(self.code(self.argv(identity=self.base / 'missing' / 'id.json')), 2)
        victim = self.base / 'victim.json'
        victim.write_text('keep')
        link = self.base / 'link.json'
        link.symlink_to(victim)
        self.assertEqual(self.code(self.argv(cookies=link)), 2)
        self.assertEqual(victim.read_text(), 'keep')
        directory = self.base / 'adir'
        directory.mkdir()
        self.assertEqual(self.code(self.argv(identity=directory)), 2)
        self.assertFalse(self.paths['cookies'].exists())


class BrowserLaunchTests(Base):
    """Drive the real PlaywrightDriver against a recording fake of the playwright module."""
    def fake_playwright(self, record):
        class Page:
            def goto(self, url, **kw):
                record['goto'] = (url, kw)

        class Chromium:
            def launch_persistent_context(self, **options):
                record['options'] = options
                return types.SimpleNamespace(pages=[], new_page=lambda: Page(), cookies=lambda: [], close=lambda: None)

        class Started:
            chromium = Chromium()

            def stop(self):
                pass
        sync_api = types.ModuleType('playwright.sync_api')
        sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: Started())
        return {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': sync_api}

    def test_visible_chrome_on_the_dedicated_profile_with_default_options_only(self):
        record = {}
        with mock.patch.dict(sys.modules, self.fake_playwright(record)):
            helper.PlaywrightDriver(self.paths['profile'], 'chrome', '')
            self.assertEqual(record['options'], {'user_data_dir': str(self.paths['profile']), 'headless': False, 'channel': 'chrome',
                                                 'timeout': 60000})
            self.assertEqual(record['goto'][0], 'https://www.xiaohongshu.com/explore')
            self.assertEqual(record['goto'][1]['timeout'], 60000)
            helper.PlaywrightDriver(self.paths['profile'], 'chrome', '/opt/chrome/chrome', startup_timeout=25)
            self.assertEqual(record['options'], {'user_data_dir': str(self.paths['profile']), 'headless': False,
                                                 'executable_path': '/opt/chrome/chrome', 'timeout': 25000})
            self.assertEqual(record['goto'][1]['timeout'], 25000)

    def test_script_has_no_disguise_or_automation_hiding_and_no_publish_logic(self):
        source = (ROOT / 'scripts' / 'xiaohongshu_login_local.py').read_text(encoding='utf-8').lower()
        for forbidden in ('user_agent', 'stealth', 'webdriver', 'add_init_script', 'ignore_default_args', 'args=[', 'fingerprint',
                          'proxy', 'publish', 'ssh', 'subprocess', 'urllib', 'requests'):
            self.assertNotIn(forbidden, source.replace('never publishes', '').replace('no automation-hiding or fingerprint options', ''), forbidden)
        self.assertIn('u.value', helper.USER_INFO_JS)
        self.assertIn('u._value', helper.USER_INFO_JS)

    def test_real_driver_screenshots_the_qr_element_only_once(self):
        shots = []

        class Element:
            visible = True

            def is_visible(self):
                return self.visible

            def screenshot(self, timeout):
                shots.append(timeout)
                return PNG1

        element = Element()
        driver = helper.PlaywrightDriver.__new__(helper.PlaywrightDriver)
        driver.page = types.SimpleNamespace(locator=lambda selector: types.SimpleNamespace(first=element))
        element.visible = False
        self.assertIsNone(driver.qr_png())          # not visible: a check only, no screenshot
        self.assertEqual(shots, [])
        element.visible = True
        self.assertEqual(driver.qr_png(), PNG1)
        for _ in range(5):
            self.assertIsNone(driver.qr_png())      # never again, however long the wait
        self.assertEqual(len(shots), 1)
        failing = helper.PlaywrightDriver.__new__(helper.PlaywrightDriver)

        class Broken(Element):
            def screenshot(self, timeout):
                shots.append('failed')
                raise RuntimeError('screenshot failed')
        failing.page = types.SimpleNamespace(locator=lambda selector: types.SimpleNamespace(first=Broken()))
        self.assertIsNone(failing.qr_png())
        self.assertIsNone(failing.qr_png())
        self.assertEqual(shots.count('failed'), 1)  # a failed attempt is not repeated either

    def test_driver_reads_page_state_and_swallows_navigation_errors(self):
        driver = helper.PlaywrightDriver.__new__(helper.PlaywrightDriver)

        class Page:
            def evaluate(self, js):
                raise RuntimeError('Execution context was destroyed')

            def locator(self, selector):
                raise RuntimeError('gone')
        driver.page = Page()
        self.assertIsNone(driver.user_info())
        self.assertIsNone(driver.qr_png())


TimeoutErr = type('TimeoutError', (Exception,), {})   # same class name Playwright uses


class StartupStageTests(Base):
    """Launch and navigation are separate stages with whitelisted diagnostics and a bounded retry."""
    SECRET_PATH = '/secret/home/Chrome/chrome-binary'
    SECRET_URL = 'https://secret.example/private?token=abc'

    def fake(self, launch_errors=(), goto_errors=(), infos=(), new_page_error=None, cookies=None):
        record = {'launches': 0, 'gotos': 0, 'ctx_closed': 0, 'pw_stopped': 0, 'options': []}
        launch_errors, goto_errors, infos = list(launch_errors), list(goto_errors), list(infos)

        class Page:
            def goto(self, url, **kw):
                record['gotos'] += 1
                if goto_errors:
                    error = goto_errors.pop(0)
                    if error:
                        raise error

            def evaluate(self, js):
                return infos.pop(0) if len(infos) > 1 else (infos[0] if infos else None)

            def locator(self, selector):
                raise RuntimeError('no qr')

        class Context:
            pages = []

            def new_page(self):
                if new_page_error:
                    raise new_page_error
                return Page()

            def cookies(self):
                return cookies if cookies is not None else [cookie('.xiaohongshu.com')]

            def close(self):
                record['ctx_closed'] += 1

        class Chromium:
            def launch_persistent_context(self, **options):
                record['launches'] += 1
                record['options'].append(options)
                if launch_errors:
                    error = launch_errors.pop(0)
                    if error:
                        raise error
                return Context()

        class Started:
            chromium = Chromium()

            def stop(self):
                record['pw_stopped'] += 1
        sync_api = types.ModuleType('playwright.sync_api')
        sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: Started())
        modules = {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': sync_api}
        return record, modules

    def run_real(self, modules, extra=()):
        argv = self.argv(extra=list(extra))
        with mock.patch.dict(sys.modules, modules), mock.patch.object(helper.time, 'sleep', lambda s: None):
            return helper.main(argv, now=self.clock.now, sleep=self.clock.sleep, out=self.lines.append)

    def events(self):
        return [json.loads(l) for l in self.lines]

    def test_launch_failure_reports_stage_and_code_closes_everything_and_retries_once(self):
        err = lambda: RuntimeError("browserType.launchPersistentContext: Executable doesn't exist at %s" % self.SECRET_PATH)
        record, modules = self.fake(launch_errors=[err(), err()])
        self.assertEqual(self.run_real(modules), 5)
        self.assertEqual(record['launches'], 2)                      # one bounded retry, no more
        self.assertEqual(record['pw_stopped'], 1)                    # the Playwright instance this task started is stopped
        failure = self.events()[-1]
        self.assertEqual((failure['reason'], failure['stage'], failure['exception'], failure['code']),
                         ('browser_launch_failed', 'launch', 'RuntimeError', 'executable_missing'))
        self.assertEqual(sorted(failure), ['code', 'exception', 'reason', 'stage', 'status'])
        self.assertNotIn(self.SECRET_PATH, '\n'.join(self.lines))
        self.assertTrue(any(e['status'] == 'launch_retry' for e in self.events()))
        self.assertFalse(self.paths['cookies'].exists())

    def test_first_launch_failure_then_success_continues(self):
        record, modules = self.fake(launch_errors=[RuntimeError('net::ERR_CONNECTION_REFUSED while launching')], infos=[logged_in()])
        self.assertEqual(self.run_real(modules), 0)
        self.assertEqual(record['launches'], 2)
        retry = [e for e in self.events() if e['status'] == 'launch_retry'][0]
        self.assertEqual((retry['stage'], retry['code']), ('launch', 'net::ERR_CONNECTION_REFUSED'))
        self.assertEqual(record['ctx_closed'], 1)

    def test_page_creation_failure_closes_context_and_playwright(self):
        record, modules = self.fake(new_page_error=RuntimeError('cannot create page'))
        self.assertEqual(self.run_real(modules), 5)
        self.assertEqual((record['ctx_closed'], record['pw_stopped']), (1, 1))
        self.assertEqual(self.events()[-1]['stage'], 'launch')

    def test_navigation_timeout_keeps_the_window_and_waits_without_relaunching(self):
        timeout = TimeoutErr('Timeout 60000ms exceeded.\n  navigating to "%s"' % self.SECRET_URL)
        record, modules = self.fake(goto_errors=[timeout], infos=[guest(), guest(), logged_in()])
        self.assertEqual(self.run_real(modules), 0)                  # the person logged in by hand: normal flow continued
        self.assertEqual(record['launches'], 1)                      # Chrome was not started again
        self.assertEqual(record['gotos'], 1)                         # a timeout is not retried: the page may still be loading
        slow = [e for e in self.events() if e['status'] == 'navigation_timeout'][0]
        self.assertEqual((slow['stage'], slow['exception'], slow['code']), ('navigate', 'TimeoutError', 'TimeoutError'))
        self.assertNotIn(self.SECRET_URL, '\n'.join(self.lines))
        self.assertEqual((record['ctx_closed'], record['pw_stopped']), (1, 1))   # closed once, at the very end

    def test_navigation_timeout_without_login_still_times_out_normally(self):
        record, modules = self.fake(goto_errors=[TimeoutErr('Timeout')], infos=[guest()])
        self.assertEqual(self.run_real(modules), 3)
        self.assertEqual(record['launches'], 1)

    def test_network_error_during_navigation_is_retried_once_then_reported(self):
        net = lambda: RuntimeError('page.goto: net::ERR_NAME_NOT_RESOLVED at %s' % self.SECRET_URL)
        record, modules = self.fake(goto_errors=[net(), net()], infos=[logged_in()])
        self.assertEqual(self.run_real(modules), 0)
        self.assertEqual(record['gotos'], 2)
        failed = [e for e in self.events() if e['status'] == 'navigation_failed'][0]
        self.assertEqual((failed['stage'], failed['code']), ('navigate', 'net::ERR_NAME_NOT_RESOLVED'))
        self.assertNotIn(self.SECRET_URL, '\n'.join(self.lines))
        record, modules = self.fake(goto_errors=[net(), None], infos=[logged_in()])
        self.setUp()
        self.assertEqual(self.run_real(modules), 0)
        self.assertEqual(record['gotos'], 2)
        self.assertFalse(any(e['status'] == 'navigation_failed' for e in self.events()))

    def test_browser_closed_during_navigation_is_a_startup_failure(self):
        record, modules = self.fake(goto_errors=[RuntimeError('Target page, context or browser has been closed')])
        self.assertEqual(self.run_real(modules), 5)
        failure = self.events()[-1]
        self.assertEqual((failure['reason'], failure['stage'], failure['code']), ('browser_closed_during_navigation', 'navigate', 'target_closed'))
        self.assertEqual((record['ctx_closed'], record['pw_stopped']), (1, 1))

    def test_startup_timeout_is_bounded_and_applied_to_both_stages(self):
        record, modules = self.fake(infos=[logged_in()])
        self.assertEqual(self.run_real(modules, ['--startup-timeout', '20']), 0)
        self.assertEqual(record['options'][0]['timeout'], 20000)
        for bad in ('9', '121', '0'):
            self.setUp()
            self.assertEqual(self.run_real(self.fake()[1], ['--startup-timeout', bad]), 2, bad)

    def test_classify_emits_only_whitelisted_identifiers(self):
        cases = [(RuntimeError('x net::ERR_TIMED_OUT y'), ('RuntimeError', 'net::ERR_TIMED_OUT')),
                 (TimeoutErr('Timeout 5ms'), ('TimeoutError', 'TimeoutError')),
                 (RuntimeError('user data directory is already in use at /p'), ('RuntimeError', 'profile_in_use')),
                 (RuntimeError('Operation not permitted /x'), ('RuntimeError', 'permission_denied')),
                 (ValueError('something with /secret and https://' + 'u:p' + '@h'), ('ValueError', 'unclassified'))]
        for exc, expected in cases:
            self.assertEqual(helper.classify(exc), expected)
        weird = type('Bad Name!', (Exception,), {})
        self.assertEqual(helper.classify(weird('x'))[0], 'Exception')
        for exc, _ in cases:
            self.assertNotIn('secret', json.dumps(helper.classify(exc)))


if __name__ == '__main__':
    unittest.main()
