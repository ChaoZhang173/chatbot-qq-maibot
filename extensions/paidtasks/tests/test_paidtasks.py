import concurrent.futures
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from paidtasks.afdian import AfdianClient, ProviderError, normalize, signed_body, fen
from paidtasks.store import Store, identity
from paidtasks.reconcile import Reconciler
from paidtasks.executor import execute, arithmetic
from paidtasks.runtime import Application

ROOT=Path(__file__).resolve().parents[1]

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=1800000000.0
        self.a={'channel':'qq','account':'bot','conversation':'groupA','actor':'alice'}
        self.b={**self.a,'actor':'bob'}
        self.p=json.loads((ROOT/'config.example.json').read_text())
        self.p.update(run_mode='pilot',intake_enabled=True,creator_id='creator',plan_id='rice',sku_id='one',
            product_url='https://afdian.com/item/synthetic',product_verified=True,remark_verified=True,
            owner_notification_verified=True,owner_notification_mode='active_probe',pilot_allowlist=[identity(self.a),identity(self.b)],initial_budget_microyuan=1000000)
        self.s=Store(self.tmp.name,self.p,now=lambda:self.clock);self.s.put('bootstrap_complete',True)

    def tearDown(self):self.s.close();self.tmp.cleanup()

    def quote(self,c=None,task='text_cleanup',arguments=None):
        q=self.s.quote(c or self.a,task,arguments or {'text':' a \n\n b '})
        self.s.accept(q['order_id'],q['version'],c or self.a)
        return q

    def evidence(self,q,trade='T00001',**kwargs):
        raw={'out_trade_no':trade,'user_id':'payer','plan_id':'rice','total_amount':f"{q['quote']['amount_fen']/100:.2f}",
            'status':2,'product_type':1,'sku_detail':[{'sku_id':'one','count':q['quote']['quantity']}],
            'redeem_id':'','discount':'0.00','remark':q['binding_token']}
        raw.update(kwargs)
        return normalize('creator',raw,self.clock)

class APIContract(unittest.TestCase):
    def test_official_sign_vector(self):
        b=signed_body('abc','123',{'a':333},1624339905)
        self.assertEqual(b['sign'],'a4acc28b81598b7e5d84ebdc3e91710c')
        self.assertEqual(b['params'],'{"a":333}')

    def test_amount_never_float_or_rounded(self):
        self.assertEqual(fen('1.01'),101)
        for bad in [1.0,'1.001','NaN','-1','1e2']:
            with self.assertRaises(ValueError):fen(bad)

    def test_retry_bounded_and_error_redacted(self):
        calls=[]
        def transport(*args):calls.append(1);raise ProviderError('unavailable')
        c=AfdianClient('creator','synthetic-token',transport=transport,sleep=lambda _:None)
        with self.assertRaises(ProviderError):c.ping()
        self.assertEqual(len(calls),3)
        c=AfdianClient('creator','synthetic-token',transport=lambda *a:{'ec':400005,'data':{'debug':'synthetic-token'}})
        with self.assertRaisesRegex(ProviderError,'configuration_rejected'):c.ping()

    def test_query_requires_exact_order_and_business_ec(self):
        c=AfdianClient('creator','token',transport=lambda *a:{'ec':200,'data':{'list':[]}},attempts=1)
        with self.assertRaises(ProviderError):c.query_order('T123')

class PaymentAndBudget(Base):
    def test_success_once_delivery_and_close_are_separate(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));o=self.s.claim_job()
        result=execute(o,self.s.root);self.s.finish_job(o['id'],result,0)
        self.assertEqual(self.s.read_order(o['id'])['status'],'DELIVERABLE_READY')
        with self.assertRaises(ValueError):self.s.customer_close(o['id'],self.a)
        row=self.s.db.execute("SELECT * FROM outbox WHERE event_key=?",('delivery:'+o['id'],)).fetchone()
        self.s.db.execute("UPDATE outbox SET status='sending' WHERE event_key=?",(row['event_key'],))
        self.s.outbox_result(row['event_key'],'sent',{'remote_id':'fake-receipt'})
        self.s.customer_close(o['id'],self.a)
        self.assertEqual(self.s.read_order(o['id'])['status'],'CLOSED')
        self.assertEqual(self.s.available_budget(),1000000)

    def test_two_ten_yuan_customers_do_not_swap(self):
        self.p.update(max_order_fen=1000,quantity_verified=True,task_prices_fen={'text_cleanup':1000})
        a=self.quote(self.a);b=self.quote(self.b)
        ra=self.s.apply_payment(self.evidence(b,'T_B'));rb=self.s.apply_payment(self.evidence(a,'T_A'))
        self.assertEqual(ra['order_id'],b['order_id']);self.assertEqual(rb['order_id'],a['order_id'])
        self.assertEqual(b['quote']['quantity'],10)

    def test_concurrent_webhook_poll_duplicates(self):
        q=self.quote();e=self.evidence(q)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda _:self.s.apply_payment(e),range(32)))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM ledger WHERE kind='customer_paid'").fetchone()[0],1)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM outbox WHERE event_key=?",('paid:'+q['order_id'],)).fetchone()[0],1)

    def test_forged_webhook_only_enqueues_lookup(self):
        self.quote();self.s.payment_hint('FORGED1')
        self.assertIsNone(self.s.claim_job())
        with self.assertRaises(ValueError):self.s.apply_payment({'status':2})

    def test_product_amount_and_quantity_mismatches_keep_paid_facts(self):
        cases=[{'total_amount':'0.00'},{'total_amount':'2.00'},{'total_amount':'0.99'},
            {'plan_id':'other'},{'sku_detail':[{'sku_id':'wrong','count':1}]},
            {'sku_detail':[{'sku_id':'one','count':2}]},
            {'sku_detail':[{'sku_id':'one','count':1},{'sku_id':'extra','count':1}]},
            {'redeem_id':'redeemed'},{'discount':'0.01'},{'product_type':0}]
        q=self.quote()
        for i,case in enumerate(cases):
            result=self.s.apply_payment(self.evidence(q,'BAD'+str(i),**case));self.assertFalse(result['bound'])
        self.assertEqual(self.s.db.execute('SELECT sum(provider_paid) FROM payments').fetchone()[0],len(cases))
        self.assertIsNone(self.s.claim_job())

    def test_unaccepted_old_expired_and_unknown_binding(self):
        q=self.s.quote(self.a,'text_cleanup',{'text':'x'})
        self.assertFalse(self.s.apply_payment(self.evidence(q,'U1'))['bound'])
        newer=self.s.quote(self.a,'text_cleanup',{'text':'y'},prior=q['order_id'])
        self.s.accept(newer['order_id'],newer['version'],self.a)
        self.assertFalse(self.s.apply_payment(self.evidence(q,'OLD'))['bound'])
        self.clock+=2000
        self.assertFalse(self.s.apply_payment(self.evidence(newer,'LATE'))['bound'])
        self.assertEqual(self.s.db.execute('SELECT status FROM reservations').fetchone()[0],'held')
        self.assertFalse(self.s.apply_payment(self.evidence(newer,'FAKE',remark='PT-'+'F'*32))['bound'])

    def test_quote_immutable(self):
        q=self.quote()
        with self.assertRaises(Exception):self.s.db.execute('UPDATE quotes SET expires=0 WHERE id=?',(q['order_id'],))

    def test_manual_binding_still_checks_official_amount(self):
        self.p['remark_verified']=False;q=self.quote();e=self.evidence(q)
        self.assertFalse(self.s.apply_payment(e)['bound'])
        evidence={'basis':'customer identity verified by owner','timing_reviewed':True}
        self.assertFalse(self.s.apply_payment(self.evidence(q,'WRONG',total_amount='9.00'),owner_order=q['order_id'],owner_evidence=evidence)['bound'])
        self.assertTrue(self.s.apply_payment(e,owner_order=q['order_id'],owner_evidence=evidence)['bound'])

    def test_paid_state_survives_missing_budget_reservation(self):
        q=self.quote();self.s.db.execute('DELETE FROM reservations WHERE order_id=?',(q['order_id'],))
        self.s.apply_payment(self.evidence(q));o=self.s.read_order(q['order_id'])
        self.assertEqual(o['payment'],'PAID_VERIFIED');self.assertEqual(o['status'],'NEEDS_ATTENTION')

    def test_unauthorized_customer_cannot_read_or_accept(self):
        q=self.quote()
        with self.assertRaises(PermissionError):self.s.read_order(q['order_id'],identity(self.b))
        with self.assertRaises(PermissionError):self.s.accept(q['order_id'],1,self.b)
        with self.assertRaises(PermissionError):Application(self.s).command(self.b,'暂停',False)

    def test_budget_atomic_and_unknown_not_zero_across_day(self):
        self.p['max_outstanding_orders']=10
        quotes=[self.s.quote(self.a,'text_cleanup',{'text':str(i)}) for i in range(8)]
        def accept(q):
            try:self.s.accept(q['order_id'],1,self.a);return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:accepted=list(pool.map(accept,quotes))
        self.assertEqual(sum(accepted),4);self.assertEqual(self.s.available_budget(),0)
        q=next(q for q,yes in zip(quotes,accepted) if yes);self.s.apply_payment(self.evidence(q))
        o=self.s.claim_job();self.s.finish_job(o['id'],{'text':'answer','sha256':'synthetic'},None)
        self.clock+=86400
        self.assertEqual(self.s.available_budget(),0)

    def test_pause_keeps_paid_and_queued(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));self.s.admin('pause',{})
        self.assertIsNone(self.s.claim_job());self.assertEqual(self.s.read_order(q['order_id'])['payment'],'PAID_VERIFIED')
        self.s.admin('resume',{});self.assertIsNotNone(self.s.claim_job())

    def test_running_and_ambiguous_send_not_blindly_retried(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));self.s.claim_job();row=self.s.claim_outbox()
        self.s.recover()
        self.assertEqual(self.s.db.execute('SELECT status FROM jobs').fetchone()[0],'INTERRUPTED')
        self.assertEqual(self.s.db.execute('SELECT status FROM outbox WHERE event_key=?',(row['event_key'],)).fetchone()[0],'unknown')
        self.assertIsNone(self.s.claim_job())

    def test_delivery_retry_preserves_result(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));o=self.s.claim_job();r=execute(o,self.s.root);self.s.finish_job(o['id'],r,0)
        key='delivery:'+o['id'];self.s.db.execute("UPDATE outbox SET status='sending',attempts=1 WHERE event_key=?",(key,))
        self.s.outbox_result(key,'rejected',{'code':'synthetic-reject'})
        self.assertEqual(self.s.read_order(o['id'])['status'],'DELIVERY_PENDING')
        self.assertTrue(Path(r['text_path']).exists());self.assertIsNone(self.s.claim_job())

    def test_stale_owner_notifications_pause_new_commitments(self):
        self.quote();self.clock+=601
        with self.assertRaisesRegex(ValueError,'owner_notification_stale'):self.s.quote(self.b,'text_cleanup',{'text':'x'})

    def test_duplicate_channel_input_does_not_create_two_quotes(self):
        app=Application(self.s)
        event={'customer':self.a,'message_id':'MID1','text':'【小单】报价 '+json.dumps({'task':'text_cleanup','arguments':{'text':'x'}})}
        app.channel(event);app.channel(event)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM quotes').fetchone()[0],1)

    def test_manual_bili_acceptance_and_delivery_are_distinct(self):
        c={'channel':'bilibili_manual','account':'bilibot','conversation':'manual1','actor':'buyer'}
        self.p['pilot_allowlist'].append(identity(c));app=Application(self.s)
        q=app.dispatch({'op':'import_bili','customer':c,'task':'text_cleanup','arguments':{'text':'x'},'basis':'owner imported inquiry'})
        self.assertIsNone(self.s.read_order(q['order_id'])['accepted'])
        app.dispatch({'op':'manual_accept','id':q['order_id'],'version':1,'basis':'customer message accepts this quote','customer_confirmed_at':self.clock})
        self.s.apply_payment(self.evidence(q));o=self.s.claim_job();result=execute(o,self.s.root);self.s.finish_job(o['id'],result,0)
        with self.assertRaises(ValueError):self.s.manual_delivery(o['id'],{'recipient':'wrong','sent_at':self.clock,'artifact_sha256':result['sha256']})
        self.s.manual_delivery(o['id'],{'recipient':'buyer','sent_at':self.clock,'artifact_sha256':result['sha256']})
        self.assertEqual(self.s.read_order(o['id'])['status'],'SENT')

class PollAndArtifacts(Base):
    def test_authorized_budget_retry_does_not_allocate_twice(self):
        request={'microyuan':1000000,'allocation_id':'pilot-test-only'}
        self.s.admin('add_budget',request);self.s.admin('add_budget',request)
        self.assertEqual(self.s.get('budget_total'),2000000)
        with self.assertRaises(ValueError):self.s.admin('add_budget',{**request,'microyuan':2000000})

    def test_repeat_confirmation_after_payment_never_invites_second_payment(self):
        q=self.quote();self.s.apply_payment(self.evidence(q))
        reply=Application(self.s).command(self.a,'确认 '+q['order_id']+' 1')
        self.assertIn('请勿重复付款',reply)
        self.assertNotIn(self.p['product_url'],reply)
        with self.assertRaises(ValueError):self.s.accept(q['order_id'],1,self.a)

    def test_support_income_is_separate_from_task_and_budget(self):
        evidence=normalize('creator',{'out_trade_no':'DONATION','user_id':'payer','plan_id':'support',
            'total_amount':'1.00','status':2,'product_type':0},self.clock)
        answer=self.s.apply_payment(evidence)
        self.assertTrue(answer['support']);self.assertFalse(answer['bound'])
        self.assertEqual(self.s.accounting()['support_received_fen'],100)
        self.assertEqual(self.s.accounting()['customer_paid_fen'],0)
        self.assertEqual(self.s.available_budget(),1000000)
        self.assertIsNone(self.s.claim_job())

    def test_passive_owner_mode_requires_consent_and_fresh_private_command(self):
        self.p.update(owner_notification_mode='passive_window',passive_window_accepted=False)
        with self.assertRaises(ValueError):self.quote()
        self.p['passive_window_accepted']=True
        with self.assertRaises(ValueError):self.quote()
        self.s.put('owner_last_inbound',self.clock);self.quote()
        self.clock+=3001
        with self.assertRaises(ValueError):self.quote()

    def test_pagination_baseline_and_restart_catchup(self):
        self.s.put('bootstrap_complete',False);self.p['scan_pages_per_tick']=2
        raw={'out_trade_no':'HIST','user_id':'p','plan_id':'rice','total_amount':'1.00','status':2,'product_type':1,'sku_detail':[{'sku_id':'one','count':1}]}
        class Client:
            creator='creator'
            def list_orders(self,page):return ([dict(raw,out_trade_no='H'+str(page))],4)
        rec=Reconciler(self.s,Client())
        self.assertFalse(rec.scan()['complete']);self.assertEqual(self.s.get('scan_page'),2)
        self.assertFalse(rec.scan()['complete']);self.assertTrue(rec.scan()['complete'])
        self.assertEqual(self.s.db.execute('SELECT sum(historical) FROM payments').fetchone()[0],4)
        self.assertIsNone(self.s.claim_job())

    def test_query_failure_then_recovery(self):
        q=self.quote();e=self.evidence(q);self.s.payment_hint(e.trade_no)
        class Client:
            fail=True
            def query_order(self,trade):
                if self.fail:raise ProviderError('offline')
                return e
        c=Client();rec=Reconciler(self.s,c);rec.pending()
        self.assertIsNone(self.s.claim_job());c.fail=False;self.clock+=1000;rec.pending()
        self.assertIsNotNone(self.s.claim_job())

    def test_real_png_renderer_and_no_eval(self):
        q=self.quote(task='render_formula',arguments={'expression':'(12+8)/4'})
        self.s.apply_payment(self.evidence(q));o=self.s.claim_job();result=execute(o,self.s.root)
        from PIL import Image
        with Image.open(result['image_path']) as im:im.verify()
        self.assertEqual(result['text'],'(12+8)/4 = 5')
        for text in ['__import__("os")','2**1000000','1/0']:
            with self.assertRaises((ValueError,SyntaxError)):arithmetic(text)

    def test_saved_result_recovers_without_generation(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));order=self.s.claim_job()
        result=execute(order,self.s.root);self.s.recover()
        before=Path(result['text_path']).stat().st_mtime_ns
        answer=self.s.recover_saved_result(order['id'],'owner checked saved output and hashes')
        self.assertFalse(answer['regenerated'])
        self.assertEqual(Path(result['text_path']).stat().st_mtime_ns,before)
        self.assertEqual(self.s.read_order(order['id'])['status'],'DELIVERABLE_READY')

    def test_tampered_result_cannot_be_recovered(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));order=self.s.claim_job()
        result=execute(order,self.s.root);self.s.recover();Path(result['text_path']).write_text('altered')
        with self.assertRaisesRegex(ValueError,'integrity'):self.s.recover_saved_result(order['id'],'owner requests recovery')
        self.assertEqual(self.s.read_order(order['id'])['status'],'NEEDS_ATTENTION')

    def test_cancel_retains_inflight_budget_and_blocks_execution(self):
        q=self.quote();before=self.s.available_budget();self.s.cancel(q['order_id'],self.a)
        self.assertEqual(self.s.available_budget(),before)
        self.assertFalse(self.s.apply_payment(self.evidence(q))['bound'])
        self.assertIsNone(self.s.claim_job())

    def test_unknown_send_requires_evidence_before_retry(self):
        self.s.emit('probe','','owner',{'stage':'probe'})
        row=self.s.claim_outbox();self.s.outbox_result(row['event_key'],'unknown')
        with self.assertRaises(ValueError):self.s.admin('retry_delivery',{'event_key':'probe'})
        self.s.admin('reconcile_delivery',{'event_key':'probe','basis':'owner checked conversation history','observed':'not_sent','core_reconciled':True})
        self.assertEqual(self.s.claim_outbox()['event_key'],'probe')

    def test_refund_while_running_holds_generated_result(self):
        q=self.quote();self.s.apply_payment(self.evidence(q));order=self.s.claim_job()
        self.s.admin('record_refund',{'order_id':order['id'],'amount_fen':100,'basis':'owner platform refund confirmation'})
        result=execute(order,self.s.root);self.s.finish_job(order['id'],result,0)
        self.assertEqual(self.s.read_order(order['id'])['status'],'NEEDS_ATTENTION')
        self.assertFalse(self.s.db.execute("SELECT 1 FROM outbox WHERE kind='delivery'").fetchone())

    def test_delivered_quote_token_removed_from_outbox(self):
        app=Application(self.s)
        app.channel({'customer':self.a,'message_id':'private-quote','text':'【小单】报价 {"task":"text_cleanup","arguments":{"text":"x"}}'})
        row=self.s.claim_outbox();self.assertIn('PT-',row['payload']['text'])
        self.s.outbox_result(row['event_key'],'sent',{'remote_id':'synthetic'})
        self.assertEqual(self.s.db.execute('SELECT payload FROM outbox WHERE event_key=?',(row['event_key'],)).fetchone()[0],'{}')

if __name__=='__main__':unittest.main()
