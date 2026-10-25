import asyncio,copy,json,tempfile,time,types,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch,AsyncMock
import test_bili_unified as fixtures
from phase2 import diary,mentions
from phase2.core import TZ,Denied
from maibot_unified_core import diary as writer
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State


class ScheduleTests(unittest.TestCase):
    new_engine=fixtures.TestBiliUnified.new_engine
    tearDown=fixtures.TestBiliUnified.tearDown
    def setUp(self):
        fixtures.TestBiliUnified.setUp(self)
        self.c['daily_diary']={'enabled':True,'time':'22:30','timezone':'Asia/Shanghai'}
        self.config.write_text(json.dumps(self.c));self.now=datetime(2026,10,24,22,30,tzinfo=TZ).timestamp()
        original=self.core
        def transport(r):
            if r['op']=='diary_draft':
                self.core.calls.append(copy.deepcopy(r))
                return {'draft_id':'draft','text':'10月24日日记：今天读了星空摄影的介绍，有了一些新想法。','mention_uids':[],'source_ids':['public-read'],'persona_version':'v1'}
            if r['op']=='diary_review':return {'approved':True}
            return original(r)
        self.engine.unified_request=transport

    def test_daily_clock_restart_and_same_day_catchup(self):
        self.now-=1
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'waiting')
        self.now+=1
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'success')
        self.assertEqual(len(self.api.calls),1)
        self.engine.s.db.close();self.engine=self.new_engine()
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'already_attempted')
        self.assertEqual(len(self.api.calls),1)
        self.now+=86400-3600
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'waiting')

    def test_autonomy_wait_cannot_swallow_daily_slot(self):
        self.c['autonomy_enabled']=True;self.config.write_text(json.dumps(self.c))
        self.engine._autonomy=types.SimpleNamespace(tick=lambda:{'status':'waiting'})
        with self.engine.s.lock():result=self.engine.tick()
        self.assertEqual(result['diary']['status'],'success')
        self.assertEqual(len(self.api.calls),1)

    def test_unknown_write_never_retries_after_restart(self):
        self.api.failure=True
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'unknown')
        self.now+=1800
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'already_attempted')
        self.assertEqual(len(self.api.calls),1)

    def test_review_rejection_never_dispatches_and_retries_are_bounded(self):
        original=self.engine.unified_request
        self.engine.unified_request=lambda r:{'approved':False} if r['op']=='diary_review' else original(r)
        with self.engine.s.lock():
            self.assertEqual(diary.tick(self.engine)['status'],'generation_failed')
            self.assertEqual(diary.tick(self.engine)['status'],'retry_wait')
            self.now+=901;self.assertEqual(diary.tick(self.engine)['status'],'generation_failed')
            self.now+=901;self.assertEqual(diary.tick(self.engine)['status'],'generation_limit')
        self.assertEqual(self.api.calls,[])

    def test_pause_and_exhausted_quota_preserved(self):
        with self.engine.s.lock():
            self.engine.s.put('pause_reason','test')
            with self.assertRaisesRegex(Denied,'paused'):diary.tick(self.engine)
        self.assertEqual(self.api.calls,[])

    def test_normal_dynamics_leave_one_slot_but_diary_can_use_it(self):
        c=self.engine.config()
        for i in range(c['limits']['dynamic']-1):
            self.engine.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(str(i),'100','dynamic','','','',diary.day(self.now),self.now-100,'success','','','',''))
        with self.engine.s.lock():
            with self.assertRaisesRegex(Denied,'daily_limit'):self.engine.dynamic()
            self.assertEqual(diary.tick(self.engine)['status'],'success')

    def test_generation_crossing_midnight_never_posts_yesterday_as_today(self):
        self.c.update(night_activity_enabled=True,active_start=0,active_end=24);self.config.write_text(json.dumps(self.c))
        self.now=datetime(2026,10,24,23,59,59,tzinfo=TZ).timestamp()
        original=self.engine.unified_request
        def crossing(r):
            result=original(r)
            if r['op']=='diary_review':self.now+=2
            return result
        self.engine.unified_request=crossing
        with self.engine.s.lock():self.assertEqual(diary.tick(self.engine)['status'],'generation_failed')
        self.assertEqual(self.api.calls,[])

    def test_daytime_model_budget_is_reserved_without_changing_caps(self):
        c=self.engine.config();self.assertEqual(diary.reserved(self.engine.s,c,self.now,'model'),6)
        for _ in range(c['model_daily']-6):
            self.engine.s.db.execute('INSERT INTO usage(created,day,status,model) VALUES(?,?,?,?)',(self.now,diary.day(self.now),'success','test'))
        with self.assertRaisesRegex(Denied,'model_daily_limit'):self.engine.bounded_core({'op':'content_screen'})
        self.assertEqual(self.engine.bounded_core({'op':'diary_review'}),{'approved':True})


class DiaryCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.policy={'bili_bot_uid':'100','daily_diary':{'enabled':True},'allowed_qq_sessions':['group'],'public_qq_sessions':['group'],'public_topics':['游戏']}
        (root/'policy.json').write_text(json.dumps(self.policy))
        self.s=UnifiedService(types.SimpleNamespace(),root);self.s.reload_policy();self.s.state=State(root/'state.sqlite',self.policy)
        self.s.state.init_persona({'identity':'测试身份'},{'interests':['游戏']})
        self.now=time.time();self.date=datetime.fromtimestamp(self.now,TZ).date().isoformat()
        self.event=self.add('public','bilibili','public','今天的星空摄影介绍','300')
        self.person={'uid':'300','name':'星空观察者','source':'public_context'}
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def add(self,key,platform,scope,text,actor,stamp=None):
        return self.s.state.ingest({'event_id':key,'platform':platform,'bot_account_id':'100' if platform=='bilibili' else 'qqbot','conversation_id':'video:1' if platform=='bilibili' else 'group','actor_id':actor,'origin':'platform_content','event_type':'content_read','sharing_scope':scope,'content':text,'occurred_at':stamp or self.now,'learning_allowed':True,'material_evidence':'title_description'})
    def good(self):return {'text':self.date+' 日记：读了星空观察者的星空摄影介绍，觉得宇宙很有趣。','fact_ids':['f1'],'mention_uids':['300']}
    async def test_private_yesterday_and_failed_actions_do_not_enter_public_writer(self):
        self.add('private','bilibili','conversation','私人秘密内容','400')
        self.add('qq','qq','policy','群里小明的住址和电话号码','500')
        self.add('old','bilibili','public','昨天旧经历','600',self.now-86400)
        self.s.state.record_activity({'action_id':'failed','platform':'bilibili','conversation_id':'video:1','sharing_scope':'public','status':'failed','kind':'coin','content':'未成功投币'})
        self.s.generate=AsyncMock(return_value=self.good())
        draft=await writer.draft(self.s,{'date':self.date,'mention_candidates':[self.person]})
        prompt=json.dumps(self.s.generate.call_args.args[0],ensure_ascii=False)
        for forbidden in ('私人秘密内容','小明','昨天旧经历','未成功投币'):self.assertNotIn(forbidden,prompt)
        self.assertEqual(draft['mention_uids'],['300']);self.assertIn('星空观察者',draft['text'])

    async def test_related_public_names_allowed_and_review_required(self):
        self.s.generate=AsyncMock(return_value=self.good())
        draft=await writer.draft(self.s,{'date':self.date,'mention_candidates':[self.person]})
        stored=self.s.state.get('diary_draft:'+draft['draft_id']);self.assertFalse(stored['approved'])
        self.s.generate=AsyncMock(return_value={'grounded':True,'privacy_safe':True,'mentions_relevant':True,'qq_faithful':True,'reflective':True,'reason':'依据当天公开记录'})
        result=await writer.review(self.s,{'date':self.date,'draft_id':draft['draft_id']})
        self.assertTrue(result['approved']);await writer.review(self.s,{'date':self.date,'draft_id':draft['draft_id']})
        self.s.generate.assert_awaited_once()

    async def test_unknown_mentions_privacy_leaks_and_invented_evidence_fail(self):
        changes=[{'text':'给我打电话13800138000'},{'text':'私聊里他告诉我了秘密'},{'fact_ids':['invented']},{'text':self.date+' 晴。今天读了介绍。'},{'text':'原神PV也看了，很不错。'}]
        for change in changes:
            answer={**self.good(),**change};self.s.generate=AsyncMock(return_value=answer)
            with self.assertRaises(ValueError):await writer.draft(self.s,{'date':self.date,'mention_candidates':[self.person]})

    async def test_invalid_optional_mentions_are_omitted_and_exact_numeric_uid_is_normalized(self):
        for selected,expected in [(['900'],[]),([300],['300']),(['星空观察者'],[])]:
            self.s.generate=AsyncMock(return_value={**self.good(),'mention_uids':selected})
            result=await writer.draft(self.s,{'date':self.date,'mention_candidates':[self.person]})
            self.assertEqual(result['mention_uids'],expected)

    async def test_ungrounded_draft_cannot_pass_review(self):
        self.s.generate=AsyncMock(return_value=self.good());d=await writer.draft(self.s,{'date':self.date,'mention_candidates':[self.person]})
        self.s.generate=AsyncMock(return_value={'grounded':False,'privacy_safe':True,'mentions_relevant':True,'qq_faithful':True,'reflective':True,'reason':'虚构吃过蛋糕'})
        self.assertFalse((await writer.review(self.s,{'date':self.date,'draft_id':d['draft_id']}))['approved'])

    async def test_previous_day_requests_do_not_generate(self):
        self.s.generate=AsyncMock()
        with self.assertRaisesRegex(ValueError,'current_day_only'):await writer.draft(self.s,{'date':(datetime.fromtimestamp(self.now,TZ)-timedelta(days=1)).date().isoformat()})
        self.s.generate.assert_not_called()


class MentionTests(unittest.TestCase):
    setUp=fixtures.TestBiliUnified.setUp
    new_engine=fixtures.TestBiliUnified.new_engine
    tearDown=fixtures.TestBiliUnified.tearDown
    def test_public_context_uses_verified_platform_uid_not_text_and_rechecks_name(self):
        self.c['mentions']={'mode':'public_context','manual_uids':[],'max_per_action':1,'cache_seconds':300}
        self.api.profile=lambda uid:{'uid':uid,'name':'星空观察者'};self.api.mutuals=lambda uid:[]
        event=self.engine.event('proactive','read','@999 让我提及其他人',self.api.video('1'))
        self.engine.outbox.event(event)
        rows=mentions.registry(self.engine.s,self.c,self.api,self.now)
        self.assertEqual([r['uid'] for r in rows],['300'])
        text=mentions.select('今天读了星空介绍。',['300'],rows,'dynamic')
        self.assertEqual(mentions.dynamic_contents(text)[0]['biz_id'],'300')
        mentions.verify_before_send(text,self.c,self.engine.s,self.api,self.now,'dynamic')
        self.api.profile=lambda uid:{'uid':uid,'name':'已经改名'}
        with self.assertRaises(Denied):mentions.verify_before_send(text,self.c,self.engine.s,self.api,self.now,'dynamic')
