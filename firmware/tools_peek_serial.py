import time
import serial

PORT = "COM3"
BAUD = 115200
DURATION = 8.0

ser = serial.Serial(PORT, BAUD, timeout=0.2)
start = time.time()
chunks = []
while time.time() - start < DURATION:
    data = ser.read(8192)
    if data:
        chunks.append(data)
ser.close()
raw = b"".join(chunks)
text = raw.decode("utf-8", errors="replace")
if text.strip():
    print(text)
else:
    print("[silent]")
