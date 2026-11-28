"""Metadata/configuration only. Does not initialize ROS or send commands."""
import json
import sys
from pathlib import Path
from hold_runtime import VERSION, DEFAULT_PATH, configure, read, prepare_session, set_gripper_policy

MODULE_DIR = Path('/opt/xr/bot/x2robot_client/lib/python3.12/site-packages/x2robot_client')
MARKER = '# XR_HARNESS_HOLD_V1'

def main():
    operation = sys.argv[1]
    path = DEFAULT_PATH
    if operation == 'capabilities':
        source = (MODULE_DIR / 'ex001_client.py').read_text()
        ready = source.count(MARKER) == 2 and (MODULE_DIR / 'xr_hold_runtime.py').exists()
        result = {'arm_lock': ready, 'joint_feedback': True, 'overlay_installed': ready,
                  'scanner_gripper_lock': 'overlay_grippers' in (MODULE_DIR/'xr_hold_runtime.py').read_text(), 'version': VERSION, 'spaces': ['end_pose', 'joints'], 'physical_hold_verified': False}
    elif operation == 'runtime-status':
        processes = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc/'cmdline').read_bytes().split(b'\0')
                if not any(arg.endswith(b'/start_ex001.py') for arg in args):
                    continue
                env = (proc/'environ').read_bytes().split(b'\0')
                session = next((x.split(b'=',1)[1].decode() for x in env if x.startswith(b'XR_HARNESS_SESSION_ID=')),None)
                processes.append({'pid':int(proc.name),'session_id':session})
            except (OSError,UnicodeError):
                continue
        result = {'running':bool(processes),'processes':processes}
    elif operation == 'prepare':
        payload=json.loads(sys.argv[2]);result=prepare_session(path,payload['session_id'],payload['instruction'])
    elif operation == 'gripper':
        payload=json.loads(sys.argv[2]);result=set_gripper_policy(path,payload['session_id'],payload.get('arm'),payload.get('target'),payload.get('reason',''))
    elif operation == 'configure':
        payload = json.loads(sys.argv[2])
        d = configure(path, payload['session_id'], payload['arms'])
        result = {'accepted': True, 'overlay_installed': True, 'state': d, 'physical_hold_verified': False}
    elif operation == 'status':
        result = read(path) if Path(path).exists() else {'state': 'unconfigured'}
    else:
        raise ValueError('unknown bridge operation')
    print(json.dumps(result, allow_nan=False))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}))
        sys.exit(1)
