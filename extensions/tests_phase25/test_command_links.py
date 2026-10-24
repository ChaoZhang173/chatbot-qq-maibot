import copy,json,time,unittest
from unittest.mock import patch
from types import SimpleNamespace
from maibot_unified_core.command_schema import parse,targets
from test_commands_web_topics import QueueTests,DeliveryTests,BV
from test_live_chat import LiveTests
from phase2.links import resolve

class LinkGrammarTests(unittest.TestCase):
    def test_app_share_suffix_is_prose_not_url_path(self):
        from phase2.command_schema import parse as worker_parse
        share='【命令】进入直播间【大肥鱼自己搓插件玩mc，ds1017震惊瘫坐-哔哩哔哩直播】 https://b23.tv/g0KpmUZ观看直播'
        expected={'kinds':['browse'],'target_type':'short','target':'https://b23.tv/g0KpmUZ','text':'','prompt':''}
        for parser in (parse,worker_parse):
            self.assertEqual(parser(share),expected)
            self.assertEqual(parser('【命令】看看【https://live.bilibili.com/123】')['target'],'123')
            with self.assertRaises(ValueError):
                parser('【命令】看看 https://b23.tv.evil.test/g0KpmUZ观看直播')
        self.assertEqual(targets(share)[0]['target'],'https://b23.tv/g0KpmUZ')

    def test_share_queries_and_natural_commands(self):
        cases=[('【命令】请进入 https://live.bilibili.com/123?broadcast_type=0&is_room_feed=1 待20分钟','live','123',['browse']),
               ('【命令】帮我看看这个视频 https://www.bilibili.com/video/'+BV+'?spm_id_from=333.1007','video',BV,['browse']),
               ('【命令】给这个特别好看的视频点个赞 https://www.bilibili.com/video/'+BV,'video',BV,['browse','like']),
               ('【命令】进入直播间123','live','123',['browse']),
               ('【命令】看看 https://b23.tv/AbcDEF?share_source=copy_link','short','https://b23.tv/AbcDEF',['browse'])]
        for text,typ,target,kinds in cases:
            with self.subTest(text=text):
                p=parse(text);self.assertEqual((p['target_type'],p['target'],p['kinds']),(typ,target,kinds))
        self.assertEqual(parse(cases[0][0])['stay_seconds'],1200)
    def test_read_never_infers_interactions_and_negation_is_respected(self):
        for text in ('看看 '+BV,'看看 '+BV+'，不要点赞和收藏', '不要评论，只看 '+BV):
            self.assertEqual(parse(text)['kinds'],['browse'])
        for text in ('不要看 '+BV,'不要点赞 '+BV,'点赞 https://evil.test/'+BV,'进入直播间123待5分钟','给直播间123投币'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse(text)
    def test_quotes_and_multiple_targets(self):
        self.assertEqual(parse('去 '+BV+' 下留言“这个游戏的光影真漂亮”')['text'],'这个游戏的光影真漂亮')
        with self.assertRaises(ValueError):parse('看 https://live.bilibili.com/123 和 '+BV)
    def test_short_redirect_never_fetches_external_host_or_cookies(self):
        class Response:
            status_code=302;headers={'Location':'https://127.0.0.1/secret'}
            def __enter__(self):return self
            def __exit__(self,*a):pass
        with patch('phase2.links.requests.get',return_value=Response()) as get:
            with self.assertRaises(ValueError):resolve('https://b23.tv/Abc')
            self.assertEqual(get.call_count,1);self.assertNotIn('cookies',get.call_args.kwargs)
        Response.headers={'Location':'https://www.bilibili.com/video/'+BV+'?spm_id=1'}
        with patch('phase2.links.requests.get',return_value=Response()):self.assertEqual(resolve('https://b23.tv/Abc')['target'],BV)

class LinkQueueTests(QueueTests):
    async def test_recent_link_is_group_scoped_and_expires(self):
        self.q.capture_links(self.message(text='游戏视频 https://www.bilibili.com/video/'+BV,actor='friend'))
        key=self.q.capture(self.message(text='【命令】看看刚才那个视频',mid='new'))
        self.assertEqual(self.q.get(key)['spec']['target'],BV)
        other=self.q.capture(self.message(text='【命令】看看刚才那个视频',session='b',mid='bnew'))
        self.assertEqual(self.q.get(other)['status'],'failed')
        self.s.state.put('qq_bili_links:a',{'at':time.time()-601,'targets':targets(BV)})
        expired=self.q.capture(self.message(text='【命令】看看刚才那个视频',mid='expired'))
        self.assertEqual(self.q.get(expired)['status'],'failed')

class BrowseDeliveryTests(DeliveryTests):
    def test_browse_reads_material_and_never_likes_comments_or_spends(self):
        self.job('看看 '+BV)
        result=self.runjob();self.assertEqual(result['status'],'success')
        self.assertEqual(result['actions'][0]['material_evidence'],'title_description')
        self.assertEqual(self.api.writes,[]);self.assertEqual(self.api.sent,[])
        self.runjob();self.assertEqual(self.e.s.used('100','browse',self.e.now()),1)

class RequestedVisitTests(LiveTests):
    def test_requested_visit_uses_exact_room_and_preserves_old_message_baseline(self):
        self.message('旧消息')
        with self.e.s.lock():result=self.live.visit('123',1200,True)
        self.assertEqual(result['room_id'],'123');self.assertFalse(self.api.sent)
        ends=result['ends']
        with self.e.s.lock():self.assertEqual(self.live.visit('123',3600,True)['ends'],ends)
    def test_requested_visit_cannot_override_rejection_or_room_limits(self):
        self.e.s.put('live_backoff_until',self.now+60)
        with self.e.s.lock(),self.assertRaises(Exception):self.live.visit('123',600,True)
        self.assertFalse(self.api.sent);self.assertIsNone(self.e.s.get('live_visit'))
