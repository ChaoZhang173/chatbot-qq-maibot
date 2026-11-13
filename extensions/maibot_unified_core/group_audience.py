"""Bind voluntary group address to an actual author, never to a bot opener."""
import json
import re
from . import memory_time

RECENT_SECONDS = 30 * 60
PERSONAL_STATE = re.compile(r'还(?:不|没)睡|怎么还(?:在|没)|你(?:现在|还在|刚才|上午|昨天|上次|之前|是不是).{0,16}(?:睡|听|吃|忙|上班|耳机)|你听上了没')
RULE = ('这是群聊的主动发言，没有当前提问者，不把最后发言者当默认对象。可以面向全群分享自己的有据发现，也可以不说。'
        'participants保留账号、昵称、最近本人原话时间和可否点名；最近说过话不等于现在在线、醒着或有空。'
        '只有addressable=true的人才可选择为本次对象；不得把甲的话和处境接到乙身上。'
        'own_recent_topics只供避重复，later_messages有独立作者，并不保证是在回复你。自己的旧发言不证明其中的事真实发生。'
        '无近期本人依据，不问“怎么还没睡”“上午听完没”等预设其状态的问题。过去发过几条没回应，不继续点同一个人。'
        '可以自然提昵称，不能从无回应推断忙、睡觉或别的状态。JSON增加audience_uid和audience_event_id；'
        '面向全群时两者都为空，点名时只能原样使用同一个addressable参与者的user_id和event_id。')


def is_group(service, session):
    return session in (service.policy.get('qq_group_sessions') or {}).values()


def target_key(attempt):
    return 'qq_proactive_target:' + attempt


def addressed(text, participants):
    """Recognise direct address; a third-person name within a story is not a target."""
    found = set()
    for p in participants:
        name = p['nickname']
        if not name or name in ('我', '你', '大家', '群友', '网友'):
            continue
        if (re.search(r'(?:^|[，,。！？\n])\s*' + re.escape(name) + r'(?:[，,:：！!?？\s]|你|怎么|还没|还不)', text)
                or '@' + name in text or '@' + p['user_id'] in text
                or re.search(r'\[CQ:at,qq=' + re.escape(p['user_id']) + r'(?:,|\])', text)):
            found.add(p['user_id'])
    return found


def targets(service, attempt, participants):
    """Read new bindings; infer only explicit names for attempts from before upgrade."""
    stored = service.state.get(target_key(attempt['id']))
    if isinstance(stored, dict):
        return {stored['user_id']} if stored.get('user_id') else set()
    return addressed(attempt.get('text', ''), participants)


def unanswered_target(service, session, actor, last_at, participants, now, exclude_attempt=None):
    rows = service.state.db.execute(
        "SELECT id,text FROM qq_proactive_attempts WHERE session=? AND status IN ('sent','sending','unknown') "
        "AND created>=? AND created<=? ORDER BY created DESC LIMIT 12", (session, last_at, now)).fetchall()
    return any(r['id'] != exclude_attempt and actor in targets(service, dict(r), participants) for r in rows)


def people(service, session, now, exclude_attempt=None):
    rows = service.state.db.execute("""SELECT payload FROM (
        SELECT payload, occurred_at, event_id, ROW_NUMBER() OVER (
            PARTITION BY actor_id ORDER BY occurred_at DESC,received_at DESC,event_id DESC) AS rank
        FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND active=1
        AND origin='external_user' AND occurred_at<=? AND occurred_at>=?) WHERE rank=1
        ORDER BY occurred_at DESC,event_id DESC LIMIT 80""",
        (str(service.policy['qq_bot_account']), session, now, now - 30 * 86400)).fetchall()
    result = []
    for row in rows:
        event = json.loads(row[0]); actor = str(event['actor_id'])
        if actor == str(service.policy['qq_bot_account']):
            continue
        result.append({'user_id': actor, 'nickname': str(event.get('nickname') or actor)[:80],
            'event_id': event['event_id'], 'at': event['occurred_at'],
            'when': memory_time.relative(event['occurred_at'], now), 'text': event.get('content', '')[:300]})
    for person in result:
        waiting = unanswered_target(service, session, person['user_id'], person['at'], result, now, exclude_attempt)
        person['addressable'] = 0 <= now - person['at'] <= RECENT_SECONDS and not waiting
        person['waiting_for_this_author'] = waiting
    return result


def selection(service, session, answer, participants, now, exclude_attempt=None):
    uid = answer.get('audience_uid', ''); eid = answer.get('audience_event_id', '')
    if not isinstance(uid, str) or not isinstance(eid, str):
        return None
    hits = addressed(str(answer.get('text', '')), participants)
    if not uid:
        return {} if not eid and not hits and not PERSONAL_STATE.search(str(answer.get('text', ''))) else None
    person = next((p for p in participants if p['user_id'] == uid and p['event_id'] == eid and p['addressable']), None)
    if not person or hits - {uid}:
        return None
    # Expiry, retraction and a new opener all invalidate an earlier model choice.
    current = next((p for p in people(service, session, now, exclude_attempt) if p['user_id'] == uid), None)
    if not current or not current['addressable'] or current['event_id'] != eid:
        return None
    return {'user_id': uid, 'event_id': eid, 'nickname': person['nickname'], 'at': person['at']}


def hide_address(text, participants):
    """Remove repeated vocatives from bot history, preserving the topic for deduplication."""
    for person in participants:
        name = person['nickname']
        if not name or name in ('我', '你', '大家', '群友', '网友'):
            continue
        text = re.sub(r'(^|[。！？\n])\s*' + re.escape(name) + r'[，,:：\s]+', r'\1', text)
        text = text.replace('@' + name, '[点名]').replace('@' + person['user_id'], '[点名]')
        text = re.sub(r'\[CQ:at,qq=' + re.escape(person['user_id']) + r'(?:,[^\]]*)?\]', '[点名]', text)
    return text


def later_messages(service, session, start, end, limit=3, now=None):
    rows = service.state.db.execute("SELECT payload FROM events WHERE platform='qq' AND bot_account_id=? "
        "AND conversation_id=? AND origin='external_user' AND active=1 AND occurred_at>? AND occurred_at<=? "
        "ORDER BY occurred_at,event_id LIMIT ?",
        (str(service.policy['qq_bot_account']), session, start, end, limit)).fetchall()
    return [{'user_id': e['actor_id'], 'nickname': e.get('nickname') or '群友', 'event_id': e['event_id'],
             'at': e['occurred_at'], 'when': memory_time.relative(e['occurred_at'], end if now is None else now), 'text': e.get('content', '')[:180]}
            for row in rows for e in [json.loads(row[0])]]
