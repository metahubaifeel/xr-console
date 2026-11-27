#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级棒棒糖 · 量子1号 Pro 通用控制台
====================================
浏览器打开 http://localhost:8900

功能
  · 实时画面：前方/桌面视角 + 头部视角（腕部相机被感知栈占用，暂不可取）
  · 通用指令：任意英文 instruction 自由下发
  · 原子动作：把官方 33 个原子动作翻译成英文按钮，点一下就发
  · 停止回零、实时日志

原理
  走官方 Portainer API (admin/xsquarerobot) 在 client 容器里 exec，
  不需要 SSH。相机用 v4l2 直采 MJPG（免转码）。

用法
  python3 控制台.py                 # 默认 10.230.20.13:8900
  python3 控制台.py --ip 10.230.20.13 --port 8900
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from scan_flow import ScanFlow

# Robot services are on the local network; never route them through a web proxy.
_robot_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------- 配置 ----------------
ROBOT_IP = "10.230.20.13"
PORTAINER_PORT = 9000
ENDPOINT_ID = 3
CONTAINER_ID = "05c14da94c70"       # ex001_client-robot_client-1
PERCEPTION_CID = ""                # ex001_client-robot_perception_ops-1（启动时自动查）
PORTAINER_USER = "admin"
PORTAINER_PASS = "xsquarerobot"
LOG_PATH = "/tmp/l0.log"
HELPER = "/tmp/d3.sh"
STOP_SH = "/tmp/stop.sh"

# 相机：device -> 标签（实测可用的排前面）
CAMS = {8: "头部前方 RGBD（看得到两只夹爪）",
        0: "BEV-1 机身旁（看全局避障）",
        6: "BEV-2 机身旁（看全局避障）"}
# 腕部相机 video2/4 被感知栈独占，取不到；video0 时好时坏，暂不展示
CAM_RES = {8: [(1280, 960), (1280, 720), (640, 480)],
           0: [(1600, 1200), (1280, 720)],
           6: [(1600, 1200), (1280, 720)]}
WRIST = {"wa": "/tmp/wrist_a.jpg", "wb": "/tmp/wrist_b.jpg"}

# API key
KEY = ""
_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api.env")
if os.path.exists(_ENV):
    for _line in open(_ENV, encoding="utf-8"):
        if _line.startswith("WALLX_API_KEY="):
            KEY = _line.split("=", 1)[1].strip()
KEY = os.environ.get("WALLX_API_KEY", KEY)

_ep = f"http://{ROBOT_IP}:{PORTAINER_PORT}/api"
_jwt = {"token": None, "ts": 0}
_lock = threading.Lock()


# ---------------- Portainer ----------------
def _request(path, payload=None, timeout=20):
    url = _ep + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if _jwt["token"]:
        req.add_header("Authorization", "Bearer " + _jwt["token"])
    with _robot_http.open(req, timeout=timeout) as r:
        return r.read()


def token(force=False):
    with _lock:
        if _jwt["token"] and not force and time.time() - _jwt["ts"] < 600:
            return _jwt["token"]
        raw = _request("/auth", {"Username": PORTAINER_USER, "Password": PORTAINER_PASS})
        _jwt["token"] = json.loads(raw)["jwt"]
        _jwt["ts"] = time.time()
        return _jwt["token"]


def _demux(raw):
    """按 Docker 8字节多路复用头正确拆包：[type][0*3][size(4BE)][payload]"""
    out, i, n = [], 0, len(raw)
    while i + 8 <= n:
        st = raw[i]
        if st not in (0, 1, 2):
            out.append(raw[i:])
            break
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        if size <= 0 or i + 8 + size > n:
            out.append(raw[i + 8:])
            break
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i + 8 >= n and i < n:
        pass
    return b"".join(out) if out else raw


def _exec(cmd_list, detach=False, timeout=30, retries=2, cid=None):
    body = json.dumps({"Cmd": cmd_list, "AttachStdout": True,
                       "AttachStderr": True, "Tty": False}).encode()
    for attempt in range(1, retries + 1):
        try:
            token(force=(attempt > 1))
            req = urllib.request.Request(
                f"{_ep}/endpoints/{ENDPOINT_ID}/docker/containers/{cid or CONTAINER_ID}/exec",
                data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", "Bearer " + _jwt["token"])
            with _robot_http.open(req, timeout=15) as r:
                eid = json.loads(r.read())["Id"]

            req2 = urllib.request.Request(
                f"{_ep}/endpoints/{ENDPOINT_ID}/docker/exec/{eid}/start",
                data=json.dumps({"Detach": detach, "Tty": False}).encode(), method="POST")
            req2.add_header("Content-Type", "application/json")
            req2.add_header("Authorization", "Bearer " + _jwt["token"])
            with _robot_http.open(req2, timeout=timeout) as r:
                raw = r.read()
            if detach:
                return b""
            return _demux(raw)
        except Exception as e:  # noqa: BLE001
            if attempt >= retries:
                return f"[控制台错误] {type(e).__name__}: {e}".encode()
    return b""


def shell(cmd, detach=False, timeout=30, retries=2, cid=None):
    return _exec(["bash", "-lc", cmd], detach=detach, timeout=timeout, retries=retries, cid=cid)


def shell_text(cmd, detach=False, timeout=30, retries=2):
    out = shell(cmd, detach=detach, timeout=timeout, retries=retries)
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else out


# ---------------- 相机 ----------------
_snap_cache = {}
_snap_lock = threading.Lock()
_cap_lock = threading.Lock()   # 取图全局串行，避免 v4l2 抢设备


def _wrist_raw(dev):
    """腕部相机：从感知容器读常驻守护写下的最新 JPEG"""
    if not PERCEPTION_CID:
        return None
    raw = shell(f"base64 -w0 {WRIST[dev]} 2>/dev/null", timeout=15, cid=PERCEPTION_CID)
    if isinstance(raw, str):
        raw = raw.encode()
    if b"/9j/" not in raw:
        return None
    txt = raw.decode("utf-8", "replace")
    i = txt.find("/9j/")
    s2 = re.sub(r"[^A-Za-z0-9+/=]", "", txt[i:]).rstrip("=")
    s2 += "=" * (-len(s2) % 4)
    try:
        data = base64.b64decode(s2)
    except Exception:  # noqa: BLE001
        return None
    j = data.rfind(b"\xff\xd9")
    if j < 0 or len(data) < 2000:
        return None
    return data[:j + 2]


_cam_cache = {}


def cam_worker():
    """后台常驻抓帧：每轮把 3 个相机各抓一次进缓存，HTTP 请求直接读缓存"""
    while True:
        for dev in list(CAMS):
            try:
                d = _snapshot_raw(dev)
                if d:
                    _cam_cache[dev] = d
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
        time.sleep(0.8)


ROBOT_API = f"http://{ROBOT_IP}:8898"


def robot_get(path, timeout=8):
    """直连机器人上的数据服务（比 Portainer exec 快 10 倍）"""
    try:
        with _robot_http.open(ROBOT_API + path, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def snapshot(dev):
    """优先返回后台缓存；没缓存时才现场抓一次"""
    c = _cam_cache.get(dev)
    if c:
        return c
    return _snapshot_raw(dev)


def _snapshot_live(dev):
    """从 /dev/videoN 取一帧 JPEG。带缓存 + 全局串行 + 重试。"""
    now = time.time()
    with _snap_lock:
        c = _snap_cache.get(dev)
        if c and now - c[0] < 0.8:
            return c[1]
    with _cap_lock:
        data = _snapshot_raw(dev)
    if data:
        with _snap_lock:
            _snap_cache[dev] = (now, data)
    return data


def _snapshot_raw(dev):
    if dev in WRIST:
        return _wrist_raw(dev)
    raw = b""
    got = None
    for _try in range(2):
        for (w, h) in CAM_RES.get(dev, [(1280, 720), (640, 480)]):
            raw = shell(
                f"v4l2-ctl -d /dev/video{dev} --set-fmt-video=width={w},height={h},"
                f"pixelformat=MJPG --stream-mmap --stream-count=1 "
                f"--stream-to=/tmp/snap{dev}.jpg 2>/dev/null; "
                f"base64 -w0 /tmp/snap{dev}.jpg 2>/dev/null",
                timeout=20)
            if isinstance(raw, str):
                raw = raw.encode()
            if b"/9j/" in raw:
                got = raw
                break
        if got:
            break
        time.sleep(0.4 + 0.2 * _try)
    if not got:
        return None
    raw = got
    txt = raw.decode("utf-8", "replace")
    i = txt.find("/9j/")
    if i < 0:
        return None
    s = re.sub(r"[^A-Za-z0-9+/=]", "", txt[i:]).rstrip("=")
    s += "=" * (-len(s) % 4)
    try:
        data = base64.b64decode(s)
    except Exception:  # noqa: BLE001
        return None
    j = data.rfind(b"\xff\xd9")
    if j < 0:
        return None
    data = data[:j + 2]
    if len(data) < 2000:
        return None
    return data


# ---------------- 机器人动作 ----------------
# 车身权限档位：名称 -> return_body
BODY_MODES = {
    "arms":  {"label": "只动双臂（最安全）", "body": {"car_pose": False, "head_pose": False, "lift": False}},
    "lift":  {"label": "双臂 + 升降台", "body": {"car_pose": False, "head_pose": False, "lift": True}},
    "look":  {"label": "双臂 + 升降 + 头部俯仰 ⭐推荐", "body": {"car_pose": False, "head_pose": True, "lift": True}},
    "full":  {"label": "⭐ 全权限（底盘+升降+头部，能跑会弯腰）", "body": {"car_pose": True, "head_pose": True, "lift": True}},
}


def push_params(mode="full", use_agent=False):
    """把 return_body 和 infer_params 写进容器，交给 d3.sh 读"""
    m = BODY_MODES.get(mode, BODY_MODES["full"])
    body = json.dumps(m["body"], separators=(",", ":"))
    async_params = {"algorithm": "sync", "api_key": KEY}
    if use_agent:
        async_params["use_agent"] = True
    params = {
        "action_horizon": 32,
        "control_dt": 0.005,
        "interpolate_multiplier": 7,
        "async_params": async_params,
    }
    for path, obj in (("/tmp/body.json", body),
                      ("/tmp/infer_params.json", json.dumps(params, separators=(",", ":")))):
        b = base64.b64encode(obj.encode()).decode()
        shell(f"echo '{b}' | base64 -d > {path}")
    tag = "agent" if use_agent else "裸API"
    return f"{m['label']} / {tag}"


def run_instruction(instr, mode="full", use_agent=False):
    tag = push_params(mode, use_agent)
    safe = instr.replace("\\", "\\\\").replace('"', '\\"')
    shell(f'setsid nohup bash {HELPER} "{safe}" > /tmp/d3_run.log 2>&1 < /dev/null &',
          detach=True)
    return f"[{tag}] {instr}"


def stop_all():
    return shell_text(f"bash {STOP_SH} 2>&1", timeout=120, retries=1).strip() or "已停止"


def read_log(n=60):
    return shell_text(f"tail -{n} {LOG_PATH} 2>/dev/null || echo '(还没有日志)'")


_st_cache = {"t": 0, "v": None}


def gripper_state():
    """夹爪读数：优先机器人直连"""
    d = robot_get("/gripper", timeout=5)
    if d:
        try:
            o = json.loads(d)
            if "left" in o:
                return o
        except Exception:  # noqa: BLE001
            pass
    if not PERCEPTION_CID:
        return None
    try:
        t = shell("cat /tmp/gripper.txt 2>/dev/null", timeout=10, cid=PERCEPTION_CID).decode("utf-8", "replace").strip()
        if not t or "," not in t:
            return None
        l, r = t.split(",")[:2]
        return {"left": float(l), "right": float(r)}
    except Exception:  # noqa: BLE001
        return None


def status():
    out = shell_text(
        "P=$(pgrep -f 'start_ex001[.]py' | head -1); "
        "if [ -n \"$P\" ]; then ps -o etime= -p $P; else echo ''; fi")
    up = out.strip()
    return {"running": bool(up), "uptime": up, "gripper": gripper_state()}


# ---------------- 容器内脚本 ----------------
_D3_SH = r'''#!/bin/bash
# 下发一句英文 instruction：bash /tmp/d3.sh "<instruction>"
KEY='__KEY__'
SCRIPT=/opt/xr/bot/x2robot_client/share/x2robot_client/scripts/start_ex001.sh
PIDF=/tmp/xr.pid
PAT='start_ex001[.]py'
if pgrep -f "$PAT" >/dev/null; then
  kill $(pgrep -f "$PAT") 2>/dev/null
  for i in $(seq 1 30); do pgrep -f "$PAT" >/dev/null || break; sleep 1; done
  pkill -9 -f "$PAT" 2>/dev/null
fi
rm -f "$PIDF"
setsid nohup bash $SCRIPT \
  --attach \
  --model-address wss://maas-api.x2robot.com/v1/vla/wallx \
  --infer-type sync \
  --instruction "$1" \
  --embodiment quanta_x1_pro/gripper-g \
  --return-body "$(cat /tmp/body.json 2>/dev/null || echo '{"car_pose":false,"head_pose":false,"lift":false}')" \
  --infer-params "$(cat /tmp/infer_params.json 2>/dev/null || echo "{\"action_horizon\":32,\"control_dt\":0.005,\"interpolate_multiplier\":7,\"async_params\":{\"algorithm\":\"sync\",\"api_key\":\"$KEY\"}}")" \
  > /tmp/l0.log 2>&1 < /dev/null &
echo $! > "$PIDF"
'''

_STOP_SH = r'''#!/bin/bash
# 只停会话，不动手臂（方便人先取下手里的东西）
PAT='start_ex001[.]py'
if pgrep -f "$PAT" >/dev/null; then
  kill $(pgrep -f "$PAT") 2>/dev/null
  for i in $(seq 1 12); do pgrep -f "$PAT" >/dev/null || break; sleep 1; done
  pkill -9 -f "$PAT" 2>/dev/null
  echo "会话已停止（手臂保持原位，夹爪保持开合）"
else
  echo "没有正在跑的会话"
fi
rm -f /tmp/xr.pid
'''


_HOME_SH = r'''#!/bin/bash
# 停止会话 + 正规归零
PAT='start_ex001[.]py'
if pgrep -f "$PAT" >/dev/null; then
  kill $(pgrep -f "$PAT") 2>/dev/null
  for i in $(seq 1 12); do pgrep -f "$PAT" >/dev/null || break; sleep 1; done
  pkill -9 -f "$PAT" 2>/dev/null
fi
rm -f /tmp/xr.pid
sleep 1
source /opt/xr/bot/x2robot_client/share/x2robot_client/scripts/session_env.sh >/dev/null 2>&1
OUT=$(python3 -m x2robot_client.zero_arm --timeout 90 2>&1)
if echo "$OUT" | grep -q "homing complete"; then
  echo "已归零（注意：夹爪里的东西还在）"
else
  echo "归零未确认："
  echo "$OUT" | tail -3
fi
'''


def ensure_scripts():
    if not KEY:
        return "⚠️ 没读到 WALLX_API_KEY"
    parts = []
    for path, body in ((HELPER, _D3_SH.replace("__KEY__", KEY)), (STOP_SH, _STOP_SH), ("/tmp/home.sh", _HOME_SH)):
        b = base64.b64encode(body.encode()).decode()
        parts.append(f"echo '{b}' | base64 -d > {path} && chmod +x {path}")
    return shell_text(" && ".join(parts) + " && bash -n /tmp/d3.sh && echo '脚本就绪'").strip()


# ---------------- 原子动作 → 英文按钮 ----------------
# (按钮文字, 英文 instruction, 原子动作代码)
GROUPS = [
    ("抓取 · G", [
        ("抓前面的东西", "pick up the object in front of you", "G1"),
        ("抓圆柱/瓶子", "grab the bottle and hold it", "G1"),
        ("抓薄片/易滑件", "pick up the flat transparent object", "G2"),
        ("捏起小零件", "pinch the small part with the gripper", "G3"),
        ("松手放下", "open the gripper and release the object", "P1"),
    ]),
    ("移动 · M", [
        ("往前伸", "move both arms forward", "M1"),
        ("往左移", "move both arms to the left", "M1"),
        ("往右移", "move both arms to the right", "M1"),
        ("抬高一点", "lift both arms up", "M1"),
        ("靠近目标", "move the arm closer to the target", "M3"),
        ("精细对准", "align the gripper precisely above the hole", "M4"),
        ("双臂退开", "move both arms back to a rest position", "M1"),
    ]),
    ("放置 · P", [
        ("放到位置上", "place the object on the target position", "P2"),
        ("放进坑/孔里", "place the object into the hole", "P2"),
        ("叠上去", "stack the object on top of the other one", "P3"),
        ("往下压到位", "push the object down into the hole", "F1"),
    ]),
    ("力控 · F", [
        ("推一下", "push the object forward", "F2"),
        ("拉出来", "pull the object towards you", "F2"),
        ("按住不动", "press and hold the object", "F1"),
        ("拧转", "rotate the gripper to turn the object", "F5"),
        ("擦/抹一遍", "wipe the surface with the gripper", "F3"),
    ]),
    ("双臂 · C", [
        ("双手抱住搬走", "hold the big object with both arms and move it", "C1"),
        ("交给另一只手", "hand the object to the other arm", "C2"),
        ("一手拿一手操作", "hold the bottle with the left arm and open it with the right", "C3"),
    ]),
    ("记忆/交互 · M_Mem/H", [
        ("找刚才那个", "find and pick up the object you saw before", "M_Mem1"),
        ("被盖住也要拿", "pick up the object even if it is covered", "M_Mem2"),
        ("接我递的东西", "take the object from the human hand", "H1"),
        ("抓移动的物体", "catch the moving object", "H2"),
    ]),
    ("取证 / 标定", [
        ("看桌面", "look at the table in front of you", "—"),
        ("回到初始位", "move both arms back to a rest position", "—"),
    ]),
]


# ---------------- 扫码枪 ----------------
SCAN_JSON = "/tmp/last_scan.json"
SCAN_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_rules.json")


def last_scan():
    try:
        with open(SCAN_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def stop_inference():
    """Stop inference only. Physical pose retention still requires hardware validation."""
    return shell_text("PAT='start_ex001[.]py'; "
                      "P=$(pgrep -f \"$PAT\"); "
                      "if [ -n \"$P\" ]; then kill $P 2>/dev/null; fi; "
                      "for i in $(seq 1 12); do pgrep -f \"$PAT\" >/dev/null || exit 0; sleep 1; done; "
                      "exit 1", timeout=20, retries=1)


def load_rules():
    try:
        with open(SCAN_RULES, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"default": "put it into the red square", "map": {}}


FLOW = ScanFlow(lambda instruction: run_instruction(instruction, "full", False),
                stop_inference, last_scan, load_rules)


def save_rules(obj):
    with open(SCAN_RULES, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def start_scanner():
    """在本机拉起扫码枪守护（HID 键盘型）"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "扫码枪.py")
    if not os.path.exists(script):
        return "无 扫码枪.py"
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                args = f.read().split(b'\0')
            if any(os.fsencode(script) == arg for arg in args) and b'--daemon' in args:
                return "已运行"
        except OSError:
            pass
    try:
        subprocess.Popen([sys.executable, script, "--daemon"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return "已启动"
    except Exception as e:  # noqa: BLE001
        return f"启动失败 {e}"


# ---------------- 数字孪生 ----------------
_KIN = {"data": None}
URDF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "urdf", "ex001_robot.urdf")


def kinematics():
    """解析 URDF -> 关节树（只取运动学，不含网格）"""
    if _KIN["data"] is not None:
        return _KIN["data"]
    import xml.etree.ElementTree as ET
    root = ET.parse(URDF_PATH).getroot()
    joints = []
    for j in root.findall("joint"):
        o = j.find("origin")
        xyz = [float(x) for x in (o.get("xyz", "0 0 0") if o is not None else "0 0 0").split()]
        rpy = [float(x) for x in (o.get("rpy", "0 0 0") if o is not None else "0 0 0").split()]
        ax = j.find("axis")
        axis = [float(x) for x in (ax.get("xyz", "0 0 1") if ax is not None else "0 0 1").split()]
        lim = j.find("limit")
        joints.append({
            "name": j.get("name"), "type": j.get("type"),
            "parent": j.find("parent").get("link"),
            "child": j.find("child").get("link"),
            "xyz": xyz, "rpy": rpy, "axis": axis,
            "limit": ([float(lim.get("lower", -3.2)), float(lim.get("upper", 3.2))]
                      if lim is not None else None),
        })
    children = {j["child"] for j in joints}
    roots = [l.get("name") for l in root.findall("link") if l.get("name") not in children]
    _KIN["data"] = {"joints": joints, "root": roots[0] if roots else None}
    return _KIN["data"]


def joint_values():
    if not PERCEPTION_CID:
        return {}
    try:
        t = shell("cat /tmp/joints.json 2>/dev/null", timeout=10, cid=PERCEPTION_CID)
        d = json.loads(t or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


TWIN_PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>超级棒棒糖 · 3D 孪生</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#0d1016;color:#e6e8ee;overflow:hidden;
  font:13px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
#c{display:block;width:100%;height:100%}
#hud{position:fixed;top:12px;left:12px;background:#161a22ee;border:1px solid #29303e;
  border-radius:10px;padding:10px 14px;font-size:12px;line-height:1.8;pointer-events:none;min-width:210px}
#hud b{color:#8fc2ff;font-size:13px}
.row{display:flex;justify-content:space-between;gap:18px}
.row span:first-child{color:#7b849b}
.row span:last-child{color:#dfe4ec;font-variant-numeric:tabular-nums}
#hint{position:fixed;bottom:12px;left:0;right:0;text-align:center;color:#4d5568;font-size:11px}
#err{position:fixed;top:12px;right:12px;max-width:420px;color:#ff9ba5;font-size:11px;
  background:#2a1216ee;border:1px solid #5c232b;border-radius:8px;padding:8px 12px;display:none;white-space:pre-wrap}
</style></head><body>
<canvas id=c></canvas>
<div id=hud>
  <b>3D 孪生</b> <span id=st style="color:#7b849b">连接中…</span>
  <div class=row><span>升降</span><span id=v_lift>--</span></div>
  <div class=row><span>左夹爪</span><span id=v_lg>--</span></div>
  <div class=row><span>右夹爪</span><span id=v_rg>--</span></div>
  <div class=row><span>左臂 J1/J4</span><span id=v_la>--</span></div>
  <div class=row><span>右臂 J1/J4</span><span id=v_ra>--</span></div>
</div>
<div id=err></div>
<div id=dbg style="display:none"></div>
<div id=hint>左键旋转 · 滚轮缩放 · 右键平移　|　数据来自 /ex001/joint_states</div>
<script src="/web/three.min.js"></script>
<script>
const errBox = document.getElementById('err');
function showErr(m){ errBox.style.display='block'; errBox.textContent += m + '\n'; }
addEventListener('error', e => showErr('JS错误: ' + e.message));

const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({canvas, antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1016);
scene.fog = new THREE.Fog(0x0d1016, 3.5, 8);
const camera = new THREE.PerspectiveCamera(42, 1, 0.02, 60);

scene.add(new THREE.HemisphereLight(0xd8e4ff, 0x1a1e28, 1.15));
const d1 = new THREE.DirectionalLight(0xffffff, 1.25); d1.position.set(2.5, 4, 2.5); scene.add(d1);
const d2 = new THREE.DirectionalLight(0x88aaff, .45); d2.position.set(-3, 1.5, -2.5); scene.add(d2);

const grid = new THREE.GridHelper(8, 32, 0x2b3446, 0x1b2029);
grid.position.y = 0; scene.add(grid);

/* ---- 相机控制 ---- */
const ctl = {az: 1.15, el: 0.14, dist: 1.75, target: new THREE.Vector3(0.20, 0.82, 0)};
function applyCam(){
  const {az, el, dist, target} = ctl;
  camera.position.set(target.x + dist*Math.cos(el)*Math.sin(az),
                      target.y + dist*Math.sin(el),
                      target.z + dist*Math.cos(el)*Math.cos(az));
  camera.lookAt(target);
}
let drag = null;
canvas.addEventListener('mousedown', e => drag = {x:e.clientX, y:e.clientY, b:e.button});
addEventListener('mouseup', () => drag = null);
addEventListener('mousemove', e => {
  if (!drag) return;
  const dx = e.clientX-drag.x, dy = e.clientY-drag.y;
  drag.x = e.clientX; drag.y = e.clientY;
  if (drag.b === 0){ ctl.az -= dx*0.008; ctl.el = Math.max(-1.25, Math.min(1.35, ctl.el+dy*0.006)); }
  else { ctl.target.x -= dx*0.0018; ctl.target.y += dy*0.0018; }
  applyCam();
});
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  ctl.dist = Math.max(0.6, Math.min(8, ctl.dist*(1 + Math.sign(e.deltaY)*0.1)));
  applyCam();
}, {passive:false});
canvas.addEventListener('contextmenu', e => e.preventDefault());

/* ---- 只画这些链 ---- */
const CHAINS = [
  {prefix:'left_arm',  color:0x5b8dd6, radius:0.026},
  {prefix:'right_arm', color:0xe08a4a, radius:0.026},
  {prefix:'head',      color:0x56c596, radius:0.020},
];
function chainOf(name){
  if (name === 'lift_joint') return {color:0xb08ccf, radius:0.034};
  for (const c of CHAINS) if (name.startsWith(c.prefix)) return c;
  return null;
}

let KIN = null;
const objs = {};        // link -> Object3D
const rot  = {};        // joint -> Object3D (承载关节值)
const info = {};        // joint -> 渲染信息

/* ---- 建树（全部关节都建，保证 FK 正确；只给会动的画形体） ---- */
function buildTree(kin){
  while (scene.children.length > 4) scene.remove(scene.children[scene.children.length-1]);
  for (const k in objs) delete objs[k];
  for (const k in rot)  delete rot[k];
  for (const k in info) delete info[k];

  const rootObj = new THREE.Object3D();
  rootObj.name = kin.root;
  rootObj.rotation.x = -Math.PI / 2;   // URDF 是 Z-up，three.js 是 Y-up
  scene.add(rootObj); objs[kin.root] = rootObj;

  // URDF 里的 joint 不是拓扑排序的，必须多轮建树
  let rest = kin.joints.slice();
  for (let pass = 0; pass < 12 && rest.length; pass++){
    const next = [];
    for (const j of rest){
      const p = objs[j.parent];
      if (!p){ next.push(j); continue; }
      const g = new THREE.Object3D();
      g.position.set(j.xyz[0], j.xyz[1], j.xyz[2]);
      g.rotation.set(j.rpy[0], j.rpy[1], j.rpy[2], 'ZYX');
      p.add(g);
      const r = new THREE.Object3D(); g.add(r);
      const l = new THREE.Object3D(); l.name = j.child; r.add(l);
      objs[j.child] = l; rot[j.name] = r;

      const movable = (j.type === 'revolute' || j.type === 'continuous' || j.type === 'prismatic');
      const ch = chainOf(j.name);
      if (movable && ch) info[j.name] = {ch, joint:j};
    }
    if (next.length === rest.length) {   // 卡住了，放弃剩余
      showErr('未能挂载 ' + rest.length + ' 个关节: ' + rest.slice(0,3).map(j=>j.name).join(','));
      break;
    }
    rest = next;
  }
  window.__KIN = kin;
}

/* ---- 找到每个会动关节的「上一个会动关节」，用于画连杆 ---- */
function buildLinks(){
  const kin = window.__KIN;
  const links = [];
  const parentOf = {};
  for (const j of kin.joints) parentOf[j.child] = j;

  for (const name in info){
    const j = info[name].joint;
    // 沿父链上溯，找最近的会动关节
    let cur = j.parent, up = null, guard = 0;
    while (cur && guard++ < 60){
      const pj = parentOf[cur];
      if (!pj) break;
      if (info[pj.name]) { up = pj.name; break; }
      cur = pj.parent;
    }
    links.push({from: up, to: name});
  }
  window.__LINKS = links;
  LINKS_MAP = {};
  for (const L of links) if (L.from) LINKS_MAP[L.to] = L.from;
  return links;
}

/* ---- 渲染体 ---- */
const cylGeo = new THREE.CylinderGeometry(1, 1, 1, 14, 1, false);
const sphGeo = new THREE.SphereGeometry(1, 16, 12);
const meshes = [];
const staticLinks = [];

function buildMeshes(){
  for (const m of meshes) scene.remove(m);
  meshes.length = 0;

  for (const name in info){
    const {ch} = info[name];
    const col = new THREE.MeshStandardMaterial({color:ch.color, metalness:.35, roughness:.45});
    const ball = new THREE.Mesh(sphGeo, col);
    ball.scale.setScalar(ch.radius*0.85);
    scene.add(ball); meshes.push(ball);
    info[name].ball = ball;
  }
  // 静态立柱：底盘原点 -> 起步关节（每帧在 applyValues 里更新）
  const staticMat = new THREE.MeshStandardMaterial({color:0x3b4256, metalness:.4, roughness:.6});
  staticLinks.length = 0;
  for (const L of window.__LINKS){
    if (L.from) continue;
    const m = new THREE.Mesh(cylGeo, staticMat);
    scene.add(m); meshes.push(m);
    staticLinks.push({joint: L.to, mesh: m});
  }
  for (const L of window.__LINKS){
    if (!L.from) continue;
    const {ch} = info[L.to];
    const col = new THREE.MeshStandardMaterial({color:ch.color, metalness:.35, roughness:.45});
    const m = new THREE.Mesh(cylGeo, col);
    scene.add(m); meshes.push(m);
    info[L.to].link = m;
  }

  // 底盘 + 升降柱
  const baseMat = new THREE.MeshStandardMaterial({color:0x2e3648, metalness:.3, roughness:.75});
  const base = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.10, 0.36), baseMat);
  base.position.set(0, 0.05, 0); scene.add(base); meshes.push(base);

  // 桌面
  const tMat = new THREE.MeshStandardMaterial({color:0x232c3d, metalness:.05, roughness:.9,
                transparent:true, opacity:.55});
  const table = new THREE.Mesh(new THREE.BoxGeometry(0.80, 0.025, 1.55), tMat);
  table.position.set(0.62, 0.74, 0); scene.add(table); meshes.push(table);
  const edge = new THREE.LineSegments(
    new THREE.EdgesGeometry(table.geometry),
    new THREE.LineBasicMaterial({color:0x3f4c66}));
  edge.position.copy(table.position); scene.add(edge); meshes.push(edge);
}

/* ---- 每帧更新 ---- */
const _Q = new THREE.Quaternion(), _UP = new THREE.Vector3(0,1,0);
const _P = {};
let LINKS_MAP = {};

function applyValues(vals){
  let n = 0;
  for (const name in info){
    const joint = info[name].joint;
    const v = vals[name];
    if (v === undefined) continue;
    n++;
    if (joint.type === 'prismatic'){
      rot[name].position.set(joint.axis[0]*v, joint.axis[1]*v, joint.axis[2]*v);
    } else {
      _Q.setFromAxisAngle(new THREE.Vector3(joint.axis[0], joint.axis[1], joint.axis[2]), v);
      rot[name].quaternion.copy(_Q);
    }
  }
  scene.updateMatrixWorld(true);

  for (const name in info){
    const v = new THREE.Vector3();
    rot[name].getWorldPosition(v);
    _P[name] = v;
  }
  try {
    const o = {};
    for (const k in _P) o[k] = _P[k].toArray().map(x=>+x.toFixed(3));
    document.getElementById('dbg').textContent = JSON.stringify(o);
  } catch(e){}
  // 静态立柱：从底盘原点连到起步关节
  for (const sl of staticLinks){
    const P = _P[sl.joint];
    if (!P){ sl.mesh.visible = false; continue; }
    const len = P.length();
    if (len < 0.03){ sl.mesh.visible = false; continue; }
    sl.mesh.visible = true;
    sl.mesh.position.copy(P).multiplyScalar(0.5);
    sl.mesh.quaternion.setFromUnitVectors(_UP, P.clone().normalize());
    sl.mesh.scale.set(0.048, len, 0.048);
  }
  for (const name in info){
    const A = _P[name];
    info[name].ball.position.copy(A);
    const m = info[name].link;
    const fromName = LINKS_MAP[name];
    if (!m || !fromName || !_P[fromName]) continue;
    const B = _P[fromName];
    const len = A.distanceTo(B);
    if (len < 0.01){ m.visible = false; continue; }
    m.visible = true;
    m.position.addVectors(A, B).multiplyScalar(0.5);
    const D = new THREE.Vector3().subVectors(A, B).normalize();
    m.quaternion.setFromUnitVectors(_UP, D);
    m.scale.set(info[name].ch.radius*0.62, len, info[name].ch.radius*0.62);
  }
  return n;
}

/* ---- 数据 ---- */
let cur = {};
async function tick(){
  try {
    const r = await fetch('/joints', {cache:'no-store'});
    const d = await r.json();
    const n = Object.keys(d || {}).length;
    if (n){
      cur = d;
      const got = applyValues(d);
      document.getElementById('st').textContent = '实时 · ' + got + ' 关节';
      document.getElementById('st').style.color = '#4ade80';
      const f = (x, k=3) => (x === undefined ? '--' : Number(x).toFixed(k));
      document.getElementById('v_lift').textContent = f(d.lift_joint) + ' m';
      document.getElementById('v_lg').textContent = f(d.left_arm_gripper, 3) + ' rad';
      document.getElementById('v_rg').textContent = f(d.right_arm_gripper, 3) + ' rad';
      document.getElementById('v_la').textContent =
        f(d.left_arm_joint1,2) + ' / ' + f(d.left_arm_joint4,2);
      document.getElementById('v_ra').textContent =
        f(d.right_arm_joint1,2) + ' / ' + f(d.right_arm_joint4,2);
    } else {
      document.getElementById('st').textContent = '等待关节数据…';
    }
  } catch(e){
    document.getElementById('st').textContent = '读取失败';
    document.getElementById('st').style.color = '#ff9ba5';
  }
  setTimeout(tick, 120);
}

/* ---- 启动 ---- */
function resize(){
  renderer.setSize(innerWidth, innerHeight, false);
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
}
addEventListener('resize', resize); resize(); applyCam();

(async () => {
  try {
    KIN = await (await fetch('/twin.json', {cache:'no-store'})).json();
    buildTree(KIN);
    buildLinks();
    buildMeshes();
    if (KIN.values) applyValues(KIN.values);
    tick();
  } catch(e){ showErr('初始化失败: ' + e.message); }
})();

(function loop(){ requestAnimationFrame(loop); renderer.render(scene, camera); })();
</script></body></html>
"""

# ---------------- 页面 ----------------
PAGE = r"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>超级棒棒糖 · 控制台</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1116;color:#e6e8ee}
header{padding:12px 18px;background:#171a22;border-bottom:1px solid #262b38;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:9}
h1{font-size:15px;margin:0;font-weight:600}
.tag{font-size:12px;padding:3px 10px;border-radius:99px;background:#232833;color:#8b93a7}
.tag.on{background:#123a24;color:#4ade80}.tag.off{background:#232833;color:#8b93a7}
.wrap{display:grid;grid-template-columns:1.15fr 1fr;gap:16px;padding:16px;max-width:1720px;margin:0 auto}
@media(max-width:1100px){.wrap{grid-template-columns:1fr}}
.cams{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cam{background:#161922;border:1px solid #262b38;border-radius:12px;overflow:hidden;position:relative}
.cam .lbl{position:absolute;top:8px;left:10px;font-size:11px;background:#000a;padding:2px 8px;border-radius:6px;color:#cfd6e6}
.cam img{display:block;width:100%;height:auto;min-height:150px;object-fit:cover;background:#0b0d12}
.cam .ph{display:flex;align-items:center;justify-content:center;height:170px;color:#5a6376;font-size:12px}
section{margin-bottom:18px}
h2{font-size:12px;color:#8b93a7;font-weight:600;letter-spacing:.6px;text-transform:uppercase;margin:0 0 9px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
button.cmd{background:#1b2029;color:#dfe4ec;border:1px solid #2e3543;border-radius:9px;padding:10px 11px;
  font-size:13px;font-weight:600;cursor:pointer;text-align:left;transition:.1s;line-height:1.3}
button.cmd:hover{background:#242c3c;border-color:#4a5c8a;transform:translateY(-1px)}
button.cmd .k{float:right;font-size:10px;color:#5b6784;font-weight:500;margin-left:6px}
button.cmd .e{display:block;font-weight:400;font-size:10.5px;color:#7c869c;margin-top:4px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button.stop{background:#3a191d;border:1px solid #6b2a31;color:#ffb4bb;border-radius:9px;padding:11px 20px;font-weight:600;cursor:pointer}
button.stop:hover{background:#552228}
.selects{display:flex;gap:10px;margin-bottom:9px;flex-wrap:wrap}
select{background:#1b2029;border:1px solid #2e3543;color:#dfe4ec;border-radius:9px;padding:11px 12px;font-size:13px;min-width:150px}
select:focus,input:focus{outline:none;border-color:#5b6ea8}
.row{display:flex;gap:9px;margin:9px 0;flex-wrap:wrap;align-items:center}
input[type=text]{flex:1;min-width:220px;background:#161922;border:1px solid #2e3543;color:#e6e8ee;border-radius:9px;padding:11px 13px;font-size:14px}
button.go{background:#1d4ed8;border:none;color:#fff;border-radius:9px;padding:11px 24px;font-weight:600;cursor:pointer}
button.go:hover{background:#2563eb}
button.ghost{background:#202632;border:1px solid #2e3543;color:#c8cede;border-radius:9px;padding:11px 16px;cursor:pointer}
pre{background:#0a0c11;border:1px solid #232832;border-radius:10px;padding:13px;font-size:12px;line-height:1.55;
  overflow:auto;max-height:40vh;white-space:pre-wrap;word-break:break-all;color:#9fb3d1;margin:0}
.msg{font-size:13px;color:#4ade80;margin:8px 0;min-height:19px}
.modes{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.mode{background:#1b2029;border:1px solid #2e3543;border-radius:9px;padding:11px 12px;cursor:pointer;
  font-size:13px;font-weight:600;color:#c3cad8;transition:.1s}
.mode small{display:block;font-weight:400;font-size:11px;color:#77809a;margin-top:4px}
.mode.on{background:#132a44;border-color:#3b6ea5;color:#8fc2ff}
.mode:hover{border-color:#4a5c8a}
.scanbox{background:#1b2029;border:1px solid #2e3543;border-radius:10px;padding:12px}
.scanline{display:flex;align-items:center;gap:10px;font-size:13px}
.scanlbl{color:#7b849b}
#scancode{font-family:ui-monospace,monospace;font-size:16px;color:#ffd479;letter-spacing:.5px}
button.mini{background:#242c3c;border:1px solid #333a4d;color:#9fb3d1;border-radius:7px;
  padding:5px 10px;font-size:11px;cursor:pointer;margin-left:auto}
.agent{display:flex;gap:9px;align-items:flex-start;margin-top:10px;padding:11px 12px;background:#1b2029;
  border:1px solid #2e3543;border-radius:9px;cursor:pointer;font-size:13px;color:#c3cad8}
.agent input{margin-top:2px;width:16px;height:16px;accent-color:#3b82f6}
.agent small{display:block;font-weight:400;font-size:11px;color:#77809a;margin-top:3px}
/* Field console: prioritize observation and the active task. */
:root{--surface:#151d2a;--line:#29374a;--accent:#61d6bb}
body{background:#0b1019;color:#e6edf6;font-size:14px}
header{background:#101824;border-color:var(--line);padding:14px 24px;box-shadow:0 6px 24px #0003}
h1{font-size:18px;letter-spacing:.4px}
.wrap{grid-template-columns:minmax(0,1.65fr) minmax(350px,1fr);gap:20px;padding:24px;max-width:1900px}
section{padding:18px;background:var(--surface);border:1px solid var(--line);border-radius:16px;margin-bottom:16px}
h2{color:#b7c8dd;font-size:14px;letter-spacing:0;margin-bottom:14px}
.cams{grid-template-columns:1fr 1fr;gap:12px}
.cam:first-child{grid-column:1/-1}
.cam:first-child img{max-height:58vh;object-fit:contain}
.cam img{min-height:0;aspect-ratio:4/3;object-fit:contain}
.cam .lbl{z-index:1;background:#08101bd9;border:1px solid #ffffff20;padding:5px 9px}
.cam .ph{height:110px}.cam:first-child .ph{height:320px}
button{font:inherit;min-height:42px;border:1px solid #384b62;border-radius:9px;background:#243449;color:#e4edf8;padding:9px 14px;cursor:pointer}
button:hover{filter:brightness(1.17)}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #61d6bb;outline-offset:3px}
button.go{background:#176a58}button.stop{background:#7b2633;color:#fff;border-color:#bc4655}
.flow-card{border-color:#368775;background:linear-gradient(145deg,#142d2d,#151d2a)}
.flow-card .row button:nth-child(2){background:#19775f;border-color:#4fc9aa}
#flowstate{margin:12px 0;border-color:#34564f;min-height:100px;color:#9ce0cc}
.timings{display:flex;gap:10px;flex-wrap:wrap;color:#a7b8cd;font-size:12px}
.timings label{flex:1}.timings input{display:block;width:100%;margin-top:5px;background:#0d1723;color:white;border:1px solid #3b5066;border-radius:7px;padding:8px}
.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mode{padding:9px;font-size:12px}.mode small{font-size:10px}
details>summary{cursor:pointer;color:#b7c8dd;font-weight:600;padding:4px 0}details[open]>summary{margin-bottom:12px}
#log{max-height:220px}small{color:#98acc3}.scanbox{background:#111c28}#scancode{font-size:25px}
@media(max-width:1000px){.wrap{grid-template-columns:1fr;padding:12px}header{padding:10px}.cam:first-child img{max-height:none}}
</style></head><body>
<header>
  <h1>BRIGHT / 扫码分拣工作台</h1>
  <span class=tag>10.230.20.13 · ex001-655 · 工位27</span>
  <span class=tag id=st>状态检测中…</span>
  <span class=tag id=gr>夹爪 -- / --</span>
  <a href="/twin" target=_blank style="margin-left:auto;color:#8fc2ff;text-decoration:none;
     border:1px solid #2e3543;border-radius:9px;padding:8px 14px;font-size:13px">🧊 3D 孪生</a>
  <button class=ghost onclick="stopOnly()">■ 停止（臂不动）</button>
  <button class=stop onclick="stopIt()">⌂ 停止 + 归零</button>
  <button onclick="holdStop()">停止推理（不归零）</button>
</header>
<div class=wrap>
  <div>
    <section><h2>现场观察 <span class=tag id=videoStats>采集状态检测中</span></h2><div class=cams id=cams></div></section>
    <section><h2>运行日志</h2><pre id=log>加载中…</pre></section>
  </div>
  <div>
    <section id=permissions>
      <h2>① 车身权限（决定它能不能弯腰/低头/走近）</h2>
      <div class=modes id=modes></div>
      <label class=agent><input type=checkbox id=agent> <b>use_agent = true</b>
        <small>官方 agent 编排层：模型自己拆步骤、多轮决策。不勾 = 裸 API，一句一个动作</small></label>
    </section>
    <section>
      <h2>② 自由指令（英文，一句一个动作）</h2>
      <div class=row>
        <input type=text id=instr placeholder="例如: place the product into the empty hole of the tray">
        <button class=go onclick="runCustom()">下发</button>
      </div>
      <div class=msg id=msg></div>
    </section>
    <section class=flow-card id=taskpanel>
      <h2>扫码分拣 / 自动一轮</h2>
      <div class=row>
        <button onclick="flowStart(true)">演练自动流程</button>
        <button onclick="flowStart(false)">真机自动一轮</button>
        <button onclick="flowCancel()">取消流程</button>
      </div>
      <div class=timings>
        <label>抓枪时限 / 秒<input id=graspSeconds type=number min=1 max=120 value=60></label>
        <label>等码时限 / 秒<input id=scanSeconds type=number min=1 max=120 value=40></label>
        <label>放置时限 / 秒<input id=placeSeconds type=number min=1 max=120 value=30></label>
      </div>
      <small>抓枪 → 对准等新码 → 按码放置。抓取及放置尚按时限切步，保持姿态待真机验证。</small>
      <pre id=flowstate style="white-space:pre-wrap">流程空闲</pre>
      <div class=scanbox>
        <div class=scanline>
          <span class=scanlbl>最近读到</span>
          <b id=scancode>--</b>
          <span id=scantime style="color:#7b849b;font-size:11px"></span>
          <button class=mini onclick="editRules()">规则</button>
        </div>
        <div class=row style="margin:8px 0 0">
          <input type=text id=scaninstr placeholder="扫码后建议下发的指令">
          <button class=go onclick="sendScan()">下发</button>
        </div>
      </div>
    </section>
    <section>
      <h2>③ 原子动作（官方33个 → 英文指令）</h2>
      <div class=selects>
        <select id=grp onchange="renderGroup()"></select>
      </div>
      <div class=grid id=grid></div>
    </section>
  </div>
</div>
<script>
const GROUPS = __GROUPS__;
const CAMS = __CAMS__;
const MODES = __MODES__;
const taskPanel=document.getElementById('taskpanel');
taskPanel.parentElement.prepend(taskPanel);
const permissions=document.getElementById('permissions');
const more=document.createElement('details');
const summary=document.createElement('summary');summary.textContent='高级设置 · 车身权限 / 实验模式';
more.appendChild(summary);while(permissions.firstChild)more.appendChild(permissions.firstChild);
permissions.appendChild(more);
let curMode = 'full';
const modesEl = document.getElementById('modes');
MODES.forEach(([k,label,hint]) => {
  const d = document.createElement('div');
  d.className = 'mode' + (k===curMode?' on':'');
  d.dataset.k = k;
  d.innerHTML = label + '<small>' + hint + '</small>';
  d.onclick = () => { curMode = k; document.querySelectorAll('.mode').forEach(x=>x.classList.toggle('on', x.dataset.k===k)); };
  modesEl.appendChild(d);
});
// 相机
const cams = document.getElementById('cams');
CAMS.forEach(([dev,label]) => {
  const d = document.createElement('div'); d.className='cam';
  d.innerHTML = `<div class=lbl>${label}</div><div class=ph id=ph${dev}>连接中…</div>` +
                `<img id=img${dev} style="display:none">`;
  cams.appendChild(d);
});
function refreshCam(dev){
  const img = document.getElementById('img'+dev), ph = document.getElementById('ph'+dev);
  const n = new Image();
  n.onload = () => { img.src = n.src; img.style.display='block'; if(ph) ph.style.display='none'; setTimeout(()=>refreshCam(dev),dev===8?65:250); };
  n.onerror = () => { if(ph) ph.textContent = '画面暂不可用，正在重试'; setTimeout(()=>refreshCam(dev),1500); };
  n.src = '/snap?d=' + dev + '&t=' + Date.now();
}
CAMS.forEach(([dev]) => refreshCam(dev));
// 分组
const grpSel = document.getElementById('grp');
GROUPS.forEach((g,i)=>{ const o=document.createElement('option'); o.value=i; o.textContent=g[0]; grpSel.appendChild(o); });
function renderGroup(){
  const g = GROUPS[grpSel.value]; const grid = document.getElementById('grid');
  grid.innerHTML='';
  g[1].forEach(([label,instr,code])=>{
    const b=document.createElement('button'); b.className='cmd';
    b.innerHTML = `<span class=k>${code}</span>${label}<span class=e>${instr}</span>`;
    b.onclick = ()=>send(instr);
    grid.appendChild(b);
  });
}
renderGroup();
function msg(t){document.getElementById('msg').textContent=t;}
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json();}
async function send(instr){
  msg('下发中: '+instr);
  const d=await post('/run',{instruction:instr, mode:curMode, use_agent:document.getElementById('agent').checked});
  msg((d.ok?'✅ ':'❌ ')+(d.message||''));
  setTimeout(refresh,1200);
}
function runCustom(){
  const v=document.getElementById('instr').value.trim();
  if(!v) return msg('先输入指令');
  send(v);
}
let lastScanTs = 0;
async function flowStart(dry){
  const d=await post('/flow/start',{dry_run:dry,
    grasp_seconds:Number(document.getElementById('graspSeconds').value),
    scan_seconds:Number(document.getElementById('scanSeconds').value),
    place_seconds:Number(document.getElementById('placeSeconds').value)}); msg(d.message||'流程启动');
}
async function holdStop(){const d=await post('/hold',{});msg(d.message||'已请求停止');}
async function videoStats(){
  try{const d=await(await fetch('/video-health')).json();
    const c=d.capture&&d.capture['8'];
    document.getElementById('videoStats').textContent=c?
      '主视角 '+c.fps+' fps · 帧龄 '+Math.max(0,(Date.now()/1000-c.ts)).toFixed(1)+'s':'采集状态不可用';
  }catch(e){}setTimeout(videoStats,2000);
}
videoStats();
async function flowCancel(){const d=await post('/flow/cancel',{});msg(d.message);}
async function flowTick(){
  try{const d=await(await fetch('/flow')).json();
    const labels={idle:'就绪',starting:'启动中',grasp:'抓取扫码枪',scan:'对准 · 等待新码',place:'按码分拣',finished_unverified:'流程结束 · 实物结果待确认',cancelled:'已取消',error:'异常停止'};
    document.getElementById('flowstate').textContent=(labels[d.stage]||d.stage)+' '+(d.code||'')+'\n'+
      d.events.slice(-5).map(e=>e.message).join('\n');
  }catch(e){} setTimeout(flowTick,1000);
}
flowTick();
async function scanTick(){
  try {
    const d = await (await fetch('/scan', {cache:'no-store'})).json();
    const ls = d.last;
    if (ls && ls.ts !== lastScanTs) {
      lastScanTs = ls.ts;
      document.getElementById('scancode').textContent = ls.code;
      document.getElementById('scantime').textContent = ls.time;
      if (d.suggest) document.getElementById('scaninstr').value = d.suggest;
      msg('📷 扫到: ' + ls.code + (d.suggest ? '  → 点「下发」' : ''));
    }
  } catch(e){}
  setTimeout(scanTick, 700);
}
function sendScan(){
  const v = document.getElementById('scaninstr').value.trim();
  if (!v) return msg('先填写要下发的指令');
  send(v);
}
async function editRules(){
  const d = await (await fetch('/scan', {cache:'no-store'})).json();
  const r = d.rules || {};
  const txt = prompt('默认指令（没匹配到码时用）：', r.default || '');
  if (txt === null) return;
  const map = {};
  let line = prompt('码 → 指令 映射，每行一条，格式:  码=指令\n（留空表示不用映射）',
                    Object.entries(r.map || {}).map(([k,v]) => k+'='+v).join('\n'));
  if (line) {
    for (const l of line.split('\n')) {
      const i = l.indexOf('=');
      if (i > 0) map[l.slice(0,i).trim()] = l.slice(i+1).trim();
    }
  }
  const res = await fetch('/scan_rules', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({default: txt, map})});
  const j = await res.json();
  msg((j.ok?'✅ ':'❌ ') + (j.message||''));
}
scanTick();

async function stopIt(){msg('停止并归零中…');const d=await post('/home',{});msg((d.ok?'✅ ':'❌ ')+(d.message||''));setTimeout(refresh,600);}
async function stopOnly(){msg('停止中…');const d=await post('/stop',{});msg((d.ok?'✅ ':'❌ ')+(d.message||''));setTimeout(refresh,400);}
async function refresh(){
  try{
    const d = await (await fetch('/state')).json();
    const st=document.getElementById('st');
    st.textContent = d.running ? ('运行中 '+d.uptime) : '空闲';
    st.className='tag '+(d.running?'on':'off');
    document.getElementById('log').textContent = d.log||'(无输出)';
    const g = d.gripper;
    const gr = document.getElementById('gr');
    if (g) {
      const f = v => (v<0.02?'闭合':(v>1.3?'全开':v.toFixed(2)+' rad'));
      gr.textContent = '左夹爪 '+f(g.left)+'  |  右夹爪 '+f(g.right);
      gr.className = 'tag';
    } else { gr.textContent = '夹爪 无读数'; }
  }catch(e){document.getElementById('st').textContent='连不上';}
}
refresh(); setInterval(refresh,3000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8" if ctype != "image/jpeg" else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            modes = [[k, v["label"], json.dumps(v["body"], ensure_ascii=False)]
                     for k, v in BODY_MODES.items()]
            page = (PAGE.replace("__GROUPS__", json.dumps(GROUPS, ensure_ascii=False))
                        .replace("__CAMS__", json.dumps(list(CAMS.items()), ensure_ascii=False))
                        .replace("__MODES__", json.dumps(modes, ensure_ascii=False)))
            self._send(200, page, "text/html")
        elif self.path.startswith("/snap"):
            m = re.search(r"[?&]d=(\d+)", self.path)
            dev = int(m.group(1)) if m else 8
            data = robot_get(f"/snap?d={dev}") or snapshot(dev)
            if not data:
                self._send(404, b"", "image/jpeg")
            else:
                self._send(200, data, "image/jpeg")
        elif self.path == "/video-health":
            self._send(200, robot_get('/health', timeout=2) or '{}')
        elif self.path == "/flow":
            self._send(200, json.dumps(FLOW.state(), ensure_ascii=False))
        elif self.path == "/scan":
            ls = last_scan()
            rules = load_rules()
            code = (ls or {}).get("code", "")
            instr = rules.get("map", {}).get(code) or rules.get("default", "")
            self._send(200, json.dumps({"last": ls, "rules": rules,
                                        "suggest": instr}, ensure_ascii=False))
        elif self.path == "/twin":
            self._send(200, TWIN_PAGE, "text/html")
        elif self.path == "/twin.json":
            try:
                kin = dict(kinematics())
                kin["values"] = joint_values()
                self._send(200, json.dumps(kin, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path == "/joints":
            d = robot_get("/joints")
            self._send(200, d if d else json.dumps(joint_values(), ensure_ascii=False))
        elif self.path.startswith("/web/"):
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web",
                              os.path.basename(self.path))
            if os.path.isfile(fp):
                ctype = "application/javascript" if fp.endswith(".js") else "application/octet-stream"
                with open(fp, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, "{}")
        elif self.path == "/state":
            try:
                now = time.time()
                if _st_cache["v"] and now - _st_cache["t"] < 5:
                    s = dict(_st_cache["v"])
                    s["log"] = _st_cache["v"].get("log", "")
                else:
                    s = status()
                    s["log"] = read_log(40)
                    _st_cache["t"] = now
                    _st_cache["v"] = dict(s)
                s["gripper"] = gripper_state()
                self._send(200, json.dumps(s, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            data = {}
        try:
            if self.path == "/flow/start":
                FLOW.start(data)
                self._send(200, json.dumps({"ok": True, "message": "流程已启动"}, ensure_ascii=False))
            elif self.path == "/flow/cancel":
                FLOW.abort()
                self._send(200, json.dumps({"ok": True, "message": "取消已请求，后台将结束当前流程"}, ensure_ascii=False))
            elif self.path == "/hold":
                FLOW.abort()
                self._send(200, json.dumps({"ok": True, "message": stop_inference() or "推理已停止，未请求归零"}, ensure_ascii=False))
            elif self.path == "/run":
                if FLOW.active():
                    return self._send(409, json.dumps({"ok": False, "message": "自动流程运行中，请先取消"}, ensure_ascii=False))
                instr = (data.get("instruction") or "").strip()
                if not instr:
                    return self._send(200, json.dumps({"ok": False, "message": "指令为空"}))
                self._send(200, json.dumps({"ok": True, "message": run_instruction(instr, data.get("mode") or "full", bool(data.get("use_agent")))},
                                           ensure_ascii=False))
            elif self.path == "/home":
                self._send(200, json.dumps({"ok": True, "message": home_all()},
                                           ensure_ascii=False))
            elif self.path == "/scan_rules":
                save_rules({"default": data.get("default", ""),
                            "map": data.get("map", {})})
                self._send(200, json.dumps({"ok": True, "message": "规则已保存"},
                                           ensure_ascii=False))
            elif self.path == "/stop":
                FLOW.abort()
                self._send(200, json.dumps({"ok": True, "message": stop_all()}, ensure_ascii=False))
            else:
                self._send(404, "{}")
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"ok": False, "message": str(e)}, ensure_ascii=False))


def find_perception_container():
    global PERCEPTION_CID
    try:
        raw = _request(f"/endpoints/{ENDPOINT_ID}/docker/containers/json?all=1", timeout=15)
        for c in json.loads(raw):
            n = c["Names"][0].lstrip("/")
            if "perception_ops" in n:
                PERCEPTION_CID = c["Id"][:12]
                return PERCEPTION_CID
    except Exception:  # noqa: BLE001
        pass
    return ""


WRIST_DAEMON = r"""#!/usr/bin/env python3
# 机器人状态守护：夹爪读数 + 关节角 -> 落盘，供控制台读取
import json
import rclpy
from std_msgs.msg import Float32
from sensor_msgs.msg import JointState

rclpy.init()
node = rclpy.create_node("xr_state")
g = {"left": 0.0, "right": 0.0}

def mk_grip(k):
    def cb(m):
        g[k] = float(m.data)
        try:
            open("/tmp/gripper.txt", "w").write("%.4f,%.4f" % (g["left"], g["right"]))
        except Exception:
            pass
    return cb

def on_js(msg):
    try:
        d = {n: float(p) for n, p in zip(msg.name, msg.position)}
        open("/tmp/joints.json", "w").write(json.dumps(d))
    except Exception:
        pass

node.create_subscription(Float32, "/databridge/slave/left_gripper_feedback", mk_grip("left"), 1)
node.create_subscription(Float32, "/databridge/slave/right_gripper_feedback", mk_grip("right"), 1)
node.create_subscription(JointState, "/ex001/joint_states", on_js, 1)

while rclpy.ok():
    rclpy.spin_once(node, timeout_sec=0.05)
"""


def ensure_wrist_daemon():
    """在感知容器里拉起腕部相机订阅守护（防止重复启动）"""
    if not PERCEPTION_CID:
        return "⚠️ 没找到感知容器"
    b = base64.b64encode(WRIST_DAEMON.encode()).decode()
    cmd = (f"echo '{b}' | base64 -d > /tmp/wg.py && "
           "if [ ! -f /tmp/wg.pid ] || ! kill -0 $(cat /tmp/wg.pid) 2>/dev/null; then "
           "source /opt/xr/setup.bash >/dev/null 2>&1; "
           "setsid nohup python3 /tmp/wg.py > /tmp/wg.log 2>&1 < /dev/null & echo $! > /tmp/wg.pid; "
           "sleep 3; fi; ls -l /tmp/wrist_a.jpg /tmp/wrist_b.jpg 2>/dev/null | wc -l")
    return shell(cmd, timeout=30, cid=PERCEPTION_CID, retries=1).decode("utf-8", "replace").strip()


def main():
    global ROBOT_IP, _ep
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=ROBOT_IP)
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    ROBOT_IP = a.ip
    _ep = f"http://{ROBOT_IP}:{PORTAINER_PORT}/api"

    print("=" * 60)
    print("  🍭 超级棒棒糖 · 量子1号 Pro 通用控制台")
    print(f"  机器人  {ROBOT_IP}  (ex001-655 / 工位27)")
    print(f"  打开    http://localhost:{a.port}")
    print("=" * 60)
    def startup_checks():
        try:
            print("  连接:", (shell_text("echo pong", timeout=12) or "").strip() or "无响应")
            print("  脚本:", ensure_scripts())
            print("  感知容器:", find_perception_container() or "未找到")
            print("  腕部守护:", ensure_wrist_daemon(), "(2=两张图就绪)")
        except Exception as e:
            print("  自检失败:", e)
    threading.Thread(target=startup_checks, daemon=True).start()

    # 抓帧已迁移到机器人端（robot_api.py:8898），本机不再抢相机
    print("  相机: 由机器人端服务提供（:8898）")
    print("  扫码枪:", start_scanner())
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"\n  服务已启动 → http://localhost:{a.port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
