#!/usr/bin/env python3
"""
send_trace.py — 通过 HTTP 向固件注入一条可目视辨识的触摸轨迹。

用途：BLE HID 联调时，协议层是否已通（GAP subscribe / input_set 返回 OK）
只能证明 notify 发出去了，**屏幕上到底有没有反应必须人眼验收**。
本工具负责把轨迹打过去，人负责看手机。

report 布局（与 firmware/ble_hid/hid_report.h 同源）：
    [state][X_MSB][X_LSB][Y_MSB][Y_LSB]
    state : 0x03 = 按下(Tip Switch + In Range)，0x00 = 抬起
    X/Y   : 16-bit 逻辑坐标，[0, 10000]

用法：
    python send_trace.py                       # 默认画方框，间隔 60ms
    python send_trace.py --shape circle        # 画圆
    python send_trace.py --shape line          # 水平直线（最简，先试这个）
    python send_trace.py --ip 10.33.175.53 --step-ms 30 --steps 20

注意：
  - 帧间隔别小于 ~40ms，否则手机端渲染跟不上、看不出轨迹。
  - 每帧都单独走一次 HTTP；帧数 = 1(down) + 边数*steps + 1(up)。
  - 返回码非 200 说明 esp_hidd_dev_input_set() 失败（routes.c 会回 400），
    常见原因：未连接会静默丢弃但仍返回 0 -> 200，所以 200 并不等于已连接，
    要看串口日志里有没有 GAP subscribe。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HID_MAX = 10000
DOWN = 0x03
UP = 0x00


def pack(x, y, state):
    x = max(0, min(HID_MAX, int(x)))
    y = max(0, min(HID_MAX, int(y)))
    return [state, (x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF]


def post(ip, rep, retries=3):
    body = json.dumps({"hid_report": rep}).encode()
    req = urllib.request.Request("http://%s/api/v1/tap" % ip, data=body,
                                 headers={"Content-Type": "application/json"})
    last = -1
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return r.status
        except urllib.error.HTTPError as e:
            last = e.code
        except Exception:
            last = -1
        time.sleep(0.12 * (attempt + 1))
    return last


def health(ip):
    try:
        with urllib.request.urlopen("http://%s/api/v1/health" % ip,
                                    timeout=6) as r:
            return r.read().decode()
    except Exception as e:
        return "FAIL %r" % (e,)


def lerp(a, b, t100):
    return a + (b - a) * t100 // 100


def build(shape, steps):
    """返回 [(x, y), ...] 的按下路径（不含首帧 down 与末帧 up）。"""
    pts = []
    if shape == "line":
        corners = [(2000, 5000), (8000, 5000)]
    elif shape == "circle":
        import math
        cx, cy, r = 5000, 5000, 3000
        n = max(steps * 4, 48)
        for k in range(n + 1):
            a = 2 * math.pi * k / n
            pts.append((int(cx + r * math.cos(a)), int(cy + r * math.sin(a))))
        return pts
    else:  # box（默认）
        corners = [(2500, 2500), (7500, 2500), (7500, 7500),
                   (2500, 7500), (2500, 2500)]

    for i in range(len(corners) - 1):
        (x0, y0), (x1, y1) = corners[i], corners[i + 1]
        for k in range(1, steps + 1):
            pts.append((lerp(x0, x1, k * 100 // steps),
                        lerp(y0, y1, k * 100 // steps)))
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="10.33.175.53")
    ap.add_argument("--shape", default="box",
                    choices=["box", "circle", "line"])
    ap.add_argument("--steps", type=int, default=12, help="每条边的插值帧数")
    ap.add_argument("--step-ms", type=float, default=60.0, help="帧间隔毫秒")
    args = ap.parse_args()

    print("health:", health(args.ip))

    pts = build(args.shape, args.steps)
    if not pts:
        print("no points")
        return 1

    codes = [post(args.ip, pack(pts[0][0], pts[0][1], DOWN))]
    time.sleep(args.step_ms / 1000.0 * 2)
    for (x, y) in pts[1:]:
        codes.append(post(args.ip, pack(x, y, DOWN)))
        time.sleep(args.step_ms / 1000.0)
    codes.append(post(args.ip, pack(pts[-1][0], pts[-1][1], UP)))

    ok = sum(1 for c in codes if c == 200)
    bad = sorted(set(codes) - {200})
    print("shape=%s frames=%d  HTTP200=%d  other=%s"
          % (args.shape, len(codes), ok, bad if bad else "none"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
