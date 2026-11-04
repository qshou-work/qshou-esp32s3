# firmware/ — ESP32 固件（Qshou 终端控制设备）

ESP32-S3-N16R8（A 板：OV5640 摄像头）的 C/C++ 固件。纯逻辑模块可在 PC 上用
`gcc` 编译/测试，硬件相关调用全部收拢到 `platform/` 接缝层；
`platform/esp32/*.c` 为真实实现，已在真机端到端验证（BLE HID 发射、OV5640 拍帧、
HTTP/MJPEG 服务、空闲 standby 降温）。

## 目录结构

```
firmware/
├── platform/            # 硬件抽象层（接缝）—— 纯逻辑与硬件的唯一边界
│   ├── platform.h       # 接缝 API 声明（固件逻辑只允许调这些）
│   ├── host/            # 本机桩（无板可跑）：BLE 写缓冲、camera 合成 JPEG、health 假值
│   └── esp32/           # 真实实现（已就绪）：esp_hid+NimBLE / esp32-camera / 遥测
├── ble_hid/             # 5 字节 report：打包/解包 + Digitizer 描述符（纯逻辑）+ 发射（走接缝）
├── camera/              # 取帧（走接缝）+ MJPEG multipart 边界帧构造（纯逻辑）
├── main/                # HTTP API 合约解析（纯逻辑）+ 路由分发 + 入口
├── tests/               # host 单测（需 gcc）+ Python 契约镜像测试（可立即跑）
├── tools/               # precheck-seams.sh（esp32 接缝交叉预检）+ stub/（桩头）
├── CMakeLists.txt       # ESP-IDF 构建骨架（到货后用）
└── README.md
```

## 三处接缝（对应规格 workspace_layout.seams）

| 接缝 | 固件侧落点 | host 桩行为 | 到货后替换 |
|------|-----------|------------|-----------|
| `seam_video_source`   | `camera_next_jpeg()` → `platform_camera_capture` | 返回带 SOI/EOI 的合成 JPEG | OV2640/OV5640 + esp32-camera（**esp32 实现已就绪**）|
| `seam_command_dispatch` | `ble_hid_emit_report()` → `platform_ble_emit` | report 写入环形历史缓冲供测试读取 | esp_hid + NimBLE（**esp32 实现已就绪**）|
| `seam_h_matrix`       | 固件不做 H 求解（控制台侧换算，见 v1.9.1） | — | —（仅数据，逻辑不变） |

> **关键边界（v1.9.1 决策）**：SCR→HID 在 Win10 控制端完成，固件只接收已换算的
> 5 字节 report 并透传发射，是“哑中继”。这满足 `R20` 密文约束（broker/固件不解析
> payload 语义），也避免固件重复实现标定逻辑。

> **接缝实现路线（pre_arrival 已完成）**：BLE HID 走 **ESP-IDF 官方 `esp_hid`
> 组件**（`CONFIG_BT_NIMBLE_ENABLED` 时内置 HID Device，无需自写 GATT、无需
> Arduino 生态第三方库 → 规避许可证不确定性）；Camera 走 **esp32-camera**
> 托管组件。详见 `docs/verification_report.md` §7。

## 5 字节 report 布局（全链路唯一真源）

```
[state][X_MSB][X_LSB][Y_MSB][Y_LSB]
state : 0x03=按下(Tip Switch+In Range)，0x00=抬起
X/Y   : 16-bit 逻辑坐标，范围 [0, 10000]（Digitizer Logical Max）
```
与 `shared/coords.py` 的 `hid_to_report` / `report_to_hid` 逐字节一致（已由
`tests/test_firmware_contract.py` 验证）。

## 构建与验证

工程已是**标准 ESP-IDF 结构**（顶层 `CMakeLists.txt` + `main/CMakeLists.txt`
的 `idf_component_register`），可被 `idf.py` 直接构建。

### 1) 零硬件构建验证（host 接缝，装好 ESP-IDF 即可跑通）✅ 已验证
```powershell
# 一键（自动注入 ESP-IDF 环境变量）
cd firmware
.\build-firmware.ps1              # 默认 host 接缝
.\build-firmware.ps1 -Clean       # 先 fullclean
# 或手动
idf.py -DPLATFORM_KIND=host build
```
host 接缝的 `platform/host/platform_net.c` 是空操作桩，因此**不需要任何真实硬件**
即可完整走通「配置 → 编译 → 链接 → 产出 .bin」，用于提前发现构建系统问题。

**实测结果**：1226 个编译单元全过，产出
- `build/qshou.bin` **191,296 B**（首字节 `e9`，占 4MB 分区 5%）
- `build/bootloader/bootloader.bin` 22,560 B
- `build/partition_table/partition-table.bin` 3,072 B
- ELF：`ELF32 / Tensilica Xtensa Processor / EXEC`

**踩坑（已修）**：接缝切换时 host 分支原只定义 `main()`，但 ESP-IDF 的
`freertos/app_startup.c` 强制引用 `app_main` → **链接期**报
`undefined reference to 'app_main'`。修法：两个分支都提供 `app_main`。

### 2) 真实接缝构建（到货后）
```powershell
idf.py -DPLATFORM_KIND=esp32 build     # 切到 platform/esp32/*.c
```

### 3) 纯逻辑立即验证（无需 C 编译器 / ESP-IDF）
```bash
python firmware/tests/test_firmware_contract.py
```
用 Python 镜像固件 C 算法，对照已验证的 `shared/coords.py` 断言逐字节一致。

### 4) esp32 接缝交叉预检（**零硬件，堵 host 构建的盲区**）
```bash
# 前置：曾成功跑过一次 idf.py build（提供 build/compile_commands.json）
cd firmware && bash tools/precheck-seams.sh build
```
**为什么需要它**：host 构建只编译 `platform/host/*.c`，`platform/esp32/*.c` 与
`main.c` 的 esp32 分支**完全不参与编译**。IDF API 的误用（宏改名、签名变更）在
host 构建里**永远暴露不出来**，只会在真机烧写当天引爆。

脚本从 `compile_commands.json` 提取 `main/main.c` 的**真实编译参数**
（`-std=gnu23` + 全部 `-I`/`-D`），仅把 `-DPLATFORM_HOST` 换成 `-DPLATFORM_ESP32`，
对 esp32 分支做 `-fsyntax-only -Wall -Wextra -Wcomment`。

**实测收获**：曾捕获 `HTTPD_ERR_RECV_TIMEOUT`（ESP-IDF v6.1 已移除该宏，
应改用 `HTTPD_SOCK_ERR_TIMEOUT`）——此错误 host 构建不报，只会在真机编译时炸。

**当前覆盖：13/13 通过**（`platform/esp32/*.c` 5 个 + `main.c` esp32 分支 +
纯逻辑层 7 个，含新增 `ble_hid/hid_descriptor.c`）。

**桩头机制**：`esp32-camera` 是第三方组件（需联网拉取）。为仍能检查
`platform_camera.c`，脚本在 `build/_precheck/stub/` 放置最小桩头，
并在「因缺组件头而失败」时**自动带桩头重试**、结果行标注 `(stub)`。
真组件存在时不会走该分支。**桩头通过 ≠ 真机可用**（仅抓语法/类型/宏误用）。

**工具可信度自证**：两轮「注入错误 → 检出 → 恢复」闭环均通过
（IDF 标识符误用、桩头化文件的字段误用），证明预检确有鉴别力，
`(stub)` 通过并非吞掉错误。

### 5) host C 单测（需原生 gcc / MinGW）
```bash
cd firmware/tests && make run
```

## 构建配置

| 文件 | 作用 |
|---|---|
| `CMakeLists.txt` | ESP-IDF 工程顶层（`project(qshou)`）|
| `main/CMakeLists.txt` | 组件注册；`PLATFORM_KIND=host\|esp32` 切换接缝 |
| `sdkconfig.defaults` | N16R8 默认配置（16MB Flash / 8MB Octal PSRAM / NimBLE / WiFi）|
| `partitions.csv` | 分区表（nvs / phy_init / factory 4MB / storage 1MB）|
| `build-firmware.ps1` | 一键构建脚本（含 ESP-IDF 环境注入）|
| `tools/precheck-seams.sh` | esp32 接缝交叉预检（零硬件，堵 host 构建盲区；含桩头回退）|
| `build/_precheck/stub/` | 第三方组件桩头（esp32-camera），供零硬件预检 |

## 当前状态

- 完成：ble_hid（report 打包/解包/发射/swipe 插值 + **57 字节 Digitizer 描述符**）、
  camera（取帧接缝 + MJPEG 构造）、
  main（http_api 合约解析 + routes 分发 + 入口编排）、platform（host 桩 + **esp32 真实实现**）、
  **标准 ESP-IDF 构建工程（含 sdkconfig.defaults / partitions.csv / 一键脚本）**、
  Python 契约测试（全绿）。
- **ESP-IDF 构建链路已端到端跑通**：`idf.py -DPLATFORM_KIND=host build` →
  `Project build complete.`，产出 191KB `.bin`（魔数 `e9`）。到货前已消除
  「卡在构建系统」的风险。
- **esp32 接缝已由交叉预检覆盖**（`tools/precheck-seams.sh`，**13/13 通过**），
  并据此修复了 `HTTPD_ERR_RECV_TIMEOUT`（IDF v6.1 已移除该宏）——此缺陷
  host 构建不报，只会在真机编译时炸。
- **真机链路已全部跑通（2026-10-25 复核）**：
  - `platform/esp32/platform_ble.c`：`esp_hid` + NimBLE transport，
    真实按键/手势发射经 Raw Input 抓包验证送达宿主。
  - `platform/esp32/platform_camera.c`：esp32-camera 路线，DVP 引脚宏可覆写、
    PSRAM 双缓冲、`fb_get → memcpy → fb_return`；OV5640 自动探测出帧，
    framesize 上限 sensor 感知（OV5640→1080p / OV2640→UXGA），
    空闲 60s standby 降温（0x3008[6]）已实测（入睡→唤醒→出帧全链路）。
  - `ble_hid/hid_descriptor.c`：HID 描述符，独立解析器校验通过。
- **`main/main.c` 已重构为零 ESP-IDF 依赖的纯编排层**：原先内联的
  `nvs_flash_init()` / `esp_http_server` 回调 / `BLEDevice::init()` / `xTaskCreate()`
  全部下移到 `platform_net` 接缝，因此本文件在任何平台都能编译，
  也不再依赖 Arduino 生态的 `BLEDevice.h`。
- HTTP API（26 路由）+ MJPEG（端口 81 独立 socket 服务）+ WebSocket events
  均在真机运行；`python run_tests.py` 219 项 Python 测试 + 固件契约全绿。

