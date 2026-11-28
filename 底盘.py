#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底盘控制（差速：前进/后退 + 转向，不能横移）

话题  /databridge/slave/chassis_control_on_keyboard   geometry_msgs/Twist
反馈  /odom
URDF  只有 left_wheel_joint / right_wheel_joint → 差速驱动

用法
----
  python3 底盘.py forward 0.10 2.0     # 前进：0.10 m/s，持续 2 秒
  python3 底盘.py back    0.10 2.0     # 后退
  python3 底盘.py turn    0.30 1.5     # 左转：0.30 rad/s，1.5 秒（负值右转）
  python3 底盘.py stop                 # 立刻停

安全
----
  · 每条命令结束都会自动发零速度（不会失控）
  · 速度上限默认 0.2 m/s / 0.6 rad/s，可用 --max 放开
  · 底盘动之前请确认前方没人、没桌子
"""

import argparse
import base64
import os
import subprocess
import sys
import time

ROBOT_IP = os.environ.get("ROBOT_IP", "10.230.20.13")
SSH_USER = "xr"
SSH_PASS = os.environ.get("XR_SSH_PASS", "123")
CONTAINER = "ex001_client-robot_perception_ops-1"
TOPIC = "/databridge/slave/chassis_control_on_keyboard"


def _ssh(remote, timeout=60):
    cmd = ["sshpass", "-p", SSH_PASS, "ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "PreferredAuthentications=password",
           "-o", "PubkeyAuthentication=no",
           "-o", "LogLevel=ERROR",
           "-o", "ConnectTimeout=8",
           f"{SSH_USER}@{ROBOT_IP}", remote]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _docker_bash(script, timeout=90):
    b = base64.b64encode(script.encode()).decode()
    return _ssh(f"docker exec {CONTAINER} bash -lc 'echo {b} | base64 -d | bash'",
                timeout=timeout)


def publish(vx, wz, seconds):
    """持续发 seconds 秒的 Twist，然后发零速"""
    script = f'''source /opt/xr/setup.bash >/dev/null 2>&1
END=$(python3 -c "import time;print(time.time()+{seconds})")
while python3 -c "import time,sys;sys.exit(0 if time.time()<$END else 1)"; do
  timeout 1 ros2 topic pub --once {TOPIC} geometry_msgs/msg/Twist \
    "{{linear: {{x: {vx}, y: 0.0, z: 0.0}}, angular: {{x: 0.0, y: 0.0, z: {wz}}}}}" >/dev/null 2>&1
  sleep 0.1
done
timeout 1 ros2 topic pub --once {TOPIC} geometry_msgs/msg/Twist \
  "{{linear: {{x: 0.0, y: 0.0, z: 0.0}}, angular: {{x: 0.0, y: 0.0, z: 0.0}}}}" >/dev/null 2>&1
echo done
'''
    return _docker_bash(script, timeout=int(seconds) + 40)


def stop():
    script = f'''source /opt/xr/setup.bash >/dev/null 2>&1
for i in 1 2 3; do
  timeout 1 ros2 topic pub --once {TOPIC} geometry_msgs/msg/Twist \
    "{{linear: {{x: 0.0, y: 0.0, z: 0.0}}, angular: {{x: 0.0, y: 0.0, z: 0.0}}}}" >/dev/null 2>&1
  sleep 0.1
done
echo stopped
'''
    return _docker_bash(script, timeout=40)


def odom():
    script = '''source /opt/xr/setup.bash >/dev/null 2>&1
timeout 5 ros2 topic echo --once /odom 2>/dev/null | head -20
'''
    return _docker_bash(script, timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["forward", "back", "turn", "move", "stop", "odom"])
    ap.add_argument("value", nargs="?", type=float, default=0.1,
                    help="速度：forward/back 是 m/s，turn 是 rad/s")
    ap.add_argument("seconds", nargs="?", type=float, default=1.0)
    ap.add_argument("--max-lin", type=float, default=0.20)
    ap.add_argument("--max-ang", type=float, default=0.60)
    a = ap.parse_args()

    if a.action == "stop":
        print(stop()[1].strip())
        return 0
    if a.action == "odom":
        print(odom()[1].strip()[:800])
        return 0

    if a.action == "move":
        v = max(-a.max_lin, min(a.max_lin, a.value))
        print(f"底盘 move vx={v:+.2f} m/s × {a.seconds}s")
        rc, out = publish(v, 0.0, a.seconds)
        print(out.strip().splitlines()[-1] if out.strip() else f"rc={rc}")
        return 0 if rc == 0 else 1

    if a.action in ("forward", "back"):
        v = min(abs(a.value), a.max_lin)
        if a.action == "back":
            v = -v
        print(f"底盘 {'前进' if v > 0 else '后退'} {abs(v):.2f} m/s × {a.seconds}s")
        rc, out = publish(v, 0.0, a.seconds)
    else:
        w = max(-a.max_ang, min(a.max_ang, a.value))
        print(f"底盘 {'左转' if w > 0 else '右转'} {abs(w):.2f} rad/s × {a.seconds}s")
        rc, out = publish(0.0, w, a.seconds)
    print(out.strip().splitlines()[-1] if out.strip() else f"rc={rc}")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
