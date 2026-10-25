"""Explicit per-action choices, with durable reasons even on invalid output."""
import json,time,math
KEYS={'like','favorite','coin','follow_author'}
PROMPT=('本次必须逐项决定互动，JSON必须包含engagement:{"like":false,"coin":0,"favorite":false,"follow_author":false}'
        '和engagement_reasons:{"like":"具体原因","coin":"具体原因","favorite":"具体原因","follow_author":"具体原因"}。'
        '四项均不可省略，每个原因1至120字，做与不做都说明。coin为整数0或1，其余为布尔值。'
        '点赞表示对当前具体内容的喜欢或共鸣，不要求有话可评论；投币表示认为当前作品值得支持；'
        '收藏看以后回看价值；关注看持续关注作者的实际依据。各项独立判断，可全不做。'
        '不能因评论暂不可发就放弃判断互动，也不能为用完额度而操作。'
        '任何互动仍要求interested=true、interest_score至少65；没有兴趣要明确写不做及原因。')

def checked(decision,grant=None):
    actions=decision.get('engagement');reasons=decision.get('engagement_reasons')
    if not isinstance(actions,dict) or set(actions)!=KEYS:raise ValueError('engagement_choices_required')
    if not isinstance(reasons,dict) or set(reasons)!=KEYS:raise ValueError('engagement_reasons_required')
    if any(not isinstance(v,str) or not 1<=len(v.strip())<=120 for v in reasons.values()):raise ValueError('invalid_engagement_reason')
    if any(type(actions[k]) is not bool for k in KEYS-{'coin'}) or type(actions['coin']) is not int or actions['coin'] not in (0,1):raise ValueError('invalid_engagement_decision')
    score=decision.get('interest_score')
    if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=100:raise ValueError('invalid_interest_score')

    if any(actions.values()):
        if decision.get('interested') is not True or score<65:raise ValueError('engagement_interest_required')
        if grant:
            for key,budget in [('like','like'),('coin','coin'),('favorite','favorite'),('follow_author','follow')]:
                if actions[key] and grant['remaining'][budget]<1:raise ValueError('engagement_budget_exhausted')

def record(service,event,decision,permissions,validation='valid'):
    db=service.state.db
    db.execute('CREATE TABLE IF NOT EXISTS engagement_decisions(id INTEGER PRIMARY KEY,created REAL,event_id TEXT,target TEXT,payload TEXT)')
    payload={k:decision.get(k) for k in ('interested','interest_score','reason','engagement','engagement_reasons')}
    payload.update(validation=validation,permissions=permissions,persona_version=service.state.persona().get('version'))
    cur=db.execute('INSERT INTO engagement_decisions(created,event_id,target,payload) VALUES(?,?,?,?)',
        (time.time(),event.get('event_id'),event.get('conversation_id'),json.dumps(payload,ensure_ascii=False,default=str)))
    return cur.lastrowid
