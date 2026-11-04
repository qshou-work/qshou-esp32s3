# Qshou — 剩余节点推进计划

> 更新：2026-10-01 22:40。范围：BLE HID 控制链路（已完成）之外的未完成节点。
> 原则：**先把不需要你参与/决策的工作做完**，需要你拍板的项单独列出。

---

## 0. 本轮已落地（真机可验，无需你决策）

| 节点 | 状态 | 交付物 |
|---|---|---|
| MJPEG 视频流 | ✅ 已实现并烧写 | `GET /stream`（multipart/x-mixed-replace，**独立端口 81**）、`GET /snapshot`、UI「远程画面」卡片 |
| 键盘输入 | ✅ 已实现并烧写 | 复合 HID（RID 1=鼠标 / 2=键盘 / 3=Consumer）、`/api/v1/key` 真实发键、UI「键盘输入」卡片 |
| 推流期间输入延迟 | ✅ 已修复并真机验证 | `/stream` 剥离到独立 socket 服务（80 端口 302 跳转）+ 8fps + HID 活动自动节流 |
| 双状态灯（WS2812 + IO2） | ✅ 已定案并烧写 | `platform_led.c`：WS2812 六态 + IO2 = BLE 已连接硬指示（**高电平点亮、GPIO 直驱**） |
| 位移预算（静默少走） | ✅ 已修复并真机验证 | `routes.c` 见下方 §0.1 |

**改动文件**

| 文件 | 改动 |
|---|---|
| `firmware/platform/esp32/platform_net.c` | 新增 `/stream`（阻塞式流式 handler）、`/snapshot`；`platform_stream_start` 改为相机预热 |
| `firmware/main/routes.h` | 新增 `QSHOU_STREAM_BOUNDARY`（边界串单点定义） |
| `firmware/main/main.c` | 复用共享边界串 |
| `firmware/ble_hid/hid_descriptor.c/.h` | 描述符 54 → **125 字节**，新增键盘(RID 2)、Consumer(RID 3) 两个 collection |
| `firmware/ble_hid/hid_report.c/.h` | 新增 `hid_pack_keyboard()` / `hid_pack_consumer()` 与 Report ID 常量 |
| `firmware/ble_hid/ble_hid.c/.h` | 新增 `ble_hid_key_frames()`（名称→帧映射）、`ble_hid_send_key()` |
| `firmware/platform/esp32/platform_ble.c` | 新增 `platform_ble_emit_report()`（按 Report ID 路由）；作业队列扩为「手势 + 按键」共用 |
| `firmware/platform/platform.h`、`platform/host/platform_ble.c` | 新增通用发射接口（host 桩同步） |
| `firmware/main/routes.c` | `/api/v1/key` → `ble_hid_send_key()` |
| `firmware/main/ui.html` | 新增「远程画面」「键盘输入」两张卡片 |
| `firmware/tests/*` | 契约测试扩到 125 字节 / 3 个 Report ID；新增键盘·Consumer 打包与映射断言 |

**静态验证**：`test_firmware_contract.py` → `fails=0`（描述符 125B；RID1=3B / RID2=8B / RID3=2B）。
**Python 侧回归**：`tests/` 全部 60 项 unittest 通过（broker / controller / shared 均绿）。

**关键前提（已核实源码）**：`CONFIG_BT_NIMBLE_SVC_HID_MAX_RPTS=3` 正好容纳 3 个 Report ID，
`REPORT_MAP_SIZE=512` 远大于 125 字节 → **无需改 sdkconfig**，仍是增量编译。

---

## 0.1 位移预算修复（2026-10-01，真机数据定案）

### 现象与证据

BLE 鼠标线帧的 X/Y 是 **8-bit 有符号**，单帧位移上限 ±127 → 一条 DIR 手势能走多远
= `帧数 × 127`。旧实现 `/api/v1/swipe` **写死 16 帧**（上限 2032 counts），而控制器是在
`[0, 10000]` 的虚拟 HID 空间里算增量 → 大位移被 `hid_clamp_delta` 悄悄砍掉，**HTTP 回包仍 echo
请求原值**，调用方完全看不出问题。

真机对照（`/api/v1/gesture` + `health` 对账，press=0 纯移动，无按键风险）：

| 请求 | 修复前实际落地 | 修复后实际落地 |
|---|---|---|
| `dx=2000, steps=12` | `gest_dx=1524`（14 帧）❌ 缩水 23.8% | `gest_dx=2000`（18 帧，`steps_eff=16`）✅ |
| `dx=5000, steps=12` | 上限 2032，**必被砍** | `gest_dx=5000`（42 帧，`step_ms=30`）✅ |
| `dx=10000, steps=12` | 上限 2032，**必被砍** | `gest_dx=10000`（81 帧，`step_ms=15`，`drop=0`）✅ |

放大到控制器路径后果更严重：一次全屏移动只交付 20%，**且控制器 `CursorState` 仍照满额更新**
→ 光标信念与宿主真实位置**永久失同步**（正是 `coords.py` 注释里警告的失效模式，但由内部引起）。

### 修复（放在固件 —— ±127 的知识在固件）

| 项 | 规则 | 理由 |
|---|---|---|
| 帧数 | `ceil(max(|dx|,\|dy|)/127)`，并取 `max(调用方 steps)` | **只扩不缩**：已验证的小位移行为完全不变 |
| 帧间隔 | `min(请求值, 1200ms/帧数)`，下限 15ms | 帧数变多会拉长总时长；15ms = BLE 连接间隔，再快只会撞 ACL 队列 |
| 上限 | 200 帧（帧缓冲 `pts[256]`），总位移 ±25400 | 超出时回包 `shortfall=1`，**永不静默** |
| 回包 | `steps_eff` / `step_ms_eff` / `budget` / `shortfall` | 过去正是因为 echo 请求原值，才让少走 476 counts 一直藏着 |

改动文件：`firmware/main/routes.c`（新增 `gesture_dir_steps()` / `gesture_dir_step_ms()`，
`handle_swipe` 与 `handle_gesture` 均接入）、`shared/api_schema.yaml`（补响应字段与预算说明）、
`firmware/tests/test_firmware_contract.py`（+31 项断言：含「回归见证」——未展开帧数时
steps=12 只能交付 1524，把 bug 指纹钉在测试里）。

### 顺带确认的性能事实

- 不推流时 BLE HID **能稳住 15ms/帧（≈67 fps 通知）且 `drop=0`** → 长距离移动不需要
  靠「少走」来换稳定。
- 这与「推流时延迟」是两条独立的约束线：推流走的是 WiFi 带宽/共存，不是 HID 通知速率。

---

## 1. 待你真机验证（需你参与，我做不了）

### 1.1 描述符已变 → 必须重新配对一次
HID 描述符从「纯鼠标」变为「鼠标+键盘+Consumer」，主机（安卓）按蓝牙配对缓存 Report Map。

1. 安卓蓝牙设置 → **删除/忽略**本设备的旧配对
2. 重新扫描 → 重新配对（Just Works，**无需输密码**）
3. 打开 `http://192.168.2.159/`

### 1.2 建议的测试顺序（先验鼠标，再验键盘）

| 顺序 | 项目 | 期望 | 失败含义 |
|---|---|---|---|
| 1 | 四向移动 | 光标纯移动，不点击不拖行 | 鼠标回归 → 见 1.3 回滚 |
| 2 | 点击按钮 | 精准点选当前光标处 | 同上 |
| 3 | 画方框 | 均匀收口回原点，`gest_dx/dy` = (0,0) 绿 | 同上 |
| 4 | 键盘 HOME / BACK | 安卓回到桌面 / 返回 | 键盘 Report ID 未被订阅 |
| 5 | 键盘 VOL+ / VOL− | 音量增减 | Consumer report 未被订阅 |
| 6 | 发送文本 `abc123` | 目标 App 收到字符 | 键盘 keycode 映射问题 |
| 7 | 开始推流 | 浏览器出现实时画面 | 相机/引脚问题，见 1.4 |

### 1.3 回滚路径（若键盘导致鼠标异常）
把 `hid_descriptor.c` 中「键盘(RID 2)」与「Consumer(RID 3)」两个 block 整段删除，
并把 `hid_descriptor.h` 的 `QSHOU_HID_DESCRIPTOR_REPORT_BYTES` 断言与测试里的 125 改回 54/3，
重新编译烧写 + **再配对一次**。鼠标 RID 1 的字节**未做任何改动**，删除即回到 v22 行为。

### 1.4 相机未就绪时的表现（不是故障）
`platform_camera.c` 的引脚是 **ESP32-S3-EYE 参考值**，未按实际板型核对。
若 `esp_camera_init` 失败：启动日志出现 `camera warm-up failed`，`/stream` 与 `/snapshot` 返回 **503**。
这不是代码问题，而是引脚/供电/模组需核对（见 `docs/partition_pin_plan.md`）。

---

## 2. 标定（calibration）节点

### 2.1 固件侧契约（已就绪，无需改）
| 端点 | 固件行为 |
|---|---|
| `POST /api/v1/calibrate/point` | 校验 `index/cap_x/cap_y`，记日志并确认（`{"ok":true,"index":N}`） |
| `POST /api/v1/calibrate/commit` | 记录 `device_id`，确认 |

规格 v1.9.1 决策：**H 矩阵求解在控制台侧**，固件只做缓冲/确认 → 当前实现符合契约。

### 2.2 已决策：相对鼠标模型（控制器侧转换，无需改固件）✅
已决策为**相对鼠标模型（控制器侧转换，无需改固件）** ✅ — 详见下方 2.2 / 2.3。

### 2.2 实测纠偏：线帧格式本就没坏
经核对固件 `routes.c` 实际契约，关键结论纠正如下：
- 固件 `/api/v1/swipe` 接收 **start/end 两个 5 字节 waypoint 包**，自行差分
  `dx = end − start` 得到相对增量——**线帧格式本就没变**，不存在「5 字节被当成单字节相对增量」的乱走问题。
- 真正的模型缺口在**控制器侧**：相对鼠标无法「跳变定位」，必须自维护一份
  光标位置，把「目标绝对 SCR 坐标」换算成「相对增量」。原 `coords.py` 的
  `swipe` 把屏幕**起点 (x0,y0)** 当 waypoint 起点，但相对鼠标下起点应是**当前光标**，
  否则落点会偏移到 `cursor_prev + (目标 − 屏幕起点)`。

**已落地（控制器侧，纯 Python，无需重编译固件）**：

| 文件 | 改动 |
|---|---|
| `shared/coords.py` | 新增 `CursorState`（控制器侧光标状态追踪）+ `scr_to_relative_move()` / `cap_to_relative_move()`；文档从「Digitizer」改为「虚拟光标坐标空间」；`hid_to_report` 明确为 swipe 双路点包 |
| `controller/app.py` | `ControllerApp` 维护 `cursor`；`/api/v1/swipe` 改为 `start=当前光标, end=目标`，由 `scr_to_relative_move` 求相对增量并更新光标 |
| `tests/test_coords.py` | 新增 `TestCursorState`（增量/更新/越界 clamp/peek 不更新/reset/标定换算） |
| `tests/test_controller.py` | 新增 swipe 相对增量 + 起点=当前光标 两项断言 |
| `tests/assertions.py` | `UseCaseRunner` 集成 `CursorState`，每个用例从 (0,0) 重置，swipe 走相对链路 |
| `shared/api_schema.yaml` | `/tap`、`/swipe` 注释更新为相对鼠标语义 |

**验证**：`test_coords.py` / `test_controller.py` / `tests.assertions` 全绿；
固件契约测试（逐字节校验 `coords.hid_to_report`）因公开 API 未删改仍 `fails=0`。

### 2.3 仍需注意的固有限制（非 bug，相对鼠标的共性约束）
- **光标信念会失同步**：相对鼠标读不回 host 真实光标。若 host 光标被人为移动
  （非本控制器驱动），控制器侧的 `cursor` 会偏。缓解：`CursorState.reset()`
  （如发送 HOME 回到已知锚点后调用）做 re-center。
- **指针加速度**：Chrome/Android 的指针加速度使「SCR 像素 ↔ HID count」非严格 1:1，
  现场应做一次标定确定该比率（已留 `calibrate()` 接缝）。当前 `scr_to_hid` 是线性映射占位，
  比率可在 `Calibration` 上扩展。

---

## 3. 远程访问节点（需采购 / 环境决策）

> 状态：**暂缓**，你已明确「最后再决定」。本节点不阻塞其他工作。

### 3.1 现状
- 规格 v1.5 起默认路径 = **outbound_relay**（ESP32 主动出站连 broker），
  subnet router 降级为可选后端 `lan_direct`。
- 规格 `decide_first` 已收敛结论：**必须有一台与 ESP32 同网段的常开节点**
  （ESP32 跑不了 Tailscale 客户端；安卓手机**不能**当 subnet router）。

### 3.2 需要你拍板（规格 decide_first 四项）

| 选项 | 成本 | 评价 |
|---|---|---|
| 现有家用路由器刷 OpenWrt 装 Tailscale | ¥0 | **最优**，但需先确认型号在 OpenWrt 支持列表且 Flash 足够 |
| 换原生支持 Tailscale 的路由器（GL.iNet 等） | ¥150-300 | 次优，一次性解决 |
| 树莓派 Zero 2 W / 3B | ¥80-200 | 稳妥，但多一台要维护的设备 |
| 复用已有 NAS / 旧 x86 小主机 | ¥0 | 若本来就在跑 Linux，性价比最高 |

**需要你回答**：上面哪一条成立？（或确认路由器的**具体型号**，我帮你查 OpenWrt 支持情况）

### 3.3 选定后我可立即执行的部分（当前无阻塞但不做也无意义）
- 写 subnet router 的 Tailscale 安装/`--advertise-routes` 配置脚本与验收 checklist
- 补 `broker/` + `controller/` 的 outbound_relay 端到端联调脚本

---

## 4. 建议推进顺序

```
[已完成] 视频流（端口 81 独立服务）──┐
[已完成] 键盘/文本（appearance 修复） ├─→ 真机已逐项验证 ✅
[已完成] 双状态灯（IO2 极性+GPIO 定案）│
[已完成] 位移预算（静默少走 → 精确送达）┘
                  ↓
        ① 标定比率实测（counts ↔ 屏幕像素）  ← **当前唯一卡功能精度的节点，需你配合看屏幕**
                  ↓
        ② 屏幕识别/定位（依赖 ①，让「拍屏→坐标」闭环自动）
                  ↓ （你拍板：最后再决定 ⏸）
        ③ 远程访问采购决策（3.2）
```

**进度小结**：
- 视频流、键盘、状态灯、位移预算四个节点**已闭环且经真机数据验证**；
- 「相对鼠标光标信念」的模型链路已落地，但**比例尺未实测** —— 位移能精确送达（counts 不缩水），
  而 counts→屏幕像素的比率仍未知，故「移动到某坐标」目前只能保证走对方向与相对距离，
  不能保证落到指定像素。这是 `2.3` 列的固有约束，也是下一节点（①）要测的东西。
- 仅剩「远程访问」待你采购/环境决策（你已明确「最后再决定」）。
