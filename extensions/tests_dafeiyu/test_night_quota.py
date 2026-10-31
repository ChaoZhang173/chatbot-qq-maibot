import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock
from phase2 import quota,boost
from phase2.core import DEFAULTS,TZ,Store,Sender,Denied,load_config,machine_id,active,future,day

def ts(s):return datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()

class NightQuotaTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'config.json'
  self.c=copy.deepcopy(DEFAULTS);self.c.update(night_activity_enabled=True,active_start=0,active_end=24,bot_uid='100',owner_uid='200',writer_machine=machine_id(),paused=False)
  self.c['enabled']={k:True for k in self.c['enabled']};self.path.write_text(json.dumps(self.c))
  self.s=Store(self.root/'state');self.s.put('bound_uid','100');self.s.put('baseline_at',1)
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def action(self,key,kind,t,status='success',target='room'):
  self.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,'100',kind,target,'300','thread',day(t),t,status,'','','',''))
 def test_boundaries_and_no_midnight_reset(self):
  self.assertEqual(quota.window(self.c,ts('2026-10-25T23:00')),quota.window(self.c,ts('2026-10-26T00:00')))
  self.assertEqual(quota.window(self.c,ts('2026-10-25T23:00')),quota.window(self.c,ts('2026-10-26T08:59:59')))
  self.assertEqual(quota.window(self.c,ts('2026-10-26T09:00'))[2],'day')
  self.assertEqual(quota.window(self.c,ts('2026-10-25T22:59:59'))[2],'day')
  for h in range(24):self.assertTrue(active(load_config(self.path,ts(f'2026-10-26T{h:02}:00')),ts(f'2026-10-26T{h:02}:00')))
 def test_independent_counts_failed_unknown_and_restart(self):
  for i in range(10):self.action('d'+str(i),'dynamic',ts('2026-10-25T12:00'))
  night=ts('2026-10-25T23:01');c=load_config(self.path,night)
  self.assertEqual(c['limits']['dynamic'],3);self.s.permit(c,'dynamic',None,night)
  for i,status in enumerate(['success','unknown','failed']):self.action('n'+str(i),'dynamic',night,status)
  midnight=ts('2026-10-26T00:01')
  with self.assertRaisesRegex(Denied,'daily_limit'):self.s.permit(load_config(self.path,midnight),'dynamic',None,midnight)
  self.s.db.close();self.s=Store(self.root/'state')
  self.assertEqual(self.s.used('100','dynamic',midnight,self.c),3)
  morning=ts('2026-10-26T09:00');self.assertEqual(self.s.used('100','dynamic',morning,self.c),0);self.s.permit(load_config(self.path,morning),'dynamic',None,morning)
 def test_boost_then_quarter_and_expiry(self):
  now=ts('2026-10-25T23:01');end=now+86400
  self.c['activity_boost']={'enabled':True,'starts_at':now-86400,'ends_at':end,'browse_min':900,'browse_max':1800,'poll_seconds':30,'dynamic_slots':['09:30','15:30','20:30'],'limits':dict(boost.TEMP_LIMITS),'engagement_limits':dict(boost.TEMP_ENGAGEMENT)}
  self.c['engagement']={'enabled':True,'limits':{'like':10,'favorite':5,'follow':20,'coin':2}}
  from phase2.live import DEFAULT
  self.c['live_chat']={**DEFAULT,'enabled':True};self.path.write_text(json.dumps(self.c));c=load_config(self.path,now)
  self.assertEqual(c['limits'],dict(dynamic=5,proactive=3,reply=10,dm=5));self.assertEqual(c['model_daily'],20)
  self.assertEqual(c['engagement']['limits'],dict(like=5,favorite=3,follow=5,coin=1))
  # Current live message quotas span the calendar day; only visits scale at night.
  self.assertEqual([c['live_chat'][k] for k in ('daily_limit','room_daily_limit','visits_daily')],[100,50,1])
  self.assertEqual(c['autonomy_period_limit'],8);self.assertEqual(c['account_period_limit'],1)
  self.assertEqual(load_config(self.path,end)['limits']['dynamic'],3)
 def test_usage_visits_and_settings_share_whole_night(self):
  t=ts('2026-10-25T23:30');later=ts('2026-10-26T08:30')
  self.s.db.execute('INSERT INTO usage(created,day,status,model) VALUES(?,?,?,?)',(t,day(t),'failed','fake'))
  self.assertEqual(quota.count(self.s.db,self.c,later,'usage'),1)
  self.s.db.execute('CREATE TABLE live_visits(started REAL)');self.s.db.execute('INSERT INTO live_visits VALUES(?)',(t,))
  self.s.db.execute('CREATE TABLE account_changes(created REAL)');self.s.db.execute('INSERT INTO account_changes VALUES(?)',(t,))
  for table in ('live_visits','account_changes'):self.assertEqual(quota.count(self.s.db,self.c,later,table),1)
  self.assertEqual(quota.autonomy_key(self.c,t),quota.autonomy_key(self.c,later))
 def test_zero_quota_stays_zero_and_bad_hours_rejected(self):
  self.c['limits']['dynamic']=0;self.path.write_text(json.dumps(self.c));self.assertEqual(load_config(self.path,ts('2026-10-25T23:01'))['limits']['dynamic'],0)
  for start,end in [(0,25),(24,24),(-1,24),(True,24),(9,23)]:
   self.c.update(active_start=start,active_end=end);self.path.write_text(json.dumps(self.c))
   with self.assertRaises(ValueError):load_config(self.path)
 def test_no_night_optin_preserves_old_hours_and_counters(self):
  self.c.update(night_activity_enabled=False,active_start=9,active_end=23);self.path.write_text(json.dumps(self.c));c=load_config(self.path,ts('2026-10-25T23:01'))
  self.assertFalse(active(c,ts('2026-10-25T23:01')));self.assertEqual(c['model_daily'],80)
 def test_full_day_scheduler_and_live_duration_cross_midnight(self):
  t=ts('2026-10-25T23:59:59');c=load_config(self.path,t)
  for kind in ['dynamic','proactive']:self.assertGreater(future(c,t,kind),t)
  self.assertEqual(quota.activity_end(c,t,3600),t+3600)
 def test_generation_cannot_cross_day_night_budget(self):
  now=[ts('2026-10-25T22:59:50')];api=Mock();api.identity.return_value={'mid':'100'}
  sender=Sender(self.s,self.path,api,lambda:now[0])
  def generate():now[0]=ts('2026-10-25T23:00:01');return '今天学到一点新知识'
  result=sender.deliver('dynamic',None,'boundary',generate)
  self.assertEqual(result['status'],'paused');api.send.assert_not_called()
 def test_bounded_model_calls_stop_at_night_limit(self):
  from phase2.runner import Engine
  now=ts('2026-10-26T00:01');e=Engine(self.path,self.root/'otherstate',self.root,api=Mock(),now=lambda:now)
  e.unified_request=Mock(return_value={})
  for i in range(20):e.bounded_core({'op':'test'})
  with self.assertRaisesRegex(Denied,'model_daily_limit'):e.bounded_core({'op':'test'})
  self.assertEqual(e.unified_request.call_count,20);e.s.db.close()

if __name__=='__main__':unittest.main(verbosity=2)
