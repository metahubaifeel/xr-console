import base64
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from .config import prompt, step_prompts, step7_release_prompts, step3_left_test_prompts


def http(url, payload=None, timeout=15, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json', **(headers or {})})
    opener = (urllib.request.build_opener() if url.startswith('https://api.openai.com/')
              else urllib.request.build_opener(urllib.request.ProxyHandler({})))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RuntimeError('response too large')
            return raw
    except urllib.error.HTTPError as e:
        # Never expose raw upstream bodies: they can contain credentials.
        raise RuntimeError(f'upstream HTTP {e.code}') from None
    except (OSError, TimeoutError):
        raise RuntimeError('upstream connection failed or timed out') from None


def http_json(url, payload=None, **kwargs):
    return json.loads(http(url, payload, **kwargs))


def frame(data, at=None, mime='image/jpeg'):
    return {'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode(),
            'captured_at': time.time() if at is None else at, 'digest': hashlib.sha256(data).hexdigest()}


class ConsoleRobot:
    """VLA via existing console; opt-in robot-client trajectory overlay via its bridge.

    Metadata acceptance is not proof of physical holding. GET /arm-lock exposes
    latched targets and last overlay time. No secondary ROS controller is started.
    """
    mode = 'live'
    def __init__(self, config):
        self.c = config

    def motor(self,operation,payload=None):
        if operation=='catalog':return http_json(self.c['console_url']+'/motors',timeout=12)
        return http_json(self.c['console_url']+'/motor/'+operation,payload or {},timeout=90 if operation=='move' else 15)

    def capabilities(self):
        return http_json((self.c['control_url'] or self.c['console_url']) + '/capabilities')

    def camera(self, name):
        dev = {'head': '8', 'left': self.c['wrist_left'], 'right': self.c['wrist_right'], 'bev1': '0', 'bev2': '6'}.get(name)
        if dev is None:
            raise ValueError('unknown camera')
        raw = http(self.c['console_url'] + '/snap?d=' + dev, timeout=5)
        if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
            raise RuntimeError('camera has no complete JPEG')
        return raw

    def observe(self):
        start = time.time()
        health = http_json(self.c['console_url'] + '/video-health')
        def capture(item):
            name, dev = item
            raw = http(self.c['console_url'] + '/snap?d=' + dev)
            if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
                raise RuntimeError('invalid JPEG: ' + name)
            # Wrist freshness is enforced by the console endpoint. Head uses capture stats.
            stamp = health.get('capture', {}).get('8', {}).get('ts', 0) if name == 'head' else time.time()
            return name, frame(raw, at=stamp)
        cameras = [('head', '8'), ('left', self.c['wrist_left']), ('right', self.c['wrist_right'])]
        with ThreadPoolExecutor(max_workers=3) as pool:
            frames = dict(pool.map(capture, cameras))
        joints = http_json(self.c['console_url'] + '/joints')
        if time.time() - start > self.c['max_capture_span']:
            raise RuntimeError('three-camera capture span exceeded')
        return {'captured_at': time.time(), 'frames': frames, 'joints': joints}

    def protect(self, arm, session_id=None):
        result = http_json((self.c['control_url'] or self.c['console_url']) + '/arm-lock',
                           {'arms': [] if arm is None else [arm], 'session_id': session_id})
        if result.get('accepted') is not True:
            raise RuntimeError('hold policy was not accepted by the client overlay')
        self.hold_status = result.get('state')

    def lock_state(self):
        return http_json((self.c['control_url'] or self.c['console_url']) + '/arm-lock')

    def set_gripper_lock(self,arm,target=None,reason=''):
        result=http_json(self.c['console_url']+'/gripper-lock',{'session_id':self.hold_status['session_id'],'arm':arm,'target':target,'reason':reason})
        if result.get('accepted') is not True:raise RuntimeError('gripper policy rejected')
        return result

    def update_instruction(self, instruction, step=2):
        return http_json(self.c['console_url']+'/instruction/update',
                         {'session_id':self.hold_status['session_id'],'instruction':instruction,'step':step},timeout=12)

    def runtime_status(self):
        return http_json(self.c['console_url'] + '/client-status',timeout=5)

    def execute(self, instruction):
        # No quoting-sensitive user commands are sent through the old client's shell path.
        if any(x in instruction for x in ['$', '`', '"', '\\', '\n']):
            raise ValueError('instruction contains unsupported shell characters')
        result = http_json(self.c['console_url'] + '/run', {'instruction': instruction, 'mode': 'arms', 'use_agent': False, 'harness_hold': True, 'harness_session_id': self.hold_status['session_id']})
        if not result.get('ok'):
            raise RuntimeError('VLA client rejected command')
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            status = self.runtime_status()
            if any(p.get('session_id') == self.hold_status['session_id'] for p in status.get('processes',[])):
                return {'accepted': True, 'client_process_started': True}
            time.sleep(.3)
        raise RuntimeError('VLA client did not start or exited during initialization')

    def stop(self):
        result = http_json(self.c['console_url'] + '/hold', {}, timeout=30)
        if not result.get('ok') or '[控制台错误]' in result.get('message', ''):
            raise RuntimeError('client stop not confirmed')
        if self.runtime_status().get('running'):
            raise RuntimeError('VLA client process is still running after stop')


STATUSES = {'COMPLETED', 'IN_PROGRESS', 'UNCERTAIN'}

def validate_judgment(d, step):
    # Legacy responses mean the current attempt is incomplete, not a stop request.
    if isinstance(d, dict) and d.get('status') == 'FAILED':
        d = {**d, 'status': 'IN_PROGRESS'}
    if not isinstance(d, dict) or type(d.get('step')) is not int or d['step'] != step or d.get('status') not in STATUSES:
        raise ValueError('invalid vision judgment or mismatched step')
    if any(not isinstance(d.get(k), str) or len(d[k]) > 2000 for k in ['evidence', 'next_action']):
        raise ValueError('invalid vision evidence')
    held = d.get('held_objects')
    if (not isinstance(held, dict) or set(held) != {'scanner', 'bottle'}
            or any(v not in ('left', 'right', 'unknown', None) for v in held.values())):
        raise ValueError('held_objects must identify scanner/bottle as left/right/unknown/null')
    if held['scanner'] in ('left', 'right') and held['scanner'] == held['bottle']:
        raise ValueError('scanner and bottle cannot occupy the same gripper')
    for key in ('scanner_dropped','scanner_placed'):
        if key in d and d[key] is not None and type(d[key]) is not bool:
            raise ValueError('invalid '+key)
    return {k:d[k] for k in ['step','status','evidence','next_action','held_objects','scanner_dropped','scanner_placed'] if k in d}


class OpenAIVision:
    def __init__(self, config):
        self.c = config
        self.calls = 0
        self.last_raw_text = None
        self.last_metadata = None

    def payload(self, context, observation, initial):
        verification = step_prompts(context['step'], target=context.get('target') or 'black', scanner_arm=context.get('step2_reference_hand') or context.get('held_objects',{}).get('scanner'))['verification_prompt']
        if context.get('test_profile')=='step3_left':
            verification=step3_left_test_prompts()['verification_prompt']
        if context['step']==7 and context.get('step7_phase')=='releasing':
            verification=step7_release_prompts()['verification_prompt']
        content = [{'type': 'input_text', 'text': verification},
                   {'type': 'input_text', 'text': prompt('vision_user.txt', context=json.dumps(context, ensure_ascii=False))}]
        for kind, obs in [('initial', initial), ('current', observation)]:
            for name in ['head', 'left', 'right']:
                f = obs['frames'][name]
                content += [{'type': 'input_text', 'text': f'{kind} camera={name} captured_at={f["captured_at"]}'},
                            {'type': 'input_image', 'image_url': f['url']}]
        schema = {'type': 'object', 'properties': {
            'step': {'type': 'integer'}, 'status': {'type': 'string', 'enum': sorted(STATUSES)},
            'evidence': {'type': 'string'}, 'next_action': {'type': 'string'},
            'scanner_dropped': {'type':['boolean','null']},
            'scanner_placed': {'type':['boolean','null']},
            'held_objects': {'type': 'object', 'properties': {
                'scanner': {'type': ['string', 'null'], 'enum': ['left', 'right', 'unknown', None]},
                'bottle': {'type': ['string', 'null'], 'enum': ['left', 'right', 'unknown', None]}},
                'required': ['scanner', 'bottle'], 'additionalProperties': False}},
            'required': ['step', 'status', 'evidence', 'next_action', 'held_objects', 'scanner_dropped', 'scanner_placed'], 'additionalProperties': False}
        return {'model': self.c['model'], 'instructions': prompt('vision_system.txt'),
                'input': [{'role': 'user', 'content': content}], 'store': False,
                'reasoning': {'effort': 'none'}, 'max_output_tokens': 600,
                'text': {'format': {'type': 'json_schema', 'name': 'execution_judgment', 'strict': True, 'schema': schema}}}

    def _request(self, payload):
        if not self.c['openai_key']:
            raise RuntimeError('OPENAI_API_KEY is not configured')
        self.calls += 1
        self.last_raw_text = None
        self.last_metadata = None
        result = http_json('https://api.openai.com/v1/responses', payload,
                           timeout=40, headers={'Authorization': 'Bearer ' + self.c['openai_key']})
        self.last_raw_text = ''.join(c.get('text', '') for o in result.get('output', [])
                                    for c in o.get('content', []) if c.get('type') == 'output_text')
        self.last_metadata = {'response_id': result.get('id'), 'model': result.get('model'), 'usage': result.get('usage')}
        if result.get('status') != 'completed':
            raise RuntimeError('vision response did not complete')
        return json.loads(self.last_raw_text)

    def judge(self, context, observation, initial):
        if context['step'] == 4:
            raise ValueError('Step4 uses frontend barcode events')
        return validate_judgment(self._request(self.payload(context, observation, initial)), context['step'])

    def identify_holdings(self, observation):
        content = []
        for name in ['head', 'left', 'right']:
            content += [{'type': 'input_text', 'text': 'camera=' + name},
                        {'type': 'input_image', 'image_url': observation['frames'][name]['url']}]
        hand = {'type': ['string', 'null'], 'enum': ['left', 'right', 'unknown', None]}
        schema = {'type': 'object', 'properties': {'scanner': hand, 'bottle': hand, 'evidence': {'type': 'string'}},
                  'required': ['scanner', 'bottle', 'evidence'], 'additionalProperties': False}
        result = self._request({'model': self.c['model'], 'instructions': prompt('holding_system.txt'),
                     'input': [{'role': 'user', 'content': content}], 'store': False,
                     'reasoning': {'effort': 'none'}, 'max_output_tokens': 400,
                     'text': {'format': {'type': 'json_schema', 'name': 'holding_hands', 'strict': True, 'schema': schema}}})
        if (not isinstance(result, dict) or any(result.get(k) not in ('left', 'right', 'unknown', None) for k in ['scanner','bottle'])
                or not isinstance(result.get('evidence'), str)):
            raise ValueError('invalid holding recognition')
        if result['scanner'] in ('left','right') and result['scanner'] == result['bottle']:
            raise ValueError('both objects assigned to the same gripper')
        return result
