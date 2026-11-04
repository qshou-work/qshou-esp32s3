# AI Agent 接入（OpenClaw / DeepSeek Harness / 任意 MCP 宿主）

设备本身不跑 AI。它继续提供 REST 控制面 + `GET /api/v1/schema` 自描述，
由**外部 Python MCP 适配器**把这份 schema 动态翻译成 MCP 工具，
任何支持 MCP 的 AI agent（OpenClaw、DeepSeek Harness、Claude、Cursor…）接上即可。

## 为什么是这个结构

| 层 | 职责 | 为什么放在这里 |
|---|---|---|
| 设备固件（ESP32） | REST + schema 自描述 | MCU 上跑 LLM 不现实；但**自描述必须由设备出**，否则第三方会手抄一份清单，必然漂移 |
| MCP 适配器（本仓库 `controller/mcp_adapter/`） | schema → MCP 工具 | 纯 Python、几十毫秒启动；用 `tools/list` 的 JSON-RPC 而非自己实现协议 |
| AI 宿主 | 推理、规划、用户确认 | 破坏性操作的确认 UI 在宿主侧，适配器只能标注解 |

**零手写工具定义**：设备加了端点，重启适配器即可看到，适配器代码不用改。
本项目已经因为「手抄清单」吃过三次亏（`/calibrate/*` 实现在但自描述漏了、
`X-Auth-Token` 从未实现却写在契约里、`/swipe` 的 request 混着控制器内部坐标语义）。

## 快速开始

```bash
# 1. 确认设备在线并能拉到 schema
curl http://192.168.110.111/api/v1/schema | head -c 200

# 2. 装依赖（仅 mcp SDK；设备客户端只用标准库）
C:\Users\<username>\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pip install mcp

# 3. 看看会注册哪些工具（不启动 server）
python -m controller.mcp_adapter.server --host 192.168.110.111 --list-tools
```

当前真机上是 **29 个工具**（26 个端点，其中 3 个是 GET+POST 双方法）。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QSHOU_HOST` | `qshou-0a0c.local` | 设备地址。mDNS 解析失败时用 IP（见下方「排查」） |
| `QSHOU_TOKEN` | 空 | Bearer 令牌。设备当前 `g_control_auth=false`，留空即可 |
| `QSHOU_TIMEOUT` | `20` | 单请求超时秒。设备是弱 MCU，低于 10s 会把「忙」误判成失败 |
| `QSHOU_SCHEMA_YAML` | `shared/api_schema.yaml` | 契约文件（参数类型来源） |

## OpenClaw

配置文件 `~/.openclaw/openclaw.json` 的 `mcp.servers` 下：

```json
{
  "mcp": {
    "servers": {
      "qshou": {
        "command": "C:\\Users\\<username>\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe",
        "args": ["-m", "controller.mcp_adapter.server"],
        "cwd": "C:\\Users\\<username>\\WorkBuddy\\2026-09-28-12-02-05\\qshou",
        "env": { "QSHOU_HOST": "192.168.110.111" }
      }
    }
  }
```

也可以用 CLI（更不容易写错）：`openclaw mcp add --transport stdio qshou <python 路径> -m controller.mcp_adapter.server`

### ⚠️ 两条由 OpenClaw 实际行为得出的硬约束

1. **不要在 `env` 里放 `PYTHONPATH`**。OpenClaw 有 stdio env 安全过滤器，
   会在 RPC 之前直接拒绝 `PYTHONPATH` / `NODE_OPTIONS` / `PYTHONSTARTUP` 等
   解释器启动开关（防止注入隐式 prelude、换解释器、开调试器）。
   本适配器**不需要**它：`python -m` 会自动把 `cwd` 放进 `sys.path`，
   所以上面的配置把 `cwd` 设成仓库根即可。
2. **Windows 路径必须用双反斜杠或正斜杠**。JSON 里 `C:\Users\...` 会被
   `\U` 解析成转义序列而报错。

验证：
```bash
openclaw mcp status
openclaw mcp probe qshou      # 实际连一次并列出工具
```
改完配置若工具没出现，`openclaw gateway restart`。新增 server / 改 transport
这类变更通常需要重启网关才生效（其余多数热加载）。

### Windows 上的第三个坑：环境变量大小写

若报
```
File "asyncio/windows_events.py", line 8, in <module>
OSError: [WinError 10106] 无法初始化 Winsock
```
说明宿主传给子进程的环境里**缺 `SystemRoot`**。mcp SDK 会 import
`sse_starlette` → `asyncio` → `_overlapped` → Winsock 初始化，缺 SystemRoot
就直接崩。Windows 上这两个变量是**全大写** `SYSTEMROOT` / `WINDIR`
（`SystemRoot` 只是 GUI 里显示的写法），白名单若按 `SystemRoot` 匹配就会漏掉。

已实测：用「基础白名单 + `QSHOU_HOST`、不含 `PYTHONPATH`」的环境
拉起适配器，`rc=0`、正常注册 29 个工具。

## DeepSeek Harness

DSH 官方 MCP 插件通过 `@deepseek-ai/dsh-mcp-client` 接入，配置在 `cordis.yml`：

```yaml
- id: mcp-qshou
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: qshoukvm        # [A-Za-z0-9_-]{1,32}，会成为工具名前缀
    transport: stdio
    command: C:\Users\<username>\.workbuddy\binaries\python\envs\default\Scripts\python.exe
    args: ['-m', 'controller.mcp_adapter.server']
    cwd: C:\Users\<username>\WorkBuddy\2026-09-28-12-02-05\qshou
    env:
      QSHOU_HOST: '192.168.110.111'
    toolCallTimeoutMs: 30000
    failOnStartupError: true   # 设备连不上就别静默降级成「没有工具」
```

模型看到的工具名是 `mcp__qshoukvm__<tool_name>`，例如
`mcp__qshoukvm__gesture_post`、`mcp__qshoukvm__gpio_pin_get`。

> `failOnStartupError` 建议开：默认 false 时设备不可达只会打日志，
> agent 会以为「设备没有这些能力」而不是「适配器没连上」，
> 排查方向会完全跑偏。

若用社区的 `dsh-mcp-bridge` 插件，配置形态是
`{"id":"qshou","command":"...","args":[...]}`，通过 `DSH_MCP_SERVERS` 环境变量传入。

## Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "qshou": {
      "command": "C:\\Users\\<username>\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe",
      "args": ["-m", "controller.mcp_adapter.server"],
      "cwd": "C:\\Users\\<username>\\WorkBuddy\\2026-09-28-12-02-05\\qshou",
      "env": { "QSHOU_HOST": "192.168.110.111" }
    }
  }
}
```

## 给 agent 的使用要点

这些已写进 MCP server 的 `instructions`，宿主会自动带上：

- **光标是相对鼠标**，设备只发相对增量，没有绝对坐标。要把光标移到某处，
  用 `gesture_post({type:"dir", dx, dy})` 或 `hid_mouse_post({dx,dy})` 多次逼近，
  再用 `health_get` 的 `gest_dx`/`gest_dy` 对账实际送达量。
- **位移预算**：BLE 单帧上限 ±127，可交付总位移 = `steps × 127`。
  `gesture_post` 的回包带 `steps_eff` / `budget` / `shortfall`；
  `shortfall=1` 表示这条位移没走完，别假设已落地。
- **⚠️ 标记的工具会改变设备状态或中断连接**，执行前请先向用户确认：
  `settings_post`（设备立即重启）、`wifi_connect_post` / `wifi_disconnect_post`
  （控制面可能失联）、`ble_disconnect_post`、`ble_bonds_post`（删配对）、
  `gpio_pin_post`（直接驱动 IO）。

适配器已给这些工具打上 MCP 注解（`destructiveHint=true`、`idempotentHint=false`），
支持注解的宿主会在执行前自动弹确认。

## 排查

| 症状 | 原因与处理 |
|---|---|
| 宿主里**一个工具都没有**，但适配器手动跑正常 | JSON Schema 非法最常见（本项目踩过：`type:"int"` 不是合法 JSON Schema，严格客户端直接拒收整个 `tools/list`）。跑 `python -m controller.mcp_adapter.server --list-tools` 看能否列出 |
| 报 `Nothing matches the given URI`（esp_http_server 的 404 文案） | 路径拼错。自描述里的 path 是相对 base 的（`/health`），适配器负责拼 `/api/v1` |
| 「调用成功但设备没反应」 | 参数落错了侧：GET 走 query、POST 走 body。混了设备读不到且不报错 |
| 连不上设备 | `qshou-0a0c.local` 在本机可能解析失败（Windows 需 Bonjour/mDNS）。用 IP：从串口日志的 `got ip:` 或 `GET /api/v1/health` 拿 |
| `WinError 10106` / 无法初始化 Winsock | 子进程环境缺 `SystemRoot`（Windows 上是全大写 `SYSTEMROOT`）。见上文「Windows 上的第三个坑」 |
| `httpd_accept_conn error in accept (23)` | 设备 httpd 套接字耗尽，**只能复位**。适配器内部已用锁把所有请求串行化；若仍出现，检查是不是有别的进程在并发打设备 |
| BLE 测试时设备莫名断开 | 别开串口监听：`ser.close()` 的 DTR 下降沿会复位设备，顺带踢掉 BLE 连接 |

## 验证

```bash
# MCP 端到端（以真实 MCP 客户端拉起适配器并调用）—— 17 项
python tools/verify_mcp_live.py 192.168.110.111

# 设备 REST 层回归 —— 14 项
python tools/verify_p34_live.py 192.168.110.111

# 适配器离线单测 + 反向验证
python tests/test_mcp_adapter.py                  # 30 项
python tests/mutation_check_mcp_adapter.py       # 17 条变异须全红
```

REST 通不代表 MCP 通：工具名撞车、类型不合法、`required` 标错，在 REST 层
和契约测试里全都看不出来，故两套验证都要跑。

## 已知限制

- **鉴权默认关闭**。固件的 `g_control_auth` 是编译期常量、当前为 `false`，
  公开端点（`GET /health`、`/sysinfo`、`/settings`、`/schema`、`/led`）始终免鉴权。
  对外开放前须补鉴权 + HTTPS/VLAN —— 这是上市阻断项。
- **只覆盖控制面**。视频流（`:81` 的 MJPEG）没有走 MCP 工具化 ——
  MCP 的文本/JSON 返回承载不了 MJPEG 帧，需要另做资源通道或让 agent 用
  `camera_post` 调参数 + 另行取流。
- **WebSocket 事件通道（`/events`）未工具化**。`events_get` 目前只是查询订阅者数；
  主动推送需要 MCP 的 notification/subscribe 机制，尚未实现。
- 参数类型来自 `shared/api_schema.yaml` 而非设备。设备自描述只给参数名。
  两者不一致时适配器**启动即失败**（而不是猜类型）—— 补 yaml 是唯一正确出路。