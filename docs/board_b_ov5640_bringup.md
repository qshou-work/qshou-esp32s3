# B 板（OV5640）固件准备与上机指南

> ⛔ **本文档已作废（2026-10-17）**：B 板已退货，后续不再适配 B 板。
> 项目唯一目标硬件 = **A 板（ESP32-S3 + OV5640，2026-10-25 换装）**。本文仅留作历史记录，
> 其中的烧录参数/引脚差异**不要再执行、不要再引用**。

> 背景：A 板 = ESP32-S3 + **OV2640**（已跑通全部基线）。
> B 板 = 同型号开发板 + **OV5640** 摄像头，用户新购。
> 除摄像头外两板相同。

---

## 0. 上机实录（2026-10-16，**已 bring-up 成功**）

结论先行：**B 板已完整启动、识别 OV5640 并连上现场网络 Huang**（192.168.2.218，
相机快照端到端实测通过）。但过程踩了三个坑，"烧同一份 bin 即可"的预测**不成立**
（就本源码树而言）——必须重编重烧。

### 0.1 三个坑（按发现顺序）

| # | 坑 | 症状 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | B 板 flash 是**博雅克隆片** | 16MB 配置下 bootloader MMU 映射错，console_init 前崩溃（零日志） | 型号码 0x4018 抄 W25Q128 虚报 16MB，实际 4MB；ROM 4 线读（QIO）拒绝 | 分区表重做 4MB 布局（factory 0x10000+0x200000 / storage 到 0x3F0000）+ sdkconfig FLASHSIZE_4MB/40m + 烧写必须 `--flash-mode dio` |
| 2 | **USB-Serial-JTAG 控制台陷阱** | CH343 UART 只看到 ROM 早期日志，app/panic 输出全丢失 | `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` 时 bootloader_console_init() 释放 UART0 引脚、printf 全切原生 USB 口；panic_print_char 也只写 USB-JTAG FIFO，无 USB 宿主时超时丢弃 | 主控制台切 `ESP_CONSOLE_UART_DEFAULT=y`（UART0/GPIO43-44 = CH343 COM4）+ 次控制台 USB-Serial-JTAG 保留为非阻塞镜像（A 板原生 USB 监控不受影响）。**双口方案已保留** |
| 3 | **WiFi API 调用顺序 bug**（本次最关键） | 启动后立刻静默重启死循环；串口回溯 `Saved PC 0x40386039` = app 的 `esp_restart_noos` | `platform_wifi_init()` 里 `apply_cred()`（= `esp_wifi_set_config`）在 `esp_wifi_set_mode()` **之前**调用 → 纯净 IDF v6.1 返回 `ESP_ERR_WIFI_MODE`(0x3005) → `ESP_ERROR_CHECK` → abort。**旧 dirty IDF 上侥幸不暴露**（A 板 10-08 日志佐证旧固件 WiFi 正常），换到纯净 v6.1 才炸 | `set_mode → apply_cred → start`（官方 station 示例同序）。源码注释已记录铁律 |

### 0.2 修复后 B 板启动日志实测（boot_b3.txt，全文无 rst/panic/abort）

```
bootloader banner ✓  ESP-IDF v6.1.0, flash 4MB/QIO@40MHz, 分区表 4MB 布局
八线 PSRAM ✓          vendor 0x0d(AP) 64Mbit 8MB, timing tuning 通过, memtest OK
OV5640 识别 ✓         camera: Detected OV5640 camera (PID=0x5640, addr=0x3c)
OV5640 初始化 ✓       PLL 反算 XVCLK=20MHz→SYSCLK=45MHz/PCLK=11.25MHz
camera warm-up ✓      6775 字节/帧（VGA JPEG，质量16）
BLE HID ✓             广播中（HID 0x1812, name="Qshou"）
http :80 ✓            25 routes + ui + stream + snapshot + events
mjpeg :81 ✓           独立任务 prio 4
mDNS ✓                qshou-0a0c.local
WiFi ✅（已配 Huang）     扫描 39 个 AP，连 Huang（rssi=-28, ch11 WPA2，
                       bssid 50:33:f0:c1:54:da），got ip 192.168.2.218，
                       /api/v1/sysinfo 与 /snapshot 端到端实测通
                       （前两次 4-way 握手超时后第 3 次成功，属正常抖动）
```

> 注：修复前的日志里 `QSHOU_CAM: OV2640 ready` 是**硬编码文案 bug**（已在
> `platform_camera.c` 修为按实际 PID 打印并重编验证通过，尚未烧录——待 WiFi
> 凭据到位后与 B 板一起烧）。

### 0.3 WiFi 已配（2026-10-16 解决）

`s_extra_creds[]` 追加现场网络 `{"Huang", "***"}`（`haitong` 保持第一优先，
回主场所后自动回落）。重编重烧后 B 板实测：

> 凭据说明（2026-10-27）：真实 SSID/密码已移出源码，改由
> `platform/esp32/qshou_wifi_creds.h`（不入库）提供，本文件不再记录明文密码。

```
candidate[0] 'haitong' NOT visible
candidate[1] 'Huang' IS visible on 2.4GHz
scan done, now connecting to candidate[1/2] 'Huang'
wifi:connected with Huang, aid = 9, channel 11, BW20
sta ip: 192.168.2.218, mask: 255.255.255.0, gw: 192.168.2.1
```

网络端到端验证（均 HTTP 200）：
- `GET /api/v1/sysinfo` → NVS/PSRAM/任务/分区全正常
- `GET /snapshot` → 640×480 JPEG（OV5640 实拍，画面正常）

顺手修复的附带问题：mDNS 主机名此前硬编码 `qshou-0a0c`（A 板 MAC 尾段），
B 板会同名撞播 → 改为按本机 WiFi STA MAC 尾段派生（B 板 = `qshou-b7f8.local`）。

### 0.4 A 板同步烧写（2026-10-16 晚，用户要求）

同一份产物（§2，md5 `181cc134…`）也烧到了 A 板，验证通过：

| 项 | A 板实测 |
|---|---|
| 串口 | COM3（CH343，S/N 5C4C202176）；⚠️ 换 USB 口前该口被某进程独占，见下 |
| MAC | `44:1b:f6:ca:0a:0c`（认板确认） |
| Flash | **正品 Macronix 16MB**（Manufacturer c2 / Device 2018 = MX25L128，eFuse quad）——与 B 板克隆片不同 |
| 烧写参数 | 同 B 板 `dio/4MB/40m`（4MB 布局 A/B 通用）；NVS 未动 |
| 启动 | `camera: Detected OV2640 camera`（addr 0x30）→ `QSHOU_CAM: OV2640 ready (PID=0x0026…)` ✓ |
| mDNS | `qshou-0a0c.local`（按 MAC 派生，与修复前硬编码值一致，A 板无行为变化）✓ |
| WiFi | `candidate[0] 'Huang' IS visible`（来自 **NVS**，A 板 NVS 主网络即 Huang）→ 连接成功 rssi=-22 → `got ip:192.168.2.160` ✓ |
| 端到端 | `GET /api/v1/sysinfo` 200；`GET /snapshot` 200 → **1024×768** JPEG（见下） |

> ⚠️ **A 板帧尺寸是 1024×768 而非 VGA**：A 板 NVS `qshou_cfg:cam_framesize` 存有 12
> （NVS 优先于编译期默认值 10）。这是 A 板原有配置，非固件差异。注意 KVM 标定
> （单应矩阵 H）是按 VGA 640×480 建立的，若要用标定需把 A 板改回 framesize=10
> （`/api/v1/camera` 运行时切换或写 NVS 冷生效）。
> 另：抓到的快照画面几乎全黑（镜头朝向暗面/未取下保护盖），相机本身工作正常
> （warm-up 16145 字节/帧）。

> ⚠️ **环境坑（Windows 串口被独占）**：首次尝试烧 A 板时 COM3 恒返回
> `PermissionError(13, 拒绝访问)`，`pnputil /restart-device` 无效，
> `pnputil /disable-device` 因设备在用而失败并留下「等待重启」挂起态（设备变 Error）。
> 期间排查用的「打开端口」测试脚本被 timeout 杀掉后**会留下持有句柄的僵尸 python 进程**
> （`taskkill /F` 也杀不掉），进一步加剧占用。
> **最终解法：把 USB 线换到主机另一个 USB 口**（CH343 位置从 `1-11.4` 变 `1-14`），
> 端口立即恢复。教训：串口被独占时，优先物理换口/拔插，不要在软件层反复 disable。

---

## 1. 结论：现有固件大概率**直接可用**，无需重编

已逐项核实（全部为实测，非推测）：

| 核查项 | 结论 | 证据 |
|---|---|---|
| OV5640 驱动是否编入 | ✅ **已编入** | `sdkconfig:4424 CONFIG_OV5640_SUPPORT=y`；`managed_components/espressif__esp32-camera/sensors/ov5640.c` 存在 |
| 组件是否支持 OV5640 | ✅ | esp32-camera **v2.1.7**，`idf_component.yml` 描述含 "OV2640, OV3660, **OV5640**, OV7670, OV7725" |
| sensor 如何选择 | ✅ **运行时自动探测** | `esp_camera_init()` 经 SCCB 读 PID；`OV5640_PID = 0x5640`。工程代码用 `esp_camera_sensor_get()` + NULL 防御的函数指针，**与 sensor 无关** |
| 摄像头引脚 | ✅ 与 A 板一致 | 同型号板子，FPC 座引脚由板子硬件决定；工程里 16 个引脚全是可 `-D` 覆写的宏 |
| XCLK 20MHz 能否驱动 OV5640 | ✅ 可以 | `ov5640.c::calc_sysclk()` 按 `sensor->xclk_freq_hz` **反算 PLL**，不写死 24MHz |
| framesize 枚举是否通用 | ✅ 通用 | 实测 esp32-camera v2.1.7 枚举：`6=QVGA(320×240)`、`10=VGA(640×480)`、`13=HD(1280×720)`、`15=UXGA(1600×1200)`。**枚举属驱动层，与 sensor 无关** |
| 探测失败会不会让板子失联 | ✅ **不会** | `main.c:82-83` 对 `platform_stream_start()` 失败只记日志继续；`platform_wifi_init()` 失败同样继续（`main.c:74-76`）。且 `esp_camera.c` 内 **`ESP_ERROR_CHECK` 出现 0 次**，不会 abort |
| B 板 WiFi | ⚠️ **非开箱即通** | B 板 NVS 空 → 只用内置备用 `haitong`；但 B 板当前位置扫不到该 AP（见 §0.3） |

**所以策略：先把现有固件原样烧进 B 板，用启动日志确认 OV5640 是否被识别。**

---

## 2. 固件产物（最终版 = WiFi 修复 + Huang 凭据 + 相机日志 + mDNS 派生，2026-10-16 已烧 B 板）

路径：`qshou/firmware/build/`

| 文件 | 大小 | MD5 |
|---|---|---|
| `qshou.bin` | 1,438,176 B | `181cc134709cd2fe61f4d9fba8d5686c` |
| `bootloader/bootloader.bin` | 22,560 B | `0543a2c4e33f7be7963e32ebb87f707d` |
| `partition_table/partition-table.bin` | 3,072 B | `c22b1064cc6ff5032363b15ab0e1a46a` |

烧写地址与参数（`build/flash_args` 已内置，**必须显式带上**——B 板 flash 克隆片，见 §0.1 坑 1）：

```
--flash-mode dio --flash-size 4MB --flash-freq 40m
0x0      bootloader/bootloader.bin
0x8000   partition_table/partition-table.bin
0x10000  qshou.bin
```

> ⚠️ **`0x9000` NVS 不在列表内** → 不会擦除（BLE bonding 保留）。
> 纪律 #3 依旧成立：任何时候都不要擦 NVS。
> 本版本已烧录 **B 板与 A 板**并完成端到端验证（见 §0.2/§0.3/§0.4）。

---

## 3. 烧录步骤（B 板接上 USB 后）

### 3.1 先认串口

B 板插上后，Windows 会分配一个新 COM 口（A 板占着 COM3）。
`--flash-only` 需要显式给端口，先列出：

```bash
"/c/Users/<username>/.espressif/python_env/idf6.1_py3.13_env/Scripts/python.exe" -c \
  "import serial.tools.list_ports as lp; [print(p.device,'|',p.description,'|',p.hwid) for p in lp.comports()]"
```

认板小技巧：用 esptool 读 MAC。**A 板 MAC = `44:1b:f6:ca:0a:0c`**，B 板必然不同：

```bash
"/c/Users/<username>/.espressif/python_env/idf6.1_py3.13_env/Scripts/python.exe" \
  -m esptool --chip esp32s3 --port COM4 read_mac
```

### 3.2 烧写（B 板实测可用命令，2026-10-16）

B 板 COM4 端口会闪断（flapping）→ 直接跑 esptool 大概率 `Failed to connect`，
必须用抢窗口工具。**已实测成功**的命令：

```bash
cd "C:/Users/<username>/WorkBuddy AI/2026-10-15-19-24-37"
"C:/Users/<username>/.espressif/python_env/idf6.1_py3.13_env/Scripts/python.exe" \
  .workbuddy-ai/tools/grab_cmd.py COM4 120 \
  write-flash --flash-mode dio --flash-size 4MB --flash-freq 40m \
  0x0     qshou/firmware/build/bootloader/bootloader.bin \
  0x8000  qshou/firmware/build/partition_table/partition-table.bin \
  0x10000 qshou/firmware/build/qshou.bin
```

`grab_cmd.py <PORT> <SECONDS> [esptool args...]`：等串口出现且可打开（抢窗口），
再以 `--before default-reset --connect-attempts 12` 执行。烧写约 78 秒。

> 备选（需编译环境就绪）：`idf_build_flash.py --flash-only --port COM4`，
> 它读 `build/flash_args`（已含 dio/4MB/40m），但仍需 COM 口不 flapping。

### 3.3 抓启动日志，确认 sensor 型号

```bash
cd "C:/Users/<username>/WorkBuddy AI/2026-10-15-19-24-37/qshou"
"/c/Users/<username>/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe" \
  capture_boot.py COM4 15 /tmp/board_b_boot.txt
```

`capture_boot.py`（本次新增）会主动复位板子并抓 15 秒日志，最后打印
**MARKERS** 与 **VERDICT** 两段判读。

---

## 4. 预期结果与判读

### 4.1 成功（OV5640 被识别）

启动日志里应出现（B 板实测，见 §0.2）：

```
I (xxxx) camera: Detected OV5640 camera
I (xxxx) ov5640: Calculated XVCLK: 20000000 Hz, ...
I (xxxx) ov5640: Set PLL: ...
I (xxxx) QSHOU_CAM: camera warm-up ok
I (xxxx) MAIN: Qshou firmware ready
```

> ⚠️ 旧构建里 `QSHOU_CAM: OV2640 ready` 这行型号是**写死的文案 bug**
> （已修为按实际 PID 打印，pending rebuild，见 §6.1）——
> **真正的探测结果一律看 `camera: Detected ...` 那行**。

然后：

```bash
export NO_PROXY=<B板IP>
curl -s --noproxy '*' http://<B板IP>/api/v1/sysinfo | head -c 300
curl -s --noproxy '*' http://<B板IP>/api/v1/camera -X POST \
     -H 'Content-Type: application/json' -d '{"action":"capture"}' -o /tmp/b.jpg -w '%{http_code} %{size_download}\n'
```

拿到 JPEG 即大功告成。

### 4.2 若 OV5640 **未**被识别

症状：日志无 `Detected OV5640 camera`，或出现
`E (xxxx) camera: Camera probe failed with error 0x105` / `esp_camera_init failed`。

**但板子不会失联** —— WiFi / HTTP 照常起来（见 §1 表最后两行）。排查顺序：

1. **确认摄像头插好**：FPC 排线方向、卡扣是否锁紧。OV5640 模块比 OV2640 耗电大，接触不良最常见。
2. **确认 SCCB 通**：`E ... probe failed` 通常是 SIOD/SIOC（本工程 = GPIO4/GPIO5）不通。
3. **确认供电**：OV5640 峰值电流更高，若板子 3.3V LDO 余量不足会复位或探测失败。
4. **确认模块型号**：从日志的 `Detected <X> camera` 看它实际报了什么。若报的是别的型号，
   说明模块可能不是 OV5640（或 PID 非常规）。
5. **换 XCLK 试**：OV5640 更常见的是 24MHz。改宏即可，不必动逻辑：
   ```
   QSHOU_CAM_XCLK_FREQ_HZ = 24000000
   ```
   （改法见 §6；改宏会触发部分重编。）

---

## 5. 两板共存注意事项

- **MAC 不同** → DHCP 会给不同 IP。B 板 IP 从启动日志 `got ip:` 行读，或用
  mDNS 名。✅ mDNS 主机名已按本机 WiFi STA MAC 尾段派生（2026-10-16 修复：
  此前硬编码 `qshou-0a0c`，B 板会与 A 板撞名）——A 板 `qshou-0a0c.local`、
  B 板 `qshou-b7f8.local`（B 板 WiFi MAC = `9c:13:9e:a9:b7:f8`），
  可被 NVS `qshou_cfg:mdns_host` 覆盖。
- **BLE 名字相同**（都是 `Qshou`）→ 手机蓝牙列表里会看到两个同名设备，
  配对时靠 MAC 区分。若需区分，改 `main/main.c` 的 `BLE_DEVICE_NAME`。
- **控制端要指向目标板 IP**：`verify_mcp_live.py <板IP>`。
  2026-10-16 实测：**A 板 192.168.2.160**（`qshou-0a0c.local`，OV2640，NVS 帧尺寸 1024×768）、
  **B 板 192.168.2.218**（`qshou-b7f8.local`，OV5640，VGA）。IP 由 DHCP 分配会漂移，优先用 mDNS 名。
- **NVS 独立**：两板的 WiFi 凭据 / BLE bonding / 标定数据互不影响。
  A 板 NVS 主网络已配为 Huang（候选表 3 条：NVS Huang + 内置 haitong + 内置 Huang）。
  B 板是全新 NVS，BLE 需要重新配对。

---

## 6. 可选增强（等 B 板验证通过后再决定）

若确认 OV5640 工作正常，可做三处小改（都是 `.c`/宏改动 → 增量编译，很快）：

1. **日志按实际 sensor 报型号**（`platform_camera.c`）
   现在写死 `"OV2640 ready"`，改为从 `esp_camera_sensor_get()->id.PID` 映射出
   实际型号字符串。
   ✅ **已完成并重编验证**（2026-10-16；md5 见 §2，尚未烧录，待 WiFi 凭据一起烧）。

2. **放开 framesize 上限**（`platform_camera.c:306-310`）
   现在 `if (size < 0 || size > 13) return -1;`，注释写"OV2640 支持到 UXGA(13)"，
   但枚举 13 实际是 **HD 1280×720**、UXGA 是 **15**。这行把 OV5640 的
   5MP（枚举 25）也一并挡住了。OV5640 驱动侧上限是 `FRAMESIZE_5MP`。

3. **加大帧缓冲**（`platform_camera.c:116`）
   `QSHOU_CAM_BUF_CAP = 512KB`。VGA 单帧 JPEG 约 15–35KB，绰绰有余；
   但若跑 5MP，单帧 JPEG 可能 500KB–1MB → 会触发 `frame too large, dropped`。
   上高分辨率需同步放大。

> ⚠️ 注意：KVM 场景的标定数据（单应矩阵 H、counts↔像素映射）是按
> **VGA 640×480** 建立的。换分辨率会**使现有标定失效**，需重新标定（需人工看屏）。
> 所以除非有明确收益，**建议 B 板也跑 VGA**，只把 OV5640 当作画质更好的 VGA 相机用。

---

## 7. 一句话总结

**B 板已于 2026-10-16 bring-up 并联网成功**（OV5640 识别、PSRAM/相机/BLE/HTTP/推流
全通、连 Huang 192.168.2.218、快照端到端实测通过，详见 §0）。三个坑（克隆 flash、
USB-JTAG 控制台、WiFi API 顺序）已全部修复；另顺手修复 mDNS 主机名硬编码撞名问题。
**A 板也已同步烧入同一份固件并验证通过**（OV2640 识别、连 Huang 192.168.2.160、
mDNS qshou-0a0c.local，详见 §0.4）——两板现运行字节相同的固件。
当前源码树 + 4MB 分区表 + dio/40m 烧写参数即为 A/B 通用配方。
