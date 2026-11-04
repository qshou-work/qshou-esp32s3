#!/usr/bin/env python3
"""真机验证 /api/v1/sysinfo —— 用真实数值对账，不做"能返回 200 就算过"。

判定原则（对应用户"不接受 mock/局部测试交差"的要求）：
  * 数值必须与**已知真值**对得上（NVS 分区 0x6000、分区表 4 项、PSRAM 8MB…）；
  * 必须能证明是**这一次烧的新固件**（旧固件无此端点 → 404）；
  * 资源水位必须自洽（used ≤ total、psram_free ≤ psram_total、栈余量 > 0）。
"""
import json
import socket
import sys
import time
import urllib.error
import urllib.request

DEVICES = ["10.59.228.53", "qshou-0a0c.local", "qshou-0a0c"]
# 固件侧 GET 端点不校验 X-Auth-Token（grep 确认 routes.c/platform_net.c 无 token 逻辑），
# 故不再伪造一个 header —— 假的 token 只会让"验证脚本"本身变成噪音。
KEY = None
TIMEOUT = 6

# 来自 firmware/partitions.csv 的**真值**，用于对账
EXPECT_NVS_SIZE = 0x6000        # 24 KiB
EXPECT_PART_LABELS = {"nvs", "phy_init", "factory", "storage"}


def get(ip, path, token=None, timeout=TIMEOUT):
    url = "http://%s%s" % (ip, path)
    headers = {}
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def find_device():
    for d in DEVICES:
        try:
            st, body = get(d, "/api/v1/health", timeout=4)
            if st == 200:
                return d
        except Exception:      # noqa: BLE001
            continue
    return None


def main():
    print("== 等待设备上线 ==")
    dev = None
    for i in range(40):
        dev = find_device()
        if dev:
            break
        time.sleep(3)
    if not dev:
        print("FAIL: 设备不可达（mDNS/IP 都试过）。检查是否与设备同网段。")
        return 1
    print("设备: %s" % dev)

    st, body = get(dev, "/api/v1/sysinfo", KEY)
    print("GET /api/v1/sysinfo -> HTTP %d, %d 字节" % (st, len(body)))
    if st != 200:
        print("FAIL: 非 200（旧固件会是 404 → 说明烧的不是新固件）")
        return 1
    j = json.loads(body)
    print(json.dumps(j, ensure_ascii=False, indent=2)[:2600])

    errs, warns = [], []

    def ck(cond, msg):
        (print("  ok   " + msg) if cond else errs.append(msg))

    # --- 顶层 ---
    ck(j.get("ok") is True, "ok=true")
    ck(j.get("entry_bytes") == 32, "entry_bytes=32（NVS 条目口径）")
    ck(j.get("storage_mounted") == 0, "storage_mounted=0（诚实：分区未挂载，不谎报占用）")

    # --- NVS：容量必须与分区表真值一致 ---
    n = j["nvs"]
    ck(n.get("valid") == 1, "nvs.valid=1（NVS 可读）")
    ck(n.get("part_size") == EXPECT_NVS_SIZE,
       "nvs.part_size=%s == 0x6000/24576 真值" % n.get("part_size"))
    ck(0 < n.get("used_entries", 0) <= n.get("total_entries", 0),
       "NVS 条目自洽: used=%s ≤ total=%s" % (n.get("used_entries"), n.get("total_entries")))
    ck(n.get("used_bytes_est") == n.get("used_entries", 0) * 32,
       "used_bytes_est == used_entries×32（估算口径一致）")
    ck(n.get("namespace_count", 0) >= 1,
       "namespace_count=%s ≥1（至少 qshou 命名空间）" % n.get("namespace_count"))
    ck(n.get("free_entries", 0) > 0, "free_entries>0（还有写入余量）")

    # --- 内存 ---
    m = j["mem"]
    ck(0 < m["heap_free"] <= m["internal_total"],
       "heap_free=%d ≤ internal_total=%d" % (m["heap_free"], m["internal_total"]))
    ck(0 < m["heap_min_free"] <= m["heap_free"],
       "heap_min_free=%d ≤ heap_free=%d（历史最低水位应 ≤ 当前值，否则口径错）"
       % (m["heap_min_free"], m["heap_free"]))
    ck(0 < m["heap_largest"] <= m["heap_free"],
       "heap_largest=%d ≤ heap_free" % m["heap_largest"])
    # 板载 8MB Octal PSRAM：总量应接近 8MiB（扣除 IDF 预留）
    ck(m["psram_total"] >= 4 * 1024 * 1024,
       "psram_total=%.1f MiB（板载 8MB Octal 已启用）" % (m["psram_total"] / 1048576.0))
    ck(0 < m["psram_free"] <= m["psram_total"],
       "psram_free=%.1f MiB ≤ total" % (m["psram_free"] / 1048576.0))

    # --- 分区表：必须与 partitions.csv 对得上 ---
    parts = j["partitions"]
    labels = {p["label"] for p in parts}
    ck(labels >= EXPECT_PART_LABELS,
       "分区表含全部 4 项 %s（实得 %s）" % (sorted(EXPECT_PART_LABELS), sorted(labels)))
    by = {p["label"]: p for p in parts}
    if "factory" in by:
        ck(by["factory"]["size"] == 0x400000 and by["factory"]["app"] == 1,
           "factory 4MiB 且标记为 app 分区")
    if "storage" in by:
        ck(by["storage"]["size"] == 0x100000 and by["storage"]["mounted"] == 0,
           "storage 1MiB 且 mounted=0（事实：未挂载）")
    ck(all(p["mounted"] == 0 for p in parts), "所有分区 mounted=0（本固件无文件系统）")

    # --- 任务栈水位 ---
    ts = j["tasks"]
    ck(len(ts) >= 3, "报出 %d 个任务（≥3）" % len(ts))
    for t in ts:
        ck(t["stack_free"] > 0, "任务 %-12s 栈剩余 %d B > 0（未爆栈）"
           % (t["name"], t["stack_free"]))
    small = [t for t in ts if t["stack_free"] < 1024]
    if small:
        warns.append("⚠️ 栈余量 <1KiB 的任务: %s"
                     % ", ".join("%s=%d" % (t["name"], t["stack_free"]) for t in small))

    # --- 交叉印证：health 也应是真固件 ---
    st2, b2 = get(dev, "/api/v1/health", KEY)
    h = json.loads(b2)
    hep = h.get("free_heap", 0)
    decomp = m["heap_free"] + m["psram_free"]
    print("\n交叉 /api/v1/health: wifi_rssi=%s ble_connected=%s uptime_s=%s free_heap=%s"
          % (h.get("wifi_rssi"), h.get("ble_connected"), h.get("uptime_s"), hep))
    print("口径分解: sysinfo heap_free(内部 %d) + psram_free(%d) = %d"
          % (m["heap_free"], m["psram_free"], decomp))
    ck(h.get("uptime_s", 0) > 0, "uptime_s>0（设备在跑）")

    # 🔴 这是 2026-10-02 修正过的断言。旧版写「sysinfo.heap_free 与 health.free_heap
    #    同量级」，那是**错的**：两者本就不是同一口径（前者仅内部 RAM，后者含 PSRAM），
    #    旧断言在修好口径后必然失败，且失败信号会被误读成"两个端点不一致"。
    #    正确的不变量是**分解恒等式**：总可用 = 内部可用 + PSRAM 可用。
    #    这条更强——它同时证明了两个端点各自算对了、且合起来没有重叠/漏算。
    ck(0 < m["heap_free"] < hep,
       "heap_free(内部 %d) < health.free_heap(总 %d)：确认两者口径确实不同（PSRAM 被排除）"
       % (m["heap_free"], hep))
    tol = max(300000, hep * 0.08)     # 两次请求有时差，堆会随 WiFi/BLE 活动波动
    ck(abs(decomp - hep) <= tol,
       "分解恒等式: heap_free+psram_free(%d) ≈ health.free_heap(%d)（Δ=%d，容差 %d）"
       % (decomp, hep, decomp - hep, int(tol)))

    print("\n== 结论 ==")
    if warns:
        for w in warns:
            print(w)
    if errs:
        print("FAIL %d 项:" % len(errs))
        for e in errs:
            print("  ✗ " + e)
        return 1
    print("PASS：全部 %s。" % "真机 syinfo 断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
