#!/usr/bin/env python3
"""判读.py —— 用 GPT 视觉判断「是否成功抓住扫码枪」，判完就停，等人给下一步 prompt

用法：
  python3 判读.py                     # 抓三路图 + GPT 判读
  python3 判读.py --q "机器人是否抓住扫码枪"
  python3 判读.py --img /tmp/v_chest.jpg   # 只用指定图
  python3 判读.py --model gpt-4.1

输出：JSON（grasped / confidence / reason / next_hint）+ 一张结论行
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "gpt.env")
GRAB = "/tmp/grab3.sh"

DEFAULT_Q = (
    "这是机器人双臂从三个视角（头部、胸前、底盘前）拍的照片。任务是：机械臂抓住一把"
    "手持式条码扫码枪（黑色 L 形或枪形物体，有把手）。请判断：\n"
    "1) 机械臂的夹爪是否真的夹住了扫码枪（不是空的、不是夹到别的物体）？\n"
    "2) 如果夹住了，扫码枪的读取窗口（透明的扫描头）大致朝向哪里？相对水平面是朝下、"
    "朝前、还是朝侧面？\n"
    "3) 桌上是否有一个带条码的瓶子/罐子？它在哪个方向？\n"
    "只回答 JSON：{\"grasped\": true/false, \"confidence\": 0-1, "
    "\"window_direction\": \"down|forward|side|unknown\", "
    "\"object_description\": \"...\", \"object_direction\": \"left|center|right|none\", "
    "\"reason\": \"一句话中文理由\", \"next_hint\": \"下一步建议的中文一句话\"}"
)


def load_key():
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("没有 OPENAI_API_KEY（gpt.env 里也没有）")


def post(url, payload, key, timeout=90):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    op = urllib.request.build_opener()
    try:
        with op.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:400]
        except Exception:
            pass
        raise SystemExit(f"GPT 返回 HTTP {e.code}: {body}")


def grab():
    try:
        subprocess.run([GRAB], capture_output=True, timeout=90)
    except Exception:
        pass
    out = []
    for name, f in (("head", "/tmp/v_head.jpg"), ("chest", "/tmp/v_chest.jpg"), ("bev", "/tmp/v_bev.jpg")):
        if os.path.exists(f) and os.path.getsize(f) > 5000:
            out.append((name, f))
    return out


def b64(f):
    with open(f, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default=DEFAULT_Q)
    ap.add_argument("--img", action="append", help="指定图片（可多次）；不给则抓三路")
    ap.add_argument("--model", default="gpt-4.1")
    ap.add_argument("--raw", action="store_true", help="打印原始回复")
    a = ap.parse_args()
    key = load_key()

    if a.img:
        imgs = [(os.path.basename(p), p) for p in a.img]
    else:
        imgs = grab()
    if not imgs:
        raise SystemExit("没有可用图片（/tmp/grab3.sh 可能失败）")
    print("图片: " + ", ".join(f"{n}={os.path.getsize(f)//1024}KB" for n, f in imgs), file=sys.stderr)

    content = [{"type": "text", "text": a.q}]
    for name, f in imgs:
        content.append({"type": "text", "text": f"【{name} 视角】"})
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64(f), "detail": "high"}})
    payload = {
        "model": a.model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    r = post("https://api.openai.com/v1/chat/completions", payload, key)
    txt = r["choices"][0]["message"]["content"]
    if a.raw:
        print(txt)
        return
    try:
        d = json.loads(txt)
    except Exception:
        print(txt)
        return
    g = d.get("grasped")
    icon = "✅" if g else "❌"
    print(f"\n{icon} 抓取成功: {g}   置信度 {d.get('confidence', '?')}")
    print(f"   枪口朝向: {d.get('window_direction', '?')}")
    print(f"   桌上物体: {d.get('object_description', '?')}  方向 {d.get('object_direction', '?')}")
    print(f"   理由: {d.get('reason', '')}")
    print(f"   建议: {d.get('next_hint', '')}")
    print("\n（已停，等你给下一步 prompt）")


if __name__ == "__main__":
    main()
