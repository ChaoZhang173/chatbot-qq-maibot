"""Autonomous, receipt-tracked public short posts through Tencent's official CLI."""
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse,fcntl,hashlib,json,os,re,socket,sqlite3,subprocess,time,uuid
from urllib.parse import urlsplit

TZ=ZoneInfo('Asia/Shanghai')
CLI='/opt/dafeiyu-qq-channel/tencent-channel-cli'
CORE='/srv/maibot/unified/core.sock'

def core(request):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(100);s.connect(CORE)
        s.sendall((json.dumps(request,ensure_ascii=False)+'\n').encode())
        data=s.makefile('rb').readline(100000)
    answer=json.loads(data)
    if answer.get('ok') is not True:raise RuntimeError('core_request_failed')
    return answer

def cli_publish(text):
    result=subprocess.run([CLI,'feed','publish-feed','--yes','--json'],
        input=json.dumps({'content':text,'feed_type':1},ensure_ascii=False),
        capture_output=True,text=True,timeout=60)
    try:value=json.loads(result.stdout)
    except Exception:raise RuntimeError('unreadable_publish_receipt') from None
    return value

def receipt(value):
    if not isinstance(value,dict) or value.get('success') is not True:
        return 'failed','',''
    ids=[];urls=[]
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if re.sub('_','',k).lower()=='feedid' and isinstance(x,(str,int)) and str(x):ids.append(str(x))
                if isinstance(x,str) and x.startswith('https://pd.qq.com/'):
                    parsed=urlsplit(x)
                    if parsed.hostname=='pd.qq.com' and parsed.path not in ('','/'):urls.append(x)
                elif isinstance(x,(dict,list)):walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(value.get('data'))
    return ('success',ids[0] if ids else urls[0],urls[0] if urls else '') if ids or urls else ('unknown','','')

def bounds(now):
    dt=datetime.fromtimestamp(now,TZ);start=dt.replace(hour=0,minute=0,second=0,microsecond=0)
    overnight=start+timedelta(hours=23)
    if dt.hour<9:overnight-=timedelta(days=1)
    return start.timestamp(),(start+timedelta(days=1)).timestamp(),overnight.timestamp(),dt.hour<9 or dt.hour>=23

class Runner:
    def __init__(self,root,request=core,publish=cli_publish,now=time.time):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.request=request;self.publish=publish;self.now=now
        self.db=sqlite3.connect(self.root/'state.sqlite',isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
          CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,created REAL,status TEXT);
          CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,created REAL,status TEXT,text TEXT,
            text_hash TEXT,remote_id TEXT,url TEXT,persona_version INTEGER,source_ids TEXT,synced INTEGER DEFAULT 0);''')
        self.db.execute("UPDATE posts SET status='unknown' WHERE status='sending'")
        self.db.execute("UPDATE rounds SET status='interrupted' WHERE status='planning'")
    def get(self,k,default=None):
        row=self.db.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone()
        return json.loads(row[0]) if row else default
    def put(self,k,v):self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
    def config(self):
        c=json.loads((self.root/'config.json').read_text())
        assert set(c)=={'enabled','author_confirmed','account','credential_path','credential_sha256','daily_limit','night_limit','min_gap','decision_limit','night_decision_limit'}
        if type(c['enabled']) is not bool or type(c['author_confirmed']) is not bool:raise ValueError('invalid_channel_switch')
        if c['account']!='9000000001':raise ValueError('channel_account_changed')
        limits={'daily_limit':(1,6),'night_limit':(0,2),'min_gap':(7200,86400),'decision_limit':(1,12),'night_decision_limit':(0,3)}
        for k,(lo,hi) in limits.items():
            if type(c[k]) is not int or not lo<=c[k]<=hi:raise ValueError('invalid_channel_limit')
        if c['credential_path']!='/srv/maibot/.qqcli/.env' or not re.fullmatch('[a-f0-9]{64}',c['credential_sha256']):raise ValueError('invalid_channel_credentials')
        return c
    def identity(self,c):
        path=Path(c['credential_path'])
        if path.stat().st_mode&0o077:raise PermissionError('channel_credentials_permissions')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=c['credential_sha256']:raise PermissionError('channel_credentials_changed')
    def count(self,table,start,end):
        assert table in ('posts','rounds')
        return self.db.execute('SELECT count(*) FROM '+table+' WHERE created>=? AND created<?',(start,end)).fetchone()[0]
    def gate(self,c,now):
        if not c['enabled'] or not c['author_confirmed']:return 'disabled'
        start,end,night_start,night=bounds(now)
        if self.count('posts',start,end)>=c['daily_limit']:return 'daily_limit'
        if night and self.count('posts',night_start,night_start+36000)>=c['night_limit']:return 'night_limit'
        latest=self.db.execute('SELECT max(created) FROM posts').fetchone()[0]
        if latest is not None and now-latest<c['min_gap']:return 'post_gap'
        return ''
    def sync(self,c):
        for row in self.db.execute("SELECT * FROM posts WHERE synced=0 AND status!='sending' ORDER BY created LIMIT 5").fetchall():
            action={'action_id':'qq-channel:'+row['id'],'platform':'qq_channel','bot_account_id':c['account'],
                'conversation_id':'qq_channel:global','kind':'dynamic','status':row['status'],'content':row['text'],
                'remote_id':row['remote_id'],'url':row['url'],'created':row['created'],'sharing_scope':'public',
                'receipt_verified':row['status']=='success','persona_version':row['persona_version'],
                'source_ids':json.loads(row['source_ids']),'public_visibility':'not_independently_checked'}
            self.request({'op':'activity','action':action})
            self.db.execute('UPDATE posts SET synced=1 WHERE id=?',(row['id'],))
    def publish_draft(self,draft,c):
        now=self.now();reason=self.gate(c,now)
        if reason:return {'status':reason}
        if self.config()!=c:raise ValueError('channel_policy_changed')
        self.identity(c)
        if draft.get('status')!='draft' or not 0<=now-draft.get('created_at',0)<=900:raise ValueError('stale_channel_draft')
        text=draft.get('text');cycle=draft.get('cycle','')
        if not isinstance(text,str) or not 1<=len(text)<=250 or not re.fullmatch('[a-f0-9]{64}',cycle):raise ValueError('invalid_channel_draft')
        digest=hashlib.sha256(text.encode()).hexdigest()
        if digest!=draft.get('text_hash'):raise ValueError('channel_draft_changed')
        if self.db.execute('SELECT 1 FROM posts WHERE id=? OR text_hash=?',(cycle,digest)).fetchone():return {'status':'already_attempted'}
        self.db.execute('INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,0)',
            (cycle,now,'sending',text,digest,'','',draft['persona_version'],json.dumps(draft.get('source_ids',[]))))
        try:
            answer=self.publish(text)
            status,remote_id,url=receipt(answer)
            path=self.root/('receipt-'+cycle+'.json');path.write_text(json.dumps(answer,ensure_ascii=False));path.chmod(0o600)
        except Exception:status,remote_id,url='unknown','',''
        self.db.execute('UPDATE posts SET status=?,remote_id=?,url=? WHERE id=?',(status,remote_id,url,cycle))
        self.put('next_at',now+c['min_gap'])
        return {'status':status,'url':url,'cycle':cycle}
    def tick(self):
        c=self.config();now=self.now()
        if not c['enabled'] or not c['author_confirmed']:return {'status':'disabled'}
        self.identity(c)
        try:self.sync(c)
        except Exception:pass
        if self.get('blocked_reason'):return {'status':self.get('blocked_reason')}
        reason=self.gate(c,now)
        if reason:return {'status':reason}
        if now<self.get('next_at',0):return {'status':'waiting'}
        start,end,night_start,night=bounds(now)
        if self.count('rounds',start,end)>=c['decision_limit']:return {'status':'decision_limit'}
        if night and self.count('rounds',night_start,night_start+36000)>=c['night_decision_limit']:return {'status':'night_decision_limit'}
        cycle=hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        self.db.execute('INSERT INTO rounds VALUES(?,?,?)',(cycle,now,'planning'))
        self.put('next_at',now+3600)
        history=[r[0] for r in self.db.execute('SELECT text FROM posts ORDER BY created DESC LIMIT 6')]
        try:
            draft=self.request({'op':'qq_channel_plan','cycle':cycle,'history':history})
            path=self.root/('draft-'+cycle+'.json');path.write_text(json.dumps(draft,ensure_ascii=False));path.chmod(0o600)
            self.db.execute('UPDATE rounds SET status=? WHERE id=?',(draft.get('status','invalid'),cycle))
            wait=draft.get('wait_minutes',120)
            if type(wait) is not int or not 60<=wait<=180:wait=120
            self.put('next_at',now+wait*60)
            result=self.publish_draft(draft,c) if draft.get('status')=='draft' else {'status':'skipped'}
            try:self.sync(c)
            except Exception:pass
            return result
        except Exception:
            self.db.execute("UPDATE rounds SET status='failed' WHERE id=?",(cycle,))
            return {'status':'planning_failed'}

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--state',default='/srv/dafeiyu-channel');parser.add_argument('--once',action='store_true');args=parser.parse_args()
    root=Path(args.state);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(root/'instance.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    runner=Runner(root);previous=None
    while True:
        try:result=runner.tick()
        except Exception as exc:result={'status':'unavailable','reason':type(exc).__name__}
        runner.put('last_result',{'at':time.time(),**result})
        if result!=previous:print(json.dumps(result,ensure_ascii=False),flush=True);previous=result
        if args.once:return
        time.sleep(30)

if __name__=='__main__':main()
