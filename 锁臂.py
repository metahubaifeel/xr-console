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

                client_ws.send(pack(reply))
    except ConnectionClosed:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[锁臂] 失败: {type(exc).__name__}: {exc}", flush=True)
        try:
            client_ws.close(code=1011, reason="harness error")
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default="right", choices=["left", "right", "none"],
                    help="锁哪条臂（默认 right）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8001)
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
