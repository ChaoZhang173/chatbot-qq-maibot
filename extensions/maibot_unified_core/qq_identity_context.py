"""Keep platform speaker identity separate from shared conversation memories."""
import re
from . import memory_time

RULE = ('认人以当前平台消息的账号身份为准：current_speaker明确记录当前说话者的user_id、person_id和昵称。'
        '昵称只是显示名称，不能据同名合并人物。群聊共同记忆可以回忆，但其中的“用户”、别人的名字、'
        '哥哥等关系称呼不代表当前说话者；只有同一person_id的人物证据才能用于确认其身份。'
        '自己过去喊过的名字也不是身份证据。认不准时使用当前昵称或说明不确定，不猜成另一位群友。')


def self_identity_query(query):
    return bool(re.search(r'我是谁|我叫(?:什么|啥)|我的(?:名字|昵称|称呼)|叫我(?:什么|啥)|'
                          r'(?:认得|认出|认不认得|认不认识)我|'
                          r'who\s+am\s+i|what(?:\s+is|\s*[’\']s)\s+my\s+name',
                          str(query or ''), re.I))


def current(service, session):
    """Use an ingested event and Host person ID; never infer from names or prose."""
    last = service.last_message.get(session)
    if not last or not last[1] or not last[2]:
        return {}
    event = service.state.get_event(last[1])
    account = str(service.policy.get('qq_bot_account', ''))
    if (not event or event.get('platform') != 'qq'
            or str(event.get('bot_account_id')) != account
            or event.get('conversation_id') != session
            or event.get('origin') != 'external_user'
            or str(event.get('actor_id')) != str(last[2])
            or str(last[2]) == account or not event.get('person_id')):
        return {}
    return {'user_id': str(event['actor_id']), 'person_id': str(event['person_id']),
            'nickname': str(event.get('nickname') or '')[:120],
            'source_event': event['event_id']}


def evidence_people(hit):
    meta = hit.get('metadata') if isinstance(hit.get('metadata'), dict) else {}
    people = meta.get('person_ids') if isinstance(meta.get('person_ids'), list) else []
    people = {x for x in people if isinstance(x, str) and x}
    if isinstance(meta.get('person_id'), str) and meta['person_id']:
        people.add(meta['person_id'])
    source = str(hit.get('source') or '')
    if source.startswith('person_fact:'):
        people.add(source.split(':', 1)[1])
    return sorted(people)


def identity_evidence(hit, speaker):
    meta = hit.get('metadata') if isinstance(hit.get('metadata'), dict) else {}
    personal = meta.get('source_type') == 'person_fact' or str(hit.get('source') or '').startswith('person_fact:')
    return bool(personal and speaker.get('person_id')
                and evidence_people(hit) == [speaker['person_id']])


def fact(hit, source, key):
    point, start, end = memory_time.hit_time(hit)
    meta = hit.get('metadata') if isinstance(hit.get('metadata'), dict) else {}
    out = {'text': str(hit.get('content', ''))[:500], 'source_id': key,
           'source_conversation': source.get('conversation_id'),
           'person_ids': evidence_people(hit),
           # Absolute Beijing time; relative wording is computed at render time.
           'time': memory_time.iso(point)}
    if not point and (start or end):
        out['time_start'], out['time_end'] = memory_time.iso(start), memory_time.iso(end)
    if meta.get('origin') == 'bot_self':
        out['speaker'] = '我'
    return out
