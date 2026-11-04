# 到货上机指南（ESP32-S3-N16R8 + OV2640）

> 场景：硬件已到手。此前所有「零硬件期」准备（构建链路、接缝实现、交叉预检）
> 现在要接受真机检验。本文按**风险优先级**排序，逐步执行、每步可验证。
> 配套：`docs/partition_pin_plan.md`（引脚预案）、`docs/android_prep.md`（被控端清单）。

---

## 0. 头三件事（否则一定卡住）

| # | 事项 | 为什么关键 |
|---|---|---|
| 1 | **核对实际板型的摄像头引脚** | 代码里是 **ESP32-S3-EYE 参考分配**（XCLK=15/PCLK=13/…）。若你的模组/板子不同，camera 初始化必失败。**这是第一天最大的不确定性。** |
| 2 | **拉取 esp32-camera 组件** | 沙箱此前无法访问组件注册表（HTTP 000），`platform_camera.c` 只用桩头过了语法预检，**从未真正编译过**。 |
| 3 | **确认板型容量丝印** | N16R8（16MB Flash + 8MB Octal PSRAM）vs N8R8。若为 N4 系列（无 PSRAM），帧缓冲方案需改。 |

---

## 1. 环境自检（PC 侧）

```bash
# ESP-IDF 工具链是否就位
ls ~/esp/esp-idf/tools/idf.py
xtensa-esp32s3-elf-gcc --version 2>/dev/null || \
  ls ~/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe

# 串口设备（插上板子后）
# Windows: 设备管理器 → 端口(COM & LPT) → 记下 COMx
#   板子通常用 ESP32-S3 原生 USB（USB-Serial-JTAG）或 CH340/CP210x
```

> ⚠ **USB 线必须是数据线**，不是纯充电线。这是最常见的"插上没反应"原因。

---

## 1.5 ⚠⚠ 两个 Type-C 口：一个 `usb-otg`，一个 `usb-uart`

实物板子有两个 Type-C 座，丝印分别是 **`usb-otg`** 和 **`usb-uart`**。
**这两个口走的是芯片里完全不同的两套 USB 外设，插错了就读不到日志。**

### 1.5.1 两个口的本质区别

| 丝印 | 走哪条线 | 对应芯片外设 | VID:PID | 能力 |
|---|---|---|---|---|
| **`usb-otg`** | 直连 ESP32-S3 的 **GPIO19/20**（D−/D+） | **USB-OTG / USB-Serial-JTAG** | **`303A:4001`**（S3 原生 USB-Serial-JTAG） | ✅ **烧写** ✅ **应用 console 日志** ✅ 可做 USB Host/Device |
| **`usb-uart`** | 经 **板载 USB-UART 桥**（CH343/CP2102 等）→ 接芯片 **UART0**（GPIO43/44） | 桥接芯片的串口 | 桥片自己的 VID:PID（如 `1A86:55D3` = WCH CH343） | ✅ **烧写**（走 UART 引导） ✅ bootloader + `CONFIG_ESP_CONSOLE_UART` 日志 ❌ 拿不到 USB-Serial-JTAG 日志 |

> ### 🔑 更正：`303A:1001` 到底是什么？（本板实测的关键澄清）
>
> 本项目**实测**连上 S3 的口是 **`VID_303A&PID_1001`（枚举为 COM7）**，其端口描述
> 直接显示 `USB VID:PID=303A:1001 SER=44:1B:F6:CA:0A:0C`。
>
> 查乐鑫官方文档（ESP-IoT-Solution《USB 设备常量与 COM 编号》）：
> **ESP32-S3 的 ROM Code 内建 USB-Serial/JTAG 描述符就是 `VID 0x303A` + `PID 0x1001`**，
> Serial = **MAC 地址字符串**（正是 `44:1B:F6:CA:0A:0C`，与 esptool 读到的 MAC 一致 ✅）。
> 而 `PID 0x4001`（`303A:4001`）**不是** S3 的 ROM 下载模式标识，而是
> **esptool stub flasher 运行后**呈现的 USB-Serial/JTAG 复合设备。
>
> **结论**：
> - **`303A:1001` = 真正的 ESP32-S3 原生 USB-Serial/JTAG**（bootloader / 应用运行态）
> - **`303A:4001` = 同一个口的 esptool stub 下载态**（同一物理口，不同阶段 PID 不同）
>
> 所以**两个 PID 是同一个 `usb-otg` 口在不同状态下的两种身份**，不是两个不同设备。
> 若板上真的另有 CH343（`1A86:55D3`），那才对应 `usb-uart` 口。


### 1.5.2 关键结论：**口和 console 配置必须配对，否则「烧成功但没日志」**

本板实测曾出现「烧写成功、串口 12 秒无任何输出」，根因就是**错配**：

- 固件配了 `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` → 日志只从 **`usb-otg`** 口出；
- 但当时 monitor 读的是 **`usb-uart`** 对应的端口 → 该口只在 UART0 有输出时才有数据，此时**必然全空**。

**配对口诀**：

| 想让日志从哪出 | sdkconfig 配哪个 | 插哪个口 | 读哪个 COM |
|---|---|---|---|
| **`usb-otg`**（推荐，S3 原生） | `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`<br>`CONFIG_ESP_CONSOLE_UART_DEFAULT=n` | `usb-otg` | `VID_303A&PID_4001` 那个 |
| **`usb-uart`**（桥片） | `CONFIG_ESP_CONSOLE_UART_DEFAULT=y`<br>（UART0，`UART_NUM=0`） | `usb-uart` | 桥片 VID（如 `1A86:55D3`）那个 |

> 💡 **两个口可以同时插**。这时会看到**两个串口**同时出现，恰好可以一个口烧写、另一个口看日志，或直接对照验证。

### 1.5.3 实测辨识方法（本板真实数据）

`usb-otg`（原生 USB-Serial-JTAG）的关键特征：

```
USB\VID_303A&PID_4001\...        ← 复合设备：MI_00 = CDC 串口，MI_02 = JTAG
Chip type:  ESP32-S3 (QFN56) (revision v0.2)
USB mode:   USB-Serial/JTAG
MAC:        44:1b:f6:ca:0a:0c
```

`usb-uart`（桥片）的关键特征：

```
USB\VID_1A86&PID_55D3\5C4C202176   ← 1A86 = WCH（CH343 一类）；CP210x 则是 10C4:EA60
```

**快速判定命令（PowerShell）**：

```powershell
Get-PnpDevice -Class Ports |
  Select-Object Status, FriendlyName, InstanceId | Format-List
# VID_303A&PID_4001  →  usb-otg（原生）
# 其他 VID（1A86/10C4/0403…）→  usb-uart（桥片）
```

### 1.5.4 调参记录（实测踩坑留痕）

- **`sdkconfig.defaults` 里 choice 项要显式互斥**。只写
  `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` + `CONFIG_ESP_CONSOLE_UART_DEFAULT=n`
  **不够** —— 与残留 `sdkconfig` 合并时可能仍是旧值。必须把同一 choice 内其余项
  一并写 `=n`：`UART_CUSTOM` / `USB_CDC` / `NONE`。
- **校验是否真的生效**，看 `firmware/build/config/sdkconfig.json`：
  ```bash
  grep -oE '"ESP_CONSOLE_[A-Z_]*"[^,]*' build/config/sdkconfig.json
  # 期望：ESP_CONSOLE_USB_SERIAL_JTAG: true
  #       ESP_CONSOLE_UART_DEFAULT: false
  #       ESP_CONSOLE_UART_NUM: -1
  ```
- **删 `sdkconfig` 不等于会重新读 defaults**：若 `build/` 还在，CMake 会复用
  `build/config/` 下的缓存。**必须 `rm -rf build sdkconfig` 再 `set-target`**。

---

## 2. 第一步：只验 camera 引脚（**强烈建议先做这个**）

在接 BLE 之前先把 camera 单独跑通，避免两个变量混在一起排查。

### 2.1 核对引脚

打开 `firmware/platform/esp32/platform_camera.c`，核对顶部宏与实际板型：

```c
#define QSHOU_CAM_PIN_XCLK   15    // ← 比对你板子的丝印/原理图
#define QSHOU_CAM_PIN_PCLK   13
#define QSHOU_CAM_PIN_VSYNC   6
#define QSHOU_CAM_PIN_HREF    7
#define QSHOU_CAM_PIN_SIOD    4    // SCCB SDA
#define QSHOU_CAM_PIN_SIOC    5    // SCCB SCL
#define QSHOU_CAM_PIN_D0..D7  11,9,8,10,12,18,17,16
```

**两种改法（都不碰逻辑代码）**：
- 改宏（推荐，清晰）；
- 或用编译定义覆写：`idf.py -DPLATFORM_KIND=esp32 -DQSHOU_CAM_PIN_XCLK=15 build`
  （宏已用 `#ifndef` 包裹，可安全覆写）。

### 2.2 拉组件 + 切 esp32 接缝构建

```bash
cd firmware
idf.py add-dependency "espressif/esp32-camera^2.0.0"
idf.py -DPLATFORM_KIND=esp32 build
```

> **这是 `platform_camera.c` 第一次真正参与编译**。若报 `esp_camera.h` 相关
> 类型/字段错误，说明桩头与真实组件有差异——按真实头文件修正（桩头见
> `build/_precheck/stub/esp_camera.h`，是照 v2.x 摘录的）。

### 2.3 烧写 + 看串口

```bash
idf.py -p COMx flash monitor        # Ctrl+] 退出 monitor
```

**期望日志**：
```
I (xxx) QSHOU_CAM: OV2640 ready (DVP, JPEG, xclk=20000000Hz, pins X=15 P=13 V=6 H=7)
```

**若失败**，对照排查：
| 现象 | 可能原因 |
|---|---|
| `esp_camera_init failed: 0x105` / `ESP_ERR_NOT_FOUND` | SCCB 通信失败 → SDA/SCL 引脚错，或模组未供电 |
| `Failed to get the frame on time` | XCLK/PCLK/VSYNC/HREF 引脚错 |
| 图像花屏/条纹 | 数据总线 D0–D7 顺序错 |
| PSRAM 相关报错 | `CONFIG_SPIRAM_MODE_OCT` 与实际 PSRAM 类型不符（试 QSPI）|
| 完全不启动 | USB 线是充电线；或需按住 BOOT 再点 RESET 进下载模式 |

### 2.3.1 🔴 实测踩坑：「烧写成功 + Hash verified，但复位后卡在 download 模式」

**本板真实遇到的第 2 号坑**，症状与根因如下。

**症状**（串口 151 字节，反复出现）：
```
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))
waiting for download
```
- `esptool flash` 报告 `Wrote 1212016 bytes ... Hash of data verified` ✅
- `esptool -p COM7 --before no-reset read-flash 0x0 0x20` 读回头部
  `e9 03 02 4f ...` = 合法 bootloader image ✅ → **固件确实写进去了**
- 但每次复位都 `boot:0x0 (DOWNLOAD)`，**永远不跑应用**

**根因**：`boot:0x0` 表示复位采样时 **GPIO0 = 低**。
ESP32-S3 的 strapping：GPIO0 低 → 强制进 ROM 下载模式。
即 **BOOT 键(GPIO0) 在复位瞬间被拉低**，常见于：
1. 板子 BOOT 键卡住/长按未松（有些板把 BOOT 做成拨杆或需要松手）；
2. BOOT 键有电容/短路，复位沿仍为低；
3. **USB-Serial-JTAG 的 DTR/RTS 自动电路把 GPIO0 拉低**（`--before default-reset`
   会拉 DTR/RTS 组合；本板原生 USB-JTAG 对此敏感）。

**解法（按优先级）**：
```bash
# ① 先确保手没按 BOOT 键（或把它拨回 NON-BOOT），再物理拔插 USB
# ② 用 EN 硬复位、且不碰 GPIO0：
python -m esptool -p COM7 --chip esp32s3 --before no-reset --after hard-reset run
#    注意：--before no-reset 很关键，避免 esptool 自己去拉 DTR/RTS
# ③ 烧写后**立即**读串口（<1s），捕捉第一个启动日志：
python serial_read.py COM7 10
# ④ 若 ②③ 仍旧 boot:0x0，则几乎可确定是 BOOT 键/GPIO0 硬件被拉低 ——
#    试：拔掉 USB → 按住 BOOT → 插 USB → 松 BOOT → 立刻 esptool --after hard-reset run
#    （即走一次正常的上电引导序列，而非软件复位）
```

**判定表**：
| `boot:` 值 | 含义 | 处理 |
|---|---|---|
| `0x0` (DOWNLOAD) | GPIO0 在复位沿为低 | 见上（BOOT 键/GPIO0 电路） |
| `0x8` (SPI_FAST_FLASH_BOOT) | 正常启动 | 固件问题看应用日志 |
| `0xb` (DOWNLOAD(USB/UART0)) | 同上，USB 下载模式 | 同上 |

> ⚠ **教训**：`Hash verified` 只证明**写入正确**，**不证明能启动**。
> 两者要分开验证——这正是 Write-Once-Verify 之外必须补的「**启动验证**」环节。

### 2.4 看流（M0 里程碑）

设备连上 WiFi 后，浏览器打开 `http://<设备IP>/api/v1/health` 应返回 JSON；
图传端点见 `platform_net.c` 的推流任务。**先看到画面，再进下一步。**

---

## 3. 第二步：接通 BLE HID

camera 通了之后再验 BLE（此时只剩一个变量）。

### 3.1 已补的关键前置（本轮完成）

**真机头号炸点已修复**：`platform_ble.c` 原先直接调 `esp_hidd_dev_init`，
但 **NimBLE 协议栈从未初始化** → 真机上必然失败（BLE 广播不出现）。

现已加入 `bt_stack_ensure_init()`，序列（对照 IDF 官方 `esp_hid_device` 示例）：
```
esp_bt_controller_mem_release(CLASSIC_BT)   // 只用 BLE，释放经典蓝牙内存
  → nimble_port_init()                       // 内部：controller_init + enable + esp_nimble_init
  → nimble_port_freertos_init(nimble_host_task)  // 启动 host task 跑 nimble_port_run()
```
> 注意：`esp_nimble_init()` **只初始化 host 栈**，不含 controller；
> 必须用 `nimble_port_init()` 才完整（它内部会调 `esp_nimble_init`）。

### 3.2 烧写后看日志

**期望**：
```
I (xxx) QSHOU_BLE: NimBLE stack ready (controller + host)
I (xxx) QSHOU_BLE: BLE HID ready (Digitizer, report_map=57 bytes)
```

### 3.3 在被控端（Y71t）配对

1. 安卓设置 → 蓝牙 → 搜索名为 **`Qshou`** 的设备（`main.c` 传入的名字）。
2. 配对后确认它被归类为**输入设备**（见 `android_prep.md` 第 2 节）。
3. 用 `http://<设备IP>/api/v1/tap` 发一条 tap，看屏幕是否有响应。

**排查**：
| 现象 | 原因 |
|---|---|
| 搜不到设备 | `nimble_port_init` 失败 / `CONFIG_BT_NIMBLE_HID_SERVICE` 未开 |
| 能配对但 tap 无效 | 未连接（`platform_ble_emit` 在未连接时静默丢弃返 0）；或报告描述符不被识别 |
| 配对即断开 | 安全/配对参数（SM）未配好，需按官方示例设 `ble_hs_cfg.sm_*` |

> ⚠ **VID/PID 是占位值**（`0x303A/0x0001`），联调前按规格 `input.ble_hid` 确认最终值，
> 避免与中心端已配对的其它 HID 设备冲突。见 `platform_ble.c` 顶部 TODO。

---

## 4. 第三步：WiFi 凭据（NVS 灌入，**不硬编码**）

固件从 NVS 读 `wifi_ssid` / `wifi_pass`；没有则跳过 STA（非致命）。

```bash
# 方式 A：跑一次固件后用串口/HTTP 写入（推荐，符合"不硬编码"原则）
# 方式 B：临时用 nvs_flash 工具或自写一次性的 NVS 写入程序
```

无凭据时日志为：
```
W (xxx) QSHOU_NET: no wifi_ssid in NVS; skipping STA (configure then reboot)
```
—— 这是**预期行为**，不是错误。

---

## 5. 到货后仍待办

| 项 | 说明 |
|---|---|
| 键盘 HID 描述符 | 需新增按键报告特征（Report ID 与 digitizer 区分）；见 `docs/ble_descriptor_review.md` |
| BLE/WiFi 共存调优 | 两者共享 2.4G 射频；`CONFIG_SW_COEXIST_ENABLE=y` 已开，实际吞吐需实测 |
| 屏幕可用区实测（B2）| 需在 Y71t 上实测后回填 SCR 常量（**禁止提前硬编码**）|
| 放电速率（B4）/ 指针读数（B5）| 按 `docs/android_prep.md` 执行 |
| 端到端 UC1~UC5 真机断言 | 替换 calibration/device，重跑 `python -m tests.assertions` 并重落证据 |

---

## 6. 快速命令速查

```bash
# --- 构建 ---
cd firmware
idf.py -DPLATFORM_KIND=host  build        # 零硬件接缝（PC 验证）
idf.py -DPLATFORM_KIND=esp32 build        # 真机接缝（需 esp32-camera 组件）

# --- 切目标芯片（首次）---
idf.py set-target esp32s3

# --- 烧写与监视 ---
idf.py -p COMx flash monitor

# --- 串口清理（monitor 卡住时）---
# Ctrl+] 退出；必要时 idf.py -p COMx monitor --no-reset

# --- 交叉预检（零硬件，改代码后随时跑）---
bash tools/precheck-seams.sh build

# --- 纯逻辑回归（PC 侧）---
python -m tests.assertions                 # UC1~UC5（用带 numpy 的解释器）
python tools/parse_hid_descriptor.py       # HID 描述符 10 项校验
```

---

## 7. 已预知的风险与已修复项

| 风险 | 状态 |
|---|---|
| host 接缝缺 `app_main` → 链接失败 | ✅ 已修（两个分支都提供） |
| `HTTPD_ERR_RECV_TIMEOUT` 宏在 v6.1 移除 | ✅ 已修（改 `HTTPD_SOCK_ERR_TIMEOUT`）|
| **NimBLE 协议栈未初始化**（真机头号炸点）| ✅ **本轮已修**（`bt_stack_ensure_init`）|
| 隐式函数声明骗过预检 | ✅ 已加固（`-Werror=implicit-function-declaration`）|
| 摄像头引脚与实际板型不符 | ⚠ **待现场核对**（第 2.1 节）|
| esp32-camera 组件从未真正编译 | ⚠ **待拉组件后验证**（第 2.2 节）|

---

## 8. 已实测通过的链路 + 当前卡点（2026-09-29 夜）

### 8.1 ✅ 已打通

| 环节 | 结果 |
|---|---|
| 芯片识别 | ESP32-S3 rev v0.2, PSRAM 8MB, Flash 16MB, MAC `44:1b:f6:ca:0a:0c` → **N16R8 确认** |
| esp32-camera 组件 | `idf.py add-dependency "espressif/esp32-camera^2.0.0"` ✅ |
| **esp32 接缝全量构建** | **1258/1258 目标全过，0 error**，含 `platform_camera.c`+`platform_ble.c`（真机首次真实编译） |
| 产物 | `qshou.bin` = **1,212,016 B**（0x127e70），分区占用 29% |
| console 配置 | `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` / `UART_DEFAULT` 未设 / `UART_NUM=-1` ✅ |
| **烧写** | `Wrote 1212016 bytes ... Hash of data verified` `Done` ✅ |
| 固件落盘校验 | `read-flash 0x0 0x20` → `e9 03 02 4f`（合法 bootloader image）✅ |

### 8.2 🔴 当前卡点：复位后固定 `boot:0x0 (DOWNLOAD)`

```
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x15 (USB_UART_CHIP_RESET),boot:0x0 (DOWNLOAD(USB/UART0))
waiting for download
```

- **不是**固件没写进去（已用 read-flash 验证 image 合法）；
- **不是**端口/线材问题（能烧、能读、能读 ROM 日志）；
- **是** 复位采样时 **GPIO0 = 低** → 被强制进 ROM 下载模式。

**GPIO0 被拉低的三种可能**：
1. 板上 **BOOT 键**卡住 / 被按住未松；
2. GPIO0 外部电路（按键去抖电容、或 USB-Serial-JTAG 的 DTR/RTS 自动复位网络）拉低；
3. 需要走**完整上电引导**（拔插 USB），软件复位路径不适用。

**判定表**：
| `boot:` | 含义 | 处理 |
|---|---|---|
| `0x0` | GPIO0 复位沿为低 → 下载模式 | 松 BOOT 键 / 拔插 USB / 查 GPIO0 电路 |
| `0x8` | 正常 SPI Flash 启动 | ✅ 正常，看应用日志 |
| `0xb` | USB 下载模式 | 同上 |

**建议排查顺序**：
```bash
# 1) 松手所有按键 → 物理拔插 USB（走上电引导，不用软复位）
# 2) 若板有 BOOT 键：按住 BOOT → 插 USB → 松 BOOT → 立刻读串口
# 3) 只读串口、绝不碰 DTR/RTS（本仓 serial_read.py 已设 dtr=False rts=False）
python serial_read.py COM7 20
# 4) 仍 boot:0x0 → 查原理图确认 GPIO0 是否被外部拉低
```

> ⚠ **重要认识**：`Hash verified` 只证明**写入正确**，**不证明能启动**。
> 「烧写验证」与「启动验证」是**两个独立环节**，必须分别确认。

### 8.2.1 ✅ 已解决：`boot:0x0` → `boot:0x9`（2026-09-30 00:1x）

**已确认根因**：**BOOT 键(GPIO0) 在复位瞬间被拉低**。

**实测对照**（同一端口 COM7、同一固件）：

| 时间 | `boot:` 值 | 结果 |
|---|---|---|
| 09-29 23:5x | `0x0 (DOWNLOAD(USB/UART0))` | ❌ `waiting for download`，不启动 |
| 09-30 00:1x | **`0x9 (SPI_FAST_FLASH_BOOT)`** | ✅ **正常启动，全功能跑通** |

→ 用户的硬件动作（松开 BOOT / 拔插 USB 走上电引导）**就是解药**，无需改代码。

**后续若再遇到**：直接照 §8.2 的排查顺序做——**核心是"松开 BOOT 键 + 物理拔插 USB"**，
而不是反复软复位（软复位会重采样 GPIO0，键若仍按着就还是进下载模式）。

### 8.2.2 🔴 第二个真机缺口（本轮发现并已修复）：BLE 广播从未启动

**这是比 NimBLE 未初始化更隐蔽的坑** —— 因为**日志看起来"成功"**：

```
I (737) QSHOU_BLE: NimBLE stack ready (controller + host)      ← 看起来 OK
I (739) QSHOU_BLE: BLE HID ready (Digitizer, report_map=57 bytes)  ← 看起来 OK
I (737) NimBLE: GAP procedure initiated: stop advertising.    ← ⚠ 只有 stop
```

**没有 `advertise`，中心端就永远搜不到设备。**

**根因**（已核实 IDF v6.1 源码）：
`components/esp_hid/src/nimble_hidd.c` **只注册 HID GATT 服务，不做任何广播**
（grep 该文件 **零** `ble_gap_adv_*` 调用）。而 IDF 官方示例
`examples/bluetooth/esp_hid_device/main/esp_hid_gap.c` 是在 `esp_hidd_dev_init()`
**之后显式调用** `esp_hid_ble_gap_adv_init()` + `esp_hid_ble_gap_adv_start()` 的。
本模块原先漏了这两步。

**修复**（`platform_ble.c`）：新增两个静态函数并在 `platform_esp32_ble_start()`
末尾调用：
- `ble_adv_set_fields(name)` → `ble_gap_adv_set_fields()`，广播字段含
  `flags(DISC_GEN|BREDR_UNSUP)` + `tx_pwr_lvl` + 完整设备名 + **HID 服务 UUID 0x1812**
- `ble_adv_start()` → `ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, …)`
  （conn_mode=UND / disc_mode=GEN / 间隔 30–50ms，照官方推荐）
- 附带 `ble_gap_event_cb`：**断开连接后自动重新广播**（否则配对一次后设备就"消失"了）

**同时修复的第二个子问题：GAP 设备名**
`esp_hid` 也不调用 `ble_svc_gap_device_name_set()` → NimBLE 用默认名 **`"nimble"`**，
中心端扫描列表里显示成 "nimble" 无法辨识。
→ 已在 `ble_adv_set_fields()` 内加 `ble_svc_gap_device_name_set(device_name)`。

**修复后日志（实测）**：
```
I (739) NimBLE: GAP procedure initiated: advertise;
I (740) NimBLE: disc_mode=2
I (740) NimBLE:  adv_channel_map=0 own_addr_type=0 adv_filter_policy=0 adv_itvl_min=48 adv_itvl_max=80
I (741) QSHOU_BLE: advertising started (HID 0x1812, name="Qshou")   ← ✅
```

完整启动日志留痕：`docs/evidence/boot_log_20260930.txt`

### 8.3 本仓辅助脚本（`qshou/`）

| 脚本 | 用途 | 用法 |
|---|---|---|
| `idf_run.py` | 绕过 Git Bash 的 `MSYSTEM` 注入，跑 idf.py | `python idf_run.py -DPLATFORM_KIND=esp32 build` |
| | | `python idf_run.py -p COM7 flash` |
| `esptool_run.py` | 同上，直调 esptool | `python esptool_run.py -p COM7 --chip esp32s3 --after hard-reset run` |
| `serial_read.py` | 被动只读串口 N 秒（不碰 DTR/RTS） | `python serial_read.py COM7 10` |

> **为什么需要 `idf_run.py`**：沙箱的 Bash 工具（Git Bash）会向每个子进程**重新注入**
> `MSYSTEM=MINGW64`，而 `tools/idf.py:1483` 见到它**直接 abort**（连 `build/` 都不建）。
> `unset` / `env -u` 全被 shell 层还原，**只能在 Python 内部 `os.environ.pop()`** 才有效。
