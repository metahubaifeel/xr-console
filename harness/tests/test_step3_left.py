import unittest
from harness.engine import Harness,Conflict
from harness.config import settings,step3_left_test_prompts
from harness.adapters import OpenAIVision
from harness.tests.fakes import MockRobot,MockVision

class LeftBottleTests(unittest.TestCase):
    def setUp(self):
        self.r=MockRobot();self.v=MockVision();self.h=Harness(settings(),self.r,self.v);self.h.shutdown.set()
    def tearDown(self):self.h.close()
    def start(self):
        sid=self.h.start(step=3,single_step=True,test_profile='step3_left')['session_id'];self.h.pending.result(3);return sid
    def test_forces_right_arm_and_does_not_fake_scanner_hand(self):
        self.start();self.assertEqual(self.r.held_arm,'right');self.assertEqual(self.v.calls,0)
        self.assertIsNone(self.h.state()['held_objects']['scanner'])
        self.assertEqual(self.r.commands,[step3_left_test_prompts()['execution_prompt']])
    def test_left_grasp_completes_without_step4(self):
        sid=self.start()
        self.v.judge=lambda c,o,i:{'step':3,'status':'COMPLETED','evidence':'左手已抬起瓶子','next_action':'保持',
            'held_objects':{'scanner':None,'bottle':'left'},'scanner_dropped':False,'scanner_placed':False}
        self.h.observe(sid,3,automatic=True);self.h.pending.result(3)
        self.assertEqual(self.h.state()['status'],'test_completed');self.assertEqual(self.h.state()['step'],3)
        self.assertEqual(len(self.r.commands),1)
    def test_right_hand_bottle_is_not_success(self):
        sid=self.start()
        self.v.judge=lambda c,o,i:{'step':3,'status':'COMPLETED','evidence':'右手抓瓶','next_action':'用左手',
            'held_objects':{'scanner':None,'bottle':'right'}}
        self.h.observe(sid,3,automatic=True);self.h.pending.result(3)
        self.assertEqual(self.h.state()['status'],'running')
    def test_conflicting_left_gripper_lock_is_not_cleared(self):
        self.r.grippers['left']={'target':.15}
        with self.assertRaises(Conflict):self.start()
        self.assertIn('left',self.r.grippers);self.assertEqual(self.r.commands,[])
    def test_detector_uses_test_profile_prompt(self):
        obs=self.r.observe()
        payload=OpenAIVision(settings()).payload({'step':3,'test_profile':'step3_left'},obs,obs)
        self.assertEqual(payload['input'][0]['content'][0]['text'],step3_left_test_prompts()['verification_prompt'])

if __name__=='__main__':unittest.main()
