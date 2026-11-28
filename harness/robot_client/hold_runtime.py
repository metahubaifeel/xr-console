"""Opt-in trajectory overlay. No ROS initialization, model request or motion I/O.

Install beside the official EX001 client. Only enabled when
XR_HARNESS_HOLD_FILE is set by a harness-created session.
"""
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
import uuid

VERSION = '1.0'
DEFAULT_PATH = '/tmp/xr-harness/hold_state.json'


@contextlib.contextmanager
def state_lock(path):
    p = Path(path)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(p) + '.lock', 'a') as f:
        os.chmod(f.name, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        yield p


def atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix='.hold-', dir=Path(path).parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read(path):
    with open(path) as f:
        d = json.load(f)
    if d.get('version') != VERSION or not isinstance(d.get('session_id'), str) or not d['session_id']:
        raise ValueError('invalid hold session')
    arms = d.get('arms')
    if not isinstance(arms, list) or len(set(arms)) != len(arms) or any(a not in ('left', 'right') for a in arms):
        raise ValueError('invalid hold arms')
    if not isinstance(d.get('targets'), dict) or not isinstance(d.get('revision'), str):
        raise ValueError('invalid hold targets/revision')
    return d


def process_live(d):
    pid = d.get('client_pid')
    if not isinstance(pid, int):
        return False
    try:
        # PID start ticks prevent confusing a reused PID with this client.
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19] == d.get('client_start_ticks')
    except (OSError, IndexError):
        return False


def configure(path, session_id, arms):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError('session_id required')
    if not isinstance(arms, list) or any(a not in ('left', 'right') for a in arms) or len(set(arms)) != len(arms):
        raise ValueError('invalid arms')
    with state_lock(path) as p:
        old = read(p) if p.exists() else None
        if old and (session_launcher_running(old['session_id']) if old.get('binding_kind')=='sdk_service' else process_live(old)):
            raise RuntimeError('stop the harness client before changing its hold policy')
        # Repeating the same policy keeps the ORIGINAL captured target across client restarts.
        if old and old['session_id'] == session_id and old['arms'] == arms:
            return old
        d = {'version': VERSION, 'session_id': session_id, 'revision': uuid.uuid4().hex,
             'arms': arms, 'targets': {}, 'grippers': old.get('grippers',{}) if old else {}, 'requested_at': time.time(), 'state': 'armed' if arms else 'released'}
        if old and old['session_id'] == session_id:
            d['targets'] = {a: old['targets'][a] for a in arms if a in old['targets']}
        atomic_write(p, d)
        return d


def vector(value, name):
    import numpy as np
    a = np.asarray(value, dtype=np.float64)
    if a.ndim != 1 or not len(a) or not np.isfinite(a).all():
        raise ValueError('invalid current feedback: ' + name)
    return a.tolist()


def latch(d, state, mode):
    if mode not in ('end_pose', 'joints', 'joints_pos'):
        raise ValueError('unsupported action space')
    for arm in d['arms']:
        if arm in d['targets']:
            continue
        i = 1 if arm == 'left' else 2
        joints = vector(state[f'follow{i}_joints'], 'joints')
        ee = vector(state[f'follow{i}_pos'], 'end_pose')
        if len(joints) not in (6,7) or len(ee) != 7:
            raise ValueError('invalid EX001 joints or xyz/rpy/gripper feedback')
        d['targets'][arm] = {'joint_positions': joints, 'end_pose_xyz_rpy': ee[:6], 'gripper': ee[6],
                            'captured_at': time.time(), 'source': 'client.read_state_snapshot'}
    d.update(state='latched' if d['arms'] else 'released', control_mode=mode,
             lock_space='end_pose' if mode == 'end_pose' else 'joints', client_pid=os.getpid())
    try:
        d['client_start_ticks'] = Path(f'/proc/{os.getpid()}/stat').read_text().split(') ', 1)[1].split()[19]
    except OSError:
        d['client_start_ticks'] = None  # local tests; production is Linux
    return d


def overlay(left, right, d, mode, layout='decoded'):
    import numpy as np
    if mode not in ('end_pose', 'joints', 'joints_pos') or layout not in ('decoded', 'dispatch'):
        raise ValueError('unsupported action layout')
    result = []
    for arm, values in [('left', left), ('right', right)]:
        # Unlocked arm is returned untouched, including its dtype and original values.
        if arm not in d['arms']:
            result.append(values)
            continue
        a = np.asarray(values)
        if a.ndim != 2 or len(a) == 0 or not np.issubdtype(a.dtype, np.floating) or not np.isfinite(a).all():
            raise ValueError('invalid trajectory for held arm')
        target = d['targets'][arm]
        if mode == 'end_pose':
            pose = vector(target['end_pose_xyz_rpy'], 'target EE')
            if len(pose) != 6:
                raise ValueError('invalid EE target dimension')
            if layout == 'dispatch':
                x,y,z = pose[3:]
                cx,sx,cy,sy,cz,sz = math.cos(x/2),math.sin(x/2),math.cos(y/2),math.sin(y/2),math.cos(z/2),math.sin(z/2)
                pose = pose[:3] + [sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz,
                                   cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz]
            goal = pose + [target['gripper']]
        else:
            goal = vector(target['joint_positions'], 'target joints')
            if len(goal) not in (6,7):
                raise ValueError('invalid joint target dimension')
            if a.shape[1] == len(goal)+1:
                goal += [target['gripper']]
        if a.shape[1] != len(goal) or not np.isfinite(goal).all():
            raise ValueError('hold target/action shape or finite check failed')
        out = a.copy()
        out[:] = goal  # EVERY row in the chunk, not only the first action
        result.append(out)
    return tuple(result)


def apply_client_hold(owner, left, right, layout='decoded'):
    path,expected = client_binding(owner)
    if not path:
        return left,right
    with state_lock(path) as p:
        d = read(p)  # missing/corrupt enabled policy is an error; never fall back to unmasked motion
        if not expected or d['session_id'] != expected:
            raise ValueError('hold session does not match this client')
        revision = getattr(owner, '_xr_hold_revision', None)
        if revision is not None and revision != d['revision']:
            raise RuntimeError('hold policy changed while the client was active')
        owner._xr_hold_revision = d['revision']
        d['binding_kind']='direct' if os.environ.get('XR_HARNESS_HOLD_FILE') else 'sdk_service'
        if layout == 'decoded':
            snapshot = owner._collect_state_snapshot()  # fresh SDK feedback, not model output or old JSON
            d = latch(d, snapshot['state'], owner.control_mode)
            d['joint_dimensions']={arm: vector(snapshot['state'][f'follow{i}_joints'],'joints') for arm,i in [('left',1),('right',2)]}
        elif d.get('control_mode') != owner.control_mode:
            raise ValueError('control mode changed within a hold session')
        result = overlay(left, right, d, owner.control_mode, layout)
        result = overlay_grippers(*result,d,owner.control_mode,layout)
        d['last_overlay_at'] = time.time()
        d['last_layout'] = layout
        d['overlaid_chunks'] = d.get('overlaid_chunks', 0) + int(layout == 'dispatch')
        atomic_write(p, d)
        return result



def session_launcher_running(session):
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if any(arg.endswith(b'/start_ex001.py') for arg in args):
                env=(proc/'environ').read_bytes().split(b'\0')
                if ('XR_HARNESS_SESSION_ID='+session).encode() in env:
                    return True
        except OSError:
            continue
    return False


def prepare_session(path, session, instruction):
    import hashlib
    with state_lock(path) as p:
        d=read(p)
        if d['session_id']!=session:
            raise ValueError('launcher/session mismatch')
        d['instruction_sha256']=hashlib.sha256(instruction.encode()).hexdigest()
        atomic_write(p,d)
    return {'accepted':True}


def client_binding(owner):
    import hashlib
    path=os.environ.get('XR_HARNESS_HOLD_FILE')
    if path:
        return path,os.environ.get('XR_HARNESS_SESSION_ID')
    bound=getattr(owner,'_xr_harness_binding',None)
    if bound:
        return bound
    # ROS service owns the SDK object; launcher environment is a different process.
    p=Path(DEFAULT_PATH)
    if not p.exists():
        return None,None
    d=read(p)
    current=getattr(owner,'_instruction_current',{})
    same_hash=hashlib.sha256(str(getattr(owner,'instruction','')).encode()).hexdigest()==d.get('instruction_sha256')
    same_episode=current.get('episode_id')==d['session_id'] and current.get('source')=='xr_harness_vlm'
    if not (same_hash or same_episode) or not session_launcher_running(d['session_id']):
        return None,None
    owner._xr_harness_binding=(str(p),d['session_id'])
    return owner._xr_harness_binding


def set_gripper_policy(path,session,arm,target=None,reason=''):
    with state_lock(path) as p:
        d=read(p)
        if d['session_id']!=session:
            raise ValueError('gripper/session mismatch')
        locks=d.get('grippers',{})
        if arm is None:
            if reason not in ('vlm_drop','final_release'):
                raise ValueError('unlock requires explicit drop or verified final placement')
            locks={}
        else:
            if arm not in ('left','right') or type(target) not in (int,float) or not math.isfinite(target):
                raise ValueError('invalid gripper target')
            if not -.1<=target<=4.6:
                raise ValueError('gripper target outside EX001 SDK limits')
            if locks and arm not in locks:
                raise ValueError('holding hand changed without a drop confirmation')
            if arm not in locks:
                locks={arm:{'target':float(target),'captured_at':time.time()}}
        d.update(grippers=locks,gripper_reason=reason,gripper_updated_at=time.time())
        atomic_write(p,d)
        return {'accepted':True,'grippers':locks}


def overlay_grippers(left,right,d,mode,layout):
    import numpy as np
    result=[]
    for arm,values in [('left',left),('right',right)]:
        lock=d.get('grippers',{}).get(arm)
        if lock is None:
            result.append(values);continue
        a=np.asarray(values)
        if a.ndim!=2 or not len(a) or not np.issubdtype(a.dtype,np.floating) or not np.isfinite(a).all():
            raise ValueError('invalid gripper trajectory')
        expected=7 if mode=='end_pose' and layout=='decoded' else 8 if mode=='end_pose' else len(d.get('targets',{}).get(arm,{}).get('joint_positions',[]))+1
        if mode!='end_pose' and expected==1:
            expected=len(d.get('joint_dimensions',{}).get(arm,[]))+1
        if a.shape[1]!=expected:
            raise ValueError('gripper column absent or invalid action dimensions')
        target=lock['target']
        if type(target) not in (int,float) or not math.isfinite(target) or not -.1<=target<=4.6:
            raise ValueError('invalid held gripper target')
        out=a.copy();out[:,-1]=target;result.append(out)
    return tuple(result)
