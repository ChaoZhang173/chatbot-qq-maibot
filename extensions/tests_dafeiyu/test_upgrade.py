import asyncio,copy,io,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from PIL import Image
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from maibot_unified_core.media import MediaStore
from maibot_unified_core.gallery import Gallery
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core import receipts,compact_context,engagement_decision
from test_qq_host_adapter import load_plugin

def picture(color='blue'):
    out=io.BytesIO();Image.new('RGB',(40,30),color).save(out,'PNG');return out.getvalue()

class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.policy={'persona_learning_schema':2,'qq_bot_account':'bot','allowed_qq_sessions':['a','b'],'qq_group_sessions':{'ga':'a','gb':'b'},
                     'qq_bili_commands_enabled':True,'qq_owner_bindings':{'a':'owner'},'qq_reply_policy':{'enabled':True,'casual_probability':1}}
        (root/'policy.json').write_text(json.dumps(self.policy))
        self.s=UnifiedService(SimpleNamespace(send=SimpleNamespace(image=AsyncMock(return_value={'sent':True,'message_id':'sent-1'}),text=AsyncMock(return_value={'sent':True}))),root)
        self.s.policy=self.policy;self.s.state=State(root/'state.sqlite',self.policy)
        self.s.state.init_persona({'identity':'fixed'},{'interests':['游戏','美食'],'reply_style':'自然'})
        self.s.media=MediaStore(self.s);self.s.gallery=Gallery(self.s);self.s.commands=CommandQueue(self.s)
        self.s.generate=AsyncMock(return_value={'allowed':True,'description':'蓝色游戏风格图片'})
        self.s.memory=AsyncMock(return_value={'vector':[1.0]+[0.0]*31,'space':'test-space'})
        self.s.last_message['a']=('聊聊游戏','qq:message','owner')

    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def add(self,color='blue',session='a',origin='qq_group'):
        return self.s.media.put(picture(color),session,'owner',color,origin,'https://example.com/'+color+'.png' if origin=='public_web' else '')
    async def save(self,ref=None,**kw):
        return await self.s.gallery.save(ref or self.add(),'a','喜欢蓝色游戏画面',['游戏','蓝色'],80,**kw)

    async def test_saved_image_survives_cache_expiry_restart_and_sends_once(self):
        ref=self.add();r=await self.save(ref);ident=r['image_id']
        self.s.state.db.execute('UPDATE scoped_media SET created=?',(time.time()-3700,));self.s.media.cleanup()
        self.s.gallery=Gallery(self.s)
        _,raw=self.s.gallery.get(ident,'a');self.assertTrue(raw.startswith(b'\xff\xd8'))
        found=await self.s.gallery.search('游戏蓝色','a');self.assertEqual(found['images'][0]['image_id'],ident)
        self.assertEqual((await self.s.gallery.send(ident,'a'))['status'],'success')
        self.s.state.put('qq_image_last:a',0)
        await self.s.gallery.send(ident,'a')
        self.assertEqual(self.s.ctx.send.image.call_count,1)
        self.assertEqual(self.s.gallery.get(ident,'a')[0]['use_count'],1)
        self.assertEqual(self.s.ctx.send.text.call_count,0)

    async def test_account_gallery_shares_only_with_authorized_sessions(self):
        local=(await self.save())['image_id']
        self.assertEqual(self.s.gallery.get(local,'b')[0]['scope'],'account:qq:bot')
        self.assertEqual((await self.s.gallery.search('蓝色','b'))['images'][0]['image_id'],local)
        pub=(await self.save(self.add('red',origin='public_web')))['image_id']
        self.assertEqual(self.s.gallery.get(pub,'b')[0]['scope'],'account:qq:bot')
        with self.assertRaises(ValueError):await self.s.gallery.search('蓝色','private:unknown')

    async def test_count_and_byte_limits_replacement_preserves_failed_old_item(self):
        first=(await self.save())['image_id'];second=self.add('red')
        with patch('maibot_unified_core.gallery.MAX_IMAGES',1):
            with self.assertRaisesRegex(ValueError,'已满'):await self.save(second)
            self.assertEqual(self.s.gallery.stats()['images'],1)
            with patch('maibot_unified_core.gallery.MAX_BYTES',1):
                with self.assertRaisesRegex(ValueError,'已满'):await self.save(second,replace_id=first)
                self.s.gallery.get(first,'a')
            replacement=await self.save(second,replace_id=first)
        self.assertEqual(self.s.gallery.stats()['images'],1)
        with self.assertRaises(ValueError):self.s.gallery.get(first,'a')
        self.assertEqual(self.s.gallery.get(replacement['image_id'],'a')[0]['origin'],'qq_group')

    async def test_invalid_review_no_save_and_same_image_updates_preference(self):
        ref=self.add();self.s.generate.return_value={'allowed':False,'description':'不合适'}
        with self.assertRaises(ValueError):await self.save(ref)
        self.assertEqual(self.s.gallery.stats()['images'],0)
        self.s.generate.return_value={'allowed':True,'description':'红色游戏图'}
        r=await self.save(self.add('red'))
        again=await self.s.gallery.save(self.add('red'),'a','现在更喜欢这张',['游戏'],90)
        self.assertEqual(again['status'],'already_saved');self.assertEqual(r['image_id'],again['image_id'])
        self.assertEqual(self.s.gallery.stats()['images'],1)

    async def test_embedding_failure_keeps_image_and_falls_back_to_text(self):
        self.s.memory=AsyncMock(side_effect=RuntimeError('offline'))
        r=await self.save();self.assertFalse(r['semantic_index'])
        found=await self.s.gallery.search('游戏','a')
        self.assertEqual(found['retrieval'],'text');self.assertEqual(len(found['images']),1)

    async def test_autonomous_curation_records_interest_and_can_decline(self):
        a=self.add();b=self.add('red')
        async def generate(messages,**kw):
            if kw.get('task')=='vlm':return {'allowed':True,'description':'游戏配图'}
            return {'decisions':[{'image_id':a,'keep':True,'score':82,'reason':'喜欢游戏里的蓝色设计','tags':['游戏'],'replace_id':''},
                                  {'image_id':b,'keep':False,'score':40,'reason':'和现有收藏重复','tags':[],'replace_id':''}]}
        self.s.generate=AsyncMock(side_effect=generate)
        result=await self.s.gallery.curate();self.assertEqual(result['status'],'considered')
        self.assertEqual(self.s.gallery.stats()['images'],1)
        events=self.s.state.db.execute("SELECT payload FROM gallery_events WHERE kind='interest_decision'").fetchall()
        self.assertEqual(len(events),2);self.assertIn('现有收藏重复',events[1][0])
        calls=self.s.generate.call_count;self.assertEqual((await self.s.gallery.curate())['status'],'waiting');self.assertEqual(calls,self.s.generate.call_count)

    async def test_queue_is_silent_unless_real_user_requested_report(self):
        self.s.commands.enqueue('silent','a','owner','m1',{},True)
        self.s.commands.finish('silent',{'status':'success','actions':[{'kind':'like','status':'success'}]})
        await self.s.commands.notify();self.s.ctx.send.text.assert_not_called()
        self.s.commands.enqueue('explicit','a','owner','m2',{},True)
        self.s.state.put('command_report:explicit',True)
        self.s.commands.finish('explicit',{'status':'success','actions':[{'kind':'like','status':'success'}]})
        await self.s.commands.notify();self.s.ctx.send.text.assert_awaited_once()
        await self.s.commands.notify();self.assertEqual(self.s.ctx.send.text.call_count,1)

    async def test_command_capture_requires_explicit_feedback_words(self):
        for i,text in enumerate(['【命令】点赞 BV1eCe36GELv','【命令】点赞 BV1eCe36GELv，完成后告诉我']):
            m={'platform':'qq','session_id':'a','account_id':'bot','message_id':'c'+str(i),'scope':'group','timestamp':time.time(),
               'message_info':{'group_info':{'group_id':'ga'},'user_info':{'user_id':'owner'}},'raw_message':[{'type':'text','data':text}]}
            key=self.s.commands.capture(m)
            self.assertEqual(self.s.state.get('command_report:'+key),bool(i))

    async def test_silent_operation_suppresses_followup_but_explicit_status_allows_it(self):
        msg={'platform':'qq','session_id':'a','account_id':'bot','message_id':'m1','scope':'group','timestamp':time.time(),
             'message_info':{'group_info':{'group_id':'ga'},'user_info':{'user_id':'owner'}},'raw_message':[{'type':'text','data':'帮我点赞视频'}]}
        self.s.record_qq_reply_eligibility(msg);self.s.state.put('silent_operation:a:m1',True)
        self.assertFalse(self.s.guard_qq_reply('a','m1',{}))
        msg['message_id']='m2';msg['raw_message'][0]['data']='刚才点赞成功了吗'
        self.s.record_qq_reply_eligibility(msg);self.assertTrue(self.s.receipt_requested('a','m2'))
        self.assertTrue(self.s.guard_qq_reply('a','m2',{}))

    async def test_same_turn_context_reuses_scoped_memory_and_keeps_policy_in_budget(self):
        result={'persona':self.s.state.persona(),'facts':[{'text':'记忆'*300,'source_id':str(i)} for i in range(5)],
                'activities':[{'text':'操作已成功'*500,'status':'success'}]*6,'source_ids':['id'],'memory_available':True}
        self.s.build_context=AsyncMock(return_value=result)
        self.s.native_learning_context=AsyncMock(return_value={'expressions':[],'jargon':[]})
        self.s.native_profile_context=AsyncMock(return_value=[])
        before=self.s.state.persona();first=await self.s.qq_context('a');second=await self.s.qq_context('a')
        self.assertEqual(first,second);self.assertEqual(self.s.build_context.call_count,1)
        self.assertLess(len(first),4500);self.assertIn('默认静默',first);self.assertNotIn('操作已成功',first)
        self.assertEqual(before,self.s.state.persona());self.assertIn('记忆',first)
        self.s.last_message['a']=('新问题','new-event','owner');await self.s.qq_context('a');self.assertEqual(self.s.build_context.call_count,2)
        self.s.state._bump_revision();await self.s.qq_context('a');self.assertEqual(self.s.build_context.call_count,3)

    async def test_expression_selector_never_retrieves_large_memory(self):
        plugin=load_plugin().UnifiedCorePlugin();plugin.service=self.s
        self.s.qq_context=AsyncMock(side_effect=AssertionError('must not retrieve'))
        result=await plugin._inject(session_id='a',items=[],request_kind='expression_selector')
        text=result['modified_kwargs']['items'][0]['parts'][0]['text']
        self.assertLess(len(text),500);self.assertIn('游戏',text);self.s.qq_context.assert_not_called()

class PureTests(unittest.TestCase):
    def test_receipt_optin_is_not_inferred_from_action_or_negation(self):
        for text in ['帮我点赞这个视频','收藏图片','今天状态不错','不用汇报结果','成功后不用告诉我','点赞之后不用给我反馈']:
            self.assertFalse(receipts.requested(text),text)
        for text in ['点赞成功了吗','执行结果呢','告诉我执行结果','完成后说一声','回执呢','告诉我今天点赞了哪些视频','查看互动记录','点赞后跟我说一声']:
            self.assertTrue(receipts.requested(text),text)

    def test_missing_choice_or_reason_is_distinct_from_explicit_decline(self):
        d={'interested':False,'interest_score':20,'engagement':{'like':False,'favorite':False,'coin':0,'follow_author':False},'engagement_reasons':{k:'没有持续兴趣' for k in engagement_decision.KEYS}}
        engagement_decision.checked(d)
        bad=copy.deepcopy(d);del bad['engagement_reasons']['coin']
        with self.assertRaisesRegex(ValueError,'reasons_required'):engagement_decision.checked(bad)
        bad=copy.deepcopy(d);bad.pop('engagement')
        with self.assertRaisesRegex(ValueError,'choices_required'):engagement_decision.checked(bad)

    def test_structural_trimming_keeps_valid_json_and_mandatory_rules(self):
        p={'dynamic':{'interests':['游戏'],'reply_style':'细腻'*200},'facts':[{'text':'a'*300} for _ in range(5)]}
        out=compact_context.render('MANDATORY',p,500)
        self.assertLess(len(out),550);self.assertTrue(out.startswith('MANDATORY'));json.loads(out[out.index('{'):])

if __name__=='__main__':unittest.main()
