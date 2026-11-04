# Qshou-ESP32S3（千手）

> **Qshou（千手）开源生态中基于 ESP32-S3 硬件的终端控制工程，可通过 HTTP 和 MCP 使用。**

给 AI 安上「千手」：ESP32-S3 用摄像头当**眼睛**（拍屏）、BLE HID 当**手**（键鼠），
把任意「有屏幕 + 蓝牙」的终端（手机 / 电脑 / BIOS 界面）变成可远程操控的对象。
不依赖 ADB、不装 APP、不要求被控端有视频输出接口。

控制面双通道，同一份契约（`shared/api_schema.yaml`）：

| 使用者 | 通道 | 入口 |
|---|---|---|
| 人类 | Web UI | `http://qshou-0a0c.local/` |
| 脚本 / 自动化 | HTTP REST API | `/api/v1/*`（26 个路由，设备自描述 `/api/v1/schema`） |
| AI Agent | **MCP**（stdio） | `python -m controller.mcp_adapter.server`（工具定义从 `/api/v1/schema` 自动生成，设备加端点重启即见） |

命名取自「千手」——AI 伸出的一千只手。硬件原型为低成本 IP-KVM 类设备，
软件面按「终端控制设备」设计：视频、输入、状态、配置全部 API 化。

## 硬件

- **A 板（当前唯一目标）**：ESP32-S3 + OV5640 摄像头 + BLE HID 复合设备
  （RID 1 相对鼠标 / RID 2 键盘 / RID 3 Consumer）。mDNS `qshou-0a0c.local`
  （主机名 = `qshou-` + WiFi STA MAC 尾两字节），示例 IP `192.168.2.160`，串口 COM3。
- OV5640 空闲 60s 自动 standby 降温（0x3008[6]），下次取帧自动唤醒；
  framesize 上限 sensor 感知（OV5640 → 1080p，OV2640 → UXGA）。
- 分辨率运行时切换走 `esp_camera_reconfigure()`（deinit + 按新 frame_size 重分配帧缓冲），
  切档瞬间图传约 0.5–1s 中断。**不能只调 `sensor->set_framesize()`**——组件在 init 时按
  初始档一次性分配 fb，运行时只改寄存器不重分配缓冲，切 UXGA/1080p 会撑爆缓冲
  （`cam_hal: FB-OVF` → `fb_get` 4s 超时 → `/snapshot` 503，2026-10-25 真机实测复现）。
- 历史被控端：vivo Y71t（OriginOS，安卓只接受相对鼠标——见下方验证结论）。
- B 板（OV5640）已退货，不再适配；`docs/board_b_ov5640_bringup.md` 仅存档。

## 规格真源

`docs/spec/Qshou_Spec.yaml`（v1.9.2）。本仓库是工程实现，所有设计决策以规格为准。

> 该文件是更名前的原始规格，内部仍沿用 `Y71t_*` 命名——其中多数 "Y71t" 实指
> **被控端手机 vivo Y71t**，并非本项目。按史料原样保留，未做批量改名
> （盲目替换会把「手机型号」错改成项目名）。

## 快速开始

```bash
# 1) 人类：浏览器打开 Web UI
http://qshou-0a0c.local/

# 2) HTTP：健康检查 / 拍一张 / 发一个按键
curl http://qshou-0a0c.local/api/v1/health
curl -o snap.jpg http://qshou-0a0c.local/snapshot
curl -X POST http://qshou-0a0c.local/api/v1/key -H 'Content-Type: application/json' -d '{"key":"UP"}'

# 3) AI / MCP：stdio server（需 pip install mcp anyio）
QSHOU_HOST=qshou-0a0c.local python -m controller.mcp_adapter.server
```

MCP 适配器细节见 `docs/mcp_agent_integration.md`。⚠️ stdio 传输下**日志只能写
stderr**——往 stdout 写一个字节就会污染 JSON-RPC 流。

## 构建与凭据

```bash
# 构建（脚本在仓库根，工程在 firmware/ 子目录，故必须 -C firmware）
python idf_run.py -C firmware -DPLATFORM_KIND=esp32 build   # 真机（默认）
python idf_run.py -C firmware -DPLATFORM_KIND=host  build   # 零硬件桩，仅验证可编译

# 烧录（必须前台同步执行；后台托管会冻结 ninja→ccache→gcc 链且日志无报错）
cd firmware && python idf_build_flash.py --port COM3
```

**WiFi 凭据不入库**。正常路径是把 `wifi_ssid` / `wifi_pass` 写进设备 NVS
（浏览器 UI 或 `POST /api/v1/settings`，冷生效，NVS 永远优先）。若想额外内置
「备用网络」给 NVS 为空的板子兜底，编辑
`firmware/platform/esp32/qshou_wifi_creds.h` —— 该文件已在 `.gitignore` 中，
不会被提交，格式见同目录 `qshou_wifi_creds.h.example`。该文件缺失时
`main/CMakeLists.txt` 会在配置期从模板生成空表，因此**全新克隆开箱即可编译**。

> ⚠️ 不要用 `#if __has_include(...)` 做可选包含：可选依赖不进 gcc `-MD`
> 依赖表，ccache 直连模式会静默复用「空表版本」的旧 `.obj`，凭据补上后固件里
> 依然没有，且编译零报错（2026-10-27 实测踩坑）。现在是**无条件包含 +
> 配置期生成**，头文件是正常依赖，改它就会正确触发重编。

## 真机验证结论（务必先读，都是踩出来的）

| 结论 | 说明 |
|---|---|
| **广播 appearance 必须是 `GENERIC(0x03C0)`** | 声明成 Mouse(0x03C2) 时安卓只建鼠标设备、只订阅 RID 1 → 键盘/文本全静默失效（鼠标照常）。改后主机须「取消配对→重连」刷新设备类型。 |
| **安卓只接受相对鼠标** | Digitizer/Touch 被当触摸屏只点按；Absolute Mouse 不建鼠标设备、无光标。唯一可行路径 = Generic Desktop/Mouse + 相对 X/Y（8-bit 有符号）。 |
| **单帧位移上限 ±127** | 交付总位移 = 帧数 × 127。固件按位移自适应展开帧数并下调 `step_ms`；回包带 `shortfall` 字段，**为 1 即表示没走完**，不许假设 `{dx,dy}` 已落地。 |
| **MJPEG 必须独立于 httpd 主服务** | `esp_http_server` 只有一个 httpd 任务，阻塞式 `/stream` 会独占它 → 推流期间所有 API（含 HID 下发）全部排队。故视频流拆到端口 81 的独立 lwIP socket 服务，80 端口 `/stream` 改 302 跳转。 |
| **不要动 WiFi/BLE 共存偏好** | 本 IDF 只有 `WIFI`/`BT`/`BALANCE` 三值（无 `BT_BALANCE`）。曾试「偏向 BT + 关省电」，结果**反而使 HID 整体失效**。共射频场景下的延迟靠「降帧率 + HID 活动自动节流」解决，不靠 coexist 偏好。 |
| **推流与输入共用 2.4G 射频** | 默认 8fps（`STREAM_FRAME_DELAY_MS=125`）+ HID 活动后 250ms 内暂停发帧（`HID_THROTTLE_WINDOW_MS`）。 |
| **IO2 状态灯 = 高电平点亮，走 GPIO 直驱** | LEDC 在本板 GPIO2 上出不来波形；且 IO2 曾与相机 XCLK 抢同一组 LEDC timer/channel。详见 `firmware/platform/esp32/platform_led.c` 头注释。 |

## 现场端点

| 用途 | 地址 |
|---|---|
| Web UI | `http://qshou-0a0c.local/` |
| 视频流 | `http://qshou-0a0c.local:81/stream` |
| 健康诊断 | `http://qshou-0a0c.local/api/v1/health` |
| 单帧快照 | `http://qshou-0a0c.local/snapshot` |
| 端点自描述 | `http://qshou-0a0c.local/api/v1/schema` |
| 灯/极性现场处置 | `http://qshou-0a0c.local/api/v1/led?io2=255&pol=1&mode=1` |

## 目录

| 目录 | 职责 |
|---|---|
| `shared/` | 跨分区共享契约：`api_schema.yaml`、坐标换算（含 `CursorState`）、标定 profile |
| `broker/` | 中转服务端（MVP 全量中转；远程访问节点选型后启用） |
| `controller/` | 控制台：Win10 UI + **MCP adapter**（`mcp_adapter/`，stdio server） |
| `firmware/` | ESP32 固件（main / ble_hid / camera / platform{esp32,host}） |
| `tools/` | 离线工具（描述符解析与校验、标定网格、串口观测、真机验证脚本） |
| `tests/` | Python 自动化断言 + `tests/evidence/` 真机用例证据 |
| `firmware/tests/` | 固件契约测试（无 C 编译器也能跑：解析描述符字节 + 镜像 HID 算法） |
| `fixtures/` | 测试夹具 |
| `docs/` | 文档（推进计划、验证报告、引脚/分区方案、MCP 集成） |

## 环境约定

- Python：`shared/` 仅标准库 + numpy（核心换算零外部依赖）；
  `controller/mcp_adapter/` 需官方 MCP SDK（`pip install mcp anyio`）。
- 版本号：SemVer，与规格 `version` 独立演进
- 命名：遵循规格 `naming_conventions`

## 跑测试

```bash
python run_tests.py          # 一条命令跑全部：219 项 Python unittest + 固件契约测试
python run_tests.py -v       # 详细输出
```

⚠️ Python 侧测试需要 numpy。若报 `ModuleNotFoundError: numpy`，那是**环境缺依赖而非代码问题**：
用装过 numpy 的解释器跑，或 `python -m pip install numpy`。`run_tests.py` 会前置检查并直接给出修复命令。

## 构建与烧写（ESP-IDF）

```bash
python idf_run.py -C firmware -DPLATFORM_KIND=esp32 build   # 增量编译（脚本在仓库根，项目在 firmware/ 子目录）
cd firmware && python idf_build_flash.py --port COM3        # 增量编译 + 上传（前台执行）
```

- `PLATFORM_KIND` 必须显式指定——idf.py 会把它缓存进 build 目录 CMakeCache，
  不带参数复用缓存会把 host 桩固件覆盖进 esp32 产物。
- 全量重编译极慢（~12min），**非必要不要触发**；删掉 `firmware/build/` 就等于全量。
- Git Bash 会注入 `MSYSTEM` 导致 `idf.py` abort，故一律经 `idf_run.py` / `esptool_run.py` 调用。
- BLE 测试期间**禁开串口监听**：串口关闭时的 DTR 下降沿会复位板子并踢掉 BLE 连接。
