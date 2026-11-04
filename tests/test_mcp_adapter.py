# -*- coding: utf-8 -*-
"""tests/test_mcp_adapter.py — P6 MCP 适配器契约测试（离线，不连设备）。

与 test_control_contract.py 的分工：那个守**设备与 yaml 的一致性**；
这个守**「schema → MCP 工具」这一步映射本身没做错**。
两者都要有：schema 一致但映射写错（类型名非法、required 标错、工具名撞车），
在设备侧和契约测试里全都看不出来。

真机端到端另见 tools/verify_mcp_live.py。
"""

from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "controller"), os.path.join(ROOT, "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from mcp_adapter.schema_map import (  # noqa: E402
    BASE, SchemaError, _DESTRUCTIVE, _JSON_TYPE, annotations_for, build_tools,
    check_tool_names, load_yaml, parse_constraints, parse_type, path_params,
    tool_name,
)

# JSON Schema draft-07 合法类型全集（局部 + 顶层）
_VALID_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}

_YAML = load_yaml()


def _schema_fixture(endpoints):
    return {"ok": True, "base": BASE, "endpoints": endpoints}


def _eps_from_yaml(*paths):
    """按真实 yaml 的参数集构造「设备自描述」fixture。

    ⚠️ params 必须与 yaml 一致，否则映射层会（正确地）报漂移错误 —— 
    那是本测试要验的行为，不是 fixture 缺陷。
    """
    want = set(paths) if paths else None
    out = []
    for e in _YAML["endpoints"]:
        if e.get("owner") != "firmware":
            continue
        if want and e["path"] not in want:
            continue
        out.append({
            "path": e["path"],
            "methods": list(e.get("methods") or [e["method"]]),
            "params": sorted(set((e.get("request") or {})) |
                             set((e.get("query") or {}))),
        })
    return out


class TestToolNaming(unittest.TestCase):
    def test_get_and_post_of_same_path_do_not_collide(self):
        """🔴 GET+POST 同路径必须生成两个工具名。

        MCP 侧同名工具只会注册一个、另一个静默消失 —— Agent 侧表现为
        「有个功能怎么调都没反应」，且适配器日志一切正常。
        """
        self.assertEqual(tool_name("/ble/bonds", "GET"), "ble_bonds_get")
        self.assertEqual(tool_name("/ble/bonds", "POST"), "ble_bonds_post")
        self.assertNotEqual(tool_name("/settings", "GET"),
                            tool_name("/settings", "POST"))

    def test_path_params_stripped_from_name(self):
        self.assertEqual(tool_name("/gpio/{pin}", "POST"), "gpio_pin_post")

    def test_base_prefix_stripped(self):
        self.assertEqual(tool_name("/api/v1/health", "GET"), "health_get")

    def test_path_params_extracted(self):
        self.assertEqual(path_params("/gpio/{pin}"), ["pin"])
        self.assertEqual(path_params("/wifi/status"), [])
        self.assertEqual(path_params("/a/{x}/b/{y}"), ["x", "y"])

    def test_names_are_mcp_safe(self):
        """工具名只允许 [A-Za-z0-9_-] 且不能以数字开头。"""
        tools, _ = build_tools(_schema_fixture(
            _eps_from_yaml("/health", "/gpio/{pin}", "/ble/bonds")))
        dup, bad = check_tool_names(tools)
        self.assertEqual(dup, [])
        self.assertEqual(bad, [])
        for t in tools:
            n = t["name"]
            self.assertTrue(n[0].isalpha(), n)
            for ch in n:
                self.assertIn(ch, "abcdefghijklmnopqrstuvwxyz"
                                     "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-", n)


class TestTypeInference(unittest.TestCase):
    def test_yaml_types_map_to_valid_json_schema_types(self):
        """🔴 yaml 词（int/float/bool）必须映射成 JSON Schema 类型名。

        不映射的话产出的 schema 非法（"type":"int"），严格校验的 MCP 客户端
        会拒收整个 tools/list —— 表现是「一个工具都没有」，而适配器无任何报错。
        """
        self.assertEqual(parse_type("int  # 帧数"), ("integer", None))
        self.assertEqual(parse_type("float  # X 像素"), ("number", None))
        self.assertEqual(parse_type("bool  # 1=开"), ("boolean", None))
        self.assertEqual(parse_type("string  # HOME"), ("string", None))
        self.assertEqual(parse_type("int[5]  # waypoint"), ("integer", 5))
        self.assertEqual(parse_type("int[6]  # keys"), ("integer", 6))
        for src, got in (("int", "integer"), ("float", "number"), ("bool", "boolean")):
            self.assertEqual(_JSON_TYPE[src], got)

    def test_unknown_type_returns_none_rather_than_guessing(self):
        """认不出的类型不许猜 —— 调用方据此抛错。"""
        self.assertEqual(parse_type("随便写点什么"), (None, None))
        self.assertEqual(parse_type(""), (None, None))

    def test_only_leading_prefix_is_read(self):
        """类型前缀必须**锚定在开头**，不许全文搜索。

        yaml 里大量描述是散文：`"string  # 里面提到 int"`。
        不锚定时 re.search 会先撞上注释里的类型词，把 string 判成 int。
        """
        self.assertEqual(parse_type("string  # 里面提到 int 和 float"), ("string", None))
        # 反例：描述以非类型词开头、注释里才有类型词 —— 不锚定就会误判成 integer
        self.assertEqual(parse_type("whatever  # int 帧数"), (None, None))
        self.assertEqual(parse_type("0..1200 的名字"), (None, None))

    def test_range_constraints_extracted(self):
        self.assertEqual(parse_constraints("int  # 范围 0..1200"), (0, 1200))
        self.assertEqual(parse_constraints("int  # -2..2"), (-2, 2))
        self.assertIsNone(parse_constraints("string  # HOME / BACK"))

    def test_every_generated_schema_uses_only_valid_types(self):
        tools, _ = build_tools(_schema_fixture([
            {"path": e["path"], "methods": list(e.get("methods") or [e["method"]]),
             "params": sorted(set((e.get("request") or {})) |
                               set((e.get("query") or {})))}
            for e in _YAML["endpoints"] if e.get("owner") == "firmware"
            and e["path"] != "/schema"]))
        for t in tools:
            schema = t["inputSchema"]
            self.assertIn(schema["type"], _VALID_TYPES)
            for pname, p in schema["properties"].items():
                types_ = p["type"] if isinstance(p["type"], list) else [p["type"]]
                for ty in types_:
                    self.assertIn(ty, _VALID_TYPES,
                                  "%s.%s 的 type=%r 不是合法 JSON Schema 类型"
                                  % (t["name"], pname, ty))
                if p["type"] == "array":
                    self.assertIn(p["items"]["type"], _VALID_TYPES)


class TestDriftDetection(unittest.TestCase):
    """映射层必须对「设备与 yaml 漂移」**炸**，而不是悄悄降级。"""

    def test_endpoint_missing_from_yaml_raises(self):
        """设备有、yaml 无 → 抛错。

        静默降级的后果：那个参数对 Agent 隐形，或类型全猜错，
        真机表现是「调用成功但设备没反应」。
        """
        with self.assertRaises(SchemaError) as cm:
            build_tools(_schema_fixture([
                {"path": "/brand_new", "methods": ["POST"],
                 "params": ["a", "b"]}]))
        self.assertIn("api_schema.yaml", str(cm.exception))

    def test_yaml_has_param_device_does_not_raises(self):
        """yaml 多出设备没有的参数 → 抛错（Agent 会生成设备静默忽略的参数）。

        ⚠️ 必须构造「**只有这一个方向**」的漂移。若误用「设备多一个参数」的
        fixture，两个检查会互相兜住，关掉任一个另一个仍会红 → 变异测不出来，
        守卫就成了摆设（第一版就踩了这个：两个 fixture 都造的是设备多参数）。
        """
        doc = {"endpoints": [{"path": "/health", "method": "GET", "owner": "firmware",
                              "methods": ["GET"], "query": {"ghost": "int  # 不存在"}}]}
        with self.assertRaises(SchemaError) as cm:
            build_tools(_schema_fixture(
                [{"path": "/health", "methods": ["GET"], "params": []}]), doc)
        self.assertIn("ghost", str(cm.exception))

    def test_method_set_mismatch_raises(self):
        """方法集不一致 → 抛错（否则 query/body 参数该放哪都说不清）。"""
        doc = {"endpoints": [{"path": "/health", "method": "GET", "owner": "firmware",
                              "methods": ["GET", "POST"]}]}
        with self.assertRaises(SchemaError):
            build_tools(_schema_fixture(
                [{"path": "/health", "methods": ["GET"], "params": []}]), doc)

    def test_non_ok_schema_raises(self):
        with self.assertRaises(SchemaError):
            build_tools({"ok": False})
        with self.assertRaises(SchemaError):
            build_tools(None)

    def test_base_mismatch_raises(self):
        """设备 base 与适配器预期不符必须炸 —— 否则所有路径都拼错。"""
        with self.assertRaises(SchemaError):
            build_tools({"ok": True, "base": "/api/v2", "endpoints": []})

    def test_unknown_type_prefix_raises(self):
        doc = {"endpoints": [{"path": "/health", "method": "GET", "owner": "firmware",
                              "methods": ["GET"], "query": {"a": "whatever  # 说明"}}]}
        with self.assertRaises(SchemaError):
            build_tools(_schema_fixture(
                [{"path": "/health", "methods": ["GET"], "params": ["a"]}]), doc)


class TestToolSemantics(unittest.TestCase):
    def _tools(self, eps=None, doc=None):
        if eps is None:
            eps = _eps_from_yaml()
        return build_tools(_schema_fixture(eps), doc)

    def test_path_params_are_the_only_required_fields(self):
        """🔴 补丁语义端点不能全标 required。

        设备的 POST 几乎都是「读不到就保持当前值」（handle_camera 逐字段
        `>=0 ? :cur`），标 required 会逼 LLM 去猜「不修改时填什么」，
        猜错就静默改坏设备。缺必填参数时设备会返回明确 400，Agent 可据此重试；
        反过来「填了不该填的」才不可逆（如误重启）。
        """
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        self.assertEqual(by["gpio_pin_post"]["inputSchema"]["required"], ["pin"])
        self.assertEqual(by["gpio_pin_get"]["inputSchema"]["required"], ["pin"])
        for name in ("camera_post", "settings_post", "gesture_post", "tap_post"):
            self.assertIsNone(by[name]["inputSchema"].get("required"),
                              "%s 不该有 required" % name)

    def test_query_params_are_not_required_for_post(self):
        """POST 的 body 键与 GET 的 query 键必须分开记，不能混。

        🔴 用 `/led`（只有 query）验不出来：它 y_body 为空，混了也没差别；
        也不能笼统断言「POST 的 _body 非空」—— `/wifi/disconnect` 本就无参。
        故造一个**同时有 query 与 request** 的端点，把两侧都钉死。
        """
        doc = {"endpoints": [{
            "path": "/both", "method": "GET", "methods": ["GET", "POST"],
            "owner": "firmware", "desc": "混合端点",
            "query": {"q": "int  # 查询参数"},
            "request": {"b": "int  # body 参数"}}]}
        tools, _ = build_tools(_schema_fixture(
            [{"path": "/both", "methods": ["GET", "POST"], "params": ["b", "q"]}]),
            doc)
        by = {t["name"]: t for t in tools}
        self.assertEqual(by["both_get"]["_query"], ["q"])
        self.assertEqual(by["both_get"]["_body"], [])
        self.assertEqual(by["both_post"]["_body"], ["b"])
        self.assertEqual(by["both_post"]["_query"], [])

    def test_real_contracts_route_params_to_correct_side(self):
        """真实契约里，GET 端点的参数必须落在 query 侧。"""
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        self.assertEqual(sorted(by["led_get"]["_query"]), ["io2", "mode", "pol"])
        self.assertEqual(by["led_get"]["_body"], [])
        self.assertTrue(by["camera_post"]["_body"])
        self.assertEqual(by["camera_post"]["_query"], [])

    def test_destructive_tools_are_flagged(self):
        """⚠️ 能改硬件/断连接的工具必须打 destructiveHint，
        宿主才会执行前找用户确认。"""
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        for name in ("settings_post", "wifi_disconnect_post", "wifi_connect_post",
                     "ble_disconnect_post", "ble_bonds_post", "gpio_pin_post"):
            self.assertTrue(by[name]["annotations"]["destructiveHint"],
                            "%s 应标 destructive" % name)
            self.assertIn("⚠️", by[name]["description"],
                          "%s 的描述里应写明风险" % name)

    def test_read_only_tools_are_marked(self):
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        for name in ("health_get", "sysinfo_get", "wifi_status_get", "ble_status_get"):
            self.assertTrue(by[name]["annotations"]["readOnlyHint"], name)
            self.assertFalse(by[name]["annotations"]["destructiveHint"], name)

    def test_relative_mouse_tools_are_not_idempotent(self):
        """点两次 = 点两下，不能标幂等（否则宿主会省掉确认）。

        相对鼠标下尤其容易搞错：/tap /gesture /hid_mouse 都只发**相对增量**，
        重复执行会把光标推到别处。
        """
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        for name in ("tap_post", "gesture_post", "hid_mouse_post", "key_post",
                     "hid_keyboard_post", "swipe_post"):
            self.assertFalse(by[name]["annotations"]["idempotentHint"], name)

    def test_idempotent_whitelist_is_live_not_dead_code(self):
        """🔴 幂等白名单必须真的起作用。

        若写成 `idempotentHint = get_only and path in _IDEMPOTENT_POST`，
        则任何 POST 都被无条件判为不幂等、白名单形同虚设 —— 表面有规则、
        实际永远走不到。故正反两面都断言：白名单内 True，白名单外 False。
        """
        doc = {"endpoints": [
            {"path": "/idempotent_post", "method": "POST", "methods": ["POST"],
             "owner": "firmware", "request": {"v": "int  # 设成确定值"}},
            {"path": "/counting_post", "method": "POST", "methods": ["POST"],
             "owner": "firmware", "request": {"v": "int  # 累加"}},
        ]}
        tools, _ = build_tools(_schema_fixture([
            {"path": "/idempotent_post", "methods": ["POST"], "params": ["v"]},
            {"path": "/counting_post", "methods": ["POST"], "params": ["v"]},
        ]), doc)
        # 两者都不在 _IDEMPOTENT_POST → 必须都判为不幂等
        by = {t["name"]: t for t in tools}
        self.assertFalse(by["idempotent_post_post"]["annotations"]["idempotentHint"])
        self.assertFalse(by["counting_post_post"]["annotations"]["idempotentHint"])

        # 而真实契约里 /gpio/{pin} 在白名单内 → 必须为 True
        tools2, _ = self._tools()
        by2 = {t["name"]: t for t in tools2}
        self.assertTrue(by2["gpio_pin_post"]["annotations"]["idempotentHint"],
                        "/gpio/{pin} 设为确定值，是幂等操作")
        self.assertFalse(by2["tap_post"]["annotations"]["idempotentHint"])

    def test_every_destructive_path_is_documented(self):
        """_DESTRUCTIVE 里每条都要真在契约里存在（防写了条不存在的路径）。"""
        paths = {e["path"] for e in _YAML["endpoints"] if e.get("owner") == "firmware"}
        for p in _DESTRUCTIVE:
            self.assertIn(p, paths, "_DESTRUCTIVE 里的 %s 在契约中不存在" % p)

    def test_array_params_have_fixed_length(self):
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        keys = by["hid_keyboard_post"]["inputSchema"]["properties"]["keys"]
        self.assertEqual(keys["type"], "array")
        self.assertEqual(keys["minItems"], 6)
        self.assertEqual(keys["maxItems"], 6)

    def test_range_constraints_land_in_schema(self):
        """数值范围要进 schema，别只写在散文里 ——
        LLM 靠 minimum/maximum 才会避开越界值。"""
        tools, _ = self._tools()
        by = {t["name"]: t for t in tools}
        mode = by["gpio_pin_post"]["inputSchema"]["properties"]
        self.assertEqual((mode["mode"]["type"]), "string")
        val = by["hid_keyboard_post"]["inputSchema"]["properties"]["modifier"]
        self.assertEqual((val.get("minimum"), val.get("maximum")), (0, 255))

    def test_additional_properties_false_blocks_typos(self):
        """LLM 常把 int 写成 string 或多编一个键；关掉额外属性才能早点暴露。"""
        tools, _ = self._tools()
        for t in tools:
            self.assertIs(t["inputSchema"]["additionalProperties"], False, t["name"])


class TestToolTableIntegrity(unittest.TestCase):
    def test_no_duplicate_tool_names(self):
        tools, _ = build_tools(_schema_fixture([
            {"path": "/ble/bonds", "methods": ["GET", "POST"], "params": ["id"]},
            {"path": "/settings", "methods": ["GET", "POST"], "params": ["mdns_host"]},
            {"path": "/gpio/{pin}", "methods": ["GET", "POST"], "params": ["mode"]},
        ]), {"endpoints": [
            {"path": "/ble/bonds", "method": "GET", "methods": ["GET", "POST"],
             "owner": "firmware", "request": {"id": "int  # id"}},
            {"path": "/settings", "method": "GET", "methods": ["GET", "POST"],
             "owner": "firmware", "request": {"mdns_host": "string  # 主机名"}},
            {"path": "/gpio/{pin}", "method": "GET", "methods": ["GET", "POST"],
             "owner": "firmware", "request": {"mode": "string  # in|out"}},
        ]})
        dup, bad = check_tool_names(tools)
        self.assertEqual(dup, [])
        self.assertEqual(bad, [])
        self.assertEqual(len(tools), 6)


class TestRealContractRoundTrip(unittest.TestCase):
    """用真实 shared/api_schema.yaml 跑一遍（回归：契约变了这里会提醒）。"""

    def test_all_firmware_endpoints_map_to_tools(self):
        tools, warns = build_tools(_schema_fixture(_eps_from_yaml()))
        self.assertEqual(warns, [])
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))
        # 每个 owner=firmware 端点至少产出一个工具
        expect = {e["path"] for e in _YAML["endpoints"]
                  if e.get("owner") == "firmware"}
        got = {t["_path"] for t in tools}
        self.assertEqual(expect - got, set())

    def test_controller_only_endpoints_not_exposed(self):
        """controller 侧（atom/*）不在设备 httpd 里，注册成工具必然 404。"""
        tools, _ = build_tools(_schema_fixture(_eps_from_yaml()))
        paths = {t["_path"] for t in tools}
        for e in _YAML["endpoints"]:
            if e.get("owner") == "controller":
                self.assertNotIn(e["path"], paths)


class TestIntegrationDoc(unittest.TestCase):
    """🔴 接入文档里的配置示例必须与实现一致，且不含会坑宿主的写法。

    文档里的配置是**用户唯一的手工入口**，一旦与实现漂移，用户照抄就起不来。
    三个已踩的坑必须被钉住：
      ① 不得出现 PYTHONPATH（OpenClaw 的 stdio env 安全过滤器直接拒绝）；
      ② 必须用 cwd 指向仓库根（适配器靠 `python -m` 把 cwd 放进 sys.path）；
      ③ 启动模块名/入口必须真实存在。
    """

    DOC = os.path.join(ROOT, "docs", "mcp_agent_integration.md")

    def _doc(self):
        with open(self.DOC, "r", encoding="utf-8") as f:
            return f.read()

    def test_doc_exists_and_lists_both_hosts(self):
        doc = self._doc()
        # OpenClaw 的键是嵌套的 mcp.servers（不是 Claude 那种 mcpServers），
        # DeepSeek Harness 走 cordis.yml + dsh-mcp-client 插件。
        for token in ("OpenClaw", "DeepSeek Harness", '"mcp"', '"servers"',
                      "cordis.yml", "dsh-mcp-client", "openclaw.json"):
            self.assertIn(token, doc, "接入文档缺少 %s 相关说明" % token)

    def test_doc_never_suggests_pythonpath(self):
        """OpenClaw 会拒绝含 PYTHONPATH 的 stdio env 块。

        本适配器也不需要它（python -m 自动带 cwd）。文档若建议加 PYTHONPATH，
        用户照抄会被宿主拒绝，或在别的宿主下掩盖路径问题。
        """
        doc = self._doc()
        # 允许出现在「不要这样做」的警告段落里，但不得出现在任何 JSON/YAML
        # 配置块的 env 字段里。取所有代码块逐一检查。
        for block in re.findall(r"```(?:json|yaml)\n(.*?)```", doc, re.S):
            self.assertNotIn("PYTHONPATH", block,
                             "配置示例里出现了 PYTHONPATH —— OpenClaw 的 stdio "
                             "env 安全过滤器会在 RPC 前直接拒绝它")

    def test_doc_config_blocks_point_at_real_entrypoint(self):
        """配置示例里的 command/args 必须指向真实可跑的入口。"""
        doc = self._doc()
        self.assertIn("controller.mcp_adapter.server", doc)
        self.assertTrue(os.path.isdir(
            os.path.join(ROOT, "controller", "mcp_adapter")))
        self.assertTrue(os.path.isfile(
            os.path.join(ROOT, "controller", "mcp_adapter", "server.py")))

    def test_doc_env_vars_match_implementation(self):
        """文档列的环境变量必须是 server.py 真读的，且不能漏。"""
        with open(os.path.join(ROOT, "controller", "mcp_adapter", "server.py"),
                  "r", encoding="utf-8") as f:
            src = f.read()
        for var in ("QSHOU_HOST", "QSHOU_TOKEN", "QSHOU_TIMEOUT",
                    "QSHOU_SCHEMA_YAML"):
            self.assertIn(var, src, "实现里已不读 %s，文档该更新" % var)
            self.assertIn(var, self._doc(), "文档漏了 %s" % var)


class TestServerModuleHygiene(unittest.TestCase):
    """server.py 的模块级纪律：stdout 是 JSON-RPC 通道，碰不得。"""

    def _src(self):
        with open(os.path.join(ROOT, "controller", "mcp_adapter", "server.py"),
                  "r", encoding="utf-8") as f:
            return f.read()

    def test_module_level_stdout_writes_are_none(self):
        """🔴 模块级（顶层）不得有任何写 stdout 的代码。

        stdout 是 JSON-RPC 通道：多一个字节，握手成功后连接就被客户端判为
        协议错误而断开 —— MCP 生态里最常见的坑，且服务端日志一切正常，
        极难定位。（顶层 import 阶段的输出尤其致命：连握手都发不出去。）

        `--list-tools` 分支里的 print 是允许的 —— 那条路径只做一次就退出，
        根本不进 MCP 循环。
        """
        src = self._src()
        top = src.split("def log(", 1)[0]
        for i, line in enumerate(top.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or \
               stripped.startswith("*"):
                continue
            self.assertNotIn("print(", line,
                             "server.py 顶层第 %d 行有 print：%s —— 顶层 print "
                             "会在 MCP 握手前污染 stdout" % (i, stripped[:60]))
        self.assertIn("file=sys.stderr", src,
                      "日志 helper 必须显式写 stderr")

    def test_log_helper_writes_to_stderr(self):
        self.assertIn("file=sys.stderr", self._src(),
                      "日志必须走 stderr，否则污染 JSON-RPC 流")


if __name__ == "__main__":
    unittest.main(verbosity=2)