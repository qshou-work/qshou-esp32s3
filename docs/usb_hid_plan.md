# Qshou —— USB HID 键鼠功能方案

> 状态：**规划稿**（未动代码）。所有硬件约束均已对当前源码树与 sdkconfig 核实。
> 相关文档：`board_b_ov5640_bringup.md`（板级实测）、`partition_pin_plan.md`（引脚/分区）、
> `ble_descriptor_review.md`（现有 BLE HID 描述符评审）。

---

## 0. 一句话结论

**值得做，而且是本项目从「蓝牙遥控」升级为「真 KVM」的关键一步。**
当前 HID 只走 BLE，被控机必须**已启动 OS 且蓝牙栈可用**才能接受输入——这排除了 KVM
最核心的场景（进 BIOS、装系统、OS 崩溃后救援）。USB HID 是**带外（out-of-band）**通道，
不依赖被控机任何软件栈。

技术上有三条路（§4），推荐 **TinyUSB（ESP32-S3 原生 USB-OTG）**，因为现有代码的
HID 报文层已经是**纯逻辑且正好是 USB Boot Protocol 格式**，复用量极高。

> ✅ **状态（2026-10-17）：已落地并实测通过。** 板子枚举为 USB 复合设备
> （Boot Keyboard + 鼠标 + Consumer），键盘/鼠标/多媒体键均送达 Windows。
> 仅剩 **BIOS/UEFI 实机验证**（Phase 2）——那是本功能价值成立的最终判据。
> 实施记录见 §11.5 / §12.6 / §12.7。

---

## 1. 价值：为什么 USB HID 与 BLE HID 不是同一件事

| 维度 | 当前 BLE HID | USB HID |
|---|---|---|
| **BIOS / UEFI** | ❌ 完全不可用 | ✅ **可用**（Boot Protocol） |
| **装机 / 进恢复模式 / 改启动项** | ❌ | ✅ **可用** |
| **OS 崩溃后救援**（蓝屏 / kernel panic） | ❌ 依赖 OS 蓝牙栈 | ✅ 可用 |
| **被控机需要蓝牙硬件** | ✅ 必须有 | ❌ 不需要 |
| **配对 / 授权** | 需要（OS 内确认） | 免配对，插上即用 |
| **驱动** | 依赖 OS 蓝牙栈 | 免驱（标准 HID） |
| **延迟 / 稳定性** | 受 BLE 连接间隔与干扰影响 | 有线 FS，稳定 |
| **可扩展复合设备** | 受 HOGP 限制 | ✅ CDC 串口 / MSC U盘 |

**本质区别**：BLE 是**带内**控制（in-band，依赖被控机 OS 的蓝牙协议栈活着），
USB HID 是**带外**控制（out-of-band，被控机只把它当一根普通键盘）。
KVM 的定义性能力就是带外控制——"被控机已经没法自己救自己时，我还能操作它"。

### 三个具体的高价值场景

1. **远程装机**：被控机没有 OS（或全新硬盘），远程进 BIOS 改启动顺序 → 装系统。
   当前 BLE 方案在此**完全无能为力**。
2. **救援已经起不来的机器**：OS 崩了、网卡驱动坏了、连不上网——只要主板 USB 供电，
   我们的板子就能操作 BIOS/恢复环境。这恰恰是"远程"二字最有价值的地方。
3. **无蓝牙设备**：台式机主板、服务器、工控机、嵌入式板卡大多没有蓝牙。
   BLE 方案在这些目标上是死路，USB 是唯一通道。

### 附带价值：USB 口是个可扩展的复合设备插槽

一旦有了 USB Device 能力，同一个口还能挂：

- **CDC-ACM 串口** → 给被控机一个串口，可做**被控机 → 板子**的反向数据通道
  （日志回传、被控机侧跑脚本上报状态）。这是当前纯单向（板子→被控机）架构的补全。
- **MSC（U盘）** → 挂载 `storage` 分区（1.9MB，当前**未挂载**）暴露给被控机，
  远程装机时直接投送驱动/ISO/脚本。与装机场景天然契合。
  ⚠️ 风险：被控机可能格式化该分区；且 1.9MB 容量有限。

---

## 2. 可行性硬约束（动手前必须确认）

### 2.1 ✅ 引脚空闲——已核实

ESP32-S3 的 USB-OTG **固定使用 GPIO19（D-）/ GPIO20（D+），不可重映射**。
当前源码树占用情况（全部核实）：

| 用途 | 引脚 |
|---|---|
| 摄像头 DVP | 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18 |
| 状态灯 | 48（WS2812）、2（IO2 单色） |
| 控制台 UART0 | 43（TX）、44（RX） |

→ **GPIO19 / GPIO20 完全空闲** ✅，硬件上可行。

### 2.2 ⚠️ 控制台冲突——必须改配置（最关键的一条）

ESP32-S3 上 **USB-OTG 与 USB-Serial-JTAG 共享同一组 USB PHY（GPIO19/20）**，
二者**互斥**。当前 `sdkconfig` 里：

```
CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y   # sdkconfig:2499  ← 必须关掉
CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED=y     # sdkconfig:2501  ← 随之关闭
```

改为 `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`。**好消息**：主控制台已是
`CONFIG_ESP_CONSOLE_UART_DEFAULT=y`（UART0 / GPIO43-44 / CH343），日志与控制台不受影响，
且这个改动**正是 B 板 bring-up 时为了解决"日志不可见"已经做过的方向**，风险已知。

### 2.3 ✅ 不会变砖——烧写安全网仍在

ESP32-S3 的 ROM USB 下载模式走的是 **USB-Serial-JTAG**，**不是 USB-OTG**。
因此启用 OTG 后**原生 USB 口不能再用于烧写**——但 **CH343 UART 口烧写完全不受影响**
（A/B 两板当前都是走 CH343 UART 烧写的）。这是本方案的安全底线：
即使 USB 配置写错导致枚举异常，仍可用 UART 口重新烧录恢复。

> **纪律**：启用 USB-OTG 后，**烧写/救砖永远走 UART（CH343）口**，原生 USB 口只做 HID Device。

### 2.4 ⚠️ 只有一个 OTG 控制器——Host 与 Device 互斥

`CONFIG_SOC_USB_OTG_PERIPH_NUM=1`（sdkconfig:402）。ESP32-S3 **只有一个 USB-OTG 控制器**，
同一时刻只能做 Host **或** Device，**不能同时**。

→ 这意味着「把真实键鼠插在板子上，板子再转发给被控机」这种**本地直通/中间人**玩法
**做不到**（需要 Host + Device 同时工作）。若将来要做，必须外挂 USB Hub 芯片。
本方案的定位是**纯 Device**：板子冒充键鼠插进被控机。

### 2.5 ⚠️ 供电——需硬件确认（唯一的硬件不确定项）

板子原生 USB 口插进被控机后会**从被控机取电（5V）**。风险：

1. **峰值电流可能超 500mA**：WiFi 发射峰值（~350mA）+ 相机（~100mA+）+ LED + S3 自身，
   可能触发被控机 USB 口过流保护 → 板子掉电重启（表现为反复枚举/断连）。
2. **反向供电**：多数开发板的 CH343 口与原生 USB 口的 VBUS 是**同一 5V 轨**（无 ORing 二极管）。
   若两个口同时插（CH343 接电脑 + 原生 USB 接被控机），会把 5V 反向灌进被控机 USB 口。
   多数主板有保护，但不规范。

→ **建议**：板子**独立供电**（CH343 口接充电器/电脑），原生 USB 口只走 D+/D- 数据。
**需查开发板原理图**确认两个 USB 口的 VBUS 是否直连、有无二极管隔离。
这是 Phase 0 必须实测的一项。

> ✅ **实测结论（2026-10-17，§11.5）**：两根线**同插一台电脑**，单次枚举、零掉线、
> 系统日志无过流事件 —— 上述第 1 条风险（峰值超 500mA 触发过流）**未发生**，
> 第 2 条（倒灌）在「同源供电」前提下不成立。故 **MVP 阶段无需额外独立供电**。
> 若将来插不同主机，仍需按上法确认 VBUS 是否隔离。

### 2.6 现有 HID 架构——复用量极高（利好）

已核实的关键事实：

- `ble_hid/hid_report.c|h` 与 `ble_hid/hid_descriptor.c|h` 是**纯逻辑、零硬件依赖**
  （文件头明确声明"host 与 esp32 共用同一份描述符字节"）。
- **键盘帧格式 = 8 字节 `[modifier][reserved][k0..k5]`，鼠标 = 3 字节 `[buttons][dx][dy]`**
  ——这**正好就是 USB HID Boot Protocol 的线格式**。BIOS 兼容几乎是白送。
- 复合设备已有三个 Report ID：`1=鼠标 / 2=键盘 / 3=Consumer`。
- 接缝函数 **`platform_ble_emit_report(rid, buf, len)` 已经带 Report ID 参数**
  （`routes.c:721/734/744` 分别以 rid=1/2/3 调用）——天然适合扩展到多后端。

---

## 3. 实现方式（推荐方案：TinyUSB）

### 3.1 技术选型

**TinyUSB**（ESP-IDF 官方组件 `espressif/esp_tinyusb`，通过 component manager 引入）。
做 **HID Composite Device**：键盘 + 相对鼠标 + Consumer Control，三个 Report ID 与现有
BLE 侧一一对应。

### 3.2 核心设计决策

#### 决策 1：复用现有报文与描述符层（零改动）

`hid_report.c` / `hid_descriptor.c` 直接复用。上层（`ble_hid.c` 的手势展开、按键映射、
串行队列）也**完全复用**——它们只依赖"发一个 report"这个动作，不关心传输方式。

#### 决策 2：处理 Report ID 前缀差异（本方案最核心的适配点）

| | 线格式 | 原因 |
|---|---|---|
| **BLE / HOGP** | **不带** Report ID | 靠 GATT 特征关联区分（`hid_descriptor.c` 头注释） |
| **USB HID** | **必须带** Report ID 前缀 | descriptor 含多个 Report ID 时，每个 report 首字节即 Report ID |

适配映射：

```
鼠标    [buttons][dx][dy]          3B  →  [0x01][buttons][dx][dy]          4B
键盘    [mod][rsv][k0..k5]         8B  →  [0x02][mod][rsv][k0..k5]         9B
Consumer[usage_lo][usage_hi]       2B  →  [0x03][usage_lo][usage_hi]       3B
```

好在 `platform_*_emit_report(rid, buf, len)` 的 `rid` 参数已经存在，USB 实现只需在
`tud_hid_n_report(instance, rid, buf, len)` 里原样透传。

#### 决策 3：接口布局为 BIOS 优化（关键）

BIOS/UEFI 通常**只枚举第一个 HID 接口，且往往只认键盘**。因此：

- **键盘接口排第一位**，声明 `bInterfaceSubClass = 0x01`（Boot Interface）、
  `bInterfaceProtocol = 0x01`（Keyboard）。
- 鼠标接口第二位，`bInterfaceProtocol = 0x02`（Boot Mouse）。
- TinyUSB 侧用 `CONFIG_TINYUSB_HID_COUNT=2` 创建两个 HID 接口。
- Consumer Control 不是 boot 设备（BIOS 用不到，OS 下正常），可放在第二接口内。

#### 决策 4：鼠标先用**相对模式**，绝对模式留作第二阶段

现有 BLE 侧刻意选了**相对鼠标**（`hid_descriptor.h` 记载：安卓下绝对坐标形态全部失败）。
但**USB 场景的 host 是 PC（Windows/Linux/macOS），绝对鼠标完全可行**。

- **MVP 用相对鼠标**：直接复用现有 descriptor 与标定链路，零风险。
- **Phase 3 加绝对模式**：KVM 的精确点击（"点这个按钮"）用相对鼠标很难做准，
  绝对坐标（或 Digitizer）在 USB 下是刚需。此时 host 是 PC 不是安卓，风险低。
- 两种模式可做成**运行时可选**（API 参数），互不干扰。

#### 决策 5：传输路由层——USB 优先，回落 BLE，**严禁双发**

```
HTTP /api/v1/hid  →  hid_router  →  ├─ USB 已枚举？ → usb_hid（优先）
                                     └─ 否则           → ble_hid（回落）
```

**互斥是硬约束**：若被控机同时连着 BLE 和 USB，双发会导致输入重复（鼠标飞速乱窜）。
路由层必须二选一。可用 TinyUSB 的 `tud_mounted()` 判定 USB 状态，或用挂载/卸载回调
（`tud_mount_cb` / `tud_umount_cb`）维护一个标志位。

> 附带好处：这是**双通道冗余**——USB 线掉了自动回落 BLE，BLE 断了插上 USB 即可。

#### 决策 6：VID / PID 与设备字符串

- VID 用 Espressif 官方 `0x303A`（避免与其他厂商冲突），PID 自定义（如 `0x4001`）。
- 字符串描述符：Manufacturer `Qshou`、Product `Qshou HID`、
  **Serial 用 MAC 派生**（两板可区分，与 mDNS 主机名同思路）。

---

## 4. 替代方案对比（值得认真考虑）

| | **A. TinyUSB（推荐）** | **B. 外挂串口转 HID 芯片（CH9329）** | **C. USB Host 方向** |
|---|---|---|---|
| 原理 | S3 原生 USB-OTG 做 Device | 板子 UART 发指令给 CH9329，由它冒充 HID | S3 做 Host 接真实键鼠 |
| 额外硬件 | 无 | CH9329 模块（¥3~8）+ 接线 | 无 |
| 固件侵入 | 中（新增后端 + 路由 + 配置） | **极小**（只发串口字节） | 大 |
| 占用引脚 | GPIO19/20（固定） | 任意一对 UART 脚 | GPIO19/20 |
| 与控制台冲突 | **有**（须关 USB-JTAG 次控制台） | **无** | 有 |
| BIOS 兼容 | ✅ 可控（自定 descriptor） | ⚠️ 需实测（描述符固定） | N/A |
| 复合设备（CDC/MSC） | ✅ 支持 | ❌ 不支持 | N/A |
| 描述符可控性 | ✅ 完全可控 | ❌ 固定 | N/A |
| 稳定性 | 依赖固件质量 | 专用芯片，稳 | — |
| 适用定位 | **KVM 平台，长期可控** | **快速验证 / 成本敏感** | 不符合需求 |

**判断**：

- 本项目是"KVM **Lite** 平台"，追求的是可控性与可扩展性（复合设备、绝对鼠标、
  反向通道都在这条路上）。**推荐方案 A**。
- 但**方案 B 值得作为 Plan B**：如果 Phase 0 发现 GPIO19/20 走线/供电有问题，
  或者只想用最小代价快速拿到 USB 键鼠能力，CH9329 是极低风险的退路——
  固件侧只是"把 report 字节从 BLE 换成 UART"，改动量约几十行。
- 方案 C 不符合需求（我们要的是"板子冒充键鼠"，不是"板子接键鼠"），
  且与 Device 模式互斥（§2.4），列在此仅为澄清边界。

---

## 5. 代码改造方案

### 5.1 目录结构（新增）

```
firmware/
  ble_hid/              # 现有，保留不动
    hid_report.c|h      #   纯逻辑 —— 复用
    hid_descriptor.c|h  #   纯逻辑 —— 复用
    ble_hid.c|h         #   BLE 传输后端（现有）
  usb_hid/              # ★ 新增
    usb_hid.c|h         #   TinyUSB 初始化 + 发射（tud_hid_n_report）
    usb_descriptors.c   #   USB device/config/interface/HID 描述符（USB 专用）
  hid_router/           # ★ 新增
    hid_router.c|h      #   后端路由：USB 优先 / 回落 BLE / 显式选择
  platform/esp32/
    platform_usb.c      # ★ 新增：platform_usb_* 接缝实现
```

### 5.2 改造点清单（精确到文件）

| 文件 | 改动 |
|---|---|
| `main/routes.c` | HID 调用改走 router：3 处 `platform_ble_emit_report`(721/734/744)、`ble_hid_play_gesture`(272/310/374)、`ble_hid_send_key`(398) |
| `platform/platform.h` | 新增 `platform_usb_*` 接缝声明（emit / mounted / init） |
| `platform/esp32/platform_ble.c` | 保留 `platform_ble_emit`；新增 `platform_usb_emit` |
| `main/CMakeLists.txt` | `FW_LOGIC_SRC` 加 `usb_hid/*.c`、`hid_router/*.c`；`PLATFORM_SRC` 加 `platform_usb.c`；`FW_REQUIRES` 加 `esp_tinyusb`、`usb` |
| `main/idf_component.yml` | 加依赖 `espressif/esp_tinyusb` |
| `sdkconfig.defaults` | 关 USB-JTAG 次控制台；开 `CONFIG_TINYUSB_HID_COUNT`、`CONFIG_TINYUSB_ENABLED` |
| **`tests/test_firmware_contract.py`** | ⚠️ **契约测试会红**：它用正则校验 `platform_ble_emit_report` / `platform_ble_emit` / `ble_hid_play_gesture` 等符号与调用方式（第 297~391 行）。改名/新增后端后**必须同步更新**，否则 `make test` 失败 |

### 5.3 关键代码骨架（示意）

USB 描述符里的接口声明（BIOS 兼容的关键）：

```c
/* 接口 0：Boot Keyboard —— 必须排在第一位，BIOS 才认 */
{
    .bInterfaceClass    = TUSB_CLASS_HID,
    .bInterfaceSubClass = HID_SUBCLASS_BOOT,       /* 0x01 */
    .bInterfaceProtocol = HID_ITF_PROTOCOL_KEYBOARD, /* 0x01 */
},
/* 接口 1：Boot Mouse（Consumer 并入此接口的第二 Report ID） */
{
    .bInterfaceClass    = TUSB_CLASS_HID,
    .bInterfaceSubClass = HID_SUBCLASS_BOOT,       /* 0x01 */
    .bInterfaceProtocol = HID_ITF_PROTOCOL_MOUSE,  /* 0x02 */
},
```

发射（Report ID 透传）：

```c
int platform_usb_emit(uint8_t rid, const uint8_t *buf, size_t len)
{
    if (!tud_mounted()) return -1;              /* 未枚举：丢弃，由 router 回落 BLE */
    /* rid 即 Report ID，TinyUSB 自动加前缀字节 */
    return tud_hid_n_report(rid_to_instance(rid), rid, buf, len) ? 0 : -1;
}
```

路由（互斥）：

```c
int hid_router_emit(uint8_t rid, const uint8_t *buf, size_t len)
{
    if (s_backend == HID_BACKEND_AUTO)
        return platform_usb_emit(rid, buf, len) == 0
             ? 0 : platform_ble_emit_report(rid, buf, len);  /* 二选一，绝不双发 */
    ...
}
```

> 时序上无需担心拥塞：USB FS 中断端点由主机按 ~1ms（键盘）/8ms（鼠标）轮询，
> 而现有手势回放的 `step_ms` 是 60~120ms，**远大于轮询间隔**，USB 通道绰绰有余。

---

## 6. 分阶段计划

### Phase 0 —— 可行性验证（最小风险，先做）

**目标**：证明 GPIO19/20 能跑 TinyUSB，且不破坏现有功能。
**只加 POC 自检代码，不改既有业务逻辑**（原计划「完全不碰产品代码」，实际把
最小 demo 直接并入了固件——好处是免维护两套构建，代价是 POC 自检代码会留在
固件里直到 Phase 1 替换成正式后端）。

- [x] ~~查开发板原理图~~ → **未做**（无原理图）；替代判据：只插一口量另一口 VBUS
      ⚠️ 仍待确认（§2.5）
- [x] 最小 TinyUSB HID 键盘 demo，已装进固件并启动（`TinyUSB Driver installed`）
      ⚠️ **插 PC 枚举未验证**（等用户插线）
- [x] 关掉 `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG`，确认：
      UART 日志正常 ✅、CH343 烧写仍可用 ✅（安全网验证通过）
- [x] **实测供电**：两根线同插一台电脑，单次枚举、零 DETACHED、系统日志无过流 ✅
- [x] 产出：枚举证据 + 供电结论 + 是否需外部供电的结论 ✅（§11.5）

**决策门**：✅ **已通过** —— 供电过关，**不需要转方案 B（CH9329）**。

> 状态（2026-10-17 16:53）：**Phase 0 全部验收项通过**。设备被 Windows 识别为
> `Qshou HID` 并由 `kbdhid` 接管，自检按键 `'a'` 经 Raw Input 抓包确认送达主机，
> 来源设备路径为 `VID_303A&PID_4004`。详见 §11.5。

### Phase 1 —— MVP：USB HID 键鼠 ✅ **已打通（2026-10-17）**

- [x] 新增 `usb_hid/`（线帧转换）、`hid_router/`（后端路由）
- [x] 三 Report ID 的**线帧转换**打通（鼠标/键盘/Consumer 的 USB 线上字节）
- [x] 键盘接口 Boot Protocol + 排第一位
- [x] `hid_router` 实现 USB 优先 / 回落 BLE / 显式选择（AUTO/BLE_ONLY/USB_ONLY）
- [x] **同步更新 `test_firmware_contract.py`**（§19 + §20 + §21 共 58 条）
- [x] **USB 后端接入 TinyUSB**：复合设备（接口0 boot kbd / 接口1 鼠标+Consumer）
- [x] 验证：键盘 / 鼠标 / Consumer 均送达 Windows（Raw Input 抓包），BLE 与既有功能零回归

> 状态（2026-10-17）：**功能闭环**。踩中并修复两个「host 静默丢弃」型故障
> （双重 Report ID、描述符报告长度不符），详见 §12.6 / §12.7。

### Phase 2 —— BIOS/UEFI 实测（核心价值验证）

- [ ] 进 BIOS/UEFI，验证键盘可操作（Boot Protocol 生效）
- [ ] 若失败：检查接口顺序 / `bInterfaceSubClass` / `bInterfaceProtocol` / 是否被
      Consumer 接口干扰
- [ ] 在 Windows / Linux / macOS 三平台各验证一次枚举与输入
- [ ] 产出：BIOS 可用性结论（**这是本功能成立与否的判据**）

### Phase 3 —— 增强（按需，非 MVP）

- [ ] 绝对鼠标模式（USB 下精确点击；host 是 PC，风险低）
- [ ] CDC-ACM 反向通道（被控机 → 板子的数据/日志通道）
- [ ] MSC U盘（挂 `storage` 分区，远程装机投送文件）
- [ ] API/UI 增加后端选择与状态展示

---

## 7. 风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | 失去 USB-Serial-JTAG 控制台 | 中 | 主控制台已是 UART0；烧写走 CH343；**不会变砖**（§2.3） |
| 2 | 供电不足 / 反向供电 | **高** | ✅ **已实测排除**（§11.5）：同源双插单次枚举无过流；异源供电仍需查 VBUS 隔离 |
| 3 | BLE 与 USB 双发重复输入 | 高 | router 强制互斥，USB 优先（§3.2 决策 5） |
| 4 | 契约测试失败 | 中 | 同步更新 `test_firmware_contract.py`（易漏点） |
| 5 | BIOS 不认（枚举顺序/协议） | **高** | 键盘接口排第一 + boot subclass/protocol；Phase 2 实测调 |
| 6 | TinyUSB 与 WiFi/相机资源竞争 | 低 | ✅ 实测共存正常；8MB PSRAM 充裕，USB FS 中断 1ms 一次负载轻 |
| 7 | GPIO19/20 板级走线不可用（未知） | **高** | ✅ **已验证可用**：枚举成功、按键送达（§11.5）—— 方案 B 不再需要 |
| 8 | 被控机格式化 MSC 分区 | 低 | 仅 Phase 3；可只读挂载或不做 |

---

## 8. 与现有 BLE HID 的关系

**不替换，是互补的双通道。**

- BLE 保留：无线、无需接线、手机端友好（安卓已验证可用）。
- USB 新增：带外、BIOS、低延迟、免配对、可扩展。
- 两者由 `hid_router` 统一调度，**USB 优先，断线回落 BLE**。

最终形态：一根 USB 线插上被控机 = 完整 KVM（含 BIOS 级控制）；
不插线时 = 蓝牙遥控。两板共用同一份固件，运行时自适应。

---

## 9. 待你决策的问题

1. **目标场景优先级**：主要是"远程装机/BIOS 操作"（→ 强烈推荐本方案），
   还是"日常桌面遥控增强"（→ 优先级可降低）？
2. **是否接受"失去原生 USB 口烧写能力"**（改为只用 CH343 烧写）？这是本方案的前提。
3. **硬件条件**：板子能否独立供电（CH343 口接充电器），让原生 USB 口只走数据？
4. **是否先做 Phase 0 验证**（推荐），还是直接上 Phase 1？
5. **是否需要 Phase 3 的复合设备**（CDC/MSC），还是只要键鼠？

---

## 10. 参考事实来源（本次核实）

- 引脚占用：`platform/esp32/platform_camera.c:38-86`（相机）、`platform.h:226`（LED 48/2）
- 控制台配置：`sdkconfig:2491/2499/2501/2503/2505`
- OTG 控制器数量：`sdkconfig:402`（`CONFIG_SOC_USB_OTG_PERIPH_NUM=1`）
- HID 纯逻辑层：`ble_hid/hid_report.h`、`ble_hid/hid_descriptor.h`（文件头声明零硬件依赖）
- 键盘/鼠标线格式：`hid_report.h:26/42-49`
- 接缝带 Report ID：`main/routes.c:721/734/744`（`platform_ble_emit_report(rid, buf, len)`）
- 契约测试：`tests/test_firmware_contract.py:297-391`
- 分区（storage 未挂载）：`partitions.csv:22`、`partition_pin_plan.md`
- 组件结构：`main/CMakeLists.txt`（`FW_LOGIC_SRC` / `PLATFORM_SRC` / `FW_REQUIRES`）

---

## 11. Phase 0 实施记录（2026-10-17，已烧录 A 板）

### 11.1 用户决策

| 问题 | 决策 |
|---|---|
| 场景优先级 | **桌面遥控增强**（非远程装机/BIOS）→ 不紧急，但 Phase 0 验证照做 |
| 是否接受只用 CH343 烧写 | ✅ 接受 |
| 能否独立供电 | 不明确（问「两个 Type-C 靠得近能否同插两根线」） |
| 阶段 | ✅ 执行 Phase 0 |

**关于两个 Type-C 同插**：物理上取决于间距；电气上**两根线插同一台电脑安全**
（同 5V 源、同地电位，无倒灌，还能分担电流），**插不同主机有 VBUS 倒灌风险**
（开发板通常把两口 VBUS 并联，无 ORing 二极管）。判断法：只插一口，量另一口 VBUS。
Phase 0 需要「CH343 看日志 + 原生 USB 做 HID」→ 同插一台电脑正好安全。

### 11.2 改动清单

| 文件 | 改动 |
|---|---|
| `platform/esp32/platform_usb.c` | **新增**：TinyUSB HID Boot Keyboard（单接口、无 Report ID、`subclass=Boot`/`protocol=Keyboard`）；插线即触发、可反复验证的自检（发 `'a'`） |
| `platform/host/platform_usb.c` | **新增**：空实现，保证零硬件可构建 |
| `platform/platform.h` | 加 `platform_usb_poc_init()` 声明 |
| `main/main.c` | 推流启动后调用（失败不致命） |
| `main/CMakeLists.txt` | 两个 PLATFORM_KIND 分支各加 `platform_usb.c` |
| `main/idf_component.yml` | 加 `espressif/esp_tinyusb: "^2.0.1~1"` |
| `sdkconfig` + `sdkconfig.defaults` | 关 `ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` → `SECONDARY_NONE=y`；加 `TINYUSB_HID_COUNT=1`；**修正 defaults 里"UART0 未引出"的过时假设** |

> 备份：`.workbuddy-ai/backup_poc0/`（项目**无 git**，回退靠它）。

### 11.3 实测结果（A 板，`boot_a3.txt`）

| 检查项 | 结果 |
|---|---|
| TinyUSB 安装 | ✅ `TinyUSB Driver installed on port 0` |
| HID 描述符 | ✅ `subclass=Boot protocol=Keyboard`，序列号 `441bf6ca0a0c`（MAC 派生） |
| 自检任务 | ✅ `POC: 等待主机枚举 —— 把原生 USB 口插到电脑即可触发自检` |
| UART 日志（安全网） | ✅ 完整可见，无 panic —— **关掉 USB-JTAG 控制台未造成任何损失** |
| mDNS / 相机 / WiFi | ✅ `qshou-0a0c.local` / `OV2640 ready (PID=0x0026)` / `got ip:192.168.2.160` |
| 固件体积 | 1,438,176 → **1,464,768 B**（TinyUSB 栈 +26KB） |

**结论**：GPIO19/20 可用、控制台冲突已解除、既有功能零回归 —— **Phase 0 的技术前提全部成立**。

### 11.4 踩坑记录（两个，都已修）

1. **`CONFIG_TINYUSB_HID_COUNT` 默认值是 0** —— 不显式设 `=1` 则编译出的固件
   **没有 HID 功能**（第一次构建后检查 sdkconfig 才发现，等于白跑）。这是官方
   `tusb_hid` 示例 sdkconfig.defaults 里的唯一必需项。
2. **`tud_mount_cb` / `tud_umount_cb` 重复定义** —— `esp_tinyusb/tinyusb.c` 已
   **无条件定义**这两个符号（内部用于 MSC 挂载与电源管理，再转调 `s_ctx.event_cb`）。
   自己定义会链接失败。**正确做法**：用 `tinyusb_config_t.event_cb` 注册
   （事件 `TINYUSB_EVENT_ATTACHED` / `_DETACHED`）。
   `tud_suspend_cb` / `tud_resume_cb` 则由 `CONFIG_TINYUSB_SUSPEND_CALLBACK` /
   `_RESUME_CALLBACK` 门控（默认 n）。

### 11.5 插线实测结果 —— ✅ **全部通过（2026-10-17 16:53，A 板）**

两根 Type-C **同时插同一台电脑**（CH343 看日志 + 原生 USB 做 HID），一次性验证：

| 验收项 | 结果 | 证据 |
|---|---|---|
| USB 枚举 | ✅ | `USB\VID_303A&PID_4004\441bf6ca0a0c`，`BusReportedDeviceDesc = "Qshou HID"`（产品名生效），枚举耗时 **308 ms**（3318→3601 ms 日志） |
| 驱动绑定 | ✅ | 父节点 `Service=HidUsb` → 子节点 `Service=kbdhid` —— 被当作**键盘**接管，不是「未知设备」 |
| Boot Protocol | ✅（软件侧） | `TUD_HID_DESCRIPTOR` 第 3 参数 `HID_ITF_PROTOCOL_KEYBOARD` 已置 subclass=1/protocol=1；BIOS 级实测留 Phase 2 |
| **按键送达主机** | ✅ | Raw Input 捕获 `vkey=0x41 make=0x1E`（`'a'` 按下 + 抬起），设备路径 `\\?\HID#VID_303A&PID_4004#...` —— **来源确认是 ESP32，不是物理键盘** |
| 供电稳定性 | ✅ | 插线至今**单次枚举、零 DETACHED**；系统日志无 USB 过流/枚举失败事件 |
| 既有功能共存 | ✅ | BLE 广播正常、`OV2640 ready`、`got ip:192.168.2.160`、HTTP `200`、MJPEG 流正常 |
| UART 安全网 | ✅ | 主控制台日志完整，关掉 USB-JTAG 次控制台零损失 |

**关于「两个 Type-C 能否同时插」的最终结论**：
两根线插**同一台电脑**安全（同 5V 源、同地电位、无倒灌），实测无过流、无反复枚举。
插**不同主机**仍有 VBUS 倒灌风险（开发板两口 VBUS 通常并联无 ORing），除非确认原理图。
→ 现场使用约定：**CH343 与原生 USB 接同一台机器，或原生 USB 单独供电**。

#### 验证方法：Raw Input 监听（可复用工具）

`.workbuddy-ai/tools/rawinput_listen.py` —— 用 `RIDEV_INPUTSINK` 注册窗口，
**不依赖窗口焦点**、**按设备名过滤**，直接证明「报告从哪个设备到达系统」。
两个必须踩的坑（已写进脚本注释）：

1. **未 `ShowWindow()` 的窗口收不到任何 `WM_INPUT`**；`HWND_MESSAGE`（message-only）
   窗口同样收不到。正解：0×0 尺寸窗口 + `SW_SHOWNA`（对窗口管理器可见、对用户不可见、不抢焦点）。
2. `RAWINPUTHEADER.dwType` 里 **键盘是 1（`RIM_TYPEKEYBOARD`），不是 0**（0 是鼠标）。
   按 0 过滤会静默丢掉全部按键 —— 表现为「监听器跑着但永远 0 事件」。

> 同类陷阱：ctypes 必须显式声明 `DefWindowProcW` / `GetRawInputData` 的 `argtypes`，
> 否则 64 位下 `LPARAM` 按 `c_int` 转换会抛 `int too long to convert`。

- [x] 原生 USB 口插 PC → 设备管理器出现 **HID 键盘**（序列号 `441bf6ca0a0c`）
- [x] 插线后约 3 秒自动发 `'a'`（全链路打通，Raw Input 已抓包证实）
- [x] **供电实测**：WiFi 连网 + 相机初始化时未反复枚举
- [ ] BIOS 兼容实测（Phase 2）

---

## 12. Phase 1 逻辑层实施记录（2026-10-17，未接 TinyUSB）

> 用户决策：**先写逻辑层，不接 TinyUSB** —— 理由是 Phase 0 的供电门还没关
> （插线实测未做），若供电不过关要转 CH9329 方案，接 TinyUSB 的适配代码会重写。
> 但**报文转换与路由决策与后端无关**，两种方案下都能直接用，所以先行无浪费。

### 12.1 核心设计：USB 复合设备必须拆两个接口

这是本节最重要的一条，**决定了能不能进 BIOS**：

| BLE Report ID | 业务数据 | USB 接口 | USB Report ID | 线上字节 |
|---|---|---|---|---|
| 1 鼠标 | 3B `[buttons][dX][dY]` | 1（pointer） | 1 | **4B**（带前缀） |
| 2 键盘 | 8B `[mod][rsv][k0..k5]` | **0（boot kbd）** | **0 无前缀** | **8B 原样** |
| 3 Consumer | 2B `[usage_lo][usage_hi]` | 1（pointer） | **2**（接口内重编号） | **3B**（带前缀） |

三条不可动的理由：

1. **键盘必须单独占接口 0**：USB HID Boot Protocol 规定 boot 键盘报文是
   **固定 8 字节、无 Report ID 前缀**。若把键盘和鼠标塞进同一接口并加前缀，
   它就不再是 boot 格式 —— BIOS/UEFI 只认 boot 设备，于是「进 BIOS 按键无效」。
2. **键盘接口必须排第一位**：部分 BIOS 只探测第一个 HID 接口。
3. **Report ID 在 USB 里是 per-interface 编号**，所以 Consumer 在接口 1 内
   重新编号为 2（不是 BLE 侧的 3）。

> ⚠️ 这个错误**在普通桌面环境测不出来**（Windows/Linux/macOS 都接受带 Report ID
> 的键盘），只在 BIOS/UEFI 里暴露。因此用契约测试锁死（§12.3 第 19b 组）。

### 12.2 改动清单

| 文件 | 改动 |
|---|---|
| `usb_hid/usb_hid_frame.h/.c` | **新增**：BLE 线帧 → USB 线帧转换。用映射表表达 §12.1 的契约（唯一可读来源），长度不符即拒发 |
| `hid_router/hid_router.h/.c` | **新增**：后端抽象（`ready`/`send` 回调注册）+ 策略（AUTO/BLE_ONLY/USB_ONLY）+ 统计 |
| `main/routes.c` | 鼠标/键盘/Consumer 三个端点改走 `hid_router_emit()`（原 `platform_ble_emit_report`） |
| `platform/platform.h` | **新增** `platform_hid_set_emit()` 出口注入（平台层只持有函数指针，不反向依赖逻辑层） |
| `platform/esp32/platform_ble.c` | **手势帧（833 行）+ 按键帧（776/778 行）改走 `hid_emit_out()`** —— 见下方「改造面的意外发现」 |
| `platform/host/platform_ble.c` | `platform_hid_set_emit` 桩（保零硬件构建可链接） |
| `ble_hid/ble_hid.c` | `ble_hid_emit_report` 改走 `hid_router_emit`（⚠️ 该函数**当前只被 C 测试调用**，产品路径不经过它） |
| `main/main.c` | 装配：注册 BLE 后端（`ble_backend_ready` 适配器）+ `platform_hid_set_emit(hid_router_emit)` |
| `main/CMakeLists.txt` | `FW_LOGIC_SRC` 加两个新源文件 |
| `firmware/tests/test_firmware_contract.py` | 新增 §19（USB 线帧契约，13 条）+ §20（路由契约，23 条）；**并修正 1 条既有检查**（见下） |

> 备份：`.workbuddy-ai/backup_p1/`（项目**无 git**）。

#### ⚠️ 改造面的意外发现：手势/按键其实在平台层直发

原计划以为「把 `routes.c` 和 `ble_hid.c` 改成走 router」就够了。实际 grep 后发现：

- `ble_hid_emit_report()` 在产品代码里**没有调用者**（只被 `tests/test_firmware_c.c` 用）。
- 真正的产品发射路径在 **`platform/esp32/platform_ble.c` 的 `gesture_task`** 里：
  - 776/778 行 —— 按键队列的 press/release
  - 833 行 —— 手势轨迹逐帧回放

也就是说：**如果只改 `routes.c`，HTTP 端点会走 USB，而 box/circle/dir/tap 手势和 `/api/v1/key` 按键仍只走 BLE** —— 这种失效是**静默**的（功能看起来完全正常，只是永远不走 USB），是最难发现的一类 bug。

**为什么不能直接把 `gesture_task` 搬到逻辑层**：它依赖 `xQueueCreate`/`xTaskCreate`/`vTaskDelayUntil`/`portMAX_DELAY`，搬走会破坏「逻辑层零 RTOS 依赖 → host 可构建」这条接缝。

**采用的解法：出口注入。** 平台层内部统一走 `hid_emit_out()`：注入了 hook 就调 hook（→ `hid_router_emit`），未注入则退回直发 BLE（行为不变）。装配层在 `main.c` 里注入。这样平台层只依赖一个函数指针类型，**不反向依赖 `hid_router`**。

> 递归检查：hook = `hid_router_emit` → router 的 BLE 后端 = `platform_ble_emit_report`（不经过 hook）→ 无递归。

**既有契约检查的修正**：原有一条断言 `"platform_ble_emit_report" in gesture_task 源码`
（意图：按键必须用带 Report ID 的通用发射，不能用鼠标专用包装）。
改成断言 `hid_emit_out(job.key_rid` —— **保留原意图**（带 Report ID）并额外要求经路由层。

### 12.3 两条铁律（写进代码注释，并用契约测试守住）

**铁律 1 —— 严禁双发。**
同一帧若同时经 BLE 与 USB 送达同一台被控机，会收到两次输入：光标跳两倍、
字符重复两次。所以 `hid_router_emit()` 的语义是「选定一个后端，**只调它一次**」，
**失败也不跨后端重试** —— 因为「USB send 返回错误」不等于「host 没收到」
（EP 忙/超时时数据可能已入 FIFO），重试即构成双发风险。失败就丢这一帧并计数。

结构性保证：`hid_router.c` 全文只有**一个** `.send(` 调用点。契约测试断言
`count(".send(") == 1` —— 谁加了「失败换个后端再试」，测试立刻红。

**铁律 2 —— USB 优先、BLE 兜底。**
USB 一旦挂载就优先走它（带外能力更强、延迟更低、不受蓝牙配对状态影响）；
未挂载时回落 BLE。`HID_ROUTE_USB_ONLY` 下**不回落**（宁可丢帧，也不悄悄换链路）。

### 12.4 契约测试新增（33 条，全绿）

| 组 | 守什么 |
|---|---|
| 19a–19c | 三个 Report ID 的 USB 线帧逐字节正确（含 **键盘 8B→8B 无前缀**、Consumer 重编号） |
| 19d | 长度与描述符声明不符必须拒发（宁可丢帧也不发错位报文） |
| 19e | 源码级：键盘映射必须是 `接口0 + USB_HID_RID_NONE`（防有人「统一格式」加前缀） |
| 20a | `hid_router.c` 只允许一个发送调用点（严禁双发） |
| 20b | 业务层（`routes.c`/`ble_hid.c`）不得绕过路由直发 BLE |
| 20c–20d | `main.c` 必须注册后端、`CMakeLists.txt` 必须含新源文件 |
| 20e | 路由策略语义镜像（AUTO/USB_ONLY 的回落行为） |

验证：`run_tests.py` → **python 214/214 + firmware contract rc=0**。

### 12.5 下一步（Phase 0 门已关闭）

- [x] Phase 0 插线实测（枚举 + 供电）—— ✅ **通过**（§11.5），**不转 CH9329**
- [x] USB 后端接入 —— ✅ **已实施**，见 §12.6
- [x] 复合描述符：接口 0 = Boot Keyboard、接口 1 = 鼠标(RID1) + Consumer(RID2)
- [x] 移除 POC 自检任务（避免上电自动发 `'a'`）
- [ ] HTTP 端点暴露 `hid_router` 状态（当前后端/策略/统计），便于现场诊断
      —— ✅ **已做**：`GET /api/v1/hid` 查询，`POST /api/v1/hid {"mode":"auto|ble|usb"}` 切换策略

### 12.6 USB 后端接入实施（2026-10-17）

把 `hid_router` 的 USB 后端真正接到 TinyUSB 上。**逻辑层（`usb_hid_frame` /
`hid_router`）一行未改** —— 这是 §12 那层抽象的直接回报。

| 文件 | 改动 |
|---|---|
| `platform/esp32/platform_usb.c` | **重写**：单接口 POC → **双接口复合设备**。接口 0 = Boot Keyboard（`HID_ITF_PROTOCOL_KEYBOARD`，描述符**不传** `HID_REPORT_ID`）；接口 1 = `TUD_HID_REPORT_DESC_MOUSE(HID_REPORT_ID(1))` + `TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(2))`。新增 `platform_usb_ready/emit`；**删除 POC 自检任务** |
| `platform/host/platform_usb.c` | 三个接缝的桩；`ready` **恒返回 0**（见下方「易错点」） |
| `platform/platform.h` | `platform_usb_poc_init()` → `platform_usb_init/ready/emit` 三件套 |
| `main/main.c` | `platform_usb_init()` 成功后才 `hid_router_register(HID_BACKEND_USB, ...)` |
| `sdkconfig` + `sdkconfig.defaults` | `CONFIG_TINYUSB_HID_COUNT` **1 → 2**（两个 HID 实例） |
| `tests/test_firmware_contract.py` | 新增 §21（23 条） |

> 备份：`.workbuddy-ai/backup_p1b/`。

#### 三个实现上的关键决定

1. **必须用 `tud_hid_n_report(instance, rid, ...)`，不能用 `tud_hid_report()`。**
   `tud_hid_report()` 在 TinyUSB 里等价于 `instance=0` —— 用它发鼠标帧，数据会被
   塞进**键盘接口**，现象是「鼠标不动、键盘乱按」，而且 Windows 不报错。
   契约测试 §21a 用正则 `(?<!_)tud_hid_report\(` 锁死。

2. **发送必须加互斥锁。** `emit()` 会被 HTTP 处理任务与 `gesture_task` 并发调用，
   而 TinyUSB 的 HID FIFO 写入不是线程安全的 —— 并发写会交错出畸形报文。
   拿不到锁（10ms）就**丢帧**，不阻塞业务任务、也不排队堆积（鼠标位移丢帧可接受）。

3. **编译期闸门 `#if CFG_TUD_HID != 2 → #error`。** HID_COUNT=1 时第二个
   `TUD_HID_DESCRIPTOR` 会指向不存在的实例，配置描述符总长也算错，运行时表现为
   「能枚举但接口 1 无响应」—— 极难定位。Phase 0 已经踩过一次同类坑
   （HID_COUNT 默认 0 → 固件根本没有 HID 功能），这次用 `#error` 从源头拦住。

#### 易错点：host 桩的 `ready` 不能谎报可用

host 桩若返回 1（「无条件可用」），`HID_ROUTE_AUTO` 会**选中 USB** 并把所有帧丢在
桩里 —— BLE 明明可用却全部失效，而且日志上看不出任何异常。所以桩必须返回 0，
让 router 自然回落 BLE。

#### ⚠️ 最容易犯的错：双重 Report ID（接后端时踩中，已修）

`usb_hid_pack()` 产出的是「USB 线上完整字节」，**含 Report ID 前缀**。
而 TinyUSB 的 `tud_hid_n_report()` 在 `report_id != 0` 时**自己也会写前缀**：

```c
// tinyusb/src/class/hid/hid_device.c
if (report_id) {
    p_epbuf->epin[0] = report_id;                       // ← 协议栈自己写
    tu_memcpy_s(p_epbuf->epin + 1, ..., report, len);
    len++;
}
```

若把 `frame` 原样传进去，线上字节变成 `[01][01][btn][dx][dy]` ——
**双重 Report ID，整帧错位 1 字节**。

危险之处在于**它不会报错**：设备照常枚举，Windows 按描述符硬解析，
现象只是「鼠标乱跳、键盘乱按」，很容易被误判成描述符写错或数据算错。

正确做法（`platform_usb_emit`）：

```c
const uint8_t *payload = frame;
uint16_t plen = n;
if (meta.rid != USB_HID_RID_NONE) {   /* 有前缀才需要剥 */
    payload = frame + 1;              /* 跳过我们写的前缀，让协议栈补 */
    plen    = n - 1;
}
tud_hid_n_report(meta.itf, meta.rid, payload, plen);
```

- 键盘 `rid=0` → 不剥（本来就没前缀），协议栈也不写 → 8B 原样 ✅
- 鼠标 `rid=1` → 剥掉后 3B，协议栈补 1B → 线上 4B ✅
- Consumer `rid=2` → 剥掉后 2B，协议栈补 1B → 线上 3B ✅

契约测试 §21e2 用两条断言锁死：不得把裸 `frame` 传给 `tud_hid_n_report`，
且必须存在 `frame + 1` 的剥前缀逻辑。

> 方法论：`usb_hid_pack()` 的产出仍然保持「线上完整字节」语义 —— 它是契约测试
> 与文档的单一事实来源；后端只借用它算出的 `(接口, RID, 长度)`，数据部分剥前缀
> 后交给协议栈。这样「谁负责写前缀」只有一处，不会两边各写一次。

#### ⚠️ 第二个静默故障：描述符声明的报告长度与线帧不一致

`TUD_HID_REPORT_DESC_MOUSE()` 宏生成的报告是 **5 字节**：

```
buttons(1) + X(1) + Y(1) + 垂直滚轮(1) + 横向滚轮(1)
```

而 BLE 侧鼠标帧只有 3 字节（`[buttons][dX][dY]`），线上 4 字节。
host 按描述符期望 **6 字节**、实际收到 4 字节 → **静默丢弃全部鼠标报告**。
现象：设备管理器一切正常（"HID-compliant mouse" 状态 OK）、固件 `tud_hid_n_report()`
返回 true、HTTP 回 `{"ok":true}`，**就是光标不动，零错误日志**。

→ 手写 3 字节描述符（3 buttons + 5 bit padding + X + Y），与线帧严格对齐。
契约测试 §21d2 锁死：不得使用 `TUD_HID_REPORT_DESC_MOUSE`、不得出现滚轮 usage。

**通用教训**：HID 描述符声明的报告长度是**契约**，设备端多发少发都会被 host
静默丢弃。接新集合时，先用 `HidP_GetCaps` 问 Windows 期望多少字节，再对照固件。

#### 验证方法上的坑：Consumer 的报告不走键盘通道

验 Consumer 时用监听键盘的 Raw Input 窗口**永远收不到**——Consumer Control
集合（Usage Page `0x0C`）的报告以 `RIM_TYPEHID` 原始报告上报，**不会**被转成
`RIM_TYPEKEYBOARD` 事件。必须额外注册 `usUsagePage=0x0C, usUsage=0x01` 并解析
`RAWHID`，否则会误判成「Consumer 不工作」。
（`rawinput_listen.py` 已同时注册键盘/鼠标/Consumer 三类。）

### 12.7 端到端实测结果（2026-10-17，A 板，Windows 11）

**枚举**（`hid_caps.py` 向 Windows 查询各集合的期望报告长度）：

| 集合 | usagePage/usage | Windows 期望 IN | 固件实际发 | 结果 |
|---|---|---|---|---|
| 键盘（MI_00） | 0x0001 / 0x0006 | 9（1 RID + 8） | 8（无 RID，boot 格式） | ✅ 一致 |
| 鼠标（MI_01&Col01） | 0x0001 / 0x0002 | **4** | 4 | ✅ 一致 |
| Consumer（MI_01&Col02） | 0x000C / 0x0001 | **3** | 3 | ✅ 一致 |

设备树：`USB Composite Device` → `MI_00` = HID Keyboard Device（kbdhid）、
`MI_01` → `COL01` = HID-compliant mouse、`COL02` = 符合 HID 标准的用户控制设备。

**数据链路**（HTTP 端点 → 路由 → TinyUSB → Windows Raw Input 抓包）：

| 功能 | 指令 | 抓到的证据 |
|---|---|---|
| 键盘 | `POST /api/v1/hid/keyboard {"keys":[5]}` | `vkey=0x42 make=0x30`（b），路径 `MI_00` |
| 鼠标 | `POST /api/v1/hid/mouse {"dx":25,"dy":15}` | `MOUSE dx=25 dy=15`，路径 `MI_01&Col01` |
| Consumer | `POST /api/v1/hid/consumer {"usage_lo":233}` | 原始报告 `02 e9 00`（RID2 + 音量+），路径 `MI_01&Col02` |

**共存**：BLE 广播正常、OV2640 正常、WiFi `192.168.2.160`、HTTP 200、UART 日志无损。

> 未测：BIOS/UEFI 实机（Phase 2）、拔线后回落 BLE 的现场验证（逻辑层已由契约测试覆盖）。

### 12.8 现场诊断端点（2026-10-17 追加）

`GET /api/v1/hid` —— 报文实际走哪条链路在上层**看不见**（HTTP 调的永远是同一个
`hid_router_emit`），所以单独开一个观测口。「按了没反应」时的排查顺序：

1. **`dropped` 是否在涨** —— 涨 = 两个后端都不可用或 send 失败
2. **`usb.mounted`** —— USB 未枚举时 AUTO 必然回落 BLE；此时若 `ble.connected` 也是 0，
   报文就是发出去没人收
3. **`fallback` 是否在涨** —— 涨 = USB 挂着但单个 send 失败在回落

`POST /api/v1/hid {"mode":"auto|ble|usb"}` —— 强制某条链路。
⚠️ `usb_only` 下 USB 未挂载时**不回落**（铁律 2：宁可丢帧也不悄悄换链路），
否则现场现象与配置不符更难排查。诊断完记得调回 `auto`。


### 12.9 端点双写坑：ROUTES[] 与 routes_handle 必须同时改（2026-10-17，实测）

`/api/v1/hid` 加完、构建烧录都成功，`GET /api/v1/hid` 却返回
404 `Nothing matches the given URI`。最后发现是**同一个端点漏写了两处**：

| # | 位置 | 作用 | 漏了它的症状 |
|---|---|---|---|
| ① | `platform_net.c` 的 `ROUTES[]` | 向 httpd 注册 URI | httpd 层直接 404，`routes_handle` 里的代码**永远执行不到** |
| ② | `routes.c` 的 `routes_handle()` | 按 method 分派到 handler | 注册了也白发，或走错 handler |

两个附加细节让这次排查多花了一倍时间：

1. **响应体会骗人**：httpd 的 404 是 `Nothing matches the given URI`，
   而我们自己的 routes 404 是另一套文案。不对比源码的话，现场看响应体
   很容易误判成「网络不通」而不是「路由没注册」。
2. **嵌套错方法块 = 死代码**：GET 的分发行被写进了 `if (method == M_POST)` 块里，
   条件恒假、编译零警告 —— 这类错误靠读代码不容易发现。

契约测试 §22 现在双向锁死（含变异检验）：
- `ROUTES[]` ↔ `routes_handle` 分发表做**双向**集合比对，任何一端漏写即 FAIL
- 按花括号配对取出 M_GET / M_POST 块体，断言 GET 分派不在 POST 块内、反之亦然
- 提取字符串字面量前必须掩掉 C 注释（变异检验时注释掉 ROUTES 条目，
  不掩注释的版本会把它当"已注册"，测试照样绿 —— 等于没测）

### 12.10 active/last 语义坑：诊断口必须回答「下一帧会走哪」（2026-10-17）

ROUTES 修好后 `GET /api/v1/hid` 通了，但返回值漏了一个更隐蔽的问题：
USB 已挂载（`mounted=1`）、统计全 0，`active` 却显示 `"ble"`。

根因：`hid_router_active_backend()` 返回的是 **`s_last`** ——
上一次成功发射用的后端，**过去时**。一条都没发过时它停在初值 `ble`。
拿它当「当前生效后端」，会看到与事实相反的答案，排查时极易误判成
「路由选错了」。

`select_backend()` 本来就是「此刻会选哪个」的纯函数（且是 emit 内部用的
同一个），但它此前是 `static`。

修法：新增 **`hid_router_peek_backend(int *is_fallback)`** —— 纯查询、不发帧，
直接 `return select_backend(is_fallback)`。JSON 里：

| 字段 | 含义 |
|---|---|
| `active` | **下一帧**会走后端（peek） |
| `last` | 上一帧实际走的（原 active_backend） |
| `would_fallback` | USB 已注册但当前不可用、将回落 BLE |

铁律要求：**窥探与实走必须是同一个函数**，否则诊断看到的后端会和实际用的
不一致 —— 比没有诊断更糟。

实测（usb_only 策略下连发 2 键盘 + 1 鼠标）：
`sent_usb: 3 / sent_ble: 0 / fallback: 0 / dropped: 0`，
且 `last` 从 `ble` 翻成 `usb` —— peek 与实走行为一致得到实证。
Raw Input 侧抓到 `vkey=0x54`（usage 23 = t），设备路径 `VID_303A&PID_4008&MI_00`。

契约锁定：§20g（peek 必须 return select_backend、emit 内部仍走 select_backend、
端点字段不得混用）；C 侧 `tests/test_firmware_c.c::test_hid_router_peek`
（本机无原生 gcc，仅做了 xtensa 交叉编译 `-fsyntax-only` 语法检查，
**尚未链接运行** —— 装好 gcc 后需跑 `make run` 补上）。

### 12.11 收尾确认（2026-10-17）

| 项 | 结果 |
|---|---|
| `/api/v1/hid` GET 状态 | ✅ policy/active/last/would_fallback/两端就绪态/统计 |
| POST 切策略（含非法值 400） | ✅ auto/ble/usb |
| USB_ONLY 实发 3 帧 | ✅ 全走 USB，零丢零回落 |
| Raw Input 键盘链路回归 | ✅ `vkey=0x54`，路径 `MI_00` |
| 相机帧尺寸（NVS 冷生效） | ✅ 抓到 `640x480` JPEG，SOF0 实测 |
| 策略复位 | ✅ 已调回 `auto` |
| 全量测试 | ✅ Python 214/214，固件契约 266 条 fails=0 |

### 12.12 补 host 零硬件构建：暴露链接期重复定义（2026-10-17）

`build_host` 的旧日志停在 `[739/1307]`、无报错也无 `.bin`，看着像「构建被打断」。
补跑后真相是**链接期失败**，而且是两件事叠在一起：

**① 真实代码 bug：`platform_wifi_rssi` 重复定义**
- `platform/host/platform_net.c:44` 与 `platform/host/platform_health.c:24` 各定义一份
- 规范位置是 `platform_health.c`（esp32 接缝也在那里，返回 -55 dBm 有意义的值）；
  `platform_net.c` 那份返回 `0`，而 0 dBm 不是合法 RSSI，是复制粘贴残留
- **这类错误的迷惑性**：它是链接期错误，前面 739/1307 编译步骤全是绿的、
  日志停在半路，极容易往「是不是被打断了」的方向查，实际是代码冲突

**② 我自己的操作失误（已复原，记下来防止复发）**
- 跑 host 构建时漏了 `-B build_host`，`idf.py` 把 `-B` 相对路径解析到了**工作区根目录**，
  于是重配了主 `firmware/build/` 目录的接缝
- 更关键的坑：**`idf.py` 会把 `PLATFORM_KIND` 缓存进 build 目录的 CMakeCache**。
  之后我跑「复原构建」时没带 `-DPLATFORM_KIND`，它**复用缓存里的 host**，
  又把 host 桩固件（188,768 B）覆盖进了 `firmware/build/qshou.bin`
  （esp32 产物 1,466,336 B 被冲掉）。这正是 `build-firmware.ps1` 注释里警告过的坑 ——
  「默认值必须是 esp32」只对 **ps1** 成立，直接调 `idf.py` 时没有默认值可言
- 板子本身未受影响（flash 与 build 是两回事，板里跑的固件与源码一致且已实测）

**正确命令（切接缝必须显式带，无默认值）**

```bash
# host 零硬件构建（产物会落在工作区根的 build_host/）
run_idf.py -C qshou/firmware -DPLATFORM_KIND=host -B build_host build
# 复原 esp32 接缝
run_idf.py -C qshou/firmware -DPLATFORM_KIND=esp32 build
```

**契约锁定 §23**：扫 host 接缝全部 `.c`，每个 `platform_*` 函数只能定义一次
（变异检验：把重复定义塞回去 → 立刻 FAIL）。另 host 构建现已**链接成功**
（`Project build complete`，bin 0x2e160 B = 189,984 B，比 esp32 版小得多，
符合 host 桩不含 WiFi/BLE/相机/USB 协议栈的预期）。

### 12.13 未完成项清单（2026-10-17 收尾时点）

按优先级：

| # | 项 | 状态 / 阻塞 |
|---|---|---|
| 1 | **Phase 2：BIOS/UEFI 实机验证** | **未做**。本功能成立与否的判据 —— Windows 通过 ≠ BIOS 通过（BIOS 只认 Boot Protocol）。进 BIOS 看键盘能否操作 |
| 2 | **拔线回落 BLE 现场验证** | 未做。逻辑层已由契约覆盖，但「USB 卸载后 AUTO 是否真回落、BLE 是否还连着」需现场拔一次线 |
| 3 | ~~**B 板固件同步**~~ | ⛔ **已作废（2026-10-17）**：B 板已退货，后续不再适配。唯一目标硬件 = A 板（OV2640）。B 板 bringup 文档 `docs/board_b_ov5640_bringup.md` 已标记作废 |
| 4 | ~~C 运行时测试 `test_hid_router_peek`~~ | ✅ **已完成**（见 §12.14）：ziglang 装上后首次真跑 102 passed / 0 failed，还抓着两个固件真 bug（`select_backend` 空指针写、GESTURE_TAP 重复 down 帧），均已修复并随新固件烧录 |
| 5 | §365 三平台枚举验证 | 只验了 Windows；Linux / macOS 未验 |
| 6 | 后续扩展（§370-373） | 绝对鼠标模式、CDC-ACM 反向通道、MSC U盘、API/UI 后端选择展示 |

### 12.14 C 单元测试首次真跑：一套从未链接成功的测试抓着两个真 bug（2026-10-17）

**背景**：`firmware/tests/` 的 C 测试写了很久，但开发机没有原生 gcc 也没有
make，`Makefile` 注定跑不了 —— 也就是说**这套测试从未真正执行过**。

本轮用 `pip install ziglang`（ziglang 自带完整 C 编译器，装在受管 venv，
不污染系统）把它跑起来，编译期就连续踩坑，每一个都是「从未跑过」的直接后果：

| # | 现象 | 根因 | 性质 |
|---|---|---|---|
| ① | `M_PI` undeclared | `M_PI` 不是 C11 标准宏；gcc 默认 gnu11 环境悄悄暴露它，严格的 clang/zig cc 下没有 | 可移植性缺陷（已加 `#ifndef M_PI` 兜底） |
| ② | `platform_led_*` undefined | `Makefile` SRC 漏了 `platform_led.c` | 构建入口与源码不同步 |
| ③ | `platform_gpio_*` undefined | 同上，漏 `platform_gpio.c` / `platform_usb.c` | 同上（③④一起） |
| ④ | `host_ble_get_nth_rid` 未声明 | 实现早就有，`host_test_stubs.h` 漏声明 | 头文件与实现不同步 |
| ⑤ | emit 测试假红 | 测试写于路由器改造前（当时 emit 不经路由直接落历史缓冲）；USB 接入后 `ble_hid_emit_report` 已走 `hid_router_emit`，不注册后端就丢帧 | 测试拓扑过期 |
| ⑥ | SIGILL | 见下方「真 bug 1」 | **固件真 bug** |
| ⑦ | tap 锚定断言失败 | 见下方「真 bug 2」 | **固件真 bug** |

**真 bug 1：`select_backend` 空指针写**（`hid_router.c`）
头文件写明 `is_fallback 出参（可为 NULL）`，实现却无条件 `*is_fallback = 0`。
`hid_router_peek_backend(NULL)` 直接崩溃。实机 `routes.c` 一直传合法指针，
所以**这个 bug 随固件发过但从未触发** —— 靠 C 测试首跑才暴露。
已改为判空后写。

**真 bug 2：GESTURE_TAP 重复 down 帧**（`ble_hid.c`）
函数顶部通用「第 0 帧按下(0,0)」与 TAP 分支自带的前导 down(0,0) 重复，
实际帧序变成 5 帧（down(0,0) ×2、down(+1,0)、up(-1,0)、up(0,0)），
而代码注释写明的设计是 4 帧（第 1 帧就该是 +1px 锚定）。
安卓 host 上重复零位移 down 无害，所以一直没被发现。
已删冗余帧。

**顺带发现的自身问题**：host 接缝缺 `ble_hid_play_gesture` /
`ble_hid_send_key` 的实现（声明在 ble_hid.h，实现只在 esp32 接缝）——
host 构建一直链不上，说明这两个执行器从没在 host 侧验证过。
已按 esp32 语义同步实现（走同一条 `hid_router_emit` 路由，不绕过）。

**测试加固**：
- 契约 §24：`tests/Makefile` 与 `tests/run_c_tests.sh` 的源文件清单必须覆盖
  `platform/host/` 全部 `.c`，且两个入口清单必须一致（③④类坑锁死）
- C 测试补：host 执行器播放（帧数/RID/参数错误）、`peek_backend(NULL)`
  回归点、`host_ble_get_nth` 环形缓冲的 16 帧窗口语义
- 变异检验：撤 NULL 判空 → 崩；塞回重复 down 帧 → tap 断言红。均抓住。
- 终态：**C 单元测试 102 passed / 0 failed**（首次）；Python 214/214；
  固件契约 355 条 fails=0

**工具备忘**：`tests/run_c_tests.sh` 是本机 C 测试入口（zig lang）。
下载大 wheel 走腾讯镜像（1.5MB/s）比 pythonhosted（~25KB/s）快 60 倍；
pip 中途被杀会在 site-packages 留半成品 dist-info，重装前先清。

### 12.15 Raw Input 取证必须在「输入管道干净」的环境做（2026-10-17 实测教训）

新固件烧录后重跑 Raw Input 回归，监听器抓到 1670 个事件，**全部**来自
`VID_062A&PID_5918`（虚拟鼠标），板子（303A:PID_4008）事件为零，
`vkey=0x54` / `dx=50 dy=-30` / Consumer `02 e9 00` 一个都没有。

进程取证定位真凶：**`SunloginClient`（向日葵）+ `mstsc`（「veighna - 远程桌面
连接」）同时在跑** —— 远程被控端的操作者鼠标键盘正以高频灌入本机输入管道
（16 秒内 431 键 + 1240 鼠标移动），把板子事件彻底淹没。

板子本身无恙：宿主机上 `VID_303A&PID_4008` 三个 HID 集合（MI_00 键盘 /
MI_01&Col01 鼠标 / MI_01&Col02 Consumer）全部 Status OK，未遭抢占；
固件侧 `mounted=1`、帧计数正常累加。

**方法论结论（以后做物理层验证前先照做）**：
1. 先查远程客户端进程：`Get-Process sunlogin*,mstsc,vmware*`——有就请操作方
   退出，否则 Raw Input 捕获到的绝大多数事件与你的设备无关
2. 监听直接带设备过滤，别用肉眼从事件流里捞：
   `python .workbuddy-ai/tools/rawinput_listen.py 16 out.txt 303A`
3. `GetCursorPos` 增量在远程操作者活跃时**不可作证据**（对方同时在动鼠标；
   本次见到的"1.54x 等比位移"方向与所发报告一致纯属巧合，不能采信）
4. `GetAsyncKeyState` 只反映调用线程自己的消息队列，后台进程不泵消息时
   验不了按键——要系统级键态需换方法，不要用它下结论

**本次为何不需要重做物理层证明**：烧录改动只有 `select_backend` 判空 +
GESTURE_TAP 去重，**HID 发射路径（描述符 / 键鼠 Consumer 打包 /
platform_usb_emit）零改动**，报文与 19:30 已验证构建逐字节一致——
那次的 Raw Input 验证（`vkey=0x41/0x42/0x43`、MOUSE dx/dy 精确匹配、
Consumer `02 e9 00`、路径 `MI_00/MI_01&Col01/MI_01&Col02`）对传输链路依旧成立。

### 12.16 Raw Input 回归通过记录（新固件，2026-10-17 23:10）

向日葵/mstsc 全部退出、输入管道安静后，用带设备过滤（`303A`）的逐类隔离
监听复测，三类报文全部命中且数值精确：

| 类型 | 捕获内容 | 路径 | 判定 |
|---|---|---|---|
| 键盘 | 32×WM_KEYDOWN（vkey=0x54/t, make=0x14, flags=0x00）+ 1×WM_KEYUP（flags=0x01, msg=0x101） | `MI_00` | ✅ 完整按键周期 |
| 鼠标 | `MOUSE dx=60 dy=20`（与请求逐一相等） | `MI_01&Col01` | ✅ |
| Consumer | 原始报告 `02 e9 00` | `MI_01&Col02` | ✅ |

工具：`.workbuddy-ai/tools/isolate_hid.py`（每类一个全新监听窗口，避免
typematic 洪流与其它报文互相干扰）。

两个顺带发现：
1. Windows 对**按住**的 HID 键会产生 typematic 重复 make（~23/s）——
   一次 press+release 的正确捕获形态是「1+N 个 make + 1 个 break」，
   不是 2 个事件。曾据此误判"没有抬起事件"。
2. Windows Python 直接起的子进程**不经过** Git Bash 的 `/tmp` 路径转换，
   传 `/tmp/x` 会被解析成 `C:/tmp/x` 导致监听器末尾写文件失败（症状：
   跑完"没有输出"）。跨进程传路径一律用 `tempfile.gettempdir()`。

至此 USB HID 物理层链路在新固件上全部回归通过。
