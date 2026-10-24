import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.command_planner import envelope, checked_plan, interpret, REQUEST
from maibot_unified_core.account_schema import spec as account_spec
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from phase2.commands import CommandExecutor
from phase2.core import Denied

BV = 'BV1gB4y1F7pV'


def ready(spec):
    return {'status': 'ready', 'spec': spec}


def action(kind='browse', typ='video', target=BV):
    return {'kinds': [kind], 'target_type': typ, 'target': target, 'text': '', 'prompt': ''}


class GroundingTests(unittest.TestCase):
    def test_target_must_come_from_message_or_relevant_group_reference(self):
        for request, target in [(envelope('关注一下李四'), '242616'),
                                (envelope('看看'+BV), 'BV1ehHQ6bErB')]:
            with self.assertRaises(ValueError):
                checked_plan(ready(action('follow' if target.isdigit() else 'browse', 'user' if target.isdigit() else 'video', target)), request, {})
        recent=[{'target_type':'video','target':BV}]
        self.assertEqual(envelope('看看刚才那个', recent)['provided_targets'],recent)
        self.assertFalse(envelope('改一下自己的简介',recent)['provided_targets'])
        r=envelope('去和直播间123456的观众聊20分钟')
        self.assertEqual(checked_plan(ready({**action(typ='live',target='123456'),'stay_seconds':1200}),r,{})['stay_seconds'],1200)

    def test_model_cannot_expand_permissions_or_invent_assets(self):
        request=envelope('重写一下简介')
        for fields in ({'uname':'新名字'},{'face':'image'},{'password':'secret'},{'sign':'游戏','csrf':'x'}):
            with self.assertRaises(ValueError):
                checked_plan(ready({**action('account','account','profile'),'settings':fields}),request,{})
        for s in ({**action(),'images':['a'*64]}, {**action(),'api_path':'/anything'}, action('delete')):
            with self.assertRaises(ValueError):checked_plan(ready(s),envelope('看看'+BV),{})

    def test_public_privacy_topics_and_coin_amount_keep_hard_checks(self):
        for value in ('主人QQ号937673289','政治观点'):
            with self.assertRaises(ValueError):account_spec('profile',{'sign':value})
        with self.assertRaises(ValueError):checked_plan(ready(action('coin')),envelope('投2枚币 '+BV),{})
        self.assertEqual(checked_plan(ready(action('coin')),envelope('投一枚币 '+BV),{})['kinds'],['coin'])

    def test_folder_layout_and_image_bindings(self):
        req=envelope('调整收藏夹')
        s=account_spec('folder',{'media_id':'123','title':'游戏'})
        with self.assertRaises(ValueError):checked_plan(ready(s),req,{})
        snap={'settings':{'folder':{'folders':[{'media_id':'123'}]}}}
        self.assertEqual(checked_plan(ready(s),req,snap),s)
        with self.assertRaises(ValueError):checked_plan(ready(account_spec('space',{'index_order':[1,2]})),req,{'settings':{'space':{'index_order':[1,2,3]}}})
        s={**action('dynamic','self',''),'text':'研究游戏里的光影'}
        self.assertEqual(checked_plan(ready(s),envelope('发一条图文动态',image_id='a'*64),{})['images'],['a'*64])

    def test_empty_optional_settings_is_inert_but_nonempty_cannot_smuggle_fields(self):
        request=envelope('点赞 '+BV)
        self.assertEqual(checked_plan(ready({**action('like'),'settings':{}}),request,{}),action('like'))
        with self.assertRaises(ValueError):checked_plan(ready({**action('like'),'settings':{'sign':'偷偷修改'}}),request,{})


class ModelQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.policy={'qq_bot_account':'bot','qq_group_sessions':{'ga':'a'},'qq_owner_bindings':{'a':'owner'},
                     'allowed_qq_sessions':['a'],'qq_bili_commands_enabled':True,'qq_command_model_planning_enabled':True}
        self.save_policy()
        self.s=UnifiedService(SimpleNamespace(),self.root);self.s.policy=self.policy
        self.s.state=State(self.root/'s.db',self.policy);self.s.state.init_persona({'identity':'固定人设','bot.nickname':'蓝色大肥鱼'},{'interests':['游戏','动漫']})
        self.q=CommandQueue(self.s);self.s.commands=self.q
        self.s.generate=AsyncMock(return_value=ready(account_spec('profile',{'sign':'自信满满的游戏动漫爱好者'})))
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def save_policy(self):Path(self.root,'policy.json').write_text(json.dumps(self.policy))
    def capture(self,text='【命令】去修改一下自己的B站个人简介，按现在人设自己写',actor='owner',mid='one'):
        return self.q.capture({'platform':'qq','account_id':'bot','session_id':'a','scope':'group','timestamp':time.time(),
            'message_id':mid,'message_info':{'user_info':{'user_id':actor},'group_info':{'group_id':'ga'}},'raw_message':[{'type':'text','data':text}]})
    async def test_owner_prose_reaches_model_and_then_real_existing_executor_spec(self):
        key=self.capture();self.assertEqual(self.q.get(key)['status'],'pending')
        self.assertEqual(self.q.claim(),{'job':None,'interpretation_required':key})
        self.assertEqual(await interpret(self.q,key),{'status':'ready'})
        job=self.q.claim()['job'];self.assertTrue(job['forced']);self.assertEqual(job['spec']['settings'],{'sign':'自信满满的游戏动漫爱好者'})
        self.assertNotIn(REQUEST,job['spec'])
        content=json.loads(self.s.generate.call_args.args[0][1]['content'])
        self.assertIn('自己写',content['owner_request']);self.assertNotIn('actor',content)
    async def test_imposter_quoted_prefix_and_stale_source_never_reach_model(self):
        self.assertIsNone(self.capture(actor='imposter'))
        self.assertIsNone(self.capture(text='他说【命令】改简介'))
        key=self.capture();self.q.db.execute('UPDATE qq_bili_commands SET created=?',(time.time()-601,))
        await interpret(self.q,key);self.assertEqual(self.q.get(key)['status'],'failed');self.s.generate.assert_not_called()
    async def test_typed_bot_mention_normalized_only_after_owner_binding(self):
        # The QQ display addressee can be a known alias, not the internal name.
        persona=self.s.state.persona();persona['stable']={'identity':'固定人设'}
        persona['dynamic'].update(display_name='DeepSeek',aliases=['蓝色大肥鱼'])
        self.s.state.persona=lambda:persona
        good='@蓝色大肥鱼 【命令】修改简介，说会在评论区回复，暂不接受私聊'
        key=self.capture(text=good)
        self.assertIsNotNone(key);self.assertTrue(self.q.get(key)['forced'])
        self.assertTrue(self.q.get(key)['spec'][REQUEST].startswith('【命令】'))
        for i,text in enumerate((good, '@陌生昵称 【命令】改简介', '她说：'+good, '“'+good+'”', '@蓝色大肥鱼同学 【命令】改简介')):
            actor='imposter' if i==0 else 'owner'
            self.assertIsNone(self.capture(text=text,actor=actor,mid=str(i)))
        self.s.generate=AsyncMock(return_value=ready(account_spec('profile',{'sign':'游戏动漫研究员！会在评论区回复，暂不接受私聊。'})))
        self.assertEqual(await interpret(self.q,key),{'status':'ready'})
        self.assertIn('暂不接受私聊',self.q.claim()['job']['spec']['settings']['sign'])
    async def test_revocation_during_model_call_blocks_execution(self):
        async def revoked(*a,**kw):
            self.policy['qq_owner_bindings']['a']='new-owner';self.save_policy()
            return ready(account_spec('profile',{'sign':'游戏玩家'}))
        self.s.generate=revoked;key=self.capture();await interpret(self.q,key)
        self.assertEqual(self.q.get(key)['status'],'failed');self.assertIsNone(self.q.claim()['job'])
    async def test_replay_and_restart_do_not_repeat_model_or_promote_interrupted_plan(self):
        key=self.capture();await interpret(self.q,key);await interpret(self.q,key)
        self.assertEqual(self.s.generate.await_count,1);self.assertEqual(self.capture(),key)
        key2=self.capture(mid='two');self.s.state.put('command_interpret:'+key2,{'status':'started'})
        await interpret(CommandQueue(self.s),key2)
        self.assertEqual(self.q.get(key2)['status'],'failed');self.assertEqual(self.s.generate.await_count,1)
    async def test_concurrent_interpretation_runs_once(self):
        key=self.capture();await asyncio.gather(interpret(self.q,key),interpret(self.q,key))
        self.assertEqual(self.s.generate.await_count,1)
    async def test_clarification_or_model_failure_never_dispatches(self):
        for i,answer in enumerate([{'status':'clarify','reason':'请提供准确UID'}, {'status':'unsupported','reason':'昵称不可修改'}, {}]):
            self.s.generate=AsyncMock(return_value=answer);key=self.capture(mid=str(i));await interpret(self.q,key)
            self.assertEqual(self.q.get(key)['status'],'failed')
        self.s.generate=AsyncMock(side_effect=RuntimeError('private API error'))
        key=self.capture(mid='failure');await interpret(self.q,key)
        self.assertNotIn('private API',json.dumps(self.q.get(key)))
        self.assertIsNone(self.q.claim()['job'])
    async def test_query_only_returns_answer_without_dispatch(self):
        self.s.generate=AsyncMock(return_value={'status':'no_action','reason':'可以修改个人简介和空间公告。这次没有进行修改。'})
        key=self.capture(text='【命令】先不要改简介，只告诉我能改哪些资料')
        result=await interpret(self.q,key);self.assertEqual(result['status'],'skipped')
        self.assertIsNone(self.q.claim()['job']);self.assertIn('没有进行修改',self.q.get(key)['result']['reason'])
    async def test_stale_snapshot_not_used_and_unknown_account_result_not_success(self):
        self.s.state.put('account_snapshot',{'at':time.time()-7201,'settings':{'profile':{'sign':'stale'}}})
        key=self.capture();await interpret(self.q,key)
        content=json.loads(self.s.generate.call_args.args[0][1]['content']);self.assertEqual(content['own_account_settings'],{})
        self.q.claim();self.q.finish(key,{'status':'unknown','actions':[{'kind':'account','status':'unknown','reason':'setting_readback_not_confirmed'}]})
        self.assertEqual(self.q.get(key)['status'],'unknown')
    async def test_notify_reports_verified_bio_and_submission_is_not_public_success(self):
        sent=[]
        async def send(text,*a,**kw):sent.append(text);return {'sent':True}
        self.s.ctx=SimpleNamespace(send=SimpleNamespace(text=send))
        key=self.capture();await interpret(self.q,key);self.q.claim()
        self.q.finish(key,{'status':'success','actions':[{'kind':'account','status':'success','section':'profile','verified':True}]})
        await self.q.notify();self.assertIn('个人简介：自信满满的游戏动漫爱好者',sent[-1]);self.assertIn('已回读确认',sent[-1])
        key=self.capture(mid='post');self.q.db.execute('UPDATE qq_bili_commands SET spec=? WHERE id=?',(json.dumps(action()),key));self.q.claim()
        self.q.finish(key,{'status':'success','actions':[{'kind':'comment','status':'success'}]});await self.q.notify()
        self.assertIn('已提交（平台已接收）',sent[-1]);self.assertNotIn('已完成',sent[-1])


class WorkerBudgetTests(unittest.TestCase):
    def test_model_planning_uses_shared_budget_and_failure_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3
            calls=[]
            def request(r):calls.append(r);return {'job':None,'interpretation_required':'one'} if r['op']=='command_claim' else {'status':'failed'}
            def bounded(r):calls.append(r);raise Denied('model_daily_limit')
            e=SimpleNamespace(s=SimpleNamespace(db=sqlite3.connect(':memory:'),_lock_held=True),config=lambda:{'qq_commands_enabled':True},unified_request=request,bounded_core=bounded)
            CommandExecutor(e).process_one()
            self.assertEqual([r['op'] for r in calls],['command_claim','command_interpret','command_result'])
            self.assertEqual(calls[-1]['result']['reason'],'model_daily_limit')


if __name__=='__main__':unittest.main()
