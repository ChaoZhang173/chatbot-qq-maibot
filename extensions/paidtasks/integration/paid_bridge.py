"""MaiBot-side adapter. No payment API token enters this process or its models."""
import asyncio
import base64
import hashlib
import json
import re
import time

class PaidBridge:
    def __init__(self,service):self.s=service;self.in_flight=set()

    def enabled(self):return self.s.policy.get('paid_tasks_bridge_enabled') is True

    def owners(self):
        rows=self.s.state.db.execute('SELECT * FROM qq_private_bindings WHERE session!=""').fetchall()
        return [r for r in rows if self.s.private_access.binding(r['private_id']) and
                self.s.policy.get('qq_owner_bindings',{}).get(r['group_session'])==r['group_actor']]

    async def capture(self,message):
        if not self.enabled():return False
        m,info,user,session=self.s.qq_fields(message)
        text=self.s.qq_text(m).strip()
        # Plain mentions delivered as text may precede the exact paid-command marker.
        if text.startswith(('@','＠')):
            names=[self.s.state.persona()['stable'].get('bot.nickname','')]+list(self.s.state.persona()['stable'].get('bot.alias_names',[]))
            for name in names:
                for marker in ('@','＠'):
                    if name and text.startswith(marker+name):text=text[len(name)+1:].lstrip()
        if not text.startswith('【小单】') or m.get('is_notify'):return False
        actor=str(user.get('user_id') or '');mid=str(m.get('message_id') or '')
        if not actor or not mid or session not in self.s.policy.get('allowed_qq_sessions',[]):return False
        if not 0<=time.time()-self.s.qq_time(m)<=300:return False
        owner=self.s.policy.get('qq_owner_bindings',{}).get(session)==actor
        if m.get('scope')=='private':
            raw=str(info.get('additional_config',{}).get('qq_official_user_openid') or actor)
            owner=any(r['session']==session and r['private_id']==raw for r in self.owners())
        event={'customer':{'channel':'qq','account':str(self.s.policy['qq_bot_account']),
            'conversation':session,'actor':actor},'message_id':mid,'text':text,'owner':owner,
            'owner_private':owner and m.get('scope')=='private'}
        self.s.state.put('qq_command_message:'+session+':'+mid,'paid_command')
        self.s.record_qq_reply_eligibility(m)
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_unix_connection('/srv/maibot/paid/paid.sock'),1)
            writer.write(json.dumps({'op':'channel','event':event},ensure_ascii=False).encode()+b'\n');await writer.drain()
            reply=json.loads(await asyncio.wait_for(reader.readline(),2))
            writer.close();await writer.wait_closed()
            if not reply.get('ok'):raise ValueError('paid_unavailable')
        except Exception:
            # No claim that a request was accepted if the durable service cannot confirm it.
            self.s.state.put('paid_bridge_last_error','intake_unavailable')
        return True

    async def dispatch(self,r):
        if not self.enabled():raise ValueError('paid_bridge_disabled')
        op=r['op']
        if op=='paid_info':
            owners=self.owners()
            return {'owner_c2c_bound':len(owners)==1,'owner_sessions_count':len(owners),
                'paid_model_supported':False,'private_image_route':'existing_sdk_unverified'}
        if op=='paid_activity':
            stage=r.get('stage')
            labels={'accepted':'已接受一笔受限小任务，等待核款','paid':'一笔小任务已通过支付核验',
                'delivered':'一笔小任务已取得交付回执','failed':'一笔小任务需要处理','interrupted':'一笔小任务中断，等待核查'}
            if stage not in labels:return {'status':'sent','remote_id':'local-ignored'}
            self.s.state.record_activity({'action_id':r['key'],'platform':'qq',
                'bot_account_id':str(self.s.policy['qq_bot_account']),'conversation_id':'self',
                'kind':'paid_task_status','status':'succeeded','receipt_verified':True,
                'sharing_scope':'public','text':labels[stage]+'；不包含客户身份、需求或成果。'})
            return {'status':'sent','remote_id':'local-activity'}
        if op=='paid_generate':
            raise ValueError('paid_model_cost_contract_not_verified')
        if op=='paid_reconcile_send':
            if not isinstance(r.get('basis'),str) or len(r['basis'])<8:
                raise ValueError('manual_reconciliation_evidence_required')
            for kind in ('text','image'):
                key='paid_send:'+hashlib.sha256((r['key']+':'+kind).encode()).hexdigest()
                old=self.s.state.get(key)
                if old and old['status']=='unknown':
                    self.s.state.put(key,{**old,'status':'rejected','owner_confirmed_not_sent':True})
            self.s.state.put('paid_reconcile:'+hashlib.sha256(r['key'].encode()).hexdigest(),
                {'at':time.time(),'basis':r['basis']})
            return {'reconciled':True}
        if op!='paid_send':raise ValueError('paid_operation_unknown')
        key=r.get('key','')
        if not isinstance(key,str) or not 1<=len(key)<=200:raise ValueError('send_key')
        if r.get('owner'):
            owners=self.owners()
            if len(owners)!=1:return {'status':'rejected','reason':'owner_c2c_not_uniquely_bound'}
            session=owners[0]['session']
        else:
            c=r.get('customer',{})
            if c.get('channel')!='qq' or c.get('account')!=str(self.s.policy['qq_bot_account']):raise ValueError('channel_account')
            session=c.get('conversation')
            if session not in self.s.policy.get('allowed_qq_sessions',[]):raise ValueError('session_not_allowed')
        text=r.get('text','')
        if not isinstance(text,str) or not 1<=len(text)<=2200:raise ValueError('send_text_limit')
        parts=[('text',text)]
        if r.get('image_base64'):
            raw=base64.b64decode(r['image_base64'],validate=True)
            if len(raw)>2*1024*1024 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('bounded_png_required')
            parts.append(('image',r['image_base64']))
        last=''
        for kind,content in parts:
            partkey='paid_send:'+hashlib.sha256((key+':'+kind).encode()).hexdigest()
            old=self.s.state.get(partkey)
            fingerprint=hashlib.sha256((session+content).encode()).hexdigest()
            if old:
                if old.get('fingerprint')!=fingerprint:raise ValueError('idempotency_payload_changed')
                if old['status']=='sent':last=old.get('remote_id','');continue
                if old['status']=='unknown':return {'status':'unknown','reason':'prior_delivery_uncertain'}
            self.s.state.put(partkey,{'status':'unknown','fingerprint':fingerprint})
            try:
                self.in_flight.add(session)
                if r.get('owner'):
                    reader,writer=await asyncio.wait_for(asyncio.open_unix_connection('/srv/maibot/unified/qq-paid-owner.sock'),2)
                    writer.write(json.dumps({'target':owners[0]['private_id'],'text':content,'mode':r.get('owner_mode','blocked')},ensure_ascii=False).encode()+b'\n')
                    await writer.drain()
                    try:active=json.loads(await asyncio.wait_for(reader.readline(),25))
                    finally:writer.close();await writer.wait_closed()
                    if active.get('status')=='rejected':
                        self.s.state.put(partkey,{'status':'rejected','fingerprint':fingerprint})
                        return active
                    result={'sent':active.get('status')=='sent','message_id':active.get('remote_id','')}
                else:
                    result=await asyncio.wait_for(getattr(self.s.ctx.send,kind)(content,session,return_details=True,storage_message=False),25)
            except Exception:return {'status':'unknown','reason':'send_uncertain'}
            finally:self.in_flight.discard(session)
            good=isinstance(result,dict) and result.get('sent') is True
            remote=str(result.get('message_id') or result.get('external_message_id') or '') if isinstance(result,dict) else ''
            if good:
                self.s.state.put(partkey,{'status':'sent','fingerprint':fingerprint,'remote_id':remote});last=remote
            else:
                # Current Host send API does not distinguish rejected from timeout.
                # Preserve unknown instead of retrying a possibly accepted send.
                return {'status':'unknown','reason':'host_send_not_confirmed'}
        mode=('active' if r.get('owner_mode')=='active_probe' else 'passive_window') if r.get('owner') else 'original_session'
        return {'status':'sent','remote_id':last,'send_mode':mode}
