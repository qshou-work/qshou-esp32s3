# 验证报告 — Qshou（pre_arrival 阶段）

> 生成时间：2026-09-29（最近更新：接缝实现预研 —— 方向 ③）
> 范围：硬件到货前可验证的全部内容（A 档零硬件核心 + 用例级断言 + 固件构建链路 + 接缝实现）
> 说明：本报告记录**已实证**的内容。真机相关验证（B 档）待实物到货后补充。

---

## 1. 总览

| 验证层 | 内容 | 结果 |
|---|---|---|
| Python 单元测试 | 6 个套件 / **60 个用例** | ✅ 全过 |
| 用例级断言 | UC1~UC5（含证据落盘） | ✅ 5/5 PASS |
| 固件算法契约 | 20 项逐字节比对 | ✅ 20/20 通过 |
| HID 报告描述符 | 57 字节 Digitizer，独立解析器校验 | ✅ **10/10 通过** |
| 固件源码编译（host）| 纯逻辑 + host 桩 + main.c | ✅ 零警告零错误 |
| **固件源码编译（esp32 接缝）** | **IDF 原生标志交叉预检** | ✅ **13/13，含 1 处真实缺陷修复** |
| **接缝实现（BLE HID）** | **esp_hidd + NimBLE transport，真头预检** | ✅ **零警告零错误** |
| **接缝实现（Camera）** | **esp32-camera 路线，桩头预检** | ✅ **零警告零错误（stub）** |
| **ESP-IDF 构建链路** | **`idf.py build`（host 接缝）端到端** | ✅ **产出 191KB .bin，魔数 e9** |
| host C 单测 | `test_firmware_c.c` | ⛔ 本机无原生 gcc，未执行 |

---

## 2. Python 单元测试（60 用例）

运行：
```bash
python -m unittest discover -s tests -p "test_*.py"
```

| 套件 | 用例数 | 覆盖内容 |
|---|---:|---|
| `test_coords.py` | 12 | 坐标三级换算、HID report 打包/解包、边界夹取、单应应用 |
| `test_calib.py` | 7 | DLT+Hartley 归一化求解、leave-one-out 残差校验、序列化 |
| `test_transport.py` | 8 | Transport 协议、RelayBackend、MJPEG 解析（零第三方依赖）|
| `test_broker.py` | 15 | 握手鉴权、恒定时间 token 比较、双向字节透传、不解析内容 |
| `test_controller.py` | 9 | 控制器 HTTP 服务、指令转发、mock 设备、静态前端 |
| `test_assertions.py` | 9 | 断言框架自身（含 `passed` 推导、空证据不通过、报告拒绝非法 report）|
| **合计** | **60** | — |

结果：`Ran 60 tests ... OK`

---

## 3. 用例级断言 UC1~UC5

运行：
```bash
python -m tests.assertions        # 跑全部 UC 并落盘到 tests/evidence/
```

**关键设计改进（本轮）**：原先 `passed` 是硬编码 `True`，证据说服力不足。现改为
**由断言清单推导**——`Evidence.passed` 是 property，返回
`len(checks) > 0 and all(c["ok"] for c in checks)`。空证据不再被判为通过。
每条断言（名称 / 是否通过 / 细节）都写入证据 JSON。

| 用例 | 名称 | 验证点 | 断言数 | 结果 |
|---|---|---|---:|---|
| UC1 | 基本导航 | BLE HID 基础通路被接受；report 长度/state/坐标合法 | 6 | ✅ PASS |
| UC2 | 打开指定 App | CAP→SCR→HID 全链路；映射单射性、y 单调、残差 ≤3px | 5 | ✅ PASS |
| UC3 | 列表滚动 | swipe 起终点方向正确、有位移、report 合法 | 6 | ✅ PASS |
| UC4 | 定点返回 | 连发 5 次指令全部成功、单次往返 <50ms、无合并丢失 | 4 | ✅ PASS |
| UC5 | 长时稳定性 | 12 次重复落点零漂移、report 字节序列完全一致、坐标全程合法 | 5 | ✅ PASS |

证据文件（`tests/evidence/`）：
```
UC1_20260929_213208.json
UC2_20260929_213208.json
UC3_20260929_213208.json
UC4_20260929_213208.json
UC5_20260929_213208.json
```

证据 JSON 结构（每条含 `checks` / `checks_passed` / `checks_total` / `steps` / `duration_s`）：
```json
{
  "uc_id": "UC4",
  "covers": "连发指令的时序可靠性与去抖",
  "passed": true,
  "checks": [
    {"name": "连发 5 次 BACK 全部成功", "ok": true, "detail": ""},
    {"name": "单次指令往返 <50ms", "ok": true, "detail": "worst=0.009ms"},
    {"name": "步骤数 == 5（无合并/丢失）", "ok": true, "detail": "steps=5"},
    {"name": "整轮耗时记录", "ok": true, "detail": "total=0.062ms"}
  ],
  "checks_passed": 4, "checks_total": 4
}
```

> ⚠️ **诚实说明**：UC4/UC5 当前跑的是 **InProcessMockDevice**（进程内回声），
> 因此"时序"测的是**调用链开销**而非真实 BLE/网络时延；"长时稳定性"是
> **短循环 + 零漂移逻辑**验证，不是真实长时间运行。真机阶段需改大 `loops`
> 并接真实 transport 后重跑，届时证据才具备端到端说服力。

---

## 4. 固件算法契约（20/20）

运行：
```bash
python firmware/tests/test_firmware_contract.py
```
用 Python 镜像固件 C 算法，对照已验证的 `shared/coords.py` 断言**逐字节一致**：
report 打包/解包往返、越界夹取、big-endian 布局、swipe 段数与首尾、
api_schema 端点存在性。结果：`fails=0`。

---

## 5. 固件源码编译验证

### 5.1 纯逻辑层 + host 桩（`PLATFORM_HOST` 语义）

用已装好的交叉编译器做全量语法与警告检查：
```bash
GCC=~/.espressif/tools/xtensa-esp-elf/esp-15.2.0_20251204/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe
$GCC -fsyntax-only -Wall -Wextra -Wcomment -DPLATFORM_HOST=1 -I<firmware> <file.c>
```

| 文件 | 结果 |
|---|---|
| `ble_hid/hid_report.c` | ✅ 零警告 |
| `ble_hid/ble_hid.c` | ✅ 零警告 |
| `camera/camera.c` | ✅ 零警告 |
| `camera/mjpeg.c` | ✅ 零警告 |
| `main/http_api.c` | ✅ 零警告 |
| `main/routes.c` | ✅ 零警告 |
| `main/main.c`（host 分支）| ✅ 零警告 |
| `platform/host/*.c`（5 个）| ✅ 零警告 |

**`main.c` 两个分支都零警告**是重构成功的实证——它现在完全平台无关。

### 5.2 esp32 接缝：IDF 原生标志交叉预检（关键新增）

**为什么必须单独测这一层**：host 构建只编译 `platform/host/*.c`，
`platform/esp32/*.c` 与 `main.c` 的 esp32 分支**完全不参与 host 编译**。
因此 ESP-IDF API 的误用（宏改名、签名变更）在 host 构建里
**永远暴露不出来**，只会在真机烧写当天引爆。

为此新增脚本 `firmware/tools/precheck-seams.sh`：从 `compile_commands.json`
提取 `main.c` 的**真实编译参数**（`-std=gnu23` + 138 个 `-I` + 全部 `-D`），
仅替换 `-DPLATFORM_HOST` → `-DPLATFORM_ESP32`，对 esp32 分支做 `-fsyntax-only`。

**加固（到货期新增）**：编译标志追加
`-Werror=implicit-function-declaration`。否则「调用了不存在的函数」只产生
**隐式声明警告**，预检会误判通过，拖到链接期才炸。已用「注入不存在的符号
→ 预检报 `error: implicit-function-declaration` + `RESULT: FAIL` → 恢复 PASS」
自证有效。

| 文件 | 结果 |
|---|---|
| `platform/esp32/platform_ble.c` | ✅ 零警告（**真 `esp_hidd.h` + `nimble_port.h`**）|
| `platform/esp32/platform_camera.c` | ✅ 零警告（**stub 桩头**）|
| `platform/esp32/platform_health.c` | ✅ 零警告 |
| `platform/esp32/platform_log.c` | ✅ 零警告 |
| `platform/esp32/platform_net.c` | ✅ 零警告（修复后）|
| `main/main.c`（esp32 分支）| ✅ 零警告 |
| `ble_hid/hid_descriptor.c`（新增，纯逻辑段复查）| ✅ 零警告 |

**总览：`clean=13  issue=0  RESULT: PASS`**

#### 桩头机制：第三方组件的语法预检（新增）

`esp32-camera` 是**第三方托管组件**（需联网 `add-dependency` 拉取），
零硬件环境下通常不在。为仍能对 `platform_camera.c` 做类型/语法检查，
脚本内置**桩头回退**：

- 桩头置于 `build/_precheck/stub/esp_camera.h`，字段与 API 签名照
  esp32-camera v2.x 公开头文件摘录（`camera_config_t` / `camera_fb_t` /
  `esp_camera_init` / `esp_camera_fb_get` / `esp_camera_fb_return`）。
- 每个文件**先按无桩头编译**；仅当失败原因确为「缺少第三方组件头」
  （`*.h: No such file or directory`）时，才带桩头重试一次，并在结果行
  标注 **`(stub)`** 以示区分。**真组件存在时不会走到 stub 分支。**
- 桩头内自包含定义 `ledc_timer_t` / `ledc_channel_t`，避免
  `#include "driver/ledc.h"` 触发 `ledc_etm.h → hal/ledc_types.h → …`
  的级联依赖（预检只需类型可用）。

> ⚠ 桩头通过 ≠ 真机可用。桩头仅用于抓语法/类型/宏误用，
> 真实参数与行为须待组件拉取后复核。

**实测证据**：无桩头时 camera 的真实报错正是
`platform/esp32/platform_camera.c:30:10: fatal error: esp_camera.h: No such file or directory`
—— 证明 `(stub)` 回退是**必要的**，而非无脑兜底。
而 `platform_ble.c` **无 `(stub)` 标注**，且 `-H` 展开显示它实际引用
`esp-idf/components/esp_hid/include/esp_hidd.h`（**真实组件头**）
—— 即 BLE 接缝是用真头通过预检的，这是最强证据。

#### 预检工具的可信度自证（注入→检出→恢复）

为避免"工具形同虚设"，做了**两轮反向验证**：

**第一轮（IDF 标识符误用）**：向 `platform/esp32/platform_log.c` 注入
`ESP_LOGI` → `ESP_LOGI_TYPO_NONEXISTENT`，预检立刻报出：
```
[ERR] platform/esp32/platform_log.c
platform/esp32/platform_log.c:21:5: error: implicit declaration of
    function 'ESP_LOGI_TYPO_NONEXISTENT' [-Wimplicit-function-declaration]
```
恢复后回到 `clean=13 issue=0`。

**第二轮（桩头化文件的字段误用）**：把 `platform_camera.c` 的
`.fb_count` 改为 `.fb_countXX`（桩头路径下），预检同样检出：
```
error: 'camera_config_t' has no member named 'fb_countXX';
       did you mean 'fb_count'?
```
`RESULT: FAIL`（exit 1）；恢复后 `RESULT: PASS`。
**证明桩头预检同样具备鉴别力，`(stub)` 通过并非"吞掉错误"。**

#### 本次预检捕获的真实缺陷（高价值）

`platform/esp32/platform_net.c` 中使用了 `HTTPD_ERR_RECV_TIMEOUT`：

```
error: 'HTTPD_ERR_RECV_TIMEOUT' undeclared;
       did you mean 'HTTPD_408_REQ_TIMEOUT'?
```

- **根因**：该宏在 ESP-IDF v4.x/v5.x 存在，**v6.1 已移除**，改为
  `HTTPD_SOCK_ERR_TIMEOUT`（见 `esp_http_server.h:739`）。
- **影响**：host 构建**不会报错**（该文件不参与编译）。
  若未做预检，此错误将在**硬件到货、首次真机编译时**才暴露，
  直接卡在"到货当天跑不起来"。
- **修复**：改为 `HTTPD_SOCK_ERR_TIMEOUT`（语义不变：接收超时非致命）。

> 这正是「零硬件期做构建接入」的核心价值：把只在烧机时刻才引爆的
> 编译错误，提前到有充裕时间处理的时候发现。

---

## 6. ESP-IDF 构建链路（**端到端已跑通** ✅）

详见 `firmware/README.md`。要点：
- 工程已是标准 ESP-IDF 结构（顶层 `CMakeLists.txt` + `main/CMakeLists.txt` 的 `idf_component_register`）。
- `PLATFORM_KIND=host|esp32` 切换接缝，host 桩为**空操作**，因此**零硬件**即可走通
  「配置 → 编译 → 链接 → 产出 .bin」，用于提前发现构建系统问题。

### 6.1 构建结果（实测）

`idf.py -DPLATFORM_KIND=host build` → **`Project build complete.`** / `EXIT=0`

- 首次全量：**1226 个编译单元**（BLE + WiFi + PSRAM 全开），0 error。
- 自研 12 个源文件全部进入编译图（由 `compile_commands.json` 实证），
  `libmain.a` 成功归档：
  ```
  [1218/1226] Linking C static library esp-idf\main\libmain.a
  ```

**产物（`firmware/build/`）**：

| 文件 | 大小 | 首字节 | 校验 |
|---|---:|---|---|
| `qshou.bin` | **191,296 B** (`0x2eb40`) | `e9` | ✅ ESP 固件魔数 |
| `bootloader/bootloader.bin` | 22,560 B (`0x5820`) | `e9` | ✅ 31% 空闲 |
| `partition_table/partition-table.bin` | 3,072 B | `aa` | ✅ 分区表魔数 |

**ELF 架构核对**（`xtensa-esp32s3-elf-readelf -h`）：
```
Class: ELF32        Machine: Tensilica Xtensa Processor        Type: EXEC
```
**入口符号核对**（`nm`）：`app_main` / `app_main_entry` 均存在于最终 ELF。

**分区占用**：App 占 4 MB 分区的 **5%**（`0x3d14c0` bytes / 95% free）。

**idf.py 已生成到货即用的烧写命令**：
```
python -m esptool --chip esp32s3 -b 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m \
  0x0 build\bootloader\bootloader.bin \
  0x8000 build\partition_table\partition-table.bin \
  0x10000 build\qshou.bin
```

### 6.2 构建期解决的真实缺陷

**① host 接缝缺 `app_main` → 链接失败（重要）**

首轮完整构建在**最后链接阶段**失败：
```
libfreertos.a(app_startup.c.obj):(.literal.main_task+0x20):
    undefined reference to `app_main'
collect2.exe: error: ld returned 1 exit status
```
- **根因**：`main.c` 原用 `#if PLATFORM_ESP32 → app_main / #else → main`，
  但 ESP-IDF 的 `components/freertos/app_startup.c` **强制引用 `app_main`**，
  与接缝类型无关。
- **迷惑性**：1200+ 编译单元全过，只在最后链接才炸，极易误判为"环境问题"。
- **修复**：两个分支都提供 `app_main`；`main()` 仅在 `FW_HOST_STANDALONE_MAIN`
  宏下提供（IDF 体系不需要也不应定义 `main`）。
- 修复后增量重建（9 步）→ `Project build complete.`

**② 环境类**

- **子模块空目录**：`components/unity/unity`、`spiffs/spiffs`、`protobuf-c/protobuf-c`
  在 `git clone --recursive --depth 1` 后为空壳，导致
  `CMake Error: Include directory '...' is not a directory`。
  修法：`cd <submodule> && git checkout -f`。
- **`idf_tools.py` 拒绝 MSys/Mingw**：必须在 Windows PowerShell 中运行，
  不能在 Git Bash 下运行。
- **日志为 UTF-16LE**：需 `iconv -f UTF-16LE -t UTF-8` 后阅读；
  其中 `error` 字样多为假阳性（文件名 `esp_tls_error_capture.c`、
  PowerShell `NativeCommandError`）。

---

## 7. 接缝实现预研（方向 ③，零硬件完成）

在硬件到货前，把 `platform/esp32/` 的两个关键接缝从**骨架**推进为
**可编译的真实实现**，使「到货当天」只需拉第三方组件 + 烧写，而非从零写。

### 7.1 HID 报告描述符（纯逻辑，可完全离线验证）

新增 `firmware/ble_hid/hid_descriptor.{h,c}` —— 一个 **57 字节**的
Digitizer（Touch Screen）report descriptor：

| 项 | 值 |
|---|---|
| Usage Page / Usage | `0x0D` / `0x04`（Digitizer / Touch Screen）|
| Report ID | `0x01` |
| 报告总长 | **40 bit = 5 字节**（与 `HID_REPORT_LEN` 一致）|
| 字段顺序 | Tip Switch(1b) + In Range(1b) + padding(6b) → state 首字节；X(16b)；Y(16b) |
| Logical / Physical Max | 10000（`0x2710`）|

**独立解析器校验**：`tools/parse_hid_descriptor.py` 独立解析字节并逐项核对
**10 项规格**，实测 **10/10 通过**（Usage Page / Usage / Collection /
Report ID / 报告总长 / 字段顺序 / state 首字节 / X·Y 16bit /
Logical Max 10000 / Physical Max 10000）。

**编译期守护**：`hid_descriptor.c` 末尾有
```c
_Static_assert(QSHOU_HID_DESCRIPTOR_REPORT_BYTES == HID_REPORT_LEN, ...);
```
若有人改了 `HID_REPORT_LEN` 未同步 descriptor，**编译期即报错**，
不会拖到真机上空包才发现。

### 7.2 BLE HID 接缝（`platform/esp32/platform_ble.c`）

从骨架 → 真实实现，走 **`esp_hid` 组件（NimBLE transport）**：

- `esp_hidd_dev_init(&cfg, ESP_HID_TRANSPORT_BLE, hid_event_handler, &s_dev)`
- `platform_ble_emit()`：未启动返 -1；**未连接静默丢弃返 0**（不阻塞图像主循环）；
  已连接则 `esp_hidd_dev_input_set(s_dev, map_idx, report_id, report, len)`
- 事件回调跟踪连接态；`s_report_maps[]` 引用 `qshou_hid_report_map`
- 新增 `platform_ble_is_connected()` 便于上层查询

**⚠ 到货期发现并修复的真机头号炸点 —— NimBLE 协议栈未初始化**

原实现直接调 `esp_hidd_dev_init()`，但 **NimBLE 协议栈从未启动**。
`esp_hid` 的 NimBLE transport（`src/nimble_hidd.c`）**只负责注册 HID GATT**，
依赖 NimBLE host 已就绪，**自身不启动协议栈** → 真机上必然失败（BLE 广播不出现）。

修复：新增 `bt_stack_ensure_init()`，序列对照 IDF 官方
`examples/bluetooth/esp_hid_device/main/esp_hid_gap.c` 的 `init_low_level()`：
```
esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT)
  → nimble_port_init()                            // 内部：esp_bt_controller_init + enable + esp_nimble_init
  → nimble_port_freertos_init(nimble_host_task)   // host task 跑 nimble_port_run()
```
> 已核实 IDF v6.1 源码：`nimble_port_init()`（`nimble_port.c:322`）内部依次调
> `esp_bt_controller_init` → `esp_bt_controller_enable(ESP_BT_MODE_BLE)` →
> `esp_nimble_init()`。故**只调 `esp_nimble_init()` 会漏掉 controller 初始化**，
> 必须用 `nimble_port_init()`。

修复后预检通过（`platform_ble.c` 用真实 `nimble_port.h` / `nimble_port_freertos.h`）。

**关键决策**：NimBLE 下 `esp_hid` **官方支持 HID Device**
（`CONFIG_BT_NIMBLE_ENABLED` 时编译 `nimble_hidd.c`），因此
**无需手写 HID GATT 服务，也无需第三方库**（规避 Arduino 生态的
许可证不确定性）。

**预检结果**：零警告零错误，且用的是**真实** `esp_hidd.h`（见 §5.2）。
`main/CMakeLists.txt` 的 `FW_REQUIRES` 已加入 `esp_hid`。

**踩坑记录**（预检捕获）：`esp_hidd_event_data_t` 中 `protocol_mode` /
`control` 是**匿名结构体成员**，不能按整数访问；须写
`p->protocol_mode.protocol_mode` / `p->control.control`。

### 7.3 Camera 接缝（`platform/esp32/platform_camera.c`，esp32-camera 路线）

从骨架 → 真实实现，走 **`esp32-camera` 组件**：

- 完整 `camera_config_t`：DVP 引脚、`PIXFORMAT_JPEG`、`FRAMESIZE_VGA`、
  `jpeg_quality 12`、`fb_count 2`、`CAMERA_FB_IN_PSRAM`、`CAMERA_GRAB_LATEST`
- **内部拷贝缓冲** `s_buf`（PSRAM `heap_caps_malloc`，退化 `malloc`，
  `QSHOU_CAM_BUF_CAP = 128KB`）—— 因 `esp_camera_fb_get()` 的帧归还后失效，
  而接缝契约要求返回缓冲在下次调用前**保持有效**，故做一次 memcpy
- `camera_ensure_init()` 幂等；`platform_camera_capture()` 做
  `fb_get → memcpy → fb_return`
- **引脚护栏（D 档）**：所有 GPIO 集中在文件顶部宏，且全部用 `#ifndef`
  包裹**允许编译期覆写**（`-DQSHOU_CAM_PIN_XCLK=...`），到货核对板型后
  只需改宏，**不触碰逻辑代码**（参考分配见 `docs/partition_pin_plan.md`）

**预检结果**：零警告零错误（**经桩头**，因组件未拉取；见 §5.2 桩头机制）。

### 7.4 获取第三方组件（到货 / 联网后）

```bash
cd firmware
idf.py add-dependency "espressif/esp32-camera^2.0.0"
idf.py -DPLATFORM_KIND=esp32 build
```

> 沙箱当前**无法访问组件注册表**（HTTP 000），故 camera 的真实组件
> 尚未拉取；接缝代码已按官方 API 写好并用桩头通过预检。

---

## 8. 未验证 / 待实物（诚实清单）

| 项 | 原因 | 到货后动作 |
|---|---|---|
| `test_firmware_c.c`（host C 单测）| 本机无原生 Windows gcc | 装 mingw-w64 或改用 WSL |
| UC1~UC5 真机断言 | 需真实设备 + 真实 H 矩阵 | 替换 calibration/device，重跑并重落证据 |
| **esp32 接缝的完整 `idf.py build`** | 需拉 esp32-camera 组件并全量链接 | 到货/联网后 `idf.py add-dependency` + `-DPLATFORM_KIND=esp32 build`（当前已由交叉预检覆盖语法层）|
| BLE HID 被 Y71t 接受 | 需实物 | 烧写后按 UC1 验证 |
| OV2640 取帧 / MJPEG 编码 | 需实物 + 组件 | 接缝已实现（§7.3），待真机验证出图 |
| WiFi 凭据连通 | 需实物 + NVS 灌入 | 按 B1 清单操作 |
| 屏幕可用区实测（B2）| 需 Y71t | 实测后回填 SCR 常量（**禁止提前硬编码**，见 D4）|
| 放电速率（B4）/ 指针读数（B5）| 需实物 + 配件 | 按 B 档清单执行 |
| 键盘 HID 描述符 | 需库接入 | 按 `docs/ble_descriptor_review.md` 逐项核对 |

> **已消除的风险**：原「到货当天可能卡在构建系统」的风险**已基本消除** ——
> 标准 IDF 工程结构已跑通、构建链路端到端产出 .bin、esp32 分支语法已预检、
> 烧写命令已由 idf.py 生成。**且 BLE HID 与 Camera 两个关键接缝已从骨架
> 推进为可编译的真实实现**（§7），到货后剩余的是「拉组件 + 真机调参 +
> 补键盘描述符」，而非从零写接缝。

---

## 9. 复现方式（一键）

```bash
# Python 全量回归
python -m unittest discover -s tests -p "test_*.py"

# 用例断言 + 证据落盘
python -m tests.assertions

# 固件算法契约
python firmware/tests/test_firmware_contract.py

# HID 报告描述符规格校验（独立解析器，10 项）
python tools/parse_hid_descriptor.py

# esp32 接缝交叉预检（零硬件，专堵 host 构建覆盖不到的洞）
# 前置：曾成功跑过一次 idf.py build（提供 compile_commands.json）
# 覆盖：platform/esp32/*.c（camera 走桩头）+ main.c esp32 分支 + 纯逻辑层
cd firmware && bash tools/precheck-seams.sh build

# 固件构建（需 ESP-IDF）
cd firmware && .\build-firmware.ps1              # host 接缝（零硬件）
cd firmware && .\build-firmware.ps1 -Seam esp32   # 真实接缝（到货后）
```

---

## 10. 硬件到货上机

**→ 见 `docs/arrival_bringup.md`（到货上机指南）**。

要点（详见该文档）：
- **头三件事**：核对实际板型摄像头引脚 / 拉取 `esp32-camera` 组件 / 确认
  Flash+PSRAM 容量丝印。
- **建议顺序**：先单独跑通 camera（引脚是最大不确定性），再接 BLE HID
  （避免两个变量混在一起排查）。
- **到货期已修的真机炸点**：NimBLE 协议栈未初始化（见 §7.2）。
- 摄像头引脚现为 **ESP32-S3-EYE 参考分配**，若与实际板型不符，改
  `platform_camera.c` 顶部宏或用 `-DQSHOU_CAM_PIN_*` 覆写（不碰逻辑代码）。

---

## 11. 2026-10-25 真机复验：分辨率运行时切换 8/8 全通过

**背景**：分辨率下拉框（QVGA→1080p）此前在 15/16 档（UXGA 1600×1200 /
1080p 1920×1080）稳定返回 `/snapshot` 503。串口日志显示
`cam_hal: FB-OVF` —— 帧缓冲溢出。

**根因**：`esp32-camera` 组件在 `esp_camera_init()` → `cam_config()` 时按**初始**
frame_size 一次性分配帧缓冲（JPEG 模式 `recv_size ≈ w*h/5`，VGA ≈ 60KB）；
运行时 `sensor->set_framesize()` 只改传感器寄存器，**不重分配缓冲**。
切到 UXGA/1080p 后单帧远超缓冲 → FB-OVF → `esp_camera_fb_get()` 4s 超时
返回 NULL → 路由层 503。

**修复**：`platform_camera_set_framesize()` 改走 `esp_camera_reconfigure()`
（组件官方运行时重配 API = `deinit` + 按新 frame_size 重新 `init` 分配 fb）。
注意 `deinit` 会 `free(s_state)`，**旧 `sensor_t*` 指针失效**，必须重新
`esp_camera_sensor_get()` 并重套已存图像参数。代价：切档瞬间图传约 0.5–1s
中断（显式用户动作，可接受）。

**实测**（`tools/verify_resolution_live.py qshou-0a0c.local`，固件
`Oct 25 2026 21:00:58` / md5 `c609862976a60a8e8b4a30a5aa29385c`）：

| 枚举 | 档位 | 期望 | 实测 | 结果 |
|---|---|---|---|---|
| 6 | QVGA | 320×240 | 320×240 | ✅ |
| 9 | 480×320 | 480×320 | 480×320 | ✅ |
| 10 | VGA（默认）| 640×480 | 640×480 | ✅ |
| 11 | SVGA | 800×600 | 800×600 | ✅ |
| 12 | XGA | 1024×768 | 1024×768 | ✅ |
| 13 | 720p | 1280×720 | 1280×720 | ✅ |
| 15 | UXGA | 1600×1200 | 1600×1200 | ✅（修复前 503）|
| 16 | 1080p | 1920×1080 | 1920×1080 | ✅（修复前 503）|

**回归门**：Python 219/219 + C 单元 102/102 全通过（`run_tests.py` +
`firmware/tests/run_c_tests.sh`）。契约测试 `test_camera_framesize_contract`
同步改为断言 `esp_camera_reconfigure()`（旧断言盯 `set_framesize(s,...)`，
会把正确实现判红），并补 15/16 下拉选项守卫。

**顺带修掉的更名遗留**：`test_firmware_c.c` 的 MJPEG 边界断言还写死
`--Y7` 前缀（旧品牌 Y71t），而边界串已在 `routes.h` 改为
`QSHOU_STREAM_BOUNDARY "QshouFrame"`。改为直接引用该宏，改名不再漂移。
