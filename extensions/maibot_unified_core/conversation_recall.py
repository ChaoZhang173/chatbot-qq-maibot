"""Recall stored conversations without waiting for semantic summarisation.

Group sharing is an explicit deployment policy. Evidence keeps its original
channel, time and delivery status; retrieval never manufactures a sent message.
"""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
RULE = ('回忆优先核对conversation_records里的原始记录及时间、渠道。device_voice是Passport语音，'
        '与QQ私聊属于同一段对话；qq_group是群聊。只有speaker=我且evidence=sent或reply_recorded'
        '的记录能证明自己回复过。检索无匹配或暂不可用只表示这次没查到，不能据此断言没有聊过。')


def sessions(service, session):
    allowed = set(service.policy.get('allowed_qq_sessions', []))
    if session not in allowed:
        return []
    result = {session}
    groups = service.policy.get('shared_groups', [])
    if groups and all(isinstance(g, str) for g in groups):
        groups = [groups]
    for group in groups:
        if isinstance(group, dict):
            group = group.get('sessions', group.get('session_ids', []))
        if isinstance(group, (list, tuple)) and session in group:
            result.update(group)
    if service.policy.get('qq_group_recall_shared_enabled') is True:
        result.update(service.policy.get('qq_group_sessions', {}).values())
    return [session, *sorted((result & allowed) - {session})]


def _time_window(query):
    now = datetime.now(TZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if '昨天' in query:
        return (day - timedelta(days=1)).timestamp(), day.timestamp()
    if '今天' in query:
        return day.timestamp(), (day + timedelta(days=1)).timestamp()
    return None, None


def _terms(query):
    # Remove recall phrasing before matching topics; this is a local fallback,
    # not a replacement for the Host's semantic memory search.
    text = re.sub(r'你还记得|你记不记得|记不记得|记得|还记得|刚才|刚刚|之前|昨天|今天|最近|'
                  r'群聊|群里|群中|私聊|语音|设备|聊过|聊天|说过|说了|什么|怎么|没有|事情|'
                  r'回忆|经历|我们|你们|是不是|有没有|passport|qq', ' ', query.lower())
    terms = set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}', text))
    return {part for term in terms for part in ([term] if len(term) <= 4 or term.isascii()
                                               else [term[i:i+2] for i in range(len(term)-1)])}


def records(service, platform, session, query='', purpose='internal', limit=6):
    if platform not in ('qq', 'passport') or purpose != 'internal':
        return []
    # A Passport-native person sees only their own session: no shared groups, no QQ delivery receipts.
    chats = sessions(service, session) if platform == 'qq' else (
        [session] if service.session_platform(session) == 'passport' else [])
    if not chats:
        return []
    query = str(query or '')[:400]
    group_query = any(word in query for word in ('群', 'group'))
    voice_query = any(word in query.lower() for word in ('passport', '语音', '设备')) and not group_query
    recall_query = group_query or any(word in query for word in
        ('记得', '回忆', '聊', '刚才', '刚刚', '之前', '昨天', '今天', '语音', '设备', 'passport'))
    start, end = _time_window(query)
    terms = _terms(query)
    groups = set(service.policy.get('qq_group_sessions', {}).values())
    candidates = []
    account = str(service.policy.get('qq_bot_account', ''))
    current_event = service.last_message.get(session, ('', None, None))[1]

    def add(item, source, channel, speaker, stamp, evidence, source_id):
        if not service.state.allowed(source, platform, session, purpose):
            return
        if str(source.get('bot_account_id', '')) != account:
            return
        if start is not None and not start <= stamp < end:
            return
        text = str(item or '').strip()
        if not text:
            return
        matches = sum(term in text.lower() for term in terms)
        candidates.append((matches, stamp, {'text': text[:600], 'source_id': source_id,
            'channel': channel, 'speaker': speaker, 'time': datetime.fromtimestamp(stamp, TZ).isoformat(timespec='seconds'),
            'evidence': evidence, 'source_conversation': source['conversation_id']}))

    for chat in chats:
        if group_query and chat not in groups:
            continue
        # Filter by channel BEFORE LIMIT, so busy QQ traffic cannot evict voice history.
        if chat == session and chat not in groups:
            with service.state._lock:
                rows = service.state.db.execute(
                    "SELECT payload FROM events WHERE platform=? AND conversation_id=? AND active=1 "
                    "AND event_type IN ('device_voice_turn','device_voice_reply') "
                    "ORDER BY occurred_at DESC,rowid DESC LIMIT 120", (platform, chat)).fetchall()
            for row in rows:
                e = json.loads(row['payload'])
                if e['event_id'] == current_event:
                    continue
                mine = e.get('origin') == 'bot_self'
                add(e.get('content'), e, 'device_voice', '我' if mine else ('主人' if platform == 'qq' and not (e.get('metadata') or {}).get('profile_id') else '用户'), e['occurred_at'],
                    'reply_recorded' if mine else 'received', e['event_id'])
        # For explicit recollection include confirmed QQ replies. Incoming messages
        # alone cannot establish that the bot participated in a conversation.
        if recall_query and not voice_query and platform == 'qq':
            with service.state._lock:
                rows = service.state.db.execute(
                    "SELECT payload FROM activities WHERE platform='qq' AND conversation_id=? "
                    "AND status='succeeded' ORDER BY created_at DESC,action_id DESC LIMIT 120", (chat,)).fetchall()
            for row in rows:
                a = json.loads(row['payload'])
                if a.get('receipt_verified') is not True or a.get('kind') != 'reply':
                    continue
                add(a.get('content', a.get('text')), a, 'qq_group' if chat in groups else 'qq_private',
                    '我', float(a.get('created') or a.get('created_at') or 0), 'sent', a['action_id'])
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in candidates[:limit]]


def compact(rows, now=None):
    from . import memory_time
    return [{**{k: (str(row[k])[:260] if k == 'text' else row[k])
                for k in ('text', 'source_id', 'channel', 'speaker', 'time', 'evidence')},
             'when': memory_time.relative(row.get('time'), now) or '时间未知'}
            for row in rows[:4]]
