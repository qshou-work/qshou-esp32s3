# broker/ — 中转服务端（MVP 全量中转哑中继）

MVP 传输决策（execution_policy.mvp_transport_decision）：ESP32 主动出站连 broker，
视频与指令都经 broker，走 TCP/WebSocket。本目录实现**最简单的全量中转**：

- `relay.py` — 纯逻辑：`RelayHub` 管理「device_id + 每设备 token 鉴权 + 双向字节透传」，不解析业务数据
- `server.py` — `BrokerServer`：TCP 监听，首行 JSON 握手鉴权，之后原样转发两端字节
- `tokens.example.json` — device_id → token 注册表样例（**每设备唯一凭据**，满足 guardrail）
- `__init__.py` — 包标识，支持 `python -m broker.server` 一键 demo

## 设计边界（满足 R20 / guardrails）

- **哑中继**：broker 只透传字节，绝不解析屏幕内容或指令语义 → 即便走中继，broker 拿到的仍是密文
- **每设备唯一凭据**：token 在 broker 侧预置，鉴权用恒定时间比较避免时序侧信道
- **零第三方依赖**：仅标准库（socket / threading / json）

## 运行

```bash
# 一键端到端验证（启动中转 + 模拟设备/控制端回显）
python -m broker.server

# 生产形态（从注册表加载 token）
python -c "from broker.relay import RelayHub; from broker.server import BrokerServer; \
BrokerServer(hub=RelayHub.load_tokens('broker/tokens.json'), port=8765).start()"
```

## 与 A3 的衔接

`shared/transport.py` 的 `RelayBackend` 通过 HTTP/JSON（指令）+ MJPEG-over-HTTP（视频）
对接设备本地 API；设备侧后续接入时，把「本地 API」改为「经 broker 中转」即可，broker 接口契约不变。

## 测试

```bash
python -m unittest tests.test_broker
```
