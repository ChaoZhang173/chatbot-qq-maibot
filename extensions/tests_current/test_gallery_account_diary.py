import asyncio,base64,io,json,tempfile,time,unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
from maibot_unified_core.media import MediaStore
from maibot_unified_core.gallery import Gallery,day
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from maibot_unified_core.diary import draft
from phase2 import diary
from phase2.core import TZ
from test_live_chat import LiveTests

def picture(color):
    buf=io.BytesIO();Image.new('RGB',(32,32),color).save(buf,'PNG');return buf.getvalue()

class AccountGalleryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        p={'qq_bot_account':'bot','allowed_qq_sessions':['a','b','p'],'qq_group_sessions':{'ga':'a','gb':'b'},
           'daily_diary':{'enabled':True},'qq_native':{'enabled':True,'account':'bot','owner':'owner','destinations':[{'kind':'private','target':'owner','session':'p'}]}}
        state=State(root/'state.sqlite',p);state.init_persona({'identity':'大肥鱼'},{'interests':['动漫']})
        self.s=SimpleNamespace(policy=p,root=root,state=state,busy=asyncio.Lock(),last_message={'a':('','m1','owner'),'b':('','m2','owner'),'p':('','m3','owner')},
            ctx=SimpleNamespace(send=SimpleNamespace(image=AsyncMock(return_value={'sent':True,'message_id':'receipt'}))),
            generate=AsyncMock(return_value={'allowed':True,'description':'蓝色几何图案'}),memory=AsyncMock(return_value={'vector':[1.0]*32,'space':'test'}))
        self.s.qq_fields=lambda m:UnifiedService.qq_fields(self.s,m)
        self.s.media=MediaStore(self.s);self.g=Gallery(self.s)
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def message(self,session='a',colors=('blue',)):
        return {'platform':'qq','account_id':'bot','session_id':session,'scope':'private' if session=='p' else 'group','message_id':'incoming',
            'message_info':{'group_info':{} if session=='p' else {'group_id':'g'+session},'user_info':{'user_id':'owner'}},
            'raw_message':[{'type':'image','binary_data_base64':base64.b64encode(picture(c)).decode()} for c in colors]}
    async def test_saved_image_shared_with_both_groups_and_owner_private(self):
        ref=self.s.media.capture(self.message())[0]
        with self.assertRaises(ValueError):self.s.media.get(ref,'b')
        saved=await self.g.save(ref,'a','喜欢蓝色几何图案',['蓝色'],80)
        for session in ('a','b','p'):
            self.assertEqual((await self.g.search('蓝色',session))['images'][0]['image_id'],saved['image_id'])
            self.assertEqual(self.g.get(saved['image_id'],session)[0]['scope'],'account:qq:bot')
        with self.assertRaises(ValueError):self.g.get(saved['image_id'],'stranger')
    async def test_migrated_opaque_id_is_reused_without_duplicate_file(self):
        ref=self.s.media.capture(self.message())[0];saved=await self.g.save(ref,'a','喜欢蓝色图案')
        old=saved['image_id'];legacy='a'*64
        (self.g.root/(old+'.jpg')).rename(self.g.root/(legacy+'.jpg'))
        self.s.state.db.execute('UPDATE gallery_images SET id=? WHERE id=?',(legacy,old))
        again=await self.g.save(ref,'a','蓝色图案值得留着')
        self.assertEqual(again['status'],'already_saved');self.assertEqual(again['image_id'],legacy)
        self.assertEqual(self.g.stats()['images'],1)
    async def test_owner_private_capture_and_uncollected_isolation(self):
        ref=self.s.media.capture(self.message('p'))[0]
        self.assertEqual(self.s.media.get(ref,'p')[0]['origin'],'qq_private')
        with self.assertRaises(ValueError):self.s.media.get(ref,'a')
        with self.assertRaises(ValueError):await self.s.media.review(ref,'p',public=True)
        bad=self.message('p');bad['message_info']['user_info']['user_id']='stranger'
        self.assertEqual(self.s.media.capture(bad),[])
    async def test_no_time_gap_shared_daily_fifty_and_receipt_deduplication(self):
        refs=self.s.media.capture(self.message(colors=('blue','red')));self.assertEqual(len(refs),2)
        self.s.state.put('qq_image_usage:'+day(),48)
        first=await self.s.media.send_qq(refs[0],'a');self.assertEqual(first['status'],'success')
        self.assertEqual((await self.s.media.send_qq(refs[1],'a'))['status'],'success')
        self.assertEqual(self.s.ctx.send.image.await_count,2)
        self.assertEqual(await self.s.media.send_qq(refs[0],'a'),first)
        self.s.last_message['a']=('','new-request','owner')
        with self.assertRaisesRegex(ValueError,'50'):await self.s.media.send_qq(refs[0],'a')
        self.assertEqual(self.s.ctx.send.image.await_count,2)
    async def test_unknown_send_is_not_retried(self):
        ref=self.s.media.capture(self.message())[0];self.s.ctx.send.image.side_effect=TimeoutError()
        self.assertEqual((await self.s.media.send_qq(ref,'a'))['status'],'unknown')
        self.assertEqual((await self.s.media.send_qq(ref,'a'))['status'],'unknown')
        self.assertEqual(self.s.ctx.send.image.await_count,1)
    async def test_multiple_capture_and_busy_group_do_not_evict_other_session(self):
        refs=self.s.media.capture(self.message(colors=('blue','red','green','yellow','black')))
        self.assertEqual(len(refs),4)
        other=self.s.media.put(picture('purple'),'b',message_id='keep')
        for i in range(32):self.s.media.put(picture('white'),'a',message_id=str(i))
        self.s.media.cleanup();self.assertEqual(self.s.media.get(other,'b')[0]['session'],'b')
        self.assertEqual(self.s.state.db.execute("SELECT count(*) FROM scoped_media WHERE session='a'").fetchone()[0],30)
    async def test_night_scan_count_does_not_exhaust_daytime_curating(self):
        now=datetime(2026,10,31,12,tzinfo=TZ).timestamp()
        with patch('time.time',return_value=now):
            self.s.media.capture(self.message())
            self.s.state.put('gallery_curate:2026-10-31',4)
            async def generate(messages,**kw):
                if kw.get('task')=='learner':
                    items=json.loads(messages[1]['content'])['candidates']
                    return {'decisions':[{'image_id':i['image_id'],'keep':False,'score':30,'reason':'这张暂时不想收藏'} for i in items]}
                return {'allowed':True,'description':'蓝色图案'}
            self.s.generate=AsyncMock(side_effect=generate)
            self.assertEqual((await self.g.curate())['status'],'considered')
            self.assertEqual(self.s.state.get('gallery_curate_period:2026-10-31:day'),1)
    async def test_diary_can_choose_not_to_write(self):
        self.s.generate=AsyncMock(return_value={'willing':False,'text':'','fact_ids':[],'mention_uids':[]})
        result=await draft(self.s,{'date':day()})
        self.assertEqual(result['status'],'skipped');self.assertNotIn('draft_id',result)
        self.assertEqual(self.s.state.db.execute("SELECT count(*) FROM meta WHERE k LIKE 'diary_draft:%'").fetchone()[0],0)

class OptionalDiaryTests(LiveTests):
    def test_skipped_diary_never_publishes_or_retries_or_reserves_slots(self):
        self.now=datetime(2026,10,24,22,31,tzinfo=TZ).timestamp()
        self.c['daily_diary']={'enabled':True,'time':'22:30','timezone':'Asia/Shanghai'}
        self.c['enabled']['dynamic']=True;self.path.write_text(json.dumps(self.c))
        self.e.sender.deliver=Mock();calls=[]
        def core(payload):
            calls.append(payload['op'])
            if payload['op']=='diary_episodes':return {'episode_id':''}
            if payload['op']=='diary_draft':return {'status':'skipped','reason':'no_worthwhile_topic'}
            self.fail('Skipped diary must not enter review')
        self.e.bounded_core=core
        with patch('phase2.mentions.registry',return_value=[]),self.e.s.lock():
            self.assertEqual(diary.tick(self.e)['status'],'skipped')
            self.assertEqual(diary.tick(self.e)['status'],'skipped')
        self.assertEqual(calls,['diary_episodes','diary_draft']);self.e.sender.deliver.assert_not_called()
        for kind in ('dynamic','usage'):self.assertEqual(diary.reserved(self.e.s,self.c,self.now,kind),0)
