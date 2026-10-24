"""C2C identity proof. Group and private OpenIDs are deliberately not equated."""
import hashlib,re,secrets,time

class PrivateAccess:
    def __init__(self,service):
        self.s=service;self.db=service.state.db
        self.db.executescript('''CREATE TABLE IF NOT EXISTS qq_private_challenges(
          digest TEXT PRIMARY KEY,private_id TEXT NOT NULL,created REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS qq_private_bindings(
          private_id TEXT PRIMARY KEY,group_session TEXT NOT NULL,group_actor TEXT NOT NULL,
          verified REAL NOT NULL,session TEXT NOT NULL DEFAULT '',UNIQUE(group_session,group_actor));''')

    def enabled(self):return self.s.policy.get('qq_private_familiar_enabled') is True
    def binding(self,private_id):
        if not self.enabled():return None
        row=self.db.execute('SELECT * FROM qq_private_bindings WHERE private_id=?',(private_id,)).fetchone()
        if row and row['group_session'] in self.s.policy.get('qq_group_sessions',{}).values():return row
    def sessions(self):
        if not self.enabled():return []
        return [r['session'] for r in self.db.execute('SELECT * FROM qq_private_bindings WHERE session!=""') if self.binding(r['private_id'])]
    def register(self,private_id,session):
        if not session or session in self.s.policy.get('qq_group_sessions',{}).values() or not self.binding(private_id):return False
        other=self.db.execute('SELECT private_id FROM qq_private_bindings WHERE session=?',(session,)).fetchone()
        if other and other['private_id']!=private_id:return False
        row=self.binding(private_id)
        if row['session'] and row['session']!=session:return False
        self.db.execute('UPDATE qq_private_bindings SET session=? WHERE private_id=?',(session,private_id));return True

    def handle(self,r):
        self.s.reload_policy()
        if not self.enabled():return {'allow':False}
        scene=r.get('scene');actor=r.get('actor','');text=str(r.get('text','')).strip()
        if not isinstance(actor,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',actor):return {'allow':False}
        if scene=='private':
            if self.binding(actor):return {'allow':True}
            if r.get('outbound') or text!='验证熟人':return {'allow':False}
            now=time.time()
            self.db.execute('DELETE FROM qq_private_challenges WHERE created<?',(now-86400,))
            if self.db.execute('SELECT count(*) FROM qq_private_challenges WHERE private_id=?',(actor,)).fetchone()[0]>=3 or self.db.execute('SELECT count(*) FROM qq_private_challenges').fetchone()[0]>=50:
                return {'allow':False,'reply':'今天核验次数已达上限，请明天再试。'}
            code=secrets.token_hex(8).upper();digest=hashlib.sha256(code.encode()).hexdigest()
            self.db.execute('UPDATE qq_private_challenges SET used=1 WHERE private_id=?',(actor,))
            self.db.execute('INSERT INTO qq_private_challenges VALUES(?,?,?,0)',(digest,actor,now))
            return {'allow':False,'reply':'请在10分钟内，用你本人曾与我互动的授权QQ群账号 @我发送：私聊核验 '+code+'。不要转交他人；核验后再来私聊。'}
        if scene!='group' or not text.startswith('私聊核验'):return {'allow':True}
        session=self.s.policy.get('qq_group_sessions',{}).get(str(r.get('group','')))
        if not session:return {'allow':False}
        m=re.fullmatch(r'私聊核验\s+([A-Fa-f0-9]{16})',text)
        invalid={'allow':False,'reply':'核验未通过。请先私聊发送“验证熟人”，再用已有互动记录的群账号提交有效核验码。'}
        if not m:return invalid
        row=self.db.execute('SELECT * FROM qq_private_challenges WHERE digest=? AND used=0 AND created>?',
            (hashlib.sha256(m[1].upper().encode()).hexdigest(),time.time()-600)).fetchone()
        if not row:return invalid
        known=self.db.execute("SELECT 1 FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND actor_id=? AND origin='external_user' AND event_type='message_received' AND active=1 AND received_at<? LIMIT 1",
            (str(self.s.policy['qq_bot_account']),session,actor,row['created'])).fetchone()
        if not known:return invalid
        old=self.db.execute('SELECT private_id FROM qq_private_bindings WHERE group_session=? AND group_actor=?',(session,actor)).fetchone()
        if old and old['private_id']!=row['private_id']:return invalid
        self.db.execute('INSERT OR IGNORE INTO qq_private_bindings(private_id,group_session,group_actor,verified) VALUES(?,?,?,?)',(row['private_id'],session,actor,time.time()))
        self.db.execute('UPDATE qq_private_challenges SET used=1 WHERE digest=?',(row['digest'],))
        return {'allow':False,'reply':'熟人身份核验完成，可以私聊了。私聊记忆独立保存。'}
