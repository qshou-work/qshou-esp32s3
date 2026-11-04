# -*- coding: utf-8 -*-
"""tools/verify_mcp_live.py — P6 真机端到端验证：以真实 MCP 客户端拉起适配器并调用。

与 verify_p34_live.py 的分工：那个直接打 REST 验设备，这个验**适配器** ——
即「工具注册得对不对、调得通不通、错误码转 isError 转得对不对」。
两者都要跑：REST 通不代表 MCP 通（工具名撞了、类型不合法、required 标错，
在 REST 层全都看不出来）。

用法：python tools/verify_mcp_live.py [host]
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "QSHOU_HOST", "qshou-0a0c.local")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("%s %-44s %s" % ("PASS" if cond else "FAIL", name,
                           str(detail).replace("\n", " ")[:110]))


async def main():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["QSHOU_HOST"] = HOST
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "controller.mcp_adapter.server"],
        env=env, cwd=ROOT)

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            check("MCP initialize", init.server_info.name == "qshou",
                  "%s %s" % (init.server_info.name, init.server_info.version))
            check("instructions 非空", bool(init.instructions),
                  (init.instructions or "")[:60])

            res = await s.list_tools()
            tools = res.tools
            names = {t.name for t in tools}
            check("tools/list 有工具", len(tools) > 0, "共 %d 个" % len(tools))

            # 覆盖三类工具名形态：纯 GET / GET+POST 同路径 / 路径参数
            check("GET 工具在列", "health_get" in names)
            check("GET+POST 同路径不撞名",
                  {"ble_bonds_get", "ble_bonds_post"} <= names)
            check("路径参数工具在列", "gpio_pin_post" in names)

            # 🔴 JSON Schema 类型合法性：int/float/bool 不是合法 JSON Schema，
            #    严格客户端会直接拒收整个 tools/list（表现为「一个工具都没有」）。
            bad_types = set()
            for t in tools:
                for pname, p in (t.input_schema.get("properties") or {}).items():
                    ty = p.get("type")
                    for cand in ([ty] if isinstance(ty, str)
                                 else (ty or [])):
                        if cand not in ("object", "array", "string", "number",
                                        "integer", "boolean", "null"):
                            bad_types.add("%s.%s=%r" % (t.name, pname, cand))
            check("JSON Schema 类型全部合法", not bad_types, sorted(bad_types))

            # 危险工具必须带 destructiveHint，宿主才会执行前确认
            destructive = {t.name for t in tools
                           if t.annotations and t.annotations.destructive_hint}
            check("破坏性工具已标注",
                  {"settings_post", "wifi_disconnect_post",
                   "gpio_pin_post"} <= destructive, sorted(destructive))

            # 补丁语义端点不能把全部字段标 required（会逼 LLM 乱填）
            cam = [t for t in tools if t.name == "camera_post"][0]
            check("camera_post 非全必填",
                  not cam.input_schema.get("required"),
                  cam.input_schema.get("required"))
            gp = [t for t in tools if t.name == "gpio_pin_post"][0]
            check("gpio_pin_post 路径参数必填",
                  gp.input_schema.get("required") == ["pin"],
                  gp.input_schema.get("required"))

            async def call(name, args):
                out = await s.call_tool(name, args)
                return out, out.content[0].text

            # ---- 只读调用 ----
            # 🔴 判据必须同时看 is_error **和** 响应文本里有 "HTTP 2xx"。
            #    只看 is_error 会假绿：适配器若漏拼 /api/v1 前缀，设备返回
            #    404 + 纯文本（无 ok 字段）→ 未被转成 isError → 一路"PASS"。
            #    ⚠️ 不能要求响应里一定有 "ok": true —— /health、/led 是**无 ok
            #    字段**的老端点（http_bridge 直接透传驱动 JSON）。故只校验
            #    「非空 + 是 HTTP 2xx」，具体内容用 must 逐项断言。
            async def ok_call(name, args, must=None):
                out, txt = await call(name, args)
                good = (not out.is_error) and ("HTTP 2" in txt) and \
                    ("{" in txt or "[" in txt)
                if must:
                    good = good and (must in txt)
                check("call " + name, good, txt[:70])
                return out, txt

            await ok_call("health_get", {}, must="free_heap")
            await ok_call("gpio_get", {})
            # ---- 路径参数 ----
            await ok_call("gpio_pin_get", {"pin": 19}, must="19")
            # ---- 带 query 参数的 GET ----
            await ok_call("led_get", {"io2": 1})

            # ---- 设备 400 要转成 isError（真机白名单拒绝 IO26）----
            out, txt = await call("gpio_pin_get", {"pin": 26})
            check("非白名单引脚 → isError", bool(out.is_error)
                  and "HTTP 400" in txt, txt[:80])

            # ---- 未知工具 ----
            out, txt = await call("no_such_tool", {})
            check("未知工具 → isError", bool(out.is_error), txt[:70])

            # ---- 缺必填路径参数 ----
            out, txt = await call("gpio_pin_get", {})
            check("缺路径参数 → isError", bool(out.is_error), txt[:70])


def _unwrap(e):
    """anyio 的 TaskGroup 会把真实异常包成 ExceptionGroup，
    直接 str(e) 只看到 "unhandled errors in a TaskGroup (1 sub-exception)"，
    根因完全看不到 —— 这里递归剥到最内层。"""
    while True:
        subs = getattr(e, "exceptions", None)
        if not subs:
            return e
        e = subs[0]


def entry():
    try:
        asyncio.run(main())
    except Exception as exc:
        root = _unwrap(exc)
        import traceback
        check("联调执行", False, "%s: %s" % (type(root).__name__, root))
        traceback.print_exception(type(root), root, root.__traceback__)
    ok = sum(1 for _, c, _ in results if c)
    print("\n===== SUMMARY =====")
    print("%d/%d passed" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(entry())