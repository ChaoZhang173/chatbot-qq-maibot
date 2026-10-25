"""Time-limited activity opportunities, without targets or quota resets."""
from __future__ import annotations

from datetime import datetime
import copy
import math
import random

TEMP_LIMITS={'dynamic':20,'proactive':12,'reply':40}
TEMP_ENGAGEMENT={'like':20,'favorite':10,'follow':20,'coin':4}


def policy(config):
    raw=config.get('activity_boost')
    if raw is None:return None
    keys={'enabled','starts_at','ends_at','browse_min','browse_max','poll_seconds','dynamic_slots','limits','engagement_limits'}
    if not isinstance(raw,dict) or set(raw)!=keys or type(raw['enabled']) is not bool:
        raise ValueError('invalid_activity_boost')
    start,end=raw['starts_at'],raw['ends_at']
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or start<=0 or not 0<end-start<=604800:
        raise ValueError('boost_must_expire_within_seven_days')
    if (any(type(raw[k]) is not int for k in ('browse_min','browse_max','poll_seconds'))
            or raw['browse_min']!=900 or raw['browse_max']!=1800 or raw['poll_seconds']!=30):
        raise ValueError('boost_frequency_exceeds_authorization')
    if raw['dynamic_slots']!=['09:30','15:30','20:30']:
        raise ValueError('boost_dynamic_slots_not_authorized')
    if raw['limits']!=TEMP_LIMITS or raw['engagement_limits']!=TEMP_ENGAGEMENT:
        raise ValueError('boost_limits_exceed_authorization')
    return dict(raw)


def active(config,now):
    p=policy(config)
    return p if p and p['enabled'] and p['starts_at']<=now<p['ends_at'] else None


def poll_seconds(config,now):
    p=active(config,now)
    return min(config['poll_seconds'],p['poll_seconds']) if p else config['poll_seconds']


def effective(config,now):
    """Compute temporary limits without editing base config or daily counters."""
    p=active(config,now)
    if not p:return config
    result=copy.deepcopy(config)
    result['_activity_boost_active']=True
    for kind,value in TEMP_LIMITS.items():
        if result['limits'].get(kind,0)>0:result['limits'][kind]=value
    settings=result.get('engagement')
    if settings and settings.get('enabled'):
        settings['limits']={k:(TEMP_ENGAGEMENT[k] if v>0 else 0) for k,v in settings['limits'].items()}
    return result


def tick(engine,config,now):
    """Called only inside the normal writer loop and original active hours."""
    from .core import TZ, Denied
    p=active(config,now)
    if not p:return {'status':'expired'}
    revision=f"{p['starts_at']}:{p['ends_at']}"
    s=engine.s
    if s.get('boost_plan_revision')!=revision:
        s.put('boost_plan_revision',revision)
        s.put('boost_next_browse',min(p['ends_at'],now+random.randint(p['browse_min'],p['browse_max'])))
    results={}
    due=s.get('boost_next_browse')
    if due and due<=now and config['enabled']['proactive']:
        # Advance before any model or platform operation; no catch-up burst.
        s.put('boost_next_browse',min(p['ends_at'],now+random.randint(p['browse_min'],p['browse_max'])))
        try: results['browse']=browse(engine,config)
        except Denied as exc: results['browse']={'status':'skipped','reason':str(exc)}
    if not active(engine.config(),engine.now()):return {'status':'expired','results':results}
    current=datetime.fromtimestamp(now,TZ)
    for slot in p['dynamic_slots']:
        hour,minute=map(int,slot.split(':'))
        if current.hour!=hour or not minute<=current.minute<minute+10:continue
        token=f"{current.date().isoformat()}:{slot}"
        marker='boost_dynamic_slot:'+token
        if s.get(marker) or not config['enabled']['dynamic']:continue
        # A missed window is not replayed after downtime or next morning.
        s.put(marker,{'attempted_at':now,'revision':revision})
        try: results['dynamic']=engine.dynamic(slot=token)
        except Denied as exc: results['dynamic']={'status':'skipped','reason':str(exc)}
    return {'status':'active','ends_at':p['ends_at'],'results':results}


def browse(engine,config,autonomous=False):
    """Read interesting videos even after the separate comment quota is full.

    One shared decision per fresh video, at most two per browsing opportunity.
    Every actual action still passes its original writer/identity/budget guards.
    """
    from .core import Denied, InterestSkip, active as active_hours, day, machine_id
    s=engine.s
    now=engine.now()
    def enabled(current,at):return bool((autonomous and current.get('autonomy_enabled')) or active(current,at))
    if (getattr(s,'_lock_held',False) is not True or not enabled(config,now)
            or not active_hours(config,now) or config['paused'] or (s.root/'PAUSE').exists()
            or s.get('pause_reason') or not config['enabled']['proactive']):
        raise Denied('boost_browse_not_enabled')
    if (str(s.get('bound_uid'))!=str(config['bot_uid']) or config['writer_machine']!=machine_id()
            or not s.get('baseline_at')):raise Denied('boost_identity_or_baseline_required')
    engine.sender.identity(config)
    cycle=s.get('browse_cycle',0);s.put('browse_cycle',cycle+1)
    targets=engine.api.candidates(config,cycle)
    outcomes=[];decisions=0
    for candidate in targets[:config['max_candidates']]:
        if not candidate or candidate.kind!='video':continue
        now=engine.now()
        fresh=engine.config()
        if not enabled(fresh,now) or fresh!=config or s.get('pause_reason'):break
        if decisions>=2:break
        marker=f"boost_read:{day(now)}:{candidate.key}"
        if s.get(marker):continue
        # Persist before network/model; failed attempts are not repeatedly retried.
        s.put(marker,{'at':now,'status':'attempted'})
        target=engine.api.video(candidate.oid,with_subtitle=True)
        if target.kind!='video' or target.key!=candidate.key or str(target.author)==str(config['bot_uid']):continue
        key=f"{config['bot_uid']}:proactive:{target.key}"
        event=engine.event('proactive',key,target.text,target)
        event['event_id']+=':boost:'+day(now)+':'+target.evidence
        engine.outbox.event(event)
        allowed=True;reason=''
        try:s.permit(config,'proactive',target,now,key=key)
        except Denied as exc:allowed=False;reason=str(exc)
        model=engine.model();text=None;decisions+=1
        try:
            text=model.generate('proactive',{'text':target.text,'evidence':target.evidence},target.images,
                history=engine.history(),event=event,comment_allowed=allowed)
        except InterestSkip:pass
        except Denied as exc:
            outcomes.append({'target':target.key,'status':'skipped','reason':str(exc)})
            if str(exc) in ('model_daily_limit','model_cooldown'):break
            continue
        if not enabled(engine.config(),engine.now()):break
        result={'target':target.key,'status':'read','comment_allowed':allowed,'comment_skip_reason':reason}
        if model.last_decision:
            result['interest_decision']={k:model.last_decision.get(k) for k in ('interested','interest_score','engagement','engagement_reasons','reason')}
        if allowed and text is not None:
            try:result['comment']=engine.sender.deliver('proactive',target,key,lambda:text)
            except Denied as exc:result['comment']={'status':'skipped','reason':str(exc)}
        if engine.config().get('engagement',{}).get('enabled') and model.last_decision:
            try:
                result['engagement']=engine.engagement().execute(target,model.last_decision,event['event_id'],
                    persona_version=model.last_persona_version,source_ids=model.last_source_ids)
            except Denied as exc:result['engagement']=[{'status':'skipped','reason':str(exc)}]
        outcomes.append(result);engine.sync()
        if s.get('pause_reason') or (result.get('comment') or {}).get('status') in ('unknown','paused'):break
        if any(r.get('status')=='unknown' for r in result.get('engagement',[])):break
    engine.sync()
    return {'status':'browsed','model_decisions':decisions,'outcomes':outcomes}
