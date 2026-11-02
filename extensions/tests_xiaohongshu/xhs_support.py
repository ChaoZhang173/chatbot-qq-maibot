"""Stub MCP service and stub core for offline Xiaohongshu tests. No sockets, no network."""
import json, os, tempfile, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from xiaohongshu import config as xconfig
from xiaohongshu.core_client import CoreClient
from xiaohongshu.http_client import Ambiguous, McpClient, NotSent
from xiaohongshu.media import PublishableMedia
from xiaohongshu.runner import Executor, claim_hash
from xiaohongshu.store import Store

UID = 'u' + 'a' * 11          # synthetic bound account
OTHER = 'u' + 'b' * 11
TOKEN = 'tok-secret-' + 'x' * 16  # synthetic xsec token that must never leave memory
SERVICE_TOKEN = 'svc-secret-' + 'y' * 16
FEED = 'f' * 24
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


def post_decision(**over):
    return {'willing': True, 'title': '今日小发现', 'content': '一些想法。第二句话', 'tags': ['AI'], 'image_id': 'card1', **over}


def beijing(hour=14, day=2):
    return datetime(2026, 11, day, hour, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class Clock:
    def __init__(self, t=None):
        self.t = t or beijing()

    def __call__(self):
        return self.t


def comment(cid, text, user=OTHER, when=None, subs=()):
    return {'id': cid, 'noteId': FEED, 'content': text, 'createTime': int((when or 0) * 1000), 'userInfo': {'userId': user, 'nickname': 'nick'},
            'subComments': list(subs)}


class FakeService:
    """Records every request; behaves like xiaohongshu-mcp v2.5.5 responses."""
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.logged_in, self.uid = True, UID
        self.notifications, self.own_feeds, self.comments = [], [], []
        self.feeds, self.fail = [], None      # fail: None | 'notsent' | 'ambiguous_after' | 'ambiguous_lost'
        self.note_author, self.desc, self.note_title = UID, 'post body', 'a post'
        self.status_calls, self.status_override, self.hide_publish = 0, None, False
        self.on_status = None          # called on every login/status request (to simulate a pause mid-flow)
        self.hidden_until_deep = set()  # comment ids only returned by a deep (load_all_comments) detail read
        self.hidden_subs_until_deep = set()  # NESTED sub-comment ids that a shallow read does not return (inside subComments)
        self.misplace_reply_under = None  # land our reply in another root thread (wrong target)
        self.body_suffix = ''          # extra text the platform shows after the body (e.g. tag display)
        self.force_suffix = None       # if set, replaces the tag display after a publish (longer body than the draft)
        self.fail_list = False
        self.title_suffix = ''         # what the platform shows after a published title (a real note showed one trailing space)

    def writes(self):
        return [c for c in self.calls if c[0] == 'POST' and c[1] in ('/api/v1/publish', '/api/v1/feeds/comment', '/api/v1/feeds/comment/reply')]

    def __call__(self, method, path, body, timeout):
        data = json.loads(body) if body else None
        self.calls.append((method, path, data))
        ok = lambda payload: (200, json.dumps({'success': True, 'data': payload, 'message': 'ok'}, ensure_ascii=False).encode())
        if path == '/api/v1/login/status':
            self.status_calls += 1
            if self.on_status:
                self.on_status(self.status_calls)
            if self.status_override:
                return self.status_override
            return ok({'is_logged_in': self.logged_in, 'username': 'someone', **({'user_id': self.uid} if self.uid else {})})
        if path == '/api/v1/login/qrcode':
            return ok({'timeout': '240s', 'is_logged_in': False, 'img': 'data:image/png;base64,' + __import__('base64').b64encode(PNG).decode()})
        if path == '/api/v1/user/me':
            return ok({'data': {'userBasicInfo': {'nickname': 'me'}, 'feeds': self.own_feeds}})
        if path == '/api/v1/feeds/list':
            if self.fail_list:
                return 500, b'{"error":"boom"}'
            return ok({'feeds': self.feeds, 'count': len(self.feeds)})
        if path == '/api/v1/feeds/search':
            return ok({'feeds': self.feeds, 'count': len(self.feeds)})
        if path == '/api/v1/notifications/list':
            return ok({'data': {'tab': 'mentions', 'filtered': 0, 'items': self.notifications}})
        if path == '/api/v1/feeds/detail':
            deep = bool(data.get('load_all_comments'))
            visible = [c for c in self.comments if deep or c['id'] not in self.hidden_until_deep]
            if not deep and self.hidden_subs_until_deep:
                visible = [{**c, 'subComments': [s for s in c['subComments'] if s['id'] not in self.hidden_subs_until_deep]} for c in visible]
            return ok({'feed_id': data['feed_id'], 'data': {'note': {'noteId': data['feed_id'], 'title': self.note_title, 'desc': self.desc + self.body_suffix,
                       'time': int(self.clock() * 1000), 'user': {'userId': self.note_author, 'nickname': 'author'}},
                       'comments': {'list': visible, 'hasMore': False}}})
        if method == 'POST' and path in ('/api/v1/publish', '/api/v1/feeds/comment', '/api/v1/feeds/comment/reply'):
            if self.fail == 'notsent':
                raise NotSent('connect_failed')
            committed = self.fail != 'ambiguous_lost_before'
            if committed:
                self._apply(path, data)
            if self.fail in ('ambiguous_after', 'ambiguous_lost', 'ambiguous_lost_before'):
                raise Ambiguous('transport_interrupted')
            return ok({'status': 'done'})
        return 404, b'{}'

    def _apply(self, path, data):
        now = self.clock()
        if path == '/api/v1/publish':
            self.published = data
            self.note_title, self.desc = data['title'] + self.title_suffix, data['content']
            self.body_suffix = (self.force_suffix if self.force_suffix is not None
                                else ''.join(' #%s[话题]#' % t for t in data.get('tags', [])))
            if not self.hide_publish:
                self.own_feeds.append({'id': 'n' * 24, 'xsecToken': 'feedtoken-123456', 'noteCard': {'displayTitle': data['title'] + self.title_suffix, 'user': {'userId': UID}}})
        else:
            self.seq = getattr(self, 'seq', 100) + 1
            new = comment('cn%04d' % self.seq, data['content'], user=UID, when=now)
            if path.endswith('/reply'):
                for c in self.comments:
                    in_thread = c['id'] == data['comment_id'] or any(s['id'] == data['comment_id'] for s in c['subComments'])
                    if self.misplace_reply_under:
                        in_thread = c['id'] == self.misplace_reply_under
                    if in_thread:
                        c['subComments'].append(new)
                        break
            else:
                self.comments.append(new)


class DirectExecutor(Executor):
    """TEST ONLY: exercises the low-level send mechanics. The production class (and the CLI) never sends without an
    owner-approved draft; tests of that path use Env(strict=True)."""
    ALLOW_DIRECT = True


class FakeCore:
    """Core stub that records every payload; decisions are scripted per task. Also a minimal review engine:
    drafts are stored, `approve()` plays the owner, claim hands an approved draft out once, report records outcomes."""
    def __init__(self):
        self.payloads, self.decisions, self.down = [], {}, False
        self.drafts, self.reports, self.reject_submit, self.intro_done = {}, [], '', True
        self.report_down, self.can_submit, self.intro_active, self.tamper = False, True, False, None

    def approve(self, draft_id):
        self.drafts[draft_id]['status'] = 'approved'

    def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        if self.down:
            raise OSError('down')
        op = payload['op']
        if op == 'xhs_plan':
            decision = self.decisions.get(payload['task'], {'willing': False})
            decision = decision(payload) if callable(decision) else decision
            return {'ok': True, 'decision': decision, 'persona_version': 3, 'source_ids': ['evt:1']}
        if op == 'xhs_draft_submit':
            if self.reject_submit:
                return {'ok': False, 'error': self.reject_submit}
            d = payload['draft']
            same = next((x for x in self.drafts.values() if x['submit_key'] == d['submit_key']), None)
            if not same:
                same = {**d, 'draft_id': 'D%05d' % (len(self.drafts) + 1), 'version': 1, 'status': 'pending'}
                self.drafts[same['draft_id']] = same
            return {'ok': True, 'draft': {k: same[k] for k in ('draft_id', 'version', 'status', 'kind')}}
        if op == 'xhs_draft_status':
            return {'ok': True, 'intro_done': self.intro_done, 'intro_active': self.intro_active, 'can_submit': self.can_submit}
        if op == 'xhs_draft_claim':
            for d in self.drafts.values():
                if d['status'] == 'approved' and d['kind'] in payload.get('kinds', []):
                    d['status'], d['claim_token'] = 'claimed', '%032x' % (len(self.payloads) * 7919)
                    claim = json.loads(json.dumps({**d, 'claim_token': d['claim_token'], 'hash': claim_hash(d), 'approved_version': d['version']}))
                    if self.tamper:
                        self.tamper(claim)       # simulates a claim altered after the owner approved it
                    return {'ok': True, 'draft': claim}
            return {'ok': True, 'draft': None}
        if op == 'xhs_draft_report':
            if self.report_down:
                raise OSError('report down')
            d = self.drafts[payload['draft_id']]
            assert payload['claim_token'] == d['claim_token']
            self.reports.append((payload['draft_id'], payload['outcome']))
            d['status'] = {'failed_not_sent': 'pending'}.get(payload['outcome'], payload['outcome'])
            return {'ok': True}
        return {'ok': True}

    def of(self, op):
        return [p for p in self.payloads if p['op'] == op]


def make_config(root, **over):
    base = {'enabled': True, 'bound_uid': UID, 'state_dir': str(Path(root) / 'state'), 'media_dir': str(Path(root) / 'media'),
            'dry_run': False, 'publish_enabled': True, 'comment_enabled': True, 'reply_enabled': True, 'browse_enabled': True,
            'autonomy_enabled': True, 'limits': {'min_gap_reply': 60, 'min_gap_comment': 300, 'min_gap_post': 3600, 'daily_replies': 5}}
    limits = {**base['limits'], **over.pop('limits', {})}
    base.update(over)
    base['limits'] = limits
    return xconfig.validate(base)


class Env:
    """A complete executor over temp dirs with stub service/core and a controllable clock.
    strict=True uses the production Executor class (owner review enforced); the default uses the test-only
    DirectExecutor so the low-level send mechanics stay testable."""
    def __init__(self, test, strict=False, **over):
        self.strict = strict
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        # Real path: macOS /var is a symlink to /private/var and media_dir ancestors must not be symlinks.
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / 'media').mkdir()
        (self.root / 'media' / 'a-card.png').write_bytes(PNG)
        (self.root / 'media' / 'manifest.json').write_text(json.dumps(
            {'public': True, 'items': [{'id': 'card1', 'file': 'a-card.png', 'caption': 'public card', 'publishable': True}]}))
        self.cfg = make_config(self.root, **over)
        self.clock, self.core_stub = Clock(), FakeCore()
        self.service = FakeService(self.clock)
        self.open()
        test.addCleanup(self.close)

    def open(self):
        self.store = Store(self.cfg['state_dir'], now=self.clock)
        http = McpClient(self.cfg['service_url'], SERVICE_TOKEN, transport=self.service)
        self.ex = (Executor if self.strict else DirectExecutor)(self.cfg, http, CoreClient(transport=self.core_stub), self.store,
                                                                PublishableMedia(self.cfg['media_dir']), now=self.clock, rand=lambda: 0.9)

    def close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def restart(self):
        self.close()
        self.open()

    def notify(self, cid, text, when=None, user=OTHER, **extra):
        item = {'id': 'n' + cid, 'type': 'comment/item', 'time': int((when or self.clock()) * 1000), 'from': {'user_id': user, 'nickname': 'nick'},
                'comment_id': cid, 'comment_text': text, 'feed_id': FEED, 'feed_xsec_token': TOKEN, **extra}
        self.service.notifications.insert(0, item)
        self.service.comments.append(comment(cid, text, user=user, when=when or self.clock()))
        return item

    def private_feed(self, feed_id=FEED):
        """A ledger row proving a self-only note: created as self_only and read back exactly (the only accepted proof)."""
        aid, reason = self.store.reserve('post', content='private acceptance ' + feed_id, title='t' + feed_id[:8], image_id='card1',
                                         limits={**self.cfg['limits'], 'daily_posts': 99}, manual=True, audience='self_only')
        assert aid, reason
        self.store.finish(aid, 'accepted', remote_id=feed_id, verification='own_page_exact')

    def db_bytes(self, include_tokens=False):
        """Everything in the state dir except the private short-lived token file (which is checked separately)."""
        data = b''
        for p in Path(self.cfg['state_dir']).iterdir():
            if p.is_file() and (include_tokens or not p.name.startswith('tokens.sqlite')):
                data += p.read_bytes()
        return data
