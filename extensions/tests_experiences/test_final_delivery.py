"""Exercise the existing scheduler and real sender with a fake platform API."""
import unittest
import test_diary as old
from phase2 import diary


class FinalDelivery(unittest.TestCase):
    new_engine=old.ScheduleTests.new_engine
    setUp=old.ScheduleTests.setUp
    tearDown=old.ScheduleTests.tearDown

    def test_revoked_after_review_never_dispatches(self):
        original=self.engine.unified_request;calls=[]
        def transport(r):
            calls.append(r['op'])
            if r['op']=='diary_validate':return {'approved':False}
            result=original(r)
            if r['op']=='diary_draft':result['requires_final_validation']=True
            return result
        self.engine.unified_request=transport
        with self.engine.s.lock():result=diary.tick(self.engine)
        self.assertEqual(result['status'],'failed');self.assertEqual(self.api.calls,[])
        self.assertEqual(calls.count('diary_validate'),1)
        self.assertLess(calls.index('diary_review'), calls.index('diary_validate'))

    def test_valid_final_draft_dispatches_once_without_extra_model_call(self):
        original=self.engine.unified_request;calls=[]
        def transport(r):
            calls.append(r['op'])
            if r['op']=='diary_validate':return {'approved':True}
            result=original(r)
            if r['op']=='diary_draft':result['requires_final_validation']=True
            return result
        self.engine.unified_request=transport
        with self.engine.s.lock():result=diary.tick(self.engine)
        self.assertEqual(result['status'],'success');self.assertEqual(len(self.api.calls),1)
        self.assertEqual(calls.count('diary_validate'),1)
