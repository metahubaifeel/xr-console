#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动编排器 —— 启动一次，之后全程不需要人工输入

规则依据（参赛手册《WALL API 能力边界》）：
    「演示启动后禁止人工输入和遥控。」
    → 所以所有切步必须由软件完成：按时长 + 按扫码事件，不能靠人点。

架构
----
    机器人 client ──ws──> 锁臂.py(:8001) ──wss──> 官方 VLA
                              └──(:8002/say) 中途改 prompt
    编排器 ──POST :8002/say──> 改 prompt（不重开会话，机械臂不中断）
           ──GET  :8898/* ─-> 关节 / 夹爪 / 扫码
           ──SSH  xr@机器人──> 启动会话 / 停止 / 归零

用法
----
    python3 编排.py --flow flows/d3_scan.json            # 真实跑
    python3 编排.py --flow flows/d3_scan.json --dry-run  # 演练不动机器人
    python3 编排.py --flow flows/d3_scan.json --stop-at wait_scan  # 跑到某步就停

流程文件格式
-----------
{
  "name": "D3 扫码分拣",
  "robot_ip": "10.230.20.13",
  "steps": [
    {"id": "grasp", "say": "...", "sec": 70},
    {"id": "wait",  "wait_scan": 90},
    {"id": "place", "say_from_scan": true, "sec": 80},
    {"id": "home",  "home": true}
  ]
}
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# ---------------- 配置 ----------------
ROBOT_IP = "10.230.20.13"
SSH_USER = "xr"
SSH_PASS = os.environ.get("XR_SSH_PASS", "123")
SAY_PORT = 8002              # 锁臂.py 的控制口
ROBOT_API = 8898             # 机器人端数据服务
CONTAINER = "ex001_client-robot_client-1"
START_SCRIPT = ("/opt/xr/bot/x2robot_client/share/x2robot_client/scripts/"
                "start_ex001.sh")
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------- 日志 ----------------
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- HTTP ----------------
def http_json(url, payload=None, timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def say(instruction):
    """中途改 prompt（不重开会话）"""
    return http_json(f"http://127.0.0.1:{SAY_PORT}/say",
                     {"instruction": instruction})


def now_instruction():
    return http_json(f"http://127.0.0.1:{SAY_PORT}/", timeout=4)


def joints():
    return http_json(f"http://{ROBOT_IP}:{ROBOT_API}/joints", timeout=4)


def gripper():
    return http_json(f"http://{ROBOT_IP}:{ROBOT_API}/gripper", timeout=4)


def last_scan():
    try:
        return http_json(f"http://127.0.0.1:8900/scan", timeout=4).get("last")
    except Exception:  # noqa: BLE001
        try:
            with open("/tmp/last_scan.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None


def load_rules():
    for p in (os.path.join(HERE, "scan_rules.json"),
              os.path.join(os.getcwd(), "scan_rules.json")):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001
                pass
    return {"default": "put the bottle into the red square", "map": {}}


# ---------------- SSH ----------------
def ssh(remote_cmd, timeout=90):
    cmd = ["sshpass", "-p", SSH_PASS, "ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "PreferredAuthentications=password",
           "-o", "PubkeyAuthentication=no",
           "-o", "LogLevel=ERROR",
           "-o", f"ConnectTimeout=8",
           f"{SSH_USER}@{ROBOT_IP}", remote_cmd]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def docker_bash(script, detach=False, timeout=90):
    """把脚本 base64 后送进容器执行（避免引号地狱，也不依赖 /tmp/*.sh）"""
    import base64
    b = base64.b64encode(script.encode()).decode()
    d = "-d " if detach else ""
    remote = (f"docker exec {d}{CONTAINER} bash -lc "
              f"'echo {b} | base64 -d | bash'")
    return ssh(remote, timeout=timeout)


def start_session(harness_host, harness_port=8001):
    """启动推理会话，model-address 指向锁臂中间层"""
    script = f'''SCRIPT={START_SCRIPT}
PAT='start_ex001[.]py'
if pgrep -f "$PAT" >/dev/null; then
  kill $(pgrep -f "$PAT") 2>/dev/null
  for i in $(seq 1 25); do pgrep -f "$PAT" >/dev/null || break; sleep 1; done
  pkill -9 -f "$PAT" 2>/dev/null
fi
setsid nohup bash $SCRIPT --attach \\
  --model-address ws://{harness_host}:{harness_port} \\
  --infer-type sync \\
  --instruction "standby" \\
  --embodiment quanta_x1_pro/gripper-g \\
  --return-body '{{"car_pose":false,"head_pose":true,"lift":true}}' \\
  --infer-params '{{"action_horizon":32,"control_dt":0.005,"interpolate_multiplier":7,"async_params":{{"api_schema":"public_v2","normalization":"provided","algorithm":"sync"}}}}' \\
  > /tmp/l0.log 2>&1 < /dev/null &
'''
    return docker_bash(script, detach=False, timeout=60)


def stop_session():
    script = '''PAT='start_ex001[.]py'
if pgrep -f "$PAT" >/dev/null; then
  kill $(pgrep -f "$PAT") 2>/dev/null
  for i in $(seq 1 12); do pgrep -f "$PAT" >/dev/null || break; sleep 1; done
  pkill -9 -f "$PAT" 2>/dev/null
  echo "stopped"
else
  echo "not-running"
fi
'''
    return docker_bash(script, timeout=60)


def home_robot():
    script = '''source /opt/xr/bot/x2robot_client/share/x2robot_client/scripts/session_env.sh >/dev/null 2>&1
python3 -m x2robot_client.zero_arm --timeout 90 2>&1 | tail -3
'''
    return docker_bash(script, timeout=140)


# ---------------- 编排执行 ----------------
class Runner:
    def __init__(self, flow, dry=False, stop_at=None):
        self.flow = flow
        self.dry = dry
        self.hold = False
        self.stop_at = stop_at
        self.rules = load_rules()
        self.code = None
        self.t0 = time.time()
        self.log_lines = []

    def el(self):
        return time.time() - self.t0

    def note(self, msg):
        log(msg)
        self.log_lines.append(f"[{self.el():6.1f}s] {msg}")

    def run(self):
        name = self.flow.get("name", "未命名")
        self.note(f"===== 开始编排：{name}（{'演练' if self.dry else '真跑'}）=====")
        if not self.dry:
            rc, out = start_session(self.flow.get("harness_host") or LAN_IP)
            self.note(f"启动会话 rc={rc}")
            if rc != 0:
                self.note(f"⚠️ 启动失败：{out[:300]}")
                return False
            time.sleep(8)          # 等 INIT/START
            j = joints()
            self.note(f"会话就绪 左J1={j.get('left_arm_joint1', 0):+.3f} "
                      f"右J1={j.get('right_arm_joint1', 0):+.3f}")

        ok = True
        for i, st in enumerate(self.flow.get("steps", []), 1):
            sid = st.get("id") or f"step{i}"
            if self.stop_at and sid == self.stop_at:
                self.note(f"到达 --stop-at {sid}，停止")
                break
            try:
                r = self._step(st)
            except Exception as e:  # noqa: BLE001
                self.note(f"❌ {sid} 异常：{type(e).__name__}: {e}")
                ok = False
                break
            if r is False:
                ok = False
                break

        # 收尾
        self.note("===== 收尾 =====")
        if not self.dry:
            stop_session()
            self.note("会话已停止（机械臂冻结在当前姿态）")
            if getattr(self, 'hold', False):
                self.note("--hold：不归零，保持当前姿态，等人接手")
            else:
                rc, out = home_robot()
                self.note("归零：" + (out.strip().splitlines() or ["?"])[-1][:120])
        self.note(f"===== 结束（{'成功' if ok else '中止'}）耗时 {self.el():.0f}s =====")
        return ok

    def _step(self, st):
        sid = st.get("id") or "step"

        if st.get("home"):
            self.note(f"[{sid}] 归零")
            if not self.dry:
                home_robot()
            return True

        if st.get("chassis"):
            c = st["chassis"] or {}
            vx = float(c.get("vx", 0.0))
            wz = float(c.get("wz", 0.0))
            sec = float(c.get("sec", st.get("sec", 1.0)))
            self.note(f"[{sid}] 底盘 vx={vx:+.2f} m/s wz={wz:+.2f} rad/s × {sec}s")
            if not self.dry:
                import subprocess as _sp
                _sp.run([sys.executable, os.path.join(HERE, "底盘.py"),
                         "move" if vx else "turn", str(vx if vx else wz), str(sec)],
                        timeout=int(sec) + 60)
            else:
                time.sleep(min(sec, 3))
            return True

        if st.get("wait_scan"):
            return self._wait_scan(sid, float(st["wait_scan"]))

        instruction = st.get("say")
        if st.get("say_from_scan"):
            code = (self.code or "").strip().upper()
            instruction = self.rules.get("map", {}).get(code) or \
                          self.rules.get("default")
            self.note(f"[{sid}] 码={code} → 映射到：{instruction}")

        if not instruction:
            self.note(f"[{sid}] 没有指令，跳过")
            return True

        self.note(f"[{sid}] say: {instruction}")
        if not self.dry:
            r = say(instruction)
            self.note(f"[{sid}]   → {r.get('now')}")

        sec = float(st.get("sec", 30))
        deadline = time.time() + sec
        nxt = 5
        while time.time() < deadline:
            time.sleep(min(5, max(0.2, deadline - time.time())))
            if time.time() - (deadline - sec) >= nxt:
                nxt += 5
                if not self.dry:
                    j = joints()
                    g = gripper()
                    self.note(f"[{sid}]   t={self.el():.0f}s 左J2="
                              f"{j.get('left_arm_joint2', 0):+.2f} "
                              f"右J1={j.get('right_arm_joint1', 0):+.3f} "
                              f"左爪={g.get('left', 0):.3f}")
        return True

    def _wait_scan(self, sid, timeout):
        self.note(f"[{sid}] 等待扫码，最长 {timeout:.0f} 秒")
        baseline = time.time()
        rules = self.rules.get("map", {})
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            s = last_scan() if not self.dry else {"code": "OUTC", "ts": time.time()}
            if not s:
                continue
            if float(s.get("ts", 0)) < baseline:
                continue          # 只认本轮新扫到的
            code = str(s.get("code", "")).strip().upper()
            if code:
                self.code = code
                self.note(f"[{sid}] ✅ 扫到 {code}"
                          + ("" if code in rules else "（映射表里没有，用默认）"))
                return True
        self.note(f"[{sid}] ⚠️ {timeout:.0f} 秒内没扫到，继续下一步")
        return True


LAN_IP = "10.230.20.92"


def detect_lan_ip():
    global LAN_IP
    try:
        r = subprocess.run(["bash", "-lc",
                            "ip -4 -o addr show scope global | "
                            "awk '{print $4}' | cut -d/ -f1 | grep -v '^172\\.' | head -1"],
                           capture_output=True, timeout=5)
        ip = r.stdout.decode().strip()
        if ip:
            LAN_IP = ip
    except Exception:  # noqa: BLE001
        pass
    return LAN_IP


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flow", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stop-at")
    ap.add_argument("--hold", action="store_true", help="收尾只停会话、不归零（臂冻结在当前姿态）")
    ap.add_argument("--robot-ip")
    a = ap.parse_args()

    global ROBOT_IP
    if a.robot_ip:
        ROBOT_IP = a.robot_ip

    with open(a.flow, encoding="utf-8") as f:
        flow = json.load(f)
    if flow.get("robot_ip") and not a.robot_ip:
        ROBOT_IP = flow["robot_ip"]

    detect_lan_ip()
    print("=" * 62)
    print(f"  自动编排器 —— {flow.get('name')}")
    print(f"  机器人    {ROBOT_IP}")
    print(f"  本机IP    {LAN_IP}  （client 的 --model-address 指向 ws://{LAN_IP}:8001）")
    print(f"  模式      {'演练（不动机器人）' if a.dry_run else '真跑'}")
    print("=" * 62)

    # 前置检查
    try:
        h = http_json(f"http://127.0.0.1:{SAY_PORT}/", timeout=4)
        print(f"  锁臂中间层  ✅ 当前 instruction={h.get('now')!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  锁臂中间层  ❌ {e}\n  先启动：python3 锁臂.py --lock right")
        return 1
    try:
        j = joints()
        print(f"  机器人数据  ✅ 左J1={j.get('left_arm_joint1', 0):+.3f} "
              f"升降={j.get('lift_joint', 0):.3f}")
    except Exception as e:  # noqa: BLE001
        print(f"  机器人数据  ❌ {e}")

    _r = Runner(flow, a.dry_run, a.stop_at)
    _r.hold = bool(a.hold)
    ok = _r.run()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
