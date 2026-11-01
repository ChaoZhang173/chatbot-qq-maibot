"""HTTP client, configuration, CLI and text-card tests. Only the HTTP test listens, on loopback."""
import contextlib, io, json, os, socket, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from xhs_support import FEED, OTHER, PNG, SERVICE_TOKEN, TOKEN, UID, Env, FakeCore, FakeService, Clock, comment, post_decision
from xiaohongshu import cli, textcard
from xiaohongshu import config as xconfig
from xiaohongshu.core_client import CoreClient
from xiaohongshu.http_client import Ambiguous, McpClient, NotSent, Rejected, make_transport
from xiaohongshu.store import AlreadyRunning, Store

ROOT = Path(__file__).resolve().parents[2]


class Handler(BaseHTTPRequestHandler):
    mode, seen = 'ok', []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get('Authorization')))
        if Handler.mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:1/elsewhere')
            self.end_headers()
            return
        status, body = {'ok': (200, {'success': True, 'data': {'is_logged_in': True}}), 'err500': (500, {'error': 'boom'}),
                        'bad400': (400, {'error': 'bad', 'code': 'INVALID_REQUEST'}), 'challenge': (500, {'error': '需要验证码'}),
                        'big': (200, {'success': True, 'data': {'x': 'y' * 5000}}), 'notjson': (200, None)}[Handler.mode]
        raw = b'<html>' if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, mode, max_bytes=2 * 1024 * 1024):
        Handler.mode = mode
        Handler.seen = []
        return McpClient(self.url, SERVICE_TOKEN, timeout=5, max_bytes=max_bytes)

    def test_success_sends_bearer_token_to_fixed_route(self):
        self.assertEqual(self.client('ok').login_status(), {'is_logged_in': True})
        self.assertEqual(Handler.seen, [('/api/v1/login/status', 'Bearer ' + SERVICE_TOKEN)])

    def test_redirects_are_never_followed(self):
        with self.assertRaises(Ambiguous):
            self.client('redirect').login_status()
        self.assertEqual(len(Handler.seen), 1)

    def test_error_classification(self):
        with self.assertRaises(Ambiguous):
            self.client('err500').login_status()
        with self.assertRaises(Rejected) as ctx:
            self.client('bad400').login_status()
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(Ambiguous) as ctx:
            self.client('challenge').login_status()
        self.assertTrue(ctx.exception.challenge)
        with self.assertRaises(Ambiguous):
            self.client('notjson').login_status()

    def test_response_size_is_bounded(self):
        with self.assertRaises(Ambiguous):
            self.client('big', max_bytes=1000).login_status()

    def test_refused_connection_is_not_sent(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(NotSent):
            make_transport('http://127.0.0.1:%d' % port, SERVICE_TOKEN, 1000)('GET', '/api/v1/login/status', None, 2)

    def test_routes_are_allow_listed_and_urls_restricted(self):
        client = self.client('ok')
        for method, path in (('GET', '/mcp'), ('DELETE', '/api/v1/login/cookies'), ('GET', 'http://evil.example/x'), ('POST', '/api/v1/feeds/like')):
            with self.assertRaises(ValueError):
                client.call(method, path)
        for bad in ('http://example.com:18060', 'https://127.0.0.1:18060', 'http://127.0.0.1', 'http://' + 'user:pw' + '@127.0.0.1:18060',
                    'http://127.0.0.1:18060/api', 'http://' + '.'.join(['0'] * 4) + ':18060', 'http://[::2]:18060', 'ftp://127.0.0.1:18060'):
            with self.assertRaises(xconfig.ConfigError, msg=bad):
                xconfig.validate_url(bad)
        self.assertEqual(xconfig.validate_url('http://127.0.0.1:18060'), ('127.0.0.1', 18060))


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid_and_everything_is_off_and_empty(self):
        raw = json.loads((ROOT / 'config' / 'xiaohongshu.example.json').read_text(encoding='utf-8'))
        cfg = xconfig.validate(raw)
        self.assertFalse(any(cfg[k] for k in xconfig.SWITCHES))
        self.assertTrue(cfg['dry_run'])
        self.assertEqual((cfg['bound_uid'], cfg['token_file']), ('', ''))
        self.assertFalse(cfg['text_card']['enabled'])
        text = json.dumps(raw)
        for forbidden in ('/Users/', 'cookie', 'Bearer'):
            self.assertNotIn(forbidden, text)
        policy = json.loads((ROOT / 'config' / 'xiaohongshu-core-policy.example.json').read_text(encoding='utf-8'))
        self.assertIs(policy['xhs_enabled'], False)

    def test_validation_rules(self):
        for bad in ({'unknown': 1}, {'token': 'x'}, {'enabled': 'yes'}, {'enabled': True}, {'limits': {'daily_posts': 99}},
                    {'limits': {'nope': 1}}, {'limits': {'daily_posts': True}}, {'visibility': '所有人'}, {'bound_uid': 'bad uid!'},
                    {'state_dir': 'relative'}, {'service_url': 'http://example.com:1'}, {'token_env': 'lower'},
                    {'text_card': {'enabled': True, 'font_path': ''}}, {'limits': {'active_start_hour': 10, 'active_end_hour': 9}}):
            with self.assertRaises(xconfig.ConfigError, msg=str(bad)):
                xconfig.validate(bad)
        self.assertEqual(xconfig.validate({'enabled': True, 'bound_uid': UID})['bound_uid'], UID)

    def test_service_token_sources(self):
        cfg = xconfig.validate({})
        self.assertEqual(xconfig.load_token(cfg, {'XHS_MCP_AUTH_TOKEN': SERVICE_TOKEN}), SERVICE_TOKEN)
        for bad in ({}, {'XHS_MCP_AUTH_TOKEN': 'short'}, {'XHS_MCP_AUTH_TOKEN': 'has space ' + 'x' * 20}):
            with self.assertRaises(xconfig.ConfigError):
                xconfig.load_token(cfg, bad)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(os.path.realpath(tmp)) / 'token'
            path.write_text(SERVICE_TOKEN + '\n')
            cfg = xconfig.validate({'token_file': str(path)})
            path.chmod(0o644)
            with self.assertRaises(xconfig.ConfigError):
                xconfig.load_token(cfg)
            path.chmod(0o600)
            self.assertEqual(xconfig.load_token(cfg), SERVICE_TOKEN)

    def test_no_real_account_parameters_in_source(self):
        files = list((ROOT / 'extensions' / 'xiaohongshu').glob('*.py')) + [ROOT / 'docs' / 'XIAOHONGSHU.zh.md']
        for path in files:
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('/Users/', text, path.name)
            self.assertNotIn('xhsbot UID', text, path.name)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / 'media').mkdir()
        self.clock, self.core, self.service = Clock(), FakeCore(), None
        self.service = FakeService(self.clock)
        self.raw = {'enabled': True, 'bound_uid': UID, 'comment_enabled': True, 'dry_run': True,
                    'state_dir': str(self.root / 'state'), 'media_dir': str(self.root / 'media')}
        self.config = self.root / 'config.json'
        self.write_config()
        self.env = {'XHS_MCP_AUTH_TOKEN': SERVICE_TOKEN}

    def write_config(self, **over):
        self.raw.update(over)
        self.config.write_text(json.dumps(self.raw))

    def factory(self, cfg, environ):
        return cli.build(cfg, environ, http=McpClient(cfg['service_url'], SERVICE_TOKEN, transport=self.service),
                         core=CoreClient(transport=self.core))

    def run_cli(self, *argv, factory=True):
        out = []
        code = cli.main(['--config', str(self.config), *argv], environ=self.env, factory=self.factory if factory else None, out=out.append)
        return code, [json.loads(line) for line in out]

    def test_help_works_for_every_command(self):
        for argv in ([], ['run'], ['status'], ['doctor'], ['qrcode'], ['pause'], ['resume'], ['poll-once'], ['autonomy-once'],
                     ['publish'], ['comment'], ['reply'], ['deliver-once']):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                cli.parser().parse_args([*argv, '--help'])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn('usage', buf.getvalue())
        publish_help = io.StringIO()
        with contextlib.redirect_stdout(publish_help), self.assertRaises(SystemExit):
            cli.parser().parse_args(['publish', '--help'])
        self.assertIn('--visibility', publish_help.getvalue())

    def test_status_doctor_and_default_dry_run(self):
        code, out = self.run_cli('status')
        self.assertEqual((code, out[0]['enabled'], out[0]['dry_run']), (0, True, True))
        code, out = self.run_cli('doctor')
        self.assertTrue(out[0]['uid_matches_config'])
        self.env['XHS_TARGET_TOKEN'] = TOKEN
        code, out = self.run_cli('comment', '--feed-id', FEED, '--text', '先看看')
        self.assertEqual(out[0]['status'], 'dry_run')
        self.assertEqual(self.service.writes(), [])

    def test_manual_commands_create_pending_drafts_and_never_send(self):
        self.write_config(dry_run=False, publish_enabled=True, reply_enabled=True)
        self.env['XHS_TARGET_TOKEN'] = TOKEN
        code, out = self.run_cli('comment', '--feed-id', FEED, '--text', '真评论')
        self.assertEqual(out[0]['status'], 'draft_submitted')
        self.assertEqual(self.service.writes(), [])                # the CLI cannot publish without an approved draft
        (draft,) = self.core.drafts.values()
        self.assertEqual((draft['kind'], draft['content'], draft['audience']), ('comment', '真评论', 'public'))
        self.assertNotIn(TOKEN, json.dumps(self.core.payloads))     # the target token never goes to the core
        self.assertNotIn(TOKEN, json.dumps(out))
        self.assertNotIn(SERVICE_TOKEN, json.dumps(out))
        (self.root / 'media' / 'manifest.json').write_text(json.dumps({'public': True, 'items': []}))
        code, out = self.run_cli('publish', '--title', 't', '--content', 'c', '--image-id', 'x')
        self.assertTrue(out[0]['status'].startswith('image_rejected'))
        self.assertEqual(self.service.writes(), [])

    def test_qrcode_saves_task_file_only(self):
        target = self.root / 'qr.png'
        code, out = self.run_cli('qrcode', '--out', str(target))
        self.assertTrue(out[0]['saved'])
        self.assertEqual(target.read_bytes(), PNG)
        self.assertNotIn('iVBOR', json.dumps(out))

    def test_pause_and_resume_files(self):
        self.write_config(dry_run=False)
        self.assertEqual(self.run_cli('pause')[1][0]['status'], 'pause_requested')
        self.assertTrue((self.root / 'state' / 'PAUSE').exists())
        self.assertEqual(self.run_cli('status')[1][0]['paused'], 'pause_file')
        self.assertEqual(self.run_cli('resume')[1][0]['status'], 'resume_requested')
        self.assertFalse((self.root / 'state' / 'PAUSE').exists())
        self.assertFalse(self.run_cli('status')[1][0]['paused'])   # RESUME applied by the single writer
        self.assertFalse((self.root / 'state' / 'RESUME').exists())

    def test_running_service_keeps_the_write_lock(self):
        holder = Store(self.raw['state_dir'], now=self.clock)
        self.addCleanup(holder.close)
        code, out = self.run_cli('status', factory=False)
        self.assertEqual(code, 0)
        self.assertIn('today', out[0])
        code, out = self.run_cli('publish', '--title', 't', '--content', 'c', '--image-id', 'x', factory=False)
        self.assertEqual((code, out[0]['error']), (3, 'executor_already_running'))
        with self.assertRaises(AlreadyRunning):
            Store(self.raw['state_dir'])

    def test_bad_config_is_reported_not_traceback(self):
        self.config.write_text('{"enabled": true}')
        code, out = self.run_cli('status')
        self.assertEqual((code, out[0]['error']), (2, 'bound_uid_required_when_enabled'))


FONTS = ['/System/Library/Fonts/Supplemental/Arial Unicode.ttf', '/Library/Fonts/Arial Unicode.ttf', '/System/Library/Fonts/PingFang.ttc',
         '/System/Library/Fonts/STHeiti Light.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
         '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']
FONT = next((f for f in FONTS if os.path.isfile(f)), None)
try:
    import PIL  # noqa: F401
except ImportError:
    PIL = None


class TextCardTests(unittest.TestCase):
    def card_env(self, **over):
        return Env(self, text_card={'enabled': True, 'font_path': FONT or '/missing/font.ttf'}, **over)

    @unittest.skipUnless(PIL and FONT, 'Pillow or a system font is not available')
    def test_card_is_rendered_from_approved_text_with_unique_ids(self):
        env = self.card_env(limits={'daily_posts': 3})
        env.ex.publish('今日小发现', '一句话。更多内容', 'textcard', manual=True)
        env.ex.publish('另一个标题', '另一段话', 'textcard', manual=True)
        first, second = [w[2]['images'][0] for w in env.service.writes()]
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertTrue(path.startswith(str(env.root / 'media')))
            self.assertEqual(Path(path).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        manifest = json.loads((env.root / 'media' / 'manifest.json').read_text())
        cards = [i for i in manifest['items'] if i.get('source') == 'generated_text_card']
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]['caption'], textcard.CAPTION)
        self.assertTrue(manifest['public'])
        self.assertTrue(all(r['image_id'].startswith('card-') for r in env.store.actions()))

    @unittest.skipUnless(PIL and FONT, 'Pillow or a system font is not available')
    def test_autonomous_post_can_choose_the_text_card_candidate(self):
        env = self.card_env()
        (env.root / 'media' / 'manifest.json').write_text(json.dumps({'public': True, 'items': []}))
        env.core_stub.decisions.update(select={'action': 'post'}, post=post_decision(image_id='textcard'))
        r = env.ex.autonomy_step(force=True)
        self.assertEqual(r['status'], 'accepted')
        select = env.core_stub.of('xhs_plan')[0]
        self.assertEqual(select['candidates']['images'], [{'id': 'textcard', 'caption': textcard.CAPTION}])
        self.assertEqual(env.service.writes()[0][2]['images'][0][-4:], '.png')

    def test_disabled_or_failing_card_never_writes(self):
        env = Env(self)
        self.assertEqual(env.ex.publish('标题', '正文', 'textcard', manual=True)['status'], 'image_rejected:text_card_disabled')
        env2 = self.card_env()
        with mock.patch.object(textcard, 'render', side_effect=textcard.CardError('pillow_unavailable')):
            self.assertEqual(env2.ex.publish('标题', '正文', 'textcard', manual=True)['status'], 'image_rejected:pillow_unavailable')
        self.assertEqual(env.service.writes() + env2.service.writes(), [])
        self.assertEqual(env2.store.actions(), [])

    def test_card_offered_only_when_enabled(self):
        env = Env(self)
        (env.root / 'media' / 'manifest.json').write_text(json.dumps({'public': True, 'items': []}))
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'nothing_to_consider')
        self.assertEqual(env.core_stub.payloads, [])


if __name__ == '__main__':
    unittest.main()
