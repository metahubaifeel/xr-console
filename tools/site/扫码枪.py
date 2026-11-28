#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码枪抓取器（HID 键盘型）

扫码枪绝大多数是「USB 键盘」：一扫就把字符当键盘敲出去，末尾再敲个回车。
所以不用驱动，直接读 /dev/input/event* 就行。

用法:
  python3 扫码枪.py                  # 监听所有输入设备，自动识别
  python3 扫码枪.py --device /dev/input/event12
  python3 扫码枪.py --list           # 只列出设备

要点:
  · 需要在 input 组里（id -nG 看有没有 input）
  · 扫一次码 -> 屏幕上打印 CODE="..." ，同时追加到 /tmp/scan.log
"""

import argparse
import os
import select
import struct
import sys
import time

# Linux input-event-codes -> 字符
KEYS = {
    2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
    12: '-', 13: '=', 15: '\t', 16: 'q', 17: 'w', 18: 'e', 19: 'r', 20: 't', 21: 'y',
    22: 'u', 23: 'i', 24: 'o', 25: 'p', 26: '[', 27: ']', 28: '\n',
    30: 'a', 31: 's', 32: 'd', 33: 'f', 34: 'g', 35: 'h', 36: 'j', 37: 'k', 38: 'l',
    39: ';', 40: "'", 41: '`', 43: '\\', 44: 'z', 45: 'x', 46: 'c', 47: 'v', 48: 'b',
    49: 'n', 50: 'm', 51: ',', 52: '.', 53: '/', 57: ' ',
    71: '7', 72: '8', 73: '9', 74: '-', 75: '4', 76: '5', 77: '6', 78: '+', 79: '1',
    80: '2', 81: '3', 82: '0', 83: '.', 96: '\n', 98: '/',
}
SHIFT = set('~!@#$%^&*()_+{}|:"<>?')
SHIFT_OF = {'`': '~', '1': '!', '2': '@', '3': '#', '4': '$', '5': '%', '6': '^',
            '7': '&', '8': '*', '9': '(', '0': ')', '-': '_', '=': '+', '[': '{',
            ']': '}', '\\': '|', ';': ':', "'": '"', ',': '<', '.': '>', '/': '?'}

EV_KEY = 1
STRUCT = struct.Struct('<qqHHi')     # timeval(16) + type(2) + code(2) + value(4)


def dev_name(path):
    try:
        return open(f'/sys/class/input/{os.path.basename(path)}/device/name').read().strip()
    except Exception:  # noqa: BLE001
        return '?'


def list_devices():
    out = []
    for i in range(0, 40):
        p = f'/dev/input/event{i}'
        if os.path.exists(p):
            out.append((p, dev_name(p)))
    return out


def decode(files, quiet_names):
    """files: {fd: (path, name)}；返回识别到的字符串"""
    bufs = {fd: [] for fd in files}
    shift = False
    meta = {}
    for fd, (path, name) in files.items():
        meta[fd] = name
    pending = {fd: '' for fd in files}

    while files:
        r, _, _ = select.select(list(files), [], [], 1.0)
        if not r:
            continue
        for fd in r:
            data = os.read(fd, STRUCT.size * 64)
            for i in range(0, len(data) - STRUCT.size + 1, STRUCT.size):
                _, _, typ, code, val = STRUCT.unpack_from(data, i)
                if typ != EV_KEY:
                    continue
                name = meta[fd]
                if code in (42, 54):          # Shift
                    shift = (val != 0)
                    continue
                if val != 1:                  # 只处理按下
                    continue
                if code == 28 or code == 96:  # Enter -> 一条码结束
                    s = pending[fd]
                    pending[fd] = ''
                    if s:
                        yield name, s
                    continue
                ch = KEYS.get(code)
                if ch is None:
                    continue
                if shift and ch in SHIFT_OF:
                    ch = SHIFT_OF[ch]
                elif shift and ch.isalpha():
                    ch = ch.upper()
                pending[fd] += ch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', action='append', help='指定设备，可多次')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--all', action='store_true', help='监听所有设备（默认只监听非键盘核心设备）')
    ap.add_argument('--daemon', action='store_true', help='静默常驻，只写 /tmp/last_scan.json')
    a = ap.parse_args()

    devs = list_devices()
    if a.list:
        for p, n in devs:
            print(f'{p:22s} {n}')
        return

    targets = []
    if a.device:
        targets = [(d, dev_name(d)) for d in a.device]
    else:
        # 排除内置键盘/电源键/音频，剩下的大概率是外接扫码枪
        skip = ('Power Button', 'Sleep Button', 'Video Bus', 'Lid Switch',
                'AT Translated Set 2 keyboard', 'Ideapad extra buttons',
                'HD-Audio', 'Intel HID', 'Touchpad')
        for p, n in devs:
            if a.all or not any(s in n for s in skip):
                targets.append((p, n))

    if not targets:
        print('没找到可监听的设备，用 --list 看看', file=sys.stderr)
        return

    if not a.daemon:
        print('监听设备:')
        for p, n in targets:
            print(f'  {p}  {n}')
        print('\n>>> 现在扫一次码（Ctrl-C 退出）\n')

    files = {}
    for p, n in targets:
        try:
            files[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = (p, n)
        except PermissionError:
            print(f'  ⚠️ {p} 无权限（需要在 input 组或 sudo）', file=sys.stderr)

    try:
        for name, code in decode(files, None):
            ts = time.strftime('%H:%M:%S')
            if not a.daemon:
                print(f'[{ts}] {name}  CODE="{code}"')
            with open('/tmp/scan.log', 'a') as f:
                f.write(f'{ts}\t{code}\n')
            import json as _j
            with open('/tmp/last_scan.json', 'w') as f:
                _j.dump({'code': code, 'ts': time.time(),
                         'time': ts, 'device': name}, f, ensure_ascii=False)
    except KeyboardInterrupt:
        print('\n结束')


if __name__ == '__main__':
    main()
