# -*- coding: utf-8 -*-
"""controller/mcp_adapter/server.py — Qshou 的 MCP stdio server。

跑起来：
    python -m controller.mcp_adapter.server --host 192.168.110.111
或（宿主配置里推荐的形态，环境变量传参）：
    QSHOU_HOST=192.168.110.111 python -m controller.mcp_adapter.server

MCP 侧设计
----------
- **工具零手写**：启动时 `GET /api/v1/schema` → schema_map 生成工具定义。
  设备加了端点，重启本进程即可看到，适配器不用改。
- **stdio 传输**：宿主（OpenClaw / Claude / DeepSeek Harness）以子进程拉起，
  走 stdin/stdout 讲 JSON-RPC。⚠️ 因此**日志只能写 stderr** —— 往 stdout
  写一个字节就会污染 JSON-RPC 流，宿主侧表现为握手后连接莫名断开
  （MCP 生态里最常见的坑之一）。
- **危险操作打注解**：`destructiveHint` 让支持方在执行前找用户确认。

环境变量
--------
QSHOU_HOST      设备地址（默认 qshou-0a0c.local）
QSHOU_TOKEN     Bearer 令牌（设备当前 g_control_auth=false 时可留空）
QSHOU_TIMEOUT   单请求超时秒（默认 20）
QSHOU_SCHEMA_YAML   契约文件路径（默认 shared/api_schema.yaml）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_PARENT = os.path.dirname(_HERE)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

import anyio  # noqa: E402  (mcp 依赖它，必须先 import)
import mcp.server.stdio as stdio  # noqa: E402
import mcp.types as types  # noqa: E402
from mcp.server import Server  # noqa: E402

from mcp_adapter.device_client import DeviceClient, DeviceError  # noqa: E402
from mcp_adapter.schema_map import (  # noqa: E402
    BASE, SchemaError, build_tools, check_tool_names)

DEFAULT_HOST = "qshou-0a0c.local"

INSTRUCTIONS = """\
控制 Qshou（ESP32-S3 + OV2640 摄像头 BLE HID 远控器）。

工具清单由设备自身的 GET /api/v1/schema 动态生成，无需臆造端点。

使用要点：
- 光标是**相对鼠标**：设备只发相对增量，没有绝对坐标。要把光标移到某处，
  用 gesture_post({type:"dir", dx, dy}) 或 hid_mouse_post({dx,dy}) 多次逼近，
  并用 health_get 的 gest_dx/gest_dy 对账实际送达量。
- 位移预算：BLE 单帧上限 ±127，故 steps × 127 才是可交付总位移。
  gesture_post 的回包带 steps_eff / budget / **shortfall**；
  shortfall=1 表示这条位移没走完，别假设已落地。
- 带 ⚠️ 的工具会改变设备状态或中断连接（重启/断网/断链/驱动 IO），
  执行前请先向用户确认。
"""


def log(msg):
    """日志一律走 stderr —— stdout 是 JSON-RPC 通道，碰不得。"""
    print("[qshou-mcp] %s" % msg, file=sys.stderr, flush=True)


def _make_tool(t):
    """内部 _path/_method/_body/_query/_path_params 是分发用的，MCP 不认。"""
    return types.Tool(
        name=t["name"],
        description=t["description"],
        inputSchema=t["inputSchema"],
        annotations=types.ToolAnnotations(**t["annotations"])
        if t.get("annotations") else None,
    )


def _text(s, is_error=False):
    return [types.TextContent(type="text", text=s)], is_error


def _fmt(status, payload):
    """把设备响应格式化成 Agent 好读的样子。"""
    if isinstance(payload, dict) and payload.get("ok") is False:
        return "❌ HTTP %s：%s" % (status, payload.get("error") or payload), True
    body = json.dumps(payload, ensure_ascii=False, indent=2,
                      sort_keys=True) if isinstance(payload, (dict, list)) else str(payload)
    return "HTTP %s\n%s" % (status, body), False


class QshouServer(object):
    def __init__(self, host=None, token=None, timeout=None, schema_yaml=None):
        self.host = host or os.environ.get("QSHOU_HOST") or DEFAULT_HOST
        self.token = token or os.environ.get("QSHOU_TOKEN") or None
        self.timeout = float(timeout or os.environ.get("QSHOU_TIMEOUT") or 20)
        if schema_yaml:
            os.environ["QSHOU_SCHEMA_YAML"] = schema_yaml
        self.client = DeviceClient(self.host, self.token, self.timeout)
        self.tools = []
        self.by_name = {}
        # 🔴 mcp 2.x 是**构造期回调**（on_list_tools/on_call_tool），
        # 不是 v1 的 @server.list_tools() 装饰器 —— 装饰器写在这里连方法都绑不上。
        self.server = Server("qshou", version="1.0",
                             instructions=INSTRUCTIONS,
                             on_list_tools=self._on_list_tools,
                             on_call_tool=self._on_call_tool)

    # ------------------------------------------------------------- 启动
    def bootstrap(self):
        """拉 schema、建工具表。失败即抛 —— 起一个没有工具的 MCP server
        比直接失败更糟：Agent 会以为是「设备不支持」而不是「适配器没连上」。"""
        log("正在连接设备 %s ..." % self.host)
        schema = self.client.fetch_schema()
        tools, warns = build_tools(schema)
        dup, bad = check_tool_names(tools)
        if dup or bad:
            raise SchemaError("工具名非法或冲突：重名=%s 非法=%s" % (dup, bad))
        for w in warns:
            log("WARN %s" % w)
        self.tools = tools
        self.by_name = {t["name"]: t for t in tools}
        log("已注册 %d 个工具（设备 %s 自描述 %d 个端点）"
            % (len(tools), self.host, len(schema.get("endpoints", []))))
        return self

    # ------------------------------------------------------------- 分发
    async def _on_list_tools(self, ctx, params):
        return types.ListToolsResult(tools=[_make_tool(t) for t in self.tools])

    async def _on_call_tool(self, ctx, params):
        name = params.name
        args = params.arguments or {}
        t = self.by_name.get(name)
        if t is None:
            return types.CallToolResult(
                content=_text("未知工具 %r。设备当前支持 %d 个工具：%s"
                              % (name, len(self.by_name),
                                 ", ".join(sorted(self.by_name))), True)[0],
                is_error=True)

        path, method = t["_path"], t["_method"]
        # 🔴 自描述里的 path 是**相对 base 的**（"/health"），而 httpd 里注册的是
        # "/api/v1/health"。漏拼 base 的症状极隐蔽：设备返回 404 + 纯文本
        # "Nothing matches the given URI"，不带 ok 字段 → 若只按 ok:false 判错，
        # 就会把 404 当成功报给 Agent（表现为「工具调用成功但什么都没发生」）。
        url_path = BASE + path
        # 路径参数先填回 URL
        for pname in t["_path_params"]:
            if pname not in args:
                return types.CallToolResult(
                    content=_text("缺少路径参数 %s（%s）" % (pname, path), True)[0],
                    is_error=True)
            url_path = url_path.replace("{%s}" % pname, str(args[pname]))

        if method == "GET":
            query = {k: v for k, v in args.items() if k not in t["_path_params"]}
            try:
                status, payload = self.client.get(url_path, query)
            except DeviceError as e:
                return types.CallToolResult(content=_text(str(e), True)[0], is_error=True)
        else:
            body = {k: v for k, v in args.items() if k in t["_body"]}
            try:
                status, payload = self.client.post(url_path, body)
            except DeviceError as e:
                return types.CallToolResult(content=_text(str(e), True)[0], is_error=True)

        text, is_err = _fmt(status, payload)
        # 🔴 三种失败都要转 isError，缺一不可：
        #   ① HTTP 4xx/5xx —— esp_http_server 层拒绝（如路由不存在）返回的是
        #      **纯文本** "Nothing matches the given URI"，压根没有 ok 字段；
        #   ② HTTP 200 + {"ok":false} —— 固件层的语义失败（gpio 白名单拒绝、
        #      参数越界等），只看状态码会把失败当成功报给 Agent；
        #   ③ 5xx —— 响应缓冲装不下（snprintf 返回所需长度 ≥ cap 时 handler
        #      主动 return 500，见 P5 的 /schema 恒 500 事故）。
        if status >= 400:
            is_err = True
        if isinstance(payload, dict) and payload.get("ok") is False:
            is_err = True
        return types.CallToolResult(content=_text(text, is_err)[0], is_error=is_err)

    async def run(self):
        self.bootstrap()
        async with stdio.stdio_server() as (r, w):
            await self.server.run(r, w,
                                  self.server.create_initialization_options())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qshou-mcp")
    ap.add_argument("--host", help="设备地址（默认取 QSHOU_HOST 或 %s）" % DEFAULT_HOST)
    ap.add_argument("--token", help="Bearer 令牌（默认取 QSHOU_TOKEN）")
    ap.add_argument("--timeout", type=float, help="单请求超时秒（默认 20）")
    ap.add_argument("--schema-yaml", help="契约文件路径（默认 shared/api_schema.yaml）")
    ap.add_argument("--list-tools", action="store_true",
                    help="只打印将注册的工具清单然后退出（不启动 MCP server）")
    args = ap.parse_args(argv)

    srv = QshouServer(args.host, args.token, args.timeout, args.schema_yaml)
    if args.list_tools:
        srv.bootstrap()
        for t in srv.tools:
            req = t["inputSchema"].get("required", [])
            print("%-26s %-5s %s%s" % (
                t["name"], t["_method"], t["_path"],
                (" 必填=%s" % req) if req else ""))
        return 0
    anyio.run(srv.run)
    return 0


if __name__ == "__main__":
    sys.exit(main())