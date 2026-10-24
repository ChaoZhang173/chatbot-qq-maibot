"""Local-only control plane and optional loopback Webhook ingress."""
import asyncio
import base64
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
from .afdian import AfdianClient, ProviderError
from .executor import execute, MODEL_TASKS
from .reconcile import Reconciler
from .store import Store, identity, js, digest

ROOT = Path(os.environ.get("PAIDTASKS_ROOT", "/srv/maibot/paid"))
CORE = "/srv/maibot/unified/core.sock"

def rpc(request, path=CORE, timeout=20):
    wire=js(request).encode()+b'\n'
    if len(wire)>3*1024*1024:raise ValueError('rpc_request_too_large')
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout);client.connect(str(path));client.sendall(wire)
        data=bytearray()
        while b'\n' not in data:
            part=client.recv(65536)
            if not part or len(data)+len(part)>3*1024*1024:raise ValueError('rpc_incomplete')
            data.extend(part)
        response=json.loads(bytes(data).split(b'\n',1)[0])
    if response.get('ok') is not True:raise ValueError('core_operation_rejected')
    return response

def validate_policy(p):
    if p.get('max_parallel_jobs')!=1:raise ValueError('single_worker_required')
    if p.get('model_tasks_enabled'):raise ValueError('model_cost_contract_not_verified')
    for k in ('unit_price_fen','max_order_fen','quote_ttl_seconds','max_outstanding_orders',
              'daily_budget_microyuan','max_job_budget_microyuan','scan_pages_per_tick'):
        if type(p.get(k)) is not int or not 1<=p[k]<=100000000:raise ValueError('invalid_policy_'+k)
    if p['scan_pages_per_tick']<2:raise ValueError('pagination_overlap_needs_two_pages')
    if p.get('intake_enabled') and (not p.get('product_url','').startswith('https://afdian.com/') or not p.get('plan_id') or not p.get('sku_id')):
        raise ValueError('verified_product_configuration_required')
    if not p.get('model_tasks_enabled') and any(t in MODEL_TASKS for t in p.get('allowed_tasks',[])):
        raise ValueError('model_tasks_need_explicit_enablement')
    return p

class Application:
    def __init__(self, store, client=None, transport=rpc):
        self.s=store;self.client=client;self.transport=transport
        self.reconciler=Reconciler(store,client) if client else None
        self.next_scan=0;self.stopping=False
        store.db.execute('CREATE TABLE IF NOT EXISTS channel_requests(event_key TEXT PRIMARY KEY,reply TEXT NOT NULL,created REAL NOT NULL)')

    def channel(self, event):
        c=event['customer'];customer=identity(c)
        mid=event.get('message_id')
        if not isinstance(mid,str) or not 1<=len(mid)<=128:raise ValueError('message_id_required')
        text=event.get('text','')
        if not isinstance(text,str) or len(text)>12000:raise ValueError('command_size')
        if not text.startswith('【小单】'):return {'handled':False}
        key='channel:'+digest(customer+'\n'+mid)
        with self.s.tx():
            if self.s.db.execute('SELECT 1 FROM channel_requests WHERE event_key=?',(key,)).fetchone():return {'handled':True,'duplicate':True}
            if event.get('owner') is True and event.get('owner_private') is True:
                self.s.put('owner_last_inbound',self.s.now())
                self.s.db.execute("UPDATE outbox SET next_attempt=0 WHERE kind='owner' AND status='retry'")
            try:reply=self.command(c,text[len('【小单】'):].strip(),bool(event.get('owner')))
            except (ValueError,PermissionError) as e:reply='这单暂时不能推进：'+str(e)+ '。没有产生新的付款确认。'
            self.s.db.execute('INSERT INTO channel_requests VALUES(?,?,?)',(key,'queued',self.s.now()))
            self.s.emit(key,'','customer_reply',{'customer':c,'text':reply})
        return {'handled':True,'queued':True}

    def command(self, c, text, owner=False):
        args=text.split(' ',2);op=args[0]
        if op in ('帮助','help'):
            return ('小单先确认范围再付款，目前只向白名单开放。\n报价命令：\n【小单】报价 '+
                '{"task":"render_formula","arguments":{"expression":"(12+8)/4"}}\n'+
                '还可选 text_cleanup（text）或 render_plot（values、labels、unit）。收到报价后发送：确认 订单号 版本号。状态/结果只能查本人订单。')
        if op=='报价':
            request=json.loads(text[len(op):].strip())
            if set(request)!={'task','arguments'}:raise ValueError('only_task_and_arguments_allowed')
            q=self.s.quote(c,request['task'],request['arguments'])
            snapshot=q['quote']
            summary=js(snapshot['arguments'])
            summary=summary if len(summary)<=400 else summary[:350]+'…（完整内容按本次输入冻结）'
            return (f"报价 {q['order_id']} v{q['version']}：¥{snapshot['amount_fen']/100:.2f}，{snapshot['task']}。\n"
                f"输入核对：{summary}\n报价有效期{self.s.policy['quote_ttl_seconds']//60}分钟。\n"
                f"范围：{snapshot['scope']}；预计3分钟；一次同范围纠错。成果至少保留7天；核验凭据留作对账。\n"
                f"确认条件：{snapshot['acceptance']}。尚未接单，请勿付款。\n"
                f"确认后按指引购买{snapshot['quantity']}件。保存本次绑定码：{q['binding_token']}（仅用于本报价）。\n"
                f"同意请发送【小单】确认 {q['order_id']} {q['version']}。")
        if op=='确认' and len(args)==3:
            existing=self.s.read_order(args[1],identity(c))
            if existing['accepted'] and (existing['status']!='ACCEPTED' or existing['payment']!='UNPAID'):
                return f"这笔订单已经进入 {existing['status']} / {existing['payment']}，请勿重复付款。"
            order=self.s.accept(args[1],int(args[2]),c)
            q=order['quote']
            binding='请在订单留言完整粘贴上条报价中的绑定码。' if q['auto_binding'] else '当前采用主人核验归属；付款后提供平台订单号，由主人核对，不凭截图认款。'
            return (f"已自动接单 {order['id']}，等待客户付款，主人通知已排队。\n"
                f"金额¥{q['amount_fen']/100:.2f}；{q['quantity']}件；{q['product_url']}\n"
                f"{binding}\n请在报价有效期内完成；超时须重新核对，勿重复付款。")
        if op in ('状态','结果','验收') and len(args)>=2:
            order=self.s.read_order(args[1],identity(c))
            if op=='状态':return f"{order['id']}：{order['status']} / {order['payment']} / {order['execution']}；{order['reason']}"
            if op=='验收':self.s.customer_close(order['id'],c);return '已记录本人验收，订单结单。'
            row=self.s.db.execute('SELECT result FROM jobs WHERE order_id=? AND status="SUCCEEDED"',(order['id'],)).fetchone()
            if not row:raise ValueError('result_not_ready')
            result=json.loads(row[0]);page=int(args[2]) if len(args)==3 else 1
            chunks=[result['text'][n:n+1200] for n in range(0,len(result['text']),1200)]
            if not 1<=page<=len(chunks):raise ValueError('result_page_invalid')
            return f"成果第{page}/{len(chunks)}页（只发送到原客户会话）：\n"+chunks[page-1]
        if op=='查款' and len(args)==3:
            self.s.read_order(args[1],identity(c));self.s.payment_hint(args[2],'customer_hint')
            return '已提交官方查单；知道订单号本身不能认领付款，核实前不会开工。'
        if op=='取消' and len(args)==2:
            result=self.s.cancel(args[1],c)
            return '已登记取消：'+result['status']+'；若已发过付款指引，须核对在途付款，预算暂不释放。'
        if op in ('暂停','恢复','通知状态'):
            if not owner:raise PermissionError('owner_required')
            return js(self.s.admin('pause' if op=='暂停' else 'resume',{}) if op!='通知状态' else self.s.status())
        raise ValueError('command_unsupported_use_help')

    def dispatch(self,r):
        with self.s.lock:
            return self._dispatch(r)

    def _dispatch(self,r):
        op=r.get('op')
        if op=='status':return self.s.status()
        if op=='channel':return self.channel(r['event'])
        # The following methods are only on the protected Unix socket, never HTTP.
        if op=='admin':return self.s.admin(r['action'],r.get('data',{}))
        if op=='order':return self.s.read_order(r['id'])
        if op=='review':
            return {'payments':[dict(x) for x in self.s.db.execute("SELECT trade_no,provider_paid,historical,allocation,reason FROM payments WHERE allocation!='BOUND' ORDER BY first_seen DESC LIMIT 20")],
                'outbox':[dict(x) for x in self.s.db.execute("SELECT event_key,kind,status,attempts FROM outbox WHERE status NOT IN ('sent') ORDER BY created LIMIT 20")]}
        if op=='import_bili':
            c=r['customer']
            if c.get('channel')!='bilibili_manual' or not r.get('basis'):raise ValueError('manual_import_basis_required')
            result=self.s.quote(c,r['task'],r['arguments']);self.s.audit('owner','manual_import',result['order_id'],{'basis':r['basis']})
            return result
        if op=='manual_accept':
            if not r.get('basis') or not r.get('customer_confirmed_at'):raise ValueError('acceptance_evidence_required')
            order=self.s.read_order(r['id'])
            if json.loads(order['customer'])['channel']!='bilibili_manual':raise ValueError('manual_accept_requires_manual_channel')
            return self.s.accept(r['id'],r['version'],order['customer'],{'basis':r['basis'],'confirmed_at':r['customer_confirmed_at']})
        if op=='manual_delivered':self.s.manual_delivery(r['id'],r['evidence']);return {'recorded':True}
        if op=='recheck':self.s.payment_hint(r['trade_no'],'owner');return {'queued':True}
        if op=='bind_payment':
            if not self.client:raise ValueError('payment_client_unavailable')
            evidence=self.client.query_order(r['trade_no'])
            return self.s.apply_payment(evidence,owner_order=r['id'],owner_evidence=r['evidence'])
        if op=='owner_probe':
            mode=r.get('mode','active_probe')
            if mode not in ('active_probe','passive_window'):raise ValueError('probe_mode')
            self.s.emit('owner-probe:'+str(int(self.s.now())),'','owner',{'stage':'probe','mode':mode,'reason':'仅验证主人私聊通知，没有订单和收入'})
            return {'queued':True}
        if op=='recover_result':return self.s.recover_saved_result(r['id'],r['basis'])
        if op=='reconcile_delivery':
            data=r['data']
            if data.get('observed')=='not_sent':
                result=self.transport({'op':'paid_reconcile_send','key':data['event_key'],'basis':data['basis']})
                data={**data,'core_reconciled':result.get('reconciled') is True}
            return self.s.admin('reconcile_delivery',data)
        raise ValueError('unsupported_operation')

    def deliver(self,row):
        payload=row['payload'];kind=row['kind'];oid=row['order_id']
        if kind=='activity':
            return self.transport({'op':'paid_activity','key':row['event_key'],'stage':payload['stage']})
        if kind=='owner':
            text='口粮小单通知：'+payload['stage']
            if oid:
                order=self.s.read_order(oid);q=order['quote'];c=json.loads(order['customer'])
                text+=f"\n{oid}｜{c['channel']}｜{q['task']}｜¥{q['amount_fen']/100:.2f}\n{order['status']} / {order['payment']}"
            if payload.get('reason'):text+='\n'+payload['reason']
            return self.transport({'op':'paid_send','key':row['event_key'],'owner':True,'text':text,
                'owner_mode':payload.get('mode',self.s.policy.get('owner_notification_mode','blocked'))})
        if kind=='customer_reply':
            c=payload['customer'];text=payload['text'];image=None
        elif kind=='delivery':
            order=self.s.read_order(oid);c=json.loads(order['customer']);result=payload['result']
            if c['channel']=='bilibili_manual':return {'status':'manual_pending','draft_available':True}
            text=f"{oid} 成果已生成：\n"+result['text'][:1200]+'\n完整内容可用【小单】结果 '+oid+' 1 分页领取；检查无误后回复【小单】验收 '+oid
            image=result.get('image_path')
        else:raise ValueError('outbox_kind')
        request={'op':'paid_send','key':row['event_key'],'owner':False,'customer':c,'text':text}
        if image:
            p=Path(image).resolve();base=(self.s.root/'artifacts'/oid).resolve()
            if p.parent!=base or p.is_symlink() or p.stat().st_size>2*1024*1024:raise ValueError('artifact_path')
            raw=p.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=payload['result']['image_sha256']:raise ValueError('artifact_integrity')
            request['image_base64']=base64.b64encode(raw).decode()
        return self.transport(request)

    def tick_outbox(self):
        row=self.s.claim_outbox()
        if not row:return
        try:
            result=self.deliver(row);status=result.get('status','unknown')
            self.s.outbox_result(row['event_key'],status,{'remote_id':result.get('remote_id',''),'reason':result.get('reason','')})
            if row['kind']=='owner' and row['payload']['stage']=='probe' and status=='sent':
                self.s.put('owner_probe_success',{'at':self.s.now(),'remote_id':result.get('remote_id',''),'mode':result.get('send_mode','unverified')})
        except (OSError,ValueError,TimeoutError):
            self.s.outbox_result(row['event_key'],'unknown',{'reason':'delivery_outcome_uncertain'})

    def model(self,task,text,budget,oid):
        if not self.s.policy.get('model_tasks_enabled'):raise ValueError('model_tasks_disabled')
        return self.transport({'op':'paid_generate','task':task,'text':text,'job_id':oid,'budget_microyuan':budget},timeout=100)

    async def loop(self):
        self.s.recover()
        await asyncio.gather(self.poll_loop(), self.outbox_loop(), self.job_loop())

    async def poll_loop(self):
        while not self.stopping:
            try:
                if self.reconciler and time.time()>=self.next_scan:
                    await asyncio.to_thread(self.reconciler.scan)
                    active=self.s.capacity()>0
                    self.next_scan=time.time()+self.s.policy['active_poll_seconds' if active else 'idle_poll_seconds']
                if self.reconciler:await asyncio.to_thread(self.reconciler.pending)
            except Exception:
                self.s.put('last_poll_error','bounded_operation_failed')
            await asyncio.sleep(2)

    async def outbox_loop(self):
        while not self.stopping:
            try:
                await asyncio.to_thread(self.tick_outbox)
            except Exception:
                self.s.put('last_delivery_error','bounded_operation_failed')
            await asyncio.sleep(1)

    async def job_loop(self):
        while not self.stopping:
            try:
                order=self.s.claim_job()
                if order:
                    try:
                        # Built-ins are bounded; model transport has its own timeout.
                        # Never abandon a running thread and claim another job.
                        result=await asyncio.to_thread(execute,order,self.s.root,self.model)
                        self.s.finish_job(order['id'],result,result['actual_microyuan'])
                    except Exception:
                        # No blind re-execution. Saved artifacts and reservations survive.
                        self.s.fail_job(order['id'],'executor_failed_or_timeout')
            except Exception:
                self.s.put('last_executor_error','bounded_operation_failed')
            await asyncio.sleep(2)

    async def handle(self,reader,writer):
        try:
            rawsock=writer.get_extra_info('socket')
            _,uid,_=struct.unpack('3i',rawsock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid not in (0,os.getuid()):raise PermissionError('peer_denied')
            data=await asyncio.wait_for(reader.readline(),5)
            if len(data)>65536 or not data.endswith(b'\n'):raise ValueError('packet_size')
            request=json.loads(data)
            response={'ok':True,**await asyncio.to_thread(self.dispatch,request)}
        except Exception as e:
            response={'ok':False,'error':str(e)[:100] if isinstance(e,(ValueError,PermissionError)) else type(e).__name__}
        writer.write(js(response).encode()+b'\n');await writer.drain();writer.close();await writer.wait_closed()

def webhook_handler(app,secret):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0'
        def log_message(self,*args):pass
        def setup(self):
            super().setup();self.connection.settimeout(5)
        def answer(self,code,body):
            raw=js(body).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            if self.path!='/webhooks/afdian/'+secret:return self.answer(404,{'ec':404})
            try:
                if self.headers.get('Transfer-Encoding'):raise ValueError('chunked_not_supported')
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=65536:raise ValueError('body_limit')
                with app.s.lock:
                    slot=int(time.time()//60);count=app.s.get('http_rate',{})
                    used=count.get('used',0) if count.get('slot')==slot else 0
                    if used>=30:return self.answer(429,{'ec':429})
                    app.s.put('http_rate',{'slot':slot,'used':used+1})
                body=json.loads(self.rfile.read(n));data=body.get('data',{})
                if body.get('ec')!=200 or data.get('type')!='order':raise ValueError('event_schema')
                app.s.payment_hint(data['order']['out_trade_no'])
                self.answer(200,{'ec':200,'em':''})
            except Exception:self.answer(400,{'ec':400,'em':'invalid_or_unavailable'})
        def do_GET(self):
            self.answer(200,{'healthy':True}) if self.path=='/health' else self.answer(404,{'ec':404})
    return Handler

async def serve(root=ROOT):
    root=Path(root);p=validate_policy(json.loads((root/'policy.json').read_text()))
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lockfile=(root/'service.lock').open('a')
    fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if os.environ.get('AFDIAN_CREATOR_USER_ID'):p['creator_id']=os.environ['AFDIAN_CREATOR_USER_ID']
    store=Store(root,p)
    try:client=AfdianClient()
    except ProviderError:client=None
    app=Application(store,client)
    path=root/'paid.sock'
    if path.exists():
        try:
            with socket.socket(socket.AF_UNIX) as c:c.connect(str(path))
            raise RuntimeError('existing_paid_service')
        except ConnectionRefusedError:path.unlink()
    server=await asyncio.start_unix_server(app.handle,str(path),limit=65537);path.chmod(0o600)
    http=None
    if p.get('webhook_enabled'):
        secret=os.environ.get('AFDIAN_WEBHOOK_PATH_SECRET','')
        if not re.fullmatch(r'[a-f0-9]{32,64}',secret):raise ValueError('webhook_secret_required')
        http=BoundedHTTPServer(('127.0.0.1',p['webhook_port']),webhook_handler(app,secret));http.daemon_threads=True
        threading.Thread(target=http.serve_forever,daemon=True).start()
    try:await app.loop()
    finally:
        if http:http.shutdown()
        server.close();await server.wait_closed();path.unlink(missing_ok=True);store.close();lockfile.close()

class BoundedHTTPServer(ThreadingHTTPServer):
    request_queue_size=8
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(*args,**kwargs)
    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:super().process_request(request,client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()

if __name__=='__main__':asyncio.run(serve())
