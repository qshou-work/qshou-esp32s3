#!/usr/bin/env python3
"""真机验证设备端摇杆面板（firmware/main/ui.html 的「相对推进」卡片）。

不止验证「页面里有摇杆」，还要证明**手势真的发得出去、且按预算交付**：

  1. `GET /` 返回的 HTML 必须含摇杆面板元素（证明新 ui.html 已内嵌并生效），
     且**代码**里不得引用 `/api/v1/atom/*`（那是控制器侧端点，真机 404）；
  2. 实发 dir 手势，用 `/api/v1/health` 的 `gest_dx/gest_dy` 对账**实际净位移**；
  3. 大位移（归零场景）必须被固件**自适应展开**帧数（steps 涨上去、shortfall=0），
     而不是「少走却谎报成功」—— 这是历史坑，固件已修，这里做回归验证；
  4. tap 的净位移必须精确为 (0,0)（1px 净零抖动锚定）；
  5. BLE 必须在线；
  6. 密集连发鲁棒性：长手势回放期间连发 7 条，**每条都必须得到明确响应**
     （200/503），不得超时或连接错误 —— 证明设备在 BLE 高密度回放下仍能
     服务 HTTP（这正是前端敢在回放期间继续发请求的前提）。

⚠️ health 的 `gest_*` 是「最近一次手势」遥测、**每次覆盖写**（非累计），
   故必须在目标手势之后**立刻**读一次，中间不要插别的手势。

🔴 为什么**不**断言「密集连发必然出现 503」：`GEST_QUEUE_LEN=4` 的队列上限
   确实存在（`platform_ble.c`），但真机上**几乎不可能把它填满** —— 单条
   4 帧 × 60ms 的手势只要 360ms 播完，而一次 HTTP 往返本身就慢于这个量级，
   worker 早已取走上一条，队列始终只有 0~1 条在排队。
   实测连发**全部 200**，一条 503 都没有。把「必须 503」写成硬断言
   会得到一个随 RF 环境闪烁的脆弱测试，故改为上面的鲁棒性断言。

⚠️ 另一层真机现象：**BLE 高密度回放期间（197 帧 × 15ms 归零手势），WiFi/BLE
   共存仲裁会把 HTTP 请求拖慢甚至拖到超时** —— 实测出现过 `TimeoutError`。
   这也是前端为什么要在归零/拖拽期间立起 `exclusive` 独占门闩、完全不发请求。

🔴🔴 **测试时务必克制**（本脚本已按此加固）：ESP-IDF httpd 只有**一个任务**，
   而 lwIP 的 socket fd 上限很小（默认 10）。客户端密集超时 → 连接滞留不回收
   → `httpd_accept_conn: error in accept (23)`（ENFILE）→ **之后所有连接都被
   拒**（`Remote end closed connection without response`），必须**复位设备**才
   恢复。现象极具误导性：看起来像"设备挂了 / 固件 bug"，其实是测试打爆的。
   → 故：GET 幂等可重试、POST 不因超时重试、重试间必须 sleep、连发要留间隔。
   ⚠️ 正常使用（浏览器轮询 + 推流）不会触发：浏览器会正常关闭连接且节奏温和。
"""
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request

DEVICES = ["10.59.228.53", "qshou-0a0c.local", "qshou-0a0c"]
GEST = "/api/v1/gesture"

# 固件侧回放队列深度（firmware/platform/esp32/platform_ble.c 的 GEST_QUEUE_LEN）。
# ⚠️ 不是 1：播放是**串行**的（单 worker 顺序消费），但允许**积压 4 条**，
#    第 5 条才 503。所以「连发必 503」是错的断言 —— 必须按真实深度判。
GEST_QUEUE_LEN = 4

# 页面里必须出现的摇杆面板特征（对应 ui.html 的「相对推进」卡片）
UI_TOKENS = [
    'id="stick"', 'id="knob"', 'id="dpad"', 'id="speedSeg"',
    'id="modeSeg"', 'id="anchorBtn"', 'id="dpadClick"',
    '相对推进', 'latest-wins',
]


def strip_js_comments(s):
    """剥掉 JS/HTML 注释后再查端点引用。

    注释里拿 `/api/v1/atom/*` 当**反例**讲清楚是应当的（"设备端没有这个端点"），
    不能算「页面引用了它」。同一坑在源码级契约守卫里也踩过一次，是同一类错误。
    ⚠️ `//` 规则必须排除 `http://`，否则会误切 URL。
    """
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", s)


def req(url, body=None, timeout=15, retries=3):
    """发一次请求（POST 需传 body）。

    🔴 重试策略按**幂等性**区分，这是被真机打出来的教训：
       * **GET 幂等** → 超时/连接错都可安全重试。Windows 上 `.local` 的 mDNS
         解析是间歇性失败的（实测 `gethostbyname` 报 getaddrinfo failed、
         `urlopen` 偶尔又能通），不重试会让脚本跑到一半炸掉。
       * **POST 绝不因超时重试** → 请求可能已经送达并开始回放，重试会让手势
         **重复下发**（多走一段），比失败更糟。只有「尚未送出」（DNS 解析失败）
         才重试。
    🔴 重试之间必须 sleep：本机 httpd 的连接 fd 上限很小（lwIP 默认 10 个），
       而 httpd 只有**一个任务** —— 密集超时会让连接滞留不回收，最终
       `httpd_accept_conn: error in accept (23)`（ENFILE，套接字耗尽），
       此后**所有**连接都被拒（`Remote end closed connection`）。实测踩过。
    """
    last = None
    for attempt in range(retries):
        r = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            headers={} if body is None else {"Content-Type": "application/json"},
            method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:                     # noqa: BLE001  URLError/Timeout/OSError
            last = e
            dns_fail = isinstance(getattr(e, "reason", None), socket.gaierror)
            if attempt < retries - 1 and (body is None or dns_fail):
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
    raise last


def find_device():
    """返回可用基址，并**尽量固化成一个 IPv4**。

    ⚠️ 一旦探测到设备就不要再依赖主机名：`.local` 的解析是间歇性的，
       跑长脚本会在中途随机失败（本项目已实测踩到）。
    """
    for d in DEVICES:
        try:
            st, _ = req("http://%s/api/v1/health" % d, timeout=4)
        except Exception:      # noqa: BLE001
            continue
        if st != 200:
            continue
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d):
            return d
        try:
            ip = socket.gethostbyname(d)
            print("  (%s 解析为 %s，后续统一用 IP 以免间歇解析失败)" % (d, ip))
            return ip
        except OSError:
            print("  (警告：%s 无法解析成 IP，后续请求可能间歇失败)" % d)
            return d
    return None


def main():
    errs = []
    # 可选：命令行覆盖设备地址（换网络/换板子时不必改脚本）
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            DEVICES.insert(0, a)
            print("(使用命令行指定的设备地址: %s)" % a)
            break

    def ck(cond, msg):
        print(("  ok   " + msg) if cond else ("  FAIL " + msg))
        if not cond:
            errs.append(msg)

    def send(payload, timeout=8):
        """下发一条手势，返回 (状态, 回包dict或None)。状态可能是 'ERR:xxx'。"""
        try:
            st, txt = req(base + GEST, payload, timeout=timeout)
        except Exception as e:                     # noqa: BLE001
            return "ERR:%s" % type(e).__name__, None
        try:
            return st, json.loads(txt)
        except Exception:                          # noqa: BLE001
            return st, None

    print("== 等待设备上线 ==")
    dev = None
    for _ in range(40):
        dev = find_device()
        if dev:
            break
        time.sleep(3)
    if not dev:
        print("FAIL: 设备不可达（mDNS/IP 都试过）。检查是否与设备同网段。")
        return 1
    base = "http://%s" % dev
    print("设备: %s" % dev)

    # ---- 1. 页面确实含摇杆面板，且不引用控制器端点 ----
    print("\n== 1. 设备端 / 是否已内嵌摇杆面板 ==")
    st, html = req(base + "/")
    ck(st == 200, "GET / -> HTTP %d" % st)
    ck(len(html) > 8000, "页面 %d 字节（含摇杆后应显著大于旧版）" % len(html))
    missing = [t for t in UI_TOKENS if t not in html]
    ck(not missing, "摇杆面板元素/文案齐备%s" % ("" if not missing else "，缺: %s" % missing))
    code = strip_js_comments(html)
    ck("/api/v1/atom/" not in code,
       "页面**代码**未引用 /api/v1/atom/*（设备端无此路由，引用即 404）")
    ck("/api/v1/gesture" in code, "页面直连 /api/v1/gesture")

    # ---- 2. dir 小位移：净位移必须等于请求值 ----
    print("\n== 2. dir 小位移（模拟摇杆一个 tick）==")
    dx, dy = 200, 0
    st, r = send({"type": "dir", "dx": dx, "dy": dy,
                  "steps": 4, "step_ms": 90, "press": 0})
    ck(st == 200, "POST gesture(dir %d,%d) -> HTTP %s" % (dx, dy, st))
    ck(bool(r and r.get("ok")), "回包 ok=true（%s）" % json.dumps(r, ensure_ascii=False)[:130])
    time.sleep((int((r or {}).get("steps", 4)) * int((r or {}).get("step_ms", 90)) + 400) / 1000.0)
    st, h = req(base + "/api/v1/health")
    h = json.loads(h)
    ck(h.get("gest_dx") == dx and h.get("gest_dy") == dy,
       "health 对账：净位移 (%s,%s) == 请求 (%d,%d)；实发 %s 帧、丢帧 %s"
       % (h.get("gest_dx"), h.get("gest_dy"), dx, dy,
          h.get("gest_frames"), h.get("gest_drop")))
    ck(not h.get("gest_drop"), "丢帧=%s（应为 0；>0 = 点距不均、会少走）" % h.get("gest_drop"))
    ck(h.get("gest_frames") == int((r or {}).get("steps", 0)) + 2,
       "DIR 实际帧数 = steps+2（%s == %s+2）"
       % (h.get("gest_frames"), (r or {}).get("steps")))

    # ---- 3. 大位移：必须自适应展开帧数，不得少走 ----
    print("\n== 3. dir 大位移（归零场景：必须自适应展开，shortfall=0）==")
    big = 25000
    st, r = send({"type": "dir", "dx": -big, "dy": -big,
                  "steps": 16, "step_ms": 15, "press": 0})
    ck(st == 200, "POST dir(-%d,-%d) -> HTTP %s" % (big, big, st))
    r = r or {}
    need = -(-big // 127)          # ceil(big/127)
    ck(int(r.get("steps", 0)) >= need,
       "帧数被展开到 %s（≥ ceil(%d/127)=%d）—— 只扩不缩" % (r.get("steps"), big, need))
    ck(int(r.get("budget", 0)) >= big,
       "位移预算 %s ≥ 请求 %d" % (r.get("budget"), big))
    ck(int(r.get("shortfall", 1)) == 0, "shortfall=0（预算够，不谎报也不少走）")
    time.sleep(int(r.get("steps", 200)) * int(r.get("step_ms", 15)) / 1000.0 + 1.5)
    st, h = req(base + "/api/v1/health")
    h = json.loads(h)
    ck(h.get("gest_dx") == -big and h.get("gest_dy") == -big,
       "health 对账：净位移 (%s,%s) == 请求 (-%d,-%d)"
       % (h.get("gest_dx"), h.get("gest_dy"), big, big))

    # ---- 4. tap：净位移必须为 (0,0) ----
    print("\n== 4. tap（原地点击）净位移必须为 (0,0) ==")
    st, r = send({"type": "tap", "step_ms": 80})
    ck(st == 200, "POST tap -> HTTP %s" % st)
    r = r or {}
    time.sleep(int(r.get("steps", 4)) * int(r.get("step_ms", 80)) / 1000.0 + 0.8)
    st, h = req(base + "/api/v1/health")
    h = json.loads(h)
    ck(h.get("gest_dx") == 0 and h.get("gest_dy") == 0,
       "health 对账：tap 净位移 (%s,%s) == (0,0)（1px 净零抖动锚定）"
       % (h.get("gest_dx"), h.get("gest_dy")))

    # ---- 5. BLE 必须在线，否则以上全部无意义 ----
    print("\n== 5. BLE 连接 ==")
    ck(h.get("ble_connected") == 1,
       "ble_connected=%s（=1 才说明手势真的进了 host）" % h.get("ble_connected"))

    # ---- 6. 密集连发鲁棒性（回放期间 HTTP 必须仍能服务）----
    print("\n== 6. 密集连发鲁棒性（长手势回放中连发 4 条；队列深度 GEST_QUEUE_LEN=%d）=="
          % GEST_QUEUE_LEN)
    time.sleep(1.0)                    # 让 httpd 回收前面请求的连接（fd 上限很小）
    st0, _ = send({"type": "dir", "dx": -25000, "dy": -25000,
                   "steps": 16, "step_ms": 15, "press": 0})
    print("     占用设备的长手势（约 3s）-> HTTP %s" % st0)
    statuses = []
    for _ in range(4):
        st, _ = send({"type": "dir", "dx": 127, "dy": 0,
                      "steps": 4, "step_ms": 60, "press": 0}, timeout=20)
        statuses.append(st)
        time.sleep(0.35)               # 给单任务 httpd 喘息，别把连接 fd 堆爆
    ok_cnt = statuses.count(200)
    answered = ok_cnt + statuses.count(503)
    print("     连发结果: %s" % statuses)
    ck(answered == len(statuses),
       "每条都得到明确 HTTP 响应（200/503），无超时/连接错误；实测 %s" % statuses)
    ck(ok_cnt >= 1, "至少 %d 条被接受（BLE 高密度回放下 API 仍可用）" % ok_cnt)
    if 503 in statuses:
        print("  观测：出现 %d 条 503 —— 队列曾满（GEST_QUEUE_LEN=%d）"
              % (statuses.count(503), GEST_QUEUE_LEN))
    else:
        print("  观测：未出现 503 —— 单条手势（~360ms）比 HTTP 往返更快播完，"
              "worker 总能及时取走，队列不积压（正常，非缺陷）")

    print("\n== 结论 ==")
    if errs:
        print("FAIL %d 项:" % len(errs))
        for e in errs:
            print("  ✗ " + e)
        return 1
    print("PASS：设备端摇杆面板已生效，且 dir/tap 手势真机对账全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
