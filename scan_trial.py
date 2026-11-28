"""Isolated grasp/aim trial. Defaults to simulation; never homes or moves bottles."""
import argparse
import json
import shlex
import socket
import time

import 控制台 as console
from scan_flow import ScanFlow


def checked(command, timeout=15):
    result = console.shell_text(command + '\nrc=$?; echo __TRIAL_RC_${rc}',
                                timeout=timeout, retries=1)
    if '__TRIAL_RC_0' not in result:
        raise RuntimeError('机器人命令未确认成功：' + result[-400:])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--ip', default='10.230.20.13')
    parser.add_argument('--port', type=int, default=8001)
    parser.add_argument('--grasp-seconds', type=float, default=60)
    parser.add_argument('--scan-seconds', type=float, default=40)
    parser.add_argument('--action', choices=['grasp', 'place', 'open'], default='grasp')
    args = parser.parse_args()
    console.ROBOT_IP = args.ip
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((args.ip, 22))
        laptop_ip = route.getsockname()[0]
    endpoint = f'ws://{laptop_ip}:{args.port}'

    def ready():
        # Match the listener's owning process, not just an arbitrary open port.
        import subprocess
        listeners = subprocess.run(['ss', '-ltnp', f'sport = :{args.port}'],
                                   capture_output=True, text=True, check=True).stdout
        import re
        pids = re.findall(r'pid=(\d+)', listeners)
        for pid in pids:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                argv = [p.decode() for p in f.read().split(b'\0') if p]
            if not any(p.endswith('锁臂.py') for p in argv):
                continue
            if '--lock' not in argv or argv[argv.index('--lock') + 1] != 'right':
                continue
            probe = ('import socket; s=socket.create_connection(' +
                     repr((laptop_ip, args.port)) + ',3); s.close()')
            checked('python3 -c ' + shlex.quote(probe))
            return True
        return False

    def stop():
        checked("p=$(pgrep -f 'start_ex001[.]py' || true); "
                'if [ -n "$p" ]; then kill $p; fi; '
                "for i in $(seq 1 12); do pgrep -f 'start_ex001[.]py' >/dev/null || break; sleep 1; done; "
                "! pgrep -f 'start_ex001[.]py' >/dev/null", timeout=18)

    def run(instruction):
        if not ready():
            raise RuntimeError('锁臂进程状态变化，停止下发')
        stop()
        params = json.dumps({'action_horizon': 32, 'control_dt': 0.005,
                             'interpolate_multiplier': 7, 'async_params': {
                                 'api_schema': 'public_v2', 'normalization': 'provided',
                                 'algorithm': 'sync'}})
        script = '/opt/xr/bot/x2robot_client/share/x2robot_client/scripts/start_ex001.sh'
        argv = ['bash', script, '--attach', '--model-address', endpoint,
                '--infer-type', 'sync', '--instruction', instruction,
                '--embodiment', 'quanta_x1_pro/gripper-g', '--return-body',
                json.dumps({'car_pose': False, 'head_pose': False, 'lift': False}),
                '--infer-params', params]
        # Independent log and inline options avoid shared /tmp/*.sh and params.
        checked('setsid nohup ' + shlex.join(argv) +
                ' > /tmp/xr_scan_trial.log 2>&1 < /dev/null &\n'
                'sleep 2; pgrep -f "start_ex001[.]py" >/dev/null', timeout=8)

    if args.action != 'grasp':
        instruction = {'place': 'put the bottle down on the table',
                       'open': 'open the gripper'}[args.action]
        print(instruction, flush=True)
        if args.live:
            checked("! pgrep -f 'start_ex001[.]py' >/dev/null")
            if not ready():
                raise RuntimeError('未确认右臂锁定')
            try:
                run(instruction)
                time.sleep(4)
            finally:
                stop()
        return

    flow = ScanFlow(run, stop, console.last_scan, console.load_rules,
                    harness_ready=ready)
    if args.live:
        checked("! pgrep -f 'start_ex001[.]py' >/dev/null")
    flow.start({'dry_run': not args.live, 'scan_only': True, 'grasp_seconds': args.grasp_seconds,
                'scan_seconds': args.scan_seconds})
    try:
        seen = 0
        while flow.active():
            state = flow.state()
            for event in state['events'][seen:]:
                print(event['message'], flush=True)
            seen = len(state['events'])
            flow.worker.join(0.2)
    except KeyboardInterrupt:
        flow.abort()
        flow.worker.join(25)
    print(json.dumps(flow.state(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
