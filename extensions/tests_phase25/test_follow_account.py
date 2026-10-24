"""Operator follow tests use temporary SQLite and a synthetic API only."""
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import follow_account_remote as follow
from phase2.core import DEFAULTS, Denied, Store, TZ, machine_id
from phase2.engagement import CAPS, EngagementExecutor


class API:
    credentials = {'BILI_JCT': 'synthetic'}
    def __init__(self): self.writes=[];self.attribute=0;self.fail=False
    def identity(self): return {'mid':'100'}
    def profile(self, uid): return {'uid':uid,'name':'synthetic-author'}
    def signed(self, value): return value
    def request(self, path, params=None, data=None):
        if data is None: return {'relation':{'attribute':self.attribute}}
        self.writes.append((path,data))
        if self.fail: raise TimeoutError('synthetic')
        return {}


class ExplicitFollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
        self.now=datetime(2026,10,24,15,tzinfo=TZ).timestamp()
        self.c=copy.deepcopy(DEFAULTS)
        self.c.update(bot_uid='100',paused=False,writer_machine=machine_id(),
                      engagement={'enabled':True,'limits':dict(CAPS)},mentions={'manual_uids':['242616']})
        self.s.put('bound_uid','100');self.s.put('baseline_at',self.now-1)
        self.api=API()
    def tearDown(self): self.s.db.close();self.tmp.cleanup()
    def run_follow(self, apply=False, uid='242616'):
        if apply:
            with self.s.lock(): return follow.perform(self.s,lambda:copy.deepcopy(self.c),self.api,uid,True,lambda:self.now)
        return follow.perform(self.s,lambda:copy.deepcopy(self.c),self.api,uid,False,lambda:self.now)

    def test_default_preview_works_on_readonly_sqlite_and_never_creates_evidence(self):
        self.s.db.close()
        self.s.db=sqlite3.connect((Path(self.tmp.name)/'state.sqlite').as_uri()+'?mode=ro',uri=True)
        self.s.db.row_factory=sqlite3.Row
        result=self.run_follow()
        self.assertEqual(result['status'],'ready');self.assertFalse(result['platform_writes'])
        self.assertEqual(self.api.writes,[])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)
        self.assertIsNone(self.s.db.execute("SELECT name FROM sqlite_master WHERE name='engagement_actions'").fetchone())

    def test_apply_records_explicit_provenance_and_shares_automatic_follow_key_and_quota(self):
        first=self.run_follow(True)
        self.assertEqual(first['status'],'success')
        self.assertEqual(first['action_id'],'100:engagement:follow:author:242616')
        self.assertEqual(self.s.used('100','follow',self.now),1)
        self.assertEqual(self.api.writes,[('/x/relation/modify',{'fid':'242616','act':1,'re_src':11,'csrf':'synthetic'})])
        row=self.s.db.execute('SELECT evidence FROM engagement_actions').fetchone()
        self.assertEqual(row[0],'operator_explicit')
        event=json.loads(self.s.db.execute('SELECT event_json FROM action_context').fetchone()[0])
        self.assertEqual(event['origin'],'operator_explicit');self.assertEqual(event['event_type'],'operator_explicit')
        self.assertFalse(event['learning_allowed']);self.assertEqual(event['content'],'')
        self.assertEqual(self.run_follow(True)['reason'],'already_attempted_no_resend')
        self.assertEqual(len(self.api.writes),1)

    def test_unknown_never_retries_after_restart_even_if_state_is_still_unfollowed(self):
        self.api.fail=True
        self.assertEqual(self.run_follow(True)['status'],'unknown')
        self.s.db.close();self.s=Store(self.tmp.name);self.api.fail=False
        self.assertEqual(self.run_follow(True)['status'],'unknown')
        self.assertEqual(len(self.api.writes),1)
        self.assertEqual(self.s.used('100','follow',self.now),1)

    def test_target_allowlist_existing_relation_and_daily_limit_fail_closed(self):
        with self.assertRaisesRegex(Denied,'explicitly_authorized'):self.run_follow(True,'999')
        self.c['mentions']['manual_uids']=[]
        with self.assertRaisesRegex(Denied,'manual_contact'):self.run_follow(True)
        self.c['mentions']['manual_uids']=['242616'];self.api.attribute=2
        self.assertEqual(self.run_follow(True)['status'],'already_following')
        self.assertEqual(self.api.writes,[])
        self.api.attribute=0
        for i in range(20):
            self.s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (str(i),'100','follow','author:'+str(i),'x','x','2026-10-24',self.now,'unknown','','','',''))
        with self.assertRaisesRegex(Denied,'daily_limit'):self.run_follow(True)
        self.assertEqual(self.api.writes,[])


if __name__=='__main__':unittest.main()
