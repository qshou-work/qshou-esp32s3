#!/usr/bin/env python3
"""
capture_boot.py -- 复位开发板并抓取启动日志（用于核对 sensor 型号 / WiFi 是否连上）。

与 serial_read.py 的区别
------------------------
serial_read.py **刻意不复位**（DTR/RTS 保持高电平），因为复位会踢掉正在进行的
BLE 连接。而本脚本的目的正是要抓**上电启动日志**，所以必须主动复位。

复位时序（esptool 风格，两种极性都试，哪个出字节用哪个）：
    RTS -> EN，DTR -> IO0
    RTS 拉低(EN=0) 复位，再释放(EN=1) 启动；DTR 保持无效(IO0=1) 走正常启动。

用法
----
    python capture_boot.py COM3
    python capture_boot.py COM3 15 out.txt
    python capture_boot.py            # 自动探测 ESP32 串口
"""
from __future__ import annotations

import sys
import time

import serial
import serial.tools.list_ports as list_ports

# 启动日志里值得关注的行（大小写不敏感的子串）
MARKERS = [
    "Qshou firmware booting",
    "Detected",              # esp32-camera 报的实际 sensor：Detected OV2640/OV5640 camera
    "QSHOU_CAM",              # 本工程 camera 接缝日志
    "esp_camera_init failed",
    "frame size",
    "camera_hal",
    "got ip",
    "wifi init failed",
    "http server started",
    "mjpeg stream server up",
    "stream start failed",
    "Qshou firmware ready",
    "rst:0x",                # 复位原因
    "abort()", "Guru Meditation", "panic", "assert failed",   # 崩溃信号
]


def autodetect_port() -> str | None:
    """挑一个像 ESP32 的串口：优先 CH34x / CP210x / USB-Serial-JTAG。"""
    hints = ("ch34", "cp210", "usb-serial", "usb enhanced", "espressif", "jtag")
    cands = list(list_ports.comports())
    for p in cands:
        blob = f"{p.description} {p.hwid}".lower()
        if any(h in blob for h in hints):
            return p.device
    return None


def reset_normal(ser: serial.Serial) -> None:
    ser.dtr = False
    ser.rts = False
    time.sleep(0.05)
    ser.rts = True
    time.sleep(0.1)      # EN 拉低 -> 复位
    ser.rts = False
    time.sleep(0.1)      # EN 释放 -> 启动
    ser.dtr = False


def reset_inverted(ser: serial.Serial) -> None:
    ser.dtr = True
    ser.rts = True
    time.sleep(0.05)
    ser.rts = False
    time.sleep(0.1)
    ser.rts = True
    time.sleep(0.1)
    ser.dtr = True


def capture(port: str, seconds: float) -> tuple[str, int]:
    ser = serial.Serial(port, 115200, timeout=0.5)
    time.sleep(0.3)
    total = 0
    chunks: list[str] = []

    for label, fn in (("RTS-active", reset_normal),
                      ("RTS-inactive", reset_inverted)):
        fn(ser)
        print(f"== reset [{label}] , reading {seconds:.0f}s ==", flush=True)
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                b = ser.read(4000)
            except Exception as e:      # noqa: BLE001
                print("read err:", e, flush=True)
                break
            if b:
                total += len(b)
                txt = b.decode("utf-8", "replace")
                chunks.append(txt)
                sys.stdout.write(txt)
                sys.stdout.flush()
        if total > 0:
            break
    ser.close()
    return "".join(chunks), total


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else (autodetect_port() or "COM3")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    outfile = sys.argv[3] if len(sys.argv) > 3 else None

    print(f"[capture_boot] port={port} seconds={seconds:.0f}")
    full, total = capture(port, seconds)

    print("\n===== MARKERS =====")
    lines = full.splitlines()
    for kw in MARKERS:
        hits = [ln.strip()[:140] for ln in lines if kw.lower() in ln.lower()]
        print(f"[{kw}] {hits[0] if hits else '(not seen)'}")
    print(f"TOTAL_BYTES={total}")

    # 明确判读：sensor 与联网
    print("\n===== VERDICT =====")
    joined = full.lower()
    for name in ("ov5640", "ov2640", "ov3660", "gc2145", "gc0308", "sc030iot"):
        if f"detected {name} camera" in joined:
            print(f"sensor  : {name.upper()}  ✅ 已识别")
            break
    else:
        if "esp_camera_init failed" in joined:
            print("sensor  : ❌ esp_camera_init 失败（见上文 E 行）")
        else:
            print("sensor  : ? 未见 Detected 行（可能没接摄像头 / SCCB 不通）")

    if "got ip" in joined:
        print("network : ✅ 已联网（见上文 got ip 行）")
    elif "wifi init failed" in joined:
        print("network : ❌ WiFi 初始化失败")
    else:
        print("network : ? 未见 got ip（凭据不对或 AP 不在范围）")

    if outfile:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(full)
        print(f"\n[raw log] -> {outfile}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
