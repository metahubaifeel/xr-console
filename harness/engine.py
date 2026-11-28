import copy
import json
from pathlib import Path
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from .adapters import validate_judgment
from .motors import MotorJobs
from .config import ROOT, prompt, step_prompts, step2_update_prompt, step7_release_prompts, step3_left_test_prompts


class Conflict(ValueError):
    pass


class Harness:
    def __init__(self, config, robot, vision):
        self.c, self.robot, self.vision = config, robot, vision
        self.motor_jobs=MotorJobs(robot)
        self.lock = threading.RLock()
        self.actions = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vision-monitor')
        self.pending = None
        self.epoch = 0
        self.reference_path = ROOT.parent / '.harness_state' / 'reference.json'
        self.reference = None
        if robot.mode == 'live' and self.reference_path.exists():
            try:
                self.reference = json.loads(self.reference_path.read_text())
            except (OSError, ValueError):
                pass
        self.initial = None
        self.observation = None
        self.last_digests = {}
        self.step_started = 0
        self.session_calls = 0
        self.shutdown = threading.Event()
        self.data = {'session_id': None, 'status': 'idle', 'step': 0, 'events': [], 'judgment': None,
                     'confirmations': 0, 'scan': None, 'target': None, 'scan_retries': 0, 'step_retries': 0,
                     'auto_monitor': False, 'last_error': None, 'held_objects': {'scanner': None, 'bottle': None}, 'hold_arm': None, 'single_step': False, 'raw_judgment': None, 'vision_response': None, 'preflight': None, 'test_result': None}
        self.last_result_path = ROOT.parent / '.harness_state' / 'last_result.json'
        if robot.mode == 'live' and self.last_result_path.exists():
            try:
                previous = json.loads(self.last_result_path.read_text())
                if previous.get('status') == 'test_completed':
                    self.data.update(previous)
                    self.data['session_id'] = None
                    self.session_calls = previous.get('vision_calls', 0)
            except (OSError, ValueError):
                pass
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)
        self.scheduler.start()

    def event(self, kind, **values):
        self.data['events'].append({'ts': time.time(), 'type': kind, **values})
        self.data['events'] = self.data['events'][-100:]

    def state(self):
        with self.lock:
            return {**copy.deepcopy(self.data), 'mode': self.robot.mode,
                    'vision_calls': self.session_calls, 'vision_busy': bool(self.pending and not self.pending.done())}

    def frames(self):
        with self.lock:
            return copy.deepcopy(self.observation)

    def _validate_observation(self, obs):
        if not isinstance(obs.get('joints'), dict) or not obs['joints']:
            raise ValueError('joint feedback is missing')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in obs['joints'].values()):
            raise ValueError('invalid joint feedback')
        for name in ['head', 'left', 'right']:
            f = obs['frames'][name]
            age = time.time() - f['captured_at']
            # Robot capture timestamps and Mac wall time can differ slightly.
            if (not f.get('url', '').startswith('data:image/')
                    or not -self.c.get('max_clock_skew', 1.0) <= age <= self.c['max_frame_age']):
                raise ValueError('camera is missing or stale: ' + name)

    def _require(self, sid, statuses=('running',)):
        if sid != self.data['session_id'] or self.data['status'] not in statuses:
            raise Conflict('session is stale or not in the required state')

    def move_motor(self,payload):
        with self.actions,self.lock:
            self._idle_required()
            return self.motor_jobs.submit(payload)

    def record_reference(self):
        with self.actions, self.lock:
            self._idle_required()
            observation = self.robot.observe()
            self._validate_observation(observation)
            self._save_reference(observation)
            return {'recorded': True, 'captured_at': observation['captured_at'], 'joint_count': len(observation['joints'])}

    def _save_reference(self, observation):
        self.reference = copy.deepcopy(observation)
        if self.robot.mode == 'live':
            from .robot_client.hold_runtime import atomic_write
            self.reference_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write(self.reference_path, observation)

    def _idle_required(self):
        if self.motor_jobs.busy():
            raise Conflict('manual motor command is active')
        if self.data['status'] in ('running', 'starting', 'paused') or (self.pending and not self.pending.done()):
            raise Conflict('stop the active session before starting another test')

    def start(self, auto=False, step=1, single_step=False, target=None, code=None, check_only=False, test_profile=None):
        if test_profile is not None and (test_profile!='step3_left' or step!=3 or not single_step or check_only):
            raise ValueError('step3_left profile is restricted to a Step3 single test')
        if auto is not False:
            raise ValueError('automatic VLM polling is disabled; report step completion explicitly')
        if type(step) is not int or step not in range(1,8) or type(single_step) is not bool:
            raise ValueError('invalid step or single_step')
        if not single_step and step != 1:
            raise ValueError('full workflow starts at Step1')
        if check_only and step in (4,5):
            raise ValueError('Step4/Step5 do not use VLM verification')
        if target is not None and target not in set(self.c['code_targets'].values()):
            raise ValueError('invalid target color')
        if step == 5 and (not isinstance(code,str) or not code.strip() or len(code)>128):
            raise ValueError('Step5 requires a barcode value')
        with self.actions, self.lock:
            self._idle_required()
            prior_scanner = self.data['held_objects'].get('scanner') if step==2 else None
            caps = self.robot.capabilities()
            if caps.get('arm_lock') is not True or caps.get('joint_feedback') is not True:
                raise Conflict('robot-client trajectory overlay and joint feedback are required')
            if test_profile=='step3_left':
                locked=self.robot.lock_state().get('grippers',{})
                if 'left' in locked:
                    raise Conflict('左夹爪仍在抓枪保持，请先完成放枪或确认脱手后再测试左手抓瓶')
            observation = self.robot.observe()
            self._validate_observation(observation)
            if step == 7 and self.reference is None:
                raise Conflict('record the initial pose before testing Step7')
            if step == 1 and not check_only:
                self._save_reference(observation)
            self.initial = copy.deepcopy(self.reference) if step == 7 else observation
            self.observation = observation
            self.epoch += 1
            self.last_digests = {}
            self.session_calls = 0
            self.step_started = time.monotonic()
            self.data.update(session_id=uuid.uuid4().hex, status='running', step=step, events=[],
                             judgment=None, confirmations=0, scan=None, target=target or ('red' if step==6 else None),
                             scan_retries=0, step_retries=0, auto_monitor=False, last_error=None,
                             held_objects={'scanner':prior_scanner,'bottle':None}, hold_arm=None, single_step=single_step,
                             raw_judgment=None, vision_response=None, preflight=None, test_result=None,
                             check_only=check_only, executed=False,test_profile=test_profile,scanner_gripper_lock=None,step7_phase='placing')
            self.event('started', mode=self.robot.mode, single_step=single_step, step=step, check_only=check_only)
            if step == 5:
                self.data['scan']={'code':code.strip().upper(),'source':'frontend','received_at':time.time()}
                self.data['target']=self.c['code_targets'].get(code.strip().upper(),'black')
                self.data.update(status='test_completed',phase='finished',test_result={'success':True,'target':self.data['target']})
                self.event('mapped',code=code.strip().upper(),target=self.data['target'])
            elif step==2 and prior_scanner not in ('left','right'):
                self.data.update(status='starting',phase='identifying_hands')
                self.pending=self.pool.submit(self._preflight_start,self.epoch,step,observation)
            elif check_only:
                self.data['phase']='checking'
                self.pending=self.pool.submit(self._observe,self.epoch,step)
            elif test_profile=='step3_left':
                self.data.update(status='starting',phase='starting_client')
                self.pending=self.pool.submit(self._dispatch_single_start,self.epoch,step)
            elif single_step and step in (3,4,6,7):
                self.data.update(status='starting',phase='identifying_hands')
                self.pending=self.pool.submit(self._preflight_start,self.epoch,step,observation)
            elif single_step:
                self.data.update(status='starting',phase='starting_client')
                self.pending=self.pool.submit(self._dispatch_single_start,self.epoch,step)
            else:
                try:
                    self._enter(step)
                except Exception:
                    self._fail('VLA dispatch failed')
                    raise
            return self.state()

    def _dispatch_single_start(self, epoch, step):
        with self.actions,self.lock:
            if epoch != self.epoch or self.data['status'] != 'starting':
                return
            try:
                self._enter(step)
                self.data['status']='running'
            except Exception as exc:
                self._fail('VLA start failed: '+type(exc).__name__+': '+str(exc)[:200])

    def _preflight_start(self, epoch, step, observation):
        try:
            result = self.vision.identify_holdings(observation)
            with self.actions, self.lock:
                if epoch != self.epoch or self.data['status'] != 'starting':
                    return
                self.session_calls += 1
                self.data['preflight'] = result
                self.data['held_objects'] = {k:result[k] for k in ('scanner','bottle')}
                required = ['scanner'] if step in (2,3,7) else ['scanner','bottle']
                if any(result[k] not in ('left','right') for k in required):
                    raise ValueError('required holding hands could not be identified')
                self.event('holding_recognition', **result)
                if self.data.get('check_only'):
                    self.data.update(status='running',phase='checking')
                    self.pending=self.pool.submit(self._observe,epoch,step)
                else:
                    self._enter(step)
                    self.data['status']='running'
        except Exception as exc:
            with self.actions,self.lock:
                if epoch == self.epoch and self.data['status']=='starting':
                    self._fail('holding recognition/start failed: '+type(exc).__name__+': '+str(exc)[:200])

    def _finish_single(self, success):
        if self.data.get('executed'):
            self.robot.stop()
        self.data.update(status='test_completed',phase='finished',
                         test_result={'success':success,'step':self.data['step']})
        self.event('single_test_finished', step=self.data['step'], success=success)
        if self.robot.mode == 'live':
            from .robot_client.hold_runtime import atomic_write
            self.last_result_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            atomic_write(self.last_result_path,self.state())

    def _enter(self, step, retry=False):
        # Called with actions + state locks. No model call or model text controls motion.
        self.robot.stop()
        obj = 'scanner' if step in (3, 6) else 'bottle' if step == 4 else None
        arm = self.data['held_objects'].get(obj) if obj else None
        if self.data.get('test_profile')=='step3_left':
            obj,arm='right_arm','right'
        if obj and arm not in ('left', 'right'):
            raise ValueError('vision did not identify the hand holding ' + obj)
        self.robot.protect(arm, session_id=self.data['session_id'])
        locks=getattr(self.robot,'hold_status',{}).get('grippers',{})
        self.data['scanner_gripper_lock']=next(({'arm':side,**value} for side,value in locks.items()),None)
        self.data['hold_arm'] = arm
        self.event('hold_policy', step=step, arm=arm, object=obj)
        self.data.update(step=step, confirmations=0, judgment=None,step2_checks=0,vla_prompt_updates=0)
        self.last_digests = {}
        self.step_started = time.monotonic()
        self.data['phase'] = 'waiting_scan' if step == 4 else 'executing'
        if not self.data.get('check_only'):
            self._sync_scanner_gripper({'held_objects':self.data['held_objects']},self.observation)
        if step==7:self.data['step7_phase']='placing'
        instruction = step_prompts(step, target=self.data['target'] or 'black', scanner_arm=self.data['held_objects'].get('scanner'))['execution_prompt'].strip()
        if self.data.get('test_profile')=='step3_left':
            instruction=step3_left_test_prompts()['execution_prompt'].strip()
        if retry:
            instruction = prompt('retry.txt', instruction=instruction).strip()
        result = self.robot.execute(instruction)
        if result.get('accepted') is not True:
            raise RuntimeError('VLA did not accept command')
        self.data.update(last_check_at=time.monotonic(),auto_check=self.c.get('auto_check',True))
        self.data['vla_instruction'] = instruction
        self.data['executed'] = True
        self.event('vla_command', step=step, instruction=instruction, retry=retry)

    def _sync_scanner_gripper(self, judgment, observation):
        if self.data.get('check_only'):
            return
        lock=self.data.get('scanner_gripper_lock')
        if judgment.get('scanner_dropped') is True:
            if judgment['held_objects'].get('scanner') in ('left','right'):
                self.event('gripper_drop_ignored',reason='holding and drop evidence conflict')
                return
            if lock:
                self.robot.set_gripper_lock(None,reason='vlm_drop')
                self.data['scanner_gripper_lock']=None
                self.event('scanner_gripper_unlocked',reason='vlm_drop')
            return
        if lock or self.data.get('step7_phase')=='releasing':
            return
        arm=judgment['held_objects'].get('scanner')
        if arm not in ('left','right'):
            return
        target=observation['joints'].get(arm+'_arm_gripper')
        if type(target) not in (int,float) or not math.isfinite(target):
            raise ValueError('scanner gripper feedback missing')
        result=self.robot.set_gripper_lock(arm,target,reason='vlm_grasp')
        if not result.get('accepted'):
            raise RuntimeError('scanner gripper lock rejected')
        self.data['scanner_gripper_lock']={'arm':arm,'target':target}
        self.event('scanner_gripper_locked',arm=arm,target=target)

    def _start_final_release(self, automatic):
        self.robot.set_gripper_lock(None,reason='final_release')
        self.data['scanner_gripper_lock']=None
        instruction=step7_release_prompts()['execution_prompt'].strip()
        result=self.robot.update_instruction(instruction,step=7) if automatic else self.robot.execute(instruction)
        if result.get('accepted') is not True:
            raise RuntimeError('final placement release instruction rejected')
        self.data.update(step7_phase='releasing',vla_instruction=instruction,phase='executing')
        self.event('scanner_final_release',instruction=instruction)

    def _update_step2_instruction(self, judgment):
        advice=judgment['next_action'].strip()
        if not advice:
            self.event('vla_update_skipped',reason='empty next_action')
            return
        instruction,normalized=step2_update_prompt(advice,self.data['held_objects'].get('scanner'))
        try:
            result=self.robot.update_instruction(instruction)
            if result.get('accepted') is not True:
                raise RuntimeError('native instruction update was not accepted')
        except Exception as exc:
            self.event('vla_update_error',reason=type(exc).__name__+': '+str(exc)[:200])
            return
        self.data['vla_instruction']=instruction
        self.data['vla_prompt_updates']+=1
        self.event('vla_prompt_updated',step=2,check=self.data['step2_checks'],
                   next_action=normalized,instruction=instruction,request_id=result.get('request_id'),effective=result.get('effective',False))

    def _fail(self, reason):
        self.epoch += 1
        self.data.update(status='test_failed' if self.data.get('single_step') else 'paused', auto_monitor=False, confirmations=0, phase='paused', last_error=reason)
        self.event('paused', reason=reason)
        try:
            if not self.data.get('check_only'):
                self.robot.stop()
        except Exception:
            self.event('stop_failed', reason='stop could not be confirmed')

    def _route(self):
        self.data['step'] = 5
        code = (self.data['scan'] or {}).get('code', '')
        self.data['target'] = self.c['code_targets'].get(code, 'black')
        self.event('mapped', step=5, code=code or None, target=self.data['target'])
        self.data['step_retries'] = 0
        self._enter(6)

    def scan(self, sid, code):
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError('code must be a nonempty string of at most 128 characters')
        with self.actions, self.lock:
            self._require(sid)
            if self.data['step'] != 4:
                raise Conflict('barcode events are accepted only during this session scan step')
            self.data['scan'] = {'code': code.strip().upper(), 'received_at': time.time(), 'source': 'frontend_scanner'}
            self.event('barcode_received', code=self.data['scan']['code'])
            self.epoch += 1
            try:
                if self.data.get('single_step'):
                    self._finish_single(True)
                else:
                    self._route()
            except Exception:
                self._fail('dispatch after barcode failed')
                raise
            return self.state()

    def observe(self, sid, expected_step=None, automatic=False):
        with self.actions, self.lock:
            self._require(sid)
            if self.data['step'] == 4:
                raise Conflict('Step4 waits for a frontend barcode event; VLM verification is disabled')
            if self.pending and not self.pending.done():
                raise Conflict('one vision observation is already in flight')
            if self.session_calls >= self.c['max_vision_calls']:
                self._fail('vision call budget exhausted')
                raise Conflict('vision call budget reached; stop or retry the session')
            if expected_step is not None and (type(expected_step) is not int or expected_step != self.data['step']):
                raise Conflict('completion event refers to a different step')
            if not automatic:
                try:
                    self.robot.stop()
                except Exception:
                    self._fail('could not stop VLA before verification')
                    raise
            self.data['phase'] = 'executing' if automatic else 'checking'
            self.event('auto_check_started' if automatic else 'execution_ended', step=self.data['step'])
            epoch, step = self.epoch, self.data['step']
            self.pending = self.pool.submit(self._observe, epoch, step, automatic)
            return {'accepted': True, 'session_id': sid, 'step': step}

    def _observe(self, epoch, step, automatic=False):
        try:
            obs = self.robot.observe()
            self._validate_observation(obs)
            with self.actions, self.lock:
                if epoch != self.epoch or self.data['status'] != 'running' or step != self.data['step']:
                    return
                now = time.monotonic()
                if now - self.step_started > self.c['step_timeout']:
                    self._fail('current step timed out')
                    return
                # Require every camera to have a new image, not repeated inference on a frozen stream.
                digests = {k: v['digest'] for k, v in obs['frames'].items()}
                if any(digests[k] == self.last_digests.get(k) for k in digests):
                    self.data.update(confirmations=0, phase='executing' if automatic else 'awaiting_check')
                    self.event('observation_skipped', reason='camera image unchanged')
                    return
                self.last_digests = digests
                self.observation = obs
                self.session_calls += 1
                context = {'step': step, 'target': self.data['target'], 'scan': self.data['scan'],
                           'history': self.data['events'][-8:], 'initial_joints': self.initial['joints'],
                           'current_joints': obs['joints'],
                           'held_objects': (self.data.get('judgment') or {}).get('held_objects', self.data['held_objects']),
                           'step2_reference_hand': self.data['held_objects'].get('scanner') if step==2 else None,
                           'step7_phase':self.data.get('step7_phase'),
                           'test_profile':self.data.get('test_profile'),
                           'scanner_gripper_lock':self.data.get('scanner_gripper_lock')}
            # OpenAI runs outside control/state locks and never blocks VLA inference.
            judgment = validate_judgment(self.vision.judge(context, obs, self.initial), step)
            with self.actions, self.lock:
                if epoch != self.epoch or self.data['status'] != 'running' or step != self.data['step']:
                    return
                # A delayed model reply must not validate old frames.
                if time.time() - obs['captured_at'] > 45:
                    raise ValueError('vision result is stale')
                self.data['raw_judgment'] = getattr(self.vision,'last_raw_text',None) or json.dumps(judgment,ensure_ascii=False)
                self.data['vision_response'] = getattr(self.vision,'last_metadata',None)
                self.data['judgment'] = judgment
                self.event('vision', **judgment)
                self._sync_scanner_gripper(judgment,obs)
                if step==7 and self.data.get('step7_phase')=='placing':
                    if self.data.get('check_only'):
                        self._finish_single(judgment.get('scanner_placed') is True)
                        return
                    if judgment.get('scanner_placed') is True:
                        self._start_final_release(automatic)
                    else:
                        self.data['phase']='executing' if automatic else 'awaiting_check'
                    return
                completed = judgment['status'] == 'COMPLETED'
                if step == 7:
                    completed = completed and all(k in obs['joints'] and abs(obs['joints'][k] - v) <= self.c['joint_tolerance']
                                                   for k, v in self.initial['joints'].items() if 'gripper' not in k.lower())
                held = judgment['held_objects']
                if completed:
                    if step in (1,2):
                        completed = held['scanner'] in ('left', 'right') and held['bottle'] is None
                    elif step==3 and self.data.get('test_profile')=='step3_left':
                        completed=held['bottle']=='left'
                    elif step == 3:
                        completed = held['scanner'] in ('left', 'right') and held['bottle'] in ('left', 'right') and held['scanner'] != held['bottle']
                    elif step == 6:
                        completed = held['scanner'] in ('left', 'right') and held['bottle'] is None
                    elif step == 7:
                        completed = held['scanner'] is None and held['bottle'] is None
                    if not completed:
                        self.event('verification_blocked', reason='holding-hand evidence missing or inconsistent')
                if step==2:
                    self.data['step2_checks']=self.data.get('step2_checks',0)+1
                    if automatic and not completed and self.data.get('executed') and self.data['step2_checks']%2==0:
                        self._update_step2_instruction(judgment)
                self.data.update(confirmations=int(completed), phase='executing' if automatic else 'awaiting_check')
                if automatic and not completed:
                    return
                if self.data.get('single_step'):
                    self.data['held_objects'] = held
                    self._finish_single(completed)
                    return
                if not completed:
                    return
                self.data['held_objects'] = held
                self.data['step_retries'] = 0
                if step == 7:
                    self.robot.stop()
                    self.robot.protect(None, session_id=self.data['session_id'])
                    self.data['hold_arm'] = None
                    self.data.update(status='completed', auto_monitor=False, phase='finished')
                    self.event('completed')
                else:
                    self._enter(step + 1)
        except Exception as exc:
            with self.actions, self.lock:
                if epoch == self.epoch and self.data['status'] == 'running':
                    # fixed adapter errors only; no raw upstream response or secrets
                    self.data['raw_judgment'] = getattr(self.vision,'last_raw_text',None)
                    self.data['vision_response'] = getattr(self.vision,'last_metadata',None)
                    self._fail('observation/dispatch error: ' + type(exc).__name__ + ': ' + str(exc)[:200])

    def retry(self, sid):
        with self.actions, self.lock:
            self._require(sid, ('paused', 'running'))
            if self.data['status'] == 'running' and self.data.get('phase') != 'awaiting_check':
                raise Conflict('current step has not finished execution/verification')
            if self.pending and not self.pending.done():
                raise Conflict('wait for the outstanding observation to finish')
            if self.data['step_retries'] >= self.c['max_step_retries'] or self.session_calls >= self.c['max_vision_calls']:
                raise Conflict('retry or vision budget exhausted')
            self.data['step_retries'] += 1
            self.epoch += 1
            self.data.update(status='running', last_error=None, auto_monitor=False)
            try:
                self._enter(self.data['step'], retry=True)
            except Exception:
                self._fail('retry dispatch failed')
                raise
            return self.state()

    def stop(self, sid):
        # invalidate model result before waiting for serialized dispatch/stop
        with self.lock:
            self._require(sid, ('running', 'starting', 'paused', 'test_failed'))
            self.epoch += 1
            self.data.update(status='stopped', auto_monitor=False, phase='stopped')
        with self.actions, self.lock:
            try:
                self.robot.stop()
                # Do not unlock a holding arm on cancel: requires operator/controller handling.
                self.event('stopped', arm_lock_retained=True)
            except Exception:
                self.data['status'] = 'paused'
                self.data['last_error'] = 'stop could not be confirmed'
                self.event('stop_failed')
            return self.state()

    def _auto_tick(self):
        with self.lock:
            if (not self.c.get('auto_check',True) or self.data['status']!='running'
                    or self.data.get('phase')!='executing' or not self.data.get('executed')
                    or self.data['step'] in (4,5) or (self.pending and not self.pending.done())):
                return
            now=time.monotonic()
            if now-self.data.get('last_check_at',self.step_started)<self.c.get('min_check_interval',2.5):
                return
            sid,step=self.data['session_id'],self.data['step']
            self.data['last_check_at']=now
        try:
            self.observe(sid,step,automatic=True)
        except Conflict:
            pass

    def _schedule(self):
        while not self.shutdown.wait(.25):
            with self.actions, self.lock:
                if (self.data['status'] == 'running' and self.data.get('phase') in ('executing','waiting_scan')
                        and time.monotonic()-self.step_started>self.c['step_timeout']):
                    self._fail('current step timed out')
            try:
                self._auto_tick()
            except Exception:
                # A transient telemetry error never manufactures executed frames.
                pass

    def close(self):
        self.shutdown.set()
        self.motor_jobs.close()
        state = self.state()
        if state['status'] in ('running', 'starting', 'paused'):
            self.stop(state['session_id'])
        self.pool.shutdown(wait=True, cancel_futures=True)
