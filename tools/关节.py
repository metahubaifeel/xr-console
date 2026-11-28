#!/usr/bin/env python3
"""单关节度数控制 —— 走机器人端 motor_control.py（已部署 /tmp/xr-harness/）

用法：
  python3 关节.py catalog                      # 列出所有电机与角度范围
  python3 关节.py read                         # 读当前各关节角度
  python3 关节.py move left_arm_joint2 60      # 绝对角度
  python3 关节.py move left_arm_joint2 +20 --rel   # 相对转动
  python3 关节.py move left_arm_joint2 60 --speed 5
  python3 关节.py pose '{"left_arm_joint2":60,"left_arm_joint3":-40}'  # 多关节顺序执行
  python3 关节.py grip left 120                # 夹爪
  python3 关节.py both-zero                    # 双臂回零

注意：这是「遥控」，比赛时违规；只用于调试和姿态标定。
"""
import argparse
import json
import subprocess
import sys
import uuid

HOST = "10.230.20.13"
USER = "xr"
PASS = "123"
CONTAINER = "ex001_client-robot_client-1"
ENV = ("source /opt/xr/bot/x2robot_client/share/x2robot_client/scripts/session_env.sh "
       ">/dev/null 2>&1; ")
TOOL = "python3 /tmp/xr-harness/motor_control.py "


def run(op, payload=None, timeout=95):
    """在机器人容器里跑 motor_control.py <op> <json>，返回解析后的 dict"""
    cmd = ENV + "exec " + TOOL + op
    if payload is not None:
        cmd += " " + "'" + json.dumps(payload, ensure_ascii=False).replace("'", "'\\''") + "'"
    full = "docker exec " + CONTAINER + " bash -lc " + "'" + cmd.replace("'", "'\\''") + "'"
    p = subprocess.run(
        ["sshpass", "-p", PASS, "ssh",
         "-o", "StrictHostKeyChecking=no",
         "-o", "PreferredAuthentications=password",
         "-o", "PubkeyAuthentication=no",
         f"{USER}@{HOST}", full],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (p.stdout or "") + (p.stderr or "")
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("没拿到 JSON 输出:\n" + out[-1200:])


def catalog():
    return run("catalog")


def read():
    d = catalog()
    return {k: v["position_deg"] for k, v in d["motors"].items()}


def move(motor, degrees, rel=False, speed=8.0, tol=0.5, timeout=95):
    return run("move", {
        "motor": motor, "degrees": float(degrees), "relative": bool(rel),
        "speed_deg_s": float(speed), "tolerance_deg": float(tol),
        "command_id": uuid.uuid4().hex,
    }, timeout=timeout)


def show_read(prefix=""):
    try:
        r = read()
    except Exception as e:
        print(prefix + "读取失败:", e)
        return
    for side in ("left", "right"):
        vals = []
        for j in range(1, 7):
            vals.append(f"J{j}={r.get(side+'_arm_joint%d' % j, 0):+7.2f}")
        g = r.get(side + "_arm_gripper", 0)
        print(f"{prefix}{side:5s} " + " ".join(vals) + f"  grip={g:+7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["catalog", "read", "move", "pose", "grip", "both-zero", "test"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--rel", action="store_true", help="相对角度")
    ap.add_argument("--speed", type=float, default=8.0, help="度/秒")
    ap.add_argument("--tol", type=float, default=0.5)
    a = ap.parse_args()

    if a.cmd == "catalog":
        d = catalog()
        for k, v in sorted(d["motors"].items()):
            lo, hi = v["limits_deg"]
            print(f"  {k:20s} {v['position_deg']:+8.2f}°   [{lo:+7.1f} .. {hi:+7.1f}]  {v['kind']}/{v['side']}")
        return

    if a.cmd == "read":
        show_read()
        return

    if a.cmd == "move":
        motor, deg = a.args[0], float(a.args[1])
        r = move(motor, deg, a.rel, a.speed, a.tol)
        print(f"{motor}  {r['plan']['start_deg']:+.2f}° → {r['plan']['target_deg']:+.2f}°   "
              f"实测 {r.get('measured_deg', 0):+.2f}°  误差 {r.get('error_deg', 0):+.2f}°  "
              f"{'✅到位' if r.get('reached') else '⚠️未到位'}")
        return

    if a.cmd == "pose":
        pose = json.loads(a.args[0])
        for motor, deg in pose.items():
            r = move(motor, deg, a.rel, a.speed, a.tol)
            ok = "✅" if r.get("reached") else "⚠️"
            print(f"  {ok} {motor:20s} → {deg:+7.2f}°  实测 {r.get('measured_deg', 0):+7.2f}°")
        return

    if a.cmd == "grip":
        side, deg = a.args[0], float(a.args[1])
        r = move(side + "_arm_gripper", deg, False, a.speed, a.tol)
        print(f"{side} 夹爪 → {deg:+.1f}°  实测 {r.get('measured_deg', 0):+.2f}°")
        return

    if a.cmd == "both-zero":
        for side in ("left", "right"):
            for j in range(1, 7):
                move(f"{side}_arm_joint{j}", 0.0, False, 10.0, 1.0)
            print(f"  {side} 归零中…")
        show_read()
        return

    if a.cmd == "test":
        print("小动作测试：left_arm_joint6 +5°")
        r = move("left_arm_joint6", 5, True, 8.0, 1.0)
        print(f"  {r['plan']['start_deg']:+.2f} → {r['plan']['target_deg']:+.2f}  "
              f"实测 {r.get('measured_deg', 0):+.2f}  {'✅' if r.get('reached') else '⚠️'}")
        show_read()


if __name__ == "__main__":
    main()
