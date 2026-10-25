"""One dated public diary, independent of chat and optional autonomy plans."""
from datetime import datetime
import json
import re
from . import quota
from .core import Denied, TZ, active, day


def policy(config):
    value=config.get('daily_diary',{'enabled':False,'time':'22:30','timezone':'Asia/Shanghai'})
    if (not isinstance(value,dict) or set(value)!={'enabled','time','timezone'}
            or type(value['enabled']) is not bool or value['timezone']!='Asia/Shanghai'
            or not isinstance(value['time'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value['time'])):
        raise ValueError('invalid_daily_diary_policy')
    return value


def action_key(config,now):return str(config['bot_uid'])+':dynamic:diary:'+day(now)


def reserved(store,config,now,kind):
    p=policy(config)
    if not p['enabled'] or not config['enabled'].get('dynamic'):return 0
    key=action_key(config,now)
    if store.db.execute('SELECT 1 FROM actions WHERE id=?',(key,)).fetchone():return 0
    state=store.get('daily_diary:'+day(now),{})
    if state.get('attempts',0)>=2:return 0
    hour,minute=map(int,p['time'].split(':'))
    due=datetime.fromtimestamp(now,TZ).replace(hour=hour,minute=minute,second=0,microsecond=0).timestamp()
    # Reserve in the window that will contain tonight's diary, or a same-day catch-up.
    if now<due and quota.window(config,now)!=quota.window(config,due):return 0
    return 1 if kind=='dynamic' else 3*(2-state.get('attempts',0))


def tick(engine):
    e=engine;s=e.s;c=e.config();now=e.now();p=policy(c)
    if not p['enabled']:return {'status':'disabled'}
    if not getattr(s,'_lock_held',False):raise Denied('diary_single_writer_required')
    date=day(now);key=action_key(c,now);marker='daily_diary:'+date
    done=s.db.execute('SELECT status,remote_id,url FROM actions WHERE id=?',(key,)).fetchone()
    if done:return {'status':'already_attempted','date':date,'delivery_status':done['status'],'url':done['url']}
    h,m=map(int,p['time'].split(':'))
    due=datetime.fromtimestamp(now,TZ).replace(hour=h,minute=m,second=0,microsecond=0).timestamp()
    if now<due:return {'status':'waiting','date':date,'due_at':due}
    state=s.get(marker,{})
    if state.get('attempts',0)>=2:return {'status':'generation_limit','date':date}
    if now<state.get('retry_at',0):return {'status':'retry_wait','date':date}
    # These checks precede model use; no deadline can bypass a pause or quota.
    s.permit(c,'dynamic',None,now,key=key)
    if not active(c,now):raise Denied('outside_active_hours')
    if c['model_daily']-quota.count(s.db,c,now,'usage')<3:return {'status':'model_budget','date':date}
    e.sender.identity(c)
    state={'date':date,'attempts':state.get('attempts',0)+1,'status':'generating','at':now,'retry_at':now+900}
    s.put(marker,state)
    try:
        from .mentions import registry,select
        candidates=registry(s,c,e.api,now)
        prepared=e.bounded_core({'op':'diary_episodes','date':date})
        draft=e.bounded_core({'op':'diary_draft','date':date,'mention_candidates':candidates,'episode_id':prepared.get('episode_id','')})
        review=e.bounded_core({'op':'diary_review','date':date,'draft_id':draft['draft_id']})
        if review.get('approved') is not True:raise Denied('diary_review_rejected')
        if day(e.now())!=date or e.config()!=c:raise Denied('diary_date_or_policy_changed')
        text=select(draft['text'],draft.get('mention_uids',[]),candidates,'dynamic',limit=850)
        event=e.event('dynamic',key,'每日真实经历日记：'+date)
        e.outbox.event(event)
        s.db.execute('INSERT OR REPLACE INTO action_context(action_id,event_json,persona_version,source_ids) VALUES(?,?,?,?)',
            (key,json.dumps(event,ensure_ascii=False),str(draft['persona_version']),json.dumps(draft['source_ids'])))
        result=e.sender.deliver('dynamic',None,key,lambda:text)
        state.update(status=result['status'],draft_id=draft['draft_id'],result=result)
        s.put(marker,state);e.sync()
        return {'date':date,**result}
    except Exception as exc:
        state.update(status='generation_failed',error=type(exc).__name__)
        s.put(marker,state)
        return {'status':'generation_failed','date':date,'reason':type(exc).__name__}
