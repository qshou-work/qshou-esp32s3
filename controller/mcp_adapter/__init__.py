# -*- coding: utf-8 -*-
"""controller/mcp_adapter/__init__.py — Qshou 的 MCP 适配器。

把设备的 REST 控制面（`GET /api/v1/schema` 自描述）动态映射为 MCP 工具，
供 OpenClaw / Claude / DeepSeek Harness 等 AI agent 调用。

模块划分：
    schema_map.py     schema → MCP 工具定义（纯逻辑，可离线单测）
    device_client.py  设备 HTTP 客户端（串行化 + GET 可重试 / POST 绝不重试）
    server.py         MCP stdio server（入口：python -m controller.mcp_adapter.server）
"""

__all__ = ["schema_map", "device_client", "server"]
__version__ = "1.0.0"