"""Read/patch/re-read account settings through the existing single writer."""
import hashlib,json
from .account_schema import validate_settings,SPACE_FIELDS,MESSAGE_FIELDS
from .api import platform_message
from . import quota
from .core import Denied,PlatformError,UnknownDelivery,active,day,machine_id

class AccountSettings:
    def __init__(self,engine):
        self.e=engine;self.db=engine.s.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS account_changes(
          id TEXT PRIMARY KEY,day TEXT NOT NULL,created REAL NOT NULL,section TEXT NOT NULL,
          patch TEXT NOT NULL,before_state TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '')''')

    def snapshot(self,section,patch=None):
        e=self.e;api=e.api;uid=str(e.config()['bot_uid']);patch=patch or {}
        if section=='profile':
            p=api.request('/x/space/myinfo')
            if str(p.get('mid'))!=uid:raise Denied('account_identity_mismatch')
            n=api.request('/x/space/notice',params={'mid':uid})
            return {'sign':p.get('sign',''),'notice':n if isinstance(n,str) else ''}
        if section=='space':
            data=api.request('/ajax/settings/getSettings',params={'mid':uid},space=True)
            # The legacy endpoint can return status=true,data=[] for an existing
            # account. Empty data is not proof that its privacy settings are open.
            if not isinstance(data,dict) or not isinstance(data.get('privacy'),dict):
                data=api.request('/x/space/setting/app',params={'mid':uid})
            if not isinstance(data,dict) or not isinstance(data.get('privacy'),dict) or not data['privacy']:
                raise Denied('space_settings_unavailable')
            privacy=data.get('privacy') or {}
            result={k:v for k,v in privacy.items() if k in SPACE_FIELDS}
            if isinstance(data.get('index_order'),list) and data['index_order']:
                result['index_order']=[int(r['id']) for r in data['index_order']]
            return result
        if section=='messages':
            data=api.request('/link_setting/v1/link_setting/get',params={'mobi_app':'web','msg_notify':1,'show_unfollowed_msg':1},vc=True)
            return {k:v for k,v in data.items() if k in MESSAGE_FIELDS}
        if section=='folder':
            folders=api.request('/x/v3/fav/folder/created/list-all',params={'up_mid':uid}).get('list') or []
            fid=patch.get('media_id')
            if not fid:return {'folders':[{'media_id':str(r['id']),'title':str(r['title'])[:40]} for r in folders[:20]]}
            if not any(str(r.get('id'))==fid for r in folders):raise Denied('own_folder_required')
            data=api.request('/x/v3/fav/folder/info',params={'media_id':fid})
            if str((data.get('upper') or {}).get('mid'))!=uid:raise Denied('own_folder_required')
            return {'media_id':fid,'title':data['title'],'intro':data.get('intro',''),'privacy':int(data.get('attr',0))&1,'cover':data.get('cover','')}
        raise Denied('unknown_setting_section')

    def execute(self,key,section,patch):
        patch=validate_settings(section,patch);e=self.e;c=e.config();now=e.now()
        if not getattr(e.s,'_lock_held',False):raise Denied('single_writer_required')
        if not c.get('account_settings_enabled') or c['paused'] or (e.s.root/'PAUSE').exists() or e.s.get('pause_reason'):raise Denied('account_settings_disabled_or_paused')
        if not active(c,now):raise Denied('outside_active_hours')
        if str(e.s.get('bound_uid'))!=str(c['bot_uid']) or c['writer_machine']!=machine_id() or not e.s.get('baseline_at'):raise Denied('account_identity_required')
        row=self.db.execute('SELECT status,reason FROM account_changes WHERE id=?',(key,)).fetchone()
        if row:return {'kind':'account','status':'unknown' if row['status']=='executing' else row['status'],'reason':row['reason'],'section':section}
        for prior in self.db.execute("SELECT patch,status FROM account_changes WHERE section=? AND status IN ('unknown','executing')",(section,)):
            if json.loads(prior['patch'])==patch:return {'kind':'account','status':'unknown','reason':'prior_setting_result_unknown','section':section}
        if quota.count(self.db,c,now,'account_changes')>=c.get('account_period_limit',3):raise Denied('account_settings_daily_limit')
        # Prevent oscillation and no-receipt retry loops, including after restart.
        recent=self.db.execute('SELECT max(created) FROM account_changes').fetchone()[0]
        if recent and now-recent<3600:raise Denied('account_settings_minimum_gap')
        e.sender.identity(c);before=self.snapshot(section,patch)
        if any(k not in before for k in patch):raise Denied('setting_not_available')
        if section=='space' and 'index_order' in patch and set(patch['index_order'])!=set(before['index_order']):raise Denied('space_sections_must_be_preserved')
        if all(before[k]==v for k,v in patch.items()):return {'kind':'account','status':'skipped','reason':'already_present','section':section}
        if e.config()!=c or quota.window(c,e.now())!=quota.window(c,now):raise Denied('policy_changed')
        self.db.execute('INSERT INTO account_changes VALUES(?,?,?,?,?,?,?,?)',(key,day(now),now,section,json.dumps(patch,ensure_ascii=False),json.dumps(before,ensure_ascii=False),'executing',''))
        status='unknown';reason='write_no_receipt';details={};stage='write'
        try:
            csrf=e.api.credentials['BILI_JCT']
            if section=='profile':
                # One setting per plan, so a lost response cannot obscure a second write.
                if len(patch)!=1:raise Denied('one_profile_field_per_action')
                if 'sign' in patch:e.api.request('/x/member/web/sign/update',data={'user_sign':patch['sign'],'csrf':csrf})
                else:e.api.request('/x/space/notice/set',data={'notice':patch['notice'],'csrf':csrf})
            elif section=='space':
                if 'index_order' in patch:
                    if len(patch)!=1:raise Denied('space_layout_separate_action')
                    e.api.request('/ajax/settings/setIndexOrder',data={'index_order':','.join(map(str,patch['index_order'])),'csrf':csrf},space=True)
                else:e.api.request('/ajax/settings/setPrivacy',data={**patch,'csrf':csrf},space=True)
            elif section=='messages':e.api.request('/link_setting/v1/link_setting/set',data={**patch,'csrf':csrf,'csrf_token':csrf,'mobi_app':'web'},vc=True)
            else:
                fields={k:before[k] for k in ('media_id','title','intro','privacy','cover')};fields.update(patch)
                e.api.request('/x/v3/fav/folder/edit',data={**fields,'csrf':csrf})
            stage='readback'
            after=self.snapshot(section,patch)
            status='success' if all(after.get(k)==v for k,v in patch.items()) else 'unknown'
            reason='' if status=='success' else 'setting_readback_not_confirmed'
        except PlatformError as exc:
            status='failed' if stage=='write' else 'unknown'
            reason='platform_'+str(exc.code) if stage=='write' else 'setting_readback_not_confirmed'
            details={'platform_code':exc.code,'platform_message':platform_message((getattr(e.api,'last_platform_error',None) or {}).get('message')),'stage':stage}
        except Denied as exc:status='failed';reason=str(exc)
        except Exception:status='unknown';reason='setting_result_unverified'
        self.db.execute('UPDATE account_changes SET status=?,reason=? WHERE id=?',(status,reason,key))
        # Non-public setting values remain in the local change log, not public memory.
        e.s.db.execute('INSERT OR IGNORE INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key,str(c['bot_uid']),'account','self:'+str(c['bot_uid']),'','self:'+str(c['bot_uid']),day(now),now,status,
             '调整'+section+'设置','settings_readback:'+key if status=='success' else '', '',reason))
        return {'kind':'account','status':status,'reason':reason,'section':section,'verified':status=='success',**details}
