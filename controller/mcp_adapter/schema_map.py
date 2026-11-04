# -*- coding: utf-8 -*-
"""controller/mcp_adapter/schema_map.py — 把设备 API schema 映射为 MCP 工具定义。

设计立场
--------
**零手写**：工具清单完全由 `GET /api/v1/schema` 的返回值生成。设备加了新端点、
改了参数，适配器重启即可看到，**不需要改本文件一行**。这是把 REST 控制面接入
AI agent 的关键 —— 手抄一份工具清单必然与设备漂移（项目里已有两代这样的漂移：
`/calibrate/*` 实现在但自描述漏了、`X-Auth-Token` 从未实现却写在契约里、
`/swipe` 的 request 混着控制器内部坐标语义）。

类型从哪来
----------
设备自描述只给 `params`（名字列表），**没有类型** —— 固件里没有 JSON Schema，
硬塞进去要重编固件，且每次加参数都顶高 `/schema` 响应（那条缓冲已经因超限踩过
恒 500 的坑）。故类型从 `shared/api_schema.yaml` 的描述文本推断（`"int  # ..."`
→ integer），yaml 是详版真源，且已由 P5 的双向契约测试与设备自描述钉死。

**缺 yaml 条目就抛 SchemaError，不静默降级**：把 `steps` 当字符串发出去，
LLM 会照着填，真机被钳制或拒掉，表现为「工具调用成功但机器不动」——
这类错在真机上极难定位，宁可在启动时炸掉。

危险端点
--------
`/settings`（改配置后重启设备）、`/wifi/connect`、`/ble/*`、`/gpio/{pin}`
（直接驱动 IO）等属有副作用或不可逆操作，必须打 MCP 注解
（readOnlyHint / destructiveHint），让支持这些注解的宿主在执行前找用户确认。
这不是可选项：AI agent 拿到能改硬件的工具却没有确认提示，是最容易出事的组合。
"""

from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_ROOT, "shared") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "shared"))

import yaml  # noqa: E402

DEFAULT_YAML = os.path.join(_ROOT, "shared", "api_schema.yaml")

BASE = "/api/v1"


class SchemaError(RuntimeError):
    """schema 不可用或不可信（宁可启动失败，也不要给 LLM 一个错的工具清单）。"""


# --------------------------------------------------------------------------
# 类型推断：yaml 描述文本 → JSON Schema
# --------------------------------------------------------------------------

# 描述形如 "int  # 帧数（1..64）" / "string  # HOME / BACK" / "int[5]  # 5 字节 waypoint"
_TYPE_RE = re.compile(r"^\s*(bool|int|float|string)\s*(?:\[\s*(\d+)\s*\])?")

# 🔴 yaml 用 int/float/string/bool（人读），JSON Schema 只认
# integer/number/string/boolean。漏这层映射的话产出的 schema **非法**，
# 严格校验的 MCP 客户端会直接拒绝整个 tools/list（表现为「一个工具都没有」，
# 而适配器日志一切正常）—— 故必须有映射，且必须有守卫钉住。
_JSON_TYPE = {"int": "integer", "float": "number",
              "string": "string", "bool": "boolean"}


def parse_type(desc):
    """yaml 参数描述文本 → (json_type, array_length)。

    只认描述**开头**的类型前缀 —— 后面注释里出现的 int/string 是散文的一部分，
    全文搜索必然误判。

    返回的已是 **JSON Schema 类型名**（integer/number/string/boolean）。
    认不出的类型返回 (None, None)，由调用方决定报错还是降级，**不猜**。
    """
    m = _TYPE_RE.match(str(desc))
    if not m:
        return None, None
    base, arr = m.group(1), m.group(2)
    return _JSON_TYPE[base], (int(arr) if arr else None)


def parse_constraints(desc):
    """从注释里抽数值范围 → (lo, hi)，抽不到返回 None。

    形如 `0..1200`、`[-2..2]`、`1024..8192`。**不编造范围**，但要如实把中文
    说明带给 LLM（"范围 0..1200"），否则它会生成越界值再被设备拒绝。
    """
    text = str(desc)
    tail = text.split("#", 1)[1] if "#" in text else ""
    m = re.search(r"(-?\d+)\s*\.\.\s*(-?\d+)", tail)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _split_desc(desc):
    """`int  # 范围 0..1200` → ('int', '范围 0..1200')；无注释则第二项为空。"""
    text = str(desc)
    if "#" in text:
        head, tail = text.split("#", 1)
        return head.strip(), tail.strip()
    return text.strip(), ""


# --------------------------------------------------------------------------
# 工具命名
# --------------------------------------------------------------------------

_PATH_PARAM_RE = re.compile(r"^\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}$")

# MCP 工具名允许的字符集（保守取 SDK 通用约束）：[A-Za-z0-9_-]
_NAME_BAD_RE = re.compile(r"[^A-Za-z0-9_-]")


def path_params(path):
    """`/gpio/{pin}` → ['pin']；无路径参数 → []。"""
    out = []
    for seg in path.strip("/").split("/"):
        m = _PATH_PARAM_RE.match(seg)
        if m:
            out.append(m.group("name"))
    return out


def tool_name(path, method):
    """`POST /gpio/{pin}` → `gpio_pin_post`。

    规则：去 `/api/v1` 前缀 → 路径参数段去花括号 → 斜杠换下划线 → 拼 method。
    **method 后缀是必需的**：`/settings`、`/ble/bonds`、`/gpio/{pin}` 都是
    GET+POST 同路径，不带 method 会撞名；MCP 侧同名工具只会注册一个，
    另一个静默消失（Agent 侧表现为「有个功能怎么调都没反应」）。
    """
    p = path[len(BASE):] if path.startswith(BASE) else path
    segs = [re.sub(r"[{}]", "", s) for s in p.strip("/").split("/") if s]
    return "_".join(segs) + "_" + method.lower()


# --------------------------------------------------------------------------
# 危险端点分级
# --------------------------------------------------------------------------

# 只标**不可逆 / 会中断控制面**的。中间态（led 开关、鼠标移动、点按）
# 不标 destructive —— 否则宿主对每次点按都弹确认，实际不可用。
_DESTRUCTIVE = {
    "/settings": "写配置后设备立即重启（esp_restart），本次会话中断。"
                 "注意：各字段缺省=保持不变，故只传你要改的键；"
                 "wifi_pass 空串=不改，设备也不会回显密码",
    "/wifi/connect": "切 AP 会断当前连接，设备可能随之失联",
    "/wifi/disconnect": "主动断 WiFi，控制面失联，只能靠 BLE 或重启恢复",
    "/ble/disconnect": "主动断链，对端可能来不及重连",
    "/ble/bonds": "遗忘配对会删 NVS 里的 bond，主机侧需重新配对",
    "/gpio/{pin}": "直接驱动 IO 引脚，可能干扰外设或损坏外接电路；"
                   "合法引脚仅 19/21/22/23/25（其余为相机或 PSRAM 占用，设备会 400 拒绝）",
}

# 幂等语义：**默认都不幂等**。宿主看到 idempotentHint=False 会在重复调用时
# 多问一次，这是安全的一侧；反过来错标 True 会让宿主省掉确认。
# GET 按定义幂等（设备侧所有 GET 都是读，写全走 POST）；POST 里只有「设成某个
# 确定状态」的操作才算幂等（重复设同一个 GPIO 值、开同一个广播开关）。
#
# ⚠️ 点按/移动类**天然不幂等**（点两次 = 点两下、位置也不一样），必须显式列出。
# 相对鼠标下尤其容易搞错：/tap /gesture /hid_mouse 都只发相对增量，
# 重复执行会把光标推到别处，绝不能让宿主以为重复调用是安全的。
_IDEMPOTENT_POST = {
    "/gpio/{pin}",      # 设为确定值
    "/ble/advertise",   # 开关广播
}


def annotations_for(path, methods):
    """生成 MCP ToolAnnotations；第二个返回值是要注入描述里的风险提示。"""
    get_only = all(m == "GET" for m in methods)
    reason = _DESTRUCTIVE.get(path)
    ann = {
        "title": path,
        "readOnlyHint": get_only,
        "destructiveHint": bool(reason),
        # 只有「纯 GET」或「白名单里的 POST」才标幂等，其余一律 False。
        # 写成 `get_only and ...` 会让 _NON_IDEMPOTENT 变成死代码 —— 表面有
        # 规则、实际任何 POST 都被无条件判为不幂等，白名单形同虚设。
        "idempotentHint": get_only or path in _IDEMPOTENT_POST,
        "openWorldHint": True,   # 每次调用都在动真实硬件/网络
    }
    return ann, ("⚠️ %s" % reason if reason else None)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def load_yaml(path=None):
    path = path or os.environ.get("QSHOU_SCHEMA_YAML") or DEFAULT_YAML
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_tools(device_schema, yaml_doc=None):
    """device_schema = GET /api/v1/schema 的 JSON；返回 (tools, warnings)。

    只取 owner=firmware 的端点 —— controller 侧（atom/*）不在设备 httpd 里，
    注册成工具必然 404。

    返回的 tools 里除 MCP 标准字段外，另带 `_path`/`_method`/`_body`/`_query`
    供调用分发使用（构造 MCP Tool 对象前剥掉）。_body 记的是「走 JSON body
    的键」，_query 是「走 URL query 的键」—— 混了会被设备静默忽略。
    """
    if not device_schema or not device_schema.get("ok"):
        raise SchemaError("设备 /schema 返回非 ok：%r" % (device_schema,))
    if device_schema.get("base") != BASE:
        raise SchemaError("设备 base=%r 与适配器预期的 %r 不符"
                          % (device_schema.get("base"), BASE))

    yaml_doc = yaml_doc if yaml_doc is not None else load_yaml()
    ymap = {e["path"]: e for e in yaml_doc.get("endpoints", [])
            if e.get("owner") == "firmware"}

    tools, warnings = [], []
    for e in device_schema.get("endpoints", []):
        path = e["path"]
        methods = list(e.get("methods") or [])
        if not methods:
            warnings.append("%s 自描述未声明 methods，跳过" % path)
            continue
        y = ymap.get(path)
        if y is None:
            raise SchemaError(
                "%s 在 shared/api_schema.yaml 中无 owner=firmware 条目，"
                "无法推断参数类型。设备自描述与契约已漂移 —— 补 yaml（不要在"
                "适配器里手写类型，那样必然再次漂移）" % path)

        ym = list(y.get("methods") or [y["method"]])
        if sorted(ym) != sorted(methods):
            raise SchemaError("%s 方法集不一致：设备 %s / yaml %s"
                              % (path, sorted(methods), sorted(ym)))

        # 参数集也要一致：设备多 = yaml 漏（LLM 不知道有这个参数）；
        # yaml 多 = 契约写错（LLM 会生成设备静默忽略的参数）。
        dev_params = set(e.get("params") or [])
        y_body = set((y.get("request") or {}).keys())
        y_query = set((y.get("query") or {}).keys())
        y_all = y_body | y_query
        if y_all - dev_params:
            raise SchemaError("%s 的 yaml 参数多于设备自描述：%s（Agent 会生成"
                              "设备静默忽略的参数）" % (path, sorted(y_all - dev_params)))
        if dev_params - y_all:
            raise SchemaError("%s 的设备参数未登记进 yaml：%s（该参数对 Agent 隐形）"
                              % (path, sorted(dev_params - y_all)))

        props = {}
        for pname in path_params(path):
            props[pname] = {"type": "integer",
                            "description": "路径参数，取值见路径 %s" % path}

        for sect, keys in (("query", y_query), ("request", y_body)):
            for k in sorted(keys):
                desc = (y.get(sect) or {})[k]
                jt, arr = parse_type(desc)
                if jt is None:
                    raise SchemaError(
                        "%s.%s 类型无法识别：%r —— 请把类型前缀写成 "
                        "bool/int/float/string（可带 [N]）" % (path, k, desc))
                base = _TYPE_RE.match(str(desc)).group(1)
                head, comment = _split_desc(desc)
                rng = parse_constraints(desc)
                if arr:
                    item = {"type": "array", "items": {"type": jt},
                            "minItems": arr, "maxItems": arr}
                    body_txt = (comment or head) + "（固定 %d 个%s）" % (
                        arr, "，不足补 0" if base == "int" else "")
                else:
                    item = {"type": jt}
                    body_txt = comment or head
                    if rng:
                        item["minimum"], item["maximum"] = rng
                item["description"] = body_txt
                props[k] = item

        ann, warn = annotations_for(path, methods)
        parts = []
        base_desc = str(y.get("desc") or "").strip()
        parts.append("%s — %s" % (path, base_desc) if base_desc else path)
        if warn:
            parts.append(warn)
        note = str(y.get("note") or "").strip()
        if note:
            parts.append(note)

        for m in methods:
            is_post = m == "POST"
            schema = {"type": "object", "properties": props,
                      "additionalProperties": False}
            # 🔴 只把**路径参数**标 required，其余一律可选。
            # 理由：设备的 POST 几乎全是「补丁语义」—— json_find_int 读不到就保持
            # 当前值（handle_camera / handle_settings 逐字段 `>=0 ? :cur`），
            # 标 required 会强迫 LLM 去猜「不修改时该填什么」，填错就静默改坏设备。
            # 真的缺必填参数时设备返回明确 400 + 字段名，Agent 能据此重试；
            # 反过来「填了不该填的」才是不可逆的（如误重启设备）。
            if path_params(path):
                schema["required"] = sorted(path_params(path))
            tools.append({
                "name": tool_name(path, m),
                "description": "\n".join(parts),
                "inputSchema": schema,
                "annotations": ann,
                "_path": path,
                "_method": m,
                "_body": sorted(y_body) if is_post else [],
                # 🔴 只能是 query 侧的键。此前写成 sorted(props)（全部属性），
                # 对「同时有 query 和 request」的端点会把 body 参数也塞进
                # GET 的 query，设备读不到 → 静默失效。
                "_query": sorted(y_query) if not is_post else [],
                "_path_params": path_params(path),
            })

    return tools, warnings


def check_tool_names(tools):
    """🔴 工具名唯一性 + 字符集。撞名在 MCP 侧是**静默丢工具**。

    撞名的现实来源：GET+POST 同路径若漏了 method 后缀，
    或两条路径归一化后同名（如 /gpio/{pin} 与 /gpio/pin）。
    """
    seen, dup = {}, []
    bad = []
    for t in tools:
        n = t["name"]
        if n in seen:
            dup.append("%s（%s %s 与 %s %s）"
                       % (n, seen[n][0], seen[n][1], t["_path"], t["_method"]))
        else:
            seen[n] = (t["_path"], t["_method"])
        if _NAME_BAD_RE.search(n) or not n[0].isalpha():
            bad.append(n)
    return dup, bad