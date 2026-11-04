# Qshou — 设备延迟深查报告

- 日期：2026-10-02 19:20
- 对象：设备 `10.59.228.53`（mDNS `qshou-0a0c.local`），固件已在机
- 方法：真机 ICMP 采样 + 同步对照 + 相关性分析 + HTTP 分段计时 + 真推流负载对照
- 复现工具：`tools/diag_ping_correlate.py`、`tools/diag_http_breakdown.py`

---

## 一、TL;DR

1. **ping 抖的瓶颈在「AP↔ESP32 的无线最后一跳」**，与有线侧/网关**基本无关**（相关系数 r=0.18）。
2. **`WIFI_PS_MIN_MODEM`（modem sleep）不是主因** —— 用真实 MJPEG 推流做对照，STA 全程常驻活跃，延迟**毫无改善**（p50 405ms vs 空闲 368ms）。
3. **真正吃掉体感的其实不是 ping，是应用层**：
   - 48KB 的 UI 页面下载要 **8.7 秒**（实测 **5.4 KB/s**），因为 lwIP 的 TCP 窗口只有 5760 字节；
   - 每次轮询若新建 TCP 连接，一次握手实测 **2667ms**，而复用连接**只要 341ms（12.8 倍差）**。
4. 因此：**要改善「打开页面/点按响应」的体感，收益最大且最确定的在应用层；要改善 ping 本身，则受限于 2.4G 空口，改善空间有限且代價高。**

---

## 二、实测数据

### 2.1 拓扑（先确认问题出在哪一段）

| 项 | 实测 |
|---|---|
| 本机接口 | **以太网** `10.59.228.216`（WLAN 无 IPv4，不会形成双跳无线） |
| `tracert` 到设备 | **1 跳直达**，无中间路由/中继 |
| AP/网关 | `.104`，MAC `06:0e:99:48:54:92`，SSID `Huang`，**2.4G 信道 13** |
| 设备 RSSI | **-23 dBm**（≥-55 为优，属极佳） |
| 本机 RSSI | -26 dBm，802.11n 144Mbps |

### 2.2 延迟对照（同步采样 120 秒，同一时间窗）

| 目标 | 是否经无线空口 | n | 丢包 | min | p50 | p90 | max | avg |
|---|---|---|---|---|---|---|---|---|
| 网关 `.104` | 否（纯有线+桥接） | 117 | 1 | 1ms | **25ms** | 180ms | 275ms | 61ms |
| 设备 `.53` | **是** | 80 | **11 (12%)** | 2ms | **368ms** | 1035ms | 2043ms | 464ms |

**10 秒窗口相关系数 r = 0.18（n=12）→ 两者基本独立。**

判据：若瓶颈在共享链路（本机↔AP），两者应同步涨跌（r 接近 1）；实测基本独立 → 额外约 **340ms** 由无线最后一跳产生。

### 2.3 排除性实验

| 假设 | 实验 | 结果 | 结论 |
|---|---|---|---|
| WiFi modem sleep（DTIM 排队） | 真 MJPEG 推流 60s（**实收 338.8KB**，STA 必然常驻 active）+ 同步 ping | p50 **405ms**，vs 空闲 368ms，**无差别** | ⛔ **已排除** |
| 同上（早期弱版本） | 每 1.2s 一次小 GET + ping | avg 663ms（未改善） | ⚠️ 该实验**不严谨**：活跃度不足以让 STA 退出休眠，曾一度误导我过度否定休眠因素 |
| 信号弱/重传 | RSSI | -23 dBm（极佳） | ⛔ 排除 |
| 推流占带宽 | `last_frame_ms=0`（未推流）时采样 | 空闲照样抖 | ⛔ 排除 |

> 说明：早期记忆（同日 15:25）曾因"网关也 78ms"把主因判为网络侧。**本次数据订正该结论**：网关侧虽有轻微抖动（p50 25~49ms，略高于理想值），但它在统计上与设备延迟独立，不足以解释设备的 368ms。

### 2.4 应用层分段计时（`tools/diag_http_breakdown.py`）

| 请求 | 连接 | 握手 | TTFB | 下载 | 合计 |
|---|---|---|---|---|---|
| `GET /api/v1/health`（337B） | 新建 | **2667ms** | 1688ms | – | **4356ms** |
| 同上，**同一 socket 第 2 次** | 复用 | 0ms | **341ms** | – | **341ms** |
| `GET /`（46,936B UI 页） | 新建 | 479ms | 1279ms | **8689ms** | **10447ms** |

推算：
- 复用 vs 新建：**12.8 倍**差距 → 每请求一次 TCP 握手的代价在 300ms~2.7s 之间抖动。
- 大页面传输速率 = 46936 / 8.689 ≈ **5.4 KB/s**。
- lwIP `TCP_WND_DEFAULT = 5760` 字节，RTT ≈ 0.4s → **单窗口吞吐上限 = 5760 / 0.4 ≈ 14 KB/s**，与实测同量级。**窗口是硬瓶颈。**

---

## 三、根因分层

| 层 | 归因 | 量级 | 可否改 | 代价 |
|---|---|---|---|---|
| L1 空口 | 2.4G 空口竞争/干扰、WiFi×BLE 共存抢占 | ~340ms（p50） | 部分 | 高/未知 |
| L2 传输层 | lwIP TCP 窗口仅 5760 → 吞吐锁死 ~14KB/s | 48KB 页面 8.7s | ✅ 可改 | 中（全量重编） |
| L3 连接层 | `max_open_sockets=7` + lwIP `MAX_SOCKETS=10` + `lru_purge=true` → 并发稍多即回收 socket → 被迫反复重连 | 每次重连可能吃到 SYN 重传的秒级延迟 | ✅ 可改 | 低~中 |
| L4 应用策略 | `ui.html` 每 3s 轮询；每次可能新建连接 | 放大 L3 | ✅ 可改 | 低（增量） |

已知加重因素（尚未定量）：BLE 连接参数 `itvl=12`（**15ms**）+ `latency=0` → **每秒约 67 次 RF 事件**，即使完全不操作也不停歇，每次都要与 WiFi 共存仲裁。固件目前**没有**可断开 BLE 的端点，因此无法做纯自助对照。

---

## 四、修改建议（按「性价比」排序）

### 建议 1 — 加大 lwIP TCP 窗口（**收益最大**）

```ini
# sdkconfig.defaults
CONFIG_LWIP_TCP_WND_DEFAULT=32768
CONFIG_LWIP_TCP_SND_BUF_DEFAULT=32768
CONFIG_LWIP_TCP_RECVMBOX_SIZE=12
```

- 预期：单窗口吞吐由 ~14KB/s 提升到 ~82KB/s，**48KB 页面 8.7s → 约 0.6~1.2s**。
- 风险：⚠️ **改 `sdkconfig.defaults` = 触发全量重编**（本项目有 ccache，约 15 分钟）。
- 内存：项目已启用 `CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y`，增大缓冲会优先落 PSRAM，风险可控；内部 RAM 当前空闲约 80KB，属于需要盯的数量级。

### 建议 2 — 提高可用 socket 数，抑制 LRU 回收

```c
// firmware/platform/esp32/platform_net.c
cfg.max_open_sockets = 12;   // 原 7（默认值）
cfg.lru_purge_enable = true; // 保留，作为兜底
```
```ini
CONFIG_LWIP_MAX_SOCKETS=16   # 原 10（需同步改，否则 httpd 也拿不到）
```
- 预期：浏览器多并发时不至于把健康连接回收掉，避免每次轮询都重走握手。
- 风险：低；但 socket 增多会增加内存占用，需与建议 1 一并评估。
- 成本：改 `.c` 为增量编译；改 `MAX_SOCKETS` 属 sdkconfig → 全量。建议与建议 1 一起做，只付一次全量代价。

### 建议 3 — 放宽轮询频率

`firmware/main/ui.html` 现为 `setInterval(refreshHealth, 3000)`。在高延迟链路上，3s 间隔 + 每次可能重连，会持续给设备压力。

- 建议改为 **6~10s**，或在页面不可见（`document.hidden`）时暂停轮询。
- 成本：只改 `ui.html` → 增量编译，约几十个目标、2 分钟。

### 建议 4 — 明确保持现状的项（**不要动**）

- ⛔ **不要改 `WIFI_PS_MIN_MODEM`，更不要设 `WIFI_PS_NONE`**：历史实机已证明 PS_NONE 会导致**推流期间 HID 完全不执行**。且本次证明 PS 并非主因，改它没有收益，只有风险。
- ⛔ **不要改 coexist 偏好**（`BALANCE` 已是最佳）。
- ⚠️ `listen_interval` 只对 `WIFI_PS_MAX_MODEM` 生效（IDF 头文件明文），在当前的 `MIN_MODEM` 下**设置了也没用**；`MIN_MODEM` 的唤醒周期由 **AP 的 DTIM** 决定，设备侧无法调整。

### 建议 5 — 需你在现场配合才能定性的项（无线最后一跳）

若要继续压 ping，只能从这两处入手，且都需要现场操作：

1. **BLE 对照**：暂时关闭已配对安卓设备的蓝牙（或让设备断连），再跑 `tools/diag_ping_correlate.py 120`。若设备 p50 骤降，则定量确认 **WiFi×BLE 共存抢占**是主因。
2. **AP 侧**：登路由器后台查看/调整 DTIM 间隔，或把 AP 换到更干净的信道（当前 ch13）；若附近有持续占用 2.4G 的非 WiFi 干扰源（蓝牙设备、无线音视频），需物理规避。

---

## 五、方案 A（`MAX_MODEM` + `listen_interval=1`）实测结果 → 无效，已回退

### 5.1 一个必须先讲清的前提

`listen_interval` **只在 `WIFI_PS_MAX_MODEM` 下生效**（IDF 头文件原文：`listen interval ... when WIFI_PS_MAX_MODEM is set`）。本项目原先**从未设置**该字段 → 隐式取 `0`，而规定 **"Defaults to 3 if set to 0"**，即实际每 3 个 beacon 才醒一次。

所以"只改 listen_interval"是不起作用的，必须**同时**把 PS 切成 `MAX_MODEM`。这也是它与当年翻车的 `PS_NONE` 的不同之处：`MAX_MODEM` **仍保留 modem sleep**（CPU 也睡），只是在"每 DTIM 醒"和"永不睡"之间取一个更靠近清醒的点，理论上 BLE 的保底时隙仍在。

### 5.2 实测

串口确认改动确实生效、且未触发 fallback：

```
I (2015) wifi:Set ps type: 2, coexist: 1                 ← ps type 2 = MAX_MODEM
I (2016) QSHOU: wifi STA init done, ssid=Huang (ps=max_modem, listen_interval=1)
```

同参数（`tools/diag_ping_correlate.py 120`）前后对比：

| | n | 丢包 | p50 | avg | p90 | max |
|---|---|---|---|---|---|---|
| 改动前 `MIN_MODEM` | 80 | 11 | **368ms** | **464ms** | 1035ms | 2043ms |
| 改动后 `MAX_MODEM`+li=1 | 78 | 10 | **557ms** | **703ms** | 1492ms | 2337ms |

**延迟不降反升约 51%。**

> ⚠️ 该对比含混淆变量：测量期间 `wifi_rssi` 由 -23dBm 漂移到 -53~-82dBm，链路本身在恶化。因此具体数值**不宜过度解读**；但"**毫无改善**"这一点是明确的，且与前文推流对照的结论完全一致 —— 推流已让 STA 常驻活跃（等效不睡）时 p50 仍为 405ms ≈ 空闲 368ms，说明瓶颈不在唤醒节奏。

### 5.3 HID 回归（红线守卫）

用 `.tmp/jschk/verify_hid_after_ps.py` 专门复现历史失效场景（**推流期间**发手势）：结果：推流中 `dir(120,0)`、`dir(-120,0)` 均**即时**执行、净位移精确、`gest_drop=0`。（第 3 条仅 HTTP 层面超时，设备实际已执行，不是 PS_NONE 那种"停流才集中冲刷"的故障同型。）即：**该 PS 模式未伤 HID，但也无收益。**

### 5.4 决策

已回退 `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)`；保留 `listen_interval=1`（在 MIN_MODEM 下被忽略、无副作用，留作文档）。结论已写进代码注释，避免未来重复试错。

**→ 别再靠调 PS / listen_interval 压 ping。**

---

## 六、下一步

请确认执行范围（建议 1、2 涉及**全量重编**，按约定需你明确同意）：

- **A（推荐）**：建议 1 + 2 + 3 一起做，付一次全量重编代价，换来"页面秒开 + 轮询不再重连"。
- **B（保守）**：只做建议 3（纯增量，2 分钟），先看轮询放宽后的体感。
- **C**：先做建议 5 的 BLE 对照试验（不改动任何代码），定性后再决定要不要碰无线层。

复现命令：

```bash
python tools/diag_ping_correlate.py 120      # 网关/设备同步采样 + 相关性
python tools/diag_http_breakdown.py 10.59.228.53   # HTTP 分段耗时
```
