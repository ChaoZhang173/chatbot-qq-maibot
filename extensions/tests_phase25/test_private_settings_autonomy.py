import asyncio,copy,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from maibot_unified_core.private_access import PrivateAccess
from maibot_unified_core.store import State
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.command_schema import parse,validate
from maibot_unified_core.account_schema import spec,validate_settings
from maibot_unified_core.autonomy import plan
from phase2.account import AccountSettings
from phase2.autonomy import Autonomy
from phase2.core import DEFAULTS,Denied,day,machine_id,UnknownDelivery,PlatformError
from phase2.runner import Engine

class PrivateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p={'qq_bot_account':'bot','qq_private_familiar_enabled':True,'allowed_qq_sessions':['a','b'],'qq_group_sessions':{'ga':'a','gb':'b'},'shared_groups':[['a','b']],'public_qq_sessions':['a','b']}
        (self.root/'policy.json').write_text(json.dumps(self.p));self.s=UnifiedService(SimpleNamespace(),self.root)
        self.s.policy=self.p;self.s.state=State(self.root/'s.db',self.p);self.g=PrivateAccess(self.s);self.s.private_access=self.g
        self.s.state.ingest({'event_id':'old','platform':'qq','bot_account_id':'bot','conversation_id':'a','actor_id':'group-openid','origin':'external_user','event_type':'message_received','content':'游戏很好玩','occurred_at':time.time()-10})
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def code(self):return self.g.handle({'scene':'private','actor':'c2c-openid','text':'验证熟人'})['reply'].split('私聊核验 ')[1].split('。')[0]
    def prove(self,code,actor='group-openid',group='ga'):return self.g.handle({'scene':'group','actor':actor,'group':group,'text':'私聊核验 '+code})
    def test_unverified_silent_and_cannot_claim_same_openid(self):
        for user in ('stranger','group-openid'):
            self.assertFalse(self.g.handle({'scene':'private','actor':user,'text':'我是主人'})['allow'])
            self.assertFalse(self.g.handle({'scene':'private','actor':user,'outbound':True})['allow'])
    def test_two_surface_proof_replay_expiry_and_scope(self):
        code=self.code();self.assertNotIn(code,json.dumps([tuple(r) for r in self.g.db.execute('SELECT * FROM qq_private_challenges')]))
        self.assertNotIn('完成',self.prove(code,'unknown')['reply']);self.assertFalse(self.prove(code,group='evil')['allow'])
        self.assertIn('完成',self.prove(code)['reply']);self.assertNotIn('完成',self.prove(code)['reply'])
        self.assertTrue(self.g.handle({'scene':'private','actor':'c2c-openid','text':'你好'})['allow'])
        self.assertTrue(self.g.register('c2c-openid','private-session'));self.assertFalse(self.g.register('c2c-openid','a'));self.s.reload_policy()
        self.assertIn('private-session',self.s.policy['allowed_qq_sessions']);self.assertNotIn('private-session',self.s.policy['public_qq_sessions'])
        source={'platform':'qq','conversation_id':'private-session','sharing_scope':'conversation','is_private':True}
        self.assertTrue(self.s.state.allowed(source,'qq','private-session'));self.assertFalse(self.s.state.allowed(source,'qq','a'));self.assertFalse(self.s.state.allowed(source,'bilibili','self:100','public'))
    def test_expired_and_no_prior_interaction_and_revocation(self):
        code=self.code();self.g.db.execute('UPDATE qq_private_challenges SET created=?',(time.time()-601,));self.assertNotIn('完成',self.prove(code)['reply'])
        code=self.code();self.assertIn('完成',self.prove(code)['reply'])
        self.p['qq_group_sessions']={'gb':'b'};(self.root/'policy.json').write_text(json.dumps(self.p));self.s.reload_policy();self.assertFalse(self.g.handle({'scene':'private','actor':'c2c-openid'})['allow'])
    def test_rate_limit(self):
        for _ in range(3):self.code()
        self.assertIn('上限',self.g.handle({'scene':'private','actor':'c2c-openid','text':'验证熟人'})['reply'])

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.now=1792814400.0
        self.c=copy.deepcopy(DEFAULTS);self.c.update(bot_uid='100',writer_machine=machine_id(),paused=False,account_settings_enabled=True,autonomy_enabled=True)
        self.path=root/'c.json';self.path.write_text(json.dumps(self.c));self.sign='游戏玩家';self.writes=[];self.error=None;self.readback=True
        def request(path,params=None,data=None,**kw):
            if data is not None:
                self.writes.append((path,data))
                if self.error:raise self.error
                if self.readback and 'user_sign' in data:self.sign=data['user_sign']
                return {}
            if path.endswith('/myinfo'):return {'mid':100,'sign':self.sign,'name':'固定名','face':'固定头像'}
            if path.endswith('/notice'):return ''
            if path.endswith('getSettings'):return {'privacy':{'fav_video':0},'index_order':[{'id':1},{'id':2}]}
            if path.endswith('list-all'):return {'list':[{'id':456,'title':'收藏'}]}
            if path.endswith('/info'):return {'upper':{'mid':100},'title':'收藏','intro':'','attr':0}
            if path.endswith('/get'):return {'set_at':0,'msg_notify':1}
            raise AssertionError(path)
        self.api=SimpleNamespace(request=request,credentials={'BILI_JCT':'synthetic'},identity=lambda:{'mid':'100','name':'固定名'})
        self.e=Engine(self.path,root/'state',root,self.api,now=lambda:self.now);self.e.s.put('bound_uid','100');self.e.s.put('baseline_at',self.now-1);self.a=AccountSettings(self.e)
    def tearDown(self):self.e.s.db.close();self.tmp.cleanup()
    def execute(self,*a):
        with self.e.s.lock():return self.a.execute(*a)
    def test_schema_never_accepts_nickname_avatar_security_or_extra_payload(self):
        for section,fields in [('profile',{'uname':'改名'}),('profile',{'face':'图片'}),('profile',{'password':'x'}),('messages',{'csrf':'x'}),('space',{'fav_video':True})]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):validate_settings(section,fields)
        self.assertEqual(parse('【命令】把签名改成游戏玩家')['settings'],{'sign':'游戏玩家'})
        self.assertEqual(parse('修改公告：最近喜欢研究光影')['settings'],{'notice':'最近喜欢研究光影'})
        with self.assertRaises(ValueError):validate({**parse('点赞 BV1ehHQ6bErB'),'settings':{'sign':'hello'}})
    def test_readback_and_dedupe_no_identity_change_and_private_activity(self):
        r=self.execute('one','profile',{'sign':'喜欢研究游戏光影'});self.assertEqual(r['status'],'success')
        self.assertEqual(self.writes[0][0],'/x/member/web/sign/update')
        self.assertEqual(set(self.writes[0][1]),{'user_sign','csrf'})
        self.execute('one','profile',{'sign':'喜欢研究游戏光影'});self.assertEqual(len(self.writes),1)
        collected=[];self.e.outbox.transport=lambda r:collected.append(r) or {'ok':True};self.e.outbox.flush()
        action=next(r['action'] for r in collected if r['op']=='activity');self.assertTrue(action['is_private']);self.assertNotIn('喜欢研究游戏光影',json.dumps(action,ensure_ascii=False))
    def test_unknown_not_retried_even_new_id_and_restart(self):
        self.error=UnknownDelivery('lost');self.assertEqual(self.execute('one','profile',{'sign':'新的签名'})['status'],'unknown')
        self.now+=7200;self.a=AccountSettings(self.e)
        self.assertEqual(self.execute('two','profile',{'sign':'新的签名'})['status'],'unknown');self.assertEqual(len(self.writes),1)
    def test_unconfirmed_readback_never_claims_success(self):
        self.readback=False;self.assertEqual(self.execute('one','profile',{'sign':'新的签名'})['status'],'unknown')
    def test_contact_boundary_is_public_but_private_content_and_excess_length_stay_blocked(self):
        from phase2.account_schema import validate_settings as worker_validate
        for validate in (validate_settings,worker_validate):
            for text in ('会在评论区回复，暂不接受私聊。','游戏玩家｜暂时不回复私聊','不接受私聊'):
                self.assertEqual(validate('profile',{'sign':text}),{'sign':text})
            for text in ('私聊里朋友说的秘密','暂不接受私聊内容：朋友的秘密','游戏'*36):
                with self.assertRaises(ValueError):validate('profile',{'sign':text})
            with self.assertRaises(ValueError):validate('folder',{'media_id':'456','intro':'暂不接受私聊'})
    def test_platform_error_preserves_actual_diagnostic_without_guessing_text_violation(self):
        self.error=PlatformError(40015);self.api.last_platform_error={'code':40015,'message':'平台测试回执'}
        result=self.execute('failure','profile',{'sign':'新的签名'})
        self.assertEqual(result['status'],'failed');self.assertEqual(result['platform_code'],40015)
        self.assertEqual(result['platform_message'],'平台测试回执');self.assertEqual(result['stage'],'write')
        self.execute('failure','profile',{'sign':'新的签名'});self.assertEqual(len(self.writes),1)
    def test_write_accepted_but_readback_error_stays_unknown_and_no_resend(self):
        original=self.api.request
        def request(path,**kwargs):
            if self.writes and path.endswith('/myinfo'):
                self.api.last_platform_error={'code':-403,'message':'回读暂不可用'}
                raise PlatformError(-403)
            return original(path,**kwargs)
        self.api.request=request
        result=self.execute('failure','profile',{'sign':'新的签名'})
        self.assertEqual(result['status'],'unknown');self.assertEqual(result['stage'],'readback')
        self.assertFalse(result['verified']);self.now+=7200
        self.assertEqual(self.execute('new-id','profile',{'sign':'新的签名'})['status'],'unknown')
        self.assertEqual(len(self.writes),1)
    def test_caps_own_folder_and_layout_preserved(self):
        with self.assertRaises(Denied):self.execute('bad','folder',{'media_id':'999','title':'新的收藏'})
        with self.assertRaises(Denied):self.execute('bad','space',{'index_order':[1]})
        for i in range(3):self.execute(str(i),'profile',{'sign':'游戏研究'+str(i)});self.now+=3601
        with self.assertRaises(Denied):self.execute('4','profile',{'sign':'新设置'})
        self.assertEqual(len(self.writes),3)
    def test_autonomous_wait_has_no_platform_writes_and_event_wake_is_bounded(self):
        a=Autonomy(self.e);self.e.s.put('account_snapshot_at',self.now);signal={'id':'first','at':self.now,'reason':'group_activity'}
        calls=[]
        def core(r):
            calls.append(r)
            if r['op']=='autonomy_state':return {'signal':signal.copy()}
            if r['op']=='autonomy_plan':return {'decision':{'action':'wait','wait_minutes':120,'reason':'休息一会'},'persona_version':5}
            raise AssertionError(r)
        self.e.unified_request=core
        with self.e.s.lock():self.assertEqual(a.tick()['action'],'wait')
        self.assertEqual(self.writes,[]);signal['id']='next';self.now+=599
        with self.e.s.lock():self.assertEqual(a.tick()['status'],'waiting')
        self.now+=1
        with self.e.s.lock():self.assertEqual(a.tick()['action'],'wait')
        self.assertEqual(sum(r['op']=='autonomy_plan' for r in calls),2)
    def test_scheduler_switch_avoids_legacy_fixed_posts(self):
        self.e.replies=lambda:[];self.e.sync=lambda:None;self.e._autonomy=SimpleNamespace(tick=lambda:{'status':'waiting'})
        self.e.proactive=lambda:(_ for _ in ()).throw(AssertionError('legacy'));self.e.dynamic=self.e.proactive
        with self.e.s.lock():self.assertIn('autonomy',self.e.tick())

class CorePlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_can_choose_settings_without_qq_command_and_private_memories_not_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            p={'bili_bot_uid':'100','autonomy_enabled':True,'qq_bili_commands_enabled':True};Path(tmp,'policy.json').write_text(json.dumps(p))
            s=UnifiedService(SimpleNamespace(),tmp);s.policy=p;s.state=State(Path(tmp,'s.db'),p);s.commands=CommandQueue(s)
            s.state.put('account_snapshot',{'at':time.time(),'settings':{'profile':{'sign':'旧签名'}}})
            s.build_context=AsyncMock(return_value={'persona':{'version':5,'dynamic':{'interests':['游戏']}},'facts':[],'activities':[],'source_ids':[]})
            s.generate=AsyncMock(return_value={'action':'settings','wait_minutes':60,'reason':'想表达新的游戏兴趣','section':'profile','fields':{'sign':'想研究游戏里的光影'}})
            r={'cycle':'a'*64,'available':['wait','settings'],'trigger':'self_chosen_wakeup'}
            got=await plan(s,r);self.assertEqual(got['decision']['action'],'settings');job=s.commands.claim()['job'];self.assertFalse(job['forced']);self.assertEqual(job['spec']['settings'],{'sign':'想研究游戏里的光影'})
            await plan(s,r);self.assertEqual(s.generate.await_count,1);self.assertEqual(s.build_context.call_args.kwargs['purpose'],'public');s.state.close()

class AdapterGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_private_stops_before_media_decode_or_routing(self):
        import ast
        source=Path('upstream_current/qq_official_adapter/plugin.py').read_text();tree=ast.parse(source)
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='QQOfficialAdapterPlugin')
        receive=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_receive_message');receive.returns=None
        for a in receive.args.args:a.annotation=None
        module=ast.Module(body=[receive],type_ignores=[]);ast.fix_missing_locations(module)
        ns={'GROUP_EVENTS':{'GROUP_AT_MESSAGE_CREATE'},'extract_command_text':lambda d:d['content']}
        exec(compile(module,'adapter_gate_test','exec'),ns)
        fake=SimpleNamespace(_id_map={},_author_openid=lambda e,a:a['user_openid'],_private_gate=AsyncMock(return_value={'allow':False}),_reply_to_command=AsyncMock(),_build_attachments=AsyncMock())
        await ns['_receive_message'](fake,'C2C_MESSAGE_CREATE',{'author':{'user_openid':'stranger'},'content':'hello'})
        fake._build_attachments.assert_not_called();fake._reply_to_command.assert_not_called()
    async def test_core_outage_is_fail_closed(self):
        import ast
        from contextlib import suppress
        tree=ast.parse(Path('upstream_current/qq_official_adapter/plugin.py').read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef))
        helper=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_private_gate')
        ns={'asyncio':asyncio,'json':json,'suppress':suppress};module=ast.Module(body=[helper],type_ignores=[]);ast.fix_missing_locations(module);exec(compile(module,'adapter_gate_test','exec'),ns)
        with patch('asyncio.open_unix_connection',side_effect=ConnectionRefusedError):
            self.assertFalse((await ns['_private_gate'](None,{'scene':'private','actor':'any'}))['allow'])

class DeploymentPermissionTests(unittest.TestCase):
    def test_source_mode_survives_private_umask(self):
        import importlib.util,os,stat
        sp=importlib.util.spec_from_file_location('deploy_autonomy','scripts/deploy_autonomy_remote.py');m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as tmp:
            old=os.umask(0o077)
            try:
                p=Path(tmp,'code.py');m.atomic(p,b'pass\n',os.getuid(),os.getgid(),0o644)
                self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o644)
            finally:os.umask(old)
