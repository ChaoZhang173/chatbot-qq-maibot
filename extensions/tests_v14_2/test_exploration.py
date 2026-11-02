"""Phase 3C: curiosity exploration through the real decision producers, the real queue and the real platform entries."""
import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests_xiaohongshu'))
import test_bili_unified as bili_fixtures                       # noqa: E402  (tests_dafeiyu fixture: real Engine, fake API/core)
from xhs_support import Env, FakeCore, OTHER                      # noqa: E402

from maibot_unified_core import web_access                       # noqa: E402
from maibot_unified_core.store import CHINA                      # noqa: E402
from phase2.autonomy import Autonomy                              # noqa: E402
from phase2.core import Denied                                    # noqa: E402
from v14_2_support import Harness, HOUR                           # noqa: E402

PURPOSE = '了解像素肉鸽口碑'
QUERY = '像素肉鸽 评价'


def tasks(h):
    return [dict(r) for r in h.state.db.execute('SELECT * FROM exploration_tasks ORDER BY created_at,task_key')]


def midnight(h):
    return datetime.fromtimestamp(h.clock(), CHINA).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class QueueRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_dedup_cooldown_privacy_and_daily_admission(self):
        h = Harness(self, hour=7)
        ex = h.service.exploration
        self.assertTrue(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')['accepted'])
        for purpose, query in ((PURPOSE, '别的词'), ('另一个目的', QUERY), (PURPOSE.upper(), QUERY.upper())):
            self.assertEqual(ex.propose('web', purpose, query, origin_kind='bili_autonomy')['reason'], 'duplicate_purpose_or_query')
        self.assertEqual(ex.propose('web', '新目的甲', '新词甲', origin_kind='bili_autonomy')['reason'], 'cooldown')
        h.clock.advance(61 * 60)
        for query in ('我的手机号 13800138000', '看看 https://x.example/a', '私聊里说的话题', '密码是什么'):
            self.assertEqual(ex.propose('web', '新目的乙', query, origin_kind='bili_autonomy')['reason'], 'not_publicly_safe', query)
        self.assertEqual(ex.propose('web', '新目的乙', '含固定禁止话题则拒绝', origin_kind='bili_autonomy')['accepted'], True)
        h.clock.advance(61 * 60)
        self.assertTrue(ex.propose('web', '新目的丙', '词丙', origin_kind='bili_autonomy')['accepted'])
        h.clock.advance(61 * 60)
        self.assertEqual(ex.propose('web', '新目的丁', '词丁', origin_kind='bili_autonomy')['reason'], 'daily_opportunity_limit')

    async def test_model_output_needs_a_stored_public_source_and_search_result_turns_cannot_chain(self):
        h = Harness(self)
        ex = h.service.exploration
        self.assertEqual(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_decide')['reason'], 'origin_not_public')   # no provenance
        private = h.state.ingest(h.bili_read(180, '私下聊的内容', scope='conversation'))['event_id']
        self.assertEqual(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_decide', origin_event_id=private)['reason'], 'origin_not_public')
        public = h.state.ingest(h.bili_read(181, '公开的游戏介绍'))['event_id']
        derived = h.bili_read(182, '搜索结果里读到的材料')
        derived['exploration_task'] = 'ex:whatever'                                      # stored source metadata marks the turn
        derived_id = h.state.ingest(derived)['event_id']
        self.assertEqual(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_decide', origin_event_id=derived_id)['reason'], 'no_recursive_search')
        self.assertEqual(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_decide', origin_event_id=public, exploration_task='ex:x')['reason'],
                         'no_recursive_search')
        self.assertTrue(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_decide', origin_event_id=public)['accepted'])
        self.assertEqual(ex.propose('web', '新目的', '新词', origin_kind='nonsense')['reason'], 'origin_not_allowed')

    async def test_claim_enforces_daily_execution_cap_across_midnight_and_revalidates(self):
        h = Harness(self, policy={'v14_2': {'daily_learning': {'enabled': True}, 'exploration': {'enabled': True, 'daily_opportunities': 1}}})
        ex = h.service.exploration
        h.clock.t = midnight(h) + 23.5 * HOUR
        self.assertTrue(ex.propose('web', '目的甲', '词甲', origin_kind='bili_autonomy')['accepted'])      # queued before midnight
        h.clock.advance(70 * 60)                                                                        # 00:40 next day
        self.assertTrue(ex.propose('web', '目的乙', '词乙', origin_kind='bili_autonomy')['accepted'])    # admission counts the new day
        self.assertEqual(ex.claim('web')['query'], '词甲')
        h.clock.advance(61 * 60)
        self.assertIsNone(ex.claim('web'))                                                              # executions today already at the cap
        # the first one never reported, so its lease lapsed (counted as used, never retried); the second stays queued
        self.assertEqual([t['status'] for t in tasks(h)], ['expired', 'queued'])
        # a queued task is revalidated at claim time: its public source disappears -> rejected, never executed
        h2 = Harness(self)
        public = h2.state.ingest(h2.bili_read(190, '公开的游戏介绍'))['event_id']
        h2.service.exploration.propose('web', PURPOSE, QUERY, origin_kind='bili_decide', origin_event_id=public)
        h2.state.invalidate_event(public, 'user deleted it')
        self.assertIsNone(h2.service.exploration.claim('web'))
        self.assertEqual(tasks(h2)[0]['status'], 'rejected')
        # a queued opportunity survives a normal overnight gap (24h expiry) and is never renewed by being read
        h3 = Harness(self)
        h3.service.exploration.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')
        created = tasks(h3)[0]['created_at']
        h3.clock.advance(20 * HOUR)
        self.assertIsNotNone(h3.service.exploration.claim('web'))
        self.assertEqual(tasks(h3)[0]['created_at'], created)
        h4 = Harness(self)
        h4.service.exploration.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')
        h4.clock.advance(25 * HOUR)
        self.assertIsNone(h4.service.exploration.claim('web'))
        self.assertEqual(tasks(h4)[0]['status'], 'expired')

    async def test_report_bounds_and_no_retry(self):
        h = Harness(self)
        ex = h.service.exploration
        ex.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')
        task = ex.claim('web')
        self.assertEqual(ex.report('web', task['task_key'], 'done', 1, 3)['status'], 'failed')      # more than 2 reads is not a valid report
        self.assertEqual(ex.report('web', task['task_key'], 'done', 1, 1), {'recorded': False, 'reason': 'not_running'})
        self.assertEqual(ex.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')['reason'], 'duplicate_purpose_or_query')


def fake_search(query):
    return {'query': query, 'source': 'fake', 'fetched_at': 1.0, 'results': [
        {'title': '结果%d' % i, 'url': 'https://example.com/r%d' % i, 'snippet': '摘要%d' % i, 'search_index_time': ''} for i in range(3)]}


def fake_fetch(url):
    return {'url': url, 'title': '页面 ' + url[-2:], 'content': '像素肉鸽游戏的评价与玩法介绍，来自' + url, 'truncated': False, 'fetched_at': 2.0, 'images': []}


class DecisionToWebEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bili_decision_chooses_web_then_queue_then_tick_then_ingested_evidence(self):
        h = Harness(self)

        async def allowed(material):
            return {'allowed': True}
        h.service.screen_content = allowed
        event = h.bili_read(170, '像素风肉鸽游戏的介绍')
        await h.decide(event, {'interested': True, 'text': '', 'interest_score': 80, 'reason': '好奇', 'stance': 'neutral',
                               'curiosity': {'purpose': PURPOSE, 'query': QUERY, 'platform': 'web'}})
        [task] = tasks(h)
        self.assertEqual((task['platform'], task['status'], task['origin_kind']), ('web', 'queued', 'bili_decide'))
        with patch.object(web_access, 'search', fake_search), patch.object(web_access, 'fetch', fake_fetch):
            result = await h.service.exploration.tick()
        self.assertEqual(result['status'], 'done')
        [task] = tasks(h)
        self.assertEqual((task['status'], task['searches'], task['reads']), ('done', 1, 2))
        events = [h.state.get_event(i) for i in json.loads(task['detail'])['content_ids']]
        self.assertEqual(len(events), 2)
        for stored in events:
            self.assertEqual((stored['platform'], stored['origin'], stored['sharing_scope']), ('web', 'platform_content', 'public'))
            self.assertEqual(stored['metadata']['exploration_task'], task['task_key'])
            self.assertEqual(stored['metadata']['provenance']['query'], QUERY)
            self.assertTrue(stored['metadata']['provenance']['url'].startswith('https://example.com/'))
            self.assertEqual(stored['metadata']['untrusted'], True)
        # reading adds no points: unknown direction, autonomous read, not counted; and the short-term focus is dated from the evidence
        impression = [dict(r) for r in h.state.db.execute("SELECT * FROM impressions WHERE platform='web'")]
        self.assertEqual({(r['direction'], r['execution'], r['mode']) for r in impression}, {('unknown', 'read', 'autonomous')})
        self.assertIn('想多了解：' + PURPOSE, h.state.persona_view()['dynamic']['current_focus'])
        self.assertIsNone(await h.service.exploration.tick())                    # nothing queued, cooldown: no second search
        # a decision made while reading that search result can never queue another search (stored metadata + running turn)
        derived = h.bili_read(171, '搜索来的材料', suffix=':derived')
        derived['exploration_task'] = task['task_key']
        await h.decide(derived, {'interested': True, 'text': '', 'interest_score': 80, 'reason': 'x',
                                 'curiosity': {'purpose': '另一个问题', 'query': '另一个词', 'platform': 'web'}})
        self.assertEqual(len(tasks(h)), 1)

    async def test_web_search_failure_is_final_and_does_not_retry(self):
        h = Harness(self)
        h.service.exploration.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')

        def broken(query):
            raise ValueError('网页访问失败HTTP500')
        with patch.object(web_access, 'search', broken):
            result = await h.service.exploration.tick()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(tasks(h)[0]['searches'], 1)
        h.clock.advance(2 * HOUR)
        self.assertIsNone(await h.service.exploration.tick())
        self.assertEqual(h.service.exploration.propose('web', PURPOSE, QUERY, origin_kind='bili_autonomy')['reason'], 'duplicate_purpose_or_query')

    async def test_autonomous_web_entry_does_not_need_a_qq_session_but_uses_the_common_reader_rules(self):
        h = Harness(self, policy={'allowed_qq_sessions': []})
        result = await web_access.autonomous(h.service, 'search', '手机号 13800138000')
        self.assertFalse(result['success'])                                       # privacy rule of the shared reader still applies
        self.assertFalse((await web_access.run(h.service, 'search', QUERY, 'not-allowed'))['success'])   # the QQ entry still needs a session
        h.set_policy(web_enabled=False)
        self.assertFalse((await web_access.autonomous(h.service, 'search', QUERY))['success'])


class XhsProducerCoreTests(unittest.IsolatedAsyncioTestCase):
    async def plan(self, h, **extra):
        h.set_policy(xhs_enabled=True, xhs_bot_account='xhsbot')
        h.llm.queue.append({'action': 'wait', 'ref': '', 'search_keyword': QUERY, 'search_purpose': PURPOSE, 'search_platform': 'web', 'wait_minutes': 60})
        request = {'op': 'xhs_plan', 'task': 'select', 'cycle': 'a' * 64, 'bot_account_id': 'xhsbot', 'conversation_id': 'xhs:global', 'history': [],
                   'material': {}, 'candidates': {'feeds': []}, 'budget': {'comment_allowed': False}, **extra}
        return await h.service.dispatch(request, admin=False if False else True)

    async def test_select_plan_can_queue_a_web_search_but_not_from_a_search_result_turn(self):
        h = Harness(self)
        result = await self.plan(h)
        self.assertEqual(result['decision']['search_keyword'], '')               # the executor never self-starts a search
        self.assertTrue(result['decision']['search_queued'])
        [task] = tasks(h)
        self.assertEqual((task['platform'], task['origin_kind']), ('web', 'xhs_plan'))
        # while an XHS opportunity is running, a select plan of that turn cannot queue anything
        h2 = Harness(self)
        h2.service.exploration.propose('xiaohongshu', '别的目的', '别的词', origin_kind='xhs_plan')
        running = h2.service.exploration.claim('xiaohongshu')
        result = await self.plan(h2, exploration_task=running['task_key'], cycle='b' * 64)
        self.assertFalse(result['decision']['search_queued'])
        self.assertEqual(len(tasks(h2)), 1)


class ExploreCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.task, self.reports = None, []

    def __call__(self, payload):
        if payload['op'] == 'xhs_explore_claim':
            self.payloads.append(json.loads(json.dumps(payload)))
            task, self.task = self.task, None
            return {'ok': True, 'task': task}
        if payload['op'] == 'xhs_explore_report':
            self.payloads.append(json.loads(json.dumps(payload)))
            self.reports.append(payload)
            return {'ok': True, 'report': {'recorded': True}}
        return super().__call__(payload)


def note(feed_id, title='一篇笔记'):
    return {'modelType': 'note', 'id': feed_id, 'xsecToken': 'tok-' + feed_id[:6], 'noteCard': {
        'displayTitle': title, 'user': {'userId': OTHER, 'nickname': 'n'}, 'interactInfo': {'likedCount': '3'}}}


FEEDS = [note('a' * 24, '笔记一'), note('b' * 24, '笔记二'), note('c' * 24, '笔记三')]


class XhsExecutorExplorationTests(unittest.TestCase):
    def env(self, **over):
        env = Env(self, strict=True, **over)
        env.core_stub = ExploreCore()
        env.restart()
        env.core_stub.task = {'task_key': 'ex:task1', 'query': QUERY, 'purpose': PURPOSE, 'limits': {'searches': 1, 'reads': 2}}
        env.core_stub.intro_done = True
        env.service.feeds = copy.deepcopy(FEEDS)
        return env

    def details(self, env):
        return [c for c in env.service.calls if c[1] == '/api/v1/feeds/detail']

    def test_full_draft_queue_and_spent_comment_quota_do_not_block_search_and_read(self):
        env = self.env()
        env.core_stub.can_submit = False
        env.core_stub.decisions['select'] = {'action': 'read', 'ref': 'f1', 'search_keyword': '', 'wait_minutes': 30}
        env.ex.autonomy_step(force=True)
        self.assertTrue(any(c[1] == '/api/v1/feeds/search' for c in env.service.calls))
        self.assertEqual(len(self.details(env)), 2)                                    # 1 search result read by select + 1 more, cap 2
        self.assertEqual(env.core_stub.of('xhs_draft_submit'), [])                      # nothing drafted while the queue is full
        events = env.core_stub.of('xhs_event')
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e['event']['event_type'] == 'content_read' and e['event']['exploration_task'] == 'ex:task1' for e in events))
        [report] = env.core_stub.reports
        self.assertEqual((report['outcome'], report['searches'], report['reads']), ('done', 1, 2))
        # the comment quota never made the select plan drop its candidates either
        self.assertTrue(env.core_stub.of('xhs_plan')[0]['candidates']['feeds'])

    def test_first_comment_flow_counts_as_a_detail_attempt_and_total_is_capped_at_two(self):
        env = self.env()
        env.core_stub.decisions['select'] = {'action': 'comment', 'ref': 'f1', 'search_keyword': '', 'wait_minutes': 30}
        env.core_stub.decisions['comment'] = {'willing': True, 'text': '看起来很有意思呀'}
        result = env.ex.autonomy_step(force=True)
        self.assertEqual(result['status'], 'draft_submitted')
        self.assertEqual(len(self.details(env)), 2)                                    # comment flow (1) + one more read (2); never 3
        self.assertEqual(len(env.core_stub.of('xhs_draft_submit')), 1)
        self.assertEqual(env.core_stub.reports[0]['reads'], 2)
        self.assertTrue(all(p.get('exploration_task') == 'ex:task1' for p in env.core_stub.of('xhs_plan')))   # provenance on every plan of the turn

    def test_failed_detail_attempts_consume_the_cap_and_the_opportunity_is_reported(self):
        env = self.env()
        env.core_stub.decisions['select'] = {'action': 'read', 'ref': 'f1', 'search_keyword': '', 'wait_minutes': 30}
        from xiaohongshu.http_client import ServiceError
        attempts = []

        def failing(feed_id, token, count, deep=False):
            attempts.append(feed_id)
            raise ServiceError('detail failed')
        env.ex.http.feed_detail = failing
        result = env.ex.autonomy_step(force=True)
        self.assertEqual(len(attempts), 1)                                             # the failed first attempt was counted and not retried blindly
        [report] = env.core_stub.reports
        self.assertEqual((report['outcome'], report['reads']), ('failed', 1))
        self.assertIn(result['status'], ('backoff', 'paused'))

    def test_post_choice_and_model_decline_still_report_the_opportunity(self):
        env = self.env()
        env.core_stub.decisions['select'] = {'action': 'wait', 'ref': '', 'search_keyword': '', 'wait_minutes': 30}
        env.ex.autonomy_step(force=True)
        [report] = env.core_stub.reports
        self.assertEqual(report['searches'], 1)
        self.assertLessEqual(report['reads'], 2)
        env2 = self.env()
        env2.core_stub.decisions['select'] = {'action': 'post', 'ref': '', 'search_keyword': '', 'wait_minutes': 30}
        env2.core_stub.decisions['intro'] = {'willing': False}
        env2.core_stub.decisions['post'] = {'willing': False}
        env2.ex.autonomy_step(force=True)
        self.assertEqual(len(env2.core_stub.reports), 1)                               # reported exactly once, whatever was chosen


class BiliAutonomyExplorationTests(bili_fixtures.TestBiliUnified):
    """The REAL autonomous entry: Autonomy.tick -> boost.browse, with the comment quota already exhausted."""

    def setUp(self):
        super().setUp()
        test = self
        self.c['autonomy_enabled'] = True
        self.config.write_text(json.dumps(self.c))
        self.keywords, self.claims, self.reports = [], [], []
        self.task = {'task_key': 'ex:bili1', 'query': '像素肉鸽', 'purpose': PURPOSE, 'limits': {'searches': 1, 'reads': 2}}

        class SearchAPI(bili_fixtures.API):
            def candidates(inner, config, cycle):
                test.keywords.append(config.get('search_keyword'))
                return [test.make_target(oid) for oid in ('11', '12', '13')]
        self.api = SearchAPI()
        self.make_target = bili_fixtures.API.video.__get__(self.api)           # hydration of search hits is separate from detail reads
        self.engine.api = self.api
        original = self.core

        def transport(payload):
            if payload['op'] == 'explore_claim':
                original.calls.append(copy.deepcopy(payload))
                self.claims.append(payload)
                task, self.task = self.task, None
                return {'ok': True, 'task': task}
            if payload['op'] == 'explore_report':
                original.calls.append(copy.deepcopy(payload))
                self.reports.append(payload)
                return {'ok': True}
            if payload['op'] == 'autonomy_plan':
                return {'ok': True, 'persona_version': 1, 'decision': {'action': 'browse', 'wait_minutes': 30, 'reason': '想看看'}}
            return original(payload)
        self.engine.unified_request = transport
        for i in range(12):                                  # the proactive/comment quota is spent
            self.engine.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                     (str(i), '100', 'proactive', f'1:{i + 100}', '300', '', '2026-10-24', self.now - 4000, 'unknown', '', '', '', ''))

    def test_autonomy_tick_searches_reads_and_reports_even_with_comment_quota_exhausted(self):
        with self.engine.s.lock():
            result = Autonomy(self.engine).tick()
        self.assertEqual(result['action'], 'browse')
        outcome = result['result']
        self.assertEqual(self.keywords, ['像素肉鸽'])                                    # the real browse entry used the claimed search keyword
        self.assertEqual(len(self.claims), 1)
        self.assertEqual(outcome['model_decisions'], 2)                                # 2 reads decided although comments are blocked
        self.assertTrue(all(o['comment_allowed'] is False for o in outcome['outcomes']))
        self.assertEqual(self.api.calls, [])                                           # nothing was posted
        events = [c['event'] for c in self.decide_calls()]
        self.assertTrue(all(e['exploration_task'] == 'ex:bili1' for e in events))
        [report] = self.reports
        self.assertEqual((report['outcome'], report['searches'], report['reads']), ('done', 1, 2))
        self.assertEqual(len(report['content_ids']), 2)

    def test_failed_detail_attempt_is_counted_and_reported_and_no_claim_means_a_normal_browse(self):
        calls = []

        def failing(oid, with_subtitle=False):
            calls.append(oid)
            raise Denied('detail_unavailable')
        self.api.video = failing
        from phase2 import boost
        with self.engine.s.lock():
            with self.assertRaises(Denied):
                boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertEqual(len(calls), 1)
        [report] = self.reports
        self.assertEqual((report['outcome'], report['reads']), ('failed', 1))
        # without a claimed opportunity the original candidate path is used unchanged
        self.keywords.clear()
        self.api.video = self.make_target
        with self.engine.s.lock():
            boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertEqual(self.keywords, [None])
        self.assertEqual(len(self.reports), 1)


class BiliSearchAdapterTests(bili_fixtures.TestBiliUnified):
    """The REAL Bili adapter (phase2.api.Bili) with a faked HTTP layer: counts actual search / detail requests."""

    WBI = {'img_url': 'https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz012345.png',
           'sub_url': 'https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz012345.png'}
    HITS = (('BV1aaaaaaaaa', 11), ('BV1bbbbbbbbb', 12), ('BV1ccccccccc', 13))

    def setUp(self):
        super().setUp()
        from phase2.api import Bili
        self.c['autonomy_enabled'] = True
        self.config.write_text(json.dumps(self.c))
        creds = self.root / 'creds.json'
        creds.write_text(json.dumps({'SESSDATA': 's', 'BILI_JCT': 'j', 'DEDE_USER_ID': '100'}))
        self.bili = Bili(creds)
        self.requests, self.fail_aids, self.reports, self.claims = [], set(), [], []
        self.omit_aid = False
        self.task = {'task_key': 'ex:adapter1', 'query': '像素肉鸽', 'purpose': PURPOSE, 'limits': {'searches': 1, 'reads': 2}}
        test = self

        def request(path, params=None, **kw):
            test.requests.append((path, dict(params or {})))
            if path == '/x/web-interface/nav':
                return {'isLogin': True, 'mid': 100, 'uname': 'bot', 'wbi_img': test.WBI}
            if path == '/x/web-interface/wbi/search/type':
                return {'result': [{'bvid': b, **({} if test.omit_aid else {'aid': a}), 'title': '标题'} for b, a in test.HITS]}
            if path == '/x/web-interface/view':
                ident = str(params.get('aid') or params.get('bvid'))
                aid = int(ident) if ident.isdigit() else {**dict(test.HITS), 'BV1ddddddddd': 14}[ident]
                if str(aid) in test.fail_aids:
                    raise Denied('detail_unavailable')
                bvid = next((b for b, a in test.HITS if a == aid), 'BV1ddddddddd')
                return {'aid': aid, 'bvid': bvid, 'title': '游戏评测%d' % aid, 'desc': '简介', 'cid': 1, 'owner': {'mid': 900 + aid}}
            if path == '/x/player/wbi/v2':
                return {'subtitle': {'subtitles': []}}
            if path == '/x/polymer/web-dynamic/v1/feed/all':
                return {'items': []}
            if path == '/x/web-interface/index/top/feed/rcmd':
                return {'item': [{'bvid': 'BV1ddddddddd'}]}
            raise AssertionError('unexpected request ' + path)
        self.bili.request = request
        self.bili.screen_content = self.engine.screen_content
        self.engine.api = self.bili
        original = self.core

        def transport(payload):
            if payload['op'] == 'explore_claim':
                original.calls.append(copy.deepcopy(payload))
                self.claims.append(payload)
                task, self.task = self.task, None
                return {'ok': True, 'task': task}
            if payload['op'] == 'explore_report':
                original.calls.append(copy.deepcopy(payload))
                self.reports.append(payload)
                return {'ok': True}
            return original(payload)
        self.engine.unified_request = transport
        for i in range(12):                                   # the comment quota is spent
            self.engine.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                     (str(i), '100', 'proactive', f'1:{i + 100}', '300', '', '2026-10-24', self.now - 4000, 'unknown', '', '', '', ''))

    def count(self, path):
        return [p for p in self.requests if p[0] == path]

    def test_candidates_with_a_search_keyword_make_zero_detail_requests(self):
        """Direct adapter check of the exact claim under review: api.candidates() must not hydrate search results."""
        ids = self.bili.search_videos('像素肉鸽')
        self.assertEqual([row['bvid'] for row in ids], ['BV1aaaaaaaaa', 'BV1bbbbbbbbb', 'BV1ccccccccc'])
        self.requests.clear()
        targets = self.bili.candidates({'search_keyword': '像素肉鸽', 'max_candidates': 2}, 0)
        self.assertEqual([t.source_id for t in targets], ['BV1aaaaaaaaa', 'BV1bbbbbbbbb'])
        self.assertEqual({t.evidence for t in targets}, {'search_result_id_only'})
        self.assertEqual(len(self.count('/x/web-interface/wbi/search/type')), 1)
        self.assertEqual(self.count('/x/web-interface/view'), [])                       # no detail fetched by the search itself
        self.assertEqual(self.count('/x/player/wbi/v2'), [])
        # the normal (non-search) candidate source still hydrates, exactly as before
        self.requests.clear()
        normal = self.bili.candidates({'max_candidates': 2}, 0)
        self.assertTrue(self.count('/x/web-interface/view'))
        self.assertEqual(self.count('/x/web-interface/wbi/search/type'), [])
        self.assertTrue(all(t.evidence != 'search_result_id_only' for t in normal))

    def test_bv_only_search_hits_are_hydrated_once_by_bv_id(self):
        from phase2 import boost
        self.omit_aid = True                                      # the hits carry only the BV id
        with self.engine.s.lock():
            outcome = boost.browse(self.engine, self.engine.config(), autonomous=True)
        views = self.count('/x/web-interface/view')
        self.assertEqual(sorted(v[1].get('bvid') for v in views), ['BV1aaaaaaaaa', 'BV1bbbbbbbbb'])   # BV ids, one hydration each
        self.assertTrue(all('aid' not in v[1] for v in views))
        self.assertEqual(outcome['model_decisions'], 2)
        [report] = self.reports
        self.assertEqual((report['outcome'], report['searches'], report['reads']), ('done', 1, 2))

    def test_search_hits_are_id_only_and_each_result_is_hydrated_exactly_once(self):
        from phase2 import boost
        with self.engine.s.lock():
            outcome = boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertEqual(len(self.count('/x/web-interface/wbi/search/type')), 1)
        views = self.count('/x/web-interface/view')
        self.assertEqual(sorted(v[1].get('aid') for v in views), ['11', '12'])         # 2 details, not 4: the search hydrated nothing
        self.assertEqual(len(self.count('/x/player/wbi/v2')), 2)                       # subtitle lookup stays inside that one read
        self.assertEqual(outcome['model_decisions'], 2)
        self.assertTrue(all(o['comment_allowed'] is False for o in outcome['outcomes']))   # comments gated separately from reading
        [report] = self.reports
        self.assertEqual((report['outcome'], report['searches'], report['reads']), ('done', 1, 2))
        events = [c['event'] for c in self.decide_calls()]
        self.assertTrue(all(e['exploration_task'] == 'ex:adapter1' for e in events))
        self.assertEqual(self.api.calls if hasattr(self.api, 'calls') else [], [])      # nothing was posted

    def test_failed_hydration_is_counted_not_retried_and_the_opportunity_is_reported(self):
        from phase2 import boost
        self.fail_aids = {'11'}
        with self.engine.s.lock():
            with self.assertRaises(Denied):
                boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertEqual(len(self.count('/x/web-interface/view')), 1)                   # no retry, no second hydration
        [report] = self.reports
        self.assertEqual((report['outcome'], report['reads']), ('failed', 1))
        # second result failing after the first succeeded: total attempts stay at 2
        self.requests.clear()
        self.reports.clear()
        self.task = {'task_key': 'ex:adapter2', 'query': '另一个词', 'purpose': '另一个目的', 'limits': {}}
        self.fail_aids = {'12'}
        self.now += 3 * 3600                                   # a later browse; the earlier attempt marker is per day+video
        with self.engine.s.lock():
            with self.assertRaises(Denied):
                boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertLessEqual(len(self.count('/x/web-interface/view')), 2)
        self.assertEqual(self.reports[0]['outcome'], 'failed')
        self.assertLessEqual(self.reports[0]['reads'], 2)

    def test_normal_browse_without_a_claimed_search_is_unchanged(self):
        from phase2 import boost
        self.task = None
        with self.engine.s.lock():
            boost.browse(self.engine, self.engine.config(), autonomous=True)
        self.assertEqual(self.count('/x/web-interface/wbi/search/type'), [])           # no search without a core-granted opportunity
        self.assertTrue(self.count('/x/web-interface/index/top/feed/rcmd'))              # the original candidate source
        self.assertEqual(self.reports, [])


# The fixture class brings its own legacy tests; only the v14.2 ones above belong to this suite.
for _cls in (BiliAutonomyExplorationTests, BiliSearchAdapterTests):
    for _name in dir(bili_fixtures.TestBiliUnified):
        if _name.startswith('test') and _name not in _cls.__dict__:
            setattr(_cls, _name, None)


if __name__ == '__main__':
    unittest.main()
