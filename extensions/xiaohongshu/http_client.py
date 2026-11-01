"""Bounded client for the local xiaohongshu-mcp HTTP API.

Only fixed API paths on one explicitly configured loopback service are reachable;
redirects are never followed. Write outcomes are classified for the ledger:
NotSent (nothing left this process), Rejected (the service refused the request
before acting) or Ambiguous (the request may have been performed).
"""
import http.client, json, re
from .config import validate_url

ROUTES = {
    ('GET', '/api/v1/login/status'), ('GET', '/api/v1/login/qrcode'), ('GET', '/api/v1/user/me'),
    ('GET', '/api/v1/feeds/list'), ('POST', '/api/v1/feeds/search'), ('POST', '/api/v1/feeds/detail'),
    ('GET', '/api/v1/notifications/unread'), ('POST', '/api/v1/notifications/list'),
    ('POST', '/api/v1/publish'), ('POST', '/api/v1/feeds/comment'), ('POST', '/api/v1/feeds/comment/reply'),
}
CHALLENGE = re.compile(r'验证码|captcha|滑块|安全验证|访问频繁|频繁|限制|风控|risk|rate.?limit|forbidden|封禁|异常', re.I)


class ServiceError(Exception):
    """Base class; `detail` is bounded server text kept only for classification."""
    def __init__(self, kind, detail=''):
        super().__init__(kind)
        self.kind, self.detail = kind, str(detail)[:200]

    @property
    def challenge(self):
        return bool(CHALLENGE.search(self.detail))


class NotSent(ServiceError):
    pass


class Rejected(ServiceError):
    def __init__(self, status, detail=''):
        super().__init__('rejected', detail)
        self.status = status


class Ambiguous(ServiceError):
    pass


def make_transport(base_url, token, max_bytes):
    host, port = validate_url(base_url)

    def transport(method, path, body, timeout):
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            try:
                conn.connect()
            except OSError:
                raise NotSent('connect_failed') from None
            try:
                headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
                if body is not None:
                    headers['Content-Type'] = 'application/json'
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read(max_bytes + 1)
            except (OSError, http.client.HTTPException):
                raise Ambiguous('transport_interrupted') from None
            if len(data) > max_bytes:
                raise Ambiguous('response_too_large')
            return response.status, data
        finally:
            conn.close()
    return transport


class McpClient:
    def __init__(self, base_url, token, *, timeout=120, write_timeout=400, max_bytes=2 * 1024 * 1024, transport=None):
        validate_url(base_url)
        self.timeout, self.write_timeout, self.max_bytes = timeout, write_timeout, max_bytes
        self.transport = transport or make_transport(base_url, token, max_bytes)

    def call(self, method, path, body=None, *, write=False):
        if (method, path) not in ROUTES:
            raise ValueError('route_not_allowed')
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        status, data = self.transport(method, path, raw, self.write_timeout if write else self.timeout)
        if len(data) > self.max_bytes:
            raise Ambiguous('response_too_large')
        try:
            envelope = json.loads(data)
        except ValueError:
            if 400 <= status < 500:
                raise Rejected(status, '') from None
            raise Ambiguous('unreadable_response') from None
        if not isinstance(envelope, dict):
            raise Ambiguous('unexpected_response')
        detail = ' '.join(str(envelope.get(k, '')) for k in ('error', 'code', 'details', 'message'))
        if status in (400, 401, 403, 404, 422):
            raise Rejected(status, detail)
        if write and path == '/api/v1/feeds/comment/reply' and status >= 500:
            # Only the patched local runtime's explicit pre-submit marker proves no write.
            marker = re.match(r'XHS_NOT_SENT:(navigate|find_comment|reply_button|reply_input|type_reply|submit_button):', str(envelope.get('details') or ''))
            if marker:
                raise NotSent('reply_not_sent:' + marker[1])
        if status != 200:
            raise Ambiguous('http_%d' % status, detail)
        if envelope.get('success') is not True:
            raise Ambiguous('service_reported_failure', detail)
        return envelope.get('data')

    # --- reads -----------------------------------------------------------------
    def login_status(self):
        return self.call('GET', '/api/v1/login/status') or {}

    def login_qrcode(self):
        return self.call('GET', '/api/v1/login/qrcode') or {}

    def me(self):
        return self.call('GET', '/api/v1/user/me') or {}

    def list_feeds(self):
        return (self.call('GET', '/api/v1/feeds/list') or {}).get('feeds') or []

    def search(self, keyword):
        return (self.call('POST', '/api/v1/feeds/search', {'keyword': keyword, 'filters': {'sort_by': '综合', 'note_type': '图文'}}) or {}).get('feeds') or []

    def feed_detail(self, feed_id, xsec_token, max_comments, deep=False):
        """deep=True scrolls the whole (bounded) comment area and expands replies; used only to locate a target."""
        body = {'feed_id': feed_id, 'xsec_token': xsec_token, 'load_all_comments': deep,
                'comment_config': {'click_more_replies': deep, 'max_replies_threshold': 50 if deep else 0,
                                   'max_comment_items': min(max_comments, 100), 'scroll_speed': 'normal'}}
        return (self.call('POST', '/api/v1/feeds/detail', body) or {}).get('data') or {}

    def notifications(self, limit):
        data = self.call('POST', '/api/v1/notifications/list', {'tab': 'mentions', 'limit': limit}) or {}
        return (data.get('data') or {}).get('items') or []

    # --- writes: callers must reserve a ledger row first ------------------------
    def publish(self, title, content, images, tags, visibility='公开可见'):
        if visibility not in ('公开可见', '仅自己可见'):
            raise ValueError('invalid_visibility')
        body = {'title': title, 'content': content, 'images': images, 'tags': tags, 'visibility': visibility}
        return self.call('POST', '/api/v1/publish', body, write=True)

    def comment(self, feed_id, xsec_token, content):
        return self.call('POST', '/api/v1/feeds/comment', {'feed_id': feed_id, 'xsec_token': xsec_token, 'content': content}, write=True)

    def reply(self, feed_id, xsec_token, comment_id, content):
        # comment_id only: the service falls back to user_id matching, which could pick another comment.
        return self.call('POST', '/api/v1/feeds/comment/reply',
                         {'feed_id': feed_id, 'xsec_token': xsec_token, 'comment_id': comment_id, 'content': content}, write=True)
