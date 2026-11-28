import math
import threading
import unittest
from harness.robot_client.motor_control import plan_move,interpolation
from harness.motors import MotorJobs
from harness.engine import Harness,Conflict
from harness.config import settings
from harness.tests.fakes import MockRobot,MockVision


def motors():
    d={f'{s}_arm_joint{i}':{'position_rad':0.,'limits_rad':[-math.pi,math.pi],'kind':'arm'} for s in ['left','right'] for i in range(1,7)}
    d.update({f'{s}_arm_gripper':{'position_rad':.1,'limits_rad':[-.1,4.6],'kind':'gripper'} for s in ['left','right']})
    d.update({f'head_{axis}_joint':{'position_rad':0.,'limits_rad':[-1.,1.],'kind':'head'} for axis in ['pitch','yaw']})
    return d

class MotorPlanningTests(unittest.TestCase):
    def test_absolute_degrees_and_other_motors_unchanged(self):
        p=plan_move(motors(),{'motor':'left_arm_joint1','degrees':30})
        self.assertAlmostEqual(p['targets_rad']['left_arm_joint1'],math.pi/6)
        for key,value in p['baseline_rad'].items():
            if key!='left_arm_joint1':self.assertEqual(p['targets_rad'][key],value)
    def test_relative_rotation(self):
        m=motors();m['right_arm_joint2']['position_rad']=math.radians(40)
        p=plan_move(m,{'motor':'right_arm_joint2','degrees':-15,'relative':True})
        self.assertAlmostEqual(p['target_deg'],25)
    def test_gripper_degrees_are_sdk_motor_angle(self):
        p=plan_move(motors(),{'motor':'right_arm_gripper','degrees':90,'speed_deg_s':10})
        self.assertAlmostEqual(p['targets_rad']['right_arm_gripper'],math.pi/2)
    def test_invalid_numbers_limits_and_unknown_motor(self):
        for value in [True,float('nan'),float('inf'),999]:
            with self.assertRaises(ValueError):plan_move(motors(),{'motor':'left_arm_joint1','degrees':value})
        with self.assertRaises(ValueError):plan_move(motors(),{'motor':'lift','degrees':1})
    def test_quintic_endpoints_and_speed_limit(self):
        p=plan_move(motors(),{'motor':'left_arm_joint1','degrees':45,'speed_deg_s':5})
        self.assertEqual(interpolation(p,0),p['baseline_rad']);self.assertEqual(interpolation(p,1),p['targets_rad'])
        values=[math.degrees(interpolation(p,i/1000)['left_arm_joint1']) for i in range(1001)]
        peak=max((b-a)/(p['duration_s']/1000) for a,b in zip(values,values[1:]))
        self.assertLessEqual(peak,5.001)
    def test_grasp_lock_is_enforced(self):
        locks={'right':{'target':.1}}
        with self.assertRaises(ValueError):plan_move(motors(),{'motor':'right_arm_gripper','degrees':45},locks)
        p=plan_move(motors(),{'motor':'left_arm_joint1','degrees':5},locks)
        self.assertEqual(p['targets_rad']['right_arm_gripper'],.1)

class JobTests(unittest.TestCase):
    def test_active_motor_blocks_vla_workflow_and_can_cancel(self):
        r=MockRobot();entered=threading.Event();release=threading.Event();calls=[]
        def motor(op,data):
            calls.append(op)
            if op=='move':entered.set();release.wait(2);return {'cancelled':True,'reached':False}
            if op=='cancel':release.set();return {'requested':True}
        r.motor=motor;h=Harness(settings(),r,MockVision());h.shutdown.set()
        try:
            job=h.move_motor({'motor':'left_arm_joint1','degrees':5});self.assertTrue(entered.wait(1))
            with self.assertRaises(Conflict):h.start()
            h.motor_jobs.cancel(job['command_id']);h.motor_jobs.future.result(3)
            self.assertEqual(h.motor_jobs.state()['status'],'cancelled');self.assertEqual(calls,['move','cancel'])
        finally:h.close()

if __name__=='__main__':unittest.main()
