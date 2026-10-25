"""Occasional text-only live chat, inside the existing single-writer process."""
import hashlib,json,random,re,time
from datetime import datetime
from . import quota
from .core import Denied,PlatformError,UnknownDelivery,TZ,active,day,machine_id
from .content_rules import blocked,public_text,is_plain_live_text

DEFAULT={'enabled':False,'daily_limit':100,'room_daily_limit':40,'stay_min':600,'stay_max':3600,
         'ordinary_probability':0.85,'message_gap':60,'poll_seconds':15,'visits_daily':4,
         'interval_min':3600,'interval_max':10800}
STOP=re.compile(r'别刷屏|不要刷屏|停止(?:发言|聊天|回复)|别(?:再)?(?:回复|说话)|机器人.*(?:走开|出去)|stop\s+(?:chat|reply)',re.I)
LOW_INFORMATION=re.compile(r'福袋|抽奖|抽我|求中奖|中个奖|红包口令|老板大气|老板糊涂')

def policy(config):
    raw=config.get('live_chat',DEFAULT)
    if not isinstance(raw,dict) or set(raw)!=set(DEFAULT) or type(raw['enabled']) is not bool:raise ValueError('invalid_live_policy')
    for key in set(DEFAULT)-{'enabled','ordinary_probability'}:
        if type(raw[key]) is not int:raise ValueError('invalid_live_policy')
    if not (0<=raw['daily_limit']<=100 and 0<=raw['room_daily_limit']<=40 and
        600<=raw['stay_min']<=raw['stay_max']<=3600 and raw['ordinary_probability']==0.85 and
        raw['message_gap']>=60 and 15<=raw['poll_seconds']<=60 and 1<=raw['visits_daily']<=4 and
        3600<=raw['interval_min']<=raw['interval_max']<=10800):raise ValueError('live_policy_exceeds_authorization')
    return dict(raw)

def token(room,row):
    return hashlib.sha256(json.dumps([str(room),row.get('timeline'),str(row.get('uid')),row.get('text')],ensure_ascii=False).encode()).hexdigest()

def clean_room(row):
    room=str(row.get('roomid') or row.get('room_id') or '')
    uid=str(row.get('uid') or '')
    if not re.fullmatch('[1-9][0-9]{0,15}',room) or not uid.isdigit():raise ValueError('invalid_live_room')
    title=re.sub('<[^>]+>','',str(row.get('title','')))[:200]
    area=str(row.get('area_name') or row.get('parent_area_name') or row.get('parent_name') or '')[:100]
    return {'room_id':room,'uid':uid,'title':title,'area':area}

class LiveAPI:
    def __init__(self,api):self.api=api
    def candidates(self,parent):
        self.api.ensure_device()
        params={'platform':'web','parent_area_id':parent,'area_id':0,'sort_type':'online','page':1,'page_size':20}
        data=self.api.request('/room/v3/area/getRoomList',params=params,live=True)
        rooms=[]
        for row in (data.get('list') or [])[:30]:
            try:rooms.append(clean_room(row))
            except (ValueError,TypeError):continue
        return rooms
    def room(self,room_id):
        data=self.api.request('/xlive/web-room/v1/index/getInfoByRoom',params={'room_id':room_id},live=True)
        info=data.get('room_info') or {};room=clean_room(info)
        room['online']=info.get('live_status')==1
        room['description']=re.sub('<[^>]+>','',str(info.get('description','')))[:600]
        room['silent']=info.get('hidden_status') in (1,'1') or info.get('lock_status') in (1,'1')
        # These policies are siblings of room_info, not fields inside it. A
        # restricted room is skipped rather than guessing the account is exempt.
        silent=data.get('silent_room_info') or {};block=data.get('block_info') or {}
        room['restriction']=''
        if not isinstance(silent,dict) or not isinstance(block,dict):
            room['restriction']='room_permission_unverified'
        elif block.get('block') in (True,1,'1'):
            room['restriction']='room_access_blocked'
        elif silent.get('type') not in (None,'',0,'0'):
            room['restriction']='room_silence_policy'
        room['silent']=room['silent'] or bool(room['restriction'])
        return room
    def messages(self,room_id):
        data=self.api.request('/xlive/web-room/v1/dM/gethistory',params={'roomid':room_id},live=True)
        rows=[]
        for raw in (data.get('room') or [])[-30:]:
            if not isinstance(raw,dict) or not isinstance(raw.get('text'),str):continue
            try:stamp=datetime.strptime(raw['timeline'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ).timestamp()
            except (KeyError,ValueError,TypeError):continue
            text=raw['text'].strip()[:500]
            if not text:continue
            rows.append({'uid':str(raw.get('uid','0')),'text':text,'timeline':raw['timeline'],'time':stamp})
        return rows
    def send(self,room_id,text):
        text=public_text(text)
        if len(text)>20:raise Denied('live_message_too_long')
        if not is_plain_live_text(text):raise Denied('live_plain_text_only')
        self.api.ensure_device();csrf=self.api.credentials['BILI_JCT']
        data={'roomid':room_id,'msg':text,'rnd':int(time.time()),'color':16777215,'fontsize':25,'mode':1,'bubble':0,'csrf':csrf,'csrf_token':csrf}
        result=self.api.request('/msg/send',data=data,live=True)
        if result.get('mode_info') and not isinstance(result['mode_info'],dict):raise UnknownDelivery('invalid_live_receipt')
        return result

class LiveChat:
    def __init__(self,engine,api=None,rng=random):
        self.e=engine;self.s=engine.s;self.api=api or LiveAPI(engine.api);self.rng=rng
        self.s.db.executescript('''CREATE TABLE IF NOT EXISTS live_seen(id TEXT PRIMARY KEY,room TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS live_visits(id TEXT PRIMARY KEY,room TEXT,day TEXT,started REAL,ends REAL,status TEXT);''')

    def guard(self,c,p):
        if not getattr(self.s,'_lock_held',False):raise Denied('live_single_writer_required')
        if not p['enabled'] or not p['daily_limit'] or not p['room_daily_limit']:raise Denied('live_disabled')
        if c['paused'] or (self.s.root/'PAUSE').exists() or self.s.get('pause_reason'):raise Denied('paused')
        if not active(c,self.e.now()):raise Denied('outside_active_hours')
        if str(c['bot_uid'])!=str(self.s.get('bound_uid')) or c['writer_machine']!=machine_id() or not self.s.get('baseline_at'):raise Denied('live_identity_required')
        if self.s.used(str(c['bot_uid']),'live',self.e.now(),c)>=p['daily_limit']:raise Denied('live_daily_limit')

    def close(self,reason):
        visit=self.s.get('live_visit')
        if visit:self.s.db.execute('UPDATE live_visits SET status=? WHERE id=?',(reason,visit['id']))
        self.s.put('live_visit',None)
        return {'status':'left','reason':reason}

    def start(self,c,p):
        now=self.e.now()
        if quota.count(self.s.db,c,now,'live_visits')>=p['visits_daily']:return {'status':'visits_daily_limit'}
        end=quota.activity_end(c,now,p['stay_max'])
        if end-now<p['stay_min']:return {'status':'near_quiet_hours'}
        self.s.put('live_next_visit',now+self.rng.randint(p['interval_min'],p['interval_max']))
        # Choose a random game/virtual/knowledge/life area; the core then checks actual interest and topics.
        parent=self.rng.choice([2,3,6,9]);candidates=self.api.candidates(parent)
        candidates=[r for r in candidates if r['uid']!=str(c['bot_uid']) and not blocked(json.dumps(r,ensure_ascii=False))
            and self.s.get('live_avoid:'+r['room_id'],0)<now and
            quota.count(self.s.db,c,now,where="kind='live' AND target=?",args=('live:'+r['room_id'],))<p['room_daily_limit']]
        self.rng.shuffle(candidates);candidates=candidates[:6]
        if not candidates:return {'status':'no_eligible_room'}
        answer=self.e.bounded_core({'op':'live_plan','mode':'select','material':{'rooms':candidates}})
        decision=answer.get('decision',{})
        chosen=next((r for r in candidates if r['room_id']==str(decision.get('room_id'))),None)
        if decision.get('allowed') is not True or not chosen or decision.get('interest_score',0)<65:return {'status':'no_interest'}
        room=self.api.room(chosen['room_id'])
        if room['room_id']!=chosen['room_id'] or not room['online'] or room['silent']:return {'status':'room_unavailable'}
        if blocked(json.dumps(room,ensure_ascii=False)) or self.e.screen_content(json.dumps(room,ensure_ascii=False)).get('allowed') is not True:
            self.s.put('live_avoid:'+room['room_id'],now+86400);return {'status':'fixed_topic_restriction'}
        return self.join(c,p,room)

    def join(self,c,p,room,stay_seconds=None):
        now=self.e.now()
        end=quota.activity_end(c,now,p['stay_max'])
        if end-now<p['stay_min']:raise Denied('outside_active_hours')
        duration=min(stay_seconds or self.rng.randint(p['stay_min'],p['stay_max']),int(end-now))
        key=hashlib.sha256((room['room_id']+':'+str(now)).encode()).hexdigest()
        # Baseline every pre-existing message before opening a visit.
        for row in self.api.messages(room['room_id']):self.s.db.execute('INSERT OR IGNORE INTO live_seen VALUES(?,?,?)',(token(room['room_id'],row),room['room_id'],now))
        self.s.db.execute('INSERT INTO live_visits VALUES(?,?,?,?,?,?)',(key,room['room_id'],day(now),now,now+duration,'active'))
        visit={'id':key,'room':room,'started':now,'ends':now+duration,'poll_at':now+p['poll_seconds'],'checked':now}
        self.s.put('live_visit',visit);self.s.put('live_next_visit',now+duration+self.rng.randint(p['interval_min'],p['interval_max']))
        return {'status':'joined_text_chat','room_id':room['room_id'],'ends':visit['ends']}

    def visit(self,room_id,stay_seconds=None,forced=False):
        c=self.e.config();p=policy(c);now=self.e.now();self.guard(c,p)
        if stay_seconds is not None and (type(stay_seconds) is not int or not p['stay_min']<=stay_seconds<=p['stay_max']):raise Denied('invalid_live_stay')
        if now<self.s.get('live_backoff_until',0):raise Denied('live_backoff')
        room=self.api.room(str(room_id));rid=room['room_id']
        if not room['online'] or room['silent']:raise Denied('live_room_unavailable')
        if self.s.get('live_avoid:'+rid,0)>now:raise Denied('live_room_avoided')
        visit=self.s.get('live_visit')
        if visit and now>=visit['ends']:self.close('visit_complete');visit=None
        if visit and visit['room']['room_id']==rid:return {'status':'already_present','room_id':rid,'ends':visit['ends']}
        if visit and not forced:raise Denied('live_current_visit')
        if visit and now-visit['started']<p['stay_min']:raise Denied('live_current_visit_minimum')
        if quota.count(self.s.db,c,now,'live_visits')>=p['visits_daily']:raise Denied('live_visits_daily_limit')
        if quota.count(self.s.db,c,now,where="kind='live' AND target=?",args=('live:'+rid,))>=p['room_daily_limit']:raise Denied('live_room_daily_limit')
        if blocked(json.dumps(room,ensure_ascii=False)) or self.e.screen_content(json.dumps(room,ensure_ascii=False)).get('allowed') is not True:raise Denied('fixed_topic_restriction')
        if self.e.config()!=c:raise Denied('live_policy_changed')
        self.guard(c,p)
        if visit:self.close('owner_switched_room')
        # A deliberate visit replaces only the random idle schedule. It does not
        # reset quotas, shorten a rejection cooldown, or resend any old message.
        return self.join(c,p,room,stay_seconds)

    def poll(self,c,p,visit):
        now=self.e.now();room=visit['room'];rid=room['room_id']
        if now>=visit['ends']:return self.close('visit_complete')
        if now<visit['poll_at']:return {'status':'waiting'}
        visit['poll_at']=now+p['poll_seconds'];self.s.put('live_visit',visit)
        if now-visit['checked']>=60:
            fresh=self.api.room(rid)
            if not fresh['online'] or fresh['silent']:return self.close('room_unavailable')
            if blocked(json.dumps(fresh,ensure_ascii=False)):return self.close('fixed_topic_restriction')
            if (fresh['title'],fresh['area'],fresh['description'])!=(room['title'],room['area'],room.get('description','')):
                if self.e.screen_content(json.dumps(fresh,ensure_ascii=False)).get('allowed') is not True:return self.close('fixed_topic_restriction')
            room=fresh;visit.update(room=room,checked=now);self.s.put('live_visit',visit)
        rows=[]
        for row in self.api.messages(rid):
            key=token(rid,row)
            if self.s.db.execute('SELECT 1 FROM live_seen WHERE id=?',(key,)).fetchone():continue
            self.s.db.execute('INSERT INTO live_seen VALUES(?,?,?)',(key,rid,now))
            if row['uid']==str(c['bot_uid']) or not max(visit['started'],now-90)<=row['time']<=now+5:continue
            rows.append(row)
        if not rows:return {'status':'no_new_chat'}
        material='\n'.join(r['text'] for r in rows)
        if STOP.search(material):self.s.put('live_avoid:'+rid,now+86400);return self.close('stop_requested')
        if blocked(material):self.s.put('live_avoid:'+rid,now+86400);return self.close('fixed_topic_restriction')
        # Giveaway slogans are not a conversation invitation; do not complain or redirect the room.
        rows=[row for row in rows if not LOW_INFORMATION.search(row['text'])]
        if not rows:return {'status':'no_substantive_chat'}
        material='\n'.join(r['text'] for r in rows)
        if quota.count(self.s.db,c,now,where="kind='live' AND target=?",args=('live:'+rid,))>=p['room_daily_limit']:return self.close('room_daily_limit')
        last=self.s.db.execute("SELECT max(created) FROM actions WHERE kind='live'").fetchone()[0]
        if last and now-last<p['message_gap']:return {'status':'message_gap'}
        history=[r[0] for r in self.s.db.execute("SELECT text FROM actions WHERE kind='live' AND target=? AND status='success' ORDER BY created DESC LIMIT 5",('live:'+rid,))]
        answer=self.e.bounded_core({'op':'live_plan','mode':'reply','material':{'room_id':rid,'room':room,'new_messages':[r['text'][:200] for r in rows[-10:]],'own_recent_messages':history}})
        d=answer.get('decision',{})
        if d.get('allowed') is not True:self.s.put('live_avoid:'+rid,now+86400);return self.close('topic_or_privacy_restriction')
        if d.get('willing') is not True or not d.get('text'):return {'status':'not_replying'}
        directed=any(name and name in material for name in (self.s.get('account_name',''),'蓝色大肥鱼','大肥鱼'))
        if not directed and d.get('interest_score',0)<65 and self.rng.random()>=p['ordinary_probability']:return {'status':'ordinary_probability_skip'}
        text=public_text(d['text'])
        if len(text)>20 or text in history:return {'status':'invalid_or_repeated_text'}
        if not is_plain_live_text(text):return {'status':'invalid_live_text'}
        key='live:'+str(c['bot_uid'])+':'+token(rid,rows[-1])
        return self.send(c,p,visit,key,text,answer)

    def send(self,c,p,visit,key,text,answer):
        text=public_text(text)
        if len(text)>20:raise Denied('live_message_too_long')
        if not is_plain_live_text(text):raise Denied('live_plain_text_only')
        self.guard(c,p);self.e.sender.identity(c);now=self.e.now();rid=visit['room']['room_id']
        current=self.s.get('live_visit')
        if not current or current['id']!=visit['id'] or now>=current['ends']:raise Denied('live_visit_expired')
        if self.e.config()!=c:raise Denied('live_policy_changed')
        self.guard(c,p)
        # A room can enable silence while a model reply is being generated.
        # Recheck using metadata only; never send a probe to test a restriction.
        fresh=self.api.room(rid)
        if fresh['room_id']!=rid or not fresh['online'] or fresh['silent']:
            return self.close(fresh.get('restriction') or 'room_unavailable')
        now=self.e.now()
        if now>=current['ends']:return self.close('visit_complete')
        if self.e.config()!=c:raise Denied('live_policy_changed')
        self.guard(c,p)
        if quota.count(self.s.db,c,now,where="kind='live' AND target=?",args=('live:'+rid,))>=p['room_daily_limit']:raise Denied('live_room_daily_limit')
        last=self.s.db.execute("SELECT max(created) FROM actions WHERE kind='live'").fetchone()[0]
        if last and now-last<p['message_gap']:raise Denied('live_message_gap')
        if self.s.db.execute('SELECT 1 FROM actions WHERE id=?',(key,)).fetchone():return {'status':'already_attempted'}
        # Reserve durably before the single send. Restart/timeout never resends a danmaku.
        self.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,str(c['bot_uid']),'live','live:'+rid,visit['room']['uid'],'live:'+rid,day(now),now,'sending',text,'','https://live.bilibili.com/'+rid,''))
        event={'event_id':key,'platform':'bilibili','bot_account_id':str(c['bot_uid']),'actor_id':str(c['bot_uid']),
            'origin':'system_task','event_type':'live_text_chat','conversation_id':'live:'+rid,'source_id':key,'content':'',
            'sharing_scope':'public','learning_allowed':False,'occurred_at':now}
        self.s.db.execute('INSERT INTO action_context VALUES(?,?,?,?)',(key,json.dumps(event),str(answer.get('persona_version','')),json.dumps(answer.get('source_ids',[]))))
        try:self.api.send(rid,text)
        except PlatformError as exc:
            self.s.update(key,status='failed',error='platform_'+str(exc.code));self.s.put('platform_error:'+key,getattr(self.e.api,'last_platform_error',None))
            self.s.put('live_backoff_until',now+3600);self.s.put('live_avoid:'+rid,now+86400);self.close('platform_rejected')
            result={'status':'failed','reason':'platform_'+str(exc.code)}
            detail=getattr(self.e.api,'last_platform_error',None) or {}
            if detail.get('classification') in ('room_keyword_block','site_keyword_block','unknown_live_rejection'):
                result['rejection_classification']=detail['classification']
        except Exception:
            self.s.update(key,status='unknown',error='no_reliable_receipt');self.close('delivery_unknown');result={'status':'unknown','reason':'no_retry'}
        else:
            self.s.update(key,status='success',remote_id='live-receipt:'+key);result={'status':'success','room_id':rid,'text':text,'visibility':'not_independently_checked'}
        self.e.sync();return result

    def tick(self):
        c=self.e.config();p=policy(c);now=self.e.now()
        if not p['enabled']:return self.close('disabled') if self.s.get('live_visit') else {'status':'disabled'}
        if now<self.s.get('live_backoff_until',0):return {'status':'backoff'}
        try:
            self.guard(c,p)
            self.s.db.execute('DELETE FROM live_seen WHERE created<?',(now-7*86400,))
            visit=self.s.get('live_visit')
            if visit:return self.poll(c,p,visit)
            if c.get('autonomy_enabled'):return {'status':'waiting'}
            due=self.s.get('live_next_visit')
            if due is None:self.s.put('live_next_visit',now+self.rng.randint(600,1800));return {'status':'scheduled'}
            if now<due:return {'status':'waiting'}
            self.e.sender.identity(c);return self.start(c,p)
        except Denied as exc:
            if self.s.get('live_visit'):self.close(str(exc))
            return {'status':'skipped','reason':str(exc)}
        except PlatformError as exc:
            self.s.put('live_backoff_until',now+3600);self.close('platform_rejected')
            return {'status':'backoff','reason':'platform_'+str(exc.code)}
        except Exception:
            self.s.put('live_backoff_until',now+3600);self.close('read_or_model_error')
            return {'status':'backoff','reason':'live_read_or_model_error'}
