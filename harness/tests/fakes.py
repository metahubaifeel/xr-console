import time,struct,zlib
from harness.adapters import frame

def png(seq):
    def chunk(k, v):
        return struct.pack('!I', len(v)) + k + v + struct.pack('!I', zlib.crc32(k + v) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 240, 160, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes([seq % 255, 80, 120]) * 240) * 160)) + chunk(b'IEND', b''))


class MockRobot:
    mode = 'mock'
    def __init__(self):
        self.seq = 0
        self.commands = []
        self.updates = []
        self.grippers = {}
        self.stops = 0
        self.held_arm = None

    def capabilities(self):
        return {'arm_lock': True, 'joint_feedback': True}

    def observe(self):
        self.seq += 1
        return {'captured_at': time.time(), 'frames': {name: frame(png(self.seq + i), mime='image/png')
                for i, name in enumerate(['head', 'left', 'right'])},
                'joints': {'left_arm_joint1': 0.0, 'right_arm_joint1': 0.0,'left_arm_gripper':.04,'right_arm_gripper':.06}, 'mock': True}

    def protect(self, arm, session_id=None):
        self.held_arm = arm
        self.hold_session = session_id
        self.hold_status={'session_id':session_id,'grippers':self.grippers}

    def lock_state(self):
        return {'grippers':dict(self.grippers),'state': 'mock', 'arms': [] if self.held_arm is None else [self.held_arm], 'physical_hold_verified': False}

    def set_gripper_lock(self,arm,target=None,reason=''):
        if arm is None:self.grippers.clear()
        elif arm not in self.grippers:self.grippers[arm]={'target':target}
        return {'accepted':True,'grippers':dict(self.grippers)}

    def update_instruction(self,instruction,step=2):
        self.updates.append(instruction)
        return {'accepted':True,'effective':True,'request_id':'unit-fixture'}

    def execute(self, instruction):
        self.commands.append(instruction)
        return {'accepted': True, 'mock': True}

    def stop(self):
        self.stops += 1


class MockVision:
    def __init__(self, scanner_arm='left'):
        self.calls = 0
        self.scanner_arm = scanner_arm
    def judge(self, context, observation, initial):
        if context['step'] == 4:
            raise ValueError('Step4 uses frontend barcode events')
        self.calls += 1
        return {'step': context['step'], 'status': 'COMPLETED',
                'evidence': 'MOCK：模拟当前步骤已满足条件，不代表真实视觉判断', 'next_action': '请求后端检查切步条件',
                'scanner_dropped':False,'scanner_placed':context['step']==7,
                'held_objects': {'scanner': self.scanner_arm if context['step'] != 7 else None,
                                 'bottle': ('right' if self.scanner_arm == 'left' else 'left') if context['step'] in (3,4) else None}}



    def identify_holdings(self, observation):
        self.calls += 1
        return {'scanner':self.scanner_arm, 'bottle':'right' if self.scanner_arm=='left' else 'left', 'evidence':'unit fixture'}
