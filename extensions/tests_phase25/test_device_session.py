import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from phase2.api import Bili
from phase2.core import PlatformError

class Response:
    status_code=200;headers={}
    def __init__(self,value,status=200):self.value=value;self.status_code=status
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def raise_for_status(self):pass
    def iter_content(self,size):yield json.dumps(self.value).encode()

class DeviceTests(unittest.TestCase):
    def test_live_code_zero_with_rejection_message_is_not_success(self):
        api=Bili.__new__(Bili);api.http=Mock();api.http.request.return_value=Response({'code':0,'message':'0','msg':'您已被禁言','data':{}})
        with self.assertRaises(PlatformError):api.request('/msg/send',data={'msg':'hello'},live=True)
        self.assertEqual(api.last_platform_error['message'],'您已被禁言');self.assertEqual(api.http.request.call_count,1)
        self.assertEqual(api.http.request.call_args.args[1],'https://api.live.bilibili.com/msg/send')
        self.assertEqual(api.last_platform_error['code'],0)
        self.assertEqual(api.last_platform_error['classification'],'unknown_live_rejection')
    def test_known_live_markers_preserve_platform_code_and_fail_without_retry(self):
        for marker,classification in [('k','room_keyword_block'),('f','site_keyword_block'),('fire','site_keyword_block')]:
            with self.subTest(marker=marker):
                api=Bili.__new__(Bili);api.http=Mock()
                api.http.request.return_value=Response({'code':0,'message':'0','msg':marker,'data':{}})
                with self.assertRaises(PlatformError) as error:
                    api.request('/msg/send',data={'msg':'original message'},live=True)
                self.assertEqual(error.exception.code,'live_message_rejected')
                self.assertEqual(api.last_platform_error,{'code':0,'message':marker,'marker':marker,'classification':classification})
                self.assertEqual(api.http.request.call_count,1)
                self.assertEqual(api.http.request.call_args.kwargs['data'],{'msg':'original message'})
    def test_live_marker_is_preferred_to_generic_message(self):
        api=Bili.__new__(Bili);api.http=Mock()
        api.http.request.return_value=Response({'code':0,'message':'blocked','msg':'k'})
        with self.assertRaises(PlatformError):api.request('/msg/send',data={},live=True)
        self.assertEqual(api.last_platform_error['marker'],'k')
        self.assertEqual(api.last_platform_error['classification'],'room_keyword_block')
    def test_unknown_live_marker_is_sanitized_and_not_misclassified(self):
        api=Bili.__new__(Bili);api.http=Mock()
        marker='unknown\nhttps://example.test/?secret=private '+('a'*64)
        api.http.request.return_value=Response({'code':0,'msg':marker})
        with self.assertRaises(PlatformError):api.request('/msg/send',data={},live=True)
        self.assertEqual(api.last_platform_error['classification'],'unknown_live_rejection')
        self.assertEqual(api.last_platform_error['message'],'unknown [redacted] [redacted]')
        self.assertEqual(api.http.request.call_count,1)
    def test_non_send_live_message_and_get_are_not_interpreted_as_rejection(self):
        for path,data,live in [('/other/write',{},True),('/msg/send',None,True),('/msg/send',{},False)]:
            with self.subTest(path=path,data=data,live=live):
                api=Bili.__new__(Bili);api.http=Mock()
                api.http.request.return_value=Response({'code':0,'msg':'k','data':{'accepted':True}})
                self.assertEqual(api.request(path,data=data,live=live),{'accepted':True})
                self.assertIsNone(api.last_platform_error)
    def test_http_denials_replace_stale_error_without_reading_untrusted_body(self):
        for status in (401,403,412,429):
            with self.subTest(status=status):
                api=Bili.__new__(Bili);api.http=Mock()
                api.last_platform_error={'code':-401,'message':'old error'}
                response=Response({'token':'must-not-be-recorded'},status=status)
                response.iter_content=Mock(side_effect=AssertionError('body must not be read'))
                api.http.request.return_value=response
                with self.assertRaises(PlatformError) as error:api.request('/msg/send',data={},live=True)
                self.assertEqual(error.exception.code,status)
                self.assertEqual(api.last_platform_error,{'code':status,'message':f'HTTP {status}','http_status':status})
                self.assertEqual(api.http.request.call_count,1)
                response.iter_content.assert_not_called()
    def test_success_clears_previous_platform_error(self):
        api=Bili.__new__(Bili);api.http=Mock();api.last_platform_error={'code':-401,'message':'old'}
        api.http.request.return_value=Response({'code':0,'data':{'ok':True}})
        self.assertEqual(api.request('/x/web-interface/nav'),{'ok':True})
        self.assertIsNone(api.last_platform_error)
    def test_coin_error_message_is_preserved_without_retry(self):
        api=Bili.__new__(Bili);api.http=Mock();api.http.request.return_value=Response({'code':-401,'message':'非法访问'})
        with self.assertRaises(PlatformError):api.request('/x/web-interface/coin/add',data={'multiply':1})
        self.assertEqual(api.last_platform_error,{'code':-401,'message':'非法访问'});self.assertEqual(api.http.request.call_count,1)
    def test_platform_device_is_persisted_and_survives_identity_reload(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'credential.json';p.write_text(json.dumps({'SESSDATA':'test','BILI_JCT':'test','DEDE_USER_ID':'100'}))
            api=Bili(p);api.request=Mock(return_value={'b_3':'platform-issued-device3','b_4':'platform/issued+device4=='})
            api.ensure_device();api._load();api.ensure_device()
            self.assertEqual(api.request.call_count,1)
            self.assertEqual({c.name for c in api.http.cookies},{'SESSDATA','bili_jct','DedeUserID','buvid3','buvid4'})
            self.assertEqual((p.parent/'bili-device-cookies.json').stat().st_mode&0o777,0o600)
            self.assertEqual(len(json.loads(p.read_text())),3)
    def test_invalid_device_response_cannot_become_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'credential.json';p.write_text(json.dumps({'SESSDATA':'test','BILI_JCT':'test','DEDE_USER_ID':'100'}))
            api=Bili(p);api.request=Mock(return_value={'b_3':'bad; cookie','b_4':'other-device'})
            with self.assertRaises(Exception):api.ensure_device()
            self.assertFalse((p.parent/'bili-device-cookies.json').exists())
