import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock,Mock
from types import SimpleNamespace
from phase2.core import DEFAULTS,TZ,day,machine_id,PlatformError,Denied
from phase2.runner import Engine
from phase2.live import LiveChat,LiveAPI,DEFAULT,policy
from maibot_unified_core.live import plan
from test_engagement import API

class RNG:
    def randint(self,a,b):return a
    def choice(self,x):return x[0]
    def shuffle(self,x):pass
    def random(self):return 0.9

class FakeLive:
    def __init__(self):
        self.rows=[];self.sent=[];self.error=None;self.read_error=None
        self.info={'room_id':'123','uid':'200','title':'游戏技术交流','area':'游戏','online':True,'silent':False,'description':'聊游戏'}
    def candidates(self,parent):
        if self.read_error:raise self.read_error
        return [self.info.copy()]
    def room(self,rid):return self.info.copy()
    def messages(self,rid):return self.rows.copy()
    def send(self,rid,text):
        self.sent.append((rid,text))
        if self.error:raise self.error
        return {}

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.c=copy.deepcopy(DEFAULTS);self.c.update(bot_uid='100',writer_machine=machine_id(),paused=False,live_chat={**DEFAULT,'enabled':True})
        self.path=root/'config.json';self.path.write_text(json.dumps(self.c))
        self.now=datetime(2026,10,24,12,tzinfo=TZ).timestamp()
        api=API();api.identity=lambda:{'mid':'100','name':'蓝色大肥鱼'}
        self.e=Engine(self.path,root/'state',root,api,now=lambda:self.now)
        self.e.s.put('bound_uid','100');self.e.s.put('baseline_at',self.now-100);self.e.s.put('account_name','蓝色大肥鱼')
        self.e.sync=lambda:None;self.e.screen_content=lambda text:{'allowed':True}
        self.score=80;self.allowed=True;self.text='游戏里总有新发现'
        def core(payload):
            if payload['mode']=='select':return {'decision':{'allowed':True,'room_id':'123','interest_score':80},'persona_version':5,'source_ids':[]}
            return {'decision':{'allowed':self.allowed,'willing':True,'text':self.text,'interest_score':self.score},'persona_version':5,'source_ids':[]}
        self.e.unified_request=core;self.api=FakeLive();self.live=LiveChat(self.e,self.api,RNG())
        self.e.s.put('live_next_visit',self.now-1)
    def tearDown(self):self.e.s.db.close();self.tmp.cleanup()
    def tick(self):
        with self.e.s.lock():return self.live.tick()
    def message(self,text='这个游戏怎么配装',uid='200',offset=0):
        self.api.rows.append({'uid':uid,'text':text,'timeline':str(self.now+offset),'time':self.now+offset})
    def enter(self):self.assertEqual(self.tick()['status'],'joined_text_chat')
    def chat(self,text='这个游戏怎么配装',uid='200'):
        self.now+=61;self.message(text,uid);return self.tick()
    def test_limits_and_duration_cannot_expand(self):
        for key,value in [('daily_limit',101),('room_daily_limit',41),('stay_min',599),('stay_max',3601),('message_gap',59),('ordinary_probability',1.0)]:
            c={'live_chat':{**DEFAULT,key:value}}
            with self.subTest(key=key),self.assertRaises(ValueError):policy(c)
    def test_baseline_new_messages_shared_model_and_restart_dedupe(self):
        self.message('进场前的旧话');self.enter();self.assertFalse(self.api.sent)
        self.assertEqual(self.chat()['status'],'success');self.assertEqual(len(self.api.sent),1)
        self.live=LiveChat(self.e,self.api,RNG());self.now+=61;self.tick();self.assertEqual(len(self.api.sent),1)
        self.assertEqual(self.e.s.used('100','live',self.now),1)
        self.assertEqual(self.e.s.db.execute('SELECT count(*) FROM usage').fetchone()[0],2)
        self.assertEqual(self.e.s.db.execute('SELECT persona_version FROM action_context').fetchone()[0],'5')
    def test_minimum_stay_and_no_session_extension_on_restart(self):
        self.enter();v=self.e.s.get('live_visit');self.assertEqual(v['ends']-v['started'],600)
        self.now+=601;self.live=LiveChat(self.e,self.api,RNG());self.assertEqual(self.tick()['reason'],'visit_complete')
        self.assertIsNone(self.e.s.get('live_visit'));self.assertFalse(self.api.sent)
    def test_ordinary_probability_and_directed_bypass(self):
        self.score=10;self.enter();self.assertEqual(self.chat()['status'],'ordinary_probability_skip')
        self.assertEqual(self.chat('大肥鱼聊聊配装')['status'],'success')
    def test_self_stale_and_repeated_messages_never_trigger(self):
        self.enter();self.assertEqual(self.chat(uid='100')['status'],'no_new_chat')
        self.now+=61;self.message('过期的消息',offset=-100);self.assertEqual(self.tick()['status'],'no_new_chat')
        self.assertFalse(self.api.sent)
    def test_topic_and_stop_exit_without_sending(self):
        for text in ['政治评论','别刷屏']:
            with self.subTest(text=text):
                self.e.s.put('live_avoid:123',0);self.e.s.put('live_next_visit',self.now-1);self.enter()
                self.assertEqual(self.chat(text)['status'],'left');self.assertFalse(self.api.sent)
    def test_semantic_topic_rejection_exits(self):
        self.enter();self.allowed=False;self.assertEqual(self.chat()['reason'],'topic_or_privacy_restriction');self.assertFalse(self.api.sent)
    def test_gap_and_duplicate_text_are_checked(self):
        self.enter();self.chat();self.now+=16;self.message('再来一句');self.assertEqual(self.tick()['status'],'message_gap')
        self.now+=61;self.message('现在可以了吗');self.assertEqual(self.tick()['status'],'invalid_or_repeated_text');self.assertEqual(len(self.api.sent),1)
    def test_nonplain_reply_is_skipped_without_send_reservation_or_backoff(self):
        self.enter();self.text='测试我 那我好好表现🐳'
        self.assertEqual(self.chat()['status'],'invalid_live_text')
        self.assertFalse(self.api.sent);self.assertEqual(self.e.s.used('100','live',self.now),0)
        self.assertIsNotNone(self.e.s.get('live_visit'));self.assertIsNone(self.e.s.get('live_backoff_until'))
        with self.e.s.lock(),self.assertRaisesRegex(Denied,'live_plain_text_only'):
            self.live.send(self.c,policy(self.c),self.e.s.get('live_visit'),'blocked',self.text,{})
        self.assertFalse(self.api.sent)
    def test_unknown_delivery_is_not_retried(self):
        self.enter();self.api.error=TimeoutError();self.assertEqual(self.chat()['status'],'unknown')
        self.live=LiveChat(self.e,self.api,RNG());self.now+=61;self.tick();self.assertEqual(len(self.api.sent),1)
        self.assertEqual(self.e.s.used('100','live',self.now),1)
    def test_platform_rejection_backs_off_and_leaves(self):
        self.enter();self.api.error=PlatformError(-401);self.assertEqual(self.chat()['reason'],'platform_-401')
        self.assertEqual(self.tick()['status'],'backoff');self.assertEqual(len(self.api.sent),1)
        self.assertGreaterEqual(self.e.s.get('live_avoid:123'),self.now+86400)
    def test_room_restriction_enabled_during_generation_prevents_send(self):
        self.enter();original=self.e.unified_request
        def core(payload):
            result=original(payload)
            self.api.info.update(silent=True,restriction='room_silence_policy')
            return result
        self.e.unified_request=core
        self.assertEqual(self.chat()['reason'],'room_silence_policy')
        self.assertFalse(self.api.sent);self.assertEqual(self.e.s.used('100','live',self.now),0)
    def test_live_rejection_category_is_preserved_without_retry(self):
        self.enter();self.api.error=PlatformError('live_message_rejected')
        self.e.api.last_platform_error={'code':0,'marker':'k','classification':'room_keyword_block'}
        result=self.chat();self.assertEqual(result['rejection_classification'],'room_keyword_block')
        self.tick();self.assertEqual(len(self.api.sent),1)
    def test_giveaway_slogans_do_not_create_chat_or_model_calls(self):
        self.enter();self.assertEqual(self.chat('主播发福袋啦')['status'],'no_substantive_chat')
        self.assertFalse(self.api.sent);self.assertEqual(self.e.s.db.execute('SELECT count(*) FROM usage').fetchone()[0],1)
    def test_read_rejection_is_bounded_not_polled_repeatedly(self):
        self.api.read_error=PlatformError(-352);self.assertEqual(self.tick()['status'],'backoff')
        self.assertGreaterEqual(self.e.s.get('live_backoff_until'),self.now+3600)
    def test_pause_hours_room_offline_block_new_messages(self):
        self.enter();self.api.info['online']=False;self.assertEqual(self.chat()['reason'],'room_unavailable')
        self.e.s.put('pause_reason','manual');self.assertEqual(self.tick()['reason'],'paused')
        self.e.s.put('pause_reason',None);self.now+=12*3600;self.assertEqual(self.tick()['reason'],'outside_active_hours')
    def test_room_quota_is_daily_across_visits(self):
        self.c['live_chat']['room_daily_limit']=1;self.path.write_text(json.dumps(self.c));self.enter();self.chat()
        self.text='另一个游戏呢';self.assertEqual(self.chat()['reason'],'room_daily_limit');self.assertEqual(len(self.api.sent),1)
    def test_daily_quota_no_reset_after_restart(self):
        self.c['live_chat']['daily_limit']=1;self.path.write_text(json.dumps(self.c));self.enter();self.chat()
        self.live=LiveChat(self.e,self.api,RNG());self.assertEqual(self.tick()['reason'],'live_daily_limit');self.assertEqual(len(self.api.sent),1)
    def test_model_budget_remains_shared_and_stops_without_write(self):
        self.enter();self.c['model_daily']=1;self.path.write_text(json.dumps(self.c))
        self.assertEqual(self.chat()['reason'],'model_daily_limit');self.assertFalse(self.api.sent)

class LiveAPITests(unittest.TestCase):
    def test_room_sibling_silence_and_block_policies_are_respected(self):
        base={'room_info':{'room_id':123,'uid':200,'title':'游戏','live_status':1}}
        for extra,reason in [({'silent_room_info':{'type':'level','level':10}},'room_silence_policy'),
                             ({'block_info':{'block':True}},'room_access_blocked'),
                             ({'silent_room_info':['invalid']},'room_permission_unverified')]:
            with self.subTest(extra=extra):
                api=SimpleNamespace(request=lambda *a,**k:{**base,**extra})
                room=LiveAPI(api).room('123');self.assertTrue(room['silent']);self.assertEqual(room['restriction'],reason)
        api=SimpleNamespace(request=lambda *a,**k:{**base,'silent_room_info':{'type':'','level':0},'block_info':{'block':False}})
        self.assertFalse(LiveAPI(api).room('123')['silent'])
    def test_plain_danmaku_exact_target_and_no_paid_operation(self):
        calls=[]
        api=SimpleNamespace(credentials={'BILI_JCT':'fake'},ensure_device=lambda:None,request=lambda path,**kwargs:calls.append((path,kwargs)) or {})
        live=LiveAPI(api);live.send('123','大家聊游戏呀')
        self.assertEqual(len(calls),1);path,kw=calls[0];self.assertEqual(path,'/msg/send');self.assertTrue(kw['live'])
        self.assertEqual(kw['data']['roomid'],'123');self.assertEqual(kw['data']['csrf_token'],'fake')
        with self.assertRaises(ValueError):live.send('123','政治观点')
        self.assertEqual(len(calls),1)
    def test_public_history_parsing_ignores_audio_and_images(self):
        api=SimpleNamespace(request=lambda *a,**k:{'room':[{'uid':1,'text':'游戏好玩','timeline':'2026-10-24 12:00:01','voice':{'url':'never-read'},'emoticon':{'url':'never-read'}}]})
        rows=LiveAPI(api).messages('123');self.assertEqual(set(rows[0]),{'uid','text','timeline','time'})

class LiveCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_persona_public_context_and_no_av_claim(self):
        context={'persona':{'version':5,'identity':'元气笨蛋'},'source_ids':[],'facts':[],'activities':[]}
        s=SimpleNamespace(policy={'bili_live_enabled':True},reload_policy=lambda:None,
            build_context=AsyncMock(return_value=context),generate=AsyncMock(return_value={'allowed':True,'willing':True,'interest_score':80,'text':'这个玩法真有意思'}),
            state=SimpleNamespace(audit_context=lambda *a,**k:None))
        import asyncio
        s.busy=asyncio.Lock()
        result=await plan(s,{'mode':'reply','material':{'room_id':'123','new_messages':['聊游戏']}})
        self.assertEqual(result['persona_version'],5);self.assertEqual(s.build_context.call_args.kwargs['purpose'],'public')
        self.assertIn('完全没有直播画面和声音',s.generate.call_args.args[0][0]['content'])
        self.assertIn('只允许汉字、英文字母、数字和普通空格',s.generate.call_args.args[0][0]['content'])
        s.generate.return_value={'allowed':True,'willing':True,'interest_score':80,'text':'这个玩法真有意思🐳'}
        result=await plan(s,{'mode':'reply','material':{'room_id':'123'}})
        self.assertFalse(result['decision']['willing']);self.assertEqual(result['decision']['text'],'')
        self.assertEqual(s.generate.await_count,2)  # No rewriting or retrying the invalid candidate.
        s.generate.return_value={'allowed':True,'willing':True,'interest_score':80,'text':'政治观点'}
        with self.assertRaises(ValueError):await plan(s,{'mode':'reply','material':{'room_id':'123'}})

class LivePlainTextTests(unittest.TestCase):
    def test_character_rule_is_live_only_and_mirrored(self):
        from phase2.content_rules import is_plain_live_text,public_text
        from maibot_unified_core.content_rules import is_plain_live_text as core_plain
        for text in ('中文普通文字','聊聊 MC 版本2','English 123'):
            self.assertTrue(is_plain_live_text(text));self.assertTrue(core_plain(text))
        for text in ('鲸鱼🐳','颜文字^_^','标点！','数学1+1','装饰★','[doge]',
                     '零宽\u200b字符','字\ufe0f','中\n间','全角Ａ','   ',''):
            with self.subTest(text=text):
                self.assertFalse(is_plain_live_text(text));self.assertFalse(core_plain(text))
        self.assertEqual(public_text('其他场景仍可用🐳'),'其他场景仍可用🐳')

    def test_api_blocks_symbols_before_any_network_request(self):
        api=SimpleNamespace(ensure_device=Mock(),request=Mock(),credentials={})
        with self.assertRaisesRegex(Denied,'live_plain_text_only'):
            LiveAPI(api).send('123','测试🐳')
        api.ensure_device.assert_not_called();api.request.assert_not_called()
