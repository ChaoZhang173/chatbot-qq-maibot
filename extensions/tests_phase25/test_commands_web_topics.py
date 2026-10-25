"""Meaningful authorization, delivery, failure, privacy and topic regressions."""
import copy
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from datetime import datetime
from maibot_unified_core.commands import CommandQueue, raw_text
from maibot_unified_core.command_schema import parse,validate,PREFIX
from maibot_unified_core.content_rules import blocked
from maibot_unified_core.persona import valid_value
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from maibot_unified_core import web_access
from phase2.core import DEFAULTS,Target,TZ,machine_id,Denied
from phase2.runner import Engine
from phase2.commands import CommandExecutor
from phase2.engagement import CAPS
from phase2.api import Bili
from test_engagement import API

BV='BV1ehHQ6bErB'

class GrammarTests(unittest.TestCase):
    def test_prefix_needs_no_colon_and_both_packages_match(self):
        from phase2.command_schema import PREFIX as BILI_PREFIX,parse as bili_parse
        self.assertEqual(PREFIX,'【命令】');self.assertEqual(BILI_PREFIX,PREFIX)
        for text in ('【命令】关注 UID 242616','【命令】 点赞 '+BV,'【命令】发动态：研究游戏光影'):
            self.assertEqual(parse(text),bili_parse(text));validate(parse(text))
    def test_six_operations_and_literal_body(self):
        cases={'关注 UID 242616':['follow'],'点赞 收藏 投币 '+BV:['like','favorite','coin'],
               '在 https://www.bilibili.com/video/'+BV+' 下留言：这个做法值得一试':['comment'],
               '发动态：今天想研究游戏里的光影':['dynamic']}
        for text,kinds in cases.items():self.assertEqual(validate(parse(PREFIX+text))['kinds'],kinds)
        self.assertEqual(parse('发动态：收藏这个灵感，之后再研究')['kinds'],['dynamic'])
    def test_ambiguity_and_sensitive_or_blocked_content_fail(self):
        for text in ('关注李四','点赞 '+BV+' BV1wYtd6KERy','如果好玩就点赞 '+BV,
                     '发动态：政治立场','发动态：QQ号000000000','取消收藏 '+BV,
                     '在 '+BV+' 下留言：政治观点','投币2枚 '+BV,'关注 UID 0'):
            with self.subTest(text=text),self.assertRaises(ValueError):validate(parse(text))
    def test_topic_policy_does_not_blanket_block_game_and_tech(self):
        for text in ('总统大选解析','地域黑合集','某人谈种族优劣','女人天生低等','性别对立'):
            self.assertTrue(blocked(text))
        for text in ('日本动漫角色设计','美国公司发布GPU','男女角色都可以选','游戏战斗教程'):
            self.assertFalse(blocked(text))
        self.assertFalse(valid_value('interests',['政治']))

class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.p={'qq_bot_account':'bot','allowed_qq_sessions':['a','b'],
                'qq_group_sessions':{'ga':'a','gb':'b'},'qq_owner_bindings':{'a':'owner','b':'owner'},
                'qq_bili_commands_enabled':True,'web_enabled':True}
        (root/'policy.json').write_text(json.dumps(self.p))
        self.sent=[]
        async def send(text,session,**kw):self.sent.append((text,session));return {'sent':True,'message_id':'ack'}
        self.s=UnifiedService(SimpleNamespace(send=SimpleNamespace(text=send)),root)
        self.s.policy=self.p;self.s.state=State(root/'s.db',self.p)
        self.s.state.init_persona({'identity':'fixed'},{'interests':['AI']})
        self.q=CommandQueue(self.s);self.s.commands=self.q
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def message(self,text=PREFIX+'关注 UID 242616',actor='owner',session='a',mid='m1',**kw):
        return {'platform':'qq','account_id':'bot','session_id':session,'scope':'group','timestamp':time.time(),
            'message_id':mid,'message_info':{'user_info':{'user_id':actor,'user_nickname':'synthetic-user'},'group_info':{'group_id':'g'+session}},
            'raw_message':[{'type':'text','data':text}],**kw}
    async def test_only_platform_bound_owner_and_exact_prefix_force(self):
        for m in (self.message(actor='imposter'),self.message(text='请 '+PREFIX+'关注 UID 242616'),
                  self.message(text='【命令：】关注 UID 242616'),self.message(scope='private'),
                  self.message(account_id='other'),self.message(timestamp=time.time()-301)):
            self.assertIsNone(self.q.capture(m))
        self.assertTrue(self.q.capture(self.message()))
        self.assertTrue(self.q.capture(self.message(session='b',mid='m2')))
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM qq_bili_commands').fetchone()[0],2)
    async def test_native_at_segments_do_not_corrupt_prefix(self):
        m=self.message();m['raw_message'].insert(0,{'type':'at','data':{'target_user_id':'bot'}})
        m['processed_plain_text']='@蓝色大肥鱼 '+PREFIX+'关注 UID 242616'
        self.assertTrue(self.q.capture(m))

    async def test_replay_claim_and_restart_keep_one_job(self):
        key=self.q.capture(self.message());self.assertEqual(self.q.capture(self.message()),key)
        self.assertEqual(self.q.claim()['job']['id'],key)
        again=CommandQueue(self.s);self.assertEqual(again.claim()['job']['id'],key)
        self.q.finish(key,{'status':'success','actions':[{'kind':'follow','status':'success'}]})
        self.assertIsNone(self.q.claim()['job']);await self.q.notify();await self.q.notify()
        self.assertEqual(len(self.sent),1);self.assertIn('已完成',self.sent[0][0])
    async def test_binding_revoked_and_malformed_command_never_dispatch(self):
        key=self.q.capture(self.message());self.p['qq_owner_bindings']['a']='new-owner'
        self.assertIsNone(self.q.claim()['job']);self.assertEqual(self.q.get(key)['status'],'failed')
        key=self.q.capture(self.message(text=PREFIX+'关注未知昵称',actor='new-owner',mid='m2'))
        self.assertEqual(self.q.get(key)['status'],'failed');await self.q.notify()
    async def test_unknown_and_scoped_receipts(self):
        key=self.q.capture(self.message());self.q.claim()
        self.q.finish(key,{'status':'unknown','reason':'interrupted_unknown'})
        self.assertIsNone(self.q.get(key,'b'));await self.q.notify()
        self.assertIn('结果未知',self.sent[0][0]);self.assertIsNone(self.q.claim()['job'])
    async def test_owner_has_no_mood_veto_but_topic_restrictions_apply(self):
        key=self.q.capture(self.message());self.q.claim()
        self.s.generate=AsyncMock(return_value={'topic_allowed':True,'privacy_allowed':True,'willing':False})
        answer=json.loads(json.dumps(await self.q.review(key,'公开游戏账号')))
        self.assertIs(answer['allowed'],True)
        self.assertIs((await self.q.review(key,'政治评论账号'))['allowed'],False)
    async def test_discretionary_never_inherits_force_and_can_decline(self):
        event={'event_id':'source','platform':'qq','bot_account_id':'bot','conversation_id':'a','actor_id':'other',
            'origin':'external_user','event_type':'message_received','occurred_at':time.time(),'source_id':'source','content':'聊聊游戏'}
        result=self.s.state.ingest(event);self.s.last_message['a']=('聊聊游戏',result['event_id'],'other')
        key=self.q.discretionary('a','关注 UID 242616')['request_id'];self.assertFalse(self.q.claim()['job']['forced'])
        self.s.generate=AsyncMock(return_value={'topic_allowed':True,'privacy_allowed':True,'willing':False})
        self.assertFalse((await self.q.review(key,'游戏作者'))['allowed'])
        self.assertFalse(self.q.get(key)['forced'])
    async def test_web_topic_private_input_limits_and_failure(self):
        for query in ('政治动态','QQ号000000000'):
            with patch.object(web_access,'search',side_effect=AssertionError('no fetch')):
                self.assertFalse((await web_access.run(self.s,'search',query,'a'))['success'])
        self.s.screen_content=AsyncMock(return_value={'allowed':True})
        with patch.object(web_access,'search',return_value={'results':[{'title':'AI','url':'https://example.com'}]}) as fn:
            result=await web_access.run(self.s,'search','AI 开源模型','a')
            self.assertTrue(result['success']);await web_access.run(self.s,'search','AI 开源模型','a');self.assertEqual(fn.call_count,1)
        with patch.object(web_access,'fetch',side_effect=ValueError('cannot_read')):
            self.assertFalse((await web_access.run(self.s,'fetch','https://example.com','a'))['success'])

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name)
        c=copy.deepcopy(DEFAULTS);c.update(bot_uid='100',writer_machine=machine_id(),paused=False,
            enabled={'dynamic':True,'proactive':True,'reply':True,'dm':False},qq_commands_enabled=True,
            engagement={'enabled':True,'limits':dict(CAPS)})
        self.config=r/'c.json';self.config.write_text(json.dumps(c))
        self.api=API();self.api.identity=lambda:{'mid':'100','name':'bot'}
        self.api.profile=lambda uid:{'uid':uid,'name':'游戏作者','bio':'游戏技巧'}
        self.target=Target('video','123',1,'300','游戏教程','https://www.bilibili.com/video/'+BV,[],BV,'title_description')
        self.api.video=lambda *a,**k:self.target
        self.api.sent=[]
        def send(kind,target,text,**kwargs):self.api.sent.append((kind,text));return {'id':'remote','url':'https://example.com'}
        self.api.send=send;self.api.reconcile=lambda *a,**kw:None
        now=datetime(2026,10,24,18,tzinfo=TZ).timestamp()
        self.e=Engine(self.config,r/'state',r,self.api,now=lambda:now)
        self.e.s.put('bound_uid','100');self.e.s.put('baseline_at',now-10);self.e.sync=lambda:None
        self.jobs=[];self.results=[]
        def core(p):
            if p['op']=='command_claim':return {'job':self.jobs[0] if self.jobs else None}
            if p['op']=='command_review':return {'allowed':True,'persona_version':'5'}
            if p['op']=='command_result':self.results.append(p['result']);return {'ok':True}
            raise AssertionError(p['op'])
        self.e.unified_request=core;self.executor=CommandExecutor(self.e)
    def tearDown(self):self.e.s.db.close();self.tmp.cleanup()
    def job(self,text,key='job1'):
        self.jobs[:]=[{'id':key,'forced':True,'created':self.e.now(),'spec':validate(parse(text))}]
    def runjob(self):
        with self.e.s.lock():return self.executor.process_one()
    def test_all_engagement_share_executor_quota_and_no_replay(self):
        self.job('点赞 收藏 投币 '+BV)
        r=self.runjob();self.assertEqual(r['status'],'success');self.assertEqual(len(self.api.writes),3)
        self.runjob();self.assertEqual(len(self.api.writes),3)
        self.assertEqual(self.e.s.used('100','coin',self.e.now()),1)
    def test_real_owner_review_roundtrip_dispatches_without_mood_veto(self):
        host=QueueTests();host.setUp()
        try:
            key=host.q.capture(host.message(text='【命令】关注 UID 242616'))
            host.s.generate=AsyncMock(return_value={'topic_allowed':True,'privacy_allowed':True,'willing':False})
            def core(payload):
                if payload['op']=='command_claim':result=host.q.claim()
                elif payload['op']=='command_review':result=asyncio.run(host.q.review(payload['id'],payload['material']))
                elif payload['op']=='command_result':result=host.q.finish(payload['id'],payload['result'])
                else:raise AssertionError(payload['op'])
                return json.loads(json.dumps(result))
            self.e.unified_request=core
            self.assertEqual(self.runjob()['status'],'success')
            self.assertEqual(host.q.get(key)['status'],'success')
            self.assertEqual(len(self.api.writes),1)
            self.assertEqual(self.api.writes[0][1]['fid'],'242616')
        finally:host.tearDown()
    def test_follow_uid_and_existing_relation(self):
        self.job('关注 UID 242616');self.assertEqual(self.runjob()['status'],'success')
        self.assertEqual(self.api.writes[0][1]['fid'],'242616')
        self.job('关注 UID 242617','job2');self.api.attribute=6
        self.assertEqual(self.runjob()['status'],'success');self.assertEqual(len(self.api.writes),1)
    def test_balance_and_default_folder_failure_are_explicit(self):
        self.api.nav['money']=0;self.job('投币 '+BV)
        r=self.runjob();self.assertEqual(r['status'],'failed');self.assertIn('balance',r['actions'][0]['reason']);self.assertEqual(self.api.writes,[])
        self.api.folders=[];self.job('收藏 '+BV,'job2')
        self.assertIn('folder',self.runjob()['actions'][0]['reason'])
    def test_exact_public_text_and_shared_comment_quota(self):
        self.job('在 '+BV+' 下留言：游戏里的光影很有意思')
        self.assertEqual(self.runjob()['status'],'success');self.assertEqual(self.api.sent,[('proactive','游戏里的光影很有意思')])
        self.assertEqual(self.e.s.used('100','proactive',self.e.now()),1)
        self.job('发动态：今天研究游戏光影','job2');self.assertEqual(self.runjob()['status'],'success')
        self.assertEqual(self.e.s.used('100','dynamic',self.e.now()),1)
    def test_unknown_delivery_never_retries_after_restart(self):
        self.api.write_failure=TimeoutError();self.job('投币 '+BV)
        self.assertEqual(self.runjob()['status'],'unknown');self.executor=CommandExecutor(self.e)
        self.assertEqual(self.runjob()['status'],'unknown');self.assertEqual(len(self.api.writes),1)
    def test_crash_before_receipt_reports_unknown_without_dispatch(self):
        self.job('关注 UID 242616');self.e.s.db.execute('INSERT INTO qq_command_runs VALUES(?,?,?)',('job1','executing','{}'))
        self.assertEqual(self.runjob()['status'],'unknown');self.assertEqual(self.api.writes,[])
    def test_topic_block_stops_command_despite_owner(self):
        self.target.text='政治讨论';self.job('收藏 '+BV)
        self.assertEqual(self.runjob()['reason'],'fixed_topic_restriction');self.assertEqual(self.api.writes,[])
    def test_no_model_quota_reset_and_pause(self):
        self.e.s.put('pause_reason','manual');self.job('点赞 '+BV)
        self.assertEqual(self.runjob()['reason'],'paused');self.assertEqual(self.api.writes,[])
    def test_review_model_count_shared_with_bili_budget(self):
        self.job('关注 UID 242616');self.runjob()
        self.assertEqual(self.e.s.db.execute('SELECT count(*) FROM usage').fetchone()[0],1)

class NetworkGuardTests(unittest.TestCase):
    def test_private_resolved_addresses_and_credentials_blocked(self):
        for url in ('https://127.0.0.1','https://[::1]','http://example.com','https://u:p@example.com','https://example.com/?token=s'):
            with self.assertRaises(ValueError):web_access.checked_url(url)
        with patch.object(web_access.socket,'getaddrinfo',return_value=[(2,1,6,'',('169.254.169.254',443))]):
            with self.assertRaises(ValueError):web_access.checked_url('https://public-looking.example')
    def test_video_topic_rejected_before_subtitle_and_semantic_gate(self):
        b=Bili.__new__(Bili);calls=[]
        def request(path,params=None):
            calls.append(path);return {'title':'政治讨论','desc':'','aid':1,'cid':1,'bvid':BV,'owner':{'mid':3}}
        b.request=request
        with self.assertRaises(Denied):b.video(BV,with_subtitle=True)
        self.assertEqual(calls,['/x/web-interface/view'])
        b.request=lambda *a,**k:{'title':'隐晦的禁止话题','desc':'','aid':1,'cid':1,'bvid':BV,'owner':{'mid':3}}
        b.screen_content=lambda text:{'allowed':False}
        with self.assertRaises(Denied):b.video(BV,with_subtitle=True)
