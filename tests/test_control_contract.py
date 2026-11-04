"""tests/test_control_contract.py — P5：控制面（Agent 接入）契约守卫。

背景：P0–P4 把设备从「只会发 HID」扩成「可被程序/AI Agent 驱动」，
控制面一口气多了 14 个端点。这份测试是那一片的**唯一自动化闸门**，
因为三次真机烧写已经证明这一层的错误**编译期和 host 测试全都测不到**：

  ① 端点只改了 routes.c 的 dispatch、忘了加进 platform_net.c 的 ROUTES[]
     → 真机 404，错误文案是 esp_http_server 的 "Nothing matches the given URI"，
       看起来像「设备没烧成功 / 网络不通」，极易把排查方向带偏。
  ② `/api/v1/gpio/{pin}` 依赖 `cfg.uri_match_fn = httpd_uri_match_wildcard`；
     不设它则默认 simple 匹配，所有带引脚号的请求 404 —— 同上症状。
  ③ POST /gpio/{pin} 在 dispatch 里被漏掉（只加了 GET 分支）→ 真机 404。

同时本文件钉死两份**人工同步**的清单不许漂移：
  * shared/api_schema.yaml  ↔  routes.c::handle_schema()（设备自描述 JSON）
    两边端点集合必须完全相等。Agent 只学 schema，schema 少一个端点
    就等于该能力对 Agent 不存在；而两份手写清单必然会漂。
  * esp32 接缝 ↔ host 桩：签名与语义必须一致，否则 host 测试的绿灯是假的。

覆盖分组：
  TestControlEndpointRegistration   ROUTES[] / dispatch / 通配 / handler 容量
  TestSelfDescribingSchema          api_schema.yaml ↔ handle_schema 双向一致
  TestControlAuthHook               公开/受控端点划分与实际鉴权行为
  TestGpioSafetyContract            安全白名单与 MSPI/相机硬守卫
  TestSeamParity                    esp32/host 两接缝一致性
  TestWebsocketEventChannel         WS 注册形态与时序铁律
"""
import json
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("shared", "controller"):
    _p = os.path.join(_ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402

from test_schema import strip_c_comments  # noqa: E402  复用同一套注释剥离器

_SCHEMA_PATH = os.path.join(_ROOT, "shared", "api_schema.yaml")
_ROUTES_C = os.path.join(_ROOT, "firmware", "main", "routes.c")
_NET_C = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_net.c")
_GPIO_ESP32 = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_gpio.c")
_GPIO_HOST = os.path.join(_ROOT, "firmware", "platform", "host", "platform_gpio.c")
_PLATFORM_H = os.path.join(_ROOT, "firmware", "platform", "platform.h")

BASE = "/api/v1"

# 设备自描述（routes.c::handle_schema）里声明的全部端点。
# 🔴 这是「设备当前能自报的能力」的快照，改动 routes.c 时**必须同步改这里**。
DEVICE_SELF_DESCRIBED = {
    "/health", "/sysinfo", "/schema", "/settings", "/led", "/tap", "/swipe",
    "/gesture", "/key", "/calibrate/point", "/calibrate/commit", "/camera",
    "/wifi/status", "/wifi/scan", "/wifi/connect", "/wifi/disconnect",
    "/ble/status", "/ble/advertise", "/ble/disconnect", "/ble/bonds",
    "/hid", "/hid/mouse", "/hid/keyboard", "/hid/consumer", "/gpio",
    "/gpio/{pin}", "/events",
}

# 免鉴权的公开端点（routes.c 里在 require_auth 之前 return 的那些）。
PUBLIC_ENDPOINTS = {"/health", "/sysinfo", "/settings", "/schema", "/led"}

# 🔴 本板安全可管理 GPIO（复核后的裁定，非用户原始白名单）。
# 依据见 shared/api_schema.yaml 的 /gpio note：26–37 属 Octal PSRAM/MSPI 硬连线，
# 4/5/16/17/18 属 OV2640 相机总线。碰这些脚 gpio_config() 仍返回成功，
# 但会把脚从总线上摘掉 → 事后随机崩溃，只能查证不能试探。
GPIO_SAFE_SET = {19, 21, 22, 23, 25}
GPIO_CAMERA_PINS = {4, 5, 16, 17, 18}
GPIO_MSPI_RANGE = range(26, 38)


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _norm(path):
    """schema 路径 → ROUTES[] 注册串。

    `/gpio/{pin}` 在 httpd 里注册为通配 `/api/v1/gpio/*`（ESP-IDF 用 `*` 后缀
    作前缀通配，见 platform_net.c 注释），故归一化时把 {pin} 换成 `*`。
    """
    p = path[len(BASE):] if path.startswith(BASE) else path
    return p.replace("{pin}", "*")


def _schema():
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _schema_json_literal():
    """从 handle_schema 的 C 源码里还原真实 JSON 串。

    ⚠️ 不能直接对源码套 `"path":"..."`：JSON 写在 C 字符串里，引号被反斜杠转义、
    相邻字面量在编译期拼接。必须先取字面量→拼接→反转义。
    ⚠️ 拼接结果末尾还挂着 snprintf 的**参数**字面量（`g_control_auth ? "true" : "false"`），
    直接 json.loads 会报 "Extra data"。故按花括号配平截到第一个 depth==0 的 `}`。
    """
    src = strip_c_comments(_read(_ROUTES_C))
    m = re.search(r"static int handle_schema\(.*?\n\}", src, re.S)
    if not m:
        raise AssertionError("找不到 handle_schema 函数体")
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(0))
    raw = "".join(lits).replace('\\"', '"').replace("\\\\", "\\")
    # 填充 snprintf 的唯一占位符（auth.required = g_control_auth 三元式的 %s）。
    # 不填则 JSON 非法 —— 反而帮我们确认字面量里没有别的裸占位符。
    raw = raw.replace("%s", "false")
    depth, i = 0, 0
    while i < len(raw):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return raw[:i + 1]


def _schema_max_len():
    """最坏情况下的响应长度：把 %s 按最长替换（"true" = 4 字符）。

    snprintf 返回的是**本应写入**的长度（不含 NUL），故只要 ≥ cap，
    handle_schema 就走 `return 500`。本函数当前只有 %s 一个占位符。
    """
    return len(_schema_json_literal().replace("%s", "true"))


class _NetSource(unittest.TestCase):
    """共用 platform_net.c 的解析工具。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(_NET_C)

    def routes_table(self):
        """ROUTES[] 里注册的 URI（含通配条目）。"""
        m = re.search(r"ROUTES\[\]\s*=\s*\{(.*?)\n\};", self.src, re.S)
        self.assertIsNotNone(m, "在 %s 里找不到 ROUTES[] 定义" % _NET_C)
        return set(re.findall(r'"(/api/v1/[^"]+)"', m.group(1)))


class TestControlEndpointRegistration(_NetSource):
    """端点必须同时出现在 httpd 路由表与 routes.c dispatch 中。"""

    def _dispatch_src(self):
        return strip_c_comments(_read(_ROUTES_C))

    def test_every_control_endpoint_registered_in_httpd(self):
        """schema 里 owner=firmware 的端点都必须在 ROUTES[] 中注册。

        漏注册的典型症状：真机 404 + 文案 "Nothing matches the given URI"
        （esp_http_server 的 URI 匹配器抛的，请求根本没进 routes_handle）。
        `/events` 例外：它必须用 HTTP_GET + is_websocket 单独注册，
        放进 ROUTES[] 的通用 HTTP_ANY 桥接会让 WS 握手永远不被处理。
        """
        registered = self.routes_table()
        reg_norm = {_norm(p) for p in registered}
        missing = []
        for e in _schema()["endpoints"]:
            if e.get("owner") != "firmware":
                continue
            if e["path"] == "/events":
                continue
            if _norm(e["path"]) not in reg_norm:
                missing.append(e["path"])
        self.assertEqual(missing, [],
                         "这些固件端点未注册进 platform_net.c 的 ROUTES[] —— "
                         "真机 404（httpd 层拦截，routes.c 的 dispatch 根本不会被调用）")

    def test_events_is_not_in_generic_routes_table(self):
        """🔴 /events 绝不能进通用 ROUTES[]。

        ROUTES[] 循环用 HTTP_ANY + http_bridge 统一注册；WS 端点走这条路时，
        握手不会被处理（httpd 认不出 Upgrade），表现为「GET /events 正常，
        但 WebSocket 连不上 / 收到非 101 响应」。
        """
        self.assertNotIn(BASE + "/events", self.routes_table(),
                         "/events 必须在 ROUTES[] 之外，用 HTTP_GET + is_websocket "
                         "单独注册（见 platform_net.c::events_ws_handler 附近）")

    def test_every_registered_route_dispatched(self):
        """ROUTES[] 每条都必须在 routes.c 的 dispatch 里被处理。

        必须剥注释：只在注释里提一句端点名不算「被处理」，否则守卫给假绿灯。
        """
        src = self._dispatch_src()
        for path in sorted(self.routes_table()):
            self.assertIn(path, src,
                          "%s 在 ROUTES[] 注册了，但 routes.c 没有处理它" % path)

    def test_post_gpio_pin_dispatch_present(self):
        """🔴 POST /gpio/{pin} 必须有独立 dispatch（历史真实漏项）。

        历史：P1 只在 GET 分支加了 GPIO_PIN_ROUTE 匹配，POST 请求落到
        routes_handle 末尾 `return 404`。compile + 单测全绿（都只静态扫源码），
        真机才发现 —— 且症状是 404，与「设备没烧成功」无法区分。
        """
        src = self._dispatch_src()
        # 抽取 M_POST 分支的函数体（从 `if (method == M_POST) {` 到 return 404 前）
        m = re.search(r"if\s*\(\s*method\s*==\s*M_POST\s*\)\s*\{(.*?)\n    \}\n    return 404",
                      src, re.S)
        self.assertIsNotNone(m, "找不到 routes_handle 的 M_POST 分支")
        body = m.group(1)
        self.assertRegex(
            body, r"GPIO_PIN_ROUTE",
            "POST 分支缺少 GPIO_PIN_ROUTE 匹配 —— POST /api/v1/gpio/{pin} 会落到 "
            "末尾 return 404（真机才发现，编译与单测都测不到）")

    def test_gpio_pin_registered_as_wildcard(self):
        """/gpio/{pin} 必须注册为通配 `/api/v1/gpio/*`，且 httpd 开启通配匹配。

        两个都是 P1 真机 404 的直接成因：
          * 注册成精确 URI → 引脚号对不上，URI 匹配器直接拒绝；
          * 没设 uri_match_fn → 默认 httpd_uri_match_simple，纯精确比较，
            `*` 被当字面符。
        """
        self.assertIn(BASE + "/gpio/*", self.routes_table(),
                      "/gpio/{pin} 必须以通配 %s/gpio/* 注册" % BASE)
        self.assertRegex(
            self.src, r"cfg\.uri_match_fn\s*=\s*httpd_uri_match_wildcard",
            "httpd 未启用通配 URI 匹配：不设它则默认 httpd_uri_match_simple 纯精确"
            "比较，所有带引脚号的 GPIO 请求 404")

    def test_max_uri_handlers_capacity(self):
        """max_uri_handlers 必须装得下全部 handler（含 ROUTES 之外的 4 个）。"""
        m = re.search(r"cfg\.max_uri_handlers\s*=\s*(\d+)", self.src)
        self.assertIsNotNone(m, "找不到 cfg.max_uri_handlers 设置")
        cap = int(m.group(1))
        needed = len(self.routes_table()) + 4   # + / + /stream + /snapshot + /events
        self.assertGreaterEqual(cap, needed,
                                "max_uri_handlers=%d 装不下 %d 个 handler（加端点时"
                                "须同步调大，否则注册失败表现为端点 404）" % (cap, needed))

    def test_runtime_handler_clamp_has_floor(self):
        """NVS 配的 max_uri 不能低到装不下路由（运行时钳制须有下限）。"""
        m = re.search(r"size_t\s+need\s*=\s*[^;]+;", self.src)
        self.assertIsNotNone(m, "找不到 need 计算（应 = ROUTES 数 + 额外端点）")
        self.assertRegex(
            self.src, r"if\s*\(\s*v\s*<\s*need\s*\)\s*v\s*=\s*need",
            "NVS 配置的 max_uri_handlers 没有 need 下限保护 —— 用户配成小值时"
            "httpd 起不来，全站 404")


class TestSelfDescribingSchema(unittest.TestCase):
    """api_schema.yaml ↔ routes.c::handle_schema 双向一致。"""

    @classmethod
    def setUpClass(cls):
        cls.routes_c = strip_c_comments(_read(_ROUTES_C))

    def _handle_schema_endpoints(self):
        """从 handle_schema 的 JSON 字面量里解析出端点 path 集合。"""
        return json.loads(_schema_json_literal())["endpoints"]

    def test_schema_json_literal_is_wellformed(self):
        """handle_schema 的 JSON 字面量必须可被 json 解析。

        少一个引号/逗号 → 真机返回一段非法 JSON，Agent 侧表现为
        「schema 拉取失败，所有工具都没注册」，而不是任何本地报错。
        """
        try:
            doc = json.loads(_schema_json_literal())
        except ValueError as e:
            self.fail("handle_schema 的 JSON 字面量非法：%s" % e)
        self.assertTrue(doc.get("ok"))
        self.assertEqual(doc.get("base"), BASE)

    def test_schema_response_fits_resp_cap(self):
        """🔴🔴 handle_schema 的响应必须装得进 RESP_CAP，否则真机返回 500。

        这是本轮新发现的真 bug（P0 以来一直存在）：
          snprintf(resp, cap, ...) 在装不下时返回**本应写入的长度**（含截断语义），
          而 handle_schema 的收尾是 `return (n < 0 || (size_t)n >= cap) ? 500 : 200;`
          → 2123 > 2048 判定成立 → **GET /api/v1/schema 恒返回 500**。
          后果：Agent 唯一的自描述入口不可用，整个 P0–P6 集成方案失去地基，
          而症状是「Agent 拉 schema 失败」，极易被误判为网络/适配器问题。

          ⚠️ 这类错误编译期测不到（只是字符串拼接）、host 测试也测不到
             （host 走 stub），只有「按架构上限算字节数」才拦得住 ——
             与 TestSysinfoContract.test_response_cap_fits_worst_case 同一思路。
        """
        m = re.search(r"#define\s+RESP_CAP\s+(\d+)", _read(_NET_C))
        self.assertIsNotNone(m, "找不到 RESP_CAP 定义")
        cap = int(m.group(1))
        worst = _schema_max_len()
        self.assertLess(
            worst, cap,
            "handle_schema 最坏响应 %d 字节 ≥ RESP_CAP=%d → snprintf 返回值 >= cap "
            "→ 恒 500（真机 GET /api/v1/schema 完全不可用）。修法：精简 desc 文案，"
            "或调大 RESP_CAP（后者需评估 NVS 内存占用）" % (worst, cap))

    def test_device_self_description_matches_snapshot(self):
        """设备自描述的端点集合 == DEVICE_SELF_DESCRIBED 快照。

        这条红了说明 routes.c 改了端点清单：要么补快照，要么补端点实现。
        """
        got = {e["path"] for e in self._handle_schema_endpoints()}
        self.assertEqual(
            got, DEVICE_SELF_DESCRIBED,
            "routes.c::handle_schema 的端点清单变了。改了就要同步：\n"
            "  1) 本文件 DEVICE_SELF_DESCRIBED\n"
            "  2) shared/api_schema.yaml\n"
            "  3) 若新增端点：platform_net.c 的 ROUTES[] + max_uri_handlers")

    def test_schema_yaml_matches_device_self_description(self):
        """🔴 核心：YAML 契约与设备自描述必须完全一致（双向）。

        Agent 只学 schema。两份手写清单必然会漂，故强制相等：
          - YAML 有、设备没有 → Agent 注册了调不通的工具；
          - 设备有、YAML 没有 → 该能力对 Agent 隐形（如实现在但没人知道）。
        `/schema` 是自描述端点自身，**两侧都豁免**（设备无需在自己的 endpoints
        数组里列自己；它的存在由 test_schema_endpoint_itself_is_registered 覆盖）。
        """
        dev = {e["path"] for e in self._handle_schema_endpoints()} - {"/schema"}
        yaml_fw = {e["path"] for e in _schema()["endpoints"]
                   if e.get("owner") == "firmware"} - {"/schema"}
        self.assertEqual(
            yaml_fw - dev, set(),
            "YAML 里声明但设备自描述没有的固件端点：Agent 会注册调不通的工具")
        self.assertEqual(
            dev - yaml_fw, set(),
            "设备自描述有但 YAML 未登记的端点：该能力对 Agent 隐形，"
            "补 shared/api_schema.yaml")

    def test_no_yaml_boolean_key_traps(self):
        """🔴 YAML 裸键 `on` / `off` / `yes` / `no` 会被解析成布尔键。

        本项目真实踩过：`/ble/advertise` 的 `on:` 裸键被 YAML 解析成 `True`，
        于是「Agent 无从得知该参数存在」——而 YAML 本身**看起来完全正常**，
        只有与设备自描述交叉比对才暴露。所有 request/query 键必须是字符串。

        同理 `method`/`path` 等元字段也不能被布尔化。
        """
        doc = _schema()
        problems = []
        for e in doc["endpoints"]:
            for section in ("request", "query", "response"):
                for k in (e.get(section) or {}):
                    if not isinstance(k, str):
                        problems.append("%s.%s 的键 %r 被 YAML 解析成 %s"
                                        % (e["path"], section, k, type(k).__name__))
        self.assertEqual(problems, [],
                         "YAML 裸键被隐式转成布尔/数字（on/off/yes/no/y/n）：%s —— "
                         "加引号即可" % problems)

    def test_schema_endpoint_itself_is_registered(self):
        """`/schema` 豁免于上面的双向比对，但必须在 ROUTES[] 与 YAML 里都在。"""
        self.assertIn("/schema", _schema()["endpoints"] and
                      {e["path"] for e in _schema()["endpoints"]},
                      "YAML 未登记 /schema（Agent 的自描述入口本身）")
        net = strip_c_comments(_read(_NET_C))
        self.assertIn('"%s/schema"' % BASE, net,
                      "/api/v1/schema 未注册进 ROUTES[]")

    def test_schema_yaml_params_match_device_self_description(self):
        """YAML 是详版（可含冗余坐标字段），但**不能少**设备自描述声明的参数。

        方向性很关键：
          - YAML 多的字段（如 /tap 的 x/y/duration_ms —— 相对鼠标下固件忽略）
            是**冗余记录**，允许存在，且必须在 note 里说明被忽略；
          - YAML 少的字段 = Agent 不知道这个参数存在 → 该参数没法用。
        故只断言 `dev[p] - declared == set()`（不允许缺），反向不管。
        """
        dev = {e["path"]: set(e.get("params", []))
               for e in self._handle_schema_endpoints()}
        for e in _schema()["endpoints"]:
            p = e["path"]
            if p not in dev:
                continue
            declared = set((e.get("request") or {}).keys()) | set(
                (e.get("query") or {}).keys())
            self.assertEqual(
                dev[p] - declared, set(),
                "%s 的参数在设备自描述里声明了、YAML 却没写：%s（Agent 无从得知该参数存在）"
                % (p, sorted(dev[p] - declared)))

    def test_no_controller_only_params_left_in_firmware_request(self):
        """🔴 反向：固件端点的 request/query 键不得超出设备自描述。

        为什么必须反向（与上一条方向相反、不可合并）：
        上一条允许 YAML 多字段，是为记录「控制器侧坐标语义」；但 Agent 照
        YAML 生成入参时，多出来的键会被**静默忽略**（固件只解析自己的字段），
        表现为「调用成功但没效果」——比少字段更难查。2026-10-13 真机核对
        就是这样抓到 /swipe 仍列着 x0/y0/x1/y1/duration_ms，而
        routes.c::handle_swipe 只读 hid_report_start/end。

        出口：真不打算让设备解析的键（如控制端内部语义）从 request 移走，
        改放 note；确需保留的必须登记进 `ignored_params` 并在 note 里说明
        被忽略，形成一份有账可查的例外清单，而不是隐性冗余。
        """
        dev = {e["path"]: set(e.get("params", []))
               for e in self._handle_schema_endpoints()}
        for e in _schema()["endpoints"]:
            if e.get("owner") != "firmware":
                continue
            p = e["path"]
            if p not in dev:
                continue
            declared = set((e.get("request") or {}).keys()) | set(
                (e.get("query") or {}).keys())
            extra = declared - dev[p]
            allowed = set(e.get("ignored_params") or [])
            self.assertEqual(
                extra - allowed, set(),
                "%s 的 request 里有设备不解析的字段：%s —— Agent 会生成这些参数，"
                "固件静默忽略 → 「调用成功但没效果」。请移出 request 并在 note 说明，"
                "或显式登记进 ignored_params" % (p, sorted(extra - allowed)))
            # 登记为例外就必须解释，否则例外清单本身会腐化
            for k in allowed:
                self.assertIn(k, declared,
                              "%s 的 ignored_params 登记了不存在的字段 %r" % (p, k))
            if allowed:
                self.assertIn("忽略", str(e.get("note", "")),
                              "%s 登记了 ignored_params=%s，note 里必须写明设备会忽略它们"
                              % (p, sorted(allowed)))

    def test_auth_scheme_matches_implementation(self):
        """YAML 的 auth 段必须与固件真实读取方式一致。

        历史坑：YAML 一直写自定义头 X-Auth-Token，而 routes.c 从来只读
        Authorization（且必须 `Bearer ` 前缀）——照 YAML 实现的调用方必然 403。

        ⚠️ 只断言**固件侧**没有读这个头：YAML 的 note 里保留该名字作为
        「历史错误、勿再使用」的记录是有价值的，禁掉它反而丢失上下文。
        """
        auth = _schema()["auth"]
        self.assertEqual(auth["header"], "Authorization",
                         "固件只从 Authorization 读头（platform_net.c::http_bridge "
                         "的 httpd_req_get_hdr_value_str(req,\"Authorization\",...)）")
        self.assertEqual(auth["scheme"], "Bearer")
        self.assertIn("Bearer ", _read(_ROUTES_C),
                      "routes.c 应按 `Bearer <token>` 前缀校验")
        for name, path in (("routes.c", _ROUTES_C),
                           ("platform_net.c", _NET_C)):
            self.assertNotIn(
                "X-Auth-Token", strip_c_comments(_read(path)),
                "%s 不该读从未实现的自定义头 X-Auth-Token" % name)

    def test_public_endpoint_classification_matches_code(self):
        """YAML 的 auth: public/control 划分必须与 routes.c 的实际分支一致。"""
        for e in _schema()["endpoints"]:
            p, kind = e["path"], e.get("auth")
            self.assertIn(kind, ("public", "control"),
                          "%s 缺 auth 分类（public / control）" % p)
            if e.get("owner") != "firmware":
                continue
            if kind == "public":
                self.assertIn(p, PUBLIC_ENDPOINTS,
                              "%s 标成 public 但不在公开集合里" % p)
            else:
                self.assertNotIn(p, PUBLIC_ENDPOINTS,
                                 "%s 标成 control 却是公开端点" % p)


class TestControlAuthHook(unittest.TestCase):
    """require_auth 钩子的位置与行为。"""

    @classmethod
    def setUpClass(cls):
        cls.src = strip_c_comments(_read(_ROUTES_C))

    def test_public_endpoints_dispatched_before_auth_gate(self):
        """🔴 所有公开端点必须在 require_auth 之前 return。

        位置错了的后果：g_control_auth=false 时看不出问题（钩子直接放行），
        一旦开启鉴权，Agent 连 /schema 都拉不到 → 无法自描述 → 全盘失效。

        ⚠️ 匹配要同时接受 strcmp 与 strncmp 前缀形式：`/led` 带可选 query
        （?io2=...），源码用的是 `strncmp(path, "/api/v1/led", 11)`，
        写死 strcmp 会给出假红。
        """
        gate = self.src.index("require_auth(auth)")
        head = self.src[:gate]
        for p in sorted(PUBLIC_ENDPOINTS):
            literal = '"%s%s"' % (BASE, p)
            self.assertTrue(
                any(("%s%s" % (fn, literal)) in head
                    for fn in ("strcmp(path, ", "strncmp(path, ")),
                "%s 是公开端点，但它的 dispatch 不在鉴权门之前" % p)

    def test_auth_gate_returns_403_with_json_body(self):
        """鉴权失败必须回 403 + JSON（不是空 body）。"""
        m = re.search(r"if\s*\(\s*auth_rc\s*!=\s*0\s*\)\s*\{(.*?)\n    \}",
                      self.src, re.S)
        self.assertIsNotNone(m, "找不到鉴权失败分支")
        body = m.group(1)
        self.assertIn("unauthorized", body, "403 响应体应含 error=unauthorized")
        self.assertRegex(body, r"return\s+auth_rc",
                         "403 分支必须返回鉴权钩子给出的状态码，不能固定返回 200")

    def test_auth_disabled_by_default(self):
        """🔴 g_control_auth 默认必须为 false。

        这是个「上市阻断级」开关（当前固件零认证），但默认打开会让所有
        未适配的客户端（含设备自带 ui.html）立刻 403。
        """
        self.assertRegex(
            self.src, r"static\s+bool\s+g_control_auth\s*=\s*false",
            "g_control_auth 默认必须是 false（LAN 内使用）；对外暴露前须完成 "
            "鉴权 + HTTPS/VLAN 改造")

    def test_authorization_header_forwarded_by_bridge(self):
        """platform_net.c 必须把 Authorization 头透传给 routes_handle。

        漏了的话 require_auth 永远收到 NULL → 开启鉴权后全部 403，
        且日志里看不出原因。
        """
        net = strip_c_comments(_read(_NET_C))
        self.assertRegex(net, r'httpd_req_get_hdr_value_str\(\s*req\s*,\s*"Authorization"',
                         "http_bridge 未读取 Authorization 头")
        self.assertRegex(net, r"routes_handle\(\s*\w+\s*,\s*\w+->uri\s*,\s*\w+\s*,\s*auth\s*,",
                         "http_bridge 调用 routes_handle 时未把 auth 透传")


class TestGpioSafetyContract(unittest.TestCase):
    """🔴 GPIO 安全白名单 —— 本项目最贵的一个坑的回归闸门。

    为什么必须钉死：`gpio_config()` 只在 pad 级改 IOMUX，对 MSPI/PSRAM 脚
    **也会返回成功**，编译通过、单测全绿、真机读写测试全绿 —— 但它把脚从
    PSRAM 总线上摘掉了，事后任何 PSRAM 访问都可能数据损坏或随机崩溃。
    这类错误**只能靠查两个真源发现**，绝不能靠运行时试探：
        1) sdkconfig: CONFIG_SPIRAM_MODE_OCT=y（本板 PSRAM 是八线）
        2) IDF soc/spi_pins.h: MSPI 固定引脚 CS1=26 HD=27 WP=28 CS0=29 CLK=30
           MISO=31 MOSI=32 D4=33 D5=34 D6=35 D7=36 DQS=37（硬连线，不可映射）
    """

    @classmethod
    def setUpClass(cls):
        cls.esp = strip_c_comments(_read(_GPIO_ESP32))
        cls.host = strip_c_comments(_read(_GPIO_HOST))

    def _safe_set(self, src):
        m = re.search(r"GPIO_SAFE\[\]\s*=\s*\{([^}]*)\}", src)
        self.assertIsNotNone(m, "找不到 GPIO_SAFE[] 定义")
        return {int(x) for x in re.findall(r"\d+", m.group(1))}

    def test_both_seams_share_the_same_safe_set(self):
        """esp32 与 host 白名单必须一致，否则 host 测试的绿灯是假的。"""
        self.assertEqual(self._safe_set(self.esp), self._safe_set(self.host),
                         "esp32 与 host 的 GPIO_SAFE 不一致 —— host 桩会掩盖真机问题")
        self.assertEqual(self._safe_set(self.esp), GPIO_SAFE_SET,
                         "GPIO 安全集漂移。变更前必须先查 soc/spi_pins.h（MSPI 固定"
                         "引脚）与相机占用脚，并把 shared/api_schema.yaml 的 /gpio "
                         "note 一并更新")

    def test_safe_set_excludes_mspi_and_camera_pins(self):
        """白名单不得含 MSPI/PSRAM（26–37）或相机（4/5/16/17/18）引脚。"""
        safe = self._safe_set(self.esp)
        self.assertEqual(safe & set(GPIO_MSPI_RANGE), set(),
                         "白名单含 Octal PSRAM/MSPI 引脚：gpio_config 会成功返回，"
                         "但脚已从 PSRAM 总线摘掉 → 事后随机崩溃/数据损坏")
        self.assertEqual(safe & GPIO_CAMERA_PINS, set(),
                         "白名单含 OV2640 相机总线引脚：接管会直接崩相机")

    def test_mspi_and_camera_hard_guards_exist(self):
        """纵深防御：即便有人误扩 GPIO_SAFE，入口也必须硬拒危险脚。"""
        for name, src in (("esp32", self.esp), ("host", self.host)):
            self.assertRegex(src, r"gpio_is_mspi|host_gpio_is_mspi",
                             "%s 接缝缺少 MSPI 引脚判定函数" % name)
            self.assertRegex(src, r"gpio_is_camera|host_gpio_is_camera",
                             "%s 接缝缺少相机引脚判定函数" % name)
            self.assertRegex(
                src, r"gpio_pin_allowed|gpio_allowed",
                "%s 接缝缺少统一的引脚放行判定" % name)

    def test_mspi_guard_covers_full_26_37_range(self):
        """MSPI 守卫必须覆盖 26..37 全段（不是只挡 26/27/33/34/35）。"""
        for name, src in (("esp32", self.esp), ("host", self.host)):
            m = re.search(r"(?:gpio_is_mspi|host_gpio_is_mspi)\s*\(\s*int\s+pin\s*\)\s*"
                          r"(?:\{[^}]*\}|return\s+[^;]+;)", src)
            self.assertIsNotNone(m, "%s 接缝找不到 MSPI 判定实现" % name)
            self.assertRegex(
                m.group(0), r"26", "%s 的 MSPI 判定未提到下界 26" % name)
            self.assertRegex(
                m.group(0), r"37", "%s 的 MSPI 判定未提到上界 37" % name)

    def test_mode_set_entry_rejects_disallowed_pins(self):
        """模式设置入口必须先过放行判定（否则白名单形同虚设）。"""
        for name, src in (("esp32", self.esp), ("host", self.host)):
            m = re.search(r"int\s+platform_gpio_pin_mode\s*\([^)]*\)\s*\{(.*?)\n\}",
                          src, re.S)
            self.assertIsNotNone(m, "%s 接缝找不到 platform_gpio_pin_mode" % name)
            self.assertRegex(
                m.group(1), r"gpio_pin_allowed|gpio_allowed",
                "%s 的 pin_mode 入口未做放行校验" % name)

    def test_write_and_pwm_also_reject_disallowed_pins(self):
        """写值/PWM 入口也必须校验，不能只靠「先设 mode 时拦一道」。"""
        for fn in ("platform_gpio_write", "platform_gpio_pwm"):
            for name, src in (("esp32", self.esp), ("host", self.host)):
                m = re.search(r"int\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % fn, src, re.S)
                self.assertIsNotNone(m, "%s 接缝找不到 %s" % (name, fn))
                self.assertRegex(
                    m.group(1), r"gpio_pin_allowed|gpio_allowed|state_idx",
                    "%s 的 %s 未校验引脚可管理性" % (name, fn))

    def test_schema_documents_the_safe_set(self):
        """schema 必须写明这 5 个脚及其「为何不是用户白名单」。"""
        gpio = [e for e in _schema()["endpoints"] if e["path"] == "/gpio"][0]
        note = str(gpio.get("note", ""))
        for token in ("19", "21", "22", "23", "25", "PSRAM", "MSPI", "相机"):
            self.assertIn(token, note,
                          "/gpio 的 note 未说明安全集与占用来源（缺 %r）" % token)


class TestSeamParity(unittest.TestCase):
    """esp32 接缝与 host 桩必须都实现、签名一致。"""

    _CONTROL_FNS = (
        "platform_gpio_list", "platform_gpio_get", "platform_gpio_pin_mode",
        "platform_gpio_write", "platform_gpio_pwm", "platform_gpio_read",
        "platform_gpio_pwm_set_freq",
        "platform_wifi_connect", "platform_wifi_scan",
        "platform_wifi_scan_results", "platform_wifi_disconnect",
        "platform_wifi_ip",
        "platform_ble_advertise", "platform_ble_disconnect",
        "platform_ble_list_bonds", "platform_ble_forget_bond",
        "platform_ble_is_advertising",
        "platform_events_broadcast", "platform_events_subscriber_count",
    )

    _IMPL = {
        "platform_gpio": "platform_gpio.c",
        "platform_wifi_connect": "platform_net.c",
        "platform_wifi_scan": "platform_net.c",
        "platform_wifi_scan_results": "platform_net.c",
        "platform_wifi_disconnect": "platform_net.c",
        "platform_wifi_ip": "platform_net.c",
        "platform_ble_advertise": "platform_ble.c",
        "platform_ble_disconnect": "platform_ble.c",
        "platform_ble_list_bonds": "platform_ble.c",
        "platform_ble_forget_bond": "platform_ble.c",
        "platform_ble_is_advertising": "platform_ble.c",
    }

    def _files(self, side):
        base = os.path.join(_ROOT, "firmware", "platform", side)
        out = {}
        for fn in os.listdir(base):
            if fn.endswith(".c"):
                out[fn] = strip_c_comments(_read(os.path.join(base, fn)))
        return out

    def test_control_seam_implemented_on_both_sides(self):
        """每个控制接缝函数在 esp32 与 host 两侧都必须有实现。

        漏 host 侧 → host 构建链接失败（好）；
        漏 esp32 侧 → 链接失败（好）；
        两边都漏 → 只在真机链接时才炸（坏）。
        """
        esp, host = self._files("esp32"), self._files("host")
        for fn in self._CONTROL_FNS:
            self.assertTrue(
                any(fn in s for s in esp.values()),
                "esp32 接缝缺少 %s 的实现" % fn)
            self.assertTrue(
                any(fn in s for s in host.values()),
                "host 桩缺少 %s（会导致 host 测试链接失败）" % fn)

    def test_control_seam_declared_in_header(self):
        """platform.h 必须声明全部控制接缝函数（routes.c 只认头文件）。"""
        hdr = strip_c_comments(_read(_PLATFORM_H))
        for fn in self._CONTROL_FNS:
            self.assertRegex(hdr, r"\b%s\s*\(" % fn,
                             "platform.h 未声明 %s" % fn)

    def test_gpio_mode_enum_name_avoids_idf_collision(self):
        """🔴 枚举必须叫 gpio_mode_e，不能是 gpio_mode_t。

        ESP-IDF 的 esp_hal_gpio/include/hal/gpio_types.h 已定义 `gpio_mode_t`，
        platform.h 重定义同名类型会编译报 conflicting types —— 且这个错只在
        真机编译时才暴露（host 环境没有 IDF 头）。
        """
        hdr = _read(_PLATFORM_H)
        self.assertIn("gpio_mode_e", hdr)
        self.assertNotRegex(hdr, r"typedef enum\s*\{[^}]*\}\s*gpio_mode_t\s*;",
                            "platform.h 又用了 gpio_mode_t —— 与 ESP-IDF 的 "
                            "gpio_types.h 冲突，真机编译失败")

    def test_routes_calls_seam_not_driver_directly(self):
        """routes.c 只做编排：不得直接调 IDF 驱动（如 gpio_config）。"""
        src = strip_c_comments(_read(_ROUTES_C))
        for banned in ("gpio_config(", "ledc_", "esp_wifi_connect(",
                       "ble_gap_adv_stop(", "nimble_port_"):
            self.assertNotIn(banned, src,
                             "routes.c 直接碰驱动 %s —— 必须经 platform.h 接缝，"
                             "否则 host 桩无法替代" % banned)


class TestWebsocketEventChannel(unittest.TestCase):
    """P4 WebSocket 事件通道的注册形态与时序铁律。"""

    @classmethod
    def setUpClass(cls):
        cls.net = strip_c_comments(_read(_NET_C))
        cls.routes_c = strip_c_comments(_read(_ROUTES_C))

    def _events_registration(self):
        """抽出 events_ws_handler 的 httpd_uri_t 注册项文本。

        ⚠️ 不能用「从 handler 往前取 N 字符窗口」——窗口会跨到别的注册项上
        （变异测试实测：把 HTTP_GET 改成 HTTP_ANY 后，窗口里仍能捞到别处的
        HTTP_GET 文本 → 守卫假绿）。必须**定位到最近一个 `{` 起始的初始化器**，
        且不许跨过它。
        """
        i = self.net.rindex("events_ws_handler")
        start = self.net.rindex("{", 0, i)
        # 初始化器以 `};` 结束
        end = self.net.index("};", i) + 2
        return self.net[start:end]

    def test_ws_handler_registered_with_is_websocket(self):
        """必须 HTTP_GET + is_websocket=true 单独注册。"""
        block = self._events_registration()
        self.assertRegex(block, r"\.method\s*=\s*HTTP_GET\b(?![\w])",
                         "/events 必须用 HTTP_GET 注册（HTTP_ANY 走通用桥接，"
                         "握手不会被处理）")
        self.assertRegex(block, r"\.is_websocket\s*=\s*true",
                         "/events 注册项缺 is_websocket=true")
        self.assertRegex(block, r"\.handler\s*=\s*events_ws_handler",
                         "/events 的 handler 应指向 events_ws_handler")
        self.assertRegex(block, r"\.uri\s*=\s*\"/api/v1/events\"",
                         "/events 注册项的 uri 应为 /api/v1/events")

    def test_ws_control_frames_not_disabled(self):
        """handle_ws_control_frames 必须为 false，否则 PING/CLOSE 无人处理。"""
        self.assertRegex(
            self.net, r"\.handle_ws_control_frames\s*=\s*false",
            "handle_ws_control_frames 必须显式为 false（本实现自己在 handler 里"
            "识别 CLOSE；置 true 会让 httpd 抢先处理，控制逻辑不再生效）")

    def test_recv_frame_length_must_start_at_zero(self):
        """🔴 httpd_ws_recv_frame 的 frame.len 必须初始化为 0。

        这是 P4 最贵的坑：`httpd_ws_recv_frame_internal()` 的入口条件是
        `if (frame->len == 0) { 解析帧头/长度/mask key }`。预填非 0 会**跳过
        整个帧头解析**，直接把 socket 后续字节当 payload 读 max_len 个 →
        ① payload 是含 4 字节 mask key 的密文；② 多读几百字节破坏 TCP 流 →
        客户端报 "reserved bits must be 0" / "no close frame received" 而断开。
        """
        m = re.search(r"httpd_ws_frame_t\s+\w+\s*=\s*\{(.*?)\};", self.net, re.S)
        self.assertIsNotNone(m, "找不到 httpd_ws_frame_t 初始化")
        init = m.group(1)
        self.assertRegex(init, r"\.len\s*=\s*0\b",
                         "recv 前 frame.len 必须为 0（触发帧头解析）；非 0 会跳过"
                         "解析并读乱码、破坏 TCP 流。缓冲容量要传给 max_len 参数")
        self.assertRegex(init, r"\.type\s*=\s*HTTPD_WS_TYPE_TEXT",
                         "recv 帧类型应为 TEXT（当前实现只处理文本帧）")

    def test_first_frame_returns_status_as_handshake_ack(self):
        """首帧必须回 status：设备不会在握手后主动推帧（httpd 行为）。

        依赖这一点的是「握手确认」语义：客户端只握手不发帧就收不到任何东西。
        ⚠️ 源码里的 JSON 写在 C 字符串中 → 引号是 `\"` 转义形式，匹配时要按
        转义后的样子找（找裸 `"type"` 会永远匹配不上）。
        """
        m = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m, "找不到 events_ws_handler 函数体")
        body = m.group(1)
        self.assertIn('\\"type\\":\\"status\\"', body,
                      "收到文本帧必须回一条 status（兼作握手确认与心跳）")
        for field in ("ip", "rssi", "ble_connected", "subscribers", "echo"):
            self.assertIn('\\"%s\\"' % field, body,
                          "status 帧缺 %s 字段" % field)
        self.assertIn("httpd_ws_send_frame", body,
                      "status 必须经 httpd_ws_send_frame 以 WS 帧发出")

    def test_plain_get_detected_by_ws_session_state_not_subscription_table(self):
        """🔴 普通 GET 的判据必须是「这个 fd 现在是不是活跃 WS 会话」。

        曾经的错误写法是 `first_time = (ev_sub_find(fd) < 0)`（fd 未登记 → 当普通
        GET）。它错在 **fd 会被 lwIP 复用**：客户端被 kill / 断网时收不到 CLOSE
        帧，僵尸订阅条目一直留在表里；后来的普通 GET 恰好命中那个 fd 数值 →
        被误判成 WS 数据帧 → 不发响应直接关连接 → `GET /api/v1/events` 恒返回
        0 字节（真机复现，且只在「先连 WS 再断开」之后才暴露）。

        正确判据是公开 API httpd_ws_get_fd_info()，它直接问 httpd 内部的
        ws_handshake_done / ws_close 状态，与订阅表无关。
        """
        m = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m, "找不到 events_ws_handler 函数体")
        head = m.group(1)
        # 第一个分支就是「不是活跃 WS → 走普通 HTTP 响应」
        first = head[:head.index("httpd_ws_recv_frame")]
        # ⚠️ 必须**结构化**钉住 `!=`：只 assertIn("HTTPD_WS_CLIENT_WEBSOCKET")
        #   的话，把 != 改成 == 守卫照样绿 —— 而取反后语义完全反过来
        #   （WS 会话去发 HTTP 响应、普通请求去收帧），是真实的失效模式。
        self.assertRegex(
            first,
            r"if\s*\(\s*httpd_ws_get_fd_info\([^)]*\)\s*!=\s*HTTPD_WS_CLIENT_WEBSOCKET\s*\)",
            "普通 GET 的判定必须是「httpd_ws_get_fd_info(...) != "
            "HTTPD_WS_CLIENT_WEBSOCKET」（即该 fd 不是活跃 WS 会话）。"
            "写成 == 会把两类请求的分支完全对调")
        self.assertNotIn("ev_sub_find", first,
                         "首分支仍用 ev_sub_find 判定 —— fd 复用会让普通 GET 被"
                         "误判成 WS 帧（真机 bug：GET /events 恒 0 字节）")
        self.assertNotIn("first_time", self.net,
                         "first_time 判据已被证明不可靠，必须彻底移除而非绕过")

    def test_plain_get_path_returns_status_without_subscribing(self):
        """普通 GET 分支：回状态 JSON、正常返回，且**不得**订阅/收帧。"""
        hb = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                       self.net, re.S)
        self.assertIsNotNone(hb, "找不到 events_ws_handler 函数体")
        body = hb.group(1)
        # ⚠️ 必须在 handler 体内搜：httpd_ws_get_fd_info 在 ev_sub_reap 里也出现，
        #   直接对全文正则会匹配到那个函数，断言看似成立实则查错了对象。
        # ⚠️ 分支体按「起点 → 第一个 return ESP_OK」切，**不靠缩进**：源码排版
        #   一变（缩进增减、分支嵌套）缩进正则就会跨分支吃到邻居的代码，
        #   断言反而对错了对象（踩过一次）。
        mi = body.index("HTTPD_WS_CLIENT_WEBSOCKET)")
        self.assertIn("httpd_ws_get_fd_info", body[:mi])
        # 分支体到 httpd_resp_send 为止（**含**该 token，故用 end 参数 +1）：
        # 早于它的是 snprintf/ESP_FAIL 守卫，晚于它的是 return ESP_OK。
        # 若按 return 切会被守卫里的 ESP_FAIL 截断；若用开区间又会切掉 send 自己。
        _send = body.index("httpd_resp_send", mi) + len("httpd_resp_send")
        plain = body[mi:_send]
        self.assertIn("httpd_resp_send", plain, "普通 GET 必须回状态 JSON")
        # return ESP_OK 要在 httpd_resp_send **之后**（含响应头设置）才成立，
        # 故断言取 send 之后那一小段。
        after = body[_send:_send + 80]
        self.assertRegex(after, r"return\s+ESP_OK",
                         "普通 GET 是正常请求，必须 return ESP_OK；返回 ESP_FAIL "
                         "会让 httpd 直接关连接（那正是本 bug 的症状）")
        for bad in ("ev_sub_add", "httpd_ws_recv_frame"):
            self.assertNotIn(bad, plain,
                             "普通 GET 分支不应出现 %s：它还没握手，"
                             "收帧必然失败，且订阅普通请求毫无意义" % bad)

    def test_ws_recv_failure_never_sends_http_response(self):
        """🔴 已在 WS 模式的会话，收帧失败绝不能发 HTTP 响应。

        这条比上一条更要紧：混进 HTTP 响应会破坏 WS 流，客户端协议层直接断开
        （历史上真机踩过）。收帧失败只允许 ESP_FAIL（交 httpd 关 socket），
        并且要把该 fd 从订阅表摘掉，避免留下又一条僵尸条目。
        """
        m = re.search(r"if\s*\(\s*ret\s*!=\s*ESP_OK\s*\)\s*\{(.*?)\n    \}",
                      self.net, re.S)
        self.assertIsNotNone(m, "找不到 recv 失败分支")
        fail = m.group(1)
        self.assertNotIn("httpd_resp_send", fail,
                         "WS 会话收帧失败时发 HTTP 响应会污染 WS 流")
        self.assertRegex(fail, r"return\s+ESP_FAIL",
                         "收帧失败必须 return ESP_FAIL 让 httpd 关 socket")
        self.assertIn("ev_sub_del", fail,
                      "收帧失败必须把该 fd 摘出订阅表，否则又多一条僵尸条目")

    def test_stale_subscriber_reaping_uses_public_ws_api(self):
        """僵尸订阅回收必须走 httpd_ws_get_fd_info，不能自造判据。"""
        m = re.search(r"static void ev_sub_reap\(void\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m, "找不到 ev_sub_reap")
        self.assertIn("httpd_ws_get_fd_info", m.group(0))
        self.assertIn("s_ev_sub_count--", m.group(0),
                      "回收僵尸条目时必须同步递减计数，否则 subscribers 虚高")

    def test_reaping_never_happens_from_non_httpd_task(self):
        """🔴 ev_sub_reap() 只允许在 httpd 任务里调用。

        httpd_sess_get() 只有在 handler 内调用才走「当前请求 session」快捷路径；
        从外部任务调会无锁遍历会话表（httpd_sess.c::httpd_sess_enum）→ 竞态。
        platform_events_broadcast() 由相机/BLE/HTTP 任意任务调用，是最容易
        顺手加 reap 的地方，故显式禁掉。
        """
        m = re.search(r"int platform_events_broadcast\(const char \*json\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m, "找不到 platform_events_broadcast")
        self.assertNotIn("ev_sub_reap", m.group(1),
                         "broadcast 由非 httpd 任务调用，此处 reap 会无锁遍历"
                         "httpd 会话表（竞态）")
        # 反之：handler 内必须 reap，否则 subscribers 永远虚高
        h = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIn("ev_sub_reap", h.group(1),
                      "events_ws_handler（在 httpd 任务内）应负责回收僵尸订阅")

    def test_reaping_happens_before_plain_get_branch(self):
        """🔴 reap 必须在「普通 GET vs WS 帧」的判定**之前**执行。

        放错位置的后果很隐蔽：普通 GET 分支只回状态 JSON 就 return，
        若 reap 被放在 WS 分支里，读到的 `subscribers` 就永远是上一次 WS
        会话留下的旧值（真机实测：正常 close 帧后连查 3 次普通 GET，
        恒为 1），而所有功能看起来都正常 —— 只是个永远偏大的计数。
        """
        m = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        i_reap = body.find("ev_sub_reap()")
        i_branch = body.find("HTTPD_WS_CLIENT_WEBSOCKET)")
        self.assertNotEqual(i_reap, -1, "handler 里应调用 ev_sub_reap()")
        self.assertNotEqual(i_branch, -1, "找不到 WS/普通 GET 的判定行")
        self.assertLess(i_reap, i_branch,
                        "ev_sub_reap() 必须在普通 GET/WS 分支判定之前调用，"
                        "否则普通 GET 读到的 subscribers 永远是旧值")

    def test_ws_frames_never_mixed_with_http_responses(self):
        """订阅之后的分支里绝不能出现 httpd_resp_*。"""
        m = re.search(r"events_ws_handler\s*\(httpd_req_t\s*\*req\)\s*\{(.*?)\n\}",
                      self.net, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        # 普通 GET 分支（第一个 return ESP_OK 之前）允许 httpd_resp_send
        sub = body[body.index("httpd_ws_recv_frame"):]
        self.assertNotIn("httpd_resp_", sub,
                         "订阅后的分支出现 httpd_resp_* —— 会把 HTTP 响应混进 "
                         "WS 流，客户端协议层直接断开（历史上真机踩过）")

    def test_subscriber_cap_within_socket_budget(self):
        """订阅上限必须小于 max_open_sockets 剩余额度。"""
        m = re.search(r"#define\s+EVENTS_SUB_MAX\s+(\d+)", self.net)
        self.assertIsNotNone(m, "找不到 EVENTS_SUB_MAX")
        subs = int(m.group(1))
        mo = re.search(r"max_open_sockets\s*=\s*(\d+)", self.net)
        self.assertIsNotNone(mo, "找不到 max_open_sockets 设置")
        self.assertLess(subs, int(mo.group(1)),
                        "订阅上限 %d 必须小于 max_open_sockets=%s（另有 UI/stream/"
                        "snapshot 等占用）" % (subs, mo.group(1)))

    def test_schema_documents_ws_first_frame_requirement(self):
        """schema 的 /events note 必须写明「客户端必须先发一帧」这条时序铁律。

        断言用词取自 note 里的实际表述（不是凭空挑字），这样改文案时能立刻
        看到是哪条铁律被删了，而不是无声通过。
        """
        ev = [e for e in _schema()["endpoints"] if e["path"] == "/events"][0]
        note = str(ev.get("note", ""))
        self.assertEqual(ev.get("transport"), "websocket")
        for token, why in (
            ("握手", "未说明 WS 握手与普通 GET 的两种用法"),
            ("先发一帧", "未写明「客户端必须先发一帧」——设备不会在握手后主动推帧，"
                         "这是两次烧写才踩明白的铁律"),
            ("is_websocket", "未说明 /events 须单独注册（不进 ROUTES[]）"),
            ("status", "未说明首帧回 status 兼作握手确认"),
        ):
            self.assertIn(token, note, "/events note %s（缺 %r）" % (why, token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
