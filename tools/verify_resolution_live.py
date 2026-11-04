# -*- coding: utf-8 -*-
"""verify_resolution_live.py — 真机验证「分辨率运行时切换」。

链路上已烧入新固件（默认 frame_size=10 = 640x480）。本脚本在真机上验证：
  1) GET /api/v1/health 在线；
  2) GET /api/v1/camera 读启动默认 frame_size（期望 10）；
  3) GET / 抓设备吐出的页面，确认含修正后的下拉框（value=6/9/10/11/12/13/15/16，10 默认选中）；
  4) 对每个枚举 POST /api/v1/camera {frame_size:X}，再 GET /snapshot 解析 JPEG SOF0，
     核对实际像素（真值表：6=320x240 / 9=480x320 / 10=640x480 / 11=800x600 /
     12=1024x768 / 13=1280x720 / 15=1600x1200 / 16=1920x1080）；
     其中 15/16（UXGA/1080p）需 OV5640——framesize 上限已改 sensor 感知，
     OV2640 上最高只到 15（UXGA），POST 16 会被固件拒绝；
  5) 还原默认 10。

用法：
    python tools/verify_resolution_live.py [设备地址，默认 qshou-0a0c.local]
退出码 0 = 全通过。
"""
import json
import sys
import time
import urllib.error
import urllib.request

DEV = sys.argv[1] if len(sys.argv) > 1 else "qshou-0a0c.local"
BASE = "http://" + DEV

# 本构建实测真值表（枚举 -> 实际像素）
EXPECT = {6: (320, 240), 9: (480, 320), 10: (640, 480),
          11: (800, 600), 12: (1024, 768), 13: (1280, 720),
          15: (1600, 1200), 16: (1920, 1080)}


def get(path, timeout=15, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(BASE + path, headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
                continue
            raise
    raise last


def post_json(path, obj, timeout=15, retries=3):
    data = json.dumps(obj).encode()
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                BASE + path, data=data,
                headers={"Content-Type": "application/json",
                         "Connection": "close"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
                continue
            raise
    raise last


def parse_jpeg_dims(b):
    """从 JPEG 字节流里找 SOF0/1/2/3 标记，读出宽高。"""
    i, n = 0, len(b)
    while i < n - 9:
        if b[i] == 0xFF and b[i + 1] in (0xC0, 0xC1, 0xC2, 0xC3):
            h = (b[i + 5] << 8) | b[i + 6]
            w = (b[i + 7] << 8) | b[i + 8]
            return w, h
        i += 1
    return None


def main():
    ok = True

    # 1) health
    try:
        st, _ = get("/api/v1/health", timeout=6, retries=2)
        print("[health] HTTP %d" % st)
        if st != 200:
            ok = False
    except Exception as e:  # noqa: BLE001
        print("[health] FAIL %s" % e)
        ok = False

    # 2) 默认 frame_size（/api/v1/camera 仅支持 POST）
    try:
        st, body = post_json("/api/v1/camera", {}, timeout=8, retries=2)
        j = json.loads(body)
        fs = j.get("frame_size")
        print("[default frame_size] %s (expect 10)" % fs)
        if fs != 10:
            ok = False
            print("   ❌ 启动默认不是 10")
    except Exception as e:  # noqa: BLE001
        print("[camera default] FAIL %s" % e)
        ok = False

    # 3) 设备吐出的页面含修正下拉框
    try:
        st, html = get("/", timeout=10, retries=3)
        html = html.decode("utf-8", "replace")
        have = all(('value="%d"' % k) in html for k in (6, 9, 10, 11, 12, 13, 15, 16))
        print("[served dropdown] 含 6/9/10/11/12/13/15/16 选项: %s" % have)
        if not have:
            ok = False
        if 'value="10" selected' not in html:
            print("   ⚠️ 页面未见 value=10 selected")
    except Exception as e:  # noqa: BLE001
        print("[page] FAIL %s" % e)
        ok = False

    # 4) 逐档切换 + 抓帧核对像素
    for fs, (ew, eh) in EXPECT.items():
        good = False
        for attempt in range(3):
            try:
                post_json("/api/v1/camera", {"frame_size": fs})
                time.sleep(1.0)  # 等传感器完成 framebuffer 重分配
                st, img = get("/snapshot?t=%d" % int(time.time() * 1000),
                              timeout=10, retries=2)
                if st != 200:
                    print("   [fs=%2d] snapshot HTTP %s，重试..." % (fs, st))
                    time.sleep(0.5)
                    continue
                dims = parse_jpeg_dims(img)
                if dims is None:
                    print("   [fs=%2d] 无法解析 JPEG（%d 字节），重试..." % (fs, len(img)))
                    time.sleep(0.5)
                    continue
                w, h = dims
                good = (w == ew and h == eh)
                print("[fs=%2d] snapshot %dx%d  expect %dx%d  %s"
                      % (fs, w, h, ew, eh, "✅" if good else "❌"))
                break
            except Exception as e:  # noqa: BLE001
                print("   [fs=%2d] attempt %d 异常 %s" % (fs, attempt, e))
                time.sleep(0.5)
        if not good:
            ok = False
        time.sleep(0.8)  # 间隔，避免撑爆单任务 httpd 的 7 个 socket

    # 5) 还原默认
    try:
        post_json("/api/v1/camera", {"frame_size": 10})
        print("[restore] frame_size -> 10")
    except Exception as e:  # noqa: BLE001
        print("[restore] FAIL %s" % e)

    print("\nRESULT: %s" % ("ALL PASS ✅" if ok else "FAILURES ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
