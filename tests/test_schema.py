"""tests/test_schema.py — shared/api_schema.yaml 的回归闸门。

历史坑（2026-10-02）：api_schema.yaml 被一次编辑改坏（note 双引号未闭合），
但既有 93 项测试全部照常通过 —— 因为没有任何测试读取它。结果坏 YAML 在
CI 之外静默存活，直到某次手工加载才暴露。本测试补上这道闸门：

  1. YAML 必须可解析（结构损坏立即判错）；
  2. 5 个控制器侧原子端点必须存在且 owner=controller；
  3. speed_tiers 的档位集合 / step_ms / tick 必须与 controller/atoms.py
     里的 ATOM_SPEEDS **逐键交叉校验** —— 任一侧改了档位，另一侧不跟就会红。
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("shared", "controller"):
    _p = os.path.join(_ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402
from atoms import ATOM_SPEEDS, ATOM_DEFAULT_SPEED  # noqa: E402

_SCHEMA_PATH = os.path.join(_ROOT, "shared", "api_schema.yaml")

_REQUIRED_CONTROLLER_ENDPOINTS = {
    "/atom/move",
    "/atom/drag",
    "/atom/click",
    "/atom/anchor",
    "/atom/state",
}

# 真实传感器端点：固件实现，UI 据此把曝光/对比度/饱和度从 CSS 近似升级为真实改相机。
_REQUIRED_FIRMWARE_ENDPOINTS = {
    "/camera": {"method": "POST", "owner": "firmware",
                "fields": ["exposure_auto", "exposure_value", "auto_wb", "auto_gain",
                           "contrast", "saturation", "frame_size"]},
}


def _read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def strip_c_comments(src):
    """去掉 C 注释 —— 否则注释里提到的 API 名会被误判成「代码在用」。

    ⚠️ 必须跳过字符串字面量：像 "/api/v1/gpio/*" 里的 /* 不是注释起点，
    若用 naive 的 /\\*.*?\\*/ 会把其后直到下一个 */ 的代码整段吞掉，导致
    gpio 通配路由「在 routes.c 里存在却查不到」。字符串内容原样保留。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':                      # 字符串字面量：原样保留
            j = i + 1
            while j < n:
                if src[j] == '\\':        # 跳过转义（\" \\ 等）
                    j += 2
                    continue
                if src[j] == '"':
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        if src[i:i + 2] == '/*':          # 块注释 /* ... */
            j = src.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if src[i:i + 2] == '//':          # 行注释 // ...（至行尾）
            j = src.find('\n', i)
            i = n if j == -1 else j
            continue
        out.append(c)
        i += 1
    return ''.join(out)


class TestApiSchemaIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            cls.doc = yaml.safe_load(f)

    def test_yaml_parses(self):
        """YAML 必须可解析（结构损坏（如引号未闭合）必须立即失败）。"""
        self.assertIsInstance(self.doc, dict, "api_schema.yaml 顶层必须是 mapping")

    def test_controller_endpoints_present_and_owned(self):
        endpoints = self.doc.get("endpoints", [])
        by_path = {e["path"]: e for e in endpoints}
        for path in _REQUIRED_CONTROLLER_ENDPOINTS:
            self.assertIn(path, by_path,
                           "缺少控制器侧原子端点 %s（见 controller/atoms.py）" % path)
            self.assertEqual(by_path[path].get("owner"), "controller",
                             "%s 的 owner 必须是 controller" % path)

    def test_speed_tiers_cross_check_atoms(self):
        """档位表必须与 ATOM_SPEEDS 逐键一致（step_ms / tick 不能漂移）。"""
        tiers = self.doc.get("speed_tiers", {}).get("tiers", {})
        self.assertEqual(set(tiers.keys()), set(ATOM_SPEEDS.keys()),
                         "speed_tiers 与 ATOM_SPEEDS 的档位名不一致")
        for name, tier in ATOM_SPEEDS.items():
            y = tiers[name]
            self.assertEqual(y.get("step_ms"), tier["step_ms"],
                             "档位 %s 的 step_ms 在 schema 与 atoms 不一致" % name)
            self.assertEqual(y.get("tick"), tier["tick"],
                             "档位 %s 的 tick 在 schema 与 atoms 不一致" % name)

    def test_default_speed_in_tiers(self):
        tiers = self.doc.get("speed_tiers", {}).get("tiers", {})
        self.assertIn(ATOM_DEFAULT_SPEED, tiers,
                      "default_speed=%s 不在 speed_tiers 中" % ATOM_DEFAULT_SPEED)

    def test_camera_endpoint_firmware_owned(self):
        """/camera 是真实传感器端点（固件 owner），UI 用它把曝光从 CSS 近似升级为真实改相机。"""
        endpoints = self.doc.get("endpoints", [])
        by_path = {e["path"]: e for e in endpoints}
        for path, spec in _REQUIRED_FIRMWARE_ENDPOINTS.items():
            self.assertIn(path, by_path, "缺少固件传感器端点 %s" % path)
            e = by_path[path]
            self.assertEqual(e.get("method"), spec["method"], "%s 方法应为 %s" % (path, spec["method"]))
            self.assertEqual(e.get("owner"), spec["owner"], "%s 的 owner 必须是 %s" % (path, spec["owner"]))
            req = e.get("request", {})
            for f in spec["fields"]:
                self.assertIn(f, req, "%s 的 request 缺少字段 %s" % (path, f))


    def test_camera_autofunc_contract(self):
        """画面调节卡的自动功能（白平衡/增益/曝光）必须有真实固件支撑，不能只是 UI 摆设。

        守卫点（曾踩坑：凭记忆把 AGC 写成 set_gainctrl，实际 esp32-camera v2 命名为
        set_gain_ctrl，编译直接失败）：
        1) ui.html 含三个自动开关按钮（data-k=autoExp/autoWb/autoGain 与 id=adjAuto*）及「一键自动」。
        2) esp32 platform_camera.c 在 camera_apply_settings 里真正调用 set_whitebal(s,...) 与
           set_gain_ctrl(s,...)（注意是 set_gain_ctrl，不是 set_gainctrl）。
        3) routes.c 解析 auto_wb / auto_gain 并调用 platform_camera_set_auto_wb / _gain。
        4) platform_camera_get_settings 的签名已扩展到 auto_wb / auto_gain 两个新指针。
        """
        import re
        ui = _read_file(os.path.join(_ROOT, "firmware", "main", "ui.html"))
        cam_c = strip_c_comments(_read_file(
            os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_camera.c")))
        routes_c = strip_c_comments(_read_file(
            os.path.join(_ROOT, "firmware", "main", "routes.c")))

        # 1) UI 控件（id 沿用本页 adjXxx 驼峰风格：adj + 首字母大写）
        auto_ids = {"autoExp": "adjAutoExp", "autoWb": "adjAutoWb", "autoGain": "adjAutoGain"}
        for k, idv in auto_ids.items():
            self.assertIn('data-k="%s"' % k, ui, "ui.html 缺少自动开关 data-k=%s" % k)
            self.assertIn('id="%s"' % idv, ui, "ui.html 缺少自动开关 %s" % idv)
        self.assertIn('id="adjAutoAll"', ui, "ui.html 缺少「一键自动」按钮")

        # 2) 固件真正套用 AWB / AGC（必须在 camera_apply_settings 内、且是对传感器的调用）
        m = re.search(r"static void camera_apply_settings\(void\)\s*\{(.*?)\n\}", cam_c, re.S)
        self.assertIsNotNone(m, "找不到 camera_apply_settings()")
        body = m.group(1)
        self.assertRegex(body, r"set_whitebal\(\s*s\b", "camera_apply_settings 未真正调用 set_whitebal")
        self.assertRegex(body, r"set_gain_ctrl\(\s*s\b", "camera_apply_settings 未真正调用 set_gain_ctrl")

        # 3) 路由解析 + 调用
        self.assertRegex(routes_c, r'json_find_int\([^)]*"?auto_wb"', "routes.c 未解析 auto_wb")
        self.assertRegex(routes_c, r'json_find_int\([^)]*"?auto_gain"', "routes.c 未解析 auto_gain")
        self.assertRegex(routes_c, r"platform_camera_set_auto_wb\(", "routes.c 未调用 platform_camera_set_auto_wb")
        self.assertRegex(routes_c, r"platform_camera_set_auto_gain\(", "routes.c 未调用 platform_camera_set_auto_gain")

        # 4) get_settings 签名扩展
        g = re.search(r"int\s+platform_camera_get_settings\(([^;]*?)\)\s*\{", cam_c, re.S)
        self.assertIsNotNone(g, "找不到 platform_camera_get_settings 定义")
        self.assertRegex(g.group(1), r"auto_wb", "get_settings 未扩展 auto_wb 参数")
        self.assertRegex(g.group(1), r"auto_gain", "get_settings 未扩展 auto_gain 参数")


    def test_camera_framesize_contract(self):
        """分辨率运行时切换（下拉框 + /camera frame_size）必须有真实固件支撑，不能只是 UI 摆设。

        守卫点：
        1) ui.html 含 <select id="adjRes">，选项值合法（framesize 枚举：QVGA=6 / 480×320=9 / VGA(640×480)=10 / SVGA=11 / XGA=12 / 720p=13 / UXGA=15 / 1080p=16）；
           切换必须把 frame_size 真正发往 /api/v1/camera（非纯本地）。
        2) esp32 platform_camera.c 定义 platform_camera_set_framesize 且走 esp_camera_reconfigure()
           重分配帧缓冲——不能只调 sensor set_framesize()：组件在 init 时按初始 frame_size 一次性分配
           fb，运行时只改寄存器不重分配缓冲，切 UXGA/1080p 会撑爆 fb（cam_hal FB-OVF → fb_get 4s
           超时返回 NULL → /snapshot 503）。2026-10-25 实测复现后改 reconfigure。
        3) routes.c 解析 frame_size 并调用 platform_camera_set_framesize。
        4) handle_camera 响应回显 frame_size。
        """
        import re
        ui = _read_file(os.path.join(_ROOT, "firmware", "main", "ui.html"))
        cam_c = strip_c_comments(_read_file(
            os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_camera.c")))
        routes_c = strip_c_comments(_read_file(
            os.path.join(_ROOT, "firmware", "main", "routes.c")))

        # 1) UI 下拉框 + 合法选项
        self.assertIn('id="adjRes"', ui, "ui.html 缺少分辨率下拉框 id=adjRes")
        self.assertRegex(ui, r'<option value="6"[^>]*>QVGA', "分辨率下拉缺 QVGA(320x240, value=6)")
        self.assertRegex(ui, r'<option value="10"[^>]*>VGA', "分辨率下拉缺 VGA(640x480, value=10，默认)")
        self.assertRegex(ui, r'<option value="12"[^>]*>XGA', "分辨率下拉缺 XGA(1024x768, value=12)")
        self.assertRegex(ui, r'<option value="13"[^>]*>720p', "分辨率下拉缺 720p(1280x720, value=13)")
        self.assertRegex(ui, r'<option value="15"[^>]*>UXGA', "分辨率下拉缺 UXGA(1600x1200, value=15)")
        self.assertRegex(ui, r'<option value="16"[^>]*>1080p', "分辨率下拉缺 1080p(1920x1080, value=16)")
        self.assertRegex(ui, r"frame_size\s*:\s*st\.res",
                         "分辨率切换未把 frame_size 发往 /api/v1/camera（非纯本地）")

        # 2) 固件平台层实现
        self.assertRegex(cam_c, r"int\s+platform_camera_set_framesize\s*\(",
                         "platform_camera.c 未定义 platform_camera_set_framesize")
        m = re.search(r"int\s+platform_camera_set_framesize\([^)]*\)\s*\{(.*?)\n\}", cam_c, re.S)
        self.assertIsNotNone(m, "找不到 platform_camera_set_framesize 函数体")
        self.assertRegex(m.group(1), r"esp_camera_reconfigure\s*\(",
                         "platform_camera_set_framesize 必须走 esp_camera_reconfigure() 重分配帧缓冲"
                         "（只调 s->set_framesize() 不重分配 fb，切 UXGA/1080p 必现 cam_hal FB-OVF →"
                         " /snapshot 503，2026-10-25 实测）")

        # 3) 路由解析 + 调用
        self.assertRegex(routes_c, r'json_find_int\([^)]*"?frame_size"',
                         "routes.c 未解析 frame_size")
        self.assertRegex(routes_c, r"platform_camera_set_framesize\(",
                         "routes.c 未调用 platform_camera_set_framesize")

        # 4) 响应回显
        self.assertRegex(routes_c, r"frame_size",
                         "handle_camera 响应未包含 frame_size 回显")


class TestFirmwareRouteRegistration(unittest.TestCase):
    """守卫 httpd 路由表与 api_schema 的一致性。

    历史坑（2026-10-02）：新增 POST /api/v1/camera 时只改了 routes.c 的
    dispatch，**忘了把它加进 platform/esp32/platform_net.c 的 ROUTES[]**。
    症状：真机 POST /api/v1/camera 返回 404 "Nothing matches the given URI"
    —— 那句是 esp_http_server 的 URI 匹配器抛的，请求根本没进 routes_handle()，
    所以看起来像"端点不存在"，极易误判为网络/固件没烧成功。
    """

    _NET_C = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_net.c")

    def setUp(self):
        with open(self._NET_C, "r", encoding="utf-8", errors="replace") as f:
            self.src = f.read()

    def _routes_table(self):
        import re
        m = re.search(r"ROUTES\[\]\s*=\s*\{(.*?)\}", self.src, re.S)
        self.assertIsNotNone(m, "在 %s 里找不到 ROUTES[] 定义" % self._NET_C)
        return set(re.findall(r'"(/api/v1/[^"]+)"', m.group(1)))

    # 有意的注册例外（2026-10-13 P5 扩充控制面后新增）——两条都必须走
    # 「非 ROUTES[] 路径」，所以本守卫跳过；它们的正确性由
    # tests/test_control_contract.py 的对应专项守卫钉死：
    #   /gpio/{pin} → 注册为通配 /api/v1/gpio/*（见 test_gpio_pin_registered_as_wildcard）
    #   /events     → HTTP_GET + is_websocket 单独注册（见 test_events_is_not_in_generic_routes_table
    #                  / test_ws_handler_registered_with_is_websocket）
    # 在这里写死而不是通配豁免，是为了让「新增端点默认受检查」这一性质保持。
    _REGISTRATION_EXCEPTIONS = {"/gpio/{pin}", "/events"}

    def test_schema_endpoints_registered_in_httpd(self):
        """api_schema.yaml 里每个固件侧端点都必须在 ROUTES[] 中注册。

        ⚠️ schema 里的 path 写法不统一：有的带 /api/v1 前缀（/api/v1/atom/*），
        有的只写末段（/camera）。比对时按「去掉 /api/v1 前缀后」归一化，
        否则像 /camera 这样的条目会被 startswith("/api/v1/") 过滤掉、
        守卫静默失效（曾发生）。
        ⚠️ 两个有意例外见 _REGISTRATION_EXCEPTIONS（各自有专项守卫）。
        """
        import yaml
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        registered = self._routes_table()

        def norm(p):
            return p[len("/api/v1"):] if p.startswith("/api/v1") else p

        reg_norm = {norm(p) for p in registered}
        missing = []
        for e in doc.get("endpoints", []):
            path = e.get("path") or ""
            if not path:
                continue
            if path.startswith("/atom/"):      # 控制器侧，不进固件 httpd
                continue
            if path in self._REGISTRATION_EXCEPTIONS:
                continue
            if norm(path) not in reg_norm:
                missing.append(path)
        self.assertEqual(missing, [],
                         "这些端点未注册进 platform_net.c 的 ROUTES[] —— 真机会 404 "
                         "'Nothing matches the given URI'（httpd 层拦截，routes.c 的 "
                         "dispatch 根本不会被调用）。若是新增的例外端点，必须写进 "
                         "_REGISTRATION_EXCEPTIONS 并补上对应专项守卫")

    def test_routes_table_dispatch_covered(self):
        """ROUTES[] 里每个端点都必须在 routes.c 的 dispatch 中被处理。

        ⚠️ 必须剥掉注释再查：只在注释里提一句端点名不算「被处理」，
        否则本守卫会给出假绿灯。
        """
        routes_c = os.path.join(_ROOT, "firmware", "main", "routes.c")
        rsrc = strip_c_comments(_read_file(routes_c))
        for path in sorted(self._routes_table()):
            self.assertIn(path, rsrc,
                          "%s 在 ROUTES[] 里注册了，但 routes.c 没有处理它" % path)

    def test_max_uri_handlers_capacity(self):
        """max_uri_handlers 必须容得下 ROUTES + / + /stream + /snapshot。"""
        import re
        m = re.search(r"max_uri_handlers\s*=\s*(\d+)", self.src)
        self.assertIsNotNone(m, "找不到 max_uri_handlers 设置")
        cap = int(m.group(1))
        needed = len(self._routes_table()) + 3   # + / (UI) + /stream + /snapshot
        self.assertGreaterEqual(cap, needed,
                                "max_uri_handlers=%d 装不下 %d 个 handler（加端点时须同步调大，"
                                "否则注册失败表现为端点 404）" % (cap, needed))


class TestSysinfoContract(unittest.TestCase):
    """/api/v1/sysinfo 的字段名与接缝 API 守卫。

    历史坑（2026-10-02，同一轮里连踩三个，全是「凭记忆写 IDF API」造成的）：
      ① 用了 vTaskGetInfo() —— 本固件 CONFIG_FREERTOS_USE_TRACE_FACILITY 未开，
         该函数根本不存在（声明的 `#if configUSE_TRACE_FACILITY == 1` 不成立）；
      ② 读了 nvs_stats_t.total_pages / free_pages —— 该结构体只有
         used_entries / free_entries / available_entries / total_entries /
         namespace_count，没有 page 口径；
      ③ 把 esp_partition_find_first() 的返回值（const esp_partition_t*）
         当迭代器喂给 esp_partition_next() —— 迭代必须用 esp_partition_find()。
    这三点都只有在真机编译时才会炸（本机无 host C 编译器），故在此做源码级守卫。
    """

    _HEALTH_ESP32 = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_health.c")
    _HEALTH_HOST = os.path.join(_ROOT, "firmware", "platform", "host", "platform_health.c")
    _HEADER = os.path.join(_ROOT, "firmware", "platform", "platform_sysinfo.h")
    _ROUTES_C = os.path.join(_ROOT, "firmware", "main", "routes.c")
    _NET_C = os.path.join(_ROOT, "firmware", "platform", "esp32", "platform_net.c")

    # routes.c 实际发出的字段（真源）；与 schema 的 /sysinfo 逐项对齐
    _TOP_KEYS = {"ok", "nvs", "mem", "storage_mounted", "entry_bytes",
                 "partitions", "tasks"}
    _NVS_KEYS = {"valid", "part_size", "used_entries", "total_entries",
                 "free_entries", "namespace_count", "used_bytes_est",
                 "cap_bytes_est"}
    _MEM_KEYS = {"heap_free", "heap_min_free", "heap_largest", "internal_total",
                 "psram_total", "psram_free", "psram_largest"}
    _PART_KEYS = {"label", "offset", "size", "app", "data", "mounted"}
    _TASK_KEYS = {"name", "stack_free"}

    @staticmethod
    def _read(p):
        return _read_file(p)

    @staticmethod
    def _strip_comments(src):
        return strip_c_comments(src)

    @staticmethod
    def _json_template(fn_src):
        """把 C 源码里的 JSON 格式串还原成可正则的模板。

        C 的字面量写法是 `"{\\"ok\\":true," "\\"nvs\\":{...` —— 引号被
        反斜杠转义、相邻字面量在编译期拼接。直接对源码套 `"key":` 会大面积
        误判（既有漏匹配也有跨字面量错配），故必须分三步：
          1. 剥注释；
          2. **剔除字符字面量** —— 这是最隐蔽的一坑：代码里有 `'"'` 和 `'\\'`
             这类单引号字符字面量（JSON 转义兜底用），其中的裸 `"` 会让
             「按双引号切字符串」彻底错位，导致 `label` / `name` 这类字段
             的引号被吞掉、静默漏检；
          3. 取出所有字符串字面量 → 拼接 → 反转义。
        """
        import re
        src = strip_c_comments(fn_src)
        src = re.sub(r"'(?:\\.|[^'\\])*'", "''", src)        # 剔除 'x' / '\\' / '\\0'
        lits = re.findall(r'"((?:[^"\\]|\\.)*)"', src)
        tpl = "".join(lits)
        tpl = tpl.replace('\\"', '"').replace("\\\\", "\\")
        return tpl

    def _sysinfo_fn_src(self):
        """截取 routes.c 里 handle_sysinfo 的函数体。"""
        import re
        src = self._read(self._ROUTES_C)
        m = re.search(r"static int handle_sysinfo\(.*?\n\}", src, re.S)
        self.assertIsNotNone(m, "routes.c 里找不到 handle_sysinfo 函数体")
        return m.group(0)

    def test_health_c_does_not_use_vtaskgetinfo(self):
        """未开 configUSE_TRACE_FACILITY：绝不允许出现 vTaskGetInfo/TaskStatus_t。"""
        esp = self._strip_comments(self._read(self._HEALTH_ESP32))
        self.assertNotIn("vTaskGetInfo", esp,
                         "vTaskGetInfo 需要 CONFIG_FREERTOS_USE_TRACE_FACILITY=y，"
                         "本固件未开 → 真机编译会直接失败。用 xTaskGetHandle + "
                         "uxTaskGetStackHighWaterMark 替代。")
        self.assertNotIn("TaskStatus_t", esp,
                         "TaskStatus_t/vTaskGetInfo 同属 trace facility，本固件不可用")

    def test_health_c_does_not_read_nvs_pages(self):
        """nvs_stats_t 没有 page 口径，不得读取 total_pages/free_pages。"""
        esp = self._strip_comments(self._read(self._HEALTH_ESP32))
        for bad in ("st.total_pages", "st.free_pages", "st.available_pages"):
            self.assertNotIn(bad, esp,
                             "nvs_stats_t 没有 %s 字段（只有 used_entries/"
                             "free_entries/available_entries/total_entries/"
                             "namespace_count）→ 真机编译失败" % bad)
        self.assertIn("available_entries", esp,
                      "剩余可用条目必须取自 nvs_stats_t.available_entries")

    def test_partition_iteration_uses_find_not_find_first(self):
        """分区枚举必须用 esp_partition_find()（find_first 返回的是分区指针）。"""
        esp = self._strip_comments(self._read(self._HEALTH_ESP32))
        self.assertIn("esp_partition_iterator_t", esp, "缺少迭代器类型使用")
        self.assertIn("esp_partition_find(", esp,
                      "迭代必须用 esp_partition_find()；esp_partition_find_first() "
                      "返回 const esp_partition_t*，当迭代器用会编译失败")
        import re
        self.assertIsNone(
            re.search(r"iterator_t\s+\w+\s*=\s*esp_partition_find_first", esp),
            "把 esp_partition_find_first() 的结果赋给迭代器变量 = 类型不匹配")

    def test_task_stack_uses_uptime_safe_api(self):
        """任务栈水位必须走 uxTaskGetStackHighWaterMark（唯一可用的公开 API）。"""
        esp = self._strip_comments(self._read(self._HEALTH_ESP32))
        self.assertIn("uxTaskGetStackHighWaterMark", esp,
                      "栈水位必须用 uxTaskGetStackHighWaterMark")
        self.assertIn("xTaskGetHandle", esp, "按名字查任务句柄用 xTaskGetHandle")

    def test_seam_signatures_consistent(self):
        """头文件声明的返回类型必须与实现、与 routes.c 的调用一致。

        历史坑：头里写 void platform_sysinfo_tasks(...)，routes.c 却写
        `int ntasks = platform_sysinfo_tasks(...)` —— 真机编译失败。
        """
        import re
        hdr = self._read(self._HEADER)
        for fn in ("platform_sysinfo_partitions", "platform_sysinfo_tasks"):
            m = re.search(r"\bint\s+%s\s*\(" % fn, hdr)
            self.assertIsNotNone(m,
                                 "%s 必须返回 int（调用方用它取写入条数）" % fn)
        routes = self._read(self._ROUTES_C)
        self.assertRegex(routes,
                         r"int\s+nparts\s*=\s*platform_sysinfo_partitions",
                         "routes.c 应把 platform_sysinfo_partitions 的返回值当条数用")
        self.assertRegex(routes,
                         r"int\s+ntasks\s*=\s*platform_sysinfo_tasks",
                         "routes.c 应把 platform_sysinfo_tasks 的返回值当条数用")

    # ---- 🔴 内存口径守卫（2026-10-02 真机故障回归闸门） ----
    #
    # 故障经过：platform_sysinfo_mem 最初用 esp_get_free_heap_size() 填 heap_free，
    # 而该函数统计的是**含 PSRAM 在内的全部 region**。真机上 heap_free 报 8.0MB，
    # 而同一结构体里 internal_total 只有 333KB —— 同一份 JSON 自相矛盾；
    # 更糟的是内存吃紧时它仍会报 ~7.9MB，等于安全指示灯失灵（假阴性）。
    # 这类错误的隐蔽性极高：**编译通过、host 测试通过、只有真机对账才暴露**，
    # 所以必须由契约测试钉死，不能只靠注释。

    def test_mem_uses_region_scoped_caps(self):
        """内部堆水位必须分 region 查询，禁用 esp_get_free_heap_size()。"""
        import re
        esp = self._strip_comments(self._read(self._HEALTH_ESP32))
        m = re.search(r"void\s+platform_sysinfo_mem\s*\(.*?\n\}", esp, re.S)
        self.assertIsNotNone(m, "找不到 platform_sysinfo_mem 的 esp32 实现")
        body = m.group(0)
        self.assertNotIn(
            "esp_get_free_heap_size", body,
            "platform_sysinfo_mem 不得使用 esp_get_free_heap_size()：它统计"
            "「内部 DRAM + PSRAM」总和，填进 heap_free 会 > internal_total（真机 "
            "实测 8.0MB vs 333KB），且内存吃紧时不报警。改用 heap_caps_*_size("
            "MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)。")
        for fn in ("heap_caps_get_free_size",
                   "heap_caps_get_minimum_free_size",
                   "heap_caps_get_largest_free_block",
                   "heap_caps_get_total_size"):
            self.assertIn(fn, body, "内部堆四个水位字段都应使用 %s" % fn)
        self.assertIn("MALLOC_CAP_INTERNAL", body,
                      "内部堆查询必须显式限定 MALLOC_CAP_INTERNAL，不能靠默认语义")
        self.assertIn("MALLOC_CAP_SPIRAM", body,
                      "PSRAM 必须单独查询，才满足 total = 内部 + PSRAM 的分解关系")

    def test_host_mem_stub_is_self_consistent(self):
        """host 假值必须与「内部堆」口径自洽，否则本地测试会掩盖真机口径错误。

        历史坑：host 桩曾填 heap_free=412344（总堆数字），远大于
        internal_total=333531 —— 假数据自己先矛盾了，却也照样让 host 侧全绿。
        """
        import re
        src = self._strip_comments(self._read(self._HEALTH_HOST))
        m = re.search(r"void\s+platform_sysinfo_mem\s*\(.*?\n\}", src, re.S)
        self.assertIsNotNone(m, "找不到 platform_sysinfo_mem 的 host 桩")

        def num(key):
            mm = re.search(r"out->%s\s*=\s*([0-9]+)" % key, m.group(0))
            self.assertIsNotNone(mm, "host 桩缺少 out->%s 赋值" % key)
            return int(mm.group(1))

        hf, mn, lg = num("heap_free"), num("heap_min_free"), num("heap_largest")
        tot = num("internal_total")
        self.assertLessEqual(
            hf, tot,
            "host 桩 heap_free(%d) 必须 ≤ internal_total(%d)：两者同口径（仅内部 "
            "RAM）。填「总堆」数字会掩盖真机的口径错误。" % (hf, tot))
        self.assertLessEqual(mn, hf, "heap_min_free(%d) 必须 ≤ heap_free(%d)" % (mn, hf))
        self.assertLessEqual(lg, hf, "heap_largest(%d) 必须 ≤ heap_free(%d)" % (lg, hf))

    def test_health_free_heap_scope_documented(self):
        """health.free_heap 与 sysinfo.mem.heap_free 口径不同，schema 必须写明。

        两个端点并列显示时会出现「8MB vs 41KB」的观感矛盾（前者含 PSRAM，后者仅
        内部），若不写明口径，后来者会把其中一个「修」成另一个的样子。
        """
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        by_path = {e["path"]: e for e in doc.get("endpoints", [])}
        self.assertIn("/health", by_path, "schema 缺 /health")
        self.assertIn("/sysinfo", by_path, "schema 缺 /sysinfo")
        hh = str(by_path["/health"]["response"].get("free_heap", ""))
        self.assertIn("PSRAM", hh,
                      "/health.free_heap 必须注明含 PSRAM（否则与 sysinfo 口径混淆）")
        self.assertIn("sysinfo", hh,
                      "/health.free_heap 应指向 sysinfo.mem 作为分 region 口径的对照")
        mem = by_path["/sysinfo"]["response"]["mem"]
        self.assertIn("PSRAM", str(mem.get("heap_free", "")),
                      "sysinfo.mem.heap_free 必须注明不含 PSRAM")

    def test_seam_implemented_on_both_sides(self):
        """接缝声明的每个函数都必须在 esp32 与 host 两份实现里都存在。"""
        import re
        hdr = self._read(self._HEADER)
        decls = re.findall(
            r"\b(?:void|int)\s+(platform_sysinfo_\w+)\s*\(", hdr)
        self.assertGreater(len(decls), 0, "没解析出任何接缝函数声明")
        esp, host = self._read(self._HEALTH_ESP32), self._read(self._HEALTH_HOST)
        for fn in decls:
            self.assertIn(fn, esp, "esp32 实现缺少 %s" % fn)
            self.assertIn(fn, host, "host 桩缺少 %s（会导致 host 测试链接失败）" % fn)

    def test_json_fields_match_source_and_schema(self):
        """routes.c 发出的字段 == 本测试的期望集合 == api_schema.yaml 的声明。"""
        import re
        found = set(re.findall(r'"([a-z_]+)":', self._json_template(self._sysinfo_fn_src())))
        expected = (self._TOP_KEYS | self._NVS_KEYS | self._MEM_KEYS
                    | self._PART_KEYS | self._TASK_KEYS)
        self.assertEqual(found - expected, set(),
                         "routes.c 新增了未登记的字段（schema/前端/测试要同步）")
        self.assertEqual(expected - found, set(),
                         "routes.c 缺少这些字段（前端会读到 undefined）")

        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        by_path = {e["path"]: e for e in doc.get("endpoints", [])}
        self.assertIn("/sysinfo", by_path, "api_schema.yaml 缺少 /sysinfo")
        resp = by_path["/sysinfo"].get("response", {})
        self.assertEqual(set(resp.keys()) - self._TOP_KEYS, set(),
                         "schema 的 /sysinfo.response 顶层键与固件不一致")
        self.assertEqual(self._TOP_KEYS - set(resp.keys()), set(),
                         "schema 的 /sysinfo.response 缺顶层键")
        self.assertEqual(set(resp["nvs"].keys()), self._NVS_KEYS,
                         "schema 的 nvs 子字段与固件不一致")
        self.assertEqual(set(resp["mem"].keys()), self._MEM_KEYS,
                         "schema 的 mem 子字段与固件不一致")

    def test_response_cap_fits_worst_case(self):
        """RESP_CAP 必须容得下按架构上限算出的最坏 sysinfo 响应。

        算法：routes.c 的 SYSINFO_MAX_PARTS / SYSINFO_MAX_TASKS 为条数上限，
        每个整数按 uint32 最宽 10 位十进制、label/name 按 23 字节上限代入。
        """
        import re
        routes = self._read(self._ROUTES_C)
        max_parts = int(re.search(r"SYSINFO_MAX_PARTS\s+(\d+)", routes).group(1))
        max_tasks = int(re.search(r"SYSINFO_MAX_TASKS\s+(\d+)", routes).group(1))
        net = self._read(self._NET_C)
        resp_cap = int(re.search(r"#define\s+RESP_CAP\s+(\d+)", net).group(1))

        W = 10                      # uint32 最大十进制宽度
        head = 454                  # 头部固定部分实测长度（含 "partitions":[ ）
        part = 102                  # 单条分区：23B label + 两个 10 位数字
        task = 58                   # 单条任务：23B name + 10 位数字
        worst = (head + max_parts * part + (max_parts - 1 if max_parts else 0)
                 + len('],"tasks":[') + max_tasks * task
                 + (max_tasks - 1 if max_tasks else 0) + len(']}') + 1)
        self.assertGreaterEqual(
            resp_cap, worst,
            "RESP_CAP=%d 装不下最坏情况 %d 字节 → 真机会返回 500（缓冲区截断）；"
            "调大 RESP_CAP 或下调 SYSINFO_MAX_PARTS/TASKS" % (resp_cap, worst))

    def test_host_stub_covers_invalid_task_branch(self):
        """host 桩必须留一个 valid=0 的任务，覆盖「查不到任务就跳过」分支。"""
        host = self._read(self._HEALTH_HOST)
        self.assertIn("valid=0", host.replace(" ", ""),
                      "host 桩需要一条 valid=0 的假任务，否则该渲染分支无人覆盖")


class TestDeviceJoystickContract(unittest.TestCase):
    """设备端摇杆面板（firmware/main/ui.html）的契约守卫。

    设备端摇杆与控制器端摇杆是**两套独立实现**：
      * 控制器端：`controller/static/index.html` → POST `/api/v1/atom/move|drag|click|anchor`
        （Python 侧 `AtomService` + `MoveCoalescer` 做服务端背压）；
      * 设备端  ：`firmware/main/ui.html` → 直连 `POST /api/v1/gesture`
        （固件**没有** atom/* 路由，故背压只能在浏览器 JS 里重建）。

    最容易出的两类错，都靠这里钉死：
      1. 设备端误调 `/api/v1/atom/*` → 真机必然 404
         （`Nothing matches the given URI`，且它会伪装成"摇杆没反应"）；
      2. 档位表硬编码在 ui.html 里，与 `atoms.py::ATOM_SPEEDS` 漂移 →
         同一个「fine」在两端 step_ms/tick 不同。
    """

    _UI = os.path.join(_ROOT, "firmware", "main", "ui.html")

    @staticmethod
    def _src():
        return _read_file(TestDeviceJoystickContract._UI)

    @staticmethod
    def _code():
        """剥掉 JS/HTML 注释后的源码。

        守卫要回答的是「**代码**有没有调那个端点」；注释里拿
        `/api/v1/atom/*` 当反例讲清楚是**应当的**，不能算命中。
        （同一坑在 C 侧已踩过一次，见 `strip_c_comments` 的 docstring。）

        ⚠️ `//` 规则必须排除 `http://` —— 否则会把 URL 里的 `//` 当行注释
        切掉，静默改变被检查的文本。
        """
        import re
        raw = _read_file(TestDeviceJoystickContract._UI)
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        return re.sub(r"(?<!:)//[^\n]*", "", raw)

    def test_device_ui_has_joystick_panel(self):
        """面板必须实装：自绘摇杆 + 旋钮 + 方向键 + 速度档 + 模式 + 归零。"""
        src = self._src()
        for token in ('id="stick"', 'id="knob"', 'id="dpad"', 'id="speedSeg"',
                      'id="modeSeg"', 'id="anchorBtn"', 'id="dpadClick"'):
            self.assertIn(token, src, "设备端摇杆面板缺少 %s" % token)

    def test_device_ui_never_calls_controller_atom_endpoints(self):
        """🔴 设备端绝不能调 /api/v1/atom/* —— 那是控制器侧端点，真机 404。"""
        src = self._code()
        self.assertNotIn(
            "/api/v1/atom/", src,
            "设备端没有 /api/v1/atom/* 路由（schema 里 owner=controller）。"
            "设备端摇杆必须直连 /api/v1/gesture，否则真机上会返回 "
            "Nothing matches the given URI（表现为「摇杆没反应」）。")
        self.assertIn("/api/v1/gesture", src,
                      "设备端摇杆应直连 POST /api/v1/gesture")

    def test_device_speed_tiers_match_atoms_module(self):
        """ui.html 硬编码的档位表必须与 controller/atoms.py::ATOM_SPEEDS 一致。"""
        import re
        src = self._code()
        m = re.search(r"const\s+SPEEDS\s*=\s*\{(.*?)\n\s*\};", src, re.S)
        self.assertIsNotNone(m, "ui.html 里找不到 SPEEDS 档位表")
        parsed = {}
        for tier, step_ms, tick in re.findall(
                r"(\w+)\s*:\s*\{\s*step_ms\s*:\s*(\d+)\s*,\s*tick\s*:\s*(\d+)",
                m.group(1)):
            parsed[tier] = {"step_ms": int(step_ms), "tick": int(tick)}
        self.assertEqual(
            set(parsed), set(ATOM_SPEEDS),
            "档位名不一致：ui.html=%s / atoms.py=%s"
            % (sorted(parsed), sorted(ATOM_SPEEDS)))
        for k, v in ATOM_SPEEDS.items():
            self.assertEqual(
                parsed[k]["step_ms"], v["step_ms"],
                "档位 %s 的 step_ms 漂移：ui.html=%s / atoms.py=%s（两边要一起改）"
                % (k, parsed[k]["step_ms"], v["step_ms"]))
            self.assertEqual(
                parsed[k]["tick"], v["tick"],
                "档位 %s 的 tick 漂移：ui.html=%s / atoms.py=%s（两边要一起改）"
                % (k, parsed[k]["tick"], v["tick"]))
        m2 = re.search(r"const\s+DEFAULT_SPEED\s*=\s*'(\w+)'", src)
        self.assertIsNotNone(m2, "ui.html 里找不到 DEFAULT_SPEED")
        self.assertEqual(m2.group(1), ATOM_DEFAULT_SPEED,
                         "默认档不一致：ui.html=%s / atoms.py=%s"
                         % (m2.group(1), ATOM_DEFAULT_SPEED))

    def test_device_ui_implements_backpressure(self):
        """设备端必须自建 latest-wins 背压，并处理 503 与 shortfall。

        固件回放是串行的（队列满返回 503），而摇杆按住会持续产生 tick。
        没有背压 → 大量 503 + 松手后设备仍在播旧 tick（「松手还在动」）。
        没有 shortfall 记账 → 回包 echo 请求值会掩盖「少走」（历史坑）。
        """
        src = self._code()
        # ⚠️ 这里刻意用**结构性** regex 而不是 assertIn(token)：只查 token 存在
        #    的守卫会被注释/别处的同名标识符蒙混过关（本项目做过反向验证，
        #    「删掉 exclusive 声明行」就骗过过一版 assertIn 守卫）。
        self.assertRegex(
            src, r"Date\.now\(\)\s*<\s*flyingUntil",
            "缺少 flyingUntil 闸门：没有它就会在上一串手势还在播时继续下发，"
            "无谓地打出大量 503")
        self.assertRegex(
            src, r"function\s+submitMove[\s\S]{0,200}?pending\s*=",
            "submitMove 必须是**覆盖**待发槽（latest-wins），不能改成排队")
        self.assertRegex(
            src, r"status\s*===\s*503",
            "必须显式判断 503（设备侧串行队列满）并保留重试，不能静默丢弃")
        self.assertRegex(
            src, r"exclusive\s*=\s*true",
            "缺少独占门闩：归零/拖拽/点击期间必须挡住摇杆 tick，否则会「点了之后又漂移」")
        self.assertRegex(
            src, r"shortfall",
            "必须按 shortfall/budget 记账实际交付量，不可直接采信回包 echo 的请求值")
        self.assertRegex(
            src, r"cl\(\s*accX|Math\.max\(-?BUDGET_MAX",
            "拖拽累加必须先钳到 ±BUDGET_MAX（25400），越界会被固件截断")

    def test_device_joystick_sensitivity_gain(self):
        """灵敏度总增益 JOY_GAIN：作用在**唯一**的 tickSize()，且三个入口共用它。

        背景（2026-10-03）：真机手感「相对推进太灵敏」，要把实发位移降到原档位的
        20%。做法是加一个总增益而**不动 SPEEDS 表** —— 因为 SPEEDS 必须和
        controller/atoms.py 逐档一致（见 test_device_speed_tiers_match_atoms_module），
        且那些 tick 是真机标定值，直接改写会丢掉标定出处。

        ⚠️ 守卫要防的是「以后有人绕开增益」：
           ① 直接把 SPEEDS.tick 改小 → 上面那条跨端一致性测试会红；
           ② 在 dpadStep / produceTick 里绕开 tickSize() 直接用 `.tick`
              → 方向键和摇杆就会失同步（一个 20% 一个 100%）；
           ③ tooltip 报标定原值不报实发值 → 所见≠所得。
        """
        import re
        src = self._code()

        m = re.search(r"const\s+JOY_GAIN\s*=\s*([0-9.]+)\s*;", src)
        self.assertIsNotNone(m, "设备端 ui.html 缺少灵敏度总增益 JOY_GAIN")
        gain = float(m.group(1))
        self.assertGreater(gain, 0, "JOY_GAIN 必须为正，0 会让摇杆完全不动")
        # 🔴 不得退回 1.0：那等于撤销本次「太灵敏」的修正。若要真恢复原灵敏度，
        #    请**连同本断言一起改**（保留它作为「这是有意为之」的痕迹）。
        self.assertLess(gain, 1,
                        "JOY_GAIN=1 等于退回标定原值（本次真机实测过于灵敏）；"
                        "确实要恢复请连同本断言一起改")

        # tickSize 必须真的把增益乘进去。
        # ⚠️ 别写成 `tick\s*\*\s*JOY_GAIN`（第一版就是这样写的）：一旦有人把中间
        #    变量改名（本项目实现里叫 base），守卫就**误报**。要锚的是「函数体里
        #    同时出现 .tick 与 * JOY_GAIN」这件事本身，而不是某个变量名。
        mfn = re.search(r"function\s+tickSize\(\s*\)\s*\{[\s\S]*?\n\s*\}", src)
        self.assertIsNotNone(mfn, "找不到 tickSize() 函数体")
        body = mfn.group(0)
        self.assertIn(".tick", body, "tickSize() 没有取档位表的 tick 值")
        self.assertRegex(body, r"\*\s*JOY_GAIN",
                         "tickSize() 没有乘 JOY_GAIN —— 加了增益却不起作用")

        # 三个入口必须都走 tickSize()：直接引用 .tick 的地方只允许剩 tooltip
        ticks = [loc for loc in re.finditer(r"\.tick\b", src)]
        for loc in ticks:
            ctx = src[max(0, loc.start() - 90):loc.end() + 40]
            self.assertTrue("JOY_GAIN" in ctx or "tickSize()" in ctx or
                            re.search(r"\{\s*step_ms\s*:\s*\d+\s*,\s*tick\s*:", ctx),
                            "出现了未经增益的 .tick 引用：%r\n"
                            "位移相关的 tick 一律走 tickSize()，否则各入口灵敏度会不一致" % ctx[-70:])

        # tooltip 必须报实发值，不能报标定原值
        mtt = re.search(r"b\.title\s*=[^;]*;", src)
        self.assertIsNotNone(mtt, "找不到速度按钮的 tooltip 赋值")
        self.assertIn("JOY_GAIN", mtt.group(0),
                      "tooltip 应显示**增益后**的实发 counts（报标定原值 = 所见非所得）")

    def test_device_stick_tick_slower_than_firmware_serial_queue(self):
        """tick 周期必须是正数且不比固件单串手势更密（否则无谓打 503）。"""
        import re
        src = self._code()
        m = re.search(r"const\s+STICK_TICK_MS\s*=\s*(\d+)", src)
        self.assertIsNotNone(m, "找不到 STICK_TICK_MS")
        tick = int(m.group(1))
        self.assertGreaterEqual(tick, 40,
                                "tick 周期 %dms 过密：固件手势最快要 4 帧×15ms=60ms" % tick)
        self.assertLessEqual(tick, 500, "tick 周期 %dms 太慢，手感会顿" % tick)


class TestOv5640Support(unittest.TestCase):
    """OV5640 摄像头适配守卫（2026-10-25，A 板换 5MP sensor）。

    背景：A 板摄像头由 OV2640 换成 OV5640 后暴露三处硬编码卡点：
      1) platform_camera_set_framesize 上限写死 13（720p）——OV5640 的
         1080p/UXGA 全部被拒，UI 下拉也没有对应档位；
      2) settings 的 cam_framesize 白名单同样只到 13；
      3) MJPEG 组帧缓冲 128KB——1080p JPEG(q16) 200–350KB 会被 mjpeg_part
         静默丢帧（返回 0，推流黑屏且零报错）。
    另加 OV5640 空闲 standby 降温路径守卫（0x3008[6]，espressif#672 实证方案）。
    每条断言都可变异检验：把对应改动塞回旧形态即红。"""

    def _fw(self, rel):
        return _read_file(os.path.join(_ROOT, "firmware", rel))

    def test_framesize_bound_is_sensor_aware(self):
        """set_framesize 不得写死数字上限，必须走 sensor 感知的 camera_max_framesize()。"""
        import re
        cam = strip_c_comments(self._fw(os.path.join("platform", "esp32", "platform_camera.c")))
        m = re.search(r"int\s+platform_camera_set_framesize\s*\([^)]*\)\s*\{(.*?)\n\}", cam, re.S)
        self.assertIsNotNone(m, "找不到 platform_camera_set_framesize 函数体")
        body = m.group(1)
        self.assertIn("camera_max_framesize()", body,
                      "framesize 上限必须 sensor 感知（OV2640=UXGA / OV5640=QSXGA），不得写死")
        self.assertNotRegex(body, r"size\s*>\s*13",
                            "又回到写死 13 的老路：OV5640 高分辨率会被这个上限挡死")
        # 上限查询实现本身必须读 sensor 表
        self.assertIn("esp_camera_sensor_get_info", cam,
                      "camera_max_framesize 必须经 esp_camera_sensor_get_info 查表")

    def test_settings_whitelist_covers_high_res(self):
        """/api/v1/settings 的 cam_framesize 白名单必须含 15(UXGA)/16(FHD)。"""
        routes = strip_c_comments(self._fw(os.path.join("main", "routes.c")))
        import re
        m = re.search(r'json_find_int\([^)]*"?cam_framesize"[^)]*\)', routes)
        self.assertIsNotNone(m, "routes.c 未解析 cam_framesize")
        m2 = re.search(r"has_fs\s*&&[^\n]*", routes)
        self.assertIsNotNone(m2, "找不到 cam_framesize 校验行")
        line = m2.group(0)
        for v in ("fs==15", "fs==16"):
            self.assertIn(v, line,
                          "cam_framesize 白名单缺 %s：高分辨率设不了默认值" % v)

    def test_stream_part_buf_fits_1080p(self):
        """MJPEG 组帧缓冲必须 >= 512KB，否则 1080p 帧被 mjpeg_part 静默丢弃。"""
        import re
        net = strip_c_comments(self._fw(os.path.join("platform", "esp32", "platform_net.c")))
        m = re.search(r"#define\s+STREAM_PART_CAP\s+\((\d+)\s*\*\s*1024\s*\+\s*(\d+)\)", net)
        self.assertIsNotNone(m, "找不到 STREAM_PART_CAP 定义")
        kb = int(m.group(1))
        self.assertGreaterEqual(kb, 512,
                                "STREAM_PART_CAP=%dKB < 512KB：1080p JPEG(q16) 会被静默丢帧" % kb)

    def test_ov5640_standby_path_present(self):
        """空闲 standby 降温：0x3008[6] 写入 + 仅 OV5640 生效的门控都必须存在。"""
        import re
        cam = strip_c_comments(self._fw(os.path.join("platform", "esp32", "platform_camera.c")))
        self.assertIn("0x3008", cam, "缺 OV5640 standby 寄存器 0x3008 的写入")
        m = re.search(r"set_reg\s*\(\s*s\s*,\s*0x3008\s*,\s*0x40", cam)
        self.assertIsNotNone(m, "standby 必须写 0x3008 的 bit6(0x40)（espressif#672 实证值）")
        self.assertIn("OV5640_PID", cam,
                      "standby 必须按 PID 门控：OV2640 的 standby 唤醒是已知硬件缺陷，不能碰")

    def test_ui_has_high_res_options(self):
        """UI 两处分辨率下拉都必须有 15/16 档。"""
        ui = self._fw(os.path.join("main", "ui.html"))
        for sel in ('id="adjRes"', 'id="cfgRes"'):
            i = ui.find(sel)
            self.assertGreater(i, 0, "ui.html 缺少 %s" % sel)
            block = ui[i:i + 700]
            self.assertIn('value="15"', block, "%s 缺 UXGA(1600x1200) 档" % sel)
            self.assertIn('value="16"', block, "%s 缺 1080p 档" % sel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
