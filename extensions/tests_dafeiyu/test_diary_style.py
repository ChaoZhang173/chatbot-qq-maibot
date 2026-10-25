import json,time,types,unittest
from unittest.mock import AsyncMock,patch
import test_diary as fixtures
from maibot_unified_core import diary,diary_episodes
from phase2 import diary as schedule,mentions


class EpisodesTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.DiaryCoreTests.setUp
    tearDown=fixtures.DiaryCoreTests.tearDown
    add=fixtures.DiaryCoreTests.add

    def chat(self,ident,text,*,scope='policy',session='group',when=None):
        stamp=self.now-20 if when is None else when
        return self.s.state.ingest({'event_id':ident,'platform':'qq','bot_account_id':'qqbot','conversation_id':session,
            'actor_id':'qq-user','nickname':'小明同学','origin':'external_user','event_type':'message_received',
            'sharing_scope':scope,'learning_allowed':True,'content':text,'occurred_at':stamp})

    def reply(self,ident,text,session='group',scope='policy'):
        self.s.state.record_activity({'action_id':ident,'platform':'qq','conversation_id':session,'sharing_scope':scope,
            'kind':'reply','status':'succeeded','receipt_verified':True,'content':text,'created_at':self.now-10})

    async def test_group_windows_anonymize_and_exclude_private_sensitive_and_other_day(self):
        self.s.policy['qq_group_sessions']={'native':'group'}
        self.chat('qq1','小明同学说这个蛋糕搭配好有趣，怎么看？')
        self.chat('private','小明同学私聊告诉你地址',scope='conversation')
        self.chat('medical','小明同学刚刚住院，诊断是私人病情')
        self.chat('yesterday','昨天聊了完全不同的游戏',when=self.now-86400)
        self.chat('outside','别的群在聊私人工作',session='other')
        self.reply('reply1','这个搭配让我好奇！群友提的红茶和柿子组合很有季节感。')
        windows,names=diary_episodes.collect(self.s,self.date,self.now)
        text=json.dumps(windows,ensure_ascii=False)
        self.assertEqual(len(windows),1);self.assertIn('小明同学',names)
        for private in ('小明同学','地址','住院','诊断','完全不同','私人工作'):self.assertNotIn(private,text)
        self.assertIn('蛋糕搭配',text)
        self.s.generate=AsyncMock(return_value={'episodes':[{'window_id':'w1','summary':'群友聊起蛋糕搭配，大肥鱼也围绕红茶和柿子的组合参与讨论。'}]})
        result=await diary_episodes.prepare(self.s,{'date':self.date})
        self.assertEqual(result['count'],1)
        self.s.generate.reset_mock();again=await diary_episodes.prepare(self.s,{'date':self.date})
        self.assertTrue(again['cached']);self.s.generate.assert_not_called()

    async def test_personal_names_in_summary_are_not_passed_to_writer(self):
        self.s.policy['qq_group_sessions']={'native':'group'}
        self.chat('qq1','这个蛋糕有点意思，你喜欢哪个搭配？')
        self.reply('reply1','小明同学提的搭配让我好奇。')
        self.s.generate=AsyncMock(return_value={'episodes':[{'window_id':'w1','summary':'小明同学与大肥鱼讨论了蛋糕。'}]})
        result=await diary_episodes.prepare(self.s,{'date':self.date})
        self.assertEqual(result['count'],0)

    async def test_reflective_long_draft_receives_summaries_not_raw_chat_and_keeps_sources(self):
        self.s.policy['qq_group_sessions']={'native':'group'}
        event=self.chat('qq1','小明同学说这个蛋糕搭配好有趣，怎么看？')
        self.reply('reply1','红茶和柿子的搭配让我好奇。')
        self.s.generate=AsyncMock(return_value={'episodes':[{'window_id':'w1','summary':'群友聊起蛋糕搭配，大肥鱼围绕红茶和柿子的组合参与讨论。'}]})
        result=await diary_episodes.prepare(self.s,{'date':self.date})
        body=self.date+'\n'+('读到柿子和红茶的组合时，我觉得有趣的不是收藏了几条，而是有人愿意把一个小点子接着聊下去。'*8)
        self.assertGreater(len(body),250)
        async def generate(messages,**kwargs):
            data=json.loads(messages[1]['content']);fact=next(f for f in data['facts'] if f['kind']=='qq_interaction')
            return {'text':body,'fact_ids':[fact['id']],'mention_uids':[]}
        self.s.generate=AsyncMock(side_effect=generate)
        drafted=await diary.draft(self.s,{'date':self.date,'episode_id':result['episode_id']})
        writer_input=json.dumps(self.s.generate.call_args.args[0],ensure_ascii=False)
        self.assertNotIn('小明同学',writer_input);self.assertNotIn('好有趣，怎么看',writer_input)
        self.assertIn('蛋糕搭配',writer_input)
        self.assertIn(event['event_id'],drafted['source_ids']);self.assertIn('reply1',drafted['source_ids'])
        self.s.generate=AsyncMock(return_value={'grounded':True,'privacy_safe':True,'mentions_relevant':True,'qq_faithful':True,'reflective':False,'reason':'仍在罗列操作'})
        review=await diary.review(self.s,{'date':self.date,'draft_id':drafted['draft_id']})
        self.assertFalse(review['approved'])


class LongDeliveryTests(unittest.TestCase):
    setUp=fixtures.ScheduleTests.setUp
    tearDown=fixtures.ScheduleTests.tearDown
    new_engine=fixtures.ScheduleTests.new_engine

    def test_long_diary_with_real_mention_structure_reaches_sender_intact(self):
        self.c['mentions']={'mode':'manual','manual_uids':['300'],'max_per_action':1,'cache_seconds':300}
        self.config.write_text(json.dumps(self.c))
        self.api.profile=lambda uid:{'uid':uid,'name':'星空观察者'}
        body='2026-10-24\n'+('今天读到这段星空介绍，我更想把好奇心留给内容本身。'*18)
        original=self.engine.unified_request
        def core(r):
            if r['op']=='diary_draft':return {'draft_id':'long','text':body,'mention_uids':['300'],'source_ids':['read'],'persona_version':'v1'}
            return original(r)
        self.engine.unified_request=core
        with self.engine.s.lock():result=schedule.tick(self.engine)
        self.assertEqual(result['status'],'success')
        delivered=self.api.calls[0][2]
        self.assertEqual(delivered.body,body);self.assertGreater(len(delivered),250)
        self.assertEqual(mentions.dynamic_contents(delivered)[0]['biz_id'],'300')

    def test_long_allowance_does_not_leak_into_other_dynamics(self):
        body='这只是普通动态。'*50
        with self.engine.s.lock():result=self.engine.sender.deliver('dynamic',None,'ordinary',lambda:body)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.api.calls,[])

    def test_body_above_eight_hundred_is_rejected_before_delivery(self):
        from phase2.core import Denied
        with self.assertRaises(Denied):mentions.select('字'*801,['300'],[{'uid':'300','name':'星空观察者','source':'manual'}],'dynamic',limit=850)
