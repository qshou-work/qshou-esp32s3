#!/usr/bin/env python3
"""
trace_and_watch.py — 同一个进程内：先开串口监听 → 注入触摸轨迹 → 继续监听。

为什么必须合成一个进程
----------------------
1. 串口只能被一个进程持有。开两个脚本会互相抢 COM 口。
2. 更重要的是：每次 open() 串口都会动 DTR/RTS，ESP32-S3 原生 USB-Serial/JTAG
   把 DTR 下降沿当复位请求 —— 多开合一次就多复位一次，还会踢掉 BLE 连接。
   （watch_serial.py 里已保持 dtr=rts=True，本脚本沿用。）

为什么"HTTP 200"不能单独作为成功判据
------------------------------------
routes.c:handle_tap() 里，未连接时 platform_ble_emit() 会**静默丢弃并返回 0**，
于是 HTTP 仍然是 200；只有 esp_hidd_dev_input_set() 真的失败才返回 400。
所以 200 有两种可能：「发出去了」或「压根没连接，直接扔了」。
要区分，必须同时看串口日志：连接且成功时**不会有任何错误行**，
而"刚连上"的那一刻日志里有 `GAP subscribe`。

用法：
    python trace_and_watch.py COM7 10.33.175.53 box 12 55 _out.txt
"""
import sys
import time

import serial

sys.path.insert(0, "tools")
from send_trace import build, pack, post, health  # noqa: E402


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    ip = sys.argv[2] if len(sys.argv) > 2 else "10.33.175.53"
    shape = sys.argv[3] if len(sys.argv) > 3 else "box"
    steps = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    step_ms = float(sys.argv[5]) if len(sys.argv) > 5 else 55.0
    outfile = sys.argv[6] if len(sys.argv) > 6 else None
    tail_s = float(sys.argv[7]) if len(sys.argv) > 7 else 8.0

    ser = serial.Serial(port, 115200, timeout=0.3)
    try:
        ser.dtr = True
        ser.rts = True
    except Exception:
        pass
    ser.reset_input_buffer()

    out = open(outfile, "w", encoding="utf-8") if outfile else None

    def emit(tag, text):
        line = "[%s] %s%s\n" % (time.strftime("%H:%M:%S"), tag, text)
        sys.stdout.write(line)
        sys.stdout.flush()
        if out:
            out.write(line)
            out.flush()

    def drain():
        try:
            n = ser.in_waiting
            if n:
                data = ser.read(n)
                for ln in data.decode("utf-8", "replace").splitlines():
                    if ln.strip():
                        emit("LOG ", " " + ln.rstrip())
        except Exception:
            pass

    emit("===", " trace_and_watch start | port=%s ip=%s shape=%s" % (port, ip, shape))
    emit("HEALTH", " " + health(ip))

    drain()
    time.sleep(1.0)
    drain()

    pts = build(shape, steps)
    codes = [post(ip, pack(pts[0][0], pts[0][1], 0x03))]
    time.sleep(step_ms / 1000.0 * 2)
    for (x, y) in pts[1:]:
        codes.append(post(ip, pack(x, y, 0x03)))
        drain()
        time.sleep(step_ms / 1000.0)
    codes.append(post(ip, pack(pts[-1][0], pts[-1][1], 0x00)))

    ok = sum(1 for c in codes if c == 200)
    bad = sorted(set(codes) - {200})
    emit("HTTP", " frames=%d HTTP200=%d other=%s" % (len(codes), ok, bad or "none"))

    t0 = time.time()
    while time.time() - t0 < tail_s:
        drain()
        time.sleep(0.3)

    emit("===", " done. 判据：上面没有 'input_set failed' / 'Unable to fetch "
                "protocol_mode' 即 notify 已发出；配合日志里的 GAP subscribe 可确证。")
    if out:
        out.close()
    ser.close()
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
