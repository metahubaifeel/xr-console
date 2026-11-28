import time
import unittest
from harness.engine import Harness
from harness.config import settings
from harness.tests.fakes import MockRobot, MockVision

class AutoCheckTests(unittest.TestCase):
    def setUp(self):
        self.r=MockRobot();self.v=MockVision();self.h=Harness(settings(),self.r,self.v)
        self.h.shutdown.set() # drive time deterministically; no robot/model requests
        self.sid=self.h.start(step=1,single_step=True)['session_id'];self.h.pending.result(3)
    def tearDown(self):self.h.close()
    def test_interval_only_no_frame_counter(self):
        self.h.data['last_check_at']=time.monotonic()-2.4;self.h._auto_tick();self.assertEqual(self.v.calls,0)
        self.h.data['last_check_at']=time.monotonic()-2.6;self.h._auto_tick();self.h.pending.result(3)
        self.assertEqual(self.v.calls,1);self.assertEqual(self.h.state()['status'],'test_completed')
        self.assertEqual(self.h.state()['step'],1);self.assertEqual(len(self.r.commands),1)
    def test_in_progress_keeps_executing_and_success_stops(self):
        original=self.v.judge
        def incomplete(c,o,i):
            self.v.calls+=1
            return {'step':1,'status':'IN_PROGRESS','evidence':'执行中','next_action':'继续','held_objects':{'scanner':None,'bottle':None}}
        self.v.judge=incomplete
        stops=self.r.stops
        self.h.data['last_check_at']=time.monotonic()-2.6;self.h._auto_tick();self.h.pending.result(3)
        self.assertEqual(self.h.state()['phase'],'executing');self.assertEqual(self.r.stops,stops)
        self.h._auto_tick();self.assertEqual(self.v.calls,1)
        self.v.judge=original;self.h.data['last_check_at']=time.monotonic()-2.6
        self.h._auto_tick();self.h.pending.result(3)
        self.assertEqual(self.h.state()['status'],'test_completed');self.assertGreater(self.r.stops,stops)
    def test_legacy_failed_is_in_progress_without_stopping_vla(self):
        self.v.judge=lambda c,o,i:{'step':1,'status':'FAILED','evidence':'本次未抓住','next_action':'继续尝试','held_objects':{'scanner':None,'bottle':None}}
        before=self.r.stops
        self.h.data['last_check_at']=time.monotonic()-2.6
        self.h._auto_tick();self.h.pending.result(3)
        state=self.h.state()
        self.assertEqual(state['judgment']['status'],'IN_PROGRESS')
        self.assertEqual(state['status'],'running')
        self.assertEqual(state['phase'],'executing')
        self.assertEqual(self.r.stops,before)
    def test_output_schema_has_only_three_statuses(self):
        from harness.adapters import OpenAIVision
        observation=self.r.observe()
        schema=OpenAIVision(settings()).payload({'step':1},observation,observation)['text']['format']['schema']
        self.assertEqual(set(schema['properties']['status']['enum']),{'COMPLETED','IN_PROGRESS','UNCERTAIN'})
    def test_step4_has_no_timer_detection(self):
        self.h.data.update(step=4,phase='waiting_scan',last_check_at=0)
        self.h._auto_tick();self.assertEqual(self.v.calls,0)

if __name__=='__main__':unittest.main()
