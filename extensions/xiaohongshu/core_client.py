"""Credential-free Unix-socket client for the single MaiBot decision authority.

A missing or unsupportive core fails closed: the executor never falls back to a
second model, a second persona or locally invented text.
"""
import json, re, socket

MAX_REQUEST = 3 * 1024 * 1024 - 1024
MAX_RESPONSE = 2 * 1024 * 1024


class CoreUnavailable(Exception):
    pass


class CoreRejected(CoreUnavailable):
    """The core answered but refused (e.g. draft_queue_full). `reason` is a bounded identifier, never free text."""
    def __init__(self, reason):
        super().__init__('core_rejected')
        self.reason = reason


def socket_transport(path, timeout=110):
    def request(payload):
        wire = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(wire) > MAX_REQUEST:
            raise CoreUnavailable('request_too_large')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(path))
                client.sendall(wire)
                data = bytearray()
                while b'\n' not in data:
                    part = client.recv(min(65536, MAX_RESPONSE + 1 - len(data)))
                    if not part:
                        raise ValueError('incomplete')
                    data.extend(part)
                    if len(data) > MAX_RESPONSE:
                        raise ValueError('too_large')
            return json.loads(bytes(data).split(b'\n', 1)[0])
        except (OSError, ValueError):
            raise CoreUnavailable('core_unavailable') from None
    return request


class CoreClient:
    def __init__(self, path='/srv/maibot/unified/core.sock', transport=None):
        self.transport = transport or socket_transport(path)

    def _call(self, payload):
        try:
            answer = self.transport(payload)
        except CoreUnavailable:
            raise
        except Exception:
            raise CoreUnavailable('core_unavailable') from None
        if not isinstance(answer, dict) or answer.get('ok') is not True:
            reason = answer.get('error') if isinstance(answer, dict) else ''
            raise CoreRejected(reason if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,60}', reason) else 'core_rejected')
        return answer

    def plan(self, request):
        answer = self._call({'op': 'xhs_plan', **request})
        if not isinstance(answer.get('decision'), dict) or not isinstance(answer.get('persona_version'), (int, str)):
            raise CoreUnavailable('core_invalid_decision')
        return answer

    def event(self, event):
        return self._call({'op': 'xhs_event', 'event': event})

    def activity(self, action):
        return self._call({'op': 'xhs_activity', 'action': action})

    # ---- curiosity exploration: the core grants/limits opportunities; the executor searches and reads ----
    def explore_claim(self):
        task = self._call({'op': 'xhs_explore_claim'}).get('task')
        return task if isinstance(task, dict) and isinstance(task.get('query'), str) and isinstance(task.get('task_key'), str) else None

    def explore_report(self, task_key, outcome, searches, reads, content_ids):
        return self._call({'op': 'xhs_explore_report', 'task_key': task_key, 'outcome': outcome, 'searches': searches,
                           'reads': reads, 'content_ids': list(content_ids)[:2]})

    # ---- owner-review drafts: the executor can submit, claim an approved one, report. It cannot approve. ----
    def draft_submit(self, draft):
        answer = self._call({'op': 'xhs_draft_submit', 'draft': draft})
        result = answer.get('draft')
        if not isinstance(result, dict) or not isinstance(result.get('draft_id'), str) or not isinstance(result.get('version'), int):
            raise CoreUnavailable('core_invalid_draft')
        return result

    def draft_claim(self, kinds=None):
        draft = self._call({'op': 'xhs_draft_claim', 'kinds': list(kinds) if kinds is not None else ['post', 'comment', 'reply']}).get('draft')
        return draft if isinstance(draft, dict) else None

    def draft_report(self, draft_id, claim_token, outcome, detail=''):
        return self._call({'op': 'xhs_draft_report', 'draft_id': draft_id, 'claim_token': claim_token, 'outcome': outcome, 'detail': detail})

    def draft_status(self):
        answer = self._call({'op': 'xhs_draft_status'})
        return {'intro_done': answer.get('intro_done') is True, 'intro_active': answer.get('intro_active') is True,
                'can_submit': answer.get('can_submit') is True}

    def check_claim(self):
        return self._call({'op': 'xhs_draft_check_claim'}).get('check')

    def check_report(self, request, result):
        return self._call({'op': 'xhs_draft_check_report', 'request_id': request['request_id'], 'token': request['token'], 'result': result})
