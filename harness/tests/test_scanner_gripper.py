import copy,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from harness.robot_client import hold_runtime as rt
from harness.config import settings
from harness.engine import Harness
from harness.tests.fakes import MockRobot,MockVision

class GripperRuntimeTests(unittest.TestCase):
    def test_only_gripper_column_is_fixed(self):
        for mode,layout,cols in [('end_pose','decoded',7),('end_pose','dispatch',8),('joints','decoded',7)]:
            a=np.arange(6*cols,dtype=float).reshape(6,cols);b=a+100
            d={'arms':[],'grippers':{'right':{'target':.15}},'joint_dimensions':{'right':list(range(6))}}
            left,right=rt.overlay_grippers(a,b,d,mode,layout)
            self.assertIs(left,a);np.testing.assert_array_equal(right[:,:-1],b[:,:-1]);self.assertTrue(np.all(right[:,-1]==.15))
    def test_policy_preserves_original_target_across_sessions_and_arm_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'hold_state.json';rt.configure(p,'one',[])
            rt.set_gripper_policy(p,'one','right',.15,'vlm_grasp')
            rt.set_gripper_policy(p,'one','right',.7,'vlm_grasp')
            self.assertEqual(rt.read(p)['grippers']['right']['target'],.15)
            rt.configure(p,'two',['left'])
            self.assertEqual(rt.read(p)['grippers']['right']['target'],.15)
            with self.assertRaises(ValueError):rt.set_gripper_policy(p,'two',None,reason='unknown')
            rt.set_gripper_policy(p,'two',None,reason='vlm_drop');self.assertEqual(rt.read(p)['grippers'],{})
    def test_service_process_binding_and_six_joint_sdk_feedback(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            p=Path(tmp)/'hold_state.json';rt.configure(p,'session',[]);rt.prepare_session(p,'session','original')
            rt.set_gripper_policy(p,'session','right',.15,'vlm_grasp')
            state={'follow1_joints':np.arange(6,dtype=float),'follow2_joints':np.arange(6,dtype=float),
                   'follow1_pos':np.array([.1,.2,.3,0,0,0,.04]),'follow2_pos':np.array([.1,.2,.3,0,0,0,.06])}
            owner=type('Owner',(),{'instruction':'original','control_mode':'end_pose','_collect_state_snapshot':lambda s:{'state':state}})()
            a=np.ones((8,7));b=a+1
            with patch.object(rt,'DEFAULT_PATH',str(p)),patch.object(rt,'session_launcher_running',return_value=True):
                left,right=rt.apply_client_hold(owner,a,b)
                self.assertIs(left,a);self.assertTrue(np.all(right[:,-1]==.15))
                owner.instruction='hot updated instruction'
                _,right=rt.apply_client_hold(owner,a,b)
                self.assertTrue(np.all(right[:,-1]==.15))
                ordinary=type('Owner',(),{'instruction':'unrelated'})()
                left,right=rt.apply_client_hold(ordinary,a,b);self.assertIs(left,a);self.assertIs(right,b)

class GripperFlowTests(unittest.TestCase):
    def setUp(self):
        self.r=MockRobot();self.v=MockVision(scanner_arm='right');self.h=Harness(settings(),self.r,self.v);self.h.shutdown.set()
    def tearDown(self):self.h.close()
    def result(self,sid,step,scanner,status='IN_PROGRESS',drop=None,placed=None):
        self.v.judge=lambda c,o,i:{'step':step,'status':status,'evidence':'observed','next_action':'continue',
            'held_objects':{'scanner':scanner,'bottle':None},'scanner_dropped':drop,'scanner_placed':placed}
        self.h.observe(sid,step,automatic=True);self.h.pending.result(3)
    def test_only_explicit_drop_unlocks(self):
        sid=self.h.start(step=1,single_step=True)['session_id'];self.h.pending.result(3)
        self.result(sid,1,'right');target=self.r.grippers['right']['target']
        self.result(sid,1,'unknown');self.assertEqual(self.r.grippers['right']['target'],target)
        self.result(sid,1,None);self.assertIn('right',self.r.grippers)
        self.result(sid,1,None,drop=True);self.assertEqual(self.r.grippers,{})
        self.assertEqual(self.h.state()['status'],'running')
    def test_final_release_waits_for_confirmed_placement(self):
        self.h.record_reference();s=self.h.start(step=7,single_step=True);self.h.pending.result(3);sid=s['session_id']
        self.assertIn('right',self.r.grippers)
        self.result(sid,7,'right',status='COMPLETED',placed=False)
        self.assertIn('right',self.r.grippers);self.assertEqual(self.r.updates,[])
        self.result(sid,7,'right',status='COMPLETED',placed=True)
        self.assertEqual(self.r.grippers,{});self.assertEqual(self.h.state()['step7_phase'],'releasing')
        self.assertEqual(len(self.r.updates),1)
        self.result(sid,7,None,status='COMPLETED',placed=True)
        self.assertEqual(self.h.state()['status'],'test_completed')
    def test_check_only_step7_never_unlocks_or_sends_commands(self):
        self.h.record_reference();self.r.grippers['right']={'target':.15}
        s=self.h.start(step=7,single_step=True,check_only=True);self.h.pending.result(3)
        self.assertEqual(self.r.commands,[]);self.assertEqual(self.r.updates,[]);self.assertIn('right',self.r.grippers)

if __name__=='__main__':unittest.main()
