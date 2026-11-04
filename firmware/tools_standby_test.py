import sys, time, urllib.request
import serial

PORT = "COM3"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=0.2)

def reset():
    ser.dtr = False
    ser.rts = True
    time.sleep(0.15)
    ser.rts = False

chunks = []
def drain(seconds, note):
    start = time.time()
    while time.time() - start < seconds:
        data = ser.read(8192)
        if data:
            chunks.append(data)
    sys.stderr.write("[phase] %s done, +%.1fs\n" % (note, time.time() - start))

# Phase 1: reset + boot
reset()
drain(20, "boot")

# Phase 2: idle wait -> standby should engage ~60s after last capture
drain(55, "idle-wait")   # total ~75s since boot

# Phase 3: fire snapshot while still capturing (wake path)
sys.stderr.write("[phase] firing /snapshot ...\n")
t0 = time.time()
try:
    with urllib.request.urlopen("http://192.168.2.160/snapshot", timeout=25) as r:
        body = r.read()
        sys.stderr.write("[phase] snapshot HTTP %d, %d bytes, %.2fs\n" % (r.status, len(body), time.time() - t0))
        with open(r"C:\Users\<username>\WorkBuddy AI\2026-10-15-19-24-37\qshou\firmware\serial_snap.jpg", "wb") as f:
            f.write(body)
except Exception as e:
    sys.stderr.write("[phase] snapshot FAILED: %r\n" % (e,))

drain(15, "post-snapshot")
ser.close()

out = r"C:\Users\<username>\WorkBuddy AI\2026-10-15-19-24-37\qshou\firmware\serial_standby_test.log"
raw = b"".join(chunks)
with open(out, "wb") as f:
    f.write(raw)
sys.stderr.write("captured %d bytes -> %s\n" % (len(raw), out))
