#!/usr/bin/env python3
"""对准.py —— 抓枪后的「对准条码」闭环（含 A/B/C 三个判据）

三个判据：
  A 抓枪成功   grip 角度 N 秒内变化 < 2°  且  J2/J3 稳定
  B 该转手腕   抓枪成功 且 已过 N 秒仍未读到码  →  转一格（默认 J6 ±step）
  C 放弃       转了 rounds 轮仍未读到码  →  报失败，可选放下枪

判据链条：
  外部（VLA 或人）抓到枪  →  本脚本接管
  每轮：先看扫码枪有没有新码 → 有 = 成功，立即退出
        没有 → 抓三路相机留证 → 转一格手腕 → 等 settle 秒
  rounds 用完 → 失败

用法：
  python3 对准.py                          # 默认 J6，每格 +15°，6 轮，每轮等 8s
  python3 对准.py --joint left_arm_joint5  # 换关节（俯仰）
  python3 对准.py --step -15               # 反方向
  python3 对准.py --both                   # 每轮同时转 J6 和 J5（横扫）
  python3 对准.py --check-grasp            # 只跑判据 A，不动
  python3 对准.py --dry-run                # 只看不转

依赖：
  关节.py      单关节控制（motor_control.py）
  控制台 :8900 /scan   扫码枪最近一次读数
  /tmp/grab3.sh        抓 head/chest/bev 三路图
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import 关节 as J  # noqa: E402

CONSOLE = "http://127.0.0.1:8900"
GRAB = "/tmp/grab3.sh"


# ---------- 基础 ----------
def last_scan():
    """扫码枪最近一次读数（控制台 /scan 的 last 字段）"""
    try:
        with urllib.request.urlopen(CONSOLE + "/scan", timeout=4) as r:
            return json.load(r).get("last")
    except Exception:
        return None


def scan_ts(rec):
    if not rec:
        return 0
    for k in ("ts", "time", "timestamp", "at"):
        v = rec.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0


def legs():
    """从 /scan 的 last 里取出码本身（不论字段叫什么）"""
    rec = last_scan()
    if not rec:
        return None, 0
    for k in ("code", "text", "value", "data"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper(), scan_ts(rec)
    return None, scan_ts(rec)


def grab3():
    """抓 head/chest/bev 三路图，返回各自字节数"""
    try:
        subprocess.run([GRAB], capture_output=True, timeout=90)
    except Exception:
        return {}
    out = {}
    for name, f in (("head", "/tmp/v_head.jpg"), ("chest", "/tmp/v_chest.jpg"), ("bev", "/tmp/v_bev.jpg")):
        try:
            out[name] = os.path.getsize(f)
        except Exception:
            out[name] = 0
    return out


def grip(side):
    try:
        return J.read().get(side + "_arm_gripper")
    except Exception:
        return None


def joints(side):
    try:
        r = J.read()
        return [r.get(f"{side}_arm_joint{i}") for i in range(1, 7)]
    except Exception:
        return None


def fmt(v, n=1):
    return "—" if v is None else f"{v:+.{n}f}"


# ---------- 判据 A：抓枪成功 ----------
def 判据A(side="right", window=25.0, grip_tol=2.0, joint_tol=1.5, interval=0.0):
    """夹爪闭合后，grip 与 J2/J3 在 window 秒内是否都稳定 → 认为夹住了东西

    注意：每次读关节要起一个 motor_control.py 进程（约 4-6s），所以不加 sleep，
    连续采样即可。window 建议 >= 20s 才能采到 3 个以上点。
    """
    print(f"[A] 观察 {side} 臂 {window:.0f}s（grip 变化 <{grip_tol}°，J2/J3 变化 <{joint_tol}°）…")
    t0 = time.time()
    samples = []
    while time.time() - t0 < window:
        try:
            r = J.read()
        except Exception as e:
            print(f"[A]   读失败：{e}")
            time.sleep(2.0)
            continue
        g = r.get(side + "_arm_gripper")
        j2 = r.get(side + "_arm_joint2")
        j3 = r.get(side + "_arm_joint3")
        if g is not None and j2 is not None and j3 is not None:
            samples.append((g, j2, j3))
            print(f"[A]   +{time.time()-t0:4.1f}s  grip={g:+7.2f}  J2={j2:+7.2f}  J3={j3:+7.2f}")
        if interval:
            time.sleep(interval)
    if len(samples) < 3:
        print("[A] ❌ 数据不足（关节读取失败）")
        return False, {}
    gs = [s[0] for s in samples]
    j2s = [s[1] for s in samples]
    j3s = [s[2] for s in samples]
    dg = max(gs) - min(gs)
    dj2 = max(j2s) - min(j2s)
    dj3 = max(j3s) - min(j3s)
    ok = dg < grip_tol and dj2 < joint_tol and dj3 < joint_tol
    info = {"grip": gs[-1], "d_grip": dg, "d_j2": dj2, "d_j3": dj3,
            "stable_grip": dg < grip_tol, "stable_arm": dj2 < joint_tol and dj3 < joint_tol}
    print(f"[A] grip={fmt(gs[-1],2)}°  变化 {dg:.2f}°   J2={fmt(j2s[-1])}  变化 {dj2:.2f}°   "
          f"J3={fmt(j3s[-1])}  变化 {dj3:.2f}°")
    if ok:
        print("[A] ✅ 夹爪和手臂都稳定 → 判定已夹住物体")
        print("[A] ⚠️ 注意：稳定 ≠ 一定夹住（空夹到限位也会稳定），"
              "最终仍需靠扫码结果或拍照确认")
    elif info["stable_arm"] and not info["stable_grip"]:
        print("[A] ⏳ 手臂稳定但夹爪还在闭合中 → 稍等再判")
    else:
        print("[A] ⏳ 还在运动 → 稍等再判")
    return ok, info


# ---------- 判据 B/C：转手腕直到扫到 ----------
def 对准(joint="left_arm_joint6", step=15.0, rounds=6, settle=8.0, both=False,
         speed=8.0, dry=False, baseline_ts=None, save=True):
    code0, ts0 = legs()
    if baseline_ts is None:
        baseline_ts = ts0
    print(f"[B] 基线：当前码={code0 or '无'}  ts={baseline_ts:.0f}")
    print(f"[B] 计划：{joint} 每轮 {step:+.0f}°，共 {rounds} 轮，每轮等 {settle:.0f}s"
          + ("（同时转 J5）" if both else ""))
    hist = []
    for i in range(1, rounds + 1):
        # 每轮先看有没有新码
        code, ts = legs()
        if code and ts > baseline_ts:
            print(f"[成功] 第 {i} 轮开始前读到 {code}（ts={ts:.0f} > {baseline_ts:.0f}）")
            return True, code, i - 1
        if dry:
            print(f"[{i}/{rounds}] 演练：本应转 {joint} {step:+.0f}°")
            continue
        # 拍照留证
        sizes = grab3()
        print(f"[{i}/{rounds}] 拍照 " + " ".join(f"{k}={v//1024}KB" for k, v in sizes.items()))
        # 转一格
        try:
            r = J.move(joint, step, rel=True, speed=speed, tol=0.8)
            got = r.get("measured_deg")
            tgt = r.get("plan", {}).get("target_deg")
            err = r.get("error_deg")
            print(f"[{i}/{rounds}] {joint} → {fmt(tgt)}°  实测 {fmt(got)}°  误差 {fmt(err,2)}°"
                  f"  {'✅' if r.get('reached') else '⚠️'}")
        except Exception as e:
            print(f"[{i}/{rounds}] 转 {joint} 失败：{e}")
            break
        if both:
            try:
                J.move("left_arm_joint5", -step / 2, rel=True, speed=speed, tol=0.8)
            except Exception as e:
                print(f"    （J5 失败：{e}）")
        # 等扫码
        t0 = time.time()
        while time.time() - t0 < settle:
            code, ts = legs()
            if code and ts > baseline_ts:
                print(f"[成功] 第 {i} 轮转完 {time.time()-t0:.1f}s 后读到 {code}")
                return True, code, i
            time.sleep(0.5)
        print(f"[{i}/{rounds}] {settle:.0f}s 内没扫到")
        hist.append({"round": i, "joint": joint, "step": step})
    print(f"[C] ❌ {rounds} 轮用尽仍未扫到 → 判定失败")
    if save:
        p = os.path.join(HERE, "对准_失败记录.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"joint": joint, "step": step, "rounds": rounds,
                           "hist": hist, "at": time.time()}, f, ensure_ascii=False, indent=1)
            print(f"[C] 记录已存 {p}")
        except Exception:
            pass
    return False, None, rounds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--joint", default="left_arm_joint6")
    ap.add_argument("--step", type=float, default=15.0)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--both", action="store_true", help="每轮同时带一点 J5 俯仰")
    ap.add_argument("--check-grasp", action="store_true", help="只跑判据 A")
    ap.add_argument("--grasp-window", type=float, default=25.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    print("=" * 58)
    print(f"  对准条码  关节={a.joint}  每格={a.step:+.0f}°  轮数={a.rounds}")
    print("=" * 58)

    ok, info = 判据A("right", window=a.grasp_window)
    if a.check_grasp:
        print("\n[A] 结果:", json.dumps(info, ensure_ascii=False))
        return
    if not ok:
        print("[A] 判据 A 未通过 —— 建议先确认是否真的夹住了，再决定是否硬转")
        print("     （可加 --force 思路：把 --grasp-window 调小，或直接跑对准）")
    code, rounds_used = 对准(a.joint, a.step, a.rounds, a.settle, a.both, a.speed, a.dry_run)
    if code:
        print(f"\n最终结果：扫到 {code}，用了 {rounds_used} 轮")
        try:
            p = os.path.join(HERE, "scan_rules.json")
            with open(p, encoding="utf-8") as f:
                rules = json.load(f)
            say = None
            for v in (rules.values() if isinstance(rules, dict) else []):
                if isinstance(v, dict) and v.get("code") == code:
                    say = v.get("say")
            print(f"映射：{code} → {say or '（scan_rules.json 里没找到）'}")
        except Exception:
            pass
    else:
        print("\n最终结果：没扫到，需要人工或换策略")


if __name__ == "__main__":
    main()
