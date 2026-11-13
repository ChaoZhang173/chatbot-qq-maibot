"""Native QQ destinations and bounded, receipt-backed voluntary conversation.

When she speaks on her own is decided in three ways, all inside hard limits enforced here (quiet request, quiet
hours, daily caps, a 30-minute floor between attempts, a short pause while the other person is typing):
  1. qq_agenda items she wrote herself: reminders the other person asked for (always delivered) and her own
     follow-ups (she decides again at that time whether to say anything);
  2. free time: after every decision she gets a randomised next look (45-150 min, or the interval she chose),
     stretched when her recent openers went unanswered;
  3. the model may always decline; declining is normal.
Every line she sends on her own is synced into the Host's live chat history, so a reply to it is understood."""
from __future__ import annotations
import asyncio, hashlib, json, os, random, re, time
from datetime import datetime, timedelta
from .store import CHINA
from .content_rules import RULE, blocked
from .persona import CONTEXT_RULE
from .voice_style import RULE as STYLE_RULE
from . import memory_time, capabilities, group_audience, experience_check
from .agenda import Agenda, REMINDER_LATE

PAUSE_DAYS=7
MIN_GAP=1800                  # hard floor between any two voluntary attempts in one conversation
FREE_MINUTES=(45,150)         # randomised free-time interval
HINT_MINUTES=(30,480)         # bounds for the interval she may choose herself
BACKOFF=1.6                   # per unanswered opener in a row (capped at 4)
MAX_INTERVAL=8*3600
FOLLOWUP_EXPIRE=12*3600
# A bare "什么冰淇淋？" right after her proactive line: the user does not recognise that topic.
QUESTION=re.compile(r'^\s*(?:什么|啥|哪个|哪来的|哪儿来的|哪里来的)\s*([一-鿿A-Za-z0-9]{1,8}?)\s*(?:啊|呀|呢|啦)?\s*[?？!！。.…~～]*\s*$')
GENERIC={'意思','东西','情况','玩意','玩意儿','时候','时间','事情','鬼东西','问题'}
# Observation only (not a filter): wording that asserts a shared history with the user.
SHARED_CLAIM=re.compile(r'说好了|说好的|约好|你答应|你昨天|你上次|你之前说')
BOUNDARY=('事实分层：①你自己的角色生活（你做了什么、吃了什么、喜欢什么）可以前后连贯地提，但它只是你的事，'
          '不能变成对方家里的东西、对方做过的事或你们的共同经历；②对方的现实（对方说过什么、做过什么、家里有什么）'
          '只能来自recent_messages里对方本人的原话，并按when说对时间，找不到原话就别断言，想知道就直接问；'
          '③你提过的提议或玩笑，对方明确答应之前不算约定，不能说成“说好了”“你答应过”。')
VOLUNTARY=('你是同一个大肥鱼。现在是你自己的空闲时间，看看此刻有没有真想跟对方说的话：刚刷到觉得好玩的东西、突然想起之前聊过的事、'
           '好奇对方在干嘛、想使唤对方或者就是想找人说说话，都可以。也完全可以不说——没什么新鲜念头、对方可能在忙、或者你此刻没那个心情，'
           '就willing=false，这很正常，不需要理由。\n'
           '想说的话像平时在QQ上随手发消息一样：想到哪句说哪句，1至2句干脆的大白话，不铺垫前因后果，不播报，'
           '不用“刚刷到……不过我这水平……”这种先抑后扬的套路，也不用“就这么愉快的决定啦~”这种做作腔调。'
           'own_recent_topics是你近期主动说的话和之后各人的发言，只用来避免重复，不是事实来源，也不保证后面的发言在回应你：这次换个不同的切入点和句式；'
           '对方回了问号、“什么X”、否认或没接的话题，说明对方不认，不要再追着提。私聊内容不带去群聊。'
           '输出JSON {"willing":true或false,"text":"最多280字","next_check_minutes":可选，你想隔多久再看看要不要找对方（30到480之间的整数）}。')
REMINDER=('你是大肥鱼。现在到了对方之前让你提醒的时间（reminder是要提醒的事，user_asked是对方当时的原话）。'
          '用你平时的口吻自然地提醒对方，1至2句，必须让对方一看就知道要做什么；不说教，不编造对方现在的状态。'
          '只输出JSON {"text":"提醒内容"}。')

def _mentions(item,terms):
    text=json.dumps(item,ensure_ascii=False,default=str)
    return any(t in text for t in terms)

def window(now):
    d=datetime.fromtimestamp(now,CHINA)
    if d.hour<9:return (d.date()-timedelta(days=1)).isoformat()+':night',True
    if d.hour>=23:return d.date().isoformat()+':night',True
    return d.date().isoformat()+':day',False

def settings(policy):
    c=policy.get('qq_native') or {}
    if c.get('enabled') is not True:return None
    if str(c.get('account'))!=str(policy.get('qq_bot_account')):raise ValueError('native_account_mismatch')
    ids=[c.get('account'),c.get('owner')]+list(c.get('groups') or [])
    if not 1<=len(c.get('groups') or [])<=2 or any(not isinstance(x,str) or not re.fullmatch('[1-9][0-9]{4,14}',x) for x in ids):
        raise ValueError('invalid_native_targets')
    return c

class ProactiveQQ:
    def __init__(self,service):
        self.s=service;self.db=service.state.db;self.lock=asyncio.Lock()
        self.db.execute('''CREATE TABLE IF NOT EXISTS qq_proactive_attempts(
          id TEXT PRIMARY KEY, session TEXT, window TEXT, created REAL, status TEXT,
          text TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '')''')
        self.agenda=Agenda(service)

    async def provision(self):
        self.s.reload_policy();c=settings(self.s.policy)
        if not c:raise ValueError('native_qq_disabled')
        destinations=[]
        for kind,target in [('group',x) for x in c['groups']]+[('private',c['owner'])]:
            result=await self.s.ctx.chat.open_session(platform='qq',chat_type=kind,account_id=c['account'],
                group_id=target if kind=='group' else '',user_id=target if kind=='private' else '')
            session=str(result.get('session_id') or result.get('stream_id') or '') if isinstance(result,dict) else ''
            if not isinstance(result,dict) or result.get('success') is not True or not session:raise ValueError('native_session_unavailable')
            destinations.append({'kind':kind,'target':target,'session':session})
        # Persist only identities returned by Host, never a guessed hash or nickname.
        self.s.reload_policy();p=dict(self.s.policy)
        if settings(p)!=c:raise ValueError('native_policy_changed')
        p['qq_native']={**c,'destinations':destinations}
        p['allowed_qq_sessions']=list(dict.fromkeys(p.get('allowed_qq_sessions',[])+[d['session'] for d in destinations]))
        for d in destinations:
            p.setdefault('qq_owner_bindings',{})[d['session']]=c['owner']
            if d['kind']=='group':p.setdefault('qq_group_sessions',{})[d['target']]=d['session']
        group=next(d['session'] for d in destinations if d['kind']=='group')
        private=next(d['session'] for d in destinations if d['kind']=='private')
        self.db.execute('INSERT INTO qq_private_bindings(private_id,group_session,group_actor,verified,session) VALUES(?,?,?,?,?) '
            'ON CONFLICT(private_id) DO UPDATE SET group_session=excluded.group_session,group_actor=excluded.group_actor,session=excluded.session',
            (c['owner'],group,c['owner'],time.time(),private))
        path=self.s.root/'policy.json';temp=path.with_suffix('.native.tmp')
        temp.write_text(json.dumps(p,ensure_ascii=False,indent=2));os.chmod(temp,0o600);os.replace(temp,path)
        self.s.state.put('qq_native_ready_at',time.time());self.s.reload_policy()
        return {'destinations':destinations}

    def destinations(self):
        c=settings(self.s.policy)
        if not c:return []
        out=[]
        for d in c.get('destinations',[]):
            if not isinstance(d,dict) or d.get('session') not in self.s.policy.get('allowed_qq_sessions',[]):continue
            if d.get('kind')=='group' and d.get('target') in c['groups'] and self.s.policy.get('qq_group_sessions',{}).get(d['target'])==d['session']:out.append(d)
            if d.get('kind')=='private' and d.get('target')==c['owner'] and self.s.commands.owner(d['session'],c['owner']):out.append(d)
        return out

    def latest_external(self,session):
        row=self.db.execute("SELECT max(received_at) FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND origin='external_user' AND active=1",(str(self.s.policy['qq_bot_account']),session)).fetchone()
        return float(row[0] or 0)

    def paused_terms(self,session,now=None):
        now=time.time() if now is None else now
        return [x['term'] for x in self.s.state.get('qq_proactive_paused:'+session,[]) or []
                if isinstance(x,dict) and x.get('term') and float(x.get('until',0))>now]

    def pause(self,session,term,reason,days=PAUSE_DAYS,now=None):
        now=time.time() if now is None else now
        keep=[x for x in self.s.state.get('qq_proactive_paused:'+session,[]) or []
              if isinstance(x,dict) and float(x.get('until',0))>now and x.get('term')!=term]
        keep.append({'term':term,'until':now+days*86400,'reason':reason,'created':now})
        self.s.state.put('qq_proactive_paused:'+session,keep[-20:])

    def observe(self,session,actor,text):
        if not any(d['session']==session for d in self.destinations()):return
        m=QUESTION.match(str(text or ''))
        if m and len(m.group(1))>=2 and m.group(1) not in GENERIC:
            last=self.db.execute("SELECT text,created FROM qq_proactive_attempts WHERE session=? AND status IN ('sending','sent','unknown') ORDER BY created DESC LIMIT 1",(session,)).fetchone()
            if last and time.time()-float(last['created'])<12*3600 and m.group(1) in (last['text'] or ''):
                self.pause(session,m.group(1),'对方追问“'+str(text)[:30]+'”')
        if re.search(r'(?:别|不要|停止|暂停).{0,5}(?:主动|打扰|发消息|找我)',text):
            self.s.state.put('qq_proactive_quiet:'+session,True)
        elif actor==str((self.s.policy.get('qq_native') or {}).get('owner')) and re.search(r'(?:恢复|可以|允许).{0,5}主动',text):
            self.s.state.put('qq_proactive_quiet:'+session,False)

    # ---- timing ------------------------------------------------------------------------------------------
    def quiet(self,session):
        return bool(self.s.state.get('qq_proactive_quiet:'+session,False))

    def caps(self,d,now):
        """(over the daily cap?, seconds since the last voluntary attempt or None)."""
        session=d['session'];w,night=window(now)
        send_cap=1 if night else 3 if d['kind']=='group' else 5
        check_cap=max(1 if night else 4,send_cap)
        checks=self.db.execute("SELECT count(*) FROM qq_proactive_attempts WHERE session=? AND window=?",(session,w)).fetchone()[0]
        sends=self.db.execute("SELECT count(*) FROM qq_proactive_attempts WHERE session=? AND window=? AND status IN ('sending','sent','unknown','failed')",(session,w)).fetchone()[0]
        last=self.db.execute('SELECT max(created) FROM qq_proactive_attempts WHERE session=?',(session,)).fetchone()[0]
        return checks>=check_cap or sends>=send_cap,(now-float(last)) if last else None

    def next_at(self,session):
        stored=self.s.state.get('qq_proactive_next:'+session)
        if isinstance(stored,dict) and isinstance(stored.get('at'),(int,float)):return float(stored['at'])
        last=self.db.execute('SELECT max(created) FROM qq_proactive_attempts WHERE session=?',(session,)).fetchone()[0]
        return float(last)+7200 if last else 0.0     # first run after upgrade keeps the old two-hour spacing once

    def unanswered_streak(self,session,limit=6):
        rows=[dict(r) for r in self.db.execute("SELECT id,text,created FROM qq_proactive_attempts WHERE session=? AND status IN ('sent','unknown') AND text<>'' ORDER BY created DESC LIMIT ?",(session,limit))]
        account=str(self.s.policy.get('qq_bot_account',''));streak=0;end=None
        group=group_audience.is_group(self.s,session)
        participants=group_audience.people(self.s,session,time.time()) if group else []
        for row in rows:
            start=float(row['created'])
            q="SELECT 1 FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND origin='external_user' AND active=1 AND occurred_at>?"
            args=[account,session,start]
            if group:
                target_ids=group_audience.targets(self.s,row,participants)
                if target_ids:
                    q+=' AND actor_id IN ('+','.join('?' for _ in target_ids)+')';args.extend(sorted(target_ids))
            q+=' AND occurred_at<=?';args.append(end if end is not None else time.time())
            if self.db.execute(q+' LIMIT 1',args).fetchone():break
            streak+=1;end=start
        return streak

    def schedule_next(self,session,now,hint=None):
        if type(hint) in (int,float) and HINT_MINUTES[0]<=hint<=HINT_MINUTES[1]:base,source=float(hint),'chosen'
        else:base,source=random.uniform(*FREE_MINUTES),'random'
        streak=self.unanswered_streak(session)
        seconds=min(base*60*(BACKOFF**min(streak,4)),MAX_INTERVAL)
        self.s.state.put('qq_proactive_next:'+session,{'at':now+seconds,'base_minutes':round(base),'source':source,'unanswered':streak,'set_at':now})
        return now+seconds

    def eligible(self,d,now):
        if d not in self.destinations() or self.quiet(d['session']):return False
        if now-self.s.state.get('qq_native_ready_at',now)<600:return False
        capped,since=self.caps(d,now)
        if capped or (since is not None and since<MIN_GAP):return False
        if now<self.next_at(d['session']):return False
        external=self.latest_external(d['session'])
        # Give an active exchange a short pause; the per-chat limits above still apply.
        if external and now-external<120:return False
        return True

    # ---- loop --------------------------------------------------------------------------------------------
    async def tick(self):
        self.s.reload_policy()
        c=settings(self.s.policy)
        if not c or c.get('proactive_enabled') is not True:return {'status':'disabled'}
        if self.lock.locked() or self.s.busy.locked():return {'status':'busy'}
        async with self.lock:
            now=time.time()
            # A delivery interrupted by a restart may already be on QQ: never resend it.
            self.db.execute("UPDATE qq_agenda SET status='unknown',updated=? WHERE status='sending' AND updated<?",(now,now-600))
            dests=self.destinations()
            item=self.agenda.due([d['session'] for d in dests],now)
            if item:
                d=next(x for x in dests if x['session']==item['session'])
                return await (self.remind(item,d,now) if item['kind']=='reminder' else self.follow_up(item,d,now))
            d=next((d for d in dests if self.eligible(d,now)),None)
            if not d:return {'status':'waiting'}
            return await self.converse(d,now)

    async def remind(self,item,d,now):
        """A reminder the other person asked for: delivered even if she would rather not talk; never resent."""
        if now-float(item['due'])>REMINDER_LATE:
            self.agenda.finish(item['id'],'expired',now,note='服务没在时间内送达，已过期');return {'status':'expired','item':item['id']}
        if not self.agenda.claim(item['id'],now):return {'status':'waiting'}
        text=''
        try:
            rows=self.recent_messages(d['session'],self.s.policy['qq_bot_account'],d['kind'],now,limit=6)
            async with self.s.busy:
                answer=await self.s.generate([{'role':'system','content':CONTEXT_RULE+'\n'+REMINDER+'\nnow：'+memory_time.now_line(now)+'。'+memory_time.RULE},
                    {'role':'user','content':json.dumps({'reminder':item['content'],'user_asked':item['evidence'],
                        'set_at':memory_time.relative(item['created'],now),'recent_messages':rows},ensure_ascii=False)}],max_tokens=200,temperature=0.7)
            text=str(answer.get('text') or '').strip()
        except Exception:
            text=''
        if not 1<=len(text)<=200 or blocked(text):
            text='提醒你一下：'+item['content']+('（你之前说：“'+item['evidence'][:60]+'”）' if item.get('evidence') else '')
        status,receipt=await self.deliver(d,text,kind='reminder',action_id='qq-reminder:'+item['id'])
        self.agenda.finish(item['id'],'delivered' if status=='sent' else 'unknown',time.time(),receipt)
        return {'status':status,'item':item['id']}

    async def follow_up(self,item,d,now):
        """Her own plan: at the planned time she decides again; the normal limits still apply."""
        session=d['session'];overdue=now-float(item['due'])     # due is never moved, so this counts from the plan
        if self.quiet(session):
            self.agenda.finish(item['id'],'skipped',now,note='对方说过别主动打扰');return {'status':'skipped','item':item['id']}
        if overdue>FOLLOWUP_EXPIRE:
            self.agenda.finish(item['id'],'expired',now,note='一直没找到合适的时机');return {'status':'expired','item':item['id']}
        capped,since=self.caps(d,now)
        external=self.latest_external(session)
        wait=None
        if external and now-external<120:wait=now+300
        elif since is not None and since<MIN_GAP:wait=now+MIN_GAP-since
        elif capped:wait=now+3600
        elif self.unanswered_streak(session)>=2 and now<self.next_at(session):wait=self.next_at(session)   # back-off holds
        if wait is not None:
            self.agenda.postpone(item['id'],wait,now);return {'status':'waiting'}
        if not self.agenda.claim(item['id'],now):return {'status':'waiting'}
        result=await self.converse(d,now,followup=item)
        final={'sent':'sent','unknown':'unknown'}.get(result.get('status'),'skipped')
        self.agenda.finish(item['id'],final,time.time(),note='' if final!='skipped' else '到点后决定先不说')
        return {**result,'item':item['id']}

    async def converse(self,d,now,followup=None):
        c=settings(self.s.policy)
        session=d['session'];w,_=window(now)
        key=hashlib.sha256((session+':'+str(now)).encode()).hexdigest()
        self.db.execute('INSERT INTO qq_proactive_attempts(id,session,window,created,status) VALUES(?,?,?,?,?)',(key,session,w,now,'reserved'))
        latest=self.latest_external(session)
        hint=None
        try:
            context=await self.s.build_context('qq_proactive','qq',session,'当前兴趣和最近经历',purpose='internal')
            context.pop('current_speaker',None)
            context.pop('relationship',None)
            context['daily_learning_items']=[{**x,'when':memory_time.relative(x.get('occurred_at'),now)} for x in self.s.daily.recent_items('qq',session,hours=24)]
            current=context.get('persona',{}).get('dynamic',{})
            for field in ('current_focus','current_state'):
                if field in current:context[field]=current[field]
            self.annotate_times(context,now)
            participants=group_audience.people(self.s,session,now) if d['kind']=='group' else None
            rows=self.recent_messages(session,c['account'],d['kind'],now,participants=participants)
            own_topics=self.own_topics(session,now,participants=participants)
            paused=self.paused_terms(session,now)
            if paused:
                # 'field', not 'key': the attempt id above must survive this loop.
                for field in ('facts','conversation_records','daily_learning_items','activities'):
                    if isinstance(context.get(field),list):context[field]=[x for x in context[field] if not _mentions(x,paused)]
                rows=[x for x in rows if not _mentions(x,paused)]
                own_topics=[x for x in own_topics if not _mentions(x,paused)]
            prompt=RULE+'\n'+CONTEXT_RULE+'\n'+VOLUNTARY
            prompt+='\n'+BOUNDARY+capabilities.text(self.s,session,'qq')+self.agenda.context(session,now)
            if participants is not None:prompt+='\n'+group_audience.RULE
            if followup:
                prompt+=('\n这次是你之前自己记下的打算（followup）：'+followup['content']+'（'+(memory_time.relative(followup['created'],now) or '')+
                         '记的）。现在想说就自然地说；时机不对、没兴致了或已经聊过了就willing=false。')
            prompt+='\nnow：'+memory_time.now_line()+'。'+memory_time.RULE
            async with self.s.busy:
                from .qq_voice import get as qq_voice
                if qq_voice(self.s).enabled(session):
                    prompt += ('你可以按自己的表达偏好、心情和内容，自主选择语音或文字，也可以连续用语音。'
                               '没有语音次数或间隔要求，不需要偏向文字。对方不方便听或要求文字时尊重对方。'
                               'JSON可增加delivery="voice"或"text"、mood="neutral"/"pleased"/"gentle"/"serious"/"puzzled"、voice_style="可选的简短整体语气描述，也可留空"。') + STYLE_RULE
                payload={'now':memory_time.now_line(now),'context':context,'recent_messages':rows,'own_recent_topics':own_topics,'destination':d['kind']}
                if participants is not None:payload['participants']=participants
                answer=await self.s.generate([{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False,default=str)}],max_tokens=520,temperature=0.85)
            hint=answer.get('next_check_minutes')
            text=answer.get('text','')
            if answer.get('willing') is not True or not isinstance(text,str) or not 1<=len(text.strip())<=280 or blocked(text):
                self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('declined',key));return {'status':'declined'}
            hit=[t for t in paused if t in text]
            if hit:
                self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('declined',key))
                self.watch('paused_topic_blocked',session,text,hit);return {'status':'declined','reason':'paused_topic'}
            audience=None
            if participants is not None:
                audience=group_audience.selection(self.s,session,answer,participants,time.time())
                if audience is None:
                    self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('declined',key))
                    self.watch('invalid_group_audience',session,text);return {'status':'declined','reason':'group_audience'}
                # Every group draft is reviewed, including unbound names that no regex can enumerate.
                async with self.s.busy:
                    checked=await experience_check.correct(self.s,text,{'status':'group_context','items':[]},
                        group_audience={'audience':audience,'participants':participants,'recent_messages':rows,
                                        'self_context':context},timeout=3.4)
                if not checked or not 1<=len(checked)<=280 or blocked(checked) or any(t in checked for t in paused):
                    self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('declined',key))
                    return {'status':'declined','reason':'group_evidence'}
                text=checked;answer={**answer,'text':text}
            if SHARED_CLAIM.search(text):self.watch('shared_claim',session,text)
            self.s.reload_policy()
            c2=settings(self.s.policy)
            if not c2 or c2.get('proactive_enabled') is not True or d not in self.destinations() or self.latest_external(session)!=latest or self.quiet(session):
                self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('cancelled',key));return {'status':'cancelled'}
            if participants is not None:
                audience=group_audience.selection(self.s,session,answer,participants,time.time())
                if audience is None:
                    self.db.execute('UPDATE qq_proactive_attempts SET status=? WHERE id=?',('cancelled',key));return {'status':'cancelled'}
                self.s.state.put(group_audience.target_key(key),audience)
            def still_allowed():
                return (self.latest_external(session)==latest and
                    (participants is None or group_audience.selection(self.s,session,answer,participants,time.time(),exclude_attempt=key) is not None))
            self.s.state.audit_context('qq_proactive','qq',session,context.get('source_ids',[]),context['persona']['version'],permissions=['scoped_proactive_qq'],model_task='replyer')
            return await self.send(key,d,text.strip(),delivery=answer.get('delivery'),mood=answer.get('mood'),voice_style=answer.get('voice_style'),still_allowed=still_allowed)
        except Exception:
            self.db.execute("UPDATE qq_proactive_attempts SET status='failed' WHERE id=? AND status='reserved'",(key,))
            return {'status':'failed'}
        finally:
            self.schedule_next(session,time.time(),hint)

    def annotate_times(self,context,now):
        """Relative wording for every dated item the proactive model sees; unknown stays unknown."""
        for f in context.get('facts') or []:
            if isinstance(f,dict):
                f['when']=memory_time.describe(memory_time.stamp(f.get('time')),memory_time.stamp(f.get('time_start')),memory_time.stamp(f.get('time_end')),now) or '时间未知'
        for r in context.get('conversation_records') or []:
            if isinstance(r,dict):r['when']=memory_time.relative(r.get('time'),now) or '时间未知'
        for a in context.get('activities') or []:
            if isinstance(a,dict):
                w=memory_time.relative(a.get('created') or a.get('created_at') or a.get('occurred_at'),now)
                if w:a['when']=w

    def recent_messages(self,session,account,kind,now,limit=12,participants=None):
        """Chronological messages keep their author and source; bot history is not evidence."""
        items=[]
        for row in self.db.execute("SELECT payload FROM events WHERE conversation_id=? AND platform='qq' AND bot_account_id=? AND active=1 AND occurred_at<=? ORDER BY occurred_at DESC,received_at DESC LIMIT ?",(session,account,now,limit)):
            e=json.loads(row['payload']);mine=e.get('origin')=='bot_self'
            who='我' if mine else ('对方' if kind=='private' else str(e.get('nickname') or '群友')[:40])
            text=str(e.get('content') or '')
            if mine and participants is not None:text=group_audience.hide_address(text,participants)
            items.append({'at':float(e.get('occurred_at') or 0),'who':who,'text':text,'user_id':str(e.get('actor_id') or ''),
                          'event_id':e['event_id'],'origin':e.get('origin'),'is_evidence':not mine})
        for row in self.db.execute("SELECT payload FROM activities WHERE platform='qq' AND conversation_id=? AND status='succeeded' AND created_at<=? ORDER BY created_at DESC LIMIT ?",(session,now,limit)):
            a=json.loads(row['payload'])
            if a.get('receipt_verified') is not True or a.get('kind') not in ('reply','proactive','reminder'):continue
            t,text=float(a.get('created') or a.get('created_at') or 0),str(a.get('content') or a.get('text') or '')
            if participants is not None:text=group_audience.hide_address(text,participants)
            if any(x['who']=='我' and x['text']==text and abs(x['at']-t)<120 for x in items):continue
            items.append({'at':t,'who':'我','text':text,'user_id':str(account),'event_id':a.get('action_id'),
                          'origin':'bot_self','is_evidence':False})
        items=[x for x in items if x['text'].strip()]
        items.sort(key=lambda x:x['at'],reverse=True)
        return [{**x,'when':memory_time.relative(x['at'],now) or '时间未知','text':x['text'][:200]} for x in reversed(items[:limit])]

    def own_topics(self,session,now,participants=None):
        """Prior openers are only deduplication hints; later messages keep their own authors."""
        group=group_audience.is_group(self.s,session)
        if group and participants is None:participants=group_audience.people(self.s,session,now)
        rows=[dict(r) for r in self.db.execute("SELECT id,text,created FROM qq_proactive_attempts WHERE session=? AND status IN ('sending','sent','unknown') AND text<>'' AND created<=? ORDER BY created DESC LIMIT ?",(session,now,3 if group else 5))]
        out=[]
        for i,r in enumerate(rows):
            start=float(r['created']);end=min(float(rows[i-1]['created']) if i else now,start+6*3600)
            replies=group_audience.later_messages(self.s,session,start,end,now=now)
            text=group_audience.hide_address(r['text'],participants) if group else r['text']
            item={'when':memory_time.relative(start,now),'i_said':text,'is_evidence':False,'later_messages':replies}
            if group:
                target_ids=group_audience.targets(self.s,r,participants)
                # An empty list asserts only that the target has not spoken, not why.
                item['target_user_ids']=sorted(target_ids)
                item['target_spoke_after']=bool(target_ids and self.db.execute(
                    "SELECT 1 FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? "
                    "AND origin='external_user' AND active=1 AND occurred_at>? AND occurred_at<=? AND actor_id IN ("+
                    ','.join('?' for _ in target_ids)+') LIMIT 1',
                    (str(self.s.policy['qq_bot_account']),session,start,end,*sorted(target_ids))).fetchone())
            out.append(item)
        return out

    def watch(self,kind,session,text,terms=()):
        """Bounded observation log for the Owner/maintainers; never sent anywhere."""
        log=self.s.state.get('qq_proactive_watch',{}) or {}
        log[kind]=int(log.get(kind,0))+1
        log['recent']=([*(log.get('recent') or []),{'kind':kind,'at':time.time(),'session':session,'text':text[:160],'terms':list(terms)}])[-30:]
        self.s.state.put('qq_proactive_watch',log)

    async def deliver(self,d,text,*,kind,action_id,delivery='text',mood='neutral',voice_style='',still_allowed=None):
        """One outbound line. Text goes through the Host send path WITH Maisaka history sync (as her own message),
        so when the other person answers, the reply model sees what she just said. Voice syncs in qq_voice."""
        try:
            from .qq_voice import get as qq_voice
            self.s.reload_policy()
            if still_allowed is not None:
                c=settings(self.s.policy)
                if not (c and c.get('proactive_enabled') is True and d in self.destinations()
                        and not self.quiet(d['session']) and still_allowed()):return 'cancelled',''
            if delivery=='voice' and qq_voice(self.s).enabled(d['session']):
                latest=self.latest_external(d['session'])
                def voice_allowed():
                    c=settings(self.s.policy)
                    return bool(c and c.get('proactive_enabled') is True and d in self.destinations()
                        and self.latest_external(d['session'])==latest and not self.quiet(d['session'])
                        and (still_allowed is None or still_allowed()))
                result=await qq_voice(self.s).deliver(d['session'],text,mood,voice_style=voice_style,still_allowed=voice_allowed)
                if result.get('status')=='cancelled':return 'cancelled',''
            else:
                result=await asyncio.wait_for(self.s.ctx.send.text(text,d['session'],return_details=True,
                    sync_to_maisaka_history=True,maisaka_source_kind='guided_reply'),20)
            status='sent' if isinstance(result,dict) and result.get('sent') is True else 'unknown'
            receipt=str((result or {}).get('message_id') or '') if isinstance(result,dict) else ''
        except Exception:status='unknown';receipt=''
        self.s.state.record_activity({'action_id':action_id,'platform':'qq','bot_account_id':self.s.policy['qq_bot_account'],
            'conversation_id':d['session'],'kind':kind,'status':'succeeded' if status=='sent' else 'unknown',
            'content':text,'created':time.time(),'remote_id':receipt,'receipt_verified':status=='sent',
            'sharing_scope':'conversation' if d['kind']=='private' else 'policy','is_private':d['kind']=='private'})
        return status,receipt

    async def send(self,key,d,text,delivery='text',mood='neutral',voice_style='',still_allowed=None):
        if d not in self.destinations():raise ValueError('proactive_destination_not_allowed')
        self.db.execute("UPDATE qq_proactive_attempts SET status='sending',text=? WHERE id=?",(text,key))
        status,receipt=await self.deliver(d,text,kind='proactive',action_id='qq-proactive:'+key,delivery=delivery,mood=mood,voice_style=voice_style,still_allowed=still_allowed)
        if status=='cancelled':
            self.db.execute("UPDATE qq_proactive_attempts SET status='cancelled' WHERE id=?",(key,))
            return {'status':'cancelled'}
        self.db.execute('UPDATE qq_proactive_attempts SET status=?,receipt=? WHERE id=?',(status,receipt[:200],key))
        return {'status':status,'session':d['session']}

    async def connection_test(self):
        self.s.reload_policy();results=[]
        async with self.lock:
            for d in self.destinations():
                key=hashlib.sha256(('connection-test:'+self.s.policy['qq_bot_account']+':'+d['session']).encode()).hexdigest()
                old=self.db.execute('SELECT status FROM qq_proactive_attempts WHERE id=?',(key,)).fetchone()
                if old:results.append({'session':d['session'],'status':old['status'],'repeated':False});continue
                now=time.time();w,_=window(now)
                self.db.execute('INSERT INTO qq_proactive_attempts(id,session,window,created,status) VALUES(?,?,?,?,?)',(key,d['session'],w,now,'reserved'))
                results.append(await self.send(key,d,'大肥鱼的新QQ已经接通啦，先在这里试运行。'))
        return {'results':results}

    async def loop(self):
        while True:
            try:await self.tick()
            except Exception:self.s.ctx.logger.warning('QQ主动聊天暂不可用')
            await asyncio.sleep(60)
