"""Direct, scoped event recall independent of daily learning and operational receipts."""
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import content_rules, shareable_experience as sharing
from .experience_policy import enabled, version, digest

TZ = ZoneInfo('Asia/Shanghai')
FOLLOW = {'还有呢': 'more', '再还有呢': 'more', '还有吗': 'more', '后来呢': 'later',
          '然后呢': 'later', '那个怎么样了': 'later', '研究得怎么样了': 'later'}


def intent(query, anchor=None):
    q = str(query or '').strip(' \n？?！!。~～')
    if q in FOLLOW:
        return FOLLOW[q] if anchor else ''
    if anchor and re.search(r'详细说说|讲了啥|有啥区别|更喜欢哪|就这些|讲讲细节', q):
        return 'more' if '就这些' in q else 'detail'
    if any(x in q for x in ('我该怎么回', '怎么回复他', '你会不会', '你能不能', '有没有想我')):
        return ''
    clean = re.sub(r'[“「"].*?[”」"]', '', q)
    if re.search(r'^(?:我|他|她|群里|我朋友|阿\S+)(?:今天|昨天|最近|这周|刚才)', clean):
        return ''
    subject = re.search(r'你|妈妈|大肥鱼|肥鱼', clean)
    question = re.search(r'看(?:到|过|了)|刷|干(?:嘛|啥|什么)|做(?:了|过)|折腾|忙什么|研究|好玩|日记|冷知识|弄懂没', clean)
    if question and (subject or re.match(r'^(?:今天|昨天|最近|这周|后来|之前)', clean)):
        return 'inventory' if re.search(r'盘点|列|所有|一个不落|都看', clean) else 'recent'
    return ''


def window(query, now):
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if '昨天' in query:
        return (day-timedelta(days=1)).timestamp(), day.timestamp()
    if '前天' in query:
        return (day-timedelta(days=2)).timestamp(), (day-timedelta(days=1)).timestamp()
    if '本周' in query or '这周' in query:
        return (day-timedelta(days=day.weekday())).timestamp(), now
    if re.search(r'近[七7]天|过去[七7]天', query):
        return now-7*86400, now
    if '今天' in query:
        return day.timestamp(), now
    if '前几天' in query: return now-5*86400, now
    return now-72*3600, now


def public_web_allowed(service, event, depth=0):
    if not enabled(service, 'web') or depth > 4 or event.get('active') is False:
        return False
    if event.get('is_private') or event.get('sensitive') or (event.get('metadata') or {}).get('is_private'):
        return False
    if event.get('platform') != 'web' or event.get('bot_account_id') != 'public-web' or event.get('sharing_scope') != 'public':
        return False
    task = event.get('exploration_task') or (event.get('metadata') or {}).get('exploration_task')
    try:
        row = service.state.db.execute('SELECT * FROM exploration_tasks WHERE task_key=?', (task,)).fetchone()
    except Exception:
        return False
    if not row or row['platform'] != 'web' or row['origin_kind'] not in ('bili_decide', 'bili_autonomy', 'xhs_plan', 'web_planner'):
        return False
    if not row['origin_event_id']:
        return row['origin_kind'] in ('bili_autonomy', 'xhs_plan', 'web_planner')
    source = service.state.get_event(row['origin_event_id'])
    if not source or source.get('active') is False or source.get('sharing_scope') != 'public' or source.get('is_private'):
        return False
    return (public_web_allowed(service, source, depth+1) if source.get('platform') == 'web'
            else service.state.allowed(source, 'bilibili', 'experience-public', 'public'))


def permitted(service, source, platform, session, purpose):
    if source.get('platform') == 'web':
        return public_web_allowed(service, source)
    return service.state.allowed(source, platform, session, purpose)


def read_candidates(service, start, end, platform='qq', session='', purpose='internal'):
    rows = service.state.db.execute("SELECT payload FROM events WHERE active=1 AND event_type='content_read' AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 2049", (start, end)).fetchall()
    items = []; seen = {}
    for row in rows[:2048]:
        e = json.loads(row[0])
        if not permitted(service, e, platform, session, purpose) or content_rules.blocked(e.get('content', '')):
            continue
        key = (e['platform'], e.get('source_id') or e.get('source_url') or e['event_id'])
        if key in seen:
            previous = seen[key]
            previous['read_times'].append(e['occurred_at'])
            previous['source_refs'].append(sharing.reference(service, 'event', e))
            continue
        text = sharing.sanitize(e.get('content', ''), service)
        task = e.get('exploration_task') or (e.get('metadata') or {}).get('exploration_task')
        item = {'id': e['event_id'], 'kind': 'read', 'platform': e['platform'], 'at': e['occurred_at'],
                'text': text[:650], 'title': text.split('\n')[0].split(' 简介')[0][:120],
                'material_evidence': e.get('material_evidence') or (e.get('metadata') or {}).get('material', 'text_excerpt'),
                'task_id': task, 'read_times': [e['occurred_at']], 'source_refs': [sharing.reference(service, 'event', e)],
                'notice': '只证明实际读取这些材料，不证明完整观看、学会或亲手做过。'}
        if task:
            row_task = service.state.db.execute('SELECT * FROM exploration_tasks WHERE task_key=?', (task,)).fetchone()
            # Only public, source-backed motives can leave their original context.
            from .exploration import Exploration
            if row_task and Exploration(service).public_origin(row_task['origin_event_id'], row_task['origin_kind']):
                item['recorded_motive'] = sharing.sanitize(row_task['purpose'], service)[:100]
                item['source_refs'].append(sharing.reference(service, 'task', dict(row_task)))
                if row_task['origin_event_id']:
                    item['source_refs'].append(sharing.reference(service, 'event', service.state.get_event(row_task['origin_event_id'])))
        seen[key] = item; items.append(item)
    return items, 'partial' if len(rows) > 2048 else 'full'


def action_candidates(service, start, end, platform='qq', session='', purpose='internal'):
    rows = service.state.db.execute("SELECT payload FROM activities WHERE status='succeeded' AND platform<>'qq' AND created_at>=? AND created_at<=? ORDER BY created_at DESC LIMIT 200", (start, end)).fetchall()
    result = []
    labels = {'like':'点赞', 'favorite':'收藏', 'coin':'投币', 'follow':'关注',
              'dynamic':'发布动态', 'reply':'回复', 'proactive':'评论', 'live':'直播文字聊天'}
    for row in rows:
        source = json.loads(row[0]); label = labels.get(source.get('kind'))
        if not label or source.get('receipt_verified') is not True or not permitted(service, source, platform, session, purpose): continue
        event = service.state.get_event(source.get('trigger_event_id')) if source.get('trigger_event_id') else None
        refs = [sharing.reference(service, 'action', source)]
        detail = source.get('content') or source.get('text') or ''
        if event and permitted(service, event, platform, session, purpose):
            detail = event.get('content') or detail
            refs.append(sharing.reference(service, 'event', event))
        if not detail or content_rules.blocked(detail): continue
        result.append({'id': source['action_id'], 'kind': 'action', 'action': source['kind'],
            'platform': source['platform'], 'at': source['created_at'], 'text': label+'：'+sharing.sanitize(detail, service)[:350],
            'title': sharing.sanitize(detail, service).split('\n')[0][:80], 'source_refs': refs,
            'notice':'只证明已确认的操作，不证明亲手实践或独立公开可见。发言正文仍仅证明说过。'})
    return result


def spread(items, limit):
    """Round robin across source tasks and time bands. Relatedness never implies causality."""
    groups = {}
    for item in sorted(items, key=lambda x: x['at'], reverse=True):
        # Time coverage comes first: many distinct evening task IDs must not evict the morning.
        key = int(item['at']//(4*3600))
        groups.setdefault(key, []).append(item)
    result = []
    while groups and len(result) < limit:
        for key in list(groups):
            result.append(groups[key].pop(0))
            if not groups[key]: del groups[key]
            if len(result) == limit: break
    return result


def query(service, query_text, *, session, platform='qq', now=None, anchor=None, limit=5):
    now = time.time() if now is None else now
    mode = intent(query_text, anchor)
    start, end = window(query_text, now)
    if mode in ('more', 'later', 'detail') and anchor:
        start, end = anchor['window']
        if mode == 'later': end = now
    result = {'status': 'unavailable', 'coverage': 'partial', 'query_attempted': False, 'details_loaded': True,
              'window': [start, end], 'as_of': now, 'mode': mode, 'items': [], 'has_more': False,
              'policy_version': version(service), 'session': session, 'platform': platform}
    if platform != 'qq' or session not in service.policy.get('allowed_qq_sessions', []):
        return result
    try:
        result['query_attempted'] = True
        candidates, coverage = read_candidates(service, start, end, platform, session)
        if re.search(r'干嘛|干了|做了|日记|收藏|点赞|盘点', query_text):
            candidates.extend(action_candidates(service, start, end, platform, session))
        if enabled(service, 'sharing') and re.search(r'聊天|聊了|趣事|干嘛|干了|做了', query_text):
            candidates.extend(sharing.collect(service, start, end, limit=8))
        if mode == 'more':
            if not anchor.get('discussed_ids'): return result
            candidates = [c for c in candidates if c['id'] not in anchor.get('discussed_ids', [])]
        elif mode == 'detail':
            candidates = [c for c in candidates if c['id'] in anchor.get('discussed_ids', [])]
        elif mode == 'later':
            tasks = anchor.get('task_ids', [])
            candidates = [c for c in candidates if c.get('task_id') in tasks and c['at'] > anchor.get('latest_evidence_at', 0)]
        # Complete DB scan is not proof of a complete life history.
        result.update(status='found' if candidates else 'no_match', coverage='partial', scan_complete=coverage=='full',
                      items=spread(candidates, limit), has_more=len(candidates) > limit)
    except Exception:
        # Nothing about an unavailable database licenses a claim that no activity happened.
        pass
    return result


def valid(service, snapshot):
    if snapshot.get('policy_version') != version(service):
        return False
    if snapshot.get('session') not in service.policy.get('allowed_qq_sessions', []):
        return False
    ref = snapshot.get('question_ref')
    if ref:
        question = sharing.resolve(service,ref)
        if not question or question.get('active') is False or digest(question)!=ref['digest']:return False
    for item in snapshot.get('items', []):
        if not sharing.valid_refs(service, item.get('source_refs', [])):
            return False
        if item['kind'] in ('read', 'action'):
            for ref in item['source_refs']:
                if ref['kind'] == 'task': continue
                if not permitted(service, sharing.resolve(service, ref), snapshot['platform'], snapshot['session'], 'internal'):
                    return False
    return True


def model_view(snapshot, budget=2600):
    out = {k: snapshot[k] for k in ('status', 'coverage', 'query_attempted', 'window', 'as_of', 'mode', 'has_more')}
    out['items'] = []
    out['window_local'] = [datetime.fromtimestamp(t,TZ).isoformat() for t in snapshot['window']]
    out['as_of_local'] = datetime.fromtimestamp(snapshot['as_of'],TZ).isoformat()
    for item in snapshot['items']:
        safe = {k: v for k, v in item.items() if k not in ('source_refs', 'policy_version')}
        safe['time'] = datetime.fromtimestamp(item['at'],TZ).strftime('%m-%d %H:%M')
        if 'records' in safe:
            safe['records'] = [{**r, 'text':r['text'][:200]} for r in safe['records'][-4:]]
        if len(json.dumps({**out, 'items': out['items']+[safe]}, ensure_ascii=False)) > budget:
            out['coverage'] = 'partial'; out['has_more'] = True
            continue
        out['items'].append(safe)
    if snapshot['items'] and not out['items']:out['status']='unavailable'
    return out
