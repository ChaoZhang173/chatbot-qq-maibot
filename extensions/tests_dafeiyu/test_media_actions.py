import base64
import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from PIL import Image
from maibot_unified_core.media import MediaStore,normalize
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.command_schema import validate,parse,PREFIX
from maibot_unified_core.store import State
from phase2.api import Bili
from phase2.core import UnknownDelivery


def picture():
    b=io.BytesIO();Image.new('RGB',(40,30),'blue').save(b,'PNG');return b.getvalue()

class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.p={'qq_bot_account':'bot','allowed_qq_sessions':['a','b'],'qq_group_sessions':{'ga':'a','gb':'b'},
            'qq_owner_bindings':{'a':'owner','b':'owner'},'qq_bili_commands_enabled':True}
        (root/'policy.json').write_text(json.dumps(self.p))
        self.s=UnifiedService(SimpleNamespace(send=SimpleNamespace(image=AsyncMock(return_value={'sent':True,'message_id':'image-receipt'}))),root)
        self.s.policy=self.p;self.s.state=State(root/'state.sqlite',self.p);self.s.state.init_persona({'identity':'fixed'},{'interests':['AI']})
        self.s.media=MediaStore(self.s);self.s.commands=CommandQueue(self.s);self.s.last_message['a']=('hello','event-1','owner')
        self.s.generate=AsyncMock(return_value={'allowed':True,'description':'蓝色几何色块'})
    def tearDown(self):self.s.state.close();self.tmp.cleanup()
    def message(self,scope='group'):
        return {'platform':'qq','account_id':'bot','session_id':'a','scope':scope,'timestamp':time.time(),'message_id':'m1',
            'message_info':{'group_info':{'group_id':'ga'},'user_info':{'user_id':'owner'}},
            'raw_message':[{'type':'at','data':{'target_user_id':'bot'}},{'type':'text','data':PREFIX+'发图文动态：今天的蓝色灵感'},
            {'type':'image','binary_data_base64':base64.b64encode(picture()).decode()}]}
    async def test_private_images_never_cached_cross_group_never_read(self):
        self.assertEqual(self.s.media.capture(self.message('private')),[])
        ref=self.s.media.capture(self.message())[0]
        with self.assertRaises(ValueError):self.s.media.get(ref,'b')
        self.assertEqual(self.s.media.get(ref,'a')[0]['width'],40)
    async def test_owner_image_job_requires_public_review_before_asset(self):
        ref=self.s.media.capture(self.message())[0];key=self.s.commands.capture(self.message())
        self.assertEqual(self.s.commands.claim()['job']['spec']['images'],[ref])
        with self.assertRaises(ValueError):self.s.commands.asset(key,ref)
        await self.s.media.review(ref,'a',public=True)
        asset=self.s.commands.asset(key,ref);self.assertEqual(asset['width'],40)
        with self.assertRaises(ValueError):self.s.commands.asset(key,'0'*64)
    async def test_new_prefix_text_only_and_prior_image_commands(self):
        ref=self.s.media.capture(self.message())[0]
        message=self.message();message['raw_message']=[{'type':'text','data':'【命令】发图文动态：蓝色灵感'}]
        key=self.s.commands.capture(message)
        self.assertEqual(self.s.commands.get(key)['spec']['images'],[ref])
        message['message_id']='m2';message['raw_message']=[{'type':'text','data':'【命令】发一条动态'}]
        key=self.s.commands.capture(message)
        self.assertEqual(self.s.commands.get(key)['status'],'pending')
        self.assertNotIn('images',self.s.commands.get(key)['spec'])
    async def test_topic_or_privacy_review_failure_prevents_qq_and_bili(self):
        ref=self.s.media.capture(self.message())[0];self.s.generate=AsyncMock(return_value={'allowed':False,'description':'禁止公开'})
        with self.assertRaises(ValueError):await self.s.media.send_qq(ref,'a')
        self.s.ctx.send.image.assert_not_called()
        key=self.s.commands.capture(self.message());self.s.commands.claim()
        self.assertFalse((await self.s.commands.review(key,'配图动态'))['allowed'])
    async def test_qq_image_receipt_dedupe_and_no_cross_group(self):
        ref=self.s.media.capture(self.message())[0]
        self.assertEqual((await self.s.media.send_qq(ref,'a'))['status'],'success')
        self.s.state.put('qq_image_last:a',0)
        self.assertEqual((await self.s.media.send_qq(ref,'a'))['status'],'success')
        self.assertEqual(self.s.ctx.send.image.call_count,1)
        with self.assertRaises(ValueError):await self.s.media.send_qq(ref,'b')
    async def test_expired_images_removed(self):
        ref=self.s.media.capture(self.message())[0]
        self.s.state.db.execute('UPDATE scoped_media SET created=?',(time.time()-3601,));self.s.media.cleanup()
        self.assertFalse((self.s.media.root/(ref+'.jpg')).exists())
        with self.assertRaises(ValueError):self.s.media.get(ref,'a')
    async def test_public_import_validates_and_preserves_source(self):
        with patch('maibot_unified_core.media.fetch_bytes',return_value=('https://example.com/p.png',picture())):
            result=await self.s.media.import_public('https://example.com/p.png','a')
        self.assertEqual(result['source_url'],'https://example.com/p.png')
        self.assertEqual(self.s.media.get(result['image_id'],'a')[0]['origin'],'public_web')

class BiliImageTests(unittest.TestCase):
    def test_upload_precedes_one_image_dynamic_and_receipt(self):
        b=Bili.__new__(Bili);b.credentials={'BILI_JCT':'fake'};calls=[]
        def request(path,**kw):
            calls.append((path,kw))
            if 'upload_bfs' in path:return {'image_url':'http://i0.hdslb.com/bfs/new_dyn/image.jpg'}
            return {'dyn_id_str':'12345'}
        b.request=request
        result=b.send('dynamic',None,'蓝色灵感',media=[{'raw':picture(),'width':40,'height':30}])
        self.assertEqual(result['id'],'12345');self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][1]['files']['file_up'][2],'image/jpeg')
        data=calls[1][1]['body']['dyn_req'];self.assertEqual(data['scene'],2);self.assertEqual(data['pics'][0]['img_width'],40)
    def test_unverified_upload_receipt_never_publishes(self):
        b=Bili.__new__(Bili);b.credentials={'BILI_JCT':'fake'};b.request=lambda *a,**k:{'image_url':'https://evil.example/x'}
        with self.assertRaises(UnknownDelivery):b.send('dynamic',None,'配图',media=[{'raw':picture(),'width':40,'height':30}])
    def test_image_spec_only_dynamic_and_normalization(self):
        plan=parse('发动态：蓝色灵感');plan['images']=['a'*64];validate(plan)
        plan['kinds']=['comment'];plan['target_type']='video';plan['target']='BV1ehHQ6bErB'
        with self.assertRaises(ValueError):validate(plan)
        data,w,h=normalize(picture());self.assertTrue(data.startswith(b'\xff\xd8'));self.assertEqual((w,h),(40,30))
