#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锁臂中间层（Harness）—— 让 VLA 只能动一只手臂

原理
----
机器人的 client 把观测发给我们，我们转发给官方 VLA；
VLA 回来的 32 帧动作里，我们**把被锁那条臂的目标位姿全部替换成
观测里传上来的当前实际位姿**。于是那条臂 32 帧都在原地，
物理上被锁死。另一条臂照常执行。

用法
----
  python3 锁臂.py                       # 默认锁右臂，监听 0.0.0.0:8001
  python3 锁臂.py --lock left           # 锁左臂
  python3 锁臂.py --lock none           # 不锁（纯转发，用于对照）
  python3 锁臂.py --port 8001 --verbose

然后把机器人 client 的 --model-address 指到：
  ws://<本机IP>:8001

规则允许：官方 FAQ「harness 改动作：允许」
"""

import argparse
import copy
import json
import os
import sys
import time

import msgpack
import numpy as np
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve

API_URL = os.environ.get("WALLX_ENDPOINT", "wss://maas-api.x2robot.com/v1/vla/wallx")
API_KEY = os.environ.get("WALLX_API_KEY", "")
TIMEOUT = 60
MAX_SIZE = 64 * 1024 * 1024

ARMS = ("left_ee", "right_ee")

_stats = {"req": 0, "frozen": 0, "err": 0, "last_t": 0.0}

# ===== 中途改 prompt =====
_current = {"instruction": None, "since": 0.0, "base": None}

# ===== 姿态钉死：把某条臂的 rotation_rpy 强制成固定值 =====
_pin = {"left_ee": None, "right_ee": None}


import math


def _rpy2R(r, p, y):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp,     cp * sr,                cp * cr]]


def _R2rpy(R):
    p = math.asin(max(-1.0, min(1.0, -R[2][0])))
    if abs(math.cos(p)) < 1e-6:
        r = 0.0
        y = math.atan2(-R[0][1], R[1][1])
    else:
        r = math.atan2(R[2][1], R[2][2])
        y = math.atan2(R[1][0], R[0][0])
    return [r, p, y]


def _rot_axis(rpy, axis, theta):
    """绕工具坐标系某一轴旋转（axis: 'x'|'y'|'z'）"""
    R = _rpy2R(*rpy)
    c, s2 = math.cos(theta), math.sin(theta)
    if axis == "x":
        Mx = [[1.0, 0.0, 0.0], [0.0, c, -s2], [0.0, s2, c]]
    elif axis == "y":
        Mx = [[c, 0.0, s2], [0.0, 1.0, 0.0], [-s2, 0.0, c]]
    else:
        Mx = [[c, -s2, 0.0], [s2, c, 0.0], [0.0, 0.0, 1.0]]
    M = [[sum(R[i][k] * Mx[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return _R2rpy(M)


_pin["tilt"] = {"left_ee": ["x", 0.0], "right_ee": ["x", 0.0]}


def set_tilt(arm, deg, axis="x"):
    if arm not in _pin["tilt"]:
        return None
    _pin["tilt"][arm] = [axis, math.radians(float(deg))]
    print(f"[tilt] {arm} 绕工具 {axis} 轴倾斜 {deg:+.1f}°", flush=True)
    return deg


def _spin_tool(rpy, theta):
    """绕工具坐标系 Z 轴自转 theta（等效转第 6 关节）"""
    R = _rpy2R(*rpy)
    c, s2 = math.cos(theta), math.sin(theta)
    Rz = [[c, -s2, 0.0], [s2, c, 0.0], [0.0, 0.0, 1.0]]
    M = [[sum(R[i][k] * Rz[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return _R2rpy(M)


_pin["spin"] = {"left_ee": 0.0, "right_ee": 0.0}


def set_spin(arm, deg):
    """给某条臂加一个工具轴自转偏移（度）。+90 = 逆时针（俯视看）"""
    if arm not in _pin["spin"]:
        return None
    _pin["spin"][arm] = math.radians(float(deg))
    print(f"[spin] {arm} 工具轴自转 {deg:+.1f}°", flush=True)
    return deg


def set_pin(arm, rpy):
    """rpy = [roll,pitch,yaw] 弧度；None 取消"""
    if arm not in _pin:
        return None
    _pin[arm] = None if rpy is None else [float(x) for x in rpy]
    print(f"[pin] {arm} 姿态钉死 → {_pin[arm]}", flush=True)
    return _pin[arm]


def apply_pin(reply, verbose=False):
    acts = reply.get("actions")
    if not isinstance(acts, dict):
        return reply
    # ⓪ 掰腕：绕工具 X/Y 轴倾斜
    for arm in ("left_ee", "right_ee"):
        ax, th = _pin["tilt"].get(arm, ["x", 0.0])
        if abs(th) < 1e-6:
            continue
        blk = acts.get(arm)
        if not isinstance(blk, dict):
            continue
        try:
            blk["rotation_rpy"] = [_rot_axis(list(r), ax, th)
                                   for r in blk.get("rotation_rpy", [])]
            if verbose:
                print(f"[tilt] {arm} {ax} {math.degrees(th):+.1f}° 已应用", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[tilt] 失败 {e}", flush=True)

    # ① 工具轴自转（等效转第 6 关节）
    for arm in ("left_ee", "right_ee"):
        th = _pin["spin"].get(arm) or 0.0
        if abs(th) < 1e-6:
            continue
        blk = acts.get(arm)
        if not isinstance(blk, dict):
            continue
        try:
            blk["rotation_rpy"] = [_spin_tool(list(r), th)
                                   for r in blk.get("rotation_rpy", [])]
            if verbose:
                print(f"[spin] {arm} 自转 {math.degrees(th):+.1f}° 已应用", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[spin] 失败 {e}", flush=True)
    for arm, rpy in _pin.items():
        if arm == "spin":
            continue
        if rpy is None:
            continue
        blk = acts.get(arm)
        if not isinstance(blk, dict):
            continue
        blk["rotation_rpy"] = [list(rpy) for _ in range(32)]
        if verbose:
            print(f"[pin] {arm} rotation_rpy → {[round(v,3) for v in rpy]} ×32", flush=True)
    return reply


def set_instruction(text):
    """设置当前生效的 instruction（None = 用 client 自己发的）"""
    _current["instruction"] = (text or "").strip() or None
    _current["since"] = time.time()
    return _current["instruction"]


# ---------------- msgpack ----------------
def unpack(frame):
    if not isinstance(frame, bytes):
        raise ValueError("expected binary MessagePack frame")
    return msgpack.unpackb(frame, raw=False, strict_map_key=False)


def pack(value):
    return msgpack.packb(value, use_bin_type=True)


def _to_list(x):
    """numpy / list / scalar -> 纯 python list"""
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):
        return x.item()
    return x


# ---------------- 锁臂核心 ----------------
def freeze_arm(reply, request, arm, verbose=False):
    """把 arm 的 32 帧动作全部替换成观测里的当前位姿"""
    if arm not in ARMS:
        return reply
    obs = (request or {}).get("observation") or {}
    state = (obs.get("state") or {}).get(arm)
    if not state:
        _stats["err"] += 1
        if verbose:
            print(f"[锁臂] ⚠️ observation.state.{arm} 不存在，跳过锁定", flush=True)
        return reply

    pos = _to_list(state.get("position"))
    rpy = _to_list(state.get("rotation_rpy"))
    grip = _to_list(state.get("gripper"))
    if pos is None or rpy is None or grip is None:
        _stats["err"] += 1
        return reply

    actions = reply.get("actions")
    if not isinstance(actions, dict):
        _stats["err"] += 1
        return reply
    block = actions.get(arm)
    if not isinstance(block, dict):
        _stats["err"] += 1
        return reply

    # 32 帧全部 = 当前位姿
    block["position"] = [list(pos) for _ in range(32)]
    block["rotation_rpy"] = [list(rpy) for _ in range(32)]
    gv = grip if isinstance(grip, list) else [grip]
    block["gripper"] = [float(gv[0]) for _ in range(32)]

    _stats["frozen"] += 1
    if verbose:
        print(f"[锁臂] {arm} 已锁定 @ pos={[round(float(v), 3) for v in pos]} "
              f"grip={round(float(gv[0]), 3)}", flush=True)
    return reply


# ---------------- 转发 ----------------
def process_observation(request):
    payload = copy.deepcopy(request)
    if _current["base"] is None:
        _current["base"] = payload.get("instruction")
    # 中途改 prompt：用当前设定覆盖 client 那一句静态指令
    if _current["instruction"]:
        payload["instruction"] = _current["instruction"]
    if any(k in payload for k in ("use_rtc", "prev_action_chunk", "inference_delay")):
        raise ValueError("本中间层只支持 sync（不支持 RTC）")
    ins = payload.get("instruction")
    if not isinstance(ins, str) or not ins.strip():
        raise ValueError("instruction 为空")
    payload["instruction"] = ins.strip()
    return payload


def handle_client(client_ws, lock_arm="right_ee", verbose=False):
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else None
    try:
        with connect(API_URL, additional_headers=headers, proxy=None,
                     open_timeout=30, close_timeout=2, max_size=MAX_SIZE,
                     ping_interval=None, compression=None) as api_ws:
            meta = api_ws.recv(timeout=TIMEOUT)
            client_ws.send(meta)
            if verbose:
                print("[锁臂] 已连上官方 VLA，开始转发", flush=True)

            while True:
                req = unpack(client_ws.recv())
                payload = process_observation(req)
                _stats["req"] += 1
                _stats["last_t"] = time.time()

                api_ws.send(pack(payload))
                reply = unpack(api_ws.recv(timeout=TIMEOUT))

                if "error" not in reply:
                    reply = freeze_arm(reply, req, lock_arm, verbose)
                    reply = apply_pin(reply, verbose)

                client_ws.send(pack(reply))
    except ConnectionClosed:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[锁臂] 失败: {type(exc).__name__}: {exc}", flush=True)
        try:
            client_ws.close(code=1011, reason="harness error")
        except Exception:  # noqa: BLE001
            pass


def start_control_server(port, verbose=False):
    """HTTP 控制口：POST /say {"instruction": "..."} 中途改 prompt"""
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = _json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(b)
            except Exception:  # noqa: BLE001
                pass

        def do_GET(self):
            self._send(200, {"now": _current["instruction"],
                             "pin": {k: v for k, v in _pin.items() if k != "spin"},
                             "spin_deg": {k: round(math.degrees(v), 1)
                                          for k, v in _pin["spin"].items()},
                             "tilt": {k: [v[0], round(math.degrees(v[1]), 1)]
                                      for k, v in _pin["tilt"].items()},
                             "client_said": _current["base"],
                             "since": _current["since"],
                             "stats": _stats})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = _json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                data = {}
            if self.path == "/say":
                v = set_instruction(data.get("instruction"))
                if verbose:
                    print(f"[prompt] 中途改为: {v!r}", flush=True)
                self._send(200, {"ok": True, "now": v})
            elif self.path == "/tilt":
                arm = data.get("arm", "left_ee")
                arm = {"left": "left_ee", "l": "left_ee",
                       "right": "right_ee", "r": "right_ee"}.get(arm, arm)
                v = set_tilt(arm, data.get("deg", 0), data.get("axis", "x"))
                self._send(200, {"ok": True, "arm": arm,
                                 "axis": data.get("axis", "x"), "deg": v})
            elif self.path == "/spin":
                arm = data.get("arm", "left_ee")
                arm = {"left": "left_ee", "l": "left_ee",
                       "right": "right_ee", "r": "right_ee"}.get(arm, arm)
                v = set_spin(arm, data.get("deg", 0))
                self._send(200, {"ok": True, "arm": arm, "deg": v})
            elif self.path == "/pin":
                arm = data.get("arm", "left_ee")
                if arm in ("left", "l"):
                    arm = "left_ee"
                if arm in ("right", "r"):
                    arm = "right_ee"
                v = set_pin(arm, data.get("rpy"))
                self._send(200, {"ok": True, "arm": arm, "rpy": v})
            elif self.path == "/clear":
                set_instruction(None)
                self._send(200, {"ok": True, "now": None})
            else:
                self._send(404, {})

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    import threading as _th
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"  控制口    http://0.0.0.0:{port}  （POST /say 中途改 prompt）", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default="right", choices=["left", "right", "none"],
                    help="锁哪条臂（默认 right）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--say-port", type=int, default=8002, help="中途改 prompt 的控制口")
    ap.add_argument("--no-say", action="store_true", help="不启动控制口")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    lock = {"left": "left_ee", "right": "right_ee", "none": ""}[a.lock]

    if not API_KEY:
        # 试着从同目录 api.env 读
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api.env")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.startswith("WALLX_API_KEY="):
                    globals()["API_KEY"] = line.split("=", 1)[1].strip()
    if not API_KEY:
        print("⚠️ 没有 WALLX_API_KEY（读 api.env 或设环境变量）", file=sys.stderr)

    print("=" * 58)
    print("  锁臂中间层（Harness）")
    print(f"  监听      {a.host}:{a.port}")
    print(f"  锁定      {'右臂' if lock == 'right_ee' else ('左臂' if lock == 'left_ee' else '不锁(纯转发)')}")
    print(f"  上游      {API_URL}")
    print(f"  client 请把 --model-address 指到  ws://<本机IP>:{a.port}")
    print("=" * 58)
    if not a.no_say:
        start_control_server(a.say_port, a.verbose)

    def handler(ws):
        handle_client(ws, lock, a.verbose)

    with serve(handler, a.host, a.port, max_size=MAX_SIZE,
               ping_interval=None, compression=None) as server:
        print("  就绪，等 client 连接…\n", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n退出。统计:", json.dumps(_stats))


if __name__ == "__main__":
    main()
