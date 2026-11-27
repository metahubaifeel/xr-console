#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从机器人容器里拉文件（正确拆 Docker 多路复用流）。

用法:
  python3 拉文件.py <容器内路径> <本地路径> [--cid 容器ID]
  python3 拉文件.py /tmp/rd.txt ./robot.urdf
"""
import argparse
import gzip
import json
import os
import sys
import urllib.request

ROBOT = "10.230.20.13"
PORT = 9000
EPID = 3
CLIENT_CID = "05c14da94c70"


def demux(raw: bytes) -> bytes:
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
    return b"".join(out) if out else raw


class Portainer:
    def __init__(self, ip=ROBOT):
        self.base = f"http://{ip}:{PORT}/api"
        self.jwt = ge = None
        r = urllib.request.Request(self.base + "/auth",
                                   data=json.dumps({"Username": "admin",
                                                    "Password": "xsquarerobot"}).encode(),
                                   method="POST")
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=15) as resp:
            self.jwt = json.loads(resp.read())["jwt"]

    def exec(self, cmd, cid, timeout=90):
        body = json.dumps({"Cmd": ["bash", "-lc", cmd], "AttachStdout": True,
                           "AttachStderr": True, "Tty": False}).encode()
        r = urllib.request.Request(
            f"{self.base}/endpoints/{EPID}/docker/containers/{cid}/exec",
            data=body, method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Authorization", "Bearer " + self.jwt)
        with urllib.request.urlopen(r, timeout=15) as resp:
            eid = json.loads(resp.read())["Id"]
        r2 = urllib.request.Request(
            f"{self.base}/endpoints/{EPID}/docker/exec/{eid}/start",
            data=json.dumps({"Detach": False, "Tty": False}).encode(), method="POST")
        r2.add_header("Content-Type", "application/json")
        r2.add_header("Authorization", "Bearer " + self.jwt)
        with urllib.request.urlopen(r2, timeout=timeout) as resp:
            return demux(resp.read())

    def pull(self, remote, local, cid, gz=True):
        if gz:
            cmd = (f"gzip -9 -c {remote} 2>/dev/null | base64 -w0")
        else:
            cmd = f"base64 -w0 {remote} 2>/dev/null"
        print(f"拉取 {remote} ...", file=sys.stderr)
        raw = self.exec(cmd, cid)
        txt = raw.decode("utf-8", "replace")
        import base64 as b64
        import re
        s = re.sub(r"[^A-Za-z0-9+/=]", "", txt).rstrip("=")
        s += "=" * (-len(s) % 4)
        data = b64.b64decode(s)
        if gz:
            data = gzip.decompress(data)
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
        with open(local, "wb") as f:
            f.write(data)
        print(f"OK {local}  {len(data)} 字节", file=sys.stderr)
        return len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("remote")
    ap.add_argument("local")
    ap.add_argument("--cid", default=CLIENT_CID)
    ap.add_argument("--ip", default=ROBOT)
    a = ap.parse_args()
    Portainer(a.ip).pull(a.remote, a.local, a.cid)


if __name__ == "__main__":
    main()
