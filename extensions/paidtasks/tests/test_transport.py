import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import threading
import types
import unittest
import urllib.error
import urllib.request
from test_paidtasks import Base
from paidtasks.runtime import Application, BoundedHTTPServer, webhook_handler

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('paid_bridge_test',ROOT/'integration/paid_bridge.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class HTTPIngress(Base):
    def test_webhook_ack_is_durable_hint_not_payment(self):
        app=Application(self.s)
        server=BoundedHTTPServer(('127.0.0.1',0),webhook_handler(app,'test-path-only'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            event={'ec':200,'data':{'type':'order','order':{'out_trade_no':'FORGED','status':2,'total_amount':'1.00'}}}
            for _ in range(3):
                request=urllib.request.Request(base+'/webhooks/afdian/test-path-only',data=json.dumps(event).encode())
                with urllib.request.urlopen(request,timeout=2) as reply:self.assertEqual(json.load(reply)['ec'],200)
            self.assertEqual(self.s.db.execute('SELECT count(*) FROM inbound').fetchone()[0],1)
            self.assertEqual(self.s.db.execute('SELECT count(*) FROM payments').fetchone()[0],0)
            self.assertIsNone(self.s.claim_job())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base+'/webhooks/afdian/wrong',data=b'{}'))
            self.assertEqual(error.exception.code,404)
        finally:server.shutdown();server.server_close();thread.join()

class BridgeDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class State:
            def __init__(self):self.values={};self.activities=[]
            def get(self,key):return self.values.get(key)
            def put(self,key,value):self.values[key]=value
            def record_activity(self,value):self.activities.append(value)
        self.calls=[]
        async def text(content,session,**kw):
            self.calls.append(('text',session,kw));return {'sent':True,'message_id':'synthetic-text'}
        async def image(content,session,**kw):
            self.calls.append(('image',session,kw));return {'sent':False}
        self.service=types.SimpleNamespace(state=State(),policy={'paid_tasks_bridge_enabled':True,'qq_bot_account':'bot','allowed_qq_sessions':['allowed']},ctx=types.SimpleNamespace(send=types.SimpleNamespace(text=text,image=image)))
        self.bridge=module.PaidBridge(self.service)
        self.request={'op':'paid_send','key':'delivery:test','owner':False,'customer':{'channel':'qq','account':'bot','conversation':'allowed','actor':'alice'},'text':'test'}

    async def test_same_delivery_once_and_changed_payload_denied(self):
        self.assertEqual((await self.bridge.dispatch(self.request))['status'],'sent')
        await self.bridge.dispatch(self.request)
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.calls[0][2]['storage_message'])
        with self.assertRaises(ValueError):await self.bridge.dispatch({**self.request,'text':'changed'})

    async def test_partial_image_failure_does_not_replay_text(self):
        request={**self.request,'image_base64':base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic').decode()}
        self.assertEqual((await self.bridge.dispatch(request))['status'],'unknown')
        self.assertEqual((await self.bridge.dispatch(request))['status'],'unknown')
        self.assertEqual([x[0] for x in self.calls],['text','image'])
        self.assertFalse(self.bridge.in_flight)

    async def test_unbound_owner_and_wrong_channel_never_send(self):
        self.bridge.owners=lambda:[]
        self.assertEqual((await self.bridge.dispatch({**self.request,'owner':True}))['status'],'rejected')
        with self.assertRaises(ValueError):await self.bridge.dispatch({**self.request,'customer':{**self.request['customer'],'conversation':'other'}})
        self.assertEqual(self.calls,[])

    async def test_shared_memory_only_has_anonymous_status(self):
        await self.bridge.dispatch({'op':'paid_activity','key':'paid:test','stage':'paid','customer':'secret customer','text':'secret request'})
        row=self.service.state.activities[0]
        self.assertNotIn('secret',json.dumps(row))
        self.assertEqual(row['sharing_scope'],'public')

if __name__=='__main__':unittest.main()
