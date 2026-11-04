import sys, time
import serial

PORT = "COM3"
BAUD = 115200
DURATION = 20.0

ser = serial.Serial(PORT, BAUD, timeout=0.2)
ser.dtr = False
ser.rts = True   # EN low -> hold reset
time.sleep(0.15)
ser.rts = False  # EN high -> release, board boots
start = time.time()
chunks = []
while time.time() - start < DURATION:
    data = ser.read(8192)
    if data:
        chunks.append(data)
ser.close()
raw = b"".join(chunks)
out = r"C:\Users\<username>\WorkBuddy AI\2026-10-15-19-24-37\qshou\firmware\serial_boot_new.log"
with open(out, "wb") as f:
    f.write(raw)
sys.stderr.write("captured %d bytes -> %s\n" % (len(raw), out))
