#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机器人端数据服务（跑在感知容器里，host 网络 = 机器人本体端口）

提供:
  GET /joints     17 个关节实时角度 (JSON)
  GET /gripper    {"left":..,"right":..}
  GET /snap?d=N   /dev/videoN 的 JPEG
  GET /health     自检

监听 0.0.0.0:8898
"""
import base64
import io
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RA_PORT", "8897"))
# 预览用小尺寸（WiFi 传 330KB 要 3 秒，太小又看不清，取中间）
CAM_RES = {8: [(960, 720), (800, 600), (640, 480)],
           0: [(800, 600), (640, 480)],
           6: [(800, 600), (640, 480)],
           2: [(640, 480)],
           4: [(640, 480)]}
CAM_RES_FULL = {8: [(1280, 960)], 0: [(1600, 1200)], 6: [(1600, 1200)]}

_cache = {}
_lock = threading.Lock()
_stats = {}


# 抓完就地缩图（WiFi 传 300KB 要好几秒）
PREVIEW_W = {8: 760, 0: 640, 6: 640, 2: 480, 4: 480}
PREVIEW_Q = 72


def shrink(jpeg, dev):
    """把 JPEG 缩到预览尺寸，失败就原样返回"""
    try:
        from PIL import Image
        w = PREVIEW_W.get(dev, 640)
        im = Image.open(io.BytesIO(jpeg))
        if im.width <= w:
            return jpeg
        im = im.convert("RGB").resize(
            (w, int(im.height * w / im.width)), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=PREVIEW_Q, optimize=True)
        out = buf.getvalue()
        return out if len(out) < len(jpeg) else jpeg
    except Exception:  # noqa: BLE001
        return jpeg


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return b""


def grab(dev, full=False):
    """抓一帧 JPEG，返回 bytes"""
    res = (CAM_RES_FULL.get(dev) or CAM_RES.get(dev)) if full else CAM_RES.get(dev, [(640, 480)])
    for (w, h) in res:
        out = f"/tmp/rz{dev}.jpg"
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/video{dev}",
                 "--set-fmt-video", f"width={w},height={h},pixelformat=MJPG",
                 "--stream-mmap", "--stream-count=1", "--stream-to", out],
                capture_output=True, timeout=6)
            d = read_bytes(out)
            if d[:2] == b"\xff\xd8" and len(d) > 3000:
                return d if full else shrink(d, dev)
        except Exception:  # noqa: BLE001
            pass
    return None


def cam_worker(dev):
    """Keep the existing MJPEG device format; never reopen it per frame."""
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                ['v4l2-ctl', '-d', f'/dev/video{dev}', '--stream-mmap',
                 '--stream-count=0', '--stream-to=/dev/stdout'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = bytearray()
            last = 0
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    start = buf.find(b'\xff\xd8')
                    end = buf.find(b'\xff\xd9', max(start, 0))
                    if start < 0 or end < 0:
                        break
                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    now = time.monotonic()
                    if now - last < 0.09:
                        continue
                    d = shrink(frame, dev)
                    with _lock:
                        old = _stats.get(dev, {})
                        _cache[dev] = d
                        _stats[dev] = {'ts': time.time(), 'seq': old.get('seq', 0) + 1,
                                       'fps': round(1 / (now-last), 1) if last else 0}
                    last = now
                if len(buf) > 8000000:
                    buf.clear()
        except Exception:
            pass
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        time.sleep(2)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path.startswith("/snap"):
            m = re.search(r"[?&]d=(\d+)", self.path)
            dev = int(m.group(1)) if m else 8
            full = "full=1" in self.path
            with _lock:
                d = None if full else _cache.get(dev)
            if full:
                return self._send(409, b'continuous preview active')
            if d:
                self._send(200, d, "image/jpeg")
            else:
                self._send(404, b"", "image/jpeg")
        elif self.path.startswith("/wrist"):
            k = "b" if "k=b" in self.path else "a"
            d = read_bytes(f"/tmp/wrist_{k}.jpg")
            if d[:2] == b"\xff\xd8":
                self._send(200, d, "image/jpeg")
            else:
                self._send(404, b"", "image/jpeg")
        elif self.path == "/joints":
            self._send(200, read_bytes("/tmp/joints.json") or b"{}")
        elif self.path == "/gripper":
            t = (read_bytes("/tmp/gripper.txt") or b"0,0").decode().strip()
            try:
                l, r = t.split(",")
                self._send(200, json.dumps({"left": float(l), "right": float(r)}))
            except Exception:  # noqa: BLE001
                self._send(200, "{}")
        elif self.path == "/health":
            with _lock:
                cams = {k: len(v) for k, v in _cache.items()}
                stats = dict(_stats)
            self._send(200, json.dumps({"ok": True, "cams": cams, "capture": stats,
                                        "joints": bool(read_bytes("/tmp/joints.json"))}))
        else:
            self._send(404, "{}")


if __name__ == "__main__":
    for dev in (8, 0, 6):
        threading.Thread(target=cam_worker, args=(dev,), daemon=True).start()
    print(f"robot data service on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
