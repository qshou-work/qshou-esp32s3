#!/usr/bin/env python3
"""
tests/test_firmware_contract.py
================================
在「本机无 C 编译器」的约束下，对固件 HID 算法契约做**立即可跑**的验证。

两层契约（务必区分清楚）：
  A) HTTP 契约（控制台→固件）：5 字节**绝对坐标** report
     [state][X_MSB][X_LSB][Y_MSB][Y_LSB]，由 shared/coords.py 生成，
     固件原样接收。本文件验证「固件镜像 == 控制台实现」（真源 coords.py）。
     （仅作控制台侧契约留存；固件 BLE 线帧已改为相对鼠标，见 B。）
  B) BLE 线帧契约（固件→中心端）：3 字节**相对**坐标 report
     [buttons][dX][dY]（dX/dY 为 8-bit 有符号相对增量），由 ble_hid.c 直接
     打包每帧相对位移。host 把每帧 (dX,dY) 累加为光标位移 → 相对鼠标，天然无失步。

运行：
  python firmware/tests/test_firmware_contract.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "shared"))
import coords as C  # noqa: E402

# ---- 镜像固件 C 算法 ----
HID_MAX = 10000          # HTTP 契约侧坐标空间（coords.py 用，[0,10000]→16 位）
HID_ABS_MAX = 32767      # 固件 BLE 线帧绝对坐标上界（描述符 Logical Max）
HID_COORD_MAX = 65535    # HTTP 契约 16 位坐标上界
HID_STATE_DOWN = 0x03
HID_STATE_UP = 0x00
HID_BTN_LEFT = 0x01


# A) HTTP 绝对契约镜像（与 shared/coords.py 逐字节等价）
def c_hid_clamp(v):
    if v < 0:
        return 0
    if v > HID_MAX:
        return HID_MAX
    return v


def c_pack(hid_x, hid_y, state):
    """5 字节绝对 report（HTTP 契约）。"""
    hx = c_hid_clamp(int(hid_x))
    hy = c_hid_clamp(int(hid_y))
    return bytes([
        state & 0xFF,
        (hx >> 8) & 0xFF, hx & 0xFF,
        (hy >> 8) & 0xFF, hy & 0xFF,
    ])


def c_unpack(rep):
    assert len(rep) == 5
    return rep[0], (rep[1] << 8) | rep[2], (rep[3] << 8) | rep[4]


# B) BLE 相对契约镜像（3 字节 [buttons][dX][dY]，dX/dY 8-bit 有符号，二进制补码）
def c_pack_relative(buttons, dx, dy):
    """3 字节相对 report（BLE 线帧）。"""
    dx = max(-127, min(127, int(dx)))
    dy = max(-127, min(127, int(dy)))
    return bytes([buttons & 0xFF, dx & 0xFF, dy & 0xFF])


def c_build_gesture_box(steps, side=120):
    """镜像 ble_hid_build_gesture_points(GESTURE_BOX)：HID 坐标增量闭合环。"""
    if steps > side:
        steps = side
    per = (side + steps - 1) // steps
    pts = [(HID_BTN_LEFT, 0, 0)]  # 按下
    exy = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    for ex, ey in exy:
        for _ in range(steps):
            pts.append((HID_BTN_LEFT, ex * per, ey * per))
    pts.append((0, 0, 0))  # 抬起
    return pts


def c_build_gesture_dir(dx, dy, steps, press=0):
    """镜像 ble_hid_build_gesture_points(GESTURE_DIR)：单方向直线移动/拖拽。

    相对模式下 dx/dy 是总相对位移；用「累计取整」保证各帧之和 == 总位移，
    每帧增量夹到 [-127,127]（BLE 线帧 X/Y 是 8-bit 有符号）。
    press=0 → 纯移动（位移帧不按键）；press=1 → 拖拽（位移帧保持按下）。
    """
    if steps < 1:
        steps = 1
    btn = HID_BTN_LEFT if press else 0
    pts = [(btn, 0, 0)]  # 第 0 帧（按需）按下
    prev_x = prev_y = 0
    for k in range(1, steps + 1):
        cx = dx * k // steps
        cy = dy * k // steps
        sx = max(-127, min(127, cx - prev_x))
        sy = max(-127, min(127, cy - prev_y))
        pts.append((btn, sx, sy))
        prev_x += sx
        prev_y += sy
    pts.append((0, 0, 0))  # 末帧抬起
    return pts


def c_build_gesture_tap():
    """镜像 ble_hid_build_gesture_points(GESTURE_TAP)：1px 净零抖动锚定点击。

    安卓相对鼠标 HID host 会丢弃「零位移 down→up」，必须有至少一帧「按下 + 非零位移」
    来锚定坐标。帧序：第 0 帧按下(0,0)；第 1 帧按住 +1px（锚定）；第 2 帧抬起 -1px
    （净位移 0，光标不漂移）。通用尾部还会补一个抬起(0,0)，净位移仍为 0。
    """
    return [(HID_BTN_LEFT, 0, 0), (HID_BTN_LEFT, 1, 0), (0, -1, 0)]


# ---- 断言框架 ----
_fails = 0


def check(cond, msg):
    global _fails
    if cond:
        print("  ok   %s" % msg)
    else:
        print("  FAIL %s" % msg)
        _fails += 1


# 1) HTTP 契约：5 字节绝对 pack 与控制台侧 coords.hid_to_report 逐字节一致
samples = [(0, 0), (10000, 10000), (5000, 2400), (540, 1200),
           (20000, -5000), (-100, 50000), (1234, 5678)]
for (x, y) in samples:
    mine = c_pack(x, y, HID_STATE_DOWN)
    theirs = C.hid_to_report(float(x), float(y), C.HID_STATE_DOWN)
    check(mine == theirs,
          "HTTP pack(%d,%d) == coords.hid_to_report -> %s" % (x, y, mine.hex()))

# 2) 绝对解包往返
for (x, y) in [(0, 0), (5000, 2400), (10000, 10000)]:
    st, ux, uy = c_unpack(c_pack(x, y, HID_STATE_UP))
    check((st, ux, uy) == (HID_STATE_UP, c_hid_clamp(x), c_hid_clamp(y)),
          "HTTP unpack roundtrip (%d,%d)" % (x, y))

# 3) 绝对字节布局：state 在前，X/Y 高位在前
rep = c_pack(0x1234, 0x0ABC, HID_STATE_DOWN)
check(rep[0] == HID_STATE_DOWN and rep[1] == 0x12 and rep[2] == 0x34
      and rep[3] == 0x0A and rep[4] == 0xBC,
      "HTTP layout [state,Xmsb,Xlsb,Ymsb,Ylsb] big-endian")

# 4) BLE 相对 report 布局：3 字节 [buttons][dX][dY]，dX/dY 8-bit 有符号（补码）
rrel = c_pack_relative(HID_BTN_LEFT, 5, -20)
check(rrel == bytes([HID_BTN_LEFT, 5, 0xEC]),
      "relative layout [btn,dx,dy] two's complement -> %s" % rrel.hex())
check(c_pack_relative(0, 200, -200) == bytes([0, 127, 0x81]),
      "relative delta clamped to [-127,127]")

# 5) gesture 几何：闭合相对环（sum dx == sum dy == 0，首帧按下、末帧抬起）
pts = c_build_gesture_box(12)
sx = sum(p[1] for p in pts)
sy = sum(p[2] for p in pts)
check(pts[0][0] == HID_BTN_LEFT and pts[-1][0] == 0,
      "gesture: first frame press, last frame release")
check(sx == 0 and sy == 0,
      "gesture box is a closed relative loop (sum dx=%d, dy=%d)" % (sx, sy))

# 6) api_schema 契约存在性
schema_text = open(os.path.join(_HERE, "..", "..", "shared", "api_schema.yaml"),
                   encoding="utf-8").read()
check('base: "/api/v1"' in schema_text, "api_schema base is /api/v1")
for rel in ["/tap", "/swipe", "/key", "/calibrate/point",
            "/calibrate/commit", "/health", "/gesture"]:
    check(('path: "%s"' % rel) in schema_text,
          "api_schema has endpoint %s" % rel)

# 7) HID 描述符解析：从 hid_descriptor.c 提取字节，验证 Report ID 1 实际声明 3 字节
#    （防止「描述符声明 N 字节、固件发 M 字节」的错位 bug 复发——_Static_assert
#     只查硬编码常量，拦不住这类错误。）
import re as _re


def extract_descriptor_bytes(src):
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)  # 去块注释
    m = _re.search(r"qshou_hid_report_map\[\]\s*=\s*\{(.*?)\};", src, _re.S)
    if not m:
        raise RuntimeError("cannot find qshou_hid_report_map[] in source")
    body = m.group(1)

    def repl_hiditem(mm):
        tagmap = {"USAGE_PAGE": 0, "LOGICAL_MIN": 1, "LOGICAL_MAX": 2,
                  "REPORT_SIZE": 7, "REPORT_ID": 8, "REPORT_COUNT": 9,
                  "USAGE": 0, "USAGE_MIN": 1, "USAGE_MAX": 2,
                  "COLLECTION": 10, "END_COLLECT": 12, "INPUT": 8}
        typmap = {"MAIN": 0, "GLOBAL": 1, "LOCAL": 2}
        b = (tagmap[mm.group(1)] << 4) | (typmap[mm.group(2)] << 2) | (int(mm.group(3)) & 0x03)
        return str(b)

    body = _re.sub(r"HID_ITEM\(\s*HID_TAG_(\w+)\s*,\s*HID_TYPE_(\w+)\s*,\s*(\d+)\s*\)",
                   repl_hiditem, body)
    tok = {
        # ⚠️ 顺序敏感：长键必须排在短键之前（"HID_REPORT_ID" 是
        # "HID_REPORT_ID_KEYBOARD" 的前缀，先替换短的会把长的打断）。
        "QSHOU_HID_USAGE_PAGE_DESKTOP": "0x01", "QSHOU_HID_USAGE_MOUSE": "0x02",
        "QSHOU_HID_USAGE_POINTER": "0x01", "QSHOU_HID_USAGE_PAGE_BUTTON": "0x09",
        "QSHOU_HID_USAGE_X": "0x30", "QSHOU_HID_USAGE_Y": "0x31",
        "QSHOU_HID_USAGE_PAGE_KEYBOARD": "0x07",
        "QSHOU_HID_USAGE_PAGE_CONSUMER": "0x0C",
        "QSHOU_HID_USAGE_KEYBOARD": "0x06",
        "QSHOU_HID_USAGE_CONSUMER_CONTROL": "0x01",
        "QSHOU_HID_USAGE_PAGE_DIGITIZER": "0x0D",
        "QSHOU_HID_USAGE_TOUCHSCREEN": "0x04",
        "QSHOU_HID_USAGE_FINGER": "0x22",
        "QSHOU_HID_USAGE_TIP_SWITCH": "0x42",
        "HID_REPORT_ID_KEYBOARD": "0x02",
        "HID_REPORT_ID_CONSUMER": "0x03",
        "HID_REPORT_ID": "0x01",
        "HID_COLLECTION_APPLICATION": "0x01", "HID_COLLECTION_PHYSICAL": "0x00",
        "HID_COLLECTION_LOGICAL": "0x02",
        "HID_INPUT_DATA_VAR_ABS": "0x02", "HID_INPUT_CONST_PAD": "0x03",
        "HID_INPUT_DATA_VAR_REL": "0x06",
    }
    for k, v in tok.items():
        body = body.replace(k, v)
    parts = [p.strip() for p in body.split(",") if p.strip()]
    return [int(p, 0) for p in parts]


def hid_report_bytes(descriptor):
    """解析 HID 报告描述符，返回 {report_id: 字节数}。"""
    report_size = 0
    report_count = 0
    report_id = 0
    bits = {0: 0}

    def rid_bits(rid):
        return bits.setdefault(rid, 0)

    i, n = 0, len(descriptor)
    while i < n:
        b = descriptor[i]
        tag = (b >> 4) & 0x0F
        typ = (b >> 2) & 0x03
        size = b & 0x03
        i += 1
        data = 0
        if size == 1:
            data = descriptor[i]; i += 1
        elif size == 2:
            data = descriptor[i] | (descriptor[i + 1] << 8); i += 2
        elif size == 3:
            data = descriptor[i] | (descriptor[i + 1] << 8) | (descriptor[i + 2] << 16); i += 3
        if typ == 1:  # global
            if tag == 7:
                report_size = data
            elif tag == 9:
                report_count = data
            elif tag == 8:
                report_id = data
                rid_bits(report_id)
        elif typ == 0:  # main
            if tag == 8:  # Input
                rid_bits(report_id)
                bits[report_id] += report_size * report_count
    return {rid: (bt + 7) // 8 for rid, bt in bits.items()}


_desc_path = os.path.join(_HERE, "..", "ble_hid", "hid_descriptor.c")
_desc_src = open(_desc_path, encoding="utf-8").read()
_dbytes = extract_descriptor_bytes(_desc_src)
_rb = hid_report_bytes(_dbytes)
# 复合设备：三个 Report ID 各自声明的字节数必须与固件打包长度一致
check(_rb.get(1) == 3,
      "HID descriptor: Report ID 1 (mouse) declares 3 bytes (got %s)" % _rb)
check(_rb.get(2) == 8,
      "HID descriptor: Report ID 2 (keyboard) declares 8 bytes (got %s)" % _rb)
check(_rb.get(3) == 2,
      "HID descriptor: Report ID 3 (consumer) declares 2 bytes (got %s)" % _rb)
_declared = sorted(rid for rid, nb in _rb.items() if nb > 0)
check(_declared == [1, 2, 3],
      "HID descriptor must declare exactly Report IDs 1/2/3 "
      "(MAX_RPTS=3) (got %s)" % _declared)
check(len(_dbytes) == 125,
      "HID descriptor total length == 125 bytes (mouse+kbd+consumer) (got %d)" % len(_dbytes))

# 8) 线帧不得带 Report ID 前缀 —— 由真机目视 box 手势「下→右→上→右」反推出的 bug
#    host 不消费 ID 字节，把载荷前 3 字节当 [buttons][dX][dY]，多补 1 字节即整体错位。
#    复合设备同理：每个 Report ID 在 HOGP 下是独立特征（Report Reference 0x2908 关联），
#    载荷里一律不带 ID 前缀，靠 esp_hidd_dev_input_set 的 id 参数路由到对应特征。
_emit_path = os.path.join(_HERE, "..", "platform", "esp32", "platform_ble.c")
_emit_src = open(_emit_path, encoding="utf-8").read()

check("HID_REPORT_LEN + 1" not in _emit_src,
      "wire buffer must NOT be HID_REPORT_LEN+1 (no Report ID byte on the wire)")
check(_re.search(r"wire\s*\[\s*0\s*\]\s*=", _emit_src) is None,
      "must NOT write a leading byte into a wire[] buffer")

# 通用发射函数必须用**入参** report/len 调用 input_set（长度随 report_id 变）
_mc = _re.search(r"int\s+platform_ble_emit_report\s*\([^)]*\)\s*\{(.*?)\n\}",
                 _emit_src, _re.S)
check(_mc is not None, "platform_ble_emit_report() found in platform_ble.c")
_mbody = _mc.group(1) if _mc else ""
_mcall = _re.search(r"esp_hidd_dev_input_set\s*\((.*?)\)\s*;", _mbody, _re.S)
check(_mcall is not None, "esp_hidd_dev_input_set() called in platform_ble_emit_report")
_margs = _mcall.group(1).strip() if _mcall else ""
check("report" in _margs,
      "input_set must pass the report buffer verbatim (got %r)" % _margs)
check(_re.search(r",\s*len\s*$", _margs) is not None,
      "input_set length must be the incoming len, not sizeof(...) (got %r)" % _margs)
check(_re.search(r"report_id", _margs) is not None,
      "input_set must route by report_id param (got %r)" % _margs)

# 鼠标包装 platform_ble_emit 必须固定用鼠标 Report ID 1
_me = _re.search(r"int\s+platform_ble_emit\s*\([^)]*\)\s*\{(.*?)\n\}", _emit_src, _re.S)
check(_me is not None, "platform_ble_emit() found in platform_ble.c")
_mebody = _me.group(1) if _me else ""
check("HID_REPORT_ID_MOUSE" in _mebody,
      "platform_ble_emit must delegate with HID_REPORT_ID_MOUSE")

# 9) GESTURE_DIR 几何：四向移动/拖拽的位移总量必须精确等于请求值（HID 单位，无 127 夹取）
#    纯移动模式（press=0，UI 四向移动按钮）：位移帧不按任何键
#    拖拽模式（press=1，点击/控制器 swipe）：位移帧全程保持按下
for (dx_req, dy_req, steps) in [(120, 0, 12), (0, -120, 12), (-200, 150, 16),
                                (0, 0, 1), (240, 240, 12), (80, 120, 4)]:
    # 纯移动（四向移动按钮，press=0）
    pts = c_build_gesture_dir(dx_req, dy_req, steps, press=0)
    moves = pts[1:-1]   # 去掉首尾的（不）按下/抬起帧
    sx = sum(p[1] for p in moves)
    sy = sum(p[2] for p in moves)
    check(sx == dx_req and sy == dy_req,
          "GESTURE_DIR move(%d,%d,%d) 位移总量精确 == 请求 (got %d,%d)"
          % (dx_req, dy_req, steps, sx, sy))
    check(moves and all(p[0] == 0 for p in moves),
          "GESTURE_DIR move(%d,%d,%d) 位移帧不按键（纯移动）"
          % (dx_req, dy_req, steps))
    # 拖拽（点击/控制器 swipe，press=1）
    pts2 = c_build_gesture_dir(dx_req, dy_req, steps, press=1)
    moves2 = pts2[1:-1]
    sx2 = sum(p[1] for p in moves2)
    sy2 = sum(p[2] for p in moves2)
    check(sx2 == dx_req and sy2 == dy_req,
          "GESTURE_DIR drag(%d,%d,%d) 位移总量精确 == 请求 (got %d,%d)"
          % (dx_req, dy_req, steps, sx2, sy2))
    check(moves2 and all(p[0] == HID_BTN_LEFT for p in moves2),
          "GESTURE_DIR drag(%d,%d,%d) 位移帧全程保持按下"
          % (dx_req, dy_req, steps))

# 9b) GESTURE_TAP：原地点击必须净位移 0，且第 1 帧用非零位移锚定按下
_tap = c_build_gesture_tap()
_tap_sx = sum(p[1] for p in _tap)
_tap_sy = sum(p[2] for p in _tap)
check(_tap_sx == 0 and _tap_sy == 0,
      "GESTURE_TAP 净位移必须为 0（光标不漂移，got %d,%d）" % (_tap_sx, _tap_sy))
check(_tap[0][0] == HID_BTN_LEFT,
      "GESTURE_TAP 第 0 帧必须按下")
check(_tap[1][1] != 0 or _tap[1][2] != 0,
      "GESTURE_TAP 第 1 帧必须用非零位移锚定按下（否则安卓丢弃零位移 down/up）")
check(_tap[-1][0] == 0,
      "GESTURE_TAP 末帧必须抬起（按键不卡住）")

# 10) 手势/按键回放必须串行化：所有作业只入队，创建常驻任务最多一次
# 真机 bug：旧实现每次触发都新建任务，零并发保护 → 上一次 dir 的尾帧
# 插进 box 序列 → box 净位移不再为 0（终点稳定偏「上次 dir 方向」一格）。
# 复合 HID 后按键也复用同一队列（按键帧插进手势序列同样会破坏闭合）。
_blesrc = _emit_src   # 同一份 platform_ble.c 源码
_pg = _re.search(r"int\s+ble_hid_play_gesture\s*\(.*?\n\}", _blesrc, _re.S)
_pg_src = _pg.group(0) if _pg else ""
check(_pg is not None, "ble_hid_play_gesture 必须存在")
check("xQueueSend" in _pg_src,
      "ble_hid_play_gesture 应只入队 (xQueueSend)，不新建任务")
check("xTaskCreate" not in _pg_src,
      "ble_hid_play_gesture 不得新建任务（创建集中在 hid_queue_ensure）")

# 队列与常驻任务的**唯一**创建点，必须被 !s_gest_task 守卫（全生命周期只建一次）
_qe = _re.search(r"static\s+int\s+hid_queue_ensure\s*\(.*?\n\}", _blesrc, _re.S)
_qe_src = _qe.group(0) if _qe else ""
check(_qe is not None, "hid_queue_ensure 必须存在（唯一的队列/任务创建点）")
check(_re.search(r"if\s*\(\s*!s_gest_task\s*\)[\s\S]{0,400}?xTaskCreate",
                 _qe_src) is not None,
      "xTaskCreate 只允许在 if(!s_gest_task) 内做首次创建（不得每次触发新建任务）")
check("&s_gest_task" in _qe_src,
      "常驻任务句柄必须保存到 s_gest_task，保证只创建一次")
# 计数时必须先剥掉注释（历史注释里提到过 xTaskCreate，会干扰计数）
_blesrc_nc = _re.sub(r"/\*.*?\*/", "", _blesrc, flags=_re.S)
_blesrc_nc = _re.sub(r"//[^\n]*", "", _blesrc_nc)
check(_blesrc_nc.count("xTaskCreate") == 1,
      "整个 platform_ble.c 内 xTaskCreate 只允许 1 处 (got %d)"
      % _blesrc_nc.count("xTaskCreate"))

# 按键必须复用同一串行队列（不得单独起任务 / 不得直接发射 → 会插帧）
_kfn = _re.search(r"int\s+ble_hid_send_key\s*\(.*?\n\}", _blesrc, _re.S)
_kfn_src = _kfn.group(0) if _kfn else ""
check(_kfn is not None, "ble_hid_send_key 必须存在")
check("xQueueSend" in _kfn_src,
      "ble_hid_send_key 必须复用串行队列 (xQueueSend)，否则会插进手势序列")
check("xTaskCreate" not in _kfn_src, "ble_hid_send_key 不得新建任务")

# 常驻任务体内不得 vTaskDelete（应常驻循环取队列）
# 注意：必须排除前置声明 `static void gesture_task(void *arg);`
#（(void *arg) 后是 ';' 而非 '{'），否则会误抓到 hid_queue_ensure 的函数体。
_gt = _re.search(r"static\s+void\s+gesture_task\s*\([^)]*\)\s*\{(.*?)\n\}",
                 _blesrc, _re.S)
_gt_src = _gt.group(1) if _gt else ""
check(_gt is not None, "gesture_task 必须存在")
check("vTaskDelete" not in _gt_src,
      "gesture_task 应常驻（不得 vTaskDelete），否则失去串行化保证")
check("xQueueReceive" in _gt_src,
      "gesture_task 应从队列取作业")
check("hid_emit_out(job.key_rid" in _gt_src,
      "gesture_task 必须用 hid_emit_out(job.key_rid, ...) 发按键帧"
      "（带 Report ID 的通用发射，且必须经路由层；不得用鼠标专用包装）")
# 路由层：队列满必须映射成 503（明确拒绝并发），不能当成 400
_rsrc = open(os.path.join(_HERE, "..", "main", "routes.c"),
             encoding="utf-8").read()
check(_re.search(r"rc\s*==\s*1\s*\)\s*\?\s*503", _rsrc) is not None,
      "play_gesture/send_key 返回 1(忙) 必须映射为 503")
check("ble_hid_send_key" in _rsrc,
      "routes.c 的 /api/v1/key 必须接真实发键 ble_hid_send_key")

# 11) box / circle 的净位移必须为 0（闭合性的静态证明）
for _steps in (1, 7, 12, 60):
    _bx = c_build_gesture_box(_steps)
    _sx = sum(p[1] for p in _bx)
    _sy = sum(p[2] for p in _bx)
    check(_sx == 0 and _sy == 0,
          "GESTURE_BOX(steps=%d) 净位移必须为 0 (got %d,%d)" % (_steps, _sx, _sy))

# 12) 帧节奏必须用「绝对周期」而非「相对延迟」
# 真机现象：方框上点距不一致。相对延迟会把每帧 emit 耗时累加进周期，
# 且帧时刻会被 BLE 连接间隔量化 → 时间轴抖动 → host 侧指针加速让位移倍率变化。
check("vTaskDelayUntil" in _gt_src,
      "gesture_task 帧间隔必须用 vTaskDelayUntil（绝对周期，补偿 emit 耗时）")
check("vTaskDelay(pdMS_TO_TICKS(spec.step_ms))" not in _gt_src,
      "禁止用 vTaskDelay(step_ms) 做帧间隔（相对延迟会累积漂移）")

# 13) HOGP 连接参数：连接建立后必须请求低 BLE 连接间隔
check("ble_gap_update_params" in _blesrc,
      "GAP connect 后必须请求 HOGP 低连接间隔（默认间隔不整除 step_ms 会量化帧节奏）")
check(_re.search(r"itvl_min\s*=\s*12", _blesrc) is not None,
      "连接的 itvl_min 应为 12（12×1.25ms = 15ms，HOGP 推荐）")
check(_re.search(r"latency\s*=\s*0", _blesrc) is not None,
      "HID 连接的 slave latency 必须为 0")

# 14) 抖动可观测量必须暴露（health 用）
check("platform_ble_gesture_jitter" in _blesrc,
      "必须提供 platform_ble_gesture_jitter()（帧节奏可观测）")
check("gest_jit_max" in _rsrc,
      "health 端点必须暴露 gest_jit_max（否则抖动无法定位）")

# 15) 丢帧必须被捕获：emit 失败 → 点距翻倍，且净位移统计必须只算成功帧
check(_re.search(r"for\s*\(\s*int\s+a\s*=\s*0;\s*a\s*<\s*3", _gt_src) is not None,
      "gesture_task 必须对 emit 失败重试（丢一帧该处点距就翻倍）")
check("sent++" in _gt_src and "drop++" in _gt_src,
      "gesture_task 必须统计 sent/drop，不能忽略 emit 返回值")
check(_re.search(r"if\s*\(\s*rc\s*==\s*0\s*\)[\s\S]{0,220}?net_x\s*\+=", _gt_src)
      is not None,
      "净位移必须只累加发送成功的帧（否则会「声称净位移 0 但屏幕没回原点」）")
check("platform_ble_gesture_sent" in _blesrc,
      "必须提供 platform_ble_gesture_sent()（丢帧可观测）")
check("gest_drop" in _rsrc,
      "health 端点必须暴露 gest_drop（点距不均的直接证据）")

# 16) 键盘 / Consumer report 打包契约（复合 HID 的 Report ID 2 / 3）
HID_RID_KEYBOARD = 2
HID_RID_CONSUMER = 3
HID_KEYBOARD_LEN = 8
HID_CONSUMER_LEN = 2


def c_pack_keyboard(modifier, keys):
    """镜像 hid_pack_keyboard：8 字节 [modifier][reserved=0][k0..k5]。"""
    ks = (list(keys) + [0] * 6)[:6]
    return bytes([modifier & 0xFF, 0] + [k & 0xFF for k in ks])


def c_pack_consumer(usage):
    """镜像 hid_pack_consumer：2 字节 16-bit usage（小端）。"""
    return bytes([usage & 0xFF, (usage >> 8) & 0xFF])


check(c_pack_keyboard(0, [0x04]) == bytes([0, 0, 0x04, 0, 0, 0, 0, 0]),
      "键盘 report 布局必须为 [modifier][reserved][k0..k5]")
check(len(c_pack_keyboard(0x02, [0x04, 0x05, 0, 0, 0, 0])) == HID_KEYBOARD_LEN,
      "键盘 report 恒 8 字节")
check(c_pack_consumer(0x0223) == bytes([0x23, 0x02]),
      "Consumer report 必须为 16-bit usage 小端（HOME=0x0223 → 23 02）")
check(c_pack_consumer(0) == bytes([0, 0]), "Consumer 抬起帧必须为全 0")
check(len(c_pack_consumer(0x00E9)) == HID_CONSUMER_LEN, "Consumer report 恒 2 字节")

# 17) 按键映射表必须覆盖规格 supported_actions 的关键键，且区分两类 Report ID
_blehid_src = open(os.path.join(_HERE, "..", "ble_hid", "ble_hid.c"),
                   encoding="utf-8").read()
for _name in ["HOME", "BACK", "VOL_UP", "VOL_DOWN", "MUTE", "PLAY_PAUSE",
              "ENTER", "ESC", "TAB", "SPACE", "BACKSPACE", "DELETE",
              "UP", "DOWN", "LEFT", "RIGHT"]:
    check('"%s"' % _name in _blehid_src,
          "ble_hid.c 按键表必须包含命名键 %s" % _name)
check("HID_REPORT_ID_CONSUMER" in _blehid_src
      and "HID_REPORT_ID_KEYBOARD" in _blehid_src,
      "按键映射必须同时使用 Consumer 与 键盘 Report ID")
check("0x0223" in _blehid_src, "HOME 必须映射到 Consumer usage 0x0223")
check("0x00E9" in _blehid_src, "VOL_UP 必须映射到 Consumer usage 0x00E9")
check("ascii_to_key" in _blehid_src, "必须支持单字符 ASCII（text 输入）")
check("HID_MOD_LSHIFT" in _blehid_src, "大写/符号必须使用 Shift 修饰键")

# 18) 位移预算契约（GESTURE_DIR / swipe）—— 2026-10-01 真机实测发现的静默少走 bug
# 真机证据：POST /api/v1/gesture {"type":"dir","dx":2000,"steps":12}
#         回包 {"ok":true,"dx":2000}，但 health 的 gest_dx = **1524** = 12×127。
# 根因：每帧 X/Y 是 8-bit 有符号（±127），交付上限 = 帧数 × 127；而 /api/v1/swipe
#      写死 16 帧（=2032 counts），控制器却在 [0,10000] 空间算增量 → 全屏移动只走 20%，
#      且 CursorState 照满额更新 → 与宿主光标永久失同步。
_PER_FRAME_MAX = 127
_MIN_STEPS = 4
_MAX_STEPS = 200
_TARGET_MS = 1200
_MIN_STEP_MS = 15


def c_gesture_dir_steps(want_steps, dx, dy):
    """镜像 routes.c gesture_dir_steps()：按位移展开帧数，**只扩不缩**。"""
    need = max(abs(dx), abs(dy))
    req = -(-need // _PER_FRAME_MAX)          # ceil(need/127)
    req = max(_MIN_STEPS, req)
    req = max(req, want_steps)                # 尊重调用方更大的值
    return min(req, _MAX_STEPS)


def c_gesture_dir_step_ms(want_ms, steps):
    """镜像 routes.c gesture_dir_step_ms()：锁定总时长，但不低于 15ms。"""
    ms = want_ms if want_ms > 0 else 90
    if steps > 0 and ms * steps > _TARGET_MS:
        bounded = max(_MIN_STEP_MS, _TARGET_MS // steps)
        ms = min(ms, bounded)
    return ms


# 18a) 关键回归：真机那次少走 476 counts 的组合，现在必须精确送达
_steps_eff = c_gesture_dir_steps(12, 2000, 0)
_pts = c_build_gesture_dir(2000, 0, _steps_eff, press=0)
_sx = sum(p[1] for p in _pts[1:-1])
check(_sx == 2000,
      "swipe/gesture dx=2000 必须精确送达（有效帧=%d, got %d）" % (_steps_eff, _sx))
_pts_old = c_build_gesture_dir(2000, 0, 12, press=0)
check(sum(p[1] for p in _pts_old[1:-1]) == 1524,
      "回归见证：未展开帧数时 steps=12 只能交付 1524（bug 指纹）")

# 18b) 一批大位移都必须精达（含控制器全对角，HID 空间上限）
for _dx, _dy, _want in [(5000, 3000, 16), (-10000, 10000, 16), (2000, 0, 12),
                        (2032, 0, 16), (10000, 0, 16), (-5000, -5000, 16)]:
    _st = c_gesture_dir_steps(_want, _dx, _dy)
    _p = c_build_gesture_dir(_dx, _dy, _st, press=1)
    _m = _p[1:-1]
    check(sum(q[1] for q in _m) == _dx and sum(q[2] for q in _m) == _dy,
          "DIR(%d,%d) 帧数展开到 %d 后位移精确送达" % (_dx, _dy, _st))
    check(_st <= _MAX_STEPS,
          "DIR(%d,%d) 帧数 %d 不超过帧缓冲上限 %d" % (_dx, _dy, _st, _MAX_STEPS))

# 18c) 只扩不缩：已验证的小位移不得被改变帧数
check(c_gesture_dir_steps(30, 100, 0) == 30, "小位移保持调用方帧数（只扩不缩）")
check(c_gesture_dir_steps(1, 0, 0) == _MIN_STEPS, "零位移收敛到最小帧数 %d" % _MIN_STEPS)

# 18d) 总时长被锁定：帧数变多则间隔下调，但不低于 15ms（=BLE 连接间隔）
_ms40 = c_gesture_dir_step_ms(60, 40)
check(_ms40 == 30 and _ms40 * 40 <= _TARGET_MS,
      "40 帧时间隔下调到 %dms，总时长 %dms ≤ %dms" % (_ms40, _ms40 * 40, _TARGET_MS))
_ms79 = c_gesture_dir_step_ms(60, 79)
check(_ms79 >= _MIN_STEP_MS, "79 帧时间隔不低于 %dms（物理下限）" % _MIN_STEP_MS)
check(c_gesture_dir_step_ms(90, 12) == 90, "小帧数不改动调用方节奏")

# 18e) 源码级：swipe 不再写死 16 帧、不再有 ±2000 硬上限、回包带 shortfall
_routes_src = open(os.path.join(_HERE, "..", "main", "routes.c"),
                   encoding="utf-8").read()
check("gesture_dir_steps" in _routes_src,
      "routes.c 必须使用 gesture_dir_steps() 展开帧数")
check(_re.search(r"spec\.steps\s*=\s*16\s*;", _routes_src) is None,
      "swipe 不得再写死 16 帧（该常量是本次 bug 的直接来源）")
check(_re.search(r"dx\s*>\s*2000", _routes_src) is None,
      "不得再有 ±2000 的硬编码位移上限")
_routes_nc = _re.sub(r"/\*.*?\*/", "", _routes_src, flags=_re.S)
check(_routes_nc.count("shortfall") >= 4,
      "两类回包都必须带 shortfall 字段（永不静默少走）")

# ══════════════════════════════════════════════════════════════════════
# 19) USB HID 线帧契约（usb_hid_frame.c）—— 描述符↔线帧必须一致
#
# 这组检查守的是「USB 侧能不能进 BIOS」这条命门：
#   键盘接口是 Boot Protocol，报文必须**固定 8 字节且无 Report ID 前缀**。
#   一旦有人为了「统一格式」给键盘也加前缀，BIOS 就再也认不出键盘了 ——
#   而且这个问题在普通桌面环境下**完全测不出来**（Windows/Linux 都接受带
#   Report ID 的键盘），只在 BIOS/UEFI 里暴露。所以用契约测试锁死。
# ══════════════════════════════════════════════════════════════════════
USB_ITF_KEYBOARD, USB_ITF_POINTER = 0, 1
USB_RID_NONE, USB_RID_MOUSE, USB_RID_CONSUMER = 0, 1, 2

# ble_rid -> (itf, usb_rid, data_len, wire_len)  —— 镜像 usb_hid_frame.c 的 s_map
USB_FRAME_MAP = {
    1: (USB_ITF_POINTER,  USB_RID_MOUSE,    3, 4),   # 鼠标   3B -> 4B
    2: (USB_ITF_KEYBOARD, USB_RID_NONE,     8, 8),   # 键盘   8B -> 8B（无前缀）
    3: (USB_ITF_POINTER,  USB_RID_CONSUMER, 2, 3),   # Consumer 2B -> 3B
}


def usb_pack(ble_rid, data):
    """镜像 usb_hid_pack()：返回 (itf, usb_rid, wire_bytes)；契约不符返回 None。"""
    if ble_rid not in USB_FRAME_MAP:
        return None
    itf, rid, dlen, _wlen = USB_FRAME_MAP[ble_rid]
    if len(data) != dlen:
        return None
    out = (b"" if rid == USB_RID_NONE else bytes([rid])) + bytes(data)
    return itf, rid, out


# 19a) 鼠标：接口 1、RID 1、带前缀、4 字节
_itf, _rid, _wire = usb_pack(1, [0x01, 0x05, 0xFD])          # buttons=左键, dx=5, dy=-3
check(_itf == USB_ITF_POINTER and _rid == USB_RID_MOUSE,
      "USB 鼠标落在接口 %d / RID %d" % (USB_ITF_POINTER, USB_RID_MOUSE))
check(_wire == bytes([0x01, 0x01, 0x05, 0xFD]),
      "USB 鼠标线帧 = [RID=1][buttons][dx][dy] -> %s" % _wire.hex())
check(len(_wire) == 4, "USB 鼠标线上长度 4 字节（3B 数据 + 1B Report ID）")

# 19b) 键盘：**Boot 协议命门** —— 接口 0、无 Report ID、8 字节原样
_kbd = [0x02, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00]      # LShift + 'a'
_itf, _rid, _wire = usb_pack(2, _kbd)
check(_itf == USB_ITF_KEYBOARD,
      "USB 键盘必须落在接口 0（BIOS 只探测第一个 HID 接口）")
check(_rid == USB_RID_NONE, "USB 键盘不得使用 Report ID（boot 协议要求无前缀）")
check(len(_wire) == 8 and _wire == bytes(_kbd),
      "USB 键盘线帧 = 8 字节原样透传 -> %s" % _wire.hex())
check(_wire[0] == 0x02,
      "USB 键盘首字节必须是 modifier(0x02)，而非 Report ID —— 加了前缀 BIOS 就认不出")
check(len(_wire) == len(_kbd),
      "USB 键盘 8B -> 8B（长度不变是「无前缀」的可观测判据）")

# 19c) Consumer：接口 1、**接口内重新编号为 2**（不是 BLE 侧的 3）、3 字节
_itf, _rid, _wire = usb_pack(3, [0x23, 0x02])                 # usage 0x0223 = Home
check(_itf == USB_ITF_POINTER and _rid == USB_RID_CONSUMER,
      "USB Consumer 落在接口 %d / 接口内 RID %d（Report ID 是 per-interface 编号）"
      % (USB_ITF_POINTER, USB_RID_CONSUMER))
check(_wire == bytes([0x02, 0x23, 0x02]),
      "USB Consumer 线帧 = [RID=2][usage_lo][usage_hi] -> %s" % _wire.hex())
check(len(_wire) == 3, "USB Consumer 线上长度 3 字节")

# 19d) 长度契约：与描述符声明不符必须拒发（宁可丢帧也不发错位报文）
check(usb_pack(1, [0, 0]) is None, "鼠标 2 字节（应为 3）必须被拒")
check(usb_pack(2, [0] * 7) is None, "键盘 7 字节（应为 8）必须被拒")
check(usb_pack(3, [0, 0, 0]) is None, "Consumer 3 字节（应为 2）必须被拒")
check(usb_pack(9, [0]) is None, "未知 BLE Report ID 必须被拒")

# 19e) 源码级：键盘映射必须用 USB_HID_RID_NONE（防有人「统一格式」加前缀）
_usb_src = open(os.path.join(_HERE, "..", "usb_hid", "usb_hid_frame.c"),
                encoding="utf-8").read()
_usb_nc = _re.sub(r"/\*.*?\*/", "", _usb_src, flags=_re.S)
check(_re.search(r"HID_REPORT_ID_KEYBOARD\s*,\s*USB_HID_ITF_KEYBOARD\s*,\s*USB_HID_RID_NONE",
                 _usb_nc) is not None,
      "usb_hid_frame.c：键盘映射必须是 接口0 + USB_HID_RID_NONE")
check(_re.search(r"HID_REPORT_ID_CONSUMER\s*,\s*USB_HID_ITF_POINTER\s*,\s*USB_HID_RID_CONSUMER",
                 _usb_nc) is not None,
      "usb_hid_frame.c：Consumer 必须在接口 1 内重新编号为 RID 2")

# ══════════════════════════════════════════════════════════════════════
# 20) HID 路由契约（hid_router.c）—— 严禁双发 / 业务层不得绕过路由
# ══════════════════════════════════════════════════════════════════════
_router_src = open(os.path.join(_HERE, "..", "hid_router", "hid_router.c"),
                   encoding="utf-8").read()
_router_nc = _re.sub(r"/\*.*?\*/", "", _router_src, flags=_re.S)

# 20a) 结构性保证「严禁双发」：emit 里只有一个发送调用点。
#      若有人加了「失败换个后端再试」，这里会出现第二个 .send( —— 立刻红。
check(_router_nc.count(".send(") == 1,
      "hid_router.c 只允许一个后端发送调用点（双发会导致重复输入），实际 %d 个"
      % _router_nc.count(".send("))

# 20b) 业务层不得绕过路由直发 BLE（绕过 = 该帧永不走 USB，且可能双发）
_routes_hid_nc = _re.sub(r"/\*.*?\*/", "", _routes_src, flags=_re.S)
check(_routes_hid_nc.count("platform_ble_emit_report") == 0,
      "routes.c 不得再直接调 platform_ble_emit_report（必须走 hid_router_emit）")
check(_routes_hid_nc.count("hid_router_emit") >= 3,
      "routes.c 的鼠标/键盘/Consumer 三个端点都必须走 hid_router_emit")

_ble_src = open(os.path.join(_HERE, "..", "ble_hid", "ble_hid.c"),
                encoding="utf-8").read()
_ble_nc = _re.sub(r"/\*.*?\*/", "", _ble_src, flags=_re.S)
check(_re.search(r"platform_ble_emit\s*\(", _ble_nc) is None,
      "ble_hid.c 不得再直接调 platform_ble_emit（手势回放必须走路由）")
check("hid_router_emit" in _ble_nc,
      "ble_hid.c 的手势回放必须走 hid_router_emit")

# 20c) 装配检查：main.c 必须注册 BLE 后端，否则所有报文静默丢弃
_main_src = open(os.path.join(_HERE, "..", "main", "main.c"),
                 encoding="utf-8").read()
check("hid_router_register(HID_BACKEND_BLE" in _main_src,
      "main.c 必须注册 BLE 后端（未注册则 emit 全部丢帧）")

# 20d) 构建检查：新源文件必须进 CMakeLists（漏加会链接失败，但提前拦住更快）
_cmake_src = open(os.path.join(_HERE, "..", "main", "CMakeLists.txt"),
                  encoding="utf-8").read()
check("usb_hid/usb_hid_frame.c" in _cmake_src,
      "CMakeLists.txt 必须包含 usb_hid/usb_hid_frame.c")
check("hid_router/hid_router.c" in _cmake_src,
      "CMakeLists.txt 必须包含 hid_router/hid_router.c")

# 20e) 路由策略语义镜像：AUTO = USB 优先、USB 不可用才回落 BLE；
#      USB_ONLY = 不回落（宁可丢帧，也不悄悄换链路）。
def route_select(policy, usb_registered, usb_ready):
    if policy == "ble":
        return "ble"
    if policy == "usb":
        return "usb"
    return "usb" if (usb_registered and usb_ready) else "ble"


check(route_select("auto", True, True) == "usb", "AUTO：USB 可用时优先 USB")
check(route_select("auto", True, False) == "ble", "AUTO：USB 已注册但未挂载时回落 BLE")
check(route_select("auto", False, False) == "ble", "AUTO：USB 未接入（当前构建）时走 BLE")
check(route_select("ble", True, True) == "ble", "BLE_ONLY：即使 USB 可用也强制 BLE")
check(route_select("usb", True, False) == "usb",
      "USB_ONLY：USB 不可用也不回落（回落等于悄悄换链路，违反可预期性）")

# 20f) 平台层出口注入：手势/按键队列跑在平台层常驻任务里，必须经过 hid_emit_out
#      否则 USB 后端接入后，HTTP 端点走 USB、手势却仍走 BLE —— 这种失效是**静默**的
#      （功能看起来正常，只是永远不走 USB），最难发现，所以用契约锁住。
_ble_plat_src = open(os.path.join(_HERE, "..", "platform", "esp32", "platform_ble.c"),
                     encoding="utf-8").read()
_ble_plat_nc = _re.sub(r"/\*.*?\*/", "", _ble_plat_src, flags=_re.S)
_emit_out_n = _ble_plat_nc.count("hid_emit_out(")
check(_emit_out_n >= 4,
      "platform_ble.c 必须经 hid_emit_out 发射（1 处定义 + 3 处调用），实际 %d 处"
      % _emit_out_n)
check("platform_hid_set_emit" in _main_src,
      "main.c 必须调用 platform_hid_set_emit 把路由注入平台层")

_host_ble_src = open(os.path.join(_HERE, "..", "platform", "host", "platform_ble.c"),
                     encoding="utf-8").read()
check("void platform_hid_set_emit" in _host_ble_src,
      "host 桩必须定义 platform_hid_set_emit（否则零硬件构建链接失败）")

# 20g) 诊断语义：peek（下一帧走哪）必须与实走同一条选择路径。
#      实测踩坑：/api/v1/hid 的 active 原先直接用 hid_router_active_backend()
#      （= 上一次成功发射的后端 s_last），一条都没发过时恒为 BLE。
#      结果 USB mounted=1、stats 全 0，active 却显示 "ble" —— 排查时看着像
#      路由选错，实际只是没发过帧。诊断口必须回答「下一帧会走哪」。
_router_h = open(os.path.join(_HERE, "..", "hid_router", "hid_router.h"),
                 encoding="utf-8").read()
check("hid_router_peek_backend" in _router_h and "hid_router_peek_backend" in _router_nc,
      "hid_router 必须提供 peek_backend()（纯查询「下一帧会走哪」，不发帧）")
# peek 必须复用 select_backend —— 窥探与实走不能是两套逻辑，否则诊断看到的后端
# 会和实际用的不一致（比没有诊断更糟）。
check(_re.search(r"hid_backend_t\s+hid_router_peek_backend\s*\([^)]*\)\s*\{"
                 r"\s*return\s+select_backend\s*\(", _router_nc) is not None,
      "peek_backend 必须 return select_backend(...)（与 emit 同一条选择路径）")
# emit 内部也必须仍走 select_backend（防止有人把选择逻辑抄成两份）
check(_re.search(r"select_backend\s*\(\s*&\s*is_fallback\s*\)", _router_nc) is not None,
      "emit 必须经 select_backend 选择后端")
# 端点侧：active 用 peek、last 用 active_backend，两者不得混用
_rs_nc = _re.sub(r"/\*.*?\*/", "", _routes_src, flags=_re.S)
check('hid_router_peek_backend(&would_fallback)' in _rs_nc,
      "handle_hid_status 必须用 hid_router_peek_backend 填 active（回答下一帧走哪）")
check('hid_router_backend_name(hid_router_active_backend())' in _rs_nc,
      "handle_hid_status 必须把 active_backend 放在 last 字段（历史值，勿与 active 混用）")
# C 源码里 JSON 键是转义形式 \"would_fallback\"，两种形态都接受
check(_re.search(r'\\?"would_fallback\\?":\s*%d', _rs_nc) is not None,
      "状态 JSON 应含 would_fallback（USB 已注册但不可用、将回落 BLE 的信号）")

# ══════════════════════════════════════════════════════════════════════
# 21) USB 后端契约（platform/esp32/platform_usb.c）—— Phase 1 接入 TinyUSB
# ══════════════════════════════════════════════════════════════════════
# 这组守的是「§19 的线帧契约在真实后端上被正确执行」：
# 19 组保证 usb_hid_pack() 算出的字节对，21 组保证这些字节真的被发到
# **对应的接口**、且描述符与之一致。两者脱节时，Windows 上照样能用
# （host 容错强），但 BIOS 会失效 —— 所以必须源码级锁死。
_usb_src = open(os.path.join(_HERE, "..", "platform", "esp32", "platform_usb.c"),
                encoding="utf-8").read()
_usb_nc = _re.sub(r"/\*.*?\*/", "", _usb_src, flags=_re.S)

# 21a) 复合设备必须用带 instance 的 tud_hid_n_report。
#      tud_hid_report() 在 TinyUSB 里等价于 instance=0 —— 用它发鼠标，
#      帧会被塞进**键盘接口**，表现为「鼠标不动、键盘乱按」。
check("tud_hid_n_report(" in _usb_nc,
      "platform_usb.c 必须用 tud_hid_n_report(instance, ...) 发复合设备报文")
check(_re.search(r"(?<!_)tud_hid_report\s*\(", _usb_nc) is None,
      "不得使用 tud_hid_report()（等价 instance=0，鼠标帧会发进键盘接口）")

# 21b) 描述符：必须是 2 个接口，且键盘在接口 0 用 boot protocol
check("TUD_CONFIG_DESCRIPTOR(1, 2," in _usb_nc,
      "配置描述符必须声明 2 个接口（0=键盘 / 1=鼠标+Consumer）")
check(_re.search(r"TUD_HID_DESCRIPTOR\(\s*USB_HID_ITF_KEYBOARD\s*,\s*\d+\s*,\s*"
                 r"HID_ITF_PROTOCOL_KEYBOARD", _usb_nc) is not None,
      "接口 0 必须是 boot protocol 键盘（BIOS 能认的必要条件）")
check(_re.search(r"TUD_HID_DESCRIPTOR\(\s*USB_HID_ITF_POINTER\s*,\s*\d+\s*,\s*"
                 r"HID_ITF_PROTOCOL_NONE", _usb_nc) is not None,
      "接口 1 必须是 HID_ITF_PROTOCOL_NONE（鼠标+Consumer 靠 Report ID 区分）")

# 21c) 键盘描述符**不得**出现 HID_REPORT_ID —— 加了就不是 boot 设备
_kbd_desc = _re.search(r"s_kbd_report_desc\[\]\s*=\s*\{(.*?)\};", _usb_nc, _re.S)
check(_kbd_desc is not None, "必须存在 s_kbd_report_desc 定义")
if _kbd_desc:
    check("HID_REPORT_ID" not in _kbd_desc.group(1),
          "键盘报文描述符不得含 HID_REPORT_ID（boot 协议要求 8 字节无前缀）")

# 21d) 指针描述符必须给鼠标 RID 1、Consumer RID 2（与 §19 的线帧一致）
_ptr_desc = _re.search(r"s_ptr_report_desc\[\]\s*=\s*\{(.*?)\};", _usb_nc, _re.S)
check(_ptr_desc is not None, "必须存在 s_ptr_report_desc 定义")
if _ptr_desc:
    _ptr_body = _ptr_desc.group(1)
    check(_re.search(r"HID_REPORT_ID\s*\(\s*USB_HID_RID_MOUSE\s*\)", _ptr_body) is not None,
          "接口 1 的鼠标描述符必须声明 RID 1")
    check(_re.search(r"HID_REPORT_ID\s*\(\s*USB_HID_RID_CONSUMER\s*\)", _ptr_body) is not None,
          "接口 1 的 Consumer 描述符必须声明 RID 2（接口内重新编号）")

# 21d2) **报告长度必须与线帧一致** —— 又一个 host 不报错的静默故障。
#   TinyUSB 的 TUD_HID_REPORT_DESC_MOUSE() 声明的是 **5 字节**报告
#   （buttons + X + Y + 垂直滚轮 + 横向滚轮），而 BLE 侧鼠标帧只有 3 字节。
#   用该宏 → host 期望 6 字节（含 RID）、实际收到 4 字节 → **静默丢弃全部鼠标报告**：
#   光标不动、设备管理器一切正常、零错误日志。接后端时踩中，排查很久。
check("TUD_HID_REPORT_DESC_MOUSE" not in _usb_nc,
      "不得用 TUD_HID_REPORT_DESC_MOUSE()（声明 5 字节报告，与 3 字节线帧不符 → host 静默丢弃）")
if _ptr_desc:
    # 手写版本必须是 3 buttons + 5 bit padding（合成 1 字节），再 X/Y 各 1 字节 = 3 字节
    check(_re.search(r"HID_REPORT_COUNT\(\s*3\s*\)[\s\S]{0,200}?HID_REPORT_SIZE\s*\(\s*5\s*\)",
                     _ptr_body) is not None,
          "鼠标描述符必须是 3 buttons + 5 bit padding（3 字节 payload），"
          "与 USB_HID_FRAME_MOUSE-1 对齐")
    # 不得出现滚轮（wheel / AC Pan）—— 那会让报告长度变成 5 字节
    check("HID_USAGE_DESKTOP_WHEEL" not in _ptr_body and "AC_PAN" not in _ptr_body,
          "鼠标描述符不得包含滚轮（会加长报告，与 3 字节线帧不符）")

# 21e) emit 必须经 usb_hid_pack 转线帧，不得在后端自己拼字节
#      （拼字节 = 出现第二份「BLE→USB 映射规则」，两处必然漂移）
check("usb_hid_pack(" in _usb_nc,
      "platform_usb_emit 必须用 usb_hid_pack() 转线帧（映射规则只能有一处）")

# 21e2) **双重 Report ID 防护** —— 最容易犯且 host 不报错的错。
#   TinyUSB 的 tud_hid_n_report() 在 report_id != 0 时会自己写前缀：
#     if (report_id) { epin[0] = report_id; memcpy(epin+1, report, len); len++; }
#   而 usb_hid_pack() 产出的 frame 已含前缀。原样传入 → 线上 [01][01][btn][dx][dy]，
#   整帧错位 1 字节，现象是鼠标乱跳/键盘乱按，Windows 却不报任何错。
check(_re.search(r"tud_hid_n_report\(\s*meta\.itf\s*,\s*meta\.rid\s*,\s*frame\s*,",
                 _usb_nc) is None,
      "不得把含 Report ID 前缀的 frame 直接交给 tud_hid_n_report（TinyUSB 会再加一次前缀）")
check("frame + 1" in _usb_nc,
      "必须剥掉 usb_hid_pack 生成的前缀（frame + 1）再交给 TinyUSB")

# 21f) 产品固件不得残留 Phase 0 自检任务 —— 否则每次上电都会往被控机发一个 'a'
check("tud_hid_keyboard_report" not in _usb_nc,
      "不得残留 Phase 0 自检（tud_hid_keyboard_report）—— 上电自动发 'a' 会污染被控机")
check("usb_poc_task" not in _usb_nc, "Phase 0 的 usb_poc_task 必须已移除")

# 21g) 编译期闸门：HID 实例数不是 2 就必须编译失败，而不是运行时诡异
check("CFG_TUD_HID" in _usb_nc and "#error" in _usb_nc,
      "platform_usb.c 必须有 CFG_TUD_HID != 2 的 #error 闸门")

# 21h) 装配：main.c 必须注册 USB 后端，且只在 init 成功后注册
check("hid_router_register(HID_BACKEND_USB" in _main_src,
      "main.c 必须注册 USB 后端（否则 USB 永远不参与路由）")
check("platform_usb_init()" in _main_src,
      "main.c 必须调用 platform_usb_init()")

# 21i) sdkconfig：两个 HID 实例是硬前提（HID_COUNT=1 时接口 1 无实例）
_sdkconfig = open(os.path.join(_HERE, "..", "sdkconfig"), encoding="utf-8").read()
check("CONFIG_TINYUSB_HID_COUNT=2" in _sdkconfig,
      "sdkconfig 必须 CONFIG_TINYUSB_HID_COUNT=2（接口0 + 接口1 两个 HID 实例）")

# 21j) host 桩的 ready 必须恒返回 0 —— 谎报可用会让 AUTO 策略把帧全丢在桩里
_host_usb_src = open(os.path.join(_HERE, "..", "platform", "host", "platform_usb.c"),
                     encoding="utf-8").read()
_host_usb_nc = _re.sub(r"/\*.*?\*/", "", _host_usb_src, flags=_re.S)
check(_re.search(r"int\s+platform_usb_ready\s*\(\s*void\s*\)\s*\{\s*return\s+0\s*;",
                 _host_usb_nc) is not None,
      "host 桩 platform_usb_ready 必须返回 0（否则 AUTO 全走 USB 并全部丢帧）")
check("int platform_usb_emit" in _host_usb_nc,
      "host 桩必须定义 platform_usb_emit（否则零硬件构建链接失败）")

# 22) HTTP 路由注册表 ↔ 分发表一致性（platform_net.c ROUTES[] ↔ routes.c routes_handle）
# ══════════════════════════════════════════════════════════════════
# 实测踩过的坑（烧录后 404 才暴露）：一个端点要在**两个地方**各写一次，
# 只写一边就静默失效，且两种失效方式的响应体都长得像"设备不在线"：
#   ① routes_handle 写了分发、platform_net.c 的 ROUTES[] 忘了注册
#      → httpd 层直接 404 "Nothing matches the given URI"，
#        routes_handle 里的代码**永远执行不到**，但响应体不是我们的 404，
#        现场极易误判成网络不通。
#   ② 分发语句嵌错了方法块（GET 行掉进 if (method == M_POST) 块里）
#      → 条件恒假成死代码，同样 404。
# 所以这里做双向集合比对 + 结构比对，任何一端漏写即 FAIL。
def _mask_comments(text):
    """等长掩码 v1：只把 /* */ 注释换成空格（保留换行），字符串**原样保留**。
    用于需要读字符串字面量的场景（提取 ROUTES 条目、strcmp 路径）。
    ⚠️ 必须掩注释：变异检验时把 ROUTES 条目注释掉，不掩的版本会把它当
    "已注册"，测试照样绿 —— 等于没测。"""
    out = list(text)
    for _cm in _re.finditer(r"/\*.*?\*/", text, _re.S):
        for _i in range(_cm.start(), _cm.end()):
            out[_i] = "\n" if text[_i] == "\n" else " "
    return "".join(out)

def _mask_code(text):
    """等长掩码 v2：在 v1 基础上再把字符串**内容**换成空格、引号保留。
    用于只需看代码结构的场景（花括号配对、method 标志判断）。
    两步顺序不能反：先掩注释（连带注释里的引号一起消失），再掩字符串内容 ——
    反过来会让注释里的引号参与字符串配对，掩出乱源。
    两个掩码都是等长的，掩码坐标可 1:1 回原文切片。"""
    _s1 = _mask_comments(text)
    out = list(_s1)
    for _sm in _re.finditer(r'"(\\.|[^"\\])*"', _s1):
        for _i in range(_sm.start() + 1, _sm.end() - 1):
            if out[_i] != "\n":
                out[_i] = " "
    return "".join(out)

_net_src = open(os.path.join(_HERE, "..", "platform", "esp32", "platform_net.c"),
                encoding="utf-8").read()
_m = _re.search(r"static const char \*ROUTES\[\]\s*=\s*\{(.*?)\};",
                _mask_comments(_net_src), _re.S)
check(_m is not None, "platform_net.c 必须存在 ROUTES[] 注册表")
_routes_set = set(_re.findall(r'"(/api/[^"]*)"', _m.group(1))) if _m else set()

check("/api/v1/hid" in _routes_set,
      "ROUTES[] 必须注册 /api/v1/hid —— 上一条 404 的直接原因就是漏了它")

_routes_src = open(os.path.join(_HERE, "..", "main", "routes.c"), encoding="utf-8").read()
_rh_m = _re.search(r"^int routes_handle\(.*?^\}",
                   _mask_comments(_routes_src), _re.S | _re.M)
check(_rh_m is not None, "routes.c 必须存在 routes_handle()")
_rh = _routes_src[_rh_m.start():_rh_m.end()] if _rh_m else ""

_disp_exact = set(_re.findall(r'strcmp\(\s*path\s*,\s*"(/api/[^"]*)"',
                              _mask_comments(_rh)))
_disp_pref = set(_re.findall(r'strncmp\(\s*path\s*,\s*"(/api/[^"]*)"',
                             _mask_comments(_rh)))

# 22b) 每一个被分派的路径都必须已在 ROUTES[] 注册（否则 httpd 层就 404 了）。
#      /api/v1/events 是特例：WebSocket 单独注册，routes_handle 里那条只是可
#      达性自检的回退分支，故放行。
_ROUTE_EXEMPT = {"/api/v1/events"}
for _p in sorted(_disp_exact - _ROUTE_EXEMPT):
    check(_p in _routes_set or
          any(_r.endswith("*") and _p.startswith(_r[:-1]) for _r in _routes_set),
          "routes_handle 分派了 %s 但 ROUTES[] 未注册（httpd 层会 404）" % _p)

# 22c) 反向：每一条 ROUTES[]（非通配）都必须有对应分发，否则注册了也白发。
for _r in sorted(_routes_set):
    if _r.endswith("*"):
        check(_re.search(r"GPIO_PIN_ROUTE", _rh) is not None,
              "ROUTES[] 有通配项 %s，但 routes_handle 未见 GPIO_PIN_ROUTE 分派" % _r)
        continue
    check(_r in _disp_exact or _r in _disp_pref,
          "ROUTES[] 注册了 %s 但 routes_handle 未分派（注册了也白发）" % _r)

# 22d) 死代码防护：GET 分发不得出现在 M_POST 块内，反之亦然。
#      实现方式：按花括号配对取出块体，再断言块内没有对立方法判断。
def _block_body(text, header):
    """取 text 中 if (...) { 与配对 } 之间的块体（**原文**，非掩码版）。"""
    _mask = _mask_code(text)
    _mm = _re.search(header, _mask)
    if not _mm:
        return None
    _i = _mask.index("{", _mm.end() - 1)
    _depth, _j = 0, _i
    while _j < len(_mask):
        if _mask[_j] == "{":
            _depth += 1
        elif _mask[_j] == "}":
            _depth -= 1
            if _depth == 0:
                return text[_i + 1:_j]
        _j += 1
    return None

_get_body = _block_body(_rh, r"if\s*\(\s*method\s*==\s*M_GET\s*\)\s*\{")
_post_body = _block_body(_rh, r"if\s*\(\s*method\s*==\s*M_POST\s*\)\s*\{")
check(_get_body is not None and _post_body is not None,
      "routes_handle 必须同时有 M_GET 与 M_POST 两个分派块")
check(_post_body is not None and "M_GET" not in _post_body,
      "M_POST 块内不得出现 M_GET 判断（条件恒假的死代码，GET 请求会 404）")
check(_get_body is not None and "M_POST" not in _get_body,
      "M_GET 块内不得出现 M_POST 判断（条件恒假的死代码，POST 请求会 404）")
check(_get_body is not None and _re.search(
        r'strcmp\(\s*path\s*,\s*"/api/v1/hid"\s*\)', _get_body) is not None,
      "GET /api/v1/hid 的分派必须位于 M_GET 块内")
check(_post_body is not None and _re.search(
        r'strcmp\(\s*path\s*,\s*"/api/v1/hid"\s*\)', _post_body) is not None,
      "POST /api/v1/hid 的分派必须位于 M_POST 块内")

# 23) host 接缝：每个 platform_* 函数只能定义一次。
# ══════════════════════════════════════════════════════════════════════
# 实测踩坑（2026-10-17 补 host 构建时暴露）：platform_wifi_rssi 在
# host/platform_net.c 与 host/platform_health.c 各定义了一份 → 链接期
# multiple definition 失败。**这类错误的迷惑性在于**：它是链接期错误，
# 前面 739/1307 编译步骤全是绿的、日志停在半路，看上去像「构建被打断」，
# 实际是代码冲突 —— 差点就往错方向查。故源码级拦住，别等链接。
_fn_def_re = _re.compile(
    r"^\s*(?:static\s+)?(?:int|void|bool|uint\d+_t|size_t|"
    r"const\s+char\s*\*|char\s*\*)\s+(platform_\w+)\s*\(")
_seen_fns = {}
_host_dir = os.path.join(_HERE, "..", "platform", "host")
check(os.path.isdir(_host_dir), "必须存在 platform/host 接缝目录")
if os.path.isdir(_host_dir):
    for _fn in sorted(os.listdir(_host_dir)):
        if not _fn.endswith(".c"):
            continue
        _txt = open(os.path.join(_host_dir, _fn), encoding="utf-8").read()
        _txt = _re.sub(r'/\*.*?\*/', " ", _txt, flags=_re.S)
        _txt = _re.sub(r'"(\\.|[^"\\])*"', '""', _txt)
        for _line in _txt.splitlines():
            if _line.rstrip().endswith(";"):      # 原型声明，跳过
                continue
            _mm = _fn_def_re.match(_line)
            if _mm:
                _seen_fns.setdefault(_mm.group(1), []).append(_fn)
for _fn, _files in sorted(_seen_fns.items()):
    check(len(_files) == 1,
          "host 接缝的 %s 被定义了 %d 次（%s）—— 重复定义只在链接期报错，"
          "极易误判成构建被打断" % (_fn, len(_files), ", ".join(_files)))
check(len(_seen_fns) > 0, "未在 host 接缝里扫到任何 platform_* 定义（正则失配？）")

# ══════════════════════════════════════════════════════════════════════
# 24) host C 单元测试的构建入口必须覆盖 host 接缝全部 .c 文件。
#     历史坑（2026-10-17 首次真跑）：Makefile SRC 漏了
#     platform_led.c / platform_gpio.c / platform_usb.c 三个文件，
#     编译 1307 步全绿、卡在链接期 undefined symbol，极像「构建被打断」。
#     因为从未链接成功过，顺带掩盖了「测试拓扑过期」等一串问题。
#     构建入口有两个（Makefile 给 gcc/make，run_c_tests.sh 给 zig），
#     任一只覆盖一半都不算数。
# ══════════════════════════════════════════════════════════════════════
_host_files = sorted(_f for _f in os.listdir(_host_dir) if _f.endswith(".c"))
check(len(_host_files) > 0, "platform/host/ 下没扫到 .c 文件")

def _check_build_entry(_label, _src):
    """构建入口的源文件清单必须包含 host 接缝全部 .c。"""
    _names = set(_re.findall(r'platform/host/([A-Za-z0-9_]+\.c)', _src))
    for _f in _host_files:
        check(_f in _names,
              "%s 漏了 host 源文件 %s —— 链接期 undefined symbol 的典型根因"
              % (_label, _f))

_mk = open(os.path.join(_HERE, "Makefile"), encoding="utf-8").read()
_check_build_entry("tests/Makefile", _mk)

_sh = open(os.path.join(_HERE, "run_c_tests.sh"), encoding="utf-8").read()
_check_build_entry("tests/run_c_tests.sh", _sh)

# 两个入口的源文件集合必须一致（否则等于维护两份真相，必漂移）
_mk_names = set(_re.findall(r'platform/host/([A-Za-z0-9_]+\.c)', _mk))
_sh_names = set(_re.findall(r'platform/host/([A-Za-z0-9_]+\.c)', _sh))
check(_mk_names == _sh_names,
      "Makefile 与 run_c_tests.sh 的 host 源文件清单不一致：%s"
      % (_mk_names ^ _sh_names))

print("\ncontract checks done; fails=%d" % _fails)
sys.exit(1 if _fails else 0)
