import unittest
from harness.engine import Harness
from harness.config import settings,step2_update_prompt
from harness.tests.fakes import MockRobot,MockVision

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.r=MockRobot();self.v=MockVision(scanner_arm='right');self.h=Harness(settings(),self.r,self.v);self.h.shutdown.set()
        self.h.data['held_objects']['scanner']='right'
        self.sid=self.h.start(step=2,single_step=True)['session_id'];self.h.pending.result(3)
        self.number=0
    def tearDown(self):self.h.close()
    def detect(self,status='IN_PROGRESS',action=None):
        self.number+=1
        advice=action if action is not None else f'重新抓住扫码枪，再调整第{self.number}次姿态'
        self.v.judge=lambda c,o,i:{'step':2,'status':status,'evidence':'检测','next_action':advice,
            'held_objects':{'scanner':'right' if status=='COMPLETED' else None,'bottle':None}}
        self.h.observe(self.sid,2,automatic=True);self.h.pending.result(3)
    def test_updates_only_on_second_and_fourth_checks_without_restart(self):
        stops=self.r.stops
        self.detect();self.assertEqual(self.r.updates,[])
        self.detect();self.assertEqual(len(self.r.updates),1)
        self.assertIn('第2次',self.r.updates[0]);self.assertIn('黑色T字形物体',self.r.updates[0]);self.assertNotIn('扫码枪',self.r.updates[0])
        self.assertIn("robot's left side",self.r.updates[0])
        self.detect();self.assertEqual(len(self.r.updates),1)
        self.detect();self.assertEqual(len(self.r.updates),2);self.assertIn('第4次',self.r.updates[1])
        self.assertEqual(len(self.r.commands),1);self.assertEqual(self.r.stops,stops)
    def test_success_on_second_check_does_not_send_new_prompt(self):
        self.detect();self.detect(status='COMPLETED')
        self.assertEqual(self.r.updates,[]);self.assertEqual(self.h.state()['status'],'test_completed')
    def test_failed_update_does_not_stop_current_vla(self):
        def reject(_):raise RuntimeError('native update rejected')
        self.r.update_instruction=reject;before=self.r.stops
        self.detect();self.detect()
        self.assertEqual(self.h.state()['status'],'running');self.assertEqual(self.r.stops,before)
        self.assertTrue(any(e['type']=='vla_update_error' for e in self.h.state()['events']))
    def test_empty_advice_is_skipped(self):
        self.detect();self.detect(action='')
        self.assertEqual(self.r.updates,[])
    def test_english_scanner_names_are_normalized(self):
        text,advice=step2_update_prompt('Pick up the barcode scanner. Hold the scanner horizontally.','left')
        self.assertNotIn('scanner',text.lower());self.assertIn('black T-shaped object',advice)
        self.assertIn("robot's right side",text)

if __name__=='__main__':unittest.main()
