#!/usr/bin/env python3
"""
serial_read.py -- read raw bytes from a Windows COM port for a few seconds.

Used to verify that the freshly-flashed ESP32-S3 firmware actually emits
startup logs on its console channel (USB-Serial/JTAG -> usb-otg port).

Usage:  python serial_read.py <PORT> <SECONDS> [OUTFILE]
"""
import sys
import time

import serial  # pyserial, shipped in IDF's python env

port = sys.argv[1] if len(sys.argv) > 1 else "COM7"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
outfile = sys.argv[3] if len(sys.argv) > 3 else None

chunks = []
try:
    # ESP32-S3 USB-Serial-JTAG ignores baud, but keep 115200 for UART bridges.
    ser = serial.Serial(port, 115200, timeout=0.3)
except Exception as e:  # noqa: BLE001
    msg = "OPEN FAIL on %s: %r\n" % (port, e)
    sys.stderr.write(msg)
    if outfile:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(msg)
    sys.exit(1)

# ⚠️ 关键：不要拉低 DTR/RTS！
# ESP32-S3 的原生 USB-Serial/JTAG 会把 DTR 低电平当成“请求芯片复位”，
# 表现为日志首行 rst:0x15 (USB_UART_CHIP_RESET)。pyserial 打开端口时默认
# 会把 DTR/RTS 置高，若我们接着把它们置低，就构成一次下降沿 → 板子重启
# → 正在进行的 BLE 连接被踢掉（这正是“蓝牙配对后不活跃”的头号干扰源）。
# 所以这里【显式保持高电平】，让其状态与打开时一致，不产生额外边沿。
try:
    ser.dtr = True
    ser.rts = True
except Exception:
    pass

t0 = time.time()
while time.time() - t0 < seconds:
    try:
        n = ser.in_waiting
    except Exception:
        n = 0
    data = ser.read(n or 1)
    if data:
        chunks.append(data)
    else:
        time.sleep(0.05)

try:
    ser.close()
except Exception:
    pass

raw = b"".join(chunks)
text = raw.decode("utf-8", errors="replace")

header = "=== %s | %.1fs | %d bytes ===\n" % (port, seconds, len(raw))
print(header + text)

if outfile:
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(header + text)
