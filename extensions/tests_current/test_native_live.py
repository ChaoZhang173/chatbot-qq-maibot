import asyncio,base64,copy,io,json,tempfile,time,unittest,wave
from pathlib import Path
from datetime import datetime
from phase2.core import TZ
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from PIL import Image
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from maibot_unified_core.private_access import PrivateAccess
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.qq_proactive import ProactiveQQ,window
from maibot_unified_core.live_observe import observe,validate
from maibot_unified_core.live_evidence import fresh as core_fresh
from phase2.live_media import fresh,DEFAULT
from phase2.live_observer import allowed_url,budget_window
from test_live_chat import LiveTests

def evidence(now):
    return {'status':'observed','room_id':'123','visit_id':'a'*64,'sample_id':'b'*64,
            'started_at':now-12,'ended_at':now,'audio_status':'ok','audio_text':'这局我们走左边',
            'visual_status':'ok','visual_summary':'游戏角色站在岔路口','uncertainty':'无法判断下一步'}

class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_stale_wrong_room_failed_channel_and_nonfinite_rejected(self):
        now=time.time();e=evidence(now)
        for check in (fresh,core_fresh):
            self.assertIsNotNone(check(e,'123','a'*64,now))
            for change in ({'room_id':'124'},{'visit_id':'c'*64},{'ended_at':now-90,'started_at':now-102},{'started_at':float('nan')},{'status':'failed'}):
                self.assertIsNone(check({**e,**change},'123','a'*64,now))
            r=check({**e,'audio_status':'failed'},'123','a'*64,now);self.assertEqual(r['audio_text'],'')
            self.assertIsNone(check({**e,'audio_status':'failed','visual_status':'failed'},'123','a'*64,now))
    def test_public_stream_only(self):
        for url in ('file:///etc/passwd','https://evil.invalid/live','http://127.0.0.1/live','https://cdn.bilivideo.com.evil.invalid/live'):
            self.assertFalse(allowed_url(url))
        with patch('socket.getaddrinfo',return_value=[(0,0,0,'',('127.0.0.1',443))]):self.assertFalse(allowed_url('https://cdn.bilivideo.com/live'))
        with patch('socket.getaddrinfo',return_value=[]):self.assertFalse(allowed_url('https://cdn.bilivideo.com/live'))
    async def test_real_jpeg_wav_validation_and_modality_failure(self):
        image=io.BytesIO();Image.new('RGB',(32,32),'blue').save(image,format='JPEG')
        audio=io.BytesIO()
        with wave.open(audio,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\x01\x10'*16000)
        now=time.time();request={'room_id':'123','visit_id':'a'*64,'started_at':now-1,'ended_at':now,
            'frames':[{'offset':0,'jpeg_base64':base64.b64encode(image.getvalue()).decode()}],'audio_base64':base64.b64encode(audio.getvalue()).decode()}
        s=SimpleNamespace(policy={'bili_live_enabled':True,'bili_live_media_enabled':True},reload_policy=lambda:None,
            ctx=SimpleNamespace(llm=SimpleNamespace(transcribe_audio=AsyncMock(return_value={'success':True,'text':'<|zh|>向左走'}))),
            generate=AsyncMock(side_effect=RuntimeError('vision unavailable')))
        r=(await observe(s,request))['observation'];self.assertEqual(r['audio_text'],'向左走');self.assertEqual(r['visual_status'],'failed');self.assertEqual(r['visual_summary'],'')
        s.ctx.llm.transcribe_audio.return_value={'success':True,'text':'あ。'}
        r=(await observe(s,request))['observation'];self.assertEqual(r['audio_status'],'unclear');self.assertEqual(r['audio_text'],'')
        with self.assertRaises(ValueError):validate({**request,'ended_at':now-100,'started_at':now-101})
        s.policy['bili_live_media_enabled']=False
        with self.assertRaises(PermissionError):await observe(s,request)

class MediaSendTests(LiveTests):
    def test_media_expired_while_generating_never_sends(self):
        self.c['live_media']={**DEFAULT,'enabled':True};self.path.write_text(json.dumps(self.c));self.enter()
        original=self.e.unified_request
        def core(payload):
            r=original(payload);self.now+=65;return r
        self.e.unified_request=core
        with patch('phase2.live.read_observation',side_effect=lambda c,v,now:{**evidence(now),'visit_id':v['id']}):
            self.assertEqual(self.chat()['status'],'stale_live_observation')
        self.assertFalse(self.api.sent)

class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p={'qq_bot_account':'9000000001','bot_accounts':{'qq':'9000000001'},'qq_private_familiar_enabled':True,
            'qq_bili_commands_enabled':True,'qq_owner_bindings':{},'qq_group_sessions':{},'allowed_qq_sessions':[],
            'qq_native':{'enabled':True,'proactive_enabled':True,'account':'9000000001','owner':'9000000002','groups':['9000000003']}}
        (self.root/'policy.json').write_text(json.dumps(self.p))
        async def opened(**kw):return {'success':True,'session_id':'group' if kw['chat_type']=='group' else 'private'}
        self.ctx=SimpleNamespace(chat=SimpleNamespace(open_session=AsyncMock(side_effect=opened)),send=SimpleNamespace(text=AsyncMock(return_value={'sent':True,'message_id':'receipt'})))
        self.s=UnifiedService(self.ctx,self.root);self.s.policy=self.p;self.s.state=State(self.root/'state.sqlite',self.p)
        self.s.state.init_persona({'identity':'大肥鱼'},{'interests':['游戏']})
        self.s.private_access=PrivateAccess(self.s);self.s.commands=CommandQueue(self.s);self.q=ProactiveQQ(self.s)
        self.s.build_context=AsyncMock(return_value={'persona':self.s.state.persona(),'source_ids':[],'facts':[],'activities':[]})
        self.s.generate=AsyncMock(return_value={'willing':True,'text':'我又想到一个游戏里的有趣配装。'})
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    async def ready(self):
        await self.q.provision();self.s.state.put('qq_native_ready_at',time.time()-700)
    async def test_exact_owner_private_commands_and_scope(self):
        await self.ready();c=self.s.commands
        self.assertTrue(c.allowed_session('private','9000000002'));self.assertTrue(c.owner('private','9000000002'))
        self.assertFalse(c.allowed_session('private','someone'));self.assertFalse(c.allowed_session('other'))
        message={'platform':'qq','account_id':'9000000001','session_id':'private','scope':'private','message_id':'m1','timestamp':time.time(),
            'message_info':{'user_info':{'user_id':'9000000002'}},'raw_message':[{'type':'text','data':'【命令】进入直播间 https://live.bilibili.com/123'}]}
        key=c.capture(message);self.assertIsNotNone(key);self.assertTrue(c.claim()['job']['forced'])
        with self.assertRaises(ValueError):c.discretionary('other','点赞 BV1ehHQ6bErB')
    async def test_unsolicited_sends_use_only_allowlist_and_no_restart_repeat(self):
        # Pin one overnight quota window; wall-clock runs may cross day/night.
        with patch('maibot_unified_core.qq_proactive.time.time',return_value=datetime(2026,10,31,23,30,tzinfo=TZ).timestamp()):
            await self.ready();r=await self.q.tick();self.assertEqual(r['status'],'sent');self.assertEqual(self.ctx.send.text.call_args.args[1],'group')
            self.assertFalse(self.q.eligible(self.q.destinations()[0],time.time()+60))
            q2=ProactiveQQ(self.s);later=time.time()+7200
            self.assertTrue(q2.eligible(q2.destinations()[0],later))
            self.s.generate.return_value={'willing':True,'text':'突然想到拿史莱姆当闹钟会怎样。'}
            with patch('maibot_unified_core.qq_proactive.time.time',return_value=later):
                self.assertEqual((await q2.tick())['status'],'sent')
            self.assertEqual(self.ctx.send.text.await_count,2)
            payload=json.loads(self.s.generate.call_args.args[0][1]['content'])
            self.assertEqual(len(payload['own_recent_topics']),1)
            self.assertFalse(q2.eligible(q2.destinations()[0],later+7200))
            with self.assertRaises(ValueError):await self.q.send('x',{'kind':'private','target':'11111','session':'evil'},'hello')
    async def test_recent_exchange_waits_briefly_without_requiring_a_reply(self):
        await self.ready();d=self.q.destinations()[0];now=time.time()
        with patch.object(self.q,'latest_external',return_value=now-60):self.assertFalse(self.q.eligible(d,now))
        with patch.object(self.q,'latest_external',return_value=now-121):self.assertTrue(self.q.eligible(d,now))
    async def test_revocation_or_new_message_during_generation_prevents_send(self):
        await self.ready()
        async def generate(*a,**kw):
            p=json.loads((self.root/'policy.json').read_text());p['qq_native']['proactive_enabled']=False;(self.root/'policy.json').write_text(json.dumps(p))
            return {'willing':True,'text':'本来想聊游戏'}
        self.s.generate=generate;self.assertEqual((await self.q.tick())['status'],'cancelled');self.ctx.send.text.assert_not_called()
    async def test_unknown_delivery_test_is_never_repeated(self):
        await self.ready();self.ctx.send.text.side_effect=TimeoutError('unknown')
        await self.q.connection_test();self.assertEqual(self.ctx.send.text.await_count,2)
        await ProactiveQQ(self.s).connection_test();self.assertEqual(self.ctx.send.text.await_count,2)
    async def test_new_incoming_message_cancels_stale_voluntary_send(self):
        await self.ready()
        async def generate(*a,**kw):
            self.s.state.ingest({'event_id':'new','platform':'qq','bot_account_id':'9000000001','conversation_id':'group',
                'actor_id':'member','origin':'external_user','event_type':'message_received','content':'刚发来的消息','occurred_at':time.time()})
            return {'willing':True,'text':'先前决定的话题'}
        self.s.generate=generate;self.assertEqual((await self.q.tick())['status'],'cancelled');self.ctx.send.text.assert_not_called()
    async def test_night_window_does_not_reset_at_midnight(self):
        from datetime import datetime
        from maibot_unified_core.store import CHINA
        before=datetime(2026,10,31,23,50,tzinfo=CHINA).timestamp();after=before+1200
        self.assertEqual(window(before),window(after));self.assertEqual(budget_window(before),budget_window(after))
    async def test_private_scope_and_stop(self):
        await self.ready();self.q.observe('group','member','不要主动发消息');self.assertFalse(self.q.eligible(self.q.destinations()[0],time.time()))
        self.q.observe('group','9000000002','恢复主动聊天');self.assertTrue(self.q.eligible(self.q.destinations()[0],time.time()))
        source={'platform':'qq','conversation_id':'private','sharing_scope':'conversation','is_private':True}
        self.assertFalse(self.s.state.allowed(source,'qq','group'));self.assertFalse(self.s.state.allowed(source,'bilibili','live:123','public'))

if __name__=='__main__':unittest.main()
