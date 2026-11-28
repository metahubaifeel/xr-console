import json
import threading
import unittest
from harness.config import settings
from harness.engine import Harness, Conflict
from harness.tests.fakes import MockRobot, MockVision

class SingleStepTests(unittest.TestCase):
    def setUp(self):self.r=MockRobot();self.v=MockVision();self.h=Harness(settings(),self.r,self.v)
    def tearDown(self):self.h.close()
    def verify(self,sid,step):
        if self.h.pending:self.h.pending.result(3)
        self.h.observe(sid,step);self.h.pending.result(3)
    def test_step1_finishes_without_step2(self):
        sid=self.h.start(step=1,single_step=True)['session_id']
        self.verify(sid,1);s=self.h.state()
        self.assertEqual((s['status'],s['step']),('test_completed',1));self.assertEqual(len(self.r.commands),1)
        self.assertEqual(json.loads(s['raw_judgment'])['step'],1);self.assertEqual(self.v.calls,1)
    def test_uncertain_also_stops_single_test(self):
        self.v.judge=lambda c,o,i:{'step':1,'status':'UNCERTAIN','evidence':'遮挡','next_action':'观察','held_objects':{'scanner':'unknown','bottle':None}}
        sid=self.h.start(step=1,single_step=True)['session_id'];self.verify(sid,1)
        s=self.h.state();self.assertEqual(s['status'],'test_completed');self.assertFalse(s['test_result']['success']);self.assertEqual(len(self.r.commands),1)
    def test_step4_frontend_event_stops_without_placing(self):
        sid=self.h.start(step=4,single_step=True)['session_id'];self.h.pending.result(3)
        self.assertEqual(len(self.r.commands),1);self.assertEqual(self.v.calls,1) # holding preflight only
        self.h.scan(sid,'OUTA');s=self.h.state()
        self.assertEqual((s['status'],s['step']),('test_completed',4));self.assertEqual(len(self.r.commands),1);self.assertEqual(self.v.calls,1)
    def test_step5_mapping_has_no_motion_or_vlm(self):
        s=self.h.start(step=5,single_step=True,code='OUTC')
        self.assertEqual((s['status'],s['target']),('test_completed','silver'));self.assertEqual(self.r.commands,[]);self.assertEqual(self.v.calls,0)
    def test_step3_preflight_selects_real_holding_side(self):
        self.v.scanner_arm='right';s=self.h.start(step=3,single_step=True);self.h.pending.result(3)
        self.assertEqual(self.r.held_arm,'right');self.assertEqual(self.h.state()['status'],'running')
        self.verify(s['session_id'],3);self.assertEqual(self.h.state()['step'],3);self.assertEqual(len(self.r.commands),1)
    def test_cancel_preflight_prevents_later_motion(self):
        entered=threading.Event();release=threading.Event();original=self.v.identify_holdings
        def slow(obs):entered.set();release.wait(2);return original(obs)
        self.v.identify_holdings=slow;s=self.h.start(step=3,single_step=True);self.assertTrue(entered.wait(1))
        self.h.stop(s['session_id']);release.set();self.h.pending.result(3)
        self.assertEqual(self.r.commands,[]);self.assertEqual(self.h.state()['status'],'stopped')
    def test_check_only_calls_vlm_without_robot_commands(self):
        s=self.h.start(step=1,single_step=True,check_only=True);self.h.pending.result(3)
        self.assertEqual(self.r.commands,[]);self.assertEqual(self.r.stops,0);self.assertEqual(self.v.calls,1)
        self.assertEqual(self.h.state()['status'],'test_completed')
    def test_step7_requires_reference(self):
        with self.assertRaises(Conflict):self.h.start(step=7,single_step=True)
        self.h.record_reference();self.h.start(step=7,single_step=True);self.h.pending.result(3)
        self.assertEqual(len(self.r.commands),1)

if __name__=='__main__':unittest.main()
