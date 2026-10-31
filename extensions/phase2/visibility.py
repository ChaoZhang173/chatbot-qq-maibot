"""Bounded, read-only visibility checks, separate from delivery and dedupe."""
import json
import time
import requests
from .api import bounded_body

KINDS = ('proactive', 'reply', 'manual_proactive', 'dynamic')


def comment_nodes(value):
    if isinstance(value, dict):
        if isinstance(value.get('content'), dict) and (value.get('rpid_str') or value.get('rpid')):
            yield value
        for child in value.values():
            yield from comment_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from comment_nodes(child)


def comment_match(data, row):
    return next((node for node in comment_nodes(data)
                 if str(node.get('rpid_str') or node.get('rpid')) == str(row['remote_id'])
                 and str((node.get('member') or {}).get('mid') or node.get('mid')) == str(row['uid'])
                 and node['content'].get('message') == row['text']), None)


def dynamic_match(data, row):
    item = data.get('item') or {}
    modules = item.get('modules') or {}
    dyn = modules.get('module_dynamic') or {}
    text = (dyn.get('desc') or {}).get('text')
    if text is None:
        text = (((dyn.get('major') or {}).get('opus') or {}).get('summary') or {}).get('text')
    return (str(item.get('id_str')) == str(row['remote_id'])
            and str((modules.get('module_author') or {}).get('mid')) == str(row['uid'])
            and text == row['text'] and item.get('visible') is not False)


class VisibilityChecker:
    def __init__(self, api, store, outbox, now=time.time, guest=None):
        self.api, self.s, self.outbox, self.now = api, store, outbox, now
        self.guest = guest or requests.Session()
        self.guest.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
        self.s.db.execute('CREATE TABLE IF NOT EXISTS visibility_checks('
                          'action_id TEXT PRIMARY KEY,attempts INTEGER NOT NULL,next_at REAL NOT NULL)')

    @staticmethod
    def get(session, path, params):
        try:
            with session.get('https://api.bilibili.com' + path, params=params,
                             timeout=(3, 6), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    return {'code': None, 'data': {}, 'read_error': True}
                payload = json.loads(bounded_body(response, 2 * 1024 * 1024))
            if not isinstance(payload, dict) or type(payload.get('code')) is not int:
                return {'code': None, 'data': {}, 'read_error': True}
            return payload
        except Exception:
            return {'code': None, 'data': {}, 'read_error': True}

    def check(self, row):
        # No account cookies are ever copied into the guest session.
        self.guest.cookies.clear()
        dynamic = row['kind'] == 'dynamic'
        if dynamic:
            path = '/x/polymer/web-dynamic/v1/detail'
            params = {'id': row['remote_id'], 'features': 'itemOpusStyle'}
        else:
            typ, oid = row['target'].split(':', 1)
            path = '/x/v2/reply/detail'
            params = {'type': int(typ), 'oid': oid, 'root': row['remote_id']}
        evidence = {'method': 'bounded_dynamic_api' if dynamic else 'bounded_comment_api'}
        for name, session in (('authenticated', self.api.http), ('guest', self.guest)):
            payload = self.get(session, path, params)
            data = payload.get('data') or {}
            match = dynamic_match(data, row) if dynamic else comment_match(data, row)
            evidence[name + '_detail_code'] = payload.get('code')
            evidence[name + '_exact_match'] = payload.get('code') == 0 and bool(match)
            if name == 'authenticated':
                evidence['authenticated_state'] = None if dynamic or not match else match.get('state')
        # A list hit proves presence in that bounded public list. A miss never
        # proves hiding: guest lists can be truncated or ranking-dependent.
        if not dynamic:
            payload = self.get(self.guest, '/x/v2/reply',
                               {'type': int(typ), 'oid': oid, 'pn': 1, 'ps': 20, 'sort': 2})
            evidence['guest_list_code'] = payload.get('code')
            evidence['guest_list_exact_match'] = payload.get('code') == 0 and bool(comment_match(payload.get('data'), row))
        state = 'not_publicly_verified'
        if evidence['guest_exact_match']:
            state = 'guest_api_visible'
        elif evidence['authenticated_exact_match'] and evidence['guest_detail_code'] in (12006, -404, 4101131):
            state = 'account_only_or_not_publicly_verified'
        return state, evidence

    def tick(self):
        now = self.now()
        if self.s.get('visibility_next_at', 0) > now:
            return {'status': 'waiting'}
        # Persistent pacing applies even after a process restart or a read error.
        self.s.put('visibility_next_at', now + 300)
        row = self.s.db.execute(
            "SELECT a.*, coalesce(v.attempts,0) AS checks FROM actions a "
            "LEFT JOIN visibility_checks v ON v.action_id=a.id "
            "WHERE a.kind IN ('proactive','reply','manual_proactive','dynamic') "
            "AND a.status='success' AND a.remote_id!='' AND a.uid=? "
            "AND a.created BETWEEN ? AND ? AND coalesce(v.attempts,0)<4 "
            "AND coalesce(v.next_at,0)<=? "
            "ORDER BY coalesce(v.attempts,0),a.created DESC LIMIT 1",
            (str(self.s.get('bound_uid', '')), now - 7 * 86400, now - 60, now),
        ).fetchone()
        if row is None:
            return {'status': 'no_due_checks'}
        attempts = row['checks'] + 1
        # Reserve the check first. Never replay a content write to verify it.
        self.s.db.execute('INSERT INTO visibility_checks VALUES(?,?,?) ON CONFLICT(action_id) '
                          'DO UPDATE SET attempts=excluded.attempts,next_at=excluded.next_at',
                          (row['id'], attempts, now + (1800, 21600, 86400, 86400)[attempts - 1]))
        try:
            state, evidence = self.check(row)
            # A human check from another account answers a different question
            # than these API reads. Preserve its source and time on later probes.
            prior = self.s.db.execute('SELECT payload FROM action_visibility WHERE action_id=?', (row['id'],)).fetchone()
            if prior:
                previous = json.loads(prior['payload']).get('visibility_evidence', {})
                for field in ('observer_list_seen', 'observer_source', 'observer_recorded_at'):
                    if field in previous:
                        evidence[field] = previous[field]
            self.outbox.visibility(row['id'], row['remote_id'], self.now(), state, evidence)
            # A successful early read is provisional. Delayed moderation can
            # change a previously visible comment, so retain all bounded checks.
            result = {'status': 'checked', 'remote_id': row['remote_id'], 'public_visibility': state,
                      'guest_list_exact_match': evidence.get('guest_list_exact_match')}
            self.s.put('visibility_last_check', {'at': now, **result})
            return result
        except Exception as exc:
            self.s.put('visibility_last_error', {'at': now, 'type': type(exc).__name__})
            return {'status': 'read_check_failed'}
