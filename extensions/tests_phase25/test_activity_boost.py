import copy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

import test_bili_unified as fixtures
from phase2 import boost
from phase2.core import TZ, Denied


class ActivityBoostTests(fixtures.TestBiliUnified):
    def setUp(self):
        super().setUp()
        self.c['activity_boost']={'enabled':True,'starts_at':self.now-60,'ends_at':self.now-60+604800,
            'browse_min':900,'browse_max':1800,'poll_seconds':30,'dynamic_slots':['09:30','15:30','20:30'],
            'limits':dict(boost.TEMP_LIMITS),'engagement_limits':dict(boost.TEMP_ENGAGEMENT)}
        self.config.write_text(json.dumps(self.c))

    def test_boost_expiry_restores_original_settings_and_does_not_extend_on_restart(self):
        end=self.c['activity_boost']['ends_at']
        self.assertEqual(boost.poll_seconds(self.c,self.now),30)
        self.assertIsNone(boost.active(self.c,end))
        self.assertEqual(boost.poll_seconds(self.c,end),60)
        self.assertEqual(self.c['browse_min'],2700)
        self.assertEqual(self.engine.config()['limits']['reply'],40)
        self.assertEqual(boost.effective(self.c,end)['limits']['reply'],20)
        for change in ({'ends_at':end+1},{'browse_min':30},{'dynamic_slots':['09:30']},{'poll_seconds':1}):
            c=copy.deepcopy(self.c);c['activity_boost'].update(change)
            with self.assertRaises(ValueError):boost.policy(c)
        self.engine.s.db.close();self.engine=self.new_engine()
        self.assertEqual(self.engine.config()['activity_boost']['ends_at'],end)

    def test_slots_are_opportunities_only_no_duplicate_or_missed_slot_replay(self):
        self.now=datetime(2026,10,24,15,31,tzinfo=TZ).timestamp()
        with self.engine.s.lock():
            a=boost.tick(self.engine,self.c,self.now)
            self.assertEqual(a['results']['dynamic']['status'],'success')
            self.assertEqual(boost.tick(self.engine,self.c,self.now)['results'],{})
        self.assertEqual(len(self.api.calls),1)
        self.engine.s.db.close();self.engine=self.new_engine()
        with self.engine.s.lock():
            self.assertEqual(boost.tick(self.engine,self.c,self.now)['results'],{})
            self.now+=3600
            with patch.object(self.engine,'dynamic') as dynamic, patch('phase2.boost.browse',return_value={'status':'no_candidate'}):
                boost.tick(self.engine,self.c,self.now)
                dynamic.assert_not_called()
        self.assertEqual(len(self.api.calls),1)

    def test_comment_cap_does_not_block_read_decision_or_reset_reply_quotas(self):
        for i in range(12):
            self.engine.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(i),'100','proactive',f'1:{i+10}','300','','2026-10-24',self.now-4000,'unknown','','','',''))
        with self.engine.s.lock():
            result=boost.browse(self.engine,self.engine.config())
            self.assertEqual(result['model_decisions'],1)
            self.assertFalse(result['outcomes'][0]['comment_allowed'])
            self.assertEqual(boost.browse(self.engine,self.engine.config())['model_decisions'],0)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.decide_calls()[-1]['permissions']['comment_allowed'],False)
        self.assertEqual(self.engine.s.used('100','proactive',self.now),12)

    def test_no_content_no_forced_post_and_read_provenance_reaches_engagement(self):
        original=self.core
        def transport(payload):
            result=original(payload)
            if payload['op']=='decide':
                result['decision']['text']=''
                result['decision']['engagement']={'like':True,'favorite':False,'coin':0,'follow_author':False}
            return result
        self.engine.unified_request=transport
        self.c['engagement']={'enabled':True,'limits':{'like':10,'favorite':5,'coin':2,'follow':20}}
        self.config.write_text(json.dumps(self.c))
        executor=unittest.mock.MagicMock()
        executor.execute.return_value=[]
        with self.engine.s.lock(),patch.object(self.engine,'engagement',return_value=executor):
            result=boost.browse(self.engine,self.engine.config())
        self.assertEqual(result['model_decisions'],1)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(executor.execute.call_count,1)
        args=executor.execute.call_args.args
        row=self.engine.s.db.execute('SELECT payload FROM unified_events WHERE event_id=?',(args[2],)).fetchone()
        self.assertEqual(json.loads(row[0])['material_evidence'],'title_description_subtitle_excerpt')

    def test_paused_or_expired_boost_never_reads_or_generates(self):
        with self.engine.s.lock(),patch.object(self.api,'candidates') as candidates:
            self.engine.s.put('pause_reason','platform_rejected')
            with self.assertRaises(Denied):boost.browse(self.engine,self.c)
            candidates.assert_not_called()

    def test_expiry_restores_caps_without_refunding_already_used_allowance(self):
        from phase2.core import day
        end=self.c['activity_boost']['ends_at']
        self.now=end-60
        for i in range(25):
            self.engine.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(i),'100','reply','1:44','300','thread',day(self.now),self.now-100,'success','','','',''))
        self.assertEqual(self.engine.config()['limits']['reply'],40)
        self.now=end
        restored=self.engine.config()
        self.assertEqual(restored['limits']['reply'],20)
        self.assertEqual(self.engine.s.used('100','reply',self.now),25)
        with self.assertRaisesRegex(Denied,'daily_limit'):
            self.engine.s.permit(restored,'reply',None,self.now,key='new')


if __name__=='__main__':unittest.main()
