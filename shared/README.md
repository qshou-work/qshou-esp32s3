# shared/ — 跨分区共享契约

无硬件依赖的机器可读契约，是其它分区的唯一真源：

- `api_schema.yaml` — 所有 `/api/v1/*` 请求/响应 JSON schema 与字段定义
- `coords.py` / `coords.h` — CAP→SCR→HID 换算纯函数（无硬件依赖）
- `transport.py` — 传输层抽象（step_8b）：`Transport` 接口 + MVP 单后端 `RelayBackend`
  （控制端 → broker 透传 → ESP32；命令走 HTTP/JSON，视频走 MJPEG over HTTP 标准库解析，零依赖）
- `devices/<device_id>/calib.json` — 标定结果（H 矩阵 + residual_offset + 残差）

注意：坐标换算函数与传输层都不依赖真实 H 矩阵/硬件，用合成 H 与注入式 http_post 即可开发与测试。
