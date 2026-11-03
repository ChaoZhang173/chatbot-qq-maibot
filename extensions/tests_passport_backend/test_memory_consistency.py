import asyncio
import json
import time
from unittest.mock import patch

from .harness import BackendCase, BOT, GROUP_SESSION, OWNER_SESSION, OWNER_PRIVATE_ID, OTHER_SESSION
from maibot_unified_core import conversation_recall
from maibot_unified_core.voice import VoiceBridge, IDEMPOTENCY_PREFIX, _ident


class RecallTests(BackendCase):
    def share_groups(self):
        self.policy['qq_group_recall_shared_enabled'] = True
        self.policy['qq_group_sessions']['second'] = 'group-two'
        self.policy['allowed_qq_sessions'].append('group-two')
        self.write_policy()
        self.svc.reload_policy()

    def sent(self, text, *, session=GROUP_SESSION, status='succeeded', stamp=None, key='sent1'):
        return self.svc.state.record_activity({'action_id': key, 'platform': 'qq', 'bot_account_id': BOT,
            'conversation_id': session, 'kind': 'reply', 'status': status, 'content': text,
            'receipt_verified': status == 'succeeded', 'created': stamp or time.time(), 'sharing_scope': 'policy'})

    async def test_private_question_recalls_verified_public_group_participation(self):
        self.share_groups()
        self.sent('我和大家讨论了星际旅行。')
        self.sent('第二个群聊了画画。', session='group-two', key='sent2')
        result = await self.svc.build_context('qq_reply','qq',OWNER_SESSION,'今天在群聊说了什么')
        self.assertEqual({x['source_conversation'] for x in result['conversation_records']}, {GROUP_SESSION,'group-two'})
        self.assertTrue(all(x['speaker']=='我' and x['evidence']=='sent' for x in result['conversation_records']))
        self.svc.last_message[OWNER_SESSION]=('今天在群聊说了什么','question',OWNER_PRIVATE_ID)
        prompt=await self.svc.qq_context(OWNER_SESSION)
        self.assertIn('星际旅行',prompt)
        self.assertIn('画画',prompt)
        self.assertLessEqual(len(prompt),4800)

    async def test_policy_revoke_removes_group_evidence(self):
        self.share_groups();self.sent('群里说的话')
        self.assertTrue(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'群里聊什么'))
        self.policy['qq_group_recall_shared_enabled']=False;self.write_policy();self.svc.reload_policy()
        self.assertEqual(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'群里聊什么'),[])

    async def test_unverified_reply_does_not_prove_participation(self):
        self.share_groups();self.sent('没有发送完成的回答',status='unknown')
        self.assertEqual(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'群里聊什么'),[])

    async def test_group_evidence_respects_today_and_yesterday(self):
        self.share_groups();self.sent('昨天的话',stamp=time.time()-86400)
        self.assertEqual(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'今天群里聊什么'),[])
        self.assertEqual(len(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'昨天群里聊什么')),1)

    async def test_voice_is_available_without_long_term_memory(self):
        self.ctx.memory.fail=True
        await self.svc.dispatch(self.req('我刚问过蓝色飞船的消息'))
        self.ctx.memory.items=[]
        self.svc.last_message[OWNER_SESSION]=('刚才在设备聊了什么','question',OWNER_PRIVATE_ID)
        with patch.object(self.svc,'build_context',side_effect=asyncio.TimeoutError):
            prompt=await self.svc.qq_context(OWNER_SESSION)
        self.assertIn('蓝色飞船',prompt)
        self.assertIn('device_voice',prompt)
        self.assertIn('不能据此断言没有聊过',prompt)

    async def test_voice_query_is_not_displaced_by_newer_group_traffic(self):
        self.share_groups()
        await self.svc.dispatch(self.req('语音里说过蓝色飞船'))
        for i in range(8):
            self.sent('后来群里的一条消息',key=f'group-{i}')
        rows=conversation_recall.records(self.svc,'qq',OWNER_SESSION,'刚才在Passport聊过什么')
        self.assertTrue(rows)
        self.assertTrue(all(r['channel']=='device_voice' for r in rows))

    async def test_new_voice_evidence_survives_cached_context(self):
        await self.svc.qq_context(OWNER_SESSION)
        self.ctx.memory.fail=True
        await self.svc.dispatch(self.req('新出现的青绿色风筝'))
        self.assertIn('青绿色风筝',await self.svc.qq_context(OWNER_SESSION))

    async def test_busy_qq_does_not_evict_device_history(self):
        await self.svc.dispatch(self.req('我的语音历史锚点'))
        for i in range(130):
            self.svc.state.ingest({'event_id':str(i),'platform':'qq','bot_account_id':BOT,
                'conversation_id':OWNER_SESSION,'actor_id':OWNER_PRIVATE_ID,'event_type':'message_received',
                'content':'后来的一句QQ消息','occurred_at':time.time()+i})
        self.assertTrue(self.svc.voice.recent_history(OWNER_SESSION))
        self.assertIn('锚点',json.dumps(conversation_recall.records(self.svc,'qq',OWNER_SESSION,'语音历史'),ensure_ascii=False))

    async def test_topic_match_beats_unrelated_recent_voice(self):
        await self.svc.dispatch(self.req('青绿色飞船的发射日是周三'))
        for i in range(5):
            await self.svc.dispatch({**self.req('随便聊一句天气'),'turn_uid':f'{i:032x}'})
        records=conversation_recall.records(self.svc,'qq',OWNER_SESSION,'记得青绿色飞船吗')
        self.assertIn('青绿色飞船',records[0]['text'])

    async def test_public_groups_are_shared_across_authorized_channels(self):
        self.share_groups();self.sent('公共群聊的共同经历')
        self.assertTrue(conversation_recall.records(self.svc,'qq','group-two','群聊经历'))
        self.assertEqual(conversation_recall.records(self.svc,'qq','not-registered','群聊经历'),[])

    async def test_private_voice_does_not_become_public_group_content(self):
        self.share_groups()
        await self.svc.dispatch(self.req('我的私聊记录'))
        for session in (GROUP_SESSION,OTHER_SESSION):
            self.assertEqual(conversation_recall.records(self.svc,'qq',session,'语音记录'),[])

    async def test_group_semantic_facts_are_available_in_private(self):
        self.share_groups()
        self.ctx.memory.items.append({'external_id':'group-fact','chat_id':GROUP_SESSION,
            'text':'群里讨论过透明计算器','metadata':{'chat_id':GROUP_SESSION}})
        result=await self.svc.build_context('qq_reply','qq',OWNER_SESSION,'透明计算器')
        self.assertTrue(any('透明计算器' in x['text'] for x in result['facts']))


class PersistenceTests(BackendCase):
    def pending_now(self):
        for row in self.svc.state.db.execute("SELECT k,v FROM meta WHERE k LIKE 'voice_memory:%'").fetchall():
            job=json.loads(row['v']);job['next_at']=0;self.svc.state.put(row['k'],job)

    async def test_replay_reports_actual_memory_status(self):
        first=await self.svc.dispatch(self.req())
        second=await self.svc.dispatch(self.req())
        self.assertTrue(first['memory_stored'])
        self.assertTrue(second['memory_stored'])

    async def test_restart_retries_failed_write_without_rerunning_brain(self):
        self.ctx.memory.fail=True
        await self.svc.dispatch(self.req('可恢复的语音内容'))
        self.svc.voice=VoiceBridge(self.svc)
        self.ctx.memory.fail=False;self.pending_now()
        await self.svc.voice.retry_memory()
        self.assertEqual(len(self.ctx.voice_host.calls),1)
        self.assertEqual(len(self.ctx.memory.items),1)
        self.assertTrue((await self.svc.dispatch(self.req('可恢复的语音内容')))['memory_stored'])

    async def test_legacy_turn_recovery_keeps_timestamp_and_deduplicates(self):
        first=await self.svc.dispatch(self.req())
        stamp=self.ctx.memory.items[0]['timestamp']
        self.svc.state.db.execute("DELETE FROM meta WHERE k LIKE 'voice_memory:%'")
        self.svc.voice=VoiceBridge(self.svc)
        await self.svc.voice.retry_memory()
        self.assertEqual(len(self.ctx.memory.items),1)
        self.assertEqual(self.ctx.memory.items[0]['timestamp'],stamp)
        self.assertEqual(len(self.ctx.voice_host.calls),1)

    async def test_reply_is_durable_before_slow_identity_lookup(self):
        calls=0
        async def slow_after_first(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>1:await asyncio.sleep(20)
            return 'host-owner'
        with patch.object(self.svc,'resolve_qq_identity',side_effect=slow_after_first):
            result=await self.svc.dispatch(self.req())
        self.assertFalse(result['memory_stored'])
        self.assertEqual(self.svc.state.db.execute("SELECT count(*) FROM events WHERE event_type='device_voice_reply'").fetchone()[0],1)
        self.assertEqual(self.svc.state.db.execute("SELECT count(*) FROM meta WHERE k LIKE 'voice_memory:%'").fetchone()[0],1)

    async def test_retry_cannot_write_after_binding_revocation(self):
        self.ctx.memory.fail=True
        await self.svc.dispatch(self.req())
        self.ctx.memory.fail=False;self.pending_now();self.write_voice_bindings({})
        await self.svc.voice.retry_memory()
        self.assertEqual(self.ctx.memory.items,[])
