# -*- coding: utf-8 -*-
"""
test_preview_adjust.py — 预览画面调节面板（纯客户端 CSS）的静态守卫。

为什么是静态守卫而不是跑浏览器：
  - 调节逻辑全部是「预览端 CSS filter/transform」，不依赖固件、不产生 HTTP 流量，
    真实行为需在浏览器里看，无法在 unittest 里断言像素。
  - 真正会悄悄坏掉的是「面板没接上 / id 拼错 / 关键逻辑被误删」——
    这些用文件内容断言即可 100% 拦住，且零依赖、秒级。

覆盖：
  1. 设备端 ui.html 与控制器 index.html 都含完整面板（旋转/曝光/对比度/饱和度/镜像/翻转/重置）。
  2. 关键实现细节在位：localStorage 持久化、CSS filter 三件套、cam-adjust 同步类（控制器标定层）。
  3. 预览元素存在（设备 #cam / 控制器 #screen + .cam-adjust-sync 的 overlay）。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_HTML = os.path.join(ROOT, "firmware", "main", "ui.html")
CTRL_HTML = os.path.join(ROOT, "controller", "static", "index.html")

# 面板必备控件 id（两文件共用同一套 id）
REQUIRED_IDS = [
    "adjRot", "adjExp", "adjCon", "adjSat",
    "adjMirror", "adjFlip", "adjExpV", "adjConV", "adjSatV", "adjReset",
]
# ⚠️ 「放大」目前**只有设备端**有（controller/static/index.html 未加）。
# 故它不进 REQUIRED_IDS —— 否则 test_controller_ui_has_full_panel 会红。
# 若将来给控制器也补上，把两个 id 移进 REQUIRED_IDS 并把下面
# DeviceLayoutTestCase.test_zoom_control_present 删掉即可。
DEVICE_ONLY_IDS = ["adjZoom", "adjZoomV", "adjRes"]
# 防休眠 keep-awake（2026-10-03 新增，只存在于**设备端** ui.html，
# 控制器侧没有 → 必须放 DEVICE_ONLY 列表，不能进 REQUIRED_IDS，
# 否则 test_controller_ui_has_full_panel 会红）。
KEEPAWAKE_IDS = ["kaBtn", "kaState", "kaNext", "kaSec", "kaSecv"]
# 旋转四档必须齐
REQUIRED_ROT = ["0", "90", "180", "270"]


class PreviewAdjustTestCase(unittest.TestCase):

    def _load(self, path):
        self.assertTrue(os.path.exists(path), "文件缺失: %s" % path)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_device_ui_has_full_panel(self):
        html = self._load(UI_HTML)
        for i in REQUIRED_IDS:
            self.assertIn('id="%s"' % i, html, "设备端缺少控件 id=%s" % i)
        for r in REQUIRED_ROT:
            self.assertIn('data-rot="%s"' % r, html, "设备端缺少旋转档 %s°" % r)
        # 预览元素
        self.assertIn('id="cam"', html, "设备端预览 <img id=cam> 缺失")

    def test_controller_ui_has_full_panel(self):
        html = self._load(CTRL_HTML)
        for i in REQUIRED_IDS:
            self.assertIn('id="%s"' % i, html, "控制器缺少控件 id=%s" % i)
        for r in REQUIRED_ROT:
            self.assertIn('data-rot="%s"' % r, html, "控制器缺少旋转档 %s°" % r)
        self.assertIn('id="screen"', html, "控制器预览 <img id=screen> 缺失")
        # 标定层须带同步类，旋转时才与画面对齐
        self.assertIn('id="overlay" class="cam-adjust-sync"', html,
                      "控制器 overlay 未加 cam-adjust-sync 同步类")

    def test_adjust_script_logic_present(self):
        for path in (UI_HTML, CTRL_HTML):
            html = self._load(path)
            with self.subTest(path=os.path.basename(path)):
                # localStorage 持久化
                self.assertIn("localStorage", html, "缺少 localStorage 持久化")
                self.assertIn("qshou-cam-adjust-v2", html, "缺省存储 key 未落地")
                # CSS filter 三件套
                self.assertIn("brightness(", html, "缺少 brightness 滤镜")
                self.assertIn("contrast(", html, "缺少 contrast 滤镜")
                self.assertIn("saturate(", html, "缺少 saturate 滤镜")
                # 旋转 transform + 90/270 自适应缩放防溢出
                self.assertIn("rotate(", html, "缺少 rotate 变换")
                self.assertIn("fit", html, "缺少 90/270 自适应缩放逻辑")
                # 复位
                self.assertIn("reset", html, "缺少重置逻辑")


class DeviceLayoutTestCase(unittest.TestCase):
    """设备端 ui.html 的**版式**守卫：两列排布 + 卡片配对顺序 + 放大控件。

    为什么要钉顺序：布局是「CSS grid 两列 + DOM 顺序即视觉顺序」实现的，
    所以**配对关系完全由 DOM 顺序决定**（第 1/2 张一行、第 3/4 张一行……）。
    重排卡片时若只改视觉不动 DOM，配对就会静默错位 —— 这种错没人会从
    代码评审里看出来，只有真机截图能发现，故在此用断言固定住。
    """

    #: 期望的两列配对（每行两个），按 DOM 顺序展平
    EXPECTED_ROWS = [
        ("设备状态", "设备信息"),
        ("远程画面", "相对推进"),
        ("画面调节", "点击 / 滑动"),
        ("键盘输入", "发送文本"),
        ("手势回放", "响应日志"),
        ("设备配置", "网络配置"),
    ]

    def _html(self):
        with open(UI_HTML, encoding="utf-8") as f:
            return f.read()

    def test_two_column_grid(self):
        html = self._html()
        self.assertIn('class="cards"', html, "卡片未包进 .cards 容器，两列排布无从生效")
        self.assertRegex(
            html, r"\.cards\s*\{[^}]*grid-template-columns:repeat\(2",
            "缺少 .cards 的两列 grid 定义")
        self.assertRegex(
            html, r"@media\s*\(max-width:\d+px\)\s*\{\s*\.cards\{[^}]*grid-template-columns:1fr",
            "缺少窄屏回退单列的媒体查询（手机上会挤成两列）")

    def test_card_pairs_and_order(self):
        """DOM 顺序必须等于期望的两列配对顺序（左→右、上→下）。"""
        import re
        html = self._html()
        # 只取 <body> 到首个 <script> 之间的卡片标题，避免匹配到 JS 里的字符串
        body = html.split("<body>")[1].split("<script>")[0]
        titles = [re.sub(r"（.*?）", "", t).strip()
                  for t in re.findall(r"<h2>(.*?)</h2>", body)]
        expected = [t for row in self.EXPECTED_ROWS for t in row]
        self.assertEqual(
            titles, expected,
            "卡片顺序与两列配对不符。\n实际: %s\n期望: %s\n"
            "（两列布局下 DOM 顺序 = 视觉顺序，改顺序必须同时改这里）"
            % (titles, expected))
        self.assertEqual(len(titles) % 2, 0, "卡片数应为偶数，否则最后一行会缺一格")

    def test_precision_step_buttons_present(self):
        """画面调节四个滑块（放大/曝光/对比度/饱和度）必须各带 - / + 微调按钮。
        这是设备端独有控件（控制器 index.html 的预览面板尚未同步，故只钉设备端）。"""
        html = self._html()
        for sid in ("adjZoom", "adjExp", "adjCon", "adjSat"):
            self.assertIn(
                'class="adj-step" data-id="%s" data-d="-1"' % sid, html,
                "%s 缺少「减小」微调按钮" % sid)
            self.assertIn(
                'class="adj-step" data-id="%s" data-d="1"' % sid, html,
                "%s 缺少「增大」微调按钮" % sid)

    def test_resolution_dropdown_present(self):
        """分辨率下拉框（设备端独有，控制器未同步）。运行时切换、免重烧。

        这是设备端独有控件（控制器 index.html 的预览面板尚未同步，故只钉设备端）。"""
        html = self._html()
        self.assertIn('id="adjRes"', html, "设备端缺少分辨率下拉框 id=adjRes")
        for v, label in (("6", "QVGA"), ("9", "480×320"), ("10", "VGA"),
                         ("11", "SVGA"), ("12", "XGA"), ("13", "720p")):
            self.assertRegex(html, r'<option value="%s"[^>]*>%s' % (v, label),
                             "分辨率下拉缺 %s 选项(value=%s)" % (label, v))
        code = self._code(html)
        self.assertRegex(code, r"frame_size\s*:\s*st\.res",
                         "分辨率切换未把 frame_size 发往 /api/v1/camera")

    @staticmethod
    def _code(html):
        """剥掉 JS/HTML 注释后的源码。

        🔴 **凡是「有没有出现某字符串」的守卫，都必须先剥注释。**
        这个坑在本项目已经踩到第三次了（C 侧 `strip_c_comments`、
        `test_schema._code`、现在的设备端放大守卫）——原因都一样：
        注释里为了讲清楚「不该做什么」会**原样写出那个字符串**，
        于是负向断言（assertNotIn / assertNotRegex）被自己的注释打红。
        ⚠️ `//` 规则必须排除 `http://`，否则会把 URL 里的 `//` 当行注释切掉。
        """
        import re
        s = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        return re.sub(r"(?<!:)//[^\n]*", "", s)

    def test_zoom_control_present(self):
        """「放大」控件必须存在，且真的乘进 transform 的 scale。"""
        html = self._html()
        code = self._code(html)
        for i in DEVICE_ONLY_IDS:
            self.assertIn('id="%s"' % i, html, "设备端缺少放大控件 id=%s" % i)
        # ⚠️ 这里刻意用**结构性** regex 而不是 assertIn("st.zoom")：后者会被
        #    别处的 st.zoom（syncUI / 事件回调 / 重置）蒙混过关 —— 反向
        #    验证实测过，「把放大系数改成常量 z=1」能骗过 assertIn 版本。
        self.assertRegex(
            code, r"const\s+z\s*=\s*\(\s*st\.zoom\s*\|\|\s*\d+\s*\)\s*/",
            "放大系数未从 st.zoom 推导（应形如 const z=(st.zoom||100)/100）："
            "把 z 写成常量就等于滑块只动数字、画面不动")
        self.assertRegex(
            code, r"scale\(['\"]?\+?\(?fit\*mx\*z",
            "放大未乘进 scale（应形如 scale(fit*mx*z, fit*my*z)）："
            "只改滑块数值不改变换等于没实现")
        # 放大属于本地预览变换，绝不能去驱动传感器（那会白白改掉整路推流的曝光）。
        # 必须**限定在放大处理器内部**查 —— 全文件向前匹配会漏掉插在处理器
        # 之外的调用（反向验证实测），向后匹配又会被「zoom 是本地变换」这类
        # 说明性注释误伤（本项目第三次踩注释误报）。
        m = re.search(r"els\.zoom\.addEventListener\(['\"]input['\"][\s\S]*?\}\);", code)
        self.assertIsNotNone(m, "找不到放大的 input 事件处理器")
        self.assertIn("applyGeom()", m.group(0),
                      "放大滑块必须触发 applyGeom()（否则画面不会变）")
        self.assertNotIn("/api/v1/camera", m.group(0),
                         "放大不该走 /api/v1/camera —— 它是纯客户端 CSS 变换")
        self.assertNotIn("applySensor", m.group(0),
                         "放大不该触发 applySensor() —— 它不是传感器参数")

    def test_keepawake_contract(self):
        """防休眠：控件齐备，且抖动必须是「纯 dir 移动 + 净零 + 错误不重发」。

        ⚠️ 负向断言一律在**剥注释后**的代码上做，并**限定在 kaJiggle 函数体内**：
        本功能的 JS 注释为了讲清「为什么不能用 tap」「为什么不能重发」会把
        `type:'tap'` 这类串原样写出来 —— 不剥注释必被自己的注释打红（本项目
        第三次踩）。同理，不做作用域限定会被页面别处的 tapInPlace 误伤。
        """
        html = self._html()
        code = self._code(html)
        for i in KEEPAWAKE_IDS:
            self.assertIn('id="%s"' % i, html, "设备端缺少防休眠控件 id=%s" % i)
        self.assertRegex(html, r'onclick="toggleKeepAwake\(\)"',
                         "防休眠按钮没挂上 toggleKeepAwake()（点了也不会生效）")

        m = re.search(r"async\s+function\s+kaJiggle\(\s*\)\s*\{[\s\S]*?\n\}", code)
        self.assertIsNotNone(m, "找不到 kaJiggle 函数体")
        fn = m.group(0)
        self.assertRegex(fn, r"type:'dir'", "抖动必须是 dir 相对移动")
        self.assertNotRegex(fn, r"type:'tap'",
                            "抖动绝不能用 tap —— tap 会真的按下抬起，等于一次点击，会误触对方 UI")
        self.assertRegex(fn, r"dx:KA_JITTER", "缺少正向抖动帧")
        self.assertRegex(fn, r"dx:-KA_JITTER", "缺少反向回位帧（不回位=光标持续漂移）")
        # ⚠️ 必须**取 503 分支自身**再判有没有 return。
        #    第一版写成 `503[\s\S]{0,120}?return`（"后面 120 字符内有没有 return"），
        #    反向验证实测 MISS：删掉 return 后，后面别的错误分支里的 return
        #    照样把它蒙混过关。切记：容错逻辑的断言要锚定分支，不能锚定邻近下文。
        m503 = re.search(r"r1\.status\s*===\s*503\s*\)\s*\{[^}]*\}", fn)
        self.assertIsNotNone(m503, "缺少 503（设备忙）处理分支")
        self.assertRegex(m503.group(0), r"\breturn\b",
                         "503 分支必须以 return 结束 —— 要**跳过本轮**，"
                         "继续往下发会造成重复下发、多走一段并永久失同步")
        self.assertRegex(fn, r"finally\s*\{", "kaBusy 必须用 try/finally 复位（异常会永久卡住）")
        self.assertRegex(code, r"clearInterval\(kaTick\);\s*kaTick\s*=\s*null;",
                         "toggle 关闭时必须清理定时器（否则关了还在发）")
        # 🔴 范围必须是 0.3s~300s 且**两端都被夹住**：下界一旦失守（<=0），
        #    setInterval 会退化成空转连续下发 → 两轮重叠 → ENFILE 打挂 httpd。
        self.assertRegex(code, r"KA_MIN\s*=\s*0\.3,\s*KA_MAX\s*=\s*300", "缺少 0.3~300s 的范围常量")
        self.assertRegex(code, r"Math\.min\(KA_MAX,\s*Math\.max\(KA_MIN,",
                         "kaSecSnap 必须双向夹紧 [KA_MIN, KA_MAX]")
        self.assertRegex(code, r"Math\.pow\(KA_MAX / KA_MIN",
                         "滑块必须是**对数刻度**（线性刻度下 0.3~5s 只占 1.6% 行程，拖不准）")
        # 🔴 排班必须锚「计划起点 + 间隔」：改成"此刻 + 间隔"会把往返时间叠加进
        #    周期（0.3s 设定实际变成 ~0.5s），属静默降频 —— 反向验证 M1 已钉死。
        self.assertRegex(code, r"Math\.max\(startAt \+ kaSec\(\) \* 1000,\s*Date\.now\(\)\)",
                         "下一轮必须排在「计划起点+间隔」并钳到现在（周期 = max(设定, 往返)）")
        self.assertRegex(code, r"kaDue\s*=\s*Infinity",
                         "本轮在飞期间必须封住 kaDue（否则心跳会在成败未判时再发一轮）")
        # 本功能是纯前端定时器，不得引入任何新端点（只允许复用既有 gesture）
        calls = set(re.findall(r"(?:api|fetch)\('(/api/v1/[a-z_]+)", code))
        for p in sorted(calls):
            self.assertIn(p, ("/api/v1/camera", "/api/v1/gesture", "/api/v1/health",
                              "/api/v1/key", "/api/v1/sysinfo", "/api/v1/settings"),
                          "出现了固件契约外的新端点 %s（需同步改 routes.c 与 ROUTES[]）" % p)

    def test_pull_preview_model(self):
        """方案 B：设备 UI 改为前端主动拉取 /snapshot，并带刷新率控制。
        断言：存在 pullFps 滑块、togglePull 开关、pullLoop 拉取循环，
        且页面不再硬编码 :81 推流（STREAM_PORT / streamURL）。"""
        html = self._html()
        self.assertIn('id="pullFps"', html, "缺少刷新率滑块 id=pullFps")
        self.assertRegex(html, r'function\s+togglePull\s*\(', "缺少拉取开关 togglePull")
        self.assertRegex(html, r'function\s+pullLoop\s*\(', "缺少拉取循环 pullLoop")
        self.assertIn('/snapshot', html, "预览应拉取 /snapshot")
        self.assertRegex(html, r"fetch\('/snapshot", "预览循环未实际 fetch /snapshot")
        # 设备端预览不再依赖 :81 推流（固件中 :81/stream 仅留作控制器兼容）
        self.assertNotIn('STREAM_PORT', html, "设备UI不应再硬编码 :81 推流端口")
        self.assertNotIn('streamURL', html, "设备UI不应再保留 MJPEG 推流 URL 构造")


if __name__ == "__main__":
    unittest.main(verbosity=2)
