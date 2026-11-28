import unittest
import json
from pathlib import Path
from unittest.mock import patch
from harness.config import settings,step_prompts
from harness.engine import Harness
from harness.adapters import OpenAIVision
from harness.tests.fakes import MockRobot,MockVision

class Step2Tests(unittest.TestCase):
    def setUp(self):
        original=Path.read_text
        def template(path,*args,**kwargs):
            if path.name=='step_2.json':
                return json.dumps({'execution_prompt':"If dropped, use the {scanner_arm} hand to grasp its handle again. Face the robot's {opening_direction} side.", 'verification_prompt':'机器人{opening_direction_zh}方'})
            return original(path,*args,**kwargs)
        self.patch=patch.object(Path,'read_text',template);self.patch.start();self.addCleanup(self.patch.stop)
    def test_templates_share_opposite_direction(self):
        for arm,direction,zh in [('right','left','左'),('left','right','右')]:
            pair=step_prompts(2,scanner_arm=arm)
            self.assertIn("robot's "+direction+" side",pair['execution_prompt'])
            self.assertIn('机器人'+zh+'方',pair['verification_prompt'])
            obs=MockRobot().observe()
            payload=OpenAIVision(settings()).payload({'step':2,'held_objects':{'scanner':arm}},obs,obs)
            self.assertEqual(payload['input'][0]['content'][0]['text'],pair['verification_prompt'])
    def test_full_flow_uses_step1_result(self):
        for arm,direction in [('right','left'),('left','right')]:
            r=MockRobot();v=MockVision(scanner_arm=arm);h=Harness(settings(),r,v);h.shutdown.set()
            try:
                sid=h.start()['session_id'];h.observe(sid,1);h.pending.result(3)
                self.assertEqual(h.state()['step'],2)
                self.assertIn("robot's "+direction+" side",r.commands[-1])
            finally:h.close()
    def test_single_step_reuses_previous_holding_hand(self):
        r=MockRobot();v=MockVision(scanner_arm='right');h=Harness(settings(),r,v);h.shutdown.set()
        try:
            h.data['held_objects']['scanner']='right'
            h.start(step=2,single_step=True);h.pending.result(3)
            self.assertEqual(v.calls,0)
            self.assertIn("robot's left side",r.commands[-1])
        finally:h.close()
    def test_unknown_hand_needs_recognition_before_motion(self):
        r=MockRobot();v=MockVision(scanner_arm='right');h=Harness(settings(),r,v);h.shutdown.set()
        try:
            h.start(step=2,single_step=True);h.pending.result(3)
            self.assertEqual(v.calls,1)
            self.assertIn("robot's left side",r.commands[-1])
            with self.assertRaises(ValueError):step_prompts(2,scanner_arm='unknown')
        finally:h.close()

    def test_drop_does_not_stop_and_next_check_uses_latest_observation(self):
        r=MockRobot();v=MockVision(scanner_arm='right');h=Harness(settings(),r,v);h.shutdown.set()
        contexts=[]
        def dropped(context,obs,initial):
            contexts.append(context)
            return {'step':2,'status':'IN_PROGRESS','evidence':'枪在桌上','next_action':'重抓','held_objects':{'scanner':None,'bottle':None}}
        try:
            h.data['held_objects']['scanner']='right'
            sid=h.start(step=2,single_step=True)['session_id'];h.pending.result(3)
            self.assertIn('use the right hand to grasp its handle again',r.commands[-1])
            before=r.stops;v.judge=dropped
            for _ in range(2):
                h.observe(sid,2,automatic=True);h.pending.result(3)
            self.assertEqual(r.stops,before);self.assertEqual(h.state()['status'],'running')
            self.assertIsNone(contexts[-1]['held_objects']['scanner'])
            self.assertEqual(contexts[-1]['step2_reference_hand'],'right')
            payload=OpenAIVision(settings()).payload(contexts[-1],r.observe(),r.observe())
            self.assertIn('机器人左方',payload['input'][0]['content'][0]['text'])
            self.assertTrue(any(e['type']=='auto_check_started' for e in h.state()['events']))
            self.assertFalse(any(e['type']=='execution_ended' for e in h.state()['events']))
        finally:h.close()

if __name__=='__main__':unittest.main()
