"""Bounded event-aware wakeups; the persona decides the activity and next wait."""
import hashlib,time
from datetime import datetime
from . import quota
from .core import Denied,active,day,machine_id
from .account import AccountSettings
from . import boost

class Autonomy:
    def __init__(self,engine):self.e=engine
    def tick(self):
        e=self.e;s=e.s;c=e.config();now=e.now()
        if not c.get('autonomy_enabled'):return {'status':'disabled'}
        if not getattr(s,'_lock_held',False):raise Denied('single_writer_required')
        if c['paused'] or (s.root/'PAUSE').exists() or s.get('pause_reason') or not active(c,now):return {'status':'paused_or_quiet'}
        if str(s.get('bound_uid'))!=str(c['bot_uid']) or c['writer_machine']!=machine_id() or not s.get('baseline_at'):raise Denied('autonomy_identity_required')
        accounts=AccountSettings(e)
        if c.get('account_settings_enabled') and now-s.get('account_snapshot_at',0)>=3600:
            s.put('account_snapshot_at',now);snapshot={};unavailable=[]
            for section in ('profile','space','folder','messages'):
                try:snapshot[section]=accounts.snapshot(section)
                except Exception:unavailable.append(section)
            snapshot['unavailable']=unavailable
            e.unified_request({'op':'account_snapshot','snapshot':snapshot})
        signal=e.unified_request({'op':'autonomy_state'}).get('signal') or {}
        last=s.get('autonomy_last_at',0);next_at=s.get('autonomy_next_at',0)
        event_due=signal.get('reason')=='public_interaction' and signal.get('id')!=s.get('autonomy_signal') and 0<=now-signal.get('at',0)<=1800 and now-last>=600
        if now<next_at and not event_due:return {'status':'waiting','next_at':next_at}
        used=s.get(quota.autonomy_key(c,now),0)
        if used>=c.get('autonomy_period_limit',30):return {'status':'decision_daily_limit'}
        model_used=quota.count(s.db,c,now,'usage')
        from .diary import reserved
        if c['model_daily']-model_used-reserved(s,c,now,'model')<3:return {'status':'model_budget_reserved_for_replies'}
        # Reserve this consideration before network/model. Crashes do not reroll.
        cycle=hashlib.sha256((str(c['bot_uid'])+':'+str(now)+':'+str(used)).encode()).hexdigest()
        s.put('autonomy_last_at',now);s.put('autonomy_next_at',now+3600);s.put('autonomy_signal',signal.get('id'))
        s.put(quota.autonomy_key(c,now),used+1)
        # Bilibili exploration has its own clock. QQ chat cannot spend wakeups
        # or make an old video feel as though it was just read.
        last_browse=s.get('autonomy_last_browse_at',0)
        anchor=max(last_browse,quota.window(c,now)[0],s.get('baseline_at',now))
        idle_minutes=max(0,int((now-anchor)/60))
        wait_streak=s.get('autonomy_wait_streak',0)
        exploration={'now_beijing':datetime.fromtimestamp(now,quota.TZ).isoformat(),
            'minutes_since_autonomous_browse':idle_minutes,'consecutive_waits':wait_streak,
            'browse_due':idle_minutes>=60,'last_browse_at':last_browse or None}
        options=['wait'];remaining={}
        if c['enabled']['proactive']:options.append('browse')
        if c['enabled']['dynamic']:
            remaining['dynamic']=max(0,c['limits']['dynamic']-s.used(str(c['bot_uid']),'dynamic',now,c))
            if remaining['dynamic']:options.append('dynamic')
        from .live import policy
        live=e.live_chat();p=policy(c)
        if p['enabled'] and not s.get('live_visit') and now>=max(s.get('live_backoff_until',0),s.get('live_next_visit',0) or 0):
            if quota.count(s.db,c,now,'live_visits')<p['visits_daily'] and s.used(str(c['bot_uid']),'live',now,c)<p['daily_limit']:options.append('live')
        if c.get('account_settings_enabled'):
            last_change=accounts.db.execute('SELECT max(created) FROM account_changes').fetchone()[0] or 0
            remaining['settings']=c.get('account_period_limit',3)-quota.count(accounts.db,c,now,'account_changes')
            if remaining['settings']>0 and now-last_change>=3600:options.append('settings')
        answer=e.bounded_core({'op':'autonomy_plan','cycle':cycle,'available':options,'remaining':remaining,'exploration':exploration,'trigger':signal.get('reason','idle') if event_due else 'self_chosen_wakeup'})
        d=answer.get('decision',{});action=d.get('action')
        if action not in options or type(d.get('wait_minutes')) is not int or not 15<=d['wait_minutes']<=180:raise Denied('invalid_autonomy_plan')
        planned_action=action
        # A bounded discovery opportunity prevents endless chat-dependent rest.
        # Actual engagement still needs the normal independent interest decision.
        fallback=(action=='wait' and 'browse' in options and idle_minutes>=60 and wait_streak>=1)
        if fallback:action='browse'
        s.put('autonomy_wait_streak',wait_streak+1 if action=='wait' else 0)
        # User's one-week browsing boost remains an opportunity frequency, never forced posting.
        wait=min(d['wait_minutes'],30) if boost.active(c,now) else d['wait_minutes']
        if action=='wait' and 'browse' in options:wait=min(wait,max(15,60-idle_minutes))
        s.put('autonomy_next_at',now+wait*60)
        if e.config()!=c:raise Denied('policy_changed')
        result={'status':'considered','action':action,'reason':d.get('reason',''),'next_at':now+wait*60,'persona_version':answer.get('persona_version')}
        result.update(planned_action=planned_action,exploration=exploration)
        if fallback:result.update(reason='连续等待且一小时未自主浏览，先发现新视频，再按兴趣决定是否互动',selection='idle_browse_opportunity')
        s.put('last_autonomy',{'at':now,'cycle':cycle,**result})
        try:
            if action=='browse':
                s.put('autonomy_last_browse_at',now)
                result['result']=boost.browse(e,c,autonomous=True)
            elif action=='dynamic':result['result']=e.dynamic(slot='autonomy:'+cycle)
            elif action=='live':
                live.guard(c,p);e.sender.identity(c);result['result']=live.start(c,p)
            elif action=='settings':result['result']={'status':'queued','request_id':d.get('request_id')}
        except Denied as exc:result['result']={'status':'skipped','reason':str(exc)}
        s.put('last_autonomy',{'at':now,'cycle':cycle,**result});return result
