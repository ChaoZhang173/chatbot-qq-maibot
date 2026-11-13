"""Offline regressions for author confusion and repeated unsolicited group targeting."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from maibot_unified_core import group_audience, qq_identity_context
from maibot_unified_core.commands import CommandQueue
from maibot_unified_core.private_access import PrivateAccess
from maibot_unified_core.qq_proactive import ProactiveQQ, window
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State


class GroupAudienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=time.time()
        self.p={'qq_bot_account':'9000000001','bot_accounts':{'qq':'9000000001'},'allowed_qq_sessions':[],
                'qq_owner_bindings':{},'qq_group_sessions':{},'qq_native':{'enabled':True,'proactive_enabled':True,
                'account':'9000000001','owner':'9000000002','groups':['9000000003']}}
        (self.root/'policy.json').write_text(json.dumps(self.p))
        async def opened(**kw):return {'success':True,'session_id':'group' if kw['chat_type']=='group' else 'private'}
        self.ctx=SimpleNamespace(chat=SimpleNamespace(open_session=AsyncMock(side_effect=opened)),
             send=SimpleNamespace(text=AsyncMock(return_value={'sent':True,'message_id':'receipt'})))
        self.s=UnifiedService(self.ctx,self.root);self.s.policy=self.p;self.s.state=State(self.root/'state.sqlite',self.p)
        self.s.state.init_persona({'identity':'大肥鱼'},{'interests':['游戏']})
        self.s.private_access=PrivateAccess(self.s);self.s.commands=CommandQueue(self.s)
        self.s.daily=SimpleNamespace(recent_items=lambda *a,**k:[])
        self.s.build_context=AsyncMock(return_value={'persona':self.s.state.persona(),'source_ids':[],
            'facts':[],'activities':[],'current_speaker':{'nickname':'甲'},'relationship':['哥哥']})
        self.s.generate=AsyncMock(return_value={'willing':True,'text':'这个节奏还挺有意思。'})
        self.q=ProactiveQQ(self.s)
        async def keep_supported(service,draft,*args,**kwargs):return draft
        self.check_patch=patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock(side_effect=keep_supported))
        self.check=self.check_patch.start()

    async def asyncSetUp(self):
        await self.q.provision();self.d=self.q.destinations()[0]
        self.s.state.put('qq_native_ready_at',self.now-700)

    def tearDown(self):
        self.check_patch.stop();self.s.state.close();self.tmp.cleanup()

    def event(self,key,actor,name,text,at,**extra):
        result=self.s.state.ingest({'event_id':key,'platform':'qq','bot_account_id':'9000000001',
            'conversation_id':'group','actor_id':actor,'nickname':name,'origin':'external_user',
            'event_type':'message_received','content':text,'occurred_at':at,'received_at':at,**extra})
        return result['event_id']

    def attempt(self,key,text,at,target=None):
        self.s.state.db.execute('INSERT INTO qq_proactive_attempts(id,session,window,created,status,text) VALUES(?,?,?,?,?,?)',
            (key,'group',window(at)[0],at,'sent',text))
        if target is not None:self.s.state.put(group_audience.target_key(key),target)

    async def test_authors_and_times_survive_history_without_implied_response(self):
        self.event('a','10101','甲','我在听歌',self.now-1000)
        self.attempt('old','甲，听完了吗？',self.now-900)
        b=self.event('b','20202','乙','上班没带耳机',self.now-800)
        self.event('self','9000000001','大肥鱼','甲，听完了吗？',self.now-890)
        people=group_audience.people(self.s,'group',self.now)
        topic=self.q.own_topics('group',self.now,participants=people)[0]
        self.assertNotIn('甲',topic['i_said']);self.assertNotIn('their_reaction',topic)
        self.assertEqual(topic['target_user_ids'],['10101']);self.assertFalse(topic['target_spoke_after'])
        self.assertEqual(topic['later_messages'][0]['user_id'],'20202')
        self.assertEqual(topic['later_messages'][0]['event_id'],b)
        self.assertEqual(topic['later_messages'][0]['at'],self.now-800)
        recent=self.q.recent_messages('group','9000000001','group',self.now,participants=people)
        self.assertTrue(all('user_id' in x and 'at' in x and 'event_id' in x for x in recent))
        self.assertNotIn('甲',next(x for x in recent if x['origin']=='bot_self')['text'])
        self.assertFalse(next(x for x in recent if x['origin']=='bot_self')['is_evidence'])

    async def test_other_member_does_not_clear_target_backoff(self):
        self.event('a','10101','甲','刚聊过',self.now-1000)
        self.attempt('old','甲，来聊聊。',self.now-900)
        self.event('b','20202','乙','嗯',self.now-800)
        self.assertEqual(self.q.unanswered_streak('group'),1)
        people={p['user_id']:p for p in group_audience.people(self.s,'group',self.now)}
        self.assertFalse(people['10101']['addressable']);self.assertTrue(people['10101']['waiting_for_this_author'])
        self.assertTrue(people['20202']['addressable'])
        self.event('a2','10101','甲','我回来了',self.now-700)
        self.assertEqual(self.q.unanswered_streak('group'),0)
        self.assertTrue(next(p for p in group_audience.people(self.s,'group',self.now) if p['user_id']=='10101')['addressable'])

    async def test_stale_speaker_and_personal_state_are_rejected(self):
        eid=self.event('a','10101','甲','上午好',self.now-12*3600)
        self.s.last_message['group']=('上午好',eid,'10101')
        self.assertEqual(qq_identity_context.current(self.s,'group',now=self.now),{})
        people=group_audience.people(self.s,'group',self.now)
        answer={'text':'甲，怎么还没睡？','audience_uid':'10101','audience_event_id':eid}
        self.assertIsNone(group_audience.selection(self.s,'group',answer,people,self.now))
        self.assertIsNone(group_audience.selection(self.s,'group',{'text':'怎么还没睡？'},people,self.now))
        self.assertEqual(group_audience.selection(self.s,'group',{'text':'这首曲子我还想听一遍。'},people,self.now),{})

    async def test_current_speaker_keeps_source_time_and_expires(self):
        eid=self.event('a','10101','甲','晚上好',self.now-60)
        self.s.last_message['group']=('晚上好',eid,'10101')
        speaker=qq_identity_context.current(self.s,'group',now=self.now)
        self.assertEqual(speaker['source_event'],eid);self.assertEqual(speaker['at'],self.now-60)
        self.assertIn('when',speaker)
        self.assertEqual(qq_identity_context.current(self.s,'group',now=self.now+1801),{})
        self.assertEqual(qq_identity_context.current(self.s,'group',now=self.now-61),{})

    async def test_latest_source_binding_cannot_mix_authors_or_ignore_retraction(self):
        a=self.event('a','10101','甲','说歌',self.now-300)
        b=self.event('b','20202','乙','没带耳机',self.now-200)
        people=group_audience.people(self.s,'group',self.now)
        self.assertIsNone(group_audience.selection(self.s,'group',{'text':'甲，听完了吗？','audience_uid':'10101','audience_event_id':b},people,self.now))
        good={'text':'甲，你刚才提的歌名是什么？','audience_uid':'10101','audience_event_id':a}
        self.assertIsNotNone(group_audience.selection(self.s,'group',good,people,self.now))
        self.s.state.db.execute('UPDATE events SET active=0 WHERE event_id=?',(a,))
        self.assertIsNone(group_audience.selection(self.s,'group',good,people,self.now))

    async def test_same_nickname_cannot_select_one_then_address_both(self):
        a=self.event('a','10101','同名','先聊',self.now-300)
        self.event('b','20202','同名','后聊',self.now-200)
        people=group_audience.people(self.s,'group',self.now)
        self.assertIsNone(group_audience.selection(self.s,'group',{'text':'同名，来听歌。','audience_uid':'10101','audience_event_id':a},people,self.now))
        self.assertEqual(group_audience.addressed('我想起甲之前分享的曲子。',[{'user_id':'10101','nickname':'甲'}]),set())

    async def test_groupwide_message_removes_stale_identity_and_sends_once(self):
        r=await self.q.converse(self.d,self.now)
        self.assertEqual(r['status'],'sent');self.ctx.send.text.assert_awaited_once()
        payload=json.loads(self.s.generate.call_args.args[0][1]['content'])
        self.assertNotIn('current_speaker',payload['context']);self.assertNotIn('relationship',payload['context'])
        self.assertEqual(payload['participants'],[])

    async def test_supported_address_passes_correction_and_self_attempt_does_not_block(self):
        a=self.event('a','10101','甲','这首歌好听',self.now-300)
        self.s.generate.return_value={'willing':True,'text':'甲，歌名给我一下。','audience_uid':'10101','audience_event_id':a}
        with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock(return_value='甲，歌名给我一下。')) as check:
            r=await self.q.converse(self.d,self.now)
        self.assertEqual(r['status'],'sent');check.assert_awaited_once();self.ctx.send.text.assert_awaited_once()
        key=self.s.state.db.execute('SELECT id FROM qq_proactive_attempts').fetchone()[0]
        self.assertEqual(self.s.state.get(group_audience.target_key(key))['event_id'],a)
        self.assertEqual(self.q.unanswered_streak('group'),1)

    async def test_reviewer_decline_or_wrong_person_never_sends(self):
        a=self.event('a','10101','甲','晚饭吃过了',self.now-300)
        self.event('b','20202','乙','嗯',self.now-200)
        self.s.generate.return_value={'willing':True,'text':'甲，怎么还没睡？','audience_uid':'10101','audience_event_id':a}
        for i,result in enumerate((None,'乙，来听歌。')):
            with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock(return_value=result)):
                r=await self.q.converse(self.d,self.now+i)
            self.assertIn(r['status'],('cancelled','declined'))
        self.ctx.send.text.assert_not_called()

    async def test_source_expiring_or_new_message_during_review_cancels(self):
        a=self.event('a','10101','甲','来听歌',self.now-1799)
        self.s.generate.return_value={'willing':True,'text':'甲，歌名给我一下。','audience_uid':'10101','audience_event_id':a}
        async def check(*args,**kwargs):
            self.event('b','20202','乙','刚发来的消息',time.time())
            return '甲，歌名给我一下。'
        with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=check):
            self.assertEqual((await self.q.converse(self.d,self.now))['status'],'cancelled')
        self.ctx.send.text.assert_not_called()
        people=group_audience.people(self.s,'group',self.now)
        self.assertIsNone(group_audience.selection(self.s,'group',self.s.generate.return_value,people,self.now+2))

    async def test_voice_rechecks_audience_after_synthesis(self):
        a=self.event('a','10101','甲','歌好听',self.now-300)
        self.s.generate.return_value={'willing':True,'text':'甲，歌名给我一下。','audience_uid':'10101','audience_event_id':a,'delivery':'voice'}
        voice=SimpleNamespace(enabled=lambda s:True)
        async def deliver(session,text,mood,*,voice_style,still_allowed):
            self.assertTrue(still_allowed())
            self.s.state.db.execute('UPDATE events SET active=0 WHERE event_id=?',(a,))
            self.assertFalse(still_allowed())
            return {'status':'cancelled'}
        voice.deliver=deliver;self.s.qq_voice=voice
        with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock(return_value='甲，歌名给我一下。')):
            self.assertEqual((await self.q.converse(self.d,self.now))['status'],'cancelled')
        self.ctx.send.text.assert_not_called()

    async def test_unbound_name_goes_through_semantic_review(self):
        self.event('a','10101','沙包','上午好',self.now-12*3600)
        self.s.generate.return_value={'willing':True,'text':'沙包今天是不是累了？','audience_uid':'','audience_event_id':''}
        with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock(return_value=None)) as check:
            self.assertEqual((await self.q.converse(self.d,self.now))['status'],'declined')
        self.assertEqual(check.call_args.kwargs['group_audience']['audience'],{})
        self.assertIn('self_context',check.call_args.kwargs['group_audience'])
        self.ctx.send.text.assert_not_called()

    async def test_private_voluntary_flow_has_no_group_gate(self):
        private=next(d for d in self.q.destinations() if d['kind']=='private')
        self.s.generate.return_value={'willing':True,'text':'又想聊聊音乐了。'}
        with patch('maibot_unified_core.qq_proactive.experience_check.correct',new=AsyncMock()) as check:
            self.assertEqual((await self.q.converse(private,self.now))['status'],'sent')
        check.assert_not_called()


if __name__=='__main__':unittest.main()
