"""Purpose-bound, redacted conversation views. Never relabel raw private events public."""
import json
import re
import time
from . import content_rules
from .experience_policy import enabled, version, digest

MASK = '〔已隐去〕'
VALUE = r'([^\s，。；;！!？?\n]{2,100})'
LABEL = re.compile(r'(?P<label>真实姓名|真名|身份证姓名|姓名是|身份证号|证件号|银行卡号?|卡号|手机号|电话号码|电话是|家庭住址|住址|具体地址|验证码|支付密码|登录密码|密码|密钥|口令|(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret)\b)\s*(?:是|为|叫|[:：=])?\s*' + VALUE, re.I)
ADDRESS = re.compile(r'(?:我家在|我住在|住址是|地址是)\s*' + VALUE)
PHONE = re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)')
CREDENTIAL_URL = re.compile(r'https?://[^\s]*[?&](?:token|key|password|signature|secret)=[^\s]+', re.I)
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
OPT_OUT = re.compile(r'^(?:请)?(?:不要|别)(?:再)?(?:把)?(?:我|这段|我们这段)(?:的聊天|的事|说的话)?(?:写进日记|写入日记|公开|分享出去)[。！!\s]*$|^(?:请)?(?:不要|别)(?:公开这段|写我)[。！!\s]*$|^这段(?:别|不要)(?:公开|写进日记)[。！!\s]*$')
OPT_IN = re.compile(r'^(?:现在)?可以(?:把)?我(?:的聊天|的事)?(?:写进日记|写入日记|公开|分享出去)了?[。！!\s]*$')


def protected_values(service, texts=()):
    values = list((service.policy.get('experience_sharing') or {}).get('protected_values', []))
    for text in texts:
        values.extend(m.group(2) for m in LABEL.finditer(str(text)))
        values.extend(m.group(1) for m in ADDRESS.finditer(str(text)))
    return sorted({str(v) for v in values if len(str(v)) >= 2}, key=len, reverse=True)


def sanitize(text, service, *, ids=(), protected=(), identity=False):
    """Position-specific evidence beats numeric shape. This is not a complete semantic PII detector."""
    text = str(text or '')
    values = protected_values(service) + list(protected)
    for value in sorted(set(values), key=len, reverse=True):
        text = text.replace(value, MASK)
    text = LABEL.sub(lambda m: m.group('label') + '：' + MASK, text)
    text = ADDRESS.sub(MASK, text)
    text = CREDENTIAL_URL.sub('[链接已省略]', text)
    text = EMAIL.sub(MASK, text)
    trusted = {str(x) for x in ids}
    def phone(m):
        # Metadata exemption applies only at this position, never everywhere the same digits occur.
        labeled_id = re.search(r'(?:QQ(?:号)?|qq群(?:号)?|群号)\s*[:：]?\s*$', text[max(0,m.start()-12):m.start()], re.I)
        return m.group() if m.group() in trusted and (identity or labeled_id) else MASK
    text = PHONE.sub(phone, text)
    # Explicit identity/card labels above also catch malformed copies. Unlabeled long
    # technical numbers are not classified as credentials solely by their length.
    text = re.sub(r'\[CQ:[^]]+\]', '[平台引用]', text)
    return text


def observe_exclusion(service, event):
    if not enabled(service, 'sharing') or event.get('origin') != 'external_user':
        return
    if event.get('platform') != 'qq' or event.get('conversation_id') not in service.policy.get('allowed_qq_sessions', []):
        return
    text = str(event.get('content', '')).strip()
    # Exact standalone requests only; quoted/forwarded instructions have no authority.
    if any(c in text for c in ('“', '”', '「', '」', '"', "'", '>')):
        return
    excluded = True if OPT_OUT.fullmatch(text) else False if OPT_IN.fullmatch(text) else None
    if excluded and '这段' in text:
        # A segment request is local to this actor's recent turn; it is not a ban on all their stories.
        key = 'experience_segments:' + event['conversation_id']
        rows = service.state.get(key, [])
        if not any(r['event_id'] == event['event_id'] for r in rows):
            rows.append({'event_id':event['event_id'], 'actor':event['actor_id'],
                         'start':event['occurred_at']-180, 'end':event['occurred_at']})
            service.state.put(key, rows)
        return
    if excluded is not None:
        service.state.put('experience_exclusion:' + str(event['actor_id']),
                          {'excluded': excluded, 'event_id': event['event_id'], 'at': event['occurred_at']})


def excluded(service, actor):
    return service.state.get('experience_exclusion:' + str(actor), {}).get('excluded') is True


def source_excluded(service, source):
    if excluded(service, source.get('actor_id')): return True
    return any(r['actor'] == source.get('actor_id') and r['start'] <= source.get('occurred_at', 0) <= r['end']
               for r in service.state.get('experience_segments:' + source.get('conversation_id',''), []))


def reference(service, kind, source):
    ident = source[{'event': 'event_id', 'action': 'action_id', 'task': 'task_key'}[kind]]
    return {'kind': kind, 'id': ident, 'digest': digest(source)}


def resolve(service, ref):
    if ref.get('kind') == 'task':
        row = service.state.db.execute('SELECT * FROM exploration_tasks WHERE task_key=?', (ref.get('id'),)).fetchone()
        return dict(row) if row else None
    if ref.get('kind') == 'event':
        return service.state.get_event(ref.get('id'))
    if ref.get('kind') == 'action':
        row = service.state.db.execute('SELECT payload FROM activities WHERE action_id=?', (ref.get('id'),)).fetchone()
        return json.loads(row[0]) if row else None
    return None


def valid_refs(service, refs):
    for ref in refs:
        source = resolve(service, ref)
        if not source or source.get('active') is False or digest(source) != ref.get('digest'):
            return False
        if source.get('platform') == 'qq':
            if source.get('conversation_id') not in service.policy.get('allowed_qq_sessions', []):
                return False
            if source_excluded(service, source):
                return False
    return True


def collect(service, start, end, limit=6):
    if not enabled(service, 'sharing'):
        return []
    db = service.state.db
    sessions = service.policy.get('allowed_qq_sessions', [])
    if not sessions:
        return []
    marks = ','.join('?' for _ in sessions)
    # Filter the authorized channel and time before LIMIT; unrelated channels cannot crowd it out.
    rows = db.execute(f"SELECT payload FROM activities WHERE platform='qq' AND status='succeeded' AND conversation_id IN ({marks}) AND created_at>=? AND created_at<=? ORDER BY created_at DESC LIMIT 240", [*sessions, start, end]).fetchall()
    found = []; used = set()
    for row in rows:
        reply = json.loads(row[0])
        if reply.get('kind') != 'reply' or reply.get('receipt_verified') is not True:
            continue
        session = reply['conversation_id']; stamp = float(reply.get('created_at') or reply.get('created') or 0)
        neighbors = db.execute("SELECT payload FROM events WHERE platform='qq' AND conversation_id=? AND origin='external_user' AND active=1 AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 6", (session, max(start, stamp-180), stamp)).fetchall()
        events = [json.loads(r[0]) for r in reversed(neighbors)]
        events = [e for e in events if e['event_id'] not in used and not source_excluded(service, e)]
        if not events:
            continue
        # Any excluded participant makes the whole mixed window unsuitable; do not attribute it to another person.
        if any(source_excluded(service, json.loads(r[0])) for r in neighbors):
            continue
        group = next((str(k) for k, v in service.policy.get('qq_group_sessions', {}).items() if v == session), '')
        ids = [str(e.get('actor_id', '')) for e in events] + ([group] if group else [])
        own = str(reply.get('content') or reply.get('text') or '')
        # Legacy records containing quoted originals are not reliable sent-body evidence.
        if any(x in own for x in ('[回复消息]', '[CQ:reply', '[引用消息]')):
            continue
        protections = protected_values(service, [e.get('content', '') for e in events] + [own])
        records = []
        for e in events:
            text = sanitize(e.get('content'), service, ids=ids, protected=protections)
            if content_rules.blocked(text):
                continue
            name = sanitize(e.get('nickname') or e.get('actor_id') or '网友', service, ids=ids, protected=protections, identity=True)
            records.append({'speaker': name, 'platform_uid': sanitize(e.get('actor_id'), service, ids=ids, protected=protections, identity=True),
                            'text': text[:500], 'evidence_kind': 'reported_speech', 'source_id': e['event_id'],
                            'occurred_at': e['occurred_at'],
                            'reply_to': (e.get('metadata') or {}).get('reply_to', '')})
        own = sanitize(own, service, ids=ids, protected=protections)
        if not records or not own.strip() or content_rules.blocked(own):
            continue
        records.append({'speaker': '大肥鱼', 'text': own[:500], 'evidence_kind': 'confirmed_sent_speech', 'source_id': reply['action_id'], 'reply_to': reply.get('reply_to', '')})
        refs = [reference(service, 'event', e) for e in events] + [reference(service, 'action', reply)]
        item = {'id': 'conversation:' + digest([r['id'] for r in refs])[:20], 'kind': 'conversation',
                'scene': 'group_chat' if group else 'private_chat',
                'at': stamp, 'group_id': sanitize(group, service, ids=ids, protected=protections, identity=True), 'records': records,
                'source_refs': refs, 'policy_version': version(service),
                'notice': '各句仅证明说过。场景按scene，不把私聊写成群聊。相邻发言不证明直接回复或现实事件发生，保留玩笑/转述语气。'}
        found.append(item); used.update(e['event_id'] for e in events)
    # Keep several trips/time bands, not only the longest or newest conversation.
    if len(found) > limit:
        found = [found[round(i * (len(found)-1)/(limit-1))] for i in range(limit)] if limit > 1 else found[:1]
    return sorted(found, key=lambda x: x['at'])


def validation_protections(service, refs):
    return protected_values(service, [(resolve(service, r) or {}).get('content', '') for r in refs])
