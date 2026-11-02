"""Offline harness for the v14.2 suite: real UnifiedService / State, fake clock, fake model, fake memory. No network."""
import hashlib
import json
import time
import tempfile
import types
from pathlib import Path

from maibot_unified_core.daily_learning import DailyLearning
from maibot_unified_core.exploration import Exploration
from maibot_unified_core.private_access import PrivateAccess
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State

ROOT = Path(__file__).resolve().parents[2]
BOT = 'bot-account'
HOUR = 3600.0


def load_json(relative):
    return json.loads((ROOT / relative).read_text(encoding='utf-8'))


class Crash(BaseException):
    """Simulates the process dying (not an Exception, so no handler can swallow it)."""


class Clock:
    def __init__(self, start):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class FakeLLM:
    def __init__(self):
        self.queue, self.calls = [], []

    async def generate(self, prompt, task_name='', max_tokens=0, temperature=0):
        self.calls.append({'prompt': prompt, 'task': task_name})
        if not self.queue:
            raise RuntimeError('no scripted model response')
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(prompt)
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {'success': True, 'response': text, 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}


class FakeMemory:
    """Idempotent by external_id, like the Host ledger. `fail` raises for the next N writes."""
    def __init__(self):
        self.written, self.calls, self.hits, self.fail = {}, [], [], 0

    async def __call__(self, operation, **kw):
        self.calls.append((operation, kw))
        if operation == 'ingest_text':
            if self.fail > 0:
                self.fail -= 1
                raise RuntimeError('memory offline')
            self.written[kw['external_id']] = kw
            return {'success': True, 'stored_ids': ['m:' + kw['external_id']]}
        if operation == 'search':
            return {'success': True, 'hits': list(self.hits)}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] == 'ingest_text']


HOST = {'bot.nickname': '大肥鱼', 'bot.alias_names': ['肥鱼'], 'personality.reply_style': '自然简短',
        'personality.multiple_reply_style': ['轻松']}


class Harness:
    """A running service core: owner bound in group 'a' and in private chat 'p-owner'; an ordinary user 'user1'."""
    def __init__(self, test, policy=None, seed=True, hour=None):
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = time.time()
        if hour is not None:   # deterministic Beijing time of day for tests that depend on calendar days
            from datetime import datetime
            from zoneinfo import ZoneInfo
            local = datetime.fromtimestamp(self.base, ZoneInfo('Asia/Shanghai'))
            self.base = local.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()
        self.clock = Clock(self.base)
        self.llm, self.memory = FakeLLM(), FakeMemory()
        self.policy = {
            'qq_bot_account': BOT, 'bili_bot_uid': '1000', 'bili_uid': -1,
            'allowed_qq_sessions': ['a', 'b', 'p-owner', 'p-user'],
            'qq_group_sessions': {'group-a': 'a', 'group-b': 'b'}, 'shared_groups': [['a', 'b']],
            'qq_owner_bindings': {'a': 'owner'}, 'qq_private_familiar_enabled': True,
            'persona_learning_schema': 2, 'web_enabled': True, 'public_qq_sessions': [],
            'v14_2': {'daily_learning': {'enabled': True}, 'exploration': {'enabled': True}}}
        self.policy.update(policy or {})
        (self.root / 'policy.json').write_text(json.dumps(self.policy))

        class Person:
            async def get_id(self, platform, actor):
                return hashlib.md5((platform + '_' + actor).encode()).hexdigest()

        quiet = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
        ctx = types.SimpleNamespace(person=Person(), llm=self.llm, logger=quiet)
        self.service = UnifiedService(ctx, root=self.root)
        self.service.reload_policy()
        self.state = self.service.state = State(self.root / 'state.sqlite', self.service.policy, now=self.clock)
        test.addCleanup(self.state.close)
        self.service.private_access = PrivateAccess(self.service)
        for private_id, group_actor, session in (('owner-private', 'owner', 'p-owner'), ('user-private', 'user1', 'p-user')):
            self.state.db.execute('INSERT INTO qq_private_bindings(private_id,group_session,group_actor,verified,session) VALUES(?,?,?,?,?)',
                                  (private_id, 'a', group_actor, self.base, session))
        self.service.reload_policy()
        self.service.memory = self.memory
        self.service.daily = DailyLearning(self.service)
        self.service.exploration = Exploration(self.service)
        if seed:
            self.seed_persona()

    # ---- persona ------------------------------------------------------------------------------
    def seed_persona(self, revision='r-v14.2'):
        config = load_json('config/persona.current.example.json')
        unified = load_json('config/unified.current.example.json')
        host = {**HOST, 'personality.personality': config['stable']['personality.personality'],
                'personality.behavior_style': config['stable']['personality.behavior_style']}
        self.host = host
        self.state.sync_learnable_persona(host, unified['persona_defaults'], revision)
        return self.state.persona()

    def set_policy(self, **changes):
        self.policy.update(changes)
        (self.root / 'policy.json').write_text(json.dumps(self.policy))
        self.service.reload_policy()

    # ---- messages -----------------------------------------------------------------------------
    def message(self, mid, text, session='a', actor='user1', private=False, at=False):
        info = {'user_info': {'user_id': actor, 'user_nickname': 'nick-' + actor}, 'additional_config': {}}
        if not private:
            info['group_info'] = {'group_id': 'group-' + session}
        message = {'message_id': mid, 'platform': 'qq', 'account_id': BOT, 'session_id': session,
                   'scope': 'private' if private else 'group', 'timestamp': str(self.clock()), 'message_info': info,
                   'raw_message': [{'type': 'text', 'data': text}]}
        if at:
            message['is_at'] = True
        return message

    async def say(self, mid, text, **kw):
        result = await self.service.ingest_qq(self.message(mid, text, **kw))
        await self.service.drain_feedback()
        return result

    # ---- bulk legacy fixtures -----------------------------------------------------------------
    def activate(self):
        return self.service.daily.activate()

    def bulk_events(self, count, prefix, session, received_at, step=10.0, text='旧闲聊内容第{i}条，聊点别的话题', platform='qq'):
        """Fixture: many legacy raw events in ONE transaction (what ingest would have stored over days)."""
        batch = []
        for i in range(count):
            stamp = received_at + i * step
            event = {'event_id': 'evt:%s:%d' % (prefix, i), 'native_event_id': '%s:%d' % (prefix, i), 'platform': platform,
                     'bot_account_id': BOT, 'conversation_id': session, 'actor_id': 'u%d' % (i % 50), 'person_id': '',
                     'origin': 'external_user', 'event_type': 'message_received', 'content': text.format(i=i), 'source_id': '%s:%d' % (prefix, i),
                     'occurred_at': stamp, 'received_at': stamp, 'learning_allowed': True, 'sharing_scope': 'policy', 'active': True, 'metadata': {}}
            batch.append((event['event_id'], platform, BOT, session, event['actor_id'], '', 'external_user', 'message_received', stamp, stamp, 1, 1,
                          'fixture', json.dumps(event, ensure_ascii=False)))
        with self.state._transaction():
            self.state.db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
        return [row[0] for row in batch]

    def events_batch_answer(self, remember=False):
        """Scripted model answer for an events batch: an explicit outcome for every event it was given."""
        def answer(prompt):
            events = json.loads(prompt[1]['content'])['events']
            return {'items': [{'event_id': e['event_id'], 'remember': remember, 'summary': '一条关于游戏的抽象话题总结' if remember else '',
                               'category': '游戏', 'public_summary': False, 'knowledge_type': 'inference'} for e in events]}
        return answer

    # ---- public bilibili material -------------------------------------------------------------
    def bili_read(self, oid, text='像素风肉鸽游戏介绍', author='999', when=None, scope='public', suffix=''):
        key = '1:' + str(oid)
        event = {'event_id': 'bilibili:1000:content_read:%s%s' % (oid, suffix), 'platform': 'bilibili', 'bot_account_id': '1000',
                 'conversation_id': key, 'actor_id': author, 'origin': 'platform_content', 'event_type': 'content_read',
                 'content': text, 'source_id': str(oid), 'source_url': 'https://www.bilibili.com/video/BV1xx411c7mD',
                 'sharing_scope': scope, 'learning_allowed': True, 'occurred_at': when or self.clock(),
                 'material_evidence': 'title_description', 'target': {'kind': 'video', 'key': key, 'oid': str(oid), 'type': 1, 'author': author}}
        if scope != 'public':
            event['is_private'] = True
        return event

    async def decide(self, event, decision, task='proactive', permissions=None):
        self.llm.queue.append(decision)
        return await self.service.dispatch({'op': 'decide', 'event': event, 'task': task, 'material': {'text': event['content'],
                                            'evidence': event['material_evidence']}, 'permissions': permissions or {}}, admin=True)

    async def receipt(self, event, kind, status='succeeded', attempt=''):
        action = {'action_id': 'act:%s:%s%s' % (kind, event['event_id'], attempt), 'platform': 'bilibili', 'bot_account_id': '1000',
                  'kind': kind, 'status': status, 'canonical_status': status, 'remote_id': 'r1' if status == 'succeeded' else '',
                  'receipt_verified': status == 'succeeded', 'input_event': event, 'conversation_id': event['conversation_id']}
        return await self.service.dispatch({'op': 'activity', 'action': action}, admin=True)
