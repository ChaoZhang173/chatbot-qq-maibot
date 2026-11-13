"""Opt-in diary material selection and sharing, keeping the deployed JSON protocol."""
import json
import re
import time
from datetime import datetime
from . import experience_view as view, shareable_experience as sharing, content_rules
from .experience_policy import version, prompt, digest, enabled
from .compact_context import persona


def day_window(date, now):
    from .diary import window
    return window(date, now)


async def prepare(service, request):
    now = time.time(); date = str(request.get('date', ''))
    start, end = day_window(date, now)
    slices = sharing.collect(service, start, end, 3)
    ident = digest([date, slices, version(service)])
    service.state.put('diary_episodes_v2:'+ident, {'created': now, 'date': date, 'slices': slices, 'policy_version': version(service)})
    return {'episode_id': ident, 'count': len(slices), 'policy_version': version(service)}


async def draft(service, request):
    from .diary import enabled as diary_enabled
    diary_enabled(service)
    now = time.time(); date = str(request.get('date', '')); start, end = day_window(date, now)
    reads, coverage = view.read_candidates(service, start, end, 'bilibili', 'diary:'+date, 'public')
    slices = sharing.collect(service, start, end, 2)
    actions = view.action_candidates(service, start, end, 'bilibili', 'diary:'+date, 'public')
    selected = view.spread(reads, 5) + view.spread(actions, 2) + slices
    facts = []; source_map = {}; all_refs = []; ids = []
    for i, item in enumerate(selected):
        fid = 'f'+str(i+1)
        fact = {k: v for k, v in item.items() if k not in ('source_refs', 'policy_version', 'id')}
        fact.update(id=fid, time=datetime.fromtimestamp(item['at'], view.TZ).strftime('%H:%M'))
        if 'text' in fact: fact['text'] = fact['text'][:420]
        if 'records' in fact:
            fact['records'] = [{**r, 'text': r['text'][:280]} for r in fact['records'][-4:]]
            ids.extend(r.get('platform_uid') for r in fact['records'])
            ids.append(fact.get('group_id'))
        facts.append(fact); source_map[fid] = item['source_refs']; all_refs.extend(item['source_refs'])
    actors = {str(source.get('actor_id', '')) for r in all_refs if r['kind'] == 'event'
              for source in [sharing.resolve(service, r) or {}] if source.get('platform') == 'bilibili'}
    raw_people = request.get('mention_candidates', [])
    if not isinstance(raw_people, list) or len(raw_people)>20: raise ValueError('invalid_diary_contacts')
    people = [p for p in raw_people if isinstance(p, dict) and
              isinstance(p.get('uid'), str) and re.fullmatch(r'[1-9]\d{0,19}', p['uid']) and p['uid'] in actors
              and p.get('source') in ('mutual', 'manual', 'public_context') and isinstance(p.get('name'), str)
              and 1<=len(p['name'])<=40 and sharing.sanitize(p['name'], service)==p['name']]
    state = service.state.persona_view()
    data = {'date': date, 'as_of': datetime.fromtimestamp(now, view.TZ).isoformat(), 'coverage': 'partial',
            'persona': persona(state.get('dynamic', {}), 1200), 'facts': facts, 'public_people': people}
    policy_stamp = version(service)
    async with service.busy:
        answer = await service.generate([{'role': 'system', 'content': prompt('diary_writer')},
                                        {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}], max_tokens=1800)
    if answer.get('willing') is False:
        return {'status': 'skipped', 'reason': 'no_worthwhile_topic', 'date': date, 'source_ids': []}
    if policy_stamp != version(service) or not sharing.valid_refs(service, all_refs):
        raise ValueError('diary_evidence_changed')
    text = answer.get('text'); used = answer.get('fact_ids')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 800: raise ValueError('diary_text_length')
    if not isinstance(used, list) or not used or any(not isinstance(i, str) or i not in source_map for i in used):
        raise ValueError('diary_evidence_required')
    text = sharing.sanitize(text, service, ids=ids, protected=sharing.validation_protections(service, all_refs))
    if content_rules.blocked(text) or '@' in text or '＠' in text or re.search(r'https?://', text):
        raise ValueError('diary_unsupported_or_unsafe_text')
    mentions = answer.get('mention_uids', [])
    mentions = [str(x) for x in mentions if str(x) in {p['uid'] for p in people}][:1] if isinstance(mentions, list) else []
    refs = list({r['id']: r for fid in used for r in source_map[fid]}.values())
    ident = digest([date, now, text, used, policy_stamp])
    result = {'draft_id': ident, 'date': date, 'text': text.strip(), 'mention_uids': mentions,
              'source_ids': [r['id'] for r in refs], 'persona_version': service.state.persona()['version'],
              'policy_version': policy_stamp, 'requires_final_validation': True}
    service.state.put('diary_draft:'+ident, {**result, 'created': now, 'facts': facts, 'people': people,
        'source_refs': all_refs, 'selected_refs': refs, 'allowed_platform_ids': ids, 'approved': False, 'style_version': 4})
    return result


def validate(service, request):
    date = str(request.get('date', '')); day_window(date, time.time())
    value = service.state.get('diary_draft:'+str(request.get('draft_id', '')), {})
    valid = (enabled(service, 'diary') and value.get('style_version') == 4 and value.get('date') == date
        and time.time()-value.get('created', 0) <= 900 and value.get('policy_version') == version(service)
        and sharing.valid_refs(service, value.get('source_refs', [])))
    if valid:
        safe = sharing.sanitize(value['text'], service, ids=value.get('allowed_platform_ids', []),
                                protected=sharing.validation_protections(service, value['source_refs']))
        valid = safe == value['text']
    return {'approved': bool(valid and value.get('approved') is True), 'valid': bool(valid)}


async def review(service, request):
    value = service.state.get('diary_draft:'+str(request.get('draft_id', '')), {})
    if not validate(service, request)['valid']: return {'approved': False, 'reason': '日记证据或策略已变化'}
    if value.get('reviewed'): return {'approved': value.get('approved') is True, 'reason': value.get('review_reason', '')}
    data = {k: value[k] for k in ('text', 'facts', 'people', 'mention_uids')}
    async with service.busy:
        answer = await service.generate([{'role': 'system', 'content': prompt('diary_reviewer')},
                                        {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}], max_tokens=500)
    approved = all(answer.get(k) is True for k in ('grounded', 'privacy_safe', 'mentions_relevant', 'qq_faithful', 'reflective'))
    approved = approved and validate(service, request)['valid']
    # Do not store an unchecked reviewer explanation which might repeat a secret.
    reason = sharing.sanitize(str(answer.get('reason', '')), service,
                               protected=sharing.validation_protections(service, value['source_refs']))[:200]
    value.update(reviewed=True, approved=approved, review_reason=reason)
    service.state.put('diary_draft:'+value['draft_id'], value)
    return {'approved': approved, 'reason': reason}
