"""Request-bound experience handoff using existing Host hooks, with no free-text planner draft."""
import copy
import json
import secrets
import time
import re
from . import experience_view as view, shareable_experience as sharing
from .experience_policy import enabled, prompt, version, digest


def event_for(service, session, message_id=''):
    last = service.last_message.get(session)
    event = service.state.get_event(last[1]) if last else None
    if not message_id:
        return event
    if event and message_id in (event.get('source_id'), event['event_id'], service.qq_display_message_id(event.get('source_id'))):
        return event
    gate = service.qq_reply_gate(session, message_id)
    native = gate['message_id'] if gate else message_id
    row = service.state.db.execute("SELECT payload FROM events WHERE platform='qq' AND conversation_id=? AND active=1 AND json_extract(payload,'$.source_id')=? ORDER BY occurred_at DESC LIMIT 1", (session, native)).fetchone()
    return json.loads(row[0]) if row else None


def anchor_key(session, actor):
    return 'experience_anchor:' + digest([session, actor])


def observe(service, event):
    if not enabled(service, 'recall') or event.get('origin') != 'external_user': return
    session, actor = event['conversation_id'], event['actor_id']
    old = service.state.get(anchor_key(session, actor), {})
    if not view.intent(event.get('content', ''), old):
        service.state.put(anchor_key(session, actor), {})


def snapshot(service, session, message_id='', token=''):
    if not enabled(service, 'recall') or session not in service.policy.get('allowed_qq_sessions', []): return None
    event = event_for(service, session, message_id)
    if not event or event.get('origin') != 'external_user' or event.get('active') is False: return None
    cache = getattr(service, '_experience_requests', None)
    if cache is None: service._experience_requests = cache = {}
    key = (session, event['event_id'])
    now = time.time()
    # Bound memory, including stale entries which must never authorize a send.
    while len(cache) > 64: cache.pop(next(iter(cache)))
    old = cache.get(key)
    if old:
        result = copy.deepcopy(old)
        if (now-result['created'] > 600 or (token and token != result['token']) or not view.valid(service, result)):
            result.update(status='unavailable', coverage='partial', items=[], invalid=True)
        return result
    anchor = service.state.get(anchor_key(session, event['actor_id']), {})
    quote = (event.get('metadata') or {}).get('reply_to')
    if quote:
        # Explicit replies may select another participant's publicly visible bot message.
        anchor = service.state.get('experience_sent:' + digest([session, quote]), anchor)
    if not anchor or now-anchor.get('at', 0) > 6*3600: anchor = None
    mode = view.intent(event.get('content', ''), anchor)
    if not mode and not token: return None
    result = view.query(service, event.get('content', ''), session=session,
                        now=min(now, float(event['occurred_at'])), anchor=anchor, limit=8 if mode=='inventory' else 5)
    result.update(token=secrets.token_hex(16), target=str(event.get('source_id') or event['event_id']),
                  event_id=event['event_id'], question_ref=sharing.reference(service,'event',event),
                  speaker=event['actor_id'], created=now)
    if token: result.update(status='unavailable', coverage='partial', items=[], invalid=True)
    cache[key] = copy.deepcopy(result)
    return result


def plan_output(service, session, items):
    if not enabled(service, 'recall'): return items
    output = []
    for item in items:
        call = item.get('tool_call', {}) if isinstance(item, dict) else {}
        if call.get('func_name') != 'reply':
            output.append(item); continue
        args = call.get('args') or {}
        if isinstance(args, dict) and set(args) == {'arguments'} and isinstance(args['arguments'], str):
            try: args = json.loads(args['arguments'])
            except ValueError: args = {}
        if not isinstance(args, dict): output.append(item); continue
        args = {k: v for k, v in args.items() if not k.startswith('_experience')}
        snap = snapshot(service, session, str(args.get('msg_id') or ''))
        if snap:
            # Non-empty trusted direction satisfies the existing Host schema, prevents reply_reason fallback.
            args['reply_reference'] = '按本轮self_experiences中的证据自然接话；未查到不代表没有发生。'
            args.pop('expression_intent', None)
            args['_experience_token'] = snap['token']
        output.append({**item, 'tool_call': {**call, 'args': args}})
    return output


def decorate(service, result, kwargs):
    if not enabled(service, 'recall') or kwargs.get('request_kind') == 'expression_selector': return result
    session = str(kwargs.get('session_id') or '')
    args = kwargs.get('reply_tool_args') or {}
    mid = str(kwargs.get('reply_message_id') or '')
    snap = snapshot(service, session, mid, args.get('_experience_token', ''))
    modified = result.get('modified_kwargs')
    if not isinstance(modified, dict): return result
    items = [i for i in modified.get('items', []) if not str((i.get('meta') or {}).get('item_id', '')).startswith('experience-context-')]
    if not snap:
        modified['items'] = items; return result
    # The actual Host supplies the original reference/reason in the hook. Remove that item,
    # including direct Host callers that did not traverse planner.after_response.
    rejected = {str(args.get('reply_reference') or '').strip(), str(kwargs.get('reply_reason') or '').strip()}
    rejected.discard('')
    items = [i for i in items if not (i.get('item_type') == 'UserMessageItem' and
        ''.join(str(p.get('text', '')) for p in i.get('parts', []) if p.get('type') == 'text').strip() in rejected)]
    text = prompt('experience_facts') + '\n' + prompt('natural_expression') + '\nself_experiences=' + json.dumps(view.model_view(snap), ensure_ascii=False)
    event = event_for(service, session, mid)
    if event: text += '\n当前对方问：' + event.get('content', '')[:600] + '\n直接接这句话；上面是可用材料，不是逐项复述的提纲。'
    items.append({'item_type': 'UserMessageItem', 'meta': {'item_id': 'experience-context-'+snap['token']},
                  'parts': [{'type': 'text', 'text': text}]})
    modified['items'] = items
    return result


def stage(service, session, mid, args, response):
    snap = snapshot(service, session, mid, (args or {}).get('_experience_token', ''))
    if not snap:
        prior = [v for (scope, _), v in getattr(service, '_experience_requests', {}).items()
                 if scope == session and mid in (v['target'], service.qq_display_message_id(v['target']))]
        return '' if (args or {}).get('_experience_token') or prior else response
    if snap.get('invalid') or not view.valid(service, snap): return ''
    refs = [r for item in snap['items'] for r in item['source_refs']]
    protected = sharing.validation_protections(service, refs)
    ids = [r.get('platform_uid') for i in snap['items'] for r in i.get('records', [])]
    clean = sharing.sanitize(response, service, ids=ids, protected=protected)
    stages = getattr(service, '_experience_stages', None)
    if stages is None: service._experience_stages = stages = {}
    stages[(session, snap['target'])] = {'snapshot': snap, 'text': clean, 'at': time.time()}
    while len(stages) > 64: stages.pop(next(iter(stages)))
    return clean


def send_stage(service, message, target=''):
    m, _, _, session = service.qq_fields(message)
    body = sent_text(service, m)
    target = str(target or m.get('reply_to') or '')
    for (scope, mid), entry in getattr(service, '_experience_stages', {}).items():
        if scope == session and (target == mid or target == service.qq_display_message_id(mid)) and body and body in entry['text']:
            return entry
    return None


def sent_text(service, message):
    parts = [p['data'] for p in message.get('raw_message', []) if isinstance(p,dict)
             and p.get('type')=='text' and isinstance(p.get('data'),str)]
    return ' '.join(parts) if parts else service.qq_text(message)


def before_send(service, message, target=''):
    entry = send_stage(service, message, target)
    if entry is None:
        _, _, _, session = service.qq_fields(message)
        # A bound experience turn may not bypass postprocessing through a different body.
        m = message if isinstance(message, dict) else {}
        target = str(target or m.get('reply_to') or '')
        staged = any(scope == session and target in (mid, service.qq_display_message_id(mid))
                     for scope, mid in getattr(service, '_experience_stages', {}))
        requested = any(scope == session and target in (v['target'], service.qq_display_message_id(v['target']))
                        for (scope, _), v in getattr(service, '_experience_requests', {}).items())
        return not (staged or requested)
    service.reload_policy()
    return (enabled(service, 'recall') and time.time()-entry['at'] < 600 and view.valid(service, entry['snapshot']))


def after_send(service, message, sent, target=''):
    if not sent: return
    entry = send_stage(service, message, target)
    if not entry: return
    snap = entry['snapshot']; m, _, _, session = service.qq_fields(message)
    text = sent_text(service, m)
    def match(item):
        title = re.sub(r'[\s《》【】“”「」]', '', item.get('title', ''))
        normalized = re.sub(r'[\s《》【】“”「」]', '', text)
        return len(title) >= 5 and (title in normalized or any(title[n:n+8] in normalized for n in range(len(title)-7)))
    discussed = [i for i in snap['items'] if match(i)]
    if snap['mode'] in ('more', 'later', 'detail') and not discussed: return
    prior = service.state.get(anchor_key(session, snap['speaker']), {})
    ids = list(dict.fromkeys(prior.get('discussed_ids', [])+[i['id'] for i in discussed]))[-30:]
    anchor = {'window': snap['window'], 'at': time.time(), 'discussed_ids': ids,
              'task_ids': [i['task_id'] for i in discussed if i.get('task_id')],
              'latest_evidence_at': max([i['at'] for i in discussed], default=0), 'bot_message_id': m.get('message_id')}
    service.state.put(anchor_key(session, snap['speaker']), anchor)
    service.state.put('experience_sent:' + digest([session, str(m.get('message_id') or '')]), anchor)
