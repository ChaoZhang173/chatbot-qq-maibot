import asyncio,hashlib,json,tempfile,time,unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from qq_channel_runner import Runner,receipt,TZ
from maibot_unified_core.qq_channel import plan
from maibot_unified_core.store import State


class ChannelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.now=datetime(2026,10,31,14,tzinfo=TZ).timestamp()
        self.c={'enabled':True,'author_confirmed':True,'account':'9000000001',
            'credential_path':'/srv/maibot/.qqcli/.env','credential_sha256':'a'*64,
            'daily_limit':6,'night_limit':2,'min_gap':7200,'decision_limit':12,'night_decision_limit':3}
        self.save();self.request=Mock();self.publish=Mock(return_value={'success':True,'data':{'feed_id':'feed-1','share':{'url':'https://pd.qq.com/s/example'}}})
        self.r=Runner(self.root,self.request,self.publish,lambda:self.now);self.r.identity=Mock()
    def save(self):(self.root/'config.json').write_text(json.dumps(self.c))
    def tearDown(self):self.r.db.close();self.tmp.cleanup()
    def draft(self,text='我宣布今天的怪点子正式开工',cycle='b'*64):
        return {'status':'draft','cycle':cycle,'text':text,'text_hash':hashlib.sha256(text.encode()).hexdigest(),
                'created_at':self.now,'persona_version':9,'source_ids':[]}
    def seed(self,stamp,index,status='success'):
        self.r.db.execute('INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,?,0)',(str(index),stamp,status,'此前的想法',str(index),'id','','9','[]'))
    def test_disabled_or_unconfirmed_never_calls_core_or_publisher(self):
        for k in ['enabled','author_confirmed']:
            self.c[k]=False;self.save();self.assertEqual(self.r.tick()['status'],'disabled')
            self.c[k]=True;self.save()
        self.request.assert_not_called();self.publish.assert_not_called()
    def test_success_gap_and_deduplication_across_restart(self):
        draft=self.draft();self.assertEqual(self.r.publish_draft(draft,self.c)['status'],'success')
        self.assertEqual(self.r.publish_draft(self.draft(cycle='c'*64),self.c)['status'],'post_gap')
        self.now+=7201;self.r.db.close();self.r=Runner(self.root,self.request,self.publish,lambda:self.now);self.r.identity=Mock()
        self.assertEqual(self.r.publish_draft(self.draft(cycle='d'*64),self.c)['status'],'already_attempted')
        self.assertEqual(self.publish.call_count,1)
    def test_timeout_counts_and_is_not_replayed(self):
        self.publish.side_effect=TimeoutError();draft=self.draft()
        self.assertEqual(self.r.publish_draft(draft,self.c)['status'],'unknown')
        self.now+=7201;self.r.db.close();self.r=Runner(self.root,self.request,self.publish,lambda:self.now);self.r.identity=Mock()
        self.assertEqual(self.r.publish_draft(self.draft(),self.c)['status'],'already_attempted')
        self.assertEqual(self.publish.call_count,1)
    def test_rejected_post_is_not_retried_or_globally_paused(self):
        self.publish.return_value={'success':False,'error':{'message':'内容涉嫌违规'}}
        self.assertEqual(self.r.publish_draft(self.draft(),self.c)['status'],'failed')
        self.assertEqual(self.r.tick()['status'],'post_gap')
        self.request.return_value={'status':'skipped','wait_minutes':120}
        self.now+=8000;self.assertEqual(self.r.tick()['status'],'skipped');self.assertEqual(self.publish.call_count,1)
    def test_whole_day_and_cross_midnight_night_limits(self):
        for i in range(6):self.seed(self.now-20000-i,i,'unknown' if i==0 else 'success')
        self.assertEqual(self.r.gate(self.c,self.now),'daily_limit')
        self.r.db.execute('DELETE FROM posts')
        self.now=datetime(2026,10,31,23,30,tzinfo=TZ).timestamp()
        self.seed(self.now,10);self.seed(self.now+60,11)
        self.now=datetime(2026,11,1,8,tzinfo=TZ).timestamp();self.assertEqual(self.r.gate(self.c,self.now),'night_limit')
        self.now=datetime(2026,11,1,9,1,tzinfo=TZ).timestamp();self.assertEqual(self.r.gate(self.c,self.now),'')
    def test_stale_or_tampered_draft_never_publishes(self):
        for change in [{'created_at':self.now-901},{'text':'偷偷替换文案'}]:
            with self.assertRaises(ValueError):self.r.publish_draft({**self.draft(),**change},self.c)
        self.publish.assert_not_called()
    def test_model_can_skip_without_posting(self):
        self.request.return_value={'status':'skipped','wait_minutes':120}
        self.assertEqual(self.r.tick()['status'],'skipped');self.publish.assert_not_called()
        self.assertEqual(self.r.tick()['status'],'waiting');self.assertEqual(self.request.call_count,1)
    def test_receipts_do_not_claim_public_visibility(self):
        self.assertEqual(receipt({'success':True,'data':{}}),('unknown','',''))
        self.assertEqual(receipt({'success':True,'data':{'feedId':'id'}}),('success','id',''))
        self.assertEqual(receipt({'success':False}),('failed','',''))
    def test_only_public_posts_from_this_account_are_shared_with_qq(self):
        state=State(self.root/'core.sqlite',{'bot_accounts':{'qq_channel':'9000000001'}})
        source={'platform':'qq_channel','bot_account_id':'9000000001','conversation_id':'qq_channel:global',
                'sharing_scope':'public','origin':'bot_self'}
        self.assertTrue(state.allowed(source,'qq','group','internal'))
        self.assertTrue(state.allowed(source,'bilibili','self','public'))
        self.assertFalse(state.allowed({**source,'is_private':True},'qq','group','internal'))
        self.assertFalse(state.allowed({**source,'bot_account_id':'someone-else'},'qq','group','internal'))
        state.close()


class ChannelPlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.values={};self.state=SimpleNamespace(get=lambda k:self.values.get(k),put=lambda k,v:self.values.__setitem__(k,v),audit_context=Mock())
        self.s=SimpleNamespace(reload_policy=lambda:None,policy={'qq_channel_enabled':True},state=self.state,busy=asyncio.Lock(),
            build_context=AsyncMock(return_value={'persona':{'version':9},'facts':[],'activities':[],'source_ids':[]}),
            generate=AsyncMock(side_effect=[{'willing':True,'text':'假如鲸鱼来当游戏最终BOSS呢','wait_minutes':90},{'approved':True}]))
    async def test_public_context_review_and_cached_plan(self):
        r={'cycle':'a'*64,'history':[]};answer=await plan(self.s,r)
        self.assertEqual(answer['status'],'draft');self.assertEqual(self.s.build_context.call_args.kwargs['purpose'],'public')
        self.assertEqual(await plan(self.s,r),answer);self.assertEqual(self.s.generate.await_count,2)
    async def test_decline_or_review_rejection_no_publishable_draft(self):
        self.s.generate.side_effect=[{'willing':False}]
        self.assertEqual((await plan(self.s,{'cycle':'a'*64}))['status'],'skipped')
        self.s.generate.side_effect=[{'willing':True,'text':'今天又有新的怪想法'},{'approved':False}]
        self.assertEqual((await plan(self.s,{'cycle':'b'*64}))['status'],'skipped')
    async def test_private_or_unapproved_mentions_are_rejected(self):
        self.s.generate.side_effect=[{'willing':True,'text':'群友说他的QQ号是12345678'}]
        with self.assertRaises(ValueError):await plan(self.s,{'cycle':'a'*64})
        self.assertEqual(self.s.generate.await_count,1)
