import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from datetime import datetime

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from maibot_unified_core.service import QQ_IDENTITY_QUEUE_LIMIT, QQ_IDENTITY_QUEUE_TTL, TZ, UnifiedService
from maibot_unified_core.store import State


class QQServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.policy={'qq_bot_account':'bot-account','allowed_qq_sessions':['a','b'],
                     'qq_group_sessions':{'group-a':'a','group-b':'b'},'shared_groups':[['a','b']]}
        (root/'policy.json').write_text(json.dumps(self.policy))
        class Person:
            async def get_id(self,platform,actor):
                return hashlib.md5((platform+'_'+actor).encode()).hexdigest()
        self.service=UnifiedService(types.SimpleNamespace(person=Person()),root=root)
        self.service.policy=self.policy
        self.service.state=State(root/'state.sqlite',self.policy)
        self.service.state.init_persona({'identity':'existing user persona'},{'interests':['AI']})

    def tearDown(self):
        self.service.state.close()
        self.temp.cleanup()

    @staticmethod
    def message(mid='msg-1',session='a',actor='same-user',text='测试偏好',stamp='1000'):
        return {'message_id':mid,'platform':'qq','account_id':'bot-account','session_id':session,
                'scope':'group','timestamp':stamp,'message_info':{
                    'user_info':{'user_id':actor,'user_nickname':'same nickname'},
                    'group_info':{'group_id':'group-'+session},'additional_config':{}},
                'raw_message':[{'type':'text','data':text}]}

    async def test_host_identity_precedes_ingest_and_same_person_crosses_groups(self):
        a=await self.service.ingest_qq(self.message())
        b=await self.service.ingest_qq(self.message(mid='msg-2',session='b'))
        expected=hashlib.md5(b'qq_same-user').hexdigest()
        self.assertEqual(a['person_id'],expected)
        self.assertEqual(b['person_id'],expected)
        self.assertEqual(self.service.state.get_event(a['event_id'])['content'],'测试偏好')
        other=await self.service.ingest_qq(self.message(mid='msg-3',actor='different-user'))
        self.assertNotEqual(other['person_id'],expected)
        duplicate=await self.service.ingest_qq(self.message())
        self.assertFalse(duplicate['is_new'])

    async def test_self_media_and_mismatched_account_are_not_user_learning(self):
        msg=self.message(actor='bot-account')
        msg['raw_message'].append({'type':'image','hash':'image-hash','binary_data_base64':'sensitive-bytes','data':'https://x/?signature=secret'})
        result=await self.service.ingest_qq(msg)
        event=self.service.state.get_event(result['event_id'])
        self.assertFalse(event['learning_allowed'])
        self.assertNotIn('sensitive-bytes',str(event))
        self.assertEqual(event['attachment_refs'],['qq-media:image-hash'])
        msg=self.message(mid='bad-account');msg['account_id']='different-bot'
        self.assertIsNone(await self.service.ingest_qq(msg))

    async def test_parallel_replies_match_changed_platform_ids_and_adoption(self):
        self.service.state.put('qq_native_candidates:a',{'at':__import__('time').time(),'items':[
            {'source_id':'jargon:7','kind':'jargon','literal':'轻量','source':{'platform':'qq','conversation_id':'a'}},
            {'source_id':'jargon:8','kind':'jargon','literal':'秘密','source':{'platform':'qq','conversation_id':'private'}}]})
        first=self.message(mid='local-1',actor='bot-account',text='轻量执行',stamp='1000')
        second=self.message(mid='local-2',actor='bot-account',text='第二条',stamp='1001')
        await self.service.record_qq_pending(first);await self.service.record_qq_pending(second)
        first['message_id']='native-1'
        action=await self.service.record_qq_sent(first,True)
        self.assertEqual(action['local_message_id'],'local-1')
        self.assertEqual(action['remote_id'],'native-1')
        self.assertEqual(action['status'],'succeeded')
        replay=await self.service.record_qq_sent(first,True)
        self.assertEqual(replay['action_id'],action['action_id'])
        self.assertEqual(len(self.service.state.recent_activity()),2)
        adopted=self.service.state.get('native_adoption:'+action['action_id'])
        self.assertEqual([x['source_id'] for x in adopted['adopted']],['jargon:7'])
        self.assertTrue(self.service.state.get(self.service.qq_send_key(second,'a')))
        failed=await self.service.record_qq_sent(second,False)
        self.assertEqual(failed['status'],'unknown')
        self.assertIsNone(self.service.state.get('native_adoption:'+failed['action_id']))

    async def test_native_candidates_and_profiles_are_evidence_scoped(self):
        await self.service.ingest_qq(self.message())
        async def memory(operation,**kwargs):
            if operation=='expression_list':
                return {'items':[{'id':1,'session_id':'a','style':'轻量','situation':'小任务'},
                                 {'id':99,'session_id':'private','style':'secret','situation':'private'}]}
            if operation=='jargon_list':
                return {'items':[{'id':2,'is_jargon':True,'session_id_dict':'{"b": 2}','content':'稳稳','meaning':'可靠'},
                                 {'id':90,'is_jargon':True,'is_global':True,'session_id_dict':'{"private":1}','content':'secret'}]}
            if operation=='profile_evidence':
                return {'profile_text':'UNSCOPED SUMMARY','evidence':[
                    {'hash':'allowed','content':'偏好简短','metadata':{'chat_id':'b'}},
                    {'hash':'denied','content':'private fact','metadata':{'chat_id':'private'}}]}
            return {}
        self.service.memory=memory
        candidates=await self.service.native_learning_context('a')
        self.assertEqual([x['source_id'] for x in candidates['expressions']],['expression:1'])
        self.assertEqual([x['source_id'] for x in candidates['jargon']],['jargon:2'])
        profiles=await self.service.native_profile_context('a')
        self.assertEqual([x['source_id'] for x in profiles],['allowed'])
        self.assertNotIn('UNSCOPED',str(profiles))

    async def test_memory_failure_keeps_receipt_activity_and_dynamic_persona(self):
        async def fail(*args,**kwargs):raise RuntimeError('offline')
        self.service.build_context=fail;self.service.memory=fail
        pending=self.message(mid='sent-local',actor='bot-account',text='确认过的回复')
        await self.service.record_qq_pending(pending)
        pending['message_id']='sent-remote';await self.service.record_qq_sent(pending,True)
        self.service.last_message['a']=('告诉我操作结果','query','user')
        text=await self.service.qq_context('a')
        self.assertIn('确认过的回复',text)
        self.assertIn('"memory_available":false',text)
        self.assertIn('"persona_version":1',text)

    async def test_host_persona_revision_preserves_dynamic_and_rejects_reflection_identity(self):
        values={'bot.nickname':'Host角色','bot.alias_names':['别名'],
                'personality.personality':'管理员维护的身份','personality.behavior_style':'管理员维护的行为','personality.reply_style':'自然简短',
                'personality.multiple_reply_style':['轻松']}
        async def get(key):return values[key]
        self.service.ctx.config=types.SimpleNamespace(get=get)
        result=await self.service.sync_host_persona()
        self.assertEqual(result['version'],2)
        self.assertEqual(self.service.state.persona()['dynamic'],{'interests':['AI']})
        self.assertEqual(self.service.state.persona(1)['stable'],{'identity':'existing user persona'})
        self.assertEqual((await self.service.sync_host_persona())['version'],2)
        values['bot.nickname']='管理员的新名字'
        await self.service.sync_host_persona()
        self.assertEqual(self.service.state.persona()['stable']['bot.nickname'],'管理员的新名字')
        rejected=self.service.state.propose_changes({'bot.nickname':'模型伪造名字'},[],'reflection')
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['reason'],'stable_or_unsupported_field')
        self.assertEqual(self.service.state.persona()['version'],3)

    async def test_partial_host_config_preserves_complete_persona(self):
        before=self.service.state.persona()
        async def get(key):
            if key=='bot.nickname':return 'partial new name'
            raise RuntimeError('transient rpc')
        self.service.ctx.config=types.SimpleNamespace(get=get)
        self.assertEqual((await self.service.sync_host_persona())['status'],'deferred')
        self.assertEqual(self.service.state.persona(),before)
        self.assertNotIn('partial new name',str(self.service.state.get('host_persona_sync')))

    async def test_restart_accepts_trusted_host_config_change(self):
        values={'bot.nickname':'Host角色','bot.alias_names':[],
                'personality.personality':'身份','personality.behavior_style':'温和行为','personality.reply_style':'简短',
                'personality.multiple_reply_style':[]}
        async def get(key):return values[key]
        async def idle():await asyncio.Event().wait()
        root=self.service.root/'restart';root.mkdir()
        (root/'policy.json').write_text(json.dumps(self.policy))
        ctx=types.SimpleNamespace(config=types.SimpleNamespace(get=get),logger=types.SimpleNamespace(info=lambda *_:None))
        # The local sandbox forbids binding Unix sockets; isolate that unrelated
        # transport boundary while exercising the real startup and persisted DB.
        class Server:
            def close(self):pass
            async def wait_closed(self):pass
        async def server(callback,path,**kwargs):Path(path).touch();return Server()
        with patch('maibot_unified_core.service.asyncio.start_unix_server',server):
            first=UnifiedService(ctx,root);first.background_loop=idle
            try:await first.start()
            finally:await first.stop()
            values['personality.reply_style']='管理员调整表达'
            second=UnifiedService(ctx,root);second.background_loop=idle
            try:
                await second.start()
                self.assertEqual(second.state.persona()['version'],2)
                self.assertEqual(second.state.persona()['dynamic']['interests'],['AI','游戏','美食'])
            finally:await second.stop()

    async def test_identity_retry_sanitizes_deduplicates_and_preserves_latest_context(self):
        native=self.service.ctx.person
        async def unavailable(*args):raise RuntimeError('identity unavailable')
        self.service.ctx.person=types.SimpleNamespace(get_id=unavailable)
        message=self.message(text='原始偏好 https://x.test/?secret=value',stamp='1000')
        message['raw_message'].append({'type':'image','hash':'image-hash','binary_data_base64':'private-image-bytes'})
        self.assertTrue((await self.service.ingest_qq(message))['retry_queued'])
        await self.service.ingest_qq(message)
        self.assertEqual(len(self.service.pending_qq_identity),1)
        self.assertEqual(self.service.state.recent_events(),[])
        queued=str(self.service.pending_qq_identity)
        self.assertNotIn('secret=value',queued)
        self.assertNotIn('private-image-bytes',queued)
        self.service.ctx.person=native
        latest=await self.service.ingest_qq(self.message(mid='new-message',stamp='2000'))
        for pending in self.service.pending_qq_identity.values():pending['next_at']=0
        result=await self.service.retry_qq_identities()
        self.assertEqual(result['completed'],1)
        self.assertEqual(result['pending'],0)
        self.assertEqual(self.service.last_message['a'][1],latest['event_id'])
        events=self.service.state.recent_events()
        self.assertEqual(len(events),2)
        self.assertEqual({e['person_id'] for e in events},{hashlib.md5(b'qq_same-user').hexdigest()})
        replay=await self.service.ingest_qq(message)
        self.assertFalse(replay['is_new'])

    async def test_identity_retry_is_bounded_and_rechecks_scope(self):
        async def unavailable(*args):raise RuntimeError('identity unavailable')
        self.service.ctx.person=types.SimpleNamespace(get_id=unavailable)
        await self.service.ingest_qq(self.message())
        sample=next(iter(self.service.pending_qq_identity.values()))['event']
        for index in range(QQ_IDENTITY_QUEUE_LIMIT+5):
            self.service.queue_qq_identity({**sample,'event_id':'queued:'+str(index)})
        self.assertEqual(len(self.service.pending_qq_identity),QQ_IDENTITY_QUEUE_LIMIT)
        for pending in self.service.pending_qq_identity.values():pending['next_at']=0
        limited=await self.service.retry_qq_identities()
        self.assertEqual(limited['attempted'],4)
        self.service.policy['allowed_qq_sessions']=[]
        result=await self.service.retry_qq_identities()
        self.assertEqual(result['pending'],0)
        self.assertEqual(result['expired'],QQ_IDENTITY_QUEUE_LIMIT)
        self.assertEqual(self.service.state.recent_events(),[])
        self.service.policy['allowed_qq_sessions']=['a']
        self.service.queue_qq_identity(sample)
        for pending in self.service.pending_qq_identity.values():pending['queued_at']-=QQ_IDENTITY_QUEUE_TTL+1
        self.assertEqual((await self.service.retry_qq_identities())['expired'],1)

    async def test_decision_pause_reads_fresh_policy_before_model(self):
        self.service.policy['decisions_enabled']=True
        (self.service.root/'policy.json').write_text(json.dumps({**self.policy,'decisions_enabled':False}))
        async def unexpected(*args,**kwargs):self.fail('paused decision reached a model or context call')
        self.service.generate=unexpected;self.service.build_context=unexpected
        with self.assertRaisesRegex(PermissionError,'decisions_disabled'):
            await self.service.decide({},admin=True)

    async def test_explicit_feedback_reuses_learner_call_without_repeating_daily_reflection(self):
        calls=[]
        async def generate(*args,**kwargs):
            calls.append(kwargs)
            return {'remember':False,'feedback':{'field':'response_length','value':'简短','reason':'明确长度反馈'}}
        self.service.generate=generate
        event=await self.service.ingest_qq(self.message(text='以后请回复短一些，不要太长'))
        day=datetime.now(TZ).date().isoformat()
        self.service.state.put('reflected_day',day)
        self.service.state.put('reflected_at',1234)
        self.service.state.put('last_reflection',{'result':{'accepted':False,'reason':'no_supported_change'}})
        await self.service.learning_tick(force=True)
        self.assertEqual(len(calls),1)
        persona=self.service.state.persona()
        self.assertEqual(persona['dynamic']['response_length'],'简短')
        self.assertEqual(persona['stable'],{'identity':'existing user persona'})
        audit=self.service.state.get('feedback:'+event['event_id'])
        self.assertTrue(audit['result']['accepted'])
        self.assertEqual(audit['result']['previous_values'],{'response_length':None})
        self.assertEqual(audit['evidence_ids'],[event['event_id']])
        self.assertEqual(self.service.state.quota_used(),1)
        self.assertEqual(self.service.state.quota_used('explicit_feedback_update'),1)
        self.assertEqual(self.service.state.get('reflected_day'),day)
        self.assertEqual(self.service.state.get('reflected_at'),1234)
        self.assertEqual(self.service.state.get('last_reflection')['result']['reason'],'no_supported_change')
        repeated=self.service.apply_explicit_feedback(event,{'field':'response_length','value':'简短'})
        self.assertTrue(repeated['replayed'])
        self.assertEqual(self.service.state.get('feedback:'+event['event_id']),audit)
        self.assertEqual((await self.service.reflect(force=True))['skipped'],'daily_limit')
        await self.service.learning_tick(force=True)
        self.assertEqual(len(calls),1)

    async def test_feedback_daily_limit_survives_restart_and_does_not_spend_model_budget(self):
        first=await self.service.ingest_qq(self.message(text='请回复短一些'))
        result=self.service.apply_explicit_feedback(first,{'field':'response_length','value':'简短'})
        self.assertTrue(result['accepted'])
        self.service.state.close()
        self.service.state=State(self.service.root/'state.sqlite',self.policy)
        later=await self.service.ingest_qq(self.message(mid='later-feedback',text='请回答详细一些'))
        rejected=self.service.apply_explicit_feedback(later,{'field':'response_length','value':'详细'})
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['reason'],'explicit_feedback_daily_limit')
        self.assertEqual(self.service.state.persona()['version'],2)
        self.assertEqual(self.service.state.persona()['dynamic']['response_length'],'简短')
        self.assertEqual(self.service.state.quota_used(),0)

    async def test_ordinary_preference_cannot_become_model_invented_expression_feedback(self):
        event=await self.service.ingest_qq(self.message(text='测试偏好，请记住：我喜欢西瓜'))
        result=self.service.apply_explicit_feedback(event,{'field':'response_length','value':'简短'})
        self.assertEqual(result['reason'],'explicit_original_feedback_required')
        self.assertEqual(self.service.state.persona()['version'],1)
        self.assertEqual(self.service.state.quota_used('explicit_feedback_update'),0)

    async def test_feedback_rechecks_switch_scope_origin_and_field_without_spending_slot(self):
        event=await self.service.ingest_qq(self.message(text='请回复短一些'))
        proposal={'field':'response_length','value':'简短'}
        for flag in ('learning_enabled','reflection_enabled','personality_enabled','personality_updates_enabled'):
            (self.service.root/'policy.json').write_text(json.dumps({**self.policy,flag:False}))
            self.assertEqual(self.service.apply_explicit_feedback(event,proposal)['reason'],'personality_updates_paused')
        (self.service.root/'policy.json').write_text(json.dumps(self.policy))
        self.assertEqual(self.service.apply_explicit_feedback(event,{'field':'bot.nickname','value':'different'})['reason'],'unsupported_feedback_field')
        private=self.message(mid='private-feedback',text='请回复短一些');private['scope']='private'
        private_event=await self.service.ingest_qq(private)
        self.assertEqual(self.service.apply_explicit_feedback(private_event,proposal)['reason'],'private_or_unshared_evidence_cannot_change_global_persona')
        own=await self.service.ingest_qq(self.message(mid='self-feedback',text='请回复短一些',actor='bot-account'))
        self.assertEqual(self.service.apply_explicit_feedback(own,proposal)['reason'],'explicit_original_feedback_required')
        self.assertEqual(self.service.state.quota_used('explicit_feedback_update'),0)
        self.assertTrue(self.service.apply_explicit_feedback(event,proposal)['accepted'])

    async def test_phrase_feedback_requires_original_literal_and_preserves_existing_avoidance(self):
        event=await self.service.ingest_qq(self.message(text='请少用“哼哼”和“哎呀”这些口头禅'))
        self.assertTrue(self.service.state.propose_changes({'avoid_phrases':['哎呀']},[event['event_id']],'已有表达偏好')['accepted'])
        self.assertEqual(self.service.apply_explicit_feedback(event,{'field':'avoid_phrases','value':'哼哼'})['reason'],'invalid_or_unsupported_feedback_value')
        self.assertEqual(self.service.apply_explicit_feedback(event,{'field':'avoid_phrases','value':['模型捏造的短语']})['reason'],'invalid_or_unsupported_feedback_value')
        self.assertEqual(self.service.state.quota_used('explicit_feedback_update'),0)
        result=self.service.apply_explicit_feedback(event,{'field':'avoid_phrases','value':['哼哼']})
        self.assertTrue(result['accepted'])
        self.assertEqual(self.service.state.persona()['dynamic']['avoid_phrases'],['哎呀','哼哼'])
        self.assertEqual(result['previous_values'],{'avoid_phrases':['哎呀']})

    async def test_single_character_catchphrase_feedback_is_supported(self):
        event=await self.service.ingest_qq(self.message(text='以后尽量少用“哼”这个口头禅'))
        result=self.service.apply_explicit_feedback(event,{'field':'avoid_phrases','value':['哼']})
        self.assertTrue(result['accepted'])
        self.assertEqual(self.service.state.persona()['dynamic']['avoid_phrases'],['哼'])

    async def test_test_feedback_rollback_retires_only_explicit_test_material(self):
        event=await self.service.ingest_qq(self.message(text='这是一条习惯测试，以后少用哼这个口头禅'))
        baseline=self.service.state.persona()['version']
        self.assertTrue(self.service.apply_explicit_feedback(event,{'field':'avoid_phrases','value':['哼']})['accepted'])
        result=await self.service.dispatch({'op':'rollback','version':baseline,'retire_test_evidence':True,'reason':'结束临时习惯验收'},admin=True)
        self.assertEqual(result['retired_test_evidence_count'],1)
        self.assertNotIn('avoid_phrases',self.service.state.persona()['dynamic'])
        self.assertFalse(self.service.state.get_event(event['event_id'])['active'])
        self.assertFalse(self.service.state.allowed(self.service.state.get_event(event['event_id']),'qq','a','internal'))

    async def test_admin_native_correction_operations_reuse_host_adapter(self):
        calls=[]
        async def memory(operation,**kwargs):calls.append((operation,kwargs));return {'success':True}
        self.service.memory=memory
        for operation in ('paragraph_get','correction_preview','correction_execute','correction_rollback'):
            result=await self.service.dispatch({'op':'memory_admin','operation':operation,'args':{'hash':'synthetic-target'}},admin=True)
            self.assertTrue(result['result']['success'])
        self.assertEqual([x[0] for x in calls],['paragraph_get','correction_preview','correction_execute','correction_rollback'])


if __name__=='__main__':unittest.main()
