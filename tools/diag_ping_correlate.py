"""同步 ping 网关与设备，观察延迟是否同步涨跌。

用法: python .tmp/jschk/correlate_ping.py [seconds]

判据：
  - 若网关与设备的延迟**同步涨跌** → 瓶颈在共享链路（本机以太网↔AP 这一侧），
    优化重点在网络/AP，不在 ESP32 固件。
  - 若两者**互不相关**、只有设备抖 → 瓶颈在无线最后一跳（含 modem sleep/共存）。
"""
import re
import subprocess
import sys
import threading
import time

GW = "10.59.228.104"
DEV = "10.59.228.53"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 120

samples = {"gw": [], "dev": []}
lock = threading.Lock()
t0 = time.time()


def reader(name, host):
    p = subprocess.Popen(["ping", "-t", "-w", "2500", host],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    while time.time() - t0 < SECS:
        line = p.stdout.readline()
        if not line:
            break
        txt = line.decode("gbk", errors="replace")
        m = re.search(r"时间[=<](\d+)ms", txt)
        now = time.time() - t0
        with lock:
            if m:
                samples[name].append((now, int(m.group(1))))
            elif "超时" in txt or "timed out" in txt.lower():
                samples[name].append((now, None))
    p.kill()


th = [threading.Thread(target=reader, args=("gw", GW)),
      threading.Thread(target=reader, args=("dev", DEV))]
for t in th:
    t.start()
for t in th:
    t.join()

print("=== 同步采样 %.0f 秒 ===" % SECS)


def stats(key, label):
    v = [x for _, x in samples[key] if x is not None]
    lost = sum(1 for _, x in samples[key] if x is None)
    if not v:
        print("%-6s 无有效样本" % label)
        return
    ls = sorted(v)
    print("%-6s n=%-4d 丢包=%-3d min=%-5d p50=%-6d p90=%-6d max=%-6d avg=%.1f" %
          (label, len(v), lost, ls[0], ls[len(ls) // 2],
           ls[int(len(ls) * 0.9)], ls[-1], sum(ls) / len(ls)))


stats("gw", "网关")
stats("dev", "设备")

# 10 秒窗口趋势
print("\n=== 10 秒窗口平均延迟趋势 ===")
print("  时间窗     网关p50   设备p50   差值")
win = 10.0
buckets = []
for i in range(int(SECS / win)):
    lo, hi = i * win, (i + 1) * win
    g = [x for t, x in samples["gw"] if lo <= t < hi and x is not None]
    d = [x for t, x in samples["dev"] if lo <= t < hi and x is not None]
    if not g and not d:
        continue
    gp = sorted(g)[len(g) // 2] if g else None
    dp = sorted(d)[len(d) // 2] if d else None
    buckets.append((gp, dp))
    fmt = lambda v: ("%5dms" % v) if v is not None else "   -  "
    delta = ("%6d" % (dp - gp)) if (gp is not None and dp is not None) else "     -"
    print("  %3.0f-%-3.0fs   %s   %s   %s" % (lo, hi, fmt(gp), fmt(dp), delta))

pairs = [(g, d) for g, d in buckets if g is not None and d is not None]
if len(pairs) >= 4:
    gs = [a for a, _ in pairs]
    ds = [b for _, b in pairs]
    n = len(gs)
    mg, md = sum(gs) / n, sum(ds) / n
    cov = sum((gs[i] - mg) * (ds[i] - md) for i in range(n))
    vg = sum((x - mg) ** 2 for x in gs) ** 0.5
    vd = sum((x - md) ** 2 for x in ds) ** 0.5
    if vg > 0 and vd > 0:
        r = cov / (vg * vd)
        print("\n网关/设备窗口平均 相关系数 r = %.2f (n=%d 窗口)" % (r, n))
        if r > 0.6:
            print("  → 高度同步：瓶颈很可能在**共享链路/AP 侧**，不是 ESP32 固件。")
        elif r < 0.3:
            print("  → 基本独立：瓶颈在**无线最后一跳**（空口/modem sleep/共存）。")
        else:
            print("  → 中等相关：两者都有份。")
