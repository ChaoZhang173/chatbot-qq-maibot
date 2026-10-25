import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State


class EngagementPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.policy={'bili_bot_uid':'100','decisions_enabled':True}
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        self.service=UnifiedService(types.SimpleNamespace(),self.root)
        self.service.reload_policy();self.service.state=State(self.root/'state.sqlite',self.policy)
        self.service.state.init_persona({'identity':'unchanged'},{})
        self.answer={'interested':True,'text':'这段介绍很有参考价值。','interest_score':80,'reason':'synthetic',
                     'engagement':{'like':True,'coin':1,'favorite':True,'follow_author':False}}
        self.answer['engagement_reasons']={k:'基于实际内容的独立选择' for k in self.answer['engagement']}
        self.calls=[]
        async def generate(messages,**kw):self.calls.append(messages);return copy.deepcopy(self.answer)
        async def context(*args,**kw):return {'persona':self.service.state.persona(),'facts':[],'activities':[],'source_ids':[],'memory_available':True}
        self.service.generate=generate;self.service.build_context=context

    def tearDown(self):self.service.state.close();self.tmp.cleanup()

    def request(self):
        return {'task':'proactive','material':{'text':'实际标题与简介','evidence':'title_description'},
                'permissions':{'enabled':True,'paused':False,'engagement':{'enabled':True,'actual_read_video':True,
                'limits':{'like':10,'favorite':5,'follow':2,'coin':2},'used_today':{'like':0,'favorite':0,'follow':0,'coin':0}}},
                'event':{'event_id':'synthetic-read','platform':'bilibili','bot_account_id':'100','conversation_id':'1:123',
                'actor_id':'200','origin':'platform_content','event_type':'content_read','content':'实际标题与简介',
                'learning_allowed':True,'material_evidence':'title_description',
                'target':{'kind':'video','key':'1:123','oid':'123','type':1,'author':'200'}}}

    async def test_actual_read_same_decision_has_bounded_optional_intents(self):
        result=await self.service.decide(self.request())
        self.assertEqual(result['decision'],self.answer)
        self.assertIn('不是完整观看证明',self.calls[-1][0]['content'])
        self.assertIn('"coin": 2',self.calls[-1][0]['content'])
        self.assertEqual(len(self.calls),1)

    async def test_comment_denied_does_not_cancel_interaction_and_audit_survives(self):
        request=self.request();request['permissions']['comment_allowed']=False
        result=await self.service.decide(request)
        self.assertEqual(result['decision']['text'],'')
        self.assertTrue(result['decision']['engagement']['like'])
        row=self.service.state.db.execute('SELECT payload FROM engagement_decisions ORDER BY id DESC LIMIT 1').fetchone()
        audit=json.loads(row[0]);self.assertEqual(audit['validation'],'valid')
        self.assertEqual(set(audit['engagement_reasons']),{'like','favorite','coin','follow_author'})
        self.answer.pop('engagement_reasons')
        with self.assertRaisesRegex(ValueError,'reasons_required'):await self.service.decide(self.request())
        bad=json.loads(self.service.state.db.execute('SELECT payload FROM engagement_decisions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(bad['validation'],'engagement_reasons_required')

    async def test_missing_schema_preserves_legacy_shape_and_no_actions(self):
        self.answer.pop('engagement');self.answer.pop('engagement_reasons')
        request=self.request();request['permissions'].pop('engagement')
        self.assertEqual((await self.service.decide(request))['decision'],self.answer)
        self.assertNotIn('engagement:',self.calls[-1][0]['content'])

    async def test_actions_require_current_actual_video_read_and_explicit_permission(self):
        changes=[lambda r:r.update(task='dynamic'),lambda r:r['event'].update(event_type='message_received'),
                 lambda r:r['event'].update(origin='external_user'),lambda r:r['event']['target'].update(kind='dynamic'),
                 lambda r:r['event']['target'].update(author='999'),lambda r:r['event'].update(conversation_id='1:999'),
                 lambda r:r['material'].update(text='unrelated material'),lambda r:r['permissions'].update(paused=True),
                 lambda r:r['permissions']['engagement'].update(actual_read_video=False),
                 lambda r:r['permissions'].pop('engagement')]
        for index,change in enumerate(changes):
            request=self.request();change(request);request['event']['event_id']='synthetic-read-'+str(index)
            with self.assertRaisesRegex(ValueError,'invalid_engagement_decision'):
                await self.service.decide(request)

    async def test_exact_schema_finite_interest_and_remaining_budget(self):
        valid=copy.deepcopy(self.answer)
        for actions in ({'like':True},dict(valid['engagement'],coin=True),dict(valid['engagement'],coin=2),
                        dict(valid['engagement'],favorite=1),dict(valid['engagement'],extra=True)):
            self.answer['engagement']=actions
            with self.assertRaisesRegex(ValueError,'invalid_engagement_decision|engagement_choices_required'):await self.service.decide(self.request())
        self.answer=copy.deepcopy(valid)
        for score in (64,float('nan'),float('inf'),True):
            self.answer['interest_score']=score
            with self.assertRaisesRegex(ValueError,'engagement_interest_required|invalid_interest_score'):await self.service.decide(self.request())
        self.answer=copy.deepcopy(valid);self.answer['interested']=False
        with self.assertRaisesRegex(ValueError,'engagement_interest_required|invalid_interest_score'):await self.service.decide(self.request())
        self.answer=copy.deepcopy(valid);request=self.request();request['permissions']['engagement']['used_today']['coin']=2
        with self.assertRaisesRegex(ValueError,'engagement_budget_exhausted'):await self.service.decide(request)
        self.answer['engagement']['coin']=0
        self.assertEqual((await self.service.decide(request))['decision']['engagement']['coin'],0)

    async def test_malformed_or_above_authorized_permissions_fail_before_model(self):
        request=self.request();request['permissions']['engagement']['limits']['coin']=3
        with self.assertRaisesRegex(ValueError,'invalid_engagement_permissions'):await self.service.decide(request)
        self.assertEqual(self.calls,[])

    async def test_temporary_caps_require_live_core_authorization_and_expire(self):
        request=self.request()
        request['permissions']['engagement']['limits']={'like':20,'favorite':10,'follow':20,'coin':4}
        with self.assertRaisesRegex(ValueError,'invalid_engagement_permissions'):
            await self.service.decide(request)
        now=time.time()
        self.policy['bili_activity_boost']={'starts_at':now-60,'ends_at':now+600,
            'engagement_limits':{'like':20,'favorite':10,'follow':20,'coin':4}}
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        self.assertTrue((await self.service.decide(request))['decision']['interested'])
        self.policy['bili_activity_boost']['ends_at']=now-1
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError,'invalid_engagement_permissions'):
            await self.service.decide(request)


if __name__=='__main__':unittest.main()
