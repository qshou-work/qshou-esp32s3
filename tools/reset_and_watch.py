#!/usr/bin/env python3
"""
reset_and_watch.py — 复位 ESP32 并在**同一个串口句柄**上持续抓日志。

为什么不用「先跑一个脚本复位、再跑 watch_serial.py」：
  两次 open/close 都会动 DTR/RTS 控制线。ESP32-S3 原生 USB-Serial/JTAG 把
  DTR 下降沿当成芯片复位请求，所以每开合一次串口就可能多复位一次，
  日志会变成两段、且可能踢掉刚建立的 BLE 连接。
  这里一次 open：先给一个 DTR 下降沿触发复位，随后保持高电平继续读。

用法：
    python reset_and_watch.py COM7 25 _out.txt 'HID|subscribe|wifi|got ip'

参数：port  seconds  outfile  [filter-regex]
"""
import re
import sys
import time

import serial


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    outfile = sys.argv[3] if len(sys.argv) > 3 else None
    pat = re.compile(sys.argv[4]) if len(sys.argv) > 4 else None

    try:
        ser = serial.Serial(port, 115200, timeout=0.3)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("OPEN FAIL on %s: %r\n" % (port, e))
        return 1

    # --- 触发一次复位：DTR 下降沿（先高后低再高） -----------------------
    try:
        ser.dtr = True
        ser.rts = True
        time.sleep(0.15)
        ser.dtr = False          # 下降沿 -> ESP32-S3 USB_UART_CHIP_RESET
        time.sleep(0.25)
        ser.dtr = True           # 回到高电平，之后不再动控制线
    except Exception as e:       # noqa: BLE001
        sys.stderr.write("reset pulse failed: %r\n" % (e,))

    f = open(outfile, "w", encoding="utf-8") if outfile else None
    head = "=== reset+watch %s | %.0fs | start %s ===\n" % (
        port, seconds, time.strftime("%H:%M:%S"))
    sys.stdout.write(head)
    if f:
        f.write(head)
        f.flush()

    t0 = time.time()
    buf = b""
    nlines = 0

    while time.time() - t0 < seconds:
        try:
            chunk = ser.read(4096)
        except Exception:
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                text = line.decode("utf-8", "replace").rstrip()
            except Exception:
                continue
            if pat and not pat.search(text):
                continue
            stamp = "[%s + %.1fs] " % (time.strftime("%H:%M:%S"),
                                       time.time() - t0)
            out = stamp + text + "\n"
            sys.stdout.write(out)
            sys.stdout.flush()
            if f:
                f.write(out)
                f.flush()
            nlines += 1

    tail = "\n=== done, %d lines ===\n" % nlines
    sys.stdout.write(tail)
    if f:
        f.write(tail)
        f.close()
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
