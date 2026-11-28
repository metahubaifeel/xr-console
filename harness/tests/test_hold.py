import ast
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from harness.robot_client import hold_runtime as rt
from harness.robot_client.patch_client import patch as patch_source
from harness.engine import Harness
from harness.config import settings
from harness.tests.fakes import MockRobot, MockVision
from harness.adapters import validate_judgment


def snapshot():
    return {'state': {'follow1_joints': np.arange(7,dtype=float)/10,
                      'follow2_joints': np.arange(7,dtype=float)/20,
                      'follow1_pos': np.array([.1,.2,.3,.1,.2,.3,.04]),
                      'follow2_pos': np.array([.3,.2,.1,.3,.2,.1,.06])}}

class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'hold_state.json'
    def tearDown(self):self.tmp.cleanup()
    def policy(self,arms):return rt.latch(rt.configure(self.path,'session',arms),snapshot()['state'],'end_pose')
    def test_left_and_right_only_whole_chunk(self):
        left=np.random.default_rng(1).random((32,7));right=left+2
        for arm in ['left','right']:
            p=self.policy([arm]);a,b=rt.overlay(left,right,p,'end_pose')
            held=a if arm=='left' else b;other=b if arm=='left' else a;original=right if arm=='left' else left
            self.assertIs(other,original)
            np.testing.assert_array_equal(held,np.tile(snapshot()['state']['follow1_pos' if arm=='left' else 'follow2_pos'],(32,1)))
        np.testing.assert_array_equal(left,np.random.default_rng(1).random((32,7)))
    def test_joint_space_and_both_arms(self):
        d=self.policy(['left','right'])
        for cols in [7,8]:
            a,b=rt.overlay(np.zeros((8,cols)),np.ones((8,cols)),d,'joints')
            np.testing.assert_array_equal(a[:,:7],np.tile(snapshot()['state']['follow1_joints'],(8,1)))
            np.testing.assert_array_equal(b[:,:7],np.tile(snapshot()['state']['follow2_joints'],(8,1)))
            if cols==8:self.assertTrue(np.all(a[:,-1]==.04))
    def test_quaternion_dispatch_layout(self):
        from math import isclose
        a,b=rt.overlay(np.ones((64,8)),np.ones((64,8))*2,self.policy(['left']),'end_pose','dispatch')
        self.assertTrue(isclose(np.linalg.norm(a[0,3:7]),1))
        np.testing.assert_array_equal(a,np.tile(a[0],(64,1)))
        self.assertTrue(np.all(b==2));self.assertEqual(a[0,-1],.04)
    def test_latch_does_not_follow_feedback_drift(self):
        d=self.policy(['left']);old=copy.deepcopy(d['targets']['left'])
        state=snapshot()['state'];state['follow1_pos']+=1
        rt.latch(d,state,'end_pose');self.assertEqual(d['targets']['left'],old)
    def test_disabled_client_is_noop_and_enabled_missing_file_fails(self):
        owner=type('Owner',(),{})();a=np.zeros((3,7));b=a+1
        with patch.dict(os.environ,{},clear=True):
            x,y=rt.apply_client_hold(owner,a,b);self.assertIs(x,a);self.assertIs(y,b)
        with patch.dict(os.environ,{'XR_HARNESS_HOLD_FILE':str(self.path),'XR_HARNESS_SESSION_ID':'session'}):
            with self.assertRaises(FileNotFoundError):rt.apply_client_hold(owner,a,b)
    def test_snapshot_persistence_session_and_revision_guards(self):
        rt.configure(self.path,'session',['left'])
        owner=type('Owner',(),{'control_mode':'end_pose','_collect_state_snapshot':lambda s:snapshot()})()
        with patch.dict(os.environ,{'XR_HARNESS_HOLD_FILE':str(self.path),'XR_HARNESS_SESSION_ID':'session'}):
            rt.apply_client_hold(owner,np.zeros((2,7)),np.ones((2,7)))
            d=rt.read(self.path);self.assertEqual(d['state'],'latched');self.assertIn('joint_positions',d['targets']['left'])
            old=copy.deepcopy(d['targets'])
            rt.apply_client_hold(owner,np.zeros((2,8)),np.ones((2,8)),layout='dispatch')
            self.assertEqual(rt.read(self.path)['targets'],old)
            d['revision']='tampered';rt.atomic_write(self.path,d)
            with self.assertRaises(RuntimeError):rt.apply_client_hold(owner,np.zeros((2,7)),np.zeros((2,7)))
        with patch.dict(os.environ,{'XR_HARNESS_HOLD_FILE':str(self.path),'XR_HARNESS_SESSION_ID':'other'}):
            with self.assertRaises(ValueError):rt.apply_client_hold(owner,np.zeros((2,7)),np.zeros((2,7)))
    def test_invalid_shapes_nan_and_target_fail_closed(self):
        d=self.policy(['left'])
        for a in [np.zeros((2,6)),np.full((2,7),np.nan),np.zeros((0,7))]:
            with self.assertRaises(ValueError):rt.overlay(a,np.zeros((2,7)),d,'end_pose')
        d['targets']['left']['gripper']=float('nan')
        with self.assertRaises(ValueError):rt.overlay(np.zeros((2,7)),np.zeros((2,7)),d,'end_pose')
    def test_unknown_holding_hand_cannot_advance(self):
        c=settings();r=MockRobot();v=MockVision();h=Harness(c,r,v)
        try:
            sid=h.start()['session_id']
            v.judge=lambda ctx,o,i:{'step':ctx['step'],'status':'COMPLETED','evidence':'看不清','next_action':'观察','held_objects':{'scanner':'unknown','bottle':None}}
            h.observe(sid,1);h.pending.result(3)
            self.assertEqual(h.state()['step'],1);self.assertEqual(len(r.commands),1)
        finally:h.close()
    def test_vlm_right_hand_drives_policy_not_fixed_configuration(self):
        c=settings();r=MockRobot();v=MockVision(scanner_arm='right');h=Harness(c,r,v)
        try:
            sid=h.start()['session_id']
            for expected in [None,'right','left']:
                h.observe(sid,h.state()['step']);h.pending.result(3)
                self.assertEqual(r.held_arm,expected)
            h.scan(sid,'OUTA')
            self.assertEqual(r.held_arm,'right') # placing bottle: hold scanner
            h.observe(sid,6);h.pending.result(3);self.assertIsNone(r.held_arm)
        finally:h.close()

if __name__=='__main__':unittest.main()
