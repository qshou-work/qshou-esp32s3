#!/usr/bin/env python3
"""
watch_serial.py — 长时间被动监听串口，实时落地到本地时钟时间戳。

与 serial_read.py 的区别：
  - serial_read.py    适合"短窗口抓一段日志然后一次性看"。
  - watch_serial.py   适合"一边让人在旁边操作设备、一边持续取证"的场景：
                      每行加 wall-clock 时间戳，边收边 flush 到文件和 stdout，
                      因此**即使进程还在跑**也能随时打开输出文件看进度。

关键：同样**不碰 DTR/RTS**（保持高电平），否则会把 ESP32-S3 复位、
踢掉正在进行的 BLE 连接。详见 serial_read.py 里的说明。

用法（在 qshou/ 下）：
    python watch_serial.py COM7 240 _ble_watch.txt        # 监听 240 秒
    python watch_serial.py COM7 240 _ble_watch.txt --filter 'NimBLE|HID|advertis|subscribe|wifi'
"""
import re
import sys
import time

import serial  # pyserial，IDF 的 python env 里有


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    outfile = sys.argv[3] if len(sys.argv) > 3 else None

    pat = None
    if "--filter" in sys.argv:
        pat = re.compile(sys.argv[sys.argv.index("--filter") + 1])

    try:
        ser = serial.Serial(port, 115200, timeout=0.3)
    except Exception as e:  # noqa: BLE001
        msg = "OPEN FAIL on %s: %r\n" % (port, e)
        sys.stderr.write(msg)
        if outfile:
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(msg)
        return 1

    # 保持与控制线状态一致，避免下降沿触发 USB-Serial/JTAG 的芯片复位
    try:
        ser.dtr = True
        ser.rts = True
    except Exception:
        pass

    f = open(outfile, "w", encoding="utf-8") if outfile else None
    head = "=== watch %s | %.0fs | start %s ===\n" % (
        port, seconds, time.strftime("%H:%M:%S"))
    sys.stdout.write(head)
    if f:
        f.write(head)
        f.flush()

    t0 = time.time()
    buf = b""
    try:
        while time.time() - t0 < seconds:
            n = ser.in_waiting
            data = ser.read(n or 1)
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                stamp = time.strftime("%H:%M:%S")
                elapsed = time.time() - t0
                out = "[%s +%6.1fs] %s\n" % (stamp, elapsed, text)
                if pat and not pat.search(text):
                    continue
                sys.stdout.write(out)
                sys.stdout.flush()
                if f:
                    f.write(out)
                    f.flush()      # 边收边刷，随时可查
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass
        if f:
            f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
