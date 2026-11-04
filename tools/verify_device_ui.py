# -*- coding: utf-8 -*-
"""真机验证设备端 Web UI 的**版式**（firmware/main/ui.html 烧进真机后的样子）。

为什么需要它：版式是「CSS grid 两列 + DOM 顺序」实现的，**配对关系完全由
DOM 顺序决定**，而顺序错了在源码评审里看不出来（CSS 看不出、JS 也看不出），
只有把真机吐出来的 HTML 抓回来逐项比对才靠得住。同理，「放大」控件是不是
真的内嵌进了固件，也只有从设备取页面才能证明（本地文件改了≠烧进去了）。

覆盖：
  1. GET / 返回 200，且是**新**版页面（含放大控件）；
  2. 10 张卡片按两列配对的顺序出现：
       设备状态|设备信息 / 远程画面|相对推进 / 画面调节|点击·滑动 /
       键盘输入|发送文本 / 手势回放|响应日志
  3. .cards 的两列 grid + 窄屏回退媒体查询都在；
  4. 各面板控件 id 齐备（摇杆 / 画面调节 / 键盘 / 日志）；
  5. 放大真的乘进了 transform 的 scale（不是只放了个滑块）；
  6. 设备端**没有**引用控制器端点 /api/v1/atom/*。

用法：
    python tools/verify_device_ui.py [设备IP或主机名]
退出码 0 = 全通过。
"""
import json                     # noqa: F401  (保持与其它 verify 脚本一致的导入面)
import re
import socket
import sys
import time
import urllib.error
import urllib.request

DEVICES = ["10.59.228.53", "qshou-0a0c.local", "qshou-0a0c"]

# 期望的两列配对（每行两个）。⚠️ 与 tests/test_preview_adjust.py::
# DeviceLayoutTestCase.EXPECTED_ROWS **必须一致** —— 那份是源码级守卫，
# 这份是真机级守卫，改版式要同时改两处。
EXPECTED_ROWS = [
    ("设备状态", "设备信息"),
    ("远程画面", "相对推进"),
    ("画面调节", "点击 / 滑动"),
    ("键盘输入", "发送文本"),
    ("手势回放", "响应日志"),
    ("设备配置", "网络配置"),
]

# 面板必备控件 id
PANEL_IDS = [
    # 摇杆
    "stick", "knob", "dpad", "speedSeg", "modeSeg", "anchorBtn", "dpadClick",
    # 画面调节（含本次新增的放大）
    "adjRot", "adjMirror", "adjFlip", "adjZoom", "adjZoomV",
    "adjExp", "adjCon", "adjSat", "adjExpV", "adjConV", "adjSatV", "adjReset",
    # 画面调节·分辨率运行时切换下拉框（设备端独有，控制器无）
    "adjRes",
    # 画面 / 键盘 / 文本 / 日志
    "cam", "streambtn", "txt", "log", "dist", "distv",
    # 防休眠 keep-awake（2026-10-03 新增，位于「点击 / 滑动」卡片）
    "kaBtn", "kaState", "kaNext", "kaSec", "kaSecv",
    # 画面调节·自动功能开关（2026-10-03 新增，仅设备端 ui.html，控制器侧无 → 不放共享 REQUIRED_IDS）
    "camAuto", "adjAutoExp", "adjAutoWb", "adjAutoGain", "adjAutoAll",
    # 状态遥测
    "ble", "rssi", "uptime", "heap", "frame", "reset", "gest",
    # 设备信息
    "siNvs", "siHeap", "siPsram", "siParts", "siTasks",
    # 手势回放
    "gtype", "steps", "stepsv", "ms", "msv",
    # 设备配置 / 网络配置（2026-10-09 新增：BLE名/VID/PID/分辨率/质量/mDNS/SSID/密码/RESP_CAP/max_uri/保存）
    "cfgBleName", "cfgVid", "cfgPid", "cfgRes", "cfgQuality",
    "cfgMdns", "cfgWifiSsid", "cfgWifiPass", "cfgRespCap", "cfgMaxUri", "cfgSaveBtn",
]


def strip_js_comments(s):
    """剥掉 JS/HTML 注释再查端点引用（注释里会拿 /api/v1/atom/* 当反例讲解）。"""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", s)


def req(url, timeout=15, retries=3):
    """GET 请求（幂等，可安全重试）。

    ⚠️ 必须重试：Windows 上 `.local` 的 mDNS 解析是**间歇性**失败的
    （实测 gethostbyname 报 getaddrinfo failed，而 urlopen 偶尔又能通）。
    ⚠️ 重试之间要 sleep：本机 httpd 的 fd 上限很小（lwIP 默认 10）且只有
    一个任务，密集超时会让连接滞留不回收 → `accept (23)` ENFILE，
    此后所有连接被拒（实测踩过）。
    """
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:               # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
    raise last


def find_device():
    """返回可用基址，并尽量固化成一个 IPv4（`.local` 解析不稳）。"""
    for d in DEVICES:
        try:
            st, _ = req("http://%s/api/v1/health" % d, timeout=4, retries=1)
        except Exception:                    # noqa: BLE001
            continue
        if st != 200:
            continue
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d):
            return d
        try:
            ip = socket.gethostbyname(d)
            print("  (%s 解析为 %s，后续统一用 IP)" % (d, ip))
            return ip
        except OSError:
            print("  (警告：%s 无法解析成 IP，后续可能间歇失败)" % d)
            return d
    return None


def main():
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            DEVICES.insert(0, a)
            print("(使用命令行指定的设备地址: %s)" % a)
            break

    errs = []

    def ck(cond, msg):
        print(("  ok   " + msg) if cond else ("  FAIL " + msg))
        if not cond:
            errs.append(msg)

    print("== 等待设备上线 ==")
    dev = None
    for _ in range(40):
        dev = find_device()
        if dev:
            break
        time.sleep(3)
    if not dev:
        print("FAIL 找不到设备（检查是否与设备同网段）")
        return 1
    base = "http://%s" % dev
    print("设备: %s" % base)

    # ---- 1. 页面可取且是新版 ----
    print("\n== 1. GET / 是否已内嵌新版页面 ==")
    st, html = req(base + "/")
    ck(st == 200, "GET / -> HTTP %d" % st)
    ck(len(html) > 8000, "页面 %d 字节" % len(html))
    ck('id="adjZoom"' in html, "页面含放大控件 id=adjZoom（证明新 ui.html 已烧进固件）")
    ck('class="cards"' in html, "页面含两列容器 .cards")

    # ---- 2. 卡片顺序 = 两列配对 ----
    print("\n== 2. 卡片顺序（两列配对，DOM 顺序即视觉顺序）==")
    body = html.split("<body>")[1].split("<script>")[0]
    titles = [re.sub(r"（.*?）", "", t).strip()
              for t in re.findall(r"<h2>(.*?)</h2>", body)]
    expected = [t for row in EXPECTED_ROWS for t in row]
    ck(titles == expected,
       "卡片顺序与配对一致，共 %d 张" % len(titles))
    if titles != expected:
        print("       实际: %s" % titles)
        print("       期望: %s" % expected)
    for i, (l, r) in enumerate(EXPECTED_ROWS):
        ck(i * 2 + 1 < len(titles) and titles[i * 2] == l and titles[i * 2 + 1] == r,
           "第 %d 行配对：%s | %s" % (i + 1, l, r))

    # ---- 3. 两列 CSS 与窄屏回退 ----
    print("\n== 3. 两列 grid 与窄屏回退 ==")
    ck(re.search(r"\.cards\s*\{[^}]*grid-template-columns:repeat\(2", html) is not None,
       ".cards 定义为两列 grid")
    ck(re.search(r"@media\s*\(max-width:\d+px\)\s*\{\s*\.cards\{[^}]*grid-template-columns:1fr",
                 html) is not None,
       "存在窄屏回退为单列的媒体查询")

    # ---- 4. 控件 id 齐备 ----
    print("\n== 4. 面板控件 id ==")
    missing = [i for i in PANEL_IDS if ('id="%s"' % i) not in html]
    ck(not missing, "控件 id 齐备%s" % ("" if not missing else "，缺: %s" % missing))

    # ---- 5. 放大真的接进 transform ----
    print("\n== 5. 放大功能实现 ==")
    code = strip_js_comments(html)
    ck(re.search(r"const\s+z\s*=\s*\(\s*st\.zoom\s*\|\|\s*\d+\s*\)\s*/", code) is not None,
       "放大系数由 st.zoom 推导（const z=(st.zoom||100)/100）")
    ck(re.search(r"scale\(['\"]?\+?\(?fit\*mx\*z", code) is not None,
       "放大乘进了 scale（形如 scale(fit*mx*z, fit*my*z)）")
    m = re.search(r"els\.zoom\.addEventListener\(['\"]input['\"][\s\S]*?\}\);", code)
    ck(m is not None and "applyGeom()" in m.group(0),
       "放大滑块触发 applyGeom()")
    ck(m is not None and "/api/v1/camera" not in m.group(0),
       "放大未去驱动传感器（纯本地预览变换）")

    # ---- 6. 摇杆面板仍在 + 不引用控制器端点 ----
    print("\n== 6. 摇杆面板与端点约束 ==")
    for t in ("相对推进", "latest-wins", "/api/v1/gesture"):
        ck(t in html, "含 %s" % t)
    ck("/api/v1/atom/" not in code,
       "未引用 /api/v1/atom/*（设备端无此路由，引用即 404）")

    # ---- 7. 拉取式预览（方案 B）已生效 ----
    print("\n== 7. 拉取式预览（方案 B）==")
    ck('id="pullFps"' in html,
       "存在刷新率滑块 id=pullFps（设备端预览由前端主动拉取）")
    ck("togglePull" in code,
       "存在 togglePull() 开关（替代旧 MJPEG 推流 toggleStream）")
    ck("fetch('/snapshot" in code or 'fetch("/snapshot' in code,
       "预览循环通过 fetch('/snapshot') 主动拉取最新帧")
    ck("STREAM_PORT" not in code and "streamURL()" not in code,
       "旧 MJPEG 推流模型（STREAM_PORT / streamURL）已从设备端移除")

    # ---- 8. 分辨率运行时切换下拉框（设备端独有，免重烧）----
    print("\n== 8. 分辨率下拉框（运行时切换）==")
    ck('id="adjRes"' in html,
       "存在分辨率下拉框 id=adjRes")
    ck(re.search(r'<option value="6"[^>]*>QVGA', html) is not None,
       "含 QVGA 320x240 选项（value=6）")
    ck(re.search(r'<option value="10"[^>]*>VGA', html) is not None,
       "含 VGA 640x480 选项（value=10，默认）")
    ck(re.search(r'<option value="12"[^>]*>XGA', html) is not None,
       "含 XGA 1024x768 选项（value=12）")
    ck(re.search(r'<option value="13"[^>]*>720p', html) is not None,
       "含 720p 1280x720 选项（value=13）")
    ck("frame_size" in strip_js_comments(html),
       "切换分辨率把 frame_size 发往 /api/v1/camera（非纯本地）")

    print("\n" + "=" * 62)
    if errs:
        print("FAIL：%d 项未通过" % len(errs))
        for e in errs:
            print("   - %s" % e)
        return 1
    print("PASS：设备端 UI 版式（两列配对 + 放大 + 控件齐备）真机核对全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
