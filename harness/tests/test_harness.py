import json
import threading
import time
import unittest
from unittest.mock import patch
from harness.tests.fakes import MockRobot, MockVision
from harness.adapters import OpenAIVision
from harness.config import settings, step_prompts, ROOT
from harness.engine import Harness, Conflict

class Tests(unittest.TestCase):
    def setUp(self):
        self.c=settings();self.r=MockRobot();self.v=MockVision();self.h=Harness(self.c,self.r,self.v)
    def tearDown(self):self.h.close()
    def start(self):self.sid=self.h.start()['session_id']
    def tick(self):
        step=self.h.state()['step'];self.h.observe(self.sid,step);self.h.pending.result(3)
    def to_scan(self):
        for _ in range(3):self.tick()
        self.assertEqual(self.h.state()['step'],4)
    def test_one_success_per_step_and_step4_frontend_event(self):
        self.start();self.assertEqual(self.v.calls,0)
        self.tick();self.assertEqual(self.h.state()['step'],2)
        self.tick();self.assertEqual(self.r.held_arm,'left')
        self.tick();self.assertEqual(self.r.held_arm,'right')
        calls=self.v.calls
        self.assertEqual(self.h.state()['phase'],'waiting_scan')
        with self.assertRaises(Conflict):self.h.observe(self.sid,4)
        self.assertEqual(self.v.calls,calls)
        self.h.scan(self.sid,'OUTB');self.assertEqual(self.h.state()['step'],6)
        self.assertEqual(self.v.calls,calls)
        self.assertEqual(self.h.state()['target'],'white');self.assertIn('white',self.r.commands[-1])
        self.tick();self.tick()
        self.assertEqual(self.h.state()['step7_phase'],'releasing')
        self.tick()
        self.assertEqual(self.h.state()['status'],'completed');self.assertEqual(len(self.r.commands),7)
    def test_unsuccessful_verification_never_advances(self):
        self.start()
        self.v.judge=lambda c,o,i:{'step':c['step'],'status':'IN_PROGRESS','evidence':'未抬起','next_action':'重试','held_objects':{'scanner':'unknown','bottle':None}}
        self.tick();self.assertEqual(self.h.state()['step'],1)
        self.assertEqual(self.h.state()['phase'],'awaiting_check');self.assertEqual(len(self.r.commands),1)
        self.h.retry(self.sid);self.assertEqual(len(self.r.commands),2)
        self.assertEqual(self.h.state()['phase'],'executing')
    def test_api_error_pauses(self):
        self.start()
        def unavailable(*args):raise RuntimeError('upstream connection failed')
        self.v.judge=unavailable
        self.tick();self.assertEqual(self.h.state()['status'],'paused')
        for _ in range(self.c['max_step_retries']):self.h.retry(self.sid);self.tick()
        with self.assertRaises(Conflict):self.h.retry(self.sid)
    def test_no_automatic_model_polling(self):
        with self.assertRaises(ValueError):self.h.start(auto=True)
        self.start();time.sleep(.02);self.assertEqual(self.v.calls,0)
        self.assertEqual(self.h.state()['step'],1)
    def test_scan_and_completion_session_step_validation(self):
        self.start()
        with self.assertRaises(Conflict):self.h.scan(self.sid,'OUTA')
        with self.assertRaises(Conflict):self.h.observe(self.sid,2)
        self.to_scan()
        with self.assertRaises(Conflict):self.h.scan('old','OUTA')
        self.h.scan(self.sid,'unknown')
        self.assertEqual(self.h.state()['target'],'black')
    def test_async_cancel_discards_reply(self):
        self.start();entered=threading.Event();release=threading.Event();orig=self.v.judge
        def slow(*args):entered.set();release.wait(2);return orig(*args)
        self.v.judge=slow;self.h.observe(self.sid,1);self.assertTrue(entered.wait(1))
        with self.assertRaises(Conflict):self.h.observe(self.sid,1)
        self.h.stop(self.sid);release.set();self.h.pending.result(3)
        self.assertEqual(self.h.state()['status'],'stopped');self.assertEqual(len(self.r.commands),1)
    def test_stale_camera_blocks_call(self):
        self.start();orig=self.r.observe
        def stale():
            o=orig();o['frames']['left']['captured_at']=time.time()-10;return o
        self.r.observe=stale;self.tick()
        self.assertEqual(self.h.state()['status'],'paused');self.assertEqual(self.v.calls,0)
    def test_reset_requires_joint_feedback(self):
        self.start();self.to_scan();self.h.scan(self.sid,'OUTA');self.tick()
        orig=self.r.observe
        def moved():
            o=orig();o['joints']['left_arm_joint1']=1;return o
        self.r.observe=moved;self.tick()
        self.assertEqual(self.h.state()['step'],7);self.assertEqual(self.h.state()['status'],'running')
    def test_prompt_pair_and_openai_mock_payload(self):
        pair=step_prompts(1,target='black');self.assertTrue(pair['execution_prompt'].strip())
        self.assertTrue(pair['verification_prompt'].strip())
        v=OpenAIVision(self.c);self.c['openai_key']='fake-test-key';o=self.r.observe()
        answer={'step':1,'status':'COMPLETED','evidence':'mock','next_action':'next','held_objects':{'scanner':'left','bottle':None}}
        response={'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(answer)}]}]}
        with patch('harness.adapters.http_json',return_value=response) as req:
            self.assertEqual(v.judge({'step':1},o,o),answer)
            req.assert_called_once();body=req.call_args.args[1]
            self.assertEqual(body['reasoning'],{'effort':'none'});self.assertNotIn('tools',body)
            self.assertEqual(body['input'][0]['content'][0]['text'],pair['verification_prompt'])
        for step in [1,2,3,4,6,7]:
            self.assertEqual(set(step_prompts(step,target='black',scanner_arm='left')),{'execution_prompt','verification_prompt'})
    def test_budget_and_live_capability_guard(self):
        self.r.capabilities=lambda:{'arm_lock':False,'joint_feedback':True}
        with self.assertRaises(Conflict):self.start()
        self.assertEqual(self.r.commands,[])
        self.r.capabilities=lambda:{'arm_lock':True,'joint_feedback':True}
        self.c['max_vision_calls']=1;self.start();self.tick()
        with self.assertRaises(Conflict):self.h.observe(self.sid)
        self.assertEqual(self.h.state()['status'],'paused');self.assertEqual(self.v.calls,1)

if __name__=='__main__':unittest.main()
