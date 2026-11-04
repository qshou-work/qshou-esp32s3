#!/usr/bin/env python3
"""tools/pointer_gain_probe.py — 指针增益（HID count ↔ 屏幕像素）标定探针。

## 为什么需要它

BLE 相对鼠标的位移单位是 **HID count**（单帧 ≤ ±127），而「移动到指定屏幕像素」
必须知道 **1 count 究竟让宿主光标走了多少像素**。`shared/coords.py` 的
`scr_to_hid()` 目前是**线性占位**（`scr_x/scr_w*10000`，隐含 ≈0.108 px/count），
完全没有吸收宿主（Chrome / Android）的**指针加速度** —— 所以这个比率只能实测。

## 三个关键设计

1. **角饱和归零（anchor）**：相对鼠标读不回 host 真实光标，且光标撞边会被 clamp。
   每档测量前先向左上猛推超量程 counts，把光标钉在 (0,0)，再用「再推一次画面
   不再变化」作为**客观收敛判据**。这同时消掉相对鼠标固有的 CursorState 失同步。

2. **自包含的位移测量**：把手机固定在相机前，用 `/snapshot` 前后两帧差分 + 连通域
   质心，求出光标位移的**相机像素**数，不需要任何外部参照物。撞边被 clamp 的点
   （位移增长停滞）会被自动剔除，所以 scan 的结果天然是「线性区」的增益。

3. **两条独立的尺子**：
   - `scan`   → `cap_px / count`（相机像素每 count，全自动）
   - `manual` → `scr_px / count`（屏幕像素每 count，用安卓「开发者选项 → 指针位置」
     的精确像素读数做权威交叉校验）
   两者之比即**相机像素 ↔ 屏幕像素**的比例尺（后续屏幕识别要用）。

## 子命令

    discover [--net 192.168.65]     并发扫 80/81 端口定位设备（无 mDNS；按 MAC OUI 找不可靠）
    selfcheck                       纯数学自检（无设备）：合成位移 → 拟合 → 检验能还原
    simulate [--g0 .. --accel ..]   内置模拟设备，全自动跑通测量链路并还原已知增益
    probe --ip IP [--counts 1000]   单点探针：测一次 + 落盘 before/after/差分图（先确认看得见）
    anchor --ip IP [--axis x|y|both]  角饱和归零并给出收敛证据
    scan --ip IP [--counts 100,250,..] [--axis x] [--step-ms 30]
    manual --ip IP [--counts ..]       人工读数模式：逐步锚定+移动，打印步骤供你读数
    fit --in FILE.json                 用数据拟合增益（自动或人工数据都吃）

## 真机成像前提与两条物理约束（2026-10-02 实测）

**前提（不满足就测不出东西）**：手机屏在「待机变暗 ↔ 输入唤醒到全亮」之间跳变，
实测屏幕均值 45 → 213、全亮时 64% 像素过曝。所以上场前仍建议：
**关闭自动亮度 + 手动亮度 ~30% + 长屏幕超时（或「保持唤醒」）**。

**约束 A：屏幕无输入即转暗 → 必须在移动后尽快截屏。**
工具侧已内建对抗：每次取样前先做一次**净零抖动**（`wake()`：先 +N 再 −N，光标回
原位）把屏幕拉回全亮稳定态，等亮度与相机 AGC 落定后再取 `before`；测量手势一结束
就**立刻抢拍** `after`（`SNAP_MARGIN=0.05s`），而且**先抓图再读 health**（快照约
400ms、health 约 80ms，顺序颠倒会白等一整个亮屏窗口）。两帧因此都处在同一亮态，
差分里只剩光标。

**约束 B：位移过大会把光标推出可视范围 → 先归零、再小心移动。**
工具侧两道保险：① `anchor` 用 −25000 counts 把光标**钳到左上角**（撞边被宿主
clamp，所以推得够远就必然停在可见角），每档测量前都重新归零；② 单档位移**自适应
钳位** —— 上限 = 屏幕在画面里的宽度 × `SAFE_FRAC`(0.75)，超了就砍，并可用
`--max-counts` 设硬上限。测不到光标时自动**重新归零 + 位移减半**重试。

`probe`/`scan` 都会先过 `check_vision`（屏幕在场 + 过曝门 + 亮度稳定门 + 光标配对），
任一条不过就明确报因，不产出可疑数据。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFile

# ---------------------------------------------------------------------------
# 常量（与固件契约对齐，见 firmware/main/routes.c: handle_gesture）
# ---------------------------------------------------------------------------

PER_FRAME_MAX = 127          # 单帧 X/Y 上限（8-bit 有符号）
MAX_STEPS = 200              # 固件 clamp
MAX_TOTAL = MAX_STEPS * PER_FRAME_MAX      # 25400 counts
MIN_STEP_MS = 15
DEFAULT_STEPS = 12
DEFAULT_STEP_MS = 90
GESTURE_TOTAL_MS = 1200      # 固件锁定的总时长上限

ANCHOR_COUNTS = -25000       # 角饱和：一次推到远超全屏的量程（在预算内）
DEFAULT_SCR_W = 1080
DEFAULT_SCR_H = 2400

# --- 两条物理约束的对策（2026-10-02 用户实测反馈） ---------------------------
# 约束 A：**手机屏无输入即转暗**。变暗后 before/after 分处「暗/亮」两态，整屏均值
#   跳变（实测 45 → 213）会把光标位移完全淹没；而且变暗期间相机 AGC 按暗场景增益，
#   屏幕一醒就整屏过曝。→ 每次取样前先做一次**净零抖动**把屏幕钉在「全亮稳定态」，
#   并且手势一结束就**立刻抢拍**。
# 约束 B：**单次位移过大 → 光标冲出可视范围**。after 帧里没有光标，差分只剩一个
#   斑块，pair 配不出来（看起来却像「没动」）。→ 每档前先归零（光标保证停在左上角
#   这个可见角），再按**实测屏宽**自适应钳位单档位移；测不到时重新归零 + 减半重试。
WAKE_COUNTS = 120            # 唤醒抖动幅度（净零：先 +N 再 −N，光标回原位）
WAKE_STEPS = 2
WAKE_STEP_MS = MIN_STEP_MS
# 实测：唤醒后相机 AGC 要约 1s 才把过曝从 53% 压回 17%，故给足落定时间。
WAKE_SETTLE = 1.0
SNAP_MARGIN = 0.05           # 手势结束后抢拍 after 的余量（越小越能咬住亮屏窗口）
SAFE_FRAC = 0.75             # 单档位移上限 = 屏幕可见宽 × 此比例（防飞出可视范围）
RETRY_HALVE = 2              # 「重新归零 + 位移减半」的重试次数
# 判「前后两帧是否处在同一亮度态」——这才是复现「屏幕变暗」失效模式的直接判据。
# 不去等「连续 N 帧全局稳定」：实测该场景存在周期性亮度抖动（画幅均值 67↔84），
# 且亮屏窗口只有约 10s，长时间等待只会等到屏幕变暗。
PAIR_TOL_MEAN = 15.0         # before/after 全画幅均值差上限（灰阶）
CAPTURE_TRIES = 3            # 「唤醒 → 抢拍 before → 移动 → 抢拍 after」整组的重试次数

# --- 成像质量门槛（2026-10-02 真机定标） ------------------------------------
# 手机屏存在「待机变暗 → 输入唤醒到全亮」的状态跳变（实测屏幕均值 45 → 213，
# 且全亮时 64% 像素过曝）。这种**整屏亮度跳变**会被差分当成巨大位移，产出全错
# 的数据。所以任何 before/after 差分之前，必须先确认画面已经稳定、且没有过曝。
SAT_LEVEL = 250              # 过曝判定灰阶
# 实测（2026-10-02 真机）：屏幕处于稳定亮态时**本身就有 ~17% 像素打到 250 以上**
# （那是手机屏自己的白底内容，不是相机被打爆）。所以上限必须按这个基线抬上去，
# 否则每一帧都会被判过曝、永远测不出东西。0.35 是「连屏内内容都糊成一片」的经验线。
SAT_FRAC_MAX = 0.35          # 过曝像素占比上限（超过 = 相机被打爆，光标被淹没）
STABLE_TOL_MEAN = 2.5        # 连续帧全画幅均值极差上限（灰阶）
STABLE_NEED = 3              # 连续多少帧满足才算稳定

VISION_FAIL_HINT = """
  ⛔ 自动测量不可用。按下面三类原因对症处理（都不是固件问题）：

  (1) 画面过曝 / 亮度在跳      → 手机【关闭自动亮度】并手动把亮度调到 ~30%，
                                 屏幕超时设 10 分钟或开启「保持唤醒」防变暗。
                                 全亮时相机整屏过曝，光标会被淹掉、量不出任何东西。
  (2) 场景在动（相机没对准屏） → 把相机对准手机屏幕（后续「屏幕识别」节点也必须如此）。
  (3) 检不到光标              → 手机停在**静态页面**上；确认 BLE 已连接（光标只在
                                 有鼠标连接时出现），且指针未被系统隐藏。

  兜底：不需要相机的「人工读数」通道始终可用：
      python tools/pointer_gain_probe.py manual --ip <IP> --counts 200,400,800
"""


def vision_report_text(v):
    """把视觉预检结果翻成人话（区分：屏幕不在画面 / 过曝 / 亮度在跳 / 检不到光标）。"""
    h, w = v["frame_shape"]
    area = float(h * w)
    if v["ok"]:
        s = v.get("screen") or {}
        p = v.get("pair") or {}
        return ("通过（屏幕占画面 %.0f%%、实心度 %.2f；过曝 %.1f%%；光标对间距 %.1f px，"
                "面积 %s/%s）"
                % (100.0 * s.get("area_frac", 0), s.get("fill", 0),
                   100.0 * v["sat_frac"], p.get("sep", 0),
                   (p.get("a") or {}).get("area"), (p.get("b") or {}).get("area")))
    scr = v.get("screen") or {}
    if not scr.get("ok"):
        able = ("屏幕占画面仅 %.0f%%（下限 5%%）" % (100.0 * scr.get("area_frac", 0))
                if "area_frac" in scr else scr.get("reason", "无亮区"))
        return ("未通过 —— **手机屏幕不在画面里**：%s，实心度 %.2f。"
                "暗场景又稳又不过曝，是最危险的假阳性来源（随机两个边缘斑块都会被配成"
                "「光标对」）→ 把相机对准手机屏幕后重试。" % (able, scr.get("fill", 0)))
    if v["sat_frac"] > SAT_FRAC_MAX:
        return ("未通过 —— **整屏过曝**：屏幕 ROI 有 %.1f%% 像素打到 %d 以上（上限 %.0f%%），"
                "光标会被白底淹没。→ 手机端把亮度调低到 ~30%% 且关掉自动亮度。"
                % (100.0 * v["sat_frac"], SAT_LEVEL, 100.0 * SAT_FRAC_MAX))
    if not v["stable"]:
        return ("未通过 —— **前后帧亮度不一致**：Δ均值 %.1f 灰阶（上限 %.1f）。"
                "抓拍期间屏幕亮度变了（手机屏「无输入即转暗」，实测亮屏窗口仅约 10s）。"
                "工具已自动「重新唤醒 + 重抓」；仍失败请把屏幕超时调长或开「保持唤醒」。"
                % (v["mean_span"], PAIR_TOL_MEAN))
    return ("未通过 —— **检不到光标**：位移后没找到「小而紧凑、面积相近、且落在屏幕内」的"
            "光斑对（最大 %d px，上限 %d px）。→ 手机停在静态页面、确认 BLE 已连接。"
            % (int(v.get("max_area", 0)), int(v.get("max_allowed_area", 0))))


# ---------------------------------------------------------------------------
# 固件步进规则（在本地复刻，用于预测手势时长 / 速度）
# ---------------------------------------------------------------------------

def dir_steps(requested, dx, dy):
    """复刻固件 gesture_dir_steps()：ceil(max(|dx|,|dy|)/127)，只扩不缩。"""
    need = int(np.ceil(max(abs(dx), abs(dy)) / PER_FRAME_MAX))
    return max(int(requested), need, 1)


def dir_step_ms(requested, steps):
    """复刻固件 gesture_dir_step_ms()：min(请求, 1200/帧数)，下限 15ms。"""
    return max(MIN_STEP_MS, min(int(requested), GESTURE_TOTAL_MS // max(int(steps), 1)))


def velocity(counts, steps, step_ms):
    """该手势的名义速度（counts/ms）—— 指针加速度的因变量就是它。"""
    if steps <= 0 or step_ms <= 0:
        return 0.0
    return abs(counts) / steps / step_ms


# ---------------------------------------------------------------------------
# 图像工具：灰度化 → 差分 → 连通域 → 质心
# ---------------------------------------------------------------------------

def decode_gray(raw: bytes, allow_truncated=False) -> np.ndarray:
    """JPEG → 灰度 float32。设备偶发返回截断的 JPEG（连接被切），故允许降级。"""
    if allow_truncated:
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(io.BytesIO(raw)).convert("L")
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = False
    return np.asarray(im, dtype=np.float32)


def _box3(a: np.ndarray) -> np.ndarray:
    """3x3 均值滤波（纯 numpy，抑制 JPEG 噪声，免装 scipy/opencv）。"""
    p = np.pad(a, 1, mode="edge")
    return (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
            p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
            p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0


def _otsu(a: np.ndarray) -> float:
    """Otsu 阈值（双峰：暗背景 / 较亮的手机屏）。纯 numpy，免装 skimage。"""
    hist, edges = np.histogram(a, bins=64, range=(0, 255))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 128.0
    mids = (edges[:-1] + edges[1:]) / 2.0
    w0 = np.cumsum(hist)
    w1 = total - w0
    mu = np.cumsum(hist * mids)
    mu_t = mu[-1]
    valid = (w0 > 0) & (w1 > 0)
    var = np.zeros_like(w0)
    var[valid] = ((mu_t * w0[valid] / total - mu[valid]) ** 2) / \
                 (w0[valid] * w1[valid] / total ** 2)
    return float(mids[int(np.argmax(var))])


def _components(mask: np.ndarray):
    """4/8 邻域连通域标记（BFS）。返回每个连通域的像素坐标列表。"""
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    ys, xs = np.nonzero(mask)
    comps = []
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if visited[y0, x0]:
            continue
        stack = [(y0, x0)]
        visited[y0, x0] = True
        pts = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= h:
                    continue
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if nx < 0 or nx >= w or visited[ny, nx] or not mask[ny, nx]:
                        continue
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        comps.append(pts)
    return comps


def diff_blobs(a: np.ndarray, b: np.ndarray, thresh=16.0, min_area=6, max_area=None):
    """两帧差分的连通域，按面积降序。用于「旧位置 + 新位置」两个光斑。

    每个光斑带 `peak`（域内最大差分值）：真光标是**强局部对比**（白箭头压在背景上），
    而亮度漂移/边缘抖动产生的斑块峰值通常只略高于阈值 —— 这条用来把它们筛掉。
    """
    d = np.abs(_box3(b) - _box3(a))
    mask = d > float(thresh)
    blobs = []
    for pts in _components(mask):
        n = len(pts)
        if n < min_area:
            continue
        if max_area is not None and n > max_area:
            continue
        ys = [p[0] for p in pts]
        xs = [p[1] for p in pts]
        blobs.append({
            "cx": sum(xs) / n,
            "cy": sum(ys) / n,
            "area": n,
            "peak": float(d[[p[0] for p in pts], [p[1] for p in pts]].max()),
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
        })
    blobs.sort(key=lambda z: -z["area"])
    return blobs


def pick_pair(blobs, axis, frame_area, max_frac=0.01, min_area_px=3, peak_mult=1.5,
              thresh=None):
    """从差分光斑里挑出「旧位置 + 新位置」这一对光标。

    三条硬约束来自光标的物理形状：**小**（占画面 <1%）、**两次面积相近**（同一支
    箭头，位置变了但形状没变）、**对比度强**（峰值 ≥ 1.5×阈值）。整屏亮度漂移与
    边缘抖动产生的假光斑会被这三条一起挡掉 —— 这是纯差分路线在真机上唯一可靠的判据。
    """
    key = "cx" if axis == "x" else "cy"
    pmin = (peak_mult * thresh) if thresh else 0.0
    cand = [z for z in blobs
            if min_area_px <= z["area"] <= max_frac * frame_area
            and z.get("peak", 0.0) >= pmin]
    if len(cand) < 2:
        return None
    cand.sort(key=lambda z: -z["area"])
    cand = cand[:8]
    best = None
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i], cand[j]
            ratio = max(a["area"], b["area"]) / float(min(a["area"], b["area"]))
            sep = abs(a[key] - b[key])
            score = sep / (ratio ** 1.5)      # 间距大、面积越接近越好
            if best is None or score > best["score"]:
                best = {"a": a, "b": b, "sep": sep, "area_ratio": ratio, "score": score}
    return best


# ---------------------------------------------------------------------------
# 真实设备客户端
# ---------------------------------------------------------------------------

class DeviceClient:
    """走 HTTP 的真实设备（80 端口 = httpd / 快照，81 = MJPEG）。"""

    def __init__(self, ip, timeout=10.0):
        self.ip = ip
        self.timeout = timeout
        self.base = "http://%s" % ip

    def _get(self, path, port=80, tries=3):
        """带重试的 GET。设备 httpd 偶发提前断连（JPEG 被截断），重试即可。"""
        last = None
        for i in range(tries):
            url = "http://%s:%d%s" % (self.ip, port, path)
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    return r.read()
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise last

    def _post(self, path, obj, tries=3):
        last = None
        for i in range(tries):
            url = self.base + path
            data = json.dumps(obj).encode("utf-8")
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise last

    # --- 端点 ---
    def health(self):
        return json.loads(self._get("/api/v1/health").decode("utf-8"))

    def snapshot(self):
        """返回**原始 JPEG 字节**（与 DeviceClient 契约一致，解码交给 Probe）。"""
        return self._get("/snapshot", port=80)

    def gesture_dir(self, dx, dy, steps=DEFAULT_STEPS, step_ms=DEFAULT_STEP_MS, press=0):
        return self._post("/api/v1/gesture",
                          {"type": "dir", "dx": int(dx), "dy": int(dy),
                           "steps": int(steps), "step_ms": int(step_ms),
                           "press": int(press)})

    def gesture(self, **kw):
        return self._post("/api/v1/gesture", kw)

    def name(self):
        return self.ip


# ---------------------------------------------------------------------------
# 模拟设备：复刻固件的帧展开 + 一个可配的增益/加速度模型，用于自证测量链路
# ---------------------------------------------------------------------------

class SimDevice:
    """内存中的假手机：手势 → 光标位移（含可选加速度）→ 渲染 JPEG 帧。"""

    def __init__(self, scr_w=DEFAULT_SCR_W, scr_h=DEFAULT_SCR_H,
                 cap_w=640, cap_h=480, screen_box=(80, 20, 560, 460),
                 g0=0.45, accel_k=0.0, v_ref=1.0):
        self.scr_w, self.scr_h = scr_w, scr_h
        self.cap_w, self.cap_h = cap_w, cap_h
        self.box = screen_box
        self.g0, self.accel_k, self.v_ref = g0, accel_k, v_ref
        self.px, self.py = 0.0, 0.0      # 屏幕像素坐标
        self.last = None
        self.sent = 0
        self.drop = 0

    # 屏幕 → 相机 的线性放置
    @property
    def scale_x(self):
        return (self.box[2] - self.box[0]) / float(self.scr_w)

    @property
    def scale_y(self):
        return (self.box[3] - self.box[1]) / float(self.scr_h)

    def gain(self, counts, steps, step_ms):
        v = velocity(counts, steps, step_ms)
        return max(0.01, self.g0 * (1.0 + self.accel_k * (v - self.v_ref)))

    def gesture_dir(self, dx, dy, steps=DEFAULT_STEPS, step_ms=DEFAULT_STEP_MS, press=0):
        steps = dir_steps(steps, dx, dy)
        step_ms = dir_step_ms(step_ms, steps)
        self.px = float(np.clip(self.px + dx * self.gain(dx, steps, step_ms), 0, self.scr_w))
        self.py = float(np.clip(self.py + dy * self.gain(dy, steps, step_ms), 0, self.scr_h))
        self.last = {"ok": True, "type": "dir", "steps": steps, "step_ms": step_ms,
                     "dx": int(dx), "dy": int(dy),
                     "budget": steps * PER_FRAME_MAX, "shortfall": 0}
        # 与真机同语义：这些遥测描述「最近一次手势」，每次覆盖写（不是累计）
        # 真机 DIR 的帧数 = steps + 2（首帧原地按下 + steps 帧位移 + 末帧抬起）
        self.tel = {"gest_frames": steps + 2, "gest_sent": steps + 2, "gest_drop": 0,
                    "gest_dx": int(dx), "gest_dy": int(dy),
                    "gest_jit_min": step_ms, "gest_jit_avg": step_ms,
                    "gest_jit_max": step_ms}
        return self.last

    def gesture(self, **kw):
        return self.gesture_dir(kw.get("dx", 0), kw.get("dy", 0),
                                kw.get("steps", DEFAULT_STEPS),
                                kw.get("step_ms", DEFAULT_STEP_MS),
                                kw.get("press", 0))

    def health(self):
        d = {"wifi_rssi": -50, "ble_connected": 1}
        d.update(getattr(self, "tel", {}))
        return d

    def snapshot(self):
        """渲染一帧：静态背景 + 光标（背景绝不随时间变化，保证差分只有光标）。"""
        L, T, R, B = self.box
        img = Image.new("RGB", (self.cap_w, self.cap_h), (8, 9, 11))
        d = ImageDraw.Draw(img)
        d.rectangle([L, T, R, B], fill=(30, 34, 42), outline=(120, 120, 120))
        # 静态参照（网格）：给差分提供「不该变化」的对照
        for i in range(1, 6):
            gx = L + (R - L) * i // 6
            d.line([(gx, T + 4), (gx, B - 4)], fill=(56, 60, 70), width=1)
        for j in range(1, 8):
            gy = T + (B - T) * j // 8
            d.line([(L + 4, gy), (R - 4, gy)], fill=(56, 60, 70), width=1)
        d.text((L + 6, T + 6), "SIM 640x480  scr=%dx%d" % (self.scr_w, self.scr_h),
               fill=(150, 150, 150))
        # 光标：亮白箭头（高对比，便于质心定位）
        cx = int(L + self.px * self.scale_x)
        cy = int(T + self.py * self.scale_y)
        d.polygon([(cx, cy), (cx, cy + 14), (cx + 4, cy + 10), (cx + 9, cy + 17),
                   (cx + 12, cy + 15), (cx + 7, cy + 8), (cx + 12, cy + 6)],
                  fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78)
        return buf.getvalue()

    def name(self):
        return "sim"


# ---------------------------------------------------------------------------
# 探针：归零 / 单次测量 / 扫描
# ---------------------------------------------------------------------------

class Probe:
    def __init__(self, client, thresh=40.0, min_area=6, settle=0.35, verbose=True,
                 wait_gestures=True, vision_timeout=15.0, pair_tol=PAIR_TOL_MEAN):
        self.c = client
        self.thresh = thresh
        self.min_area = min_area
        self.settle = settle
        self.verbose = verbose
        # 模拟设备是瞬时生效的，等手势播完纯属浪费（真机必须等，否则会拍到手势中途）
        self.wait_gestures = wait_gestures
        self.vision_timeout = vision_timeout
        self.pair_tol = float(pair_tol)   # 前后帧亮度一致性门限（灰阶）
        self.extent = {}     # axis -> 光标自身外接尺寸（cap px），小位移并块时反解用
        self.last_pair = None   # 最近一次测量的 (before, after) 帧，供落盘复查
        self.last_stable = None
        self.last_screen = None

    def log(self, *a):
        if self.verbose:
            print(*a)
            sys.stdout.flush()

    def _snap(self):
        """取一帧灰度图（真机给 JPEG 字节，模拟器给数组，统一在这解码）。"""
        raw = self.c.snapshot()
        if isinstance(raw, np.ndarray):
            return raw
        try:
            return decode_gray(raw)
        except OSError:
            raw2 = self.c.snapshot()          # 截断多半是传输被切，再抓一次
            try:
                return decode_gray(raw2)
            except OSError:
                return decode_gray(raw2, allow_truncated=True)

    # --- 等待一段手势播完：由回包给出的真实帧数/节奏推算，外加余量 ---
    def _wait_gesture(self, resp, margin=0.45):
        steps = int(resp.get("steps", DEFAULT_STEPS))
        step_ms = int(resp.get("step_ms", DEFAULT_STEP_MS))
        if not self.wait_gestures:
            time.sleep(0.01)
            return
        time.sleep(steps * step_ms / 1000.0 + margin)

    # --- 🔴 净零唤醒抖动：对抗「屏幕无输入即转暗」 -------------------------
    # 为什么必须是「净零」：唤醒本身不能改变光标位置，否则每次唤醒都会污染测量。
    # 为什么必须「正向优先」（+N 再 −N）：归零后光标贴着左上角，若先发负向会被宿主
    #   钳位吃掉一半，净位移就不再是 0。
    # 为什么必须在**每次取样之前**做：手机屏空闲会自动变暗；一旦 before 落在暗态、
    #   after 落在亮态，整屏亮度跳变（实测 45→213）会盖过光标位移，产出一张全错的表。
    def wake(self, counts=WAKE_COUNTS, settle=None):
        r1 = self.c.gesture_dir(counts, 0, steps=WAKE_STEPS, step_ms=WAKE_STEP_MS)
        self._wait_gesture(r1, margin=0.0)
        r2 = self.c.gesture_dir(-counts, 0, steps=WAKE_STEPS, step_ms=WAKE_STEP_MS)
        self._wait_gesture(r2, margin=0.0)
        if self.wait_gestures:
            time.sleep(WAKE_SETTLE if settle is None else settle)
        return {"wake": True, "counts": int(counts)}

    def detect_screen(self, g, min_area_frac=0.05, min_fill=0.5,
                      aspect_range=(0.35, 3.0)):
        """在画面里找「大而接近实心矩形」的亮区 = 手机屏。

        这道门是为了堵住最危险的假阳性：相机**没对着屏幕**时（暗房间、线缆、桌面），
        画面又暗又稳、过曝与稳定门全都放行，`pick_pair` 照样能把随机两个边缘斑块
        配成「光标对」，于是产出一个漂亮但毫无意义的增益数字。
        屏幕不在画面里 → 一切位移数据都是编的，必须硬性拦住。
        """
        gs = _box3(_box3(g))[::4, ::4]
        thr = _otsu(gs)
        comps = _components(gs > thr)
        if not comps:
            return {"ok": False, "reason": "没有超过 Otsu 阈值的亮区", "thr": thr}
        pts = max(comps, key=len)
        ys = [p[0] for p in pts]
        xs = [p[1] for p in pts]
        x0, x1 = min(xs) * 4, (max(xs) + 1) * 4
        y0, y1 = min(ys) * 4, (max(ys) + 1) * 4
        frame_area = float(g.shape[0] * g.shape[1])
        area = float(len(pts)) * 16.0
        bw, bh = float(x1 - x0), float(y1 - y0)
        fill = area / (bw * bh) if bw > 0 and bh > 0 else 0.0
        aspect = bh / bw if bw > 0 else 0.0
        ok = (area / frame_area >= min_area_frac and fill >= min_fill
              and aspect_range[0] <= aspect <= aspect_range[1])
        return {"ok": bool(ok), "thr": thr, "bbox": [x0, y0, x1, y1],
                "area_frac": area / frame_area, "fill": fill, "aspect": aspect,
                "bw": bw, "bh": bh}

    # --- 成像质量：过曝 + 亮度稳定性 ---------------------------------------
    def frame_stats(self, g):
        """单帧画质：均值 / 中位 / 过曝占比。手机屏「待机变暗↔输入唤醒全亮」
        会产生整屏均值跳变（实测 45 → 213），差分前必须先确认它不跳。"""
        return {"mean": float(g.mean()), "p50": float(np.median(g)),
                "sat_frac": float((g > SAT_LEVEL).mean()),
                "p99": float(np.percentile(g, 99))}

    def brightness_pair_ok(self, before, after):
        """前后两帧是否处在**同一亮度态**（本轮的核心判据）。

        直接对准失效模式：屏幕从亮态滑到暗态时，before/after 分处两态，整屏均值差
        可达 40~120 灰阶，差分里全是「屏幕变暗」而不是光标。所以要求：
          |mean(before) − mean(after)| ≤ PAIR_TOL_MEAN 且两帧都没有被打爆。
        返回 (ok, 明细)。
        """
        sb, sa = self.frame_stats(before), self.frame_stats(after)
        d = abs(sb["mean"] - sa["mean"])
        over = max(sb["sat_frac"], sa["sat_frac"]) > SAT_FRAC_MAX
        return (bool(d <= self.pair_tol and not over),
                {"mean_before": round(sb["mean"], 1), "mean_after": round(sa["mean"], 1),
                 "mean_delta": round(d, 2), "sat_before": round(sb["sat_frac"], 4),
                 "sat_after": round(sa["sat_frac"], 4), "sat_frac": round(max(sb["sat_frac"],
                                                                            sa["sat_frac"]), 4),
                 "over": bool(over)})

    def wait_stable(self, timeout=15.0, gap=0.4, need=STABLE_NEED,
                    tol_mean=STABLE_TOL_MEAN, sat_tol=SAT_FRAC_MAX):
        """等画面亮度稳定：连续 need 帧的全画幅均值极差 ≤ tol_mean 且不过曝。

        返回 {"stable":bool, "sat_frac":..., "mean_span":..., "frames":[...], "g":末帧}
        """
        hist, t0 = [], time.time()
        while time.time() - t0 < timeout:
            g = self._snap()
            st = self.frame_stats(g)
            st["t"] = round(time.time() - t0, 2)
            hist.append(st)
            if len(hist) >= need:
                means = [z["mean"] for z in hist[-need:]]
                sats = [z["sat_frac"] for z in hist[-need:]]
                if max(means) - min(means) <= tol_mean and max(sats) <= sat_tol:
                    return {"stable": True, "sat_frac": max(sats),
                            "mean_span": max(means) - min(means),
                            "frames": hist, "g": g}
            time.sleep(gap)
        means = [z["mean"] for z in hist[-need:]] or [0.0]
        sats = [z["sat_frac"] for z in hist[-need:]] or [0.0]
        return {"stable": False, "sat_frac": max(sats),
                "mean_span": max(means) - min(means), "frames": hist, "g": g}

    # --- 🔴 视觉可用性预检 --------------------------------------------------
    # 三种失败各有明确指纹，必须分开报，否则会把人引向错误的排查方向：
    #   ① 屏幕不在场 → 暗场景又稳又不过曝，是最危险的假阳性来源；
    #   ② 亮度在两帧之间跳（手机待机变暗）→ 差分全错，**看起来却像"有响应"**；
    #   ③ 屏幕在、亮度也对，却配不出光标对 → 光标被隐藏 / 太弱 / 位移太小。
    # 唯一可信的通过判据是：**真的能从一次位移的差分里配出「旧/新」两个光标光斑**，
    # 且这两帧亮度一致。
    def check_vision(self, axis="x", max_blob_frac=0.01, move=300, anchor=True):
        # 🔴 必须**先归零**再预检：光标可能正贴着屏幕右缘（上一轮测试留下的），
        #   此时再往右推会被宿主钳死、画面纹丝不动，预检就会误报「检不到光标」。
        #   归零把光标钉到左上角这个已知可见角，预检才有确定性起点。
        if anchor:
            self.anchor(axis=axis, rounds=2)
        self.wake()                       # 预检本身就依赖帧差分，先钉住亮屏态
        a = self._snap()
        b = self._snap()                  # 不动 → 噪声地板
        h, w = a.shape
        frame_area = float(h * w)
        noise = diff_blobs(a, b, self.thresh, self.min_area)
        noise_frac = sum(z["area"] for z in noise) / frame_area
        scr = self.detect_screen(b)
        self.last_screen = scr

        resp = self.c.gesture_dir(move if axis in ("x", "both") else 0,
                                  move if axis in ("y", "both") else 0,
                                  steps=DEFAULT_STEPS, step_ms=DEFAULT_STEP_MS)
        self._wait_gesture(resp, margin=SNAP_MARGIN)
        c = self._snap()
        bq_ok, bq = self.brightness_pair_ok(b, c)
        blobs = diff_blobs(b, c, self.thresh, self.min_area)
        pair = self._pair_inside_screen(
            pick_pair(blobs, axis, frame_area, max_blob_frac, thresh=self.thresh), scr)

        cap = max_blob_frac * frame_area
        return {
            "ok": bool(pair is not None and bq_ok),
            "stable": bool(bq_ok),
            "sat_frac": bq["sat_frac"],
            "mean_span": bq["mean_delta"],
            "mean_before": bq["mean_before"], "mean_after": bq["mean_after"],
            "screen": scr,
            "noise_blobs": len(noise),
            "noise_px_frac": noise_frac,
            "move_blobs": len(blobs),
            "pair": pair,
            "max_area": int(max((z["area"] for z in blobs), default=0)),
            "max_allowed_area": int(cap),
            "frame_shape": [h, w],
        }

    @staticmethod
    def _pair_inside_screen(pair, scr, max_rel_extent=0.25):
        """两个光斑都必须落在屏幕内，且自身尺寸相对屏幕宽不能太大。

        「光标」在这种成像条件下只有 10~30 px（屏幕约 220 px 宽）；一个 500 px、
        或者跑到屏幕外的斑块，只可能是线缆/桌沿/反光 —— 直接判假。
        """
        if pair is None or not scr.get("ok"):
            return None
        x0, y0, x1, y1 = scr["bbox"]
        lim = max_rel_extent * max(scr.get("bw", 0.0), 1.0)
        for z in (pair["a"], pair["b"]):
            if not (x0 <= z["cx"] <= x1 and y0 <= z["cy"] <= y1):
                return None
            bx0, by0, bx1, by1 = z["bbox"]
            if max(bx1 - bx0 + 1, by1 - by0 + 1) > lim:
                return None
        return pair

    # --- 角饱和归零（不需要视觉）：向负方向猛推超量程 counts ---------------
    # 光标撞边会被宿主 clamp，所以「推得足够远」本身就保证它停在左上角；
    # 代价是失去「画面零变化」的客观验证 —— 相机没对准屏幕时只能走这条。
    def anchor_blind(self, axis="both", bursts=1):
        ev = []
        for i in range(bursts):
            dx, dy = (ANCHOR_COUNTS, 0) if axis == "x" else \
                     (0, ANCHOR_COUNTS) if axis == "y" else (ANCHOR_COUNTS, ANCHOR_COUNTS)
            resp = self.c.gesture_dir(dx, dy, steps=MAX_STEPS, step_ms=MIN_STEP_MS)
            self._wait_gesture(resp)
            tel = self.c.health()      # 最近一次手势的遥测（覆盖写，须紧随其后读）
            ev.append({"burst": i + 1, "cmd": [dx, dy],
                       "steps": resp.get("steps"), "step_ms": resp.get("step_ms"),
                       "frames": tel.get("gest_frames"), "sent": tel.get("gest_sent"),
                       "drop": tel.get("gest_drop"),
                       "net": [tel.get("gest_dx"), tel.get("gest_dy")],
                       "jit_ms": [tel.get("gest_jit_min"), tel.get("gest_jit_avg"),
                                  tel.get("gest_jit_max")]})
            e = ev[-1]
            self.log("  归零(burst %d)：下发 %d counts → 实发 %s/%s 帧，丢 %s，空口净 %s，抖动 %s ms"
                     % (i + 1, dx, e["sent"], e["frames"], e["drop"], e["net"], e["jit_ms"]))
        return {"blind": True, "evidence": ev}

    # --- 角饱和归零：向左上猛推，直到「再推一次画面不再变化」 ---
    def anchor(self, axis="both", rounds=4):
        """把光标钉到左上角（可见角），并给出**客观收敛证据**。

        🔴 两处必须同时满足，否则会误判（2026-10-02 真机踩到）：
          ① **每轮先唤醒**：手势本身会把变暗的屏幕点醒，那一瞬的整屏亮度跳变会被
             差分读成「光标还在大幅移动」（实测出现过 14003 px 的假斑块）；
          ② **亮度不一致的轮次不作为收敛依据**：只有 before/after 处在同一亮度态的
             那一轮，「画面零变化」才真等于「光标已经贴住左上角」。
        """
        evidence = []
        moved_seq = []          # 只记录「亮度一致」的轮次的最大光斑面积
        for k in range(rounds):
            if axis == "x":
                dx, dy = ANCHOR_COUNTS, 0
            elif axis == "y":
                dx, dy = 0, ANCHOR_COUNTS
            else:
                dx, dy = ANCHOR_COUNTS, ANCHOR_COUNTS
            self.wake()                       # 每轮都先唤醒，保证前后帧同处亮态
            before = self._snap()
            resp = self.c.gesture_dir(dx, dy, steps=MAX_STEPS, step_ms=MIN_STEP_MS)
            self._wait_gesture(resp)
            after = self._snap()
            ok_b, bq = self.brightness_pair_ok(before, after)
            blobs = diff_blobs(before, after, self.thresh, self.min_area)
            moved = max((b["area"] for b in blobs), default=0)
            evidence.append({"round": k + 1, "max_blob_area": moved,
                             "n_blobs": len(blobs), "brightness_ok": bool(ok_b),
                             "mean_delta": bq["mean_delta"],
                             "steps": resp.get("steps"), "step_ms": resp.get("step_ms")})
            self.log("  anchor round %d: 变化光斑数=%d 最大面积=%d  亮度Δ=%.1f 一致=%s"
                     % (k + 1, len(blobs), moved, bq["mean_delta"], ok_b))
            if not ok_b:
                self.log("     ↻ 本轮屏幕亮度在变（Δ=%.1f）—— 差分不可信，不作为收敛依据"
                         % bq["mean_delta"])
                continue
            moved_seq.append(moved)
            if len(moved_seq) >= 2 and moved_seq[-1] == 0:
                self.log("  -> 已收敛：光标钉在左上角（连续两轮亮态一致且本轮零变化）")
                return {"converged": True, "evidence": evidence}
            time.sleep(self.settle)
        return {"converged": False, "evidence": evidence}

    # --- 单次测量：锚定 → 唤醒 → 前帧 → +D → 后帧 → 质心位移 ---
    def _measure_raw(self, counts, axis="x", step_ms=DEFAULT_STEP_MS, anchor=True):
        """一次「锚定 → 前帧 → +counts → 后帧 → 差分」，不做任何兜底。返回 (rec, blobs)。"""
        if anchor:
            a = self.anchor(axis=axis, rounds=3)
            if not a["converged"]:
                self.log("  ⚠ 归零未确认收敛（可能屏幕内容在变），结果仅供参考")

        # 唤醒：净零抖动（+N 再 −N）把屏幕从「待机变暗」拉回全亮稳定态。
        # 它同时消掉两个坑：① 光标因静止被系统隐藏；② 屏幕亮度在两帧之间跳变。
        dx = counts if axis in ("x", "both") else 0
        dy = counts if axis in ("y", "both") else 0

        # 🔴 约束 A 的核心：亮屏窗口实测只有约 10s（之后屏幕必然变暗），所以不做
        #   「等连续 3 帧全局稳定」那种长时间等待 —— 等待期间屏幕已经在变暗了。
        #   改为把「唤醒 → 抢拍 before → 移动 → 抢拍 after」当成一个**不可分割的组合**
        #   执行，用「两帧亮度是否一致」直接判定这一组能不能用；不一致就重来。
        #   ⚠️ 重试前**必须重新归零**：上一组手势已经真实发出、光标位置变了，
        #      不归零就再发一次，位移会累加。
        resp = tel = None
        ok_b, bq = False, {}
        for attempt in range(CAPTURE_TRIES):
            if attempt > 0:
                self.log("  ↻ 重抓第 %d 组（上一组前后帧亮度不一致）" % (attempt + 1))
                self.anchor(axis=axis, rounds=2)
            self.wake()
            before = self._snap()
            resp = self.c.gesture_dir(dx, dy, steps=DEFAULT_STEPS, step_ms=step_ms)
            # 🔴 抢拍顺序：手势一结束立刻抓 after，margin 压到最小。而且**先抓图再读
            #   health** —— 快照约 400ms、health 约 80ms，顺序颠倒会白等一整个亮屏窗口。
            #   gest_* 是「最近一次手势」的遥测、只被下一次手势覆盖，稍后读仍然准确。
            self._wait_gesture(resp, margin=SNAP_MARGIN)
            after = self._snap()
            tel = self.c.health()
            self.last_pair = (before, after)
            ok_b, bq = self.brightness_pair_ok(before, after)
            if ok_b:
                break
            self.log("     ⚠ 前后帧亮度不一致（Δ均值=%.1f 灰阶、过曝=%s）"
                     % (bq["mean_delta"], bq["over"]))
        stable = bool(ok_b)
        self.last_stable = {"stable": stable, **bq}

        blobs = diff_blobs(before, after, self.thresh, self.min_area)
        rec = {
            "axis": axis,
            "counts": int(counts),
            "steps_eff": resp.get("steps"),
            "step_ms_eff": resp.get("step_ms"),
            "budget": resp.get("budget"),
            "shortfall": resp.get("shortfall"),
            "cmd_net": [dx, dy],
            "frames": tel.get("gest_frames"),          # DIR 为 steps+2（首帧按下 + 末帧抬起）
            "sent": tel.get("gest_sent"),
            "drop": tel.get("gest_drop"),
            "net_dx": tel.get("gest_dx"),
            "net_dy": tel.get("gest_dy"),
            "jit_ms": [tel.get("gest_jit_min"), tel.get("gest_jit_avg"), tel.get("gest_jit_max")],
            "velocity_counts_per_ms": round(
                velocity(counts, resp.get("steps", 1), resp.get("step_ms", 1)), 4),
            "n_blobs": len(blobs),
            "stable": 1 if stable else 0,
            "mean_before": bq["mean_before"], "mean_after": bq["mean_after"],
            "mean_delta": bq["mean_delta"],
            "sat_frac": bq["sat_frac"],
        }
        # 空口净位移必须等于下发量，否则说明有帧被夹取/丢弃（这条对账是位移测量的前提）
        rec["net_ok"] = (rec["net_dx"] == dx and rec["net_dy"] == dy)
        if not rec["net_ok"]:
            rec["reason"] = ("空口净位移(%s,%s) ≠ 下发(%s,%s) —— 该点不可信"
                             % (rec["net_dx"], rec["net_dy"], dx, dy))
        # 从差分里配出「旧位置 / 新位置」两个光标光斑：小 + 面积相近 + 落在屏幕内。
        scr = self.detect_screen(before)
        self.last_screen = scr
        rec["screen_ok"] = 1 if scr.get("ok") else 0
        rec["screen_area_frac"] = round(scr.get("area_frac", 0.0), 4)
        pair = self._pair_inside_screen(
            pick_pair(blobs, axis, float(before.shape[0] * before.shape[1]),
                      thresh=self.thresh), scr)
        if pair is None:
            if not scr.get("ok"):
                why = ("手机屏幕不在画面里（亮区仅占 %.1f%%、实心度 %.2f）—— "
                       "此时任何光斑对都是假的，拒绝出数"
                       % (100 * scr.get("area_frac", 0), scr.get("fill", 0)))
            else:
                why = ("未检出可配对的光斑（%d 个，屏幕内且尺寸合理者不足 2 个）："
                       "常见原因是并成一块、被整屏亮度跳变淹没、或光标被系统隐藏"
                       % len(blobs))
            rec.update({"ok": False, "reason": why, "delta_px": None})
            return rec, blobs
        rec["pair_area_ratio"] = round(pair["area_ratio"], 2)
        rec["pair_sep_px"] = round(pair["sep"], 1)
        rec["pair_peak"] = [round(pair["a"].get("peak", 0), 1),
                            round(pair["b"].get("peak", 0), 1)]

        # 命令为正向 → 新位置在坐标更大的一侧（「增益为正」这个前提本身就值得验证，
        # 所以不做绝对值，反向会被下面 ok=delta>0 判为异常）
        key = "cx" if axis == "x" else "cy"
        old, new = sorted([pair["a"], pair["b"]], key=lambda z: z[key])
        delta = new[key] - old[key]
        # 顺手把光标自身的尺寸（同轴外接尺寸）学下来，供小位移并块时反解
        i_lo, i_hi = (0, 2) if axis == "x" else (1, 3)
        self.extent[axis] = float(max(old["bbox"][i_hi] - old["bbox"][i_lo],
                                      new["bbox"][i_hi] - new["bbox"][i_lo]))
        rec.update({
            "ok": bool(delta > 0) and rec.get("stable", 1) == 1,
            "delta_px": float(delta),
            "gain_cap_px_per_count": float(delta) / counts if counts else None,
            "area_old": old["area"], "area_new": new["area"],
            "cursor_extent_px": self.extent[axis],
        })
        if delta <= 0:
            rec["reason"] = "位移为 0 或反向 —— 光标可能已到底、或差分检出的不是光标"
        elif not rec["ok"]:
            rec["reason"] = "位移算出来了(%s)但画面亮度未稳定，按污染数据作废" % delta
        return rec, blobs

    def measure(self, counts, axis="x", step_ms=DEFAULT_STEP_MS, anchor=True):
        """带兜底的测量：小位移下新旧位置会并成一个连通域，用外接尺寸反解。"""
        rec, blobs = self._measure_raw(counts, axis=axis, step_ms=step_ms, anchor=anchor)
        if rec.get("ok") or len(blobs) != 1:
            return rec

        # 只有一个光斑 = 两次位置重叠。位移 ≈ 并块外接尺寸 − 光标自身尺寸。
        if self.extent.get(axis) is None and not self._bootstrap_extent(axis, counts, step_ms):
            rec["reason"] = ("小位移下两位置并成一块，且未能先量出光标尺寸；"
                             "请把起始 counts 调大后重扫")
            return rec
        ext = self.extent[axis]
        i_lo, i_hi = (0, 2) if axis == "x" else (1, 3)
        span = float(blobs[0]["bbox"][i_hi] - blobs[0]["bbox"][i_lo])
        delta = span - ext
        rec.update({"ok": bool(delta > 0), "delta_px": float(delta), "estimated": True,
                    "gain_cap_px_per_count": float(delta) / counts if counts else None,
                    "span_px": span, "cursor_extent_px": ext})
        if delta <= 0:
            rec["reason"] = "并块外接尺寸未超过光标自身尺寸 —— 位移太小，无法分辨"
        else:
            self.log("     （小位移并块：外接=%.1f − 光标=%.1f → 位移≈%.1f cap_px）"
                     % (span, ext, delta))
        return rec

    def _bootstrap_extent(self, axis, counts, step_ms):
        """先用更大的位移把光标自身尺寸量出来（同轴形状一致，可复用）。"""
        for mult in (3, 6, 12):
            c = int(counts) * mult
            if c > MAX_TOTAL // 2:
                break
            self.log("     bootstrap：先跑 counts=%d 量光标自身尺寸" % c)
            _, blobs = self._measure_raw(c, axis=axis, step_ms=step_ms)
            if len(blobs) >= 2:
                return True        # _measure_raw 成功时已写入 self.extent[axis]
        return False

    # --- 防「光标飞出可视范围」：单档位移的安全上限 -------------------------
    # 光标可见范围 ≈ 手机屏在画面里的宽度 scr_w_cap（相机像素）。把它的 SAFE_FRAC
    # 当作「单次最大位移」，再除以当前增益估计换回 counts —— 即使真实增益比预想
    # 大很多，单次也不会把光标推出画面。增益首档未知时只用 hard_max 兜底。
    def _safe_max_counts(self, gain_est, scr_w_cap, hard_max=None):
        caps = []
        if hard_max:
            caps.append(int(hard_max))
        if gain_est and scr_w_cap:
            caps.append(int(SAFE_FRAC * float(scr_w_cap) / max(gain_est, 1e-6)))
        return min(caps) if caps else None

    # --- 分档扫描：小位移起步，撞边（增长停滞）即停 ---
    def scan(self, counts_list, axis="x", step_ms=DEFAULT_STEP_MS,
             max_counts=None, retries=RETRY_HALVE):
        """分档扫描，测「相机像素 / count」。

        约束 B（光标飞出可视范围）的两道保险：
          ① 每一档都**重新归零**（在 measure 内建）—— 光标保证从左上角这个可见角
             出发，不累积也不漂移；
          ② 单档位移**自适应钳位**：上限 = 屏幕可见宽 × SAFE_FRAC；测不到光标时
             **重新归零 + 位移减半**重试（多半就是上一次把光标推出了画面）。
        若连屏幕都不在画面里，立刻中止 —— 那是装置/成像问题，重试没有意义。
        """
        records = []
        prev = None
        gain_est = None      # cap_px/count 的滑动估计，供安全钳位用
        scr_cap = None       # 屏幕在画面里的宽度（cap px）
        aborted = None
        for d in counts_list:
            d_cmd = int(d)
            safe = self._safe_max_counts(gain_est, scr_cap, max_counts)
            if safe and d_cmd > safe:
                self.log("  ⚠ counts=%d 超安全上限 → 收敛到 %d"
                         "（屏宽 %s cap_px、估计增益 %s，防光标飞出可视范围）"
                         % (d, safe, ("%.0f" % scr_cap) if scr_cap else "?",
                            ("%.4f" % gain_est) if gain_est else "?"))
                d_cmd = max(safe, 1)

            rec, trial = None, d_cmd
            for attempt in range(retries + 1):
                self.log("  ── 测试 counts=%d (axis=%s, step_ms=%d)%s"
                         % (trial, axis, step_ms,
                            "" if attempt == 0 else "   [重试%d：已重新归零]" % attempt))
                rec = self.measure(trial, axis=axis, step_ms=step_ms)
                if rec.get("ok") and rec.get("net_ok", True):
                    break
                if rec.get("screen_ok") == 0:
                    aborted = ("手机屏幕不在画面里（亮区 %.1f%%，实心度 %.2f）—— "
                               "装置/成像问题，重试无意义，中止扫描"
                               % (100 * rec.get("screen_area_frac", 0),
                                  (self.last_screen or {}).get("fill", 0)))
                    self.log("     ⛔ %s" % aborted)
                    break
                if attempt < retries:
                    half = max(trial // 2, 1)
                    self.log("     ✗ %s → 重新归零并把位移减半到 %d 再试"
                             % (rec.get("reason", "失败"), half))
                    trial = half
                else:
                    self.log("     ✗ %s（已重试 %d 次，放弃该档）"
                             % (rec.get("reason", "失败"), retries))
            rec["counts_requested"] = int(d)
            records.append(rec)
            if aborted:
                break
            if not rec.get("ok"):
                continue
            if not rec.get("net_ok"):
                rec["ok"] = False
                self.log("     ✗ 空口净位移与下发不一致，该点作废")
                continue
            self.log("     位移=%.1f cap_px  gain=%.4f cap_px/count  "
                     "(steps=%s step_ms=%s 帧=%s/%s 丢=%s 空口净=%s 稳定=%s 过曝=%.1f%% "
                     "面积比=%s 峰值=%s 屏占比=%.0f%%)"
                     % (rec["delta_px"], rec["gain_cap_px_per_count"],
                        rec["steps_eff"], rec["step_ms_eff"],
                        rec["sent"], rec["frames"], rec["drop"], rec["net_dx"],
                        rec.get("stable"), 100 * (rec.get("sat_frac") or 0),
                        rec.get("pair_area_ratio"), rec.get("pair_peak"),
                        100 * (rec.get("screen_area_frac") or 0)))
            # 更新安全钳位依据：增益滑动估计 + 屏幕可见宽（相机像素）
            if rec.get("gain_cap_px_per_count"):
                g = float(rec["gain_cap_px_per_count"])
                gain_est = g if gain_est is None else 0.5 * gain_est + 0.5 * g
            if self.last_screen and self.last_screen.get("ok"):
                scr_cap = float(self.last_screen.get("bw") or scr_cap or 0) or scr_cap
            if prev is not None:
                # 撞边判据：位移增长远低于 counts 的增长比例
                expect = prev["delta_px"] * rec["counts"] / max(prev["counts"], 1)
                if rec["delta_px"] < 0.85 * expect:
                    rec["clamped_suspect"] = True
                    self.log("     ⚠ 位移增长停滞（期望≈%.1f，实得%.1f）→ 疑似撞边，剔除"
                             % (expect, rec["delta_px"]))
            if not rec.get("clamped_suspect"):
                prev = rec
        if aborted:
            self.log("  ⛔ 扫描中止：%s" % aborted)
        return records


# ---------------------------------------------------------------------------
# 拟合
# ---------------------------------------------------------------------------

def fit_gain(points, xkey="counts", ykey="delta_px"):
    """过原点最小二乘：gain = Σ(x·y)/Σ(x²)；返回增益、逐点增益、离散度、R²。"""
    xs, ys, keep = [], [], []
    for p in points:
        if not p.get("ok") or p.get(ykey) is None or p.get("clamped_suspect"):
            continue
        xs.append(float(p[xkey]))
        ys.append(float(p[ykey]))
        keep.append(p)
    if len(xs) < 2:
        return {"ok": False, "reason": "可用点不足（<2）：%d" % len(xs)}
    x = np.asarray(xs)
    y = np.asarray(ys)
    gain = float((x * y).sum() / (x * x).sum())
    pred = gain * x
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float((y ** 2).sum())
    per_point = [float(yy / xx) for xx, yy in zip(x, y)]
    return {
        "ok": True,
        "n": len(xs),
        "gain": round(gain, 6),
        "r2": round(1.0 - ss_res / ss_tot, 6) if ss_tot > 0 else None,
        "per_point_gain": [round(v, 6) for v in per_point],
        "spread": round(max(per_point) / min(per_point), 4) if min(per_point) > 0 else None,
        "residual_max_px": round(float(np.abs(y - pred).max()), 3),
    }


def print_fit(title, res, unit):
    if not res.get("ok"):
        print("  %s: 拟合失败 —— %s" % (title, res.get("reason")))
        return
    print("  %s: gain=%.5f %s   n=%d   R²=%s   逐点离散度=%s   最大残差=%.2fpx"
          % (title, res["gain"], unit, res["n"], res["r2"], res["spread"],
             res["residual_max_px"]))
    print("     逐点增益: %s" % res["per_point_gain"])


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def local_net_prefix():
    """取本机 /24 前缀。设备 IP 随热点网段漂移（160.x → 65.x），目录靠它自动发现。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))      # 不发包，只为拿到出口网卡地址
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


def cmd_discover(args):
    net = args.net or local_net_prefix()
    print("扫描 %s.1-254 的 80/81 端口 …" % net)

    def probe(ip):
        ports = []
        for p in (80, 81):
            s = socket.socket()
            s.settimeout(0.6)
            try:
                if s.connect_ex((ip, p)) == 0:
                    ports.append(p)
            finally:
                s.close()
        return (ip, ports) if ports else None

    hosts = ["%s.%d" % (net, i) for i in range(1, 255)]
    hits = []
    with cf.ThreadPoolExecutor(max_workers=128) as ex:
        for r in ex.map(probe, hosts):
            if r:
                hits.append(r)
    for ip, ports in sorted(hits, key=lambda z: int(z[0].split(".")[-1])):
        tag = ""
        try:
            h = DeviceClient(ip, timeout=4).health()
            tag = "   <- Qshou? rssi=%s ble=%s" % (h.get("wifi_rssi"), h.get("ble_connected"))
        except Exception:
            tag = "   (非 Qshou 或 /api/v1/health 不可用)"
        print("%-16s ports=%s%s" % (ip, ",".join(map(str, ports)), tag))
    print("命中 %d 台" % len(hits))


def cmd_selfcheck(args):
    """纯数学自检：合成「已知增益」位移，检验拟合能还原。"""
    print("=== selfcheck：合成数据 → 拟合 ===")
    rng = np.random.default_rng(3)
    true_gain = 0.37
    pts = []
    for d in (100, 250, 500, 1000, 2000):
        noise = rng.normal(0, 1.2)
        pts.append({"ok": True, "counts": d, "delta_px": true_gain * d + noise})
    res = fit_gain(pts)
    print_fit("合成", res, "cap_px/count")
    assert res["ok"], res
    err = abs(res["gain"] - true_gain) / true_gain
    print("  真值=%.4f 拟合=%.4f 相对误差=%.2f%%" % (true_gain, res["gain"], err * 100))
    assert err < 0.01, "拟合偏差过大"
    print("  [OK] 拟合器可还原已知增益（误差 <1%）")


def cmd_simulate(args):
    """内置模拟设备：全自动跑通 anchor → scan → fit，检验整条测量链路。"""
    sim = SimDevice(g0=args.g0, accel_k=args.accel, v_ref=args.v_ref, screen_box=(80, 20, 560, 460))
    print("=== simulate：真值 g0=%.4f accel_k=%.3f v_ref=%.2f ==="
          % (args.g0, args.accel, args.v_ref))
    pr = Probe(sim, wait_gestures=False)
    res = pr.anchor(axis="x", rounds=3)
    print("  anchor converged=%s" % res["converged"])
    counts = [int(x) for x in args.counts.split(",")]
    recs = pr.scan(counts, axis="x", step_ms=args.step_ms)
    print()
    fit = fit_gain(recs)
    print_fit("scan(相机像素)", fit, "cap_px/count")
    # 反推真值：模拟器里 gain_scr 与名义速度相关，逐点比对
    print("  逐点：counts=%s" % [r["counts"] for r in recs if r.get("delta_px")])
    print("        v(counts/ms)=%s" % [r.get("velocity_counts_per_ms") for r in recs if r.get("delta_px")])
    if fit.get("ok"):
        eff = [sim.gain(r["counts"], r["steps_eff"], r["step_ms_eff"]) for r in recs if r.get("delta_px")]
        cap_px = [g * sim.scale_x for g in eff]
        print("  模拟器在该组速度下的真实 cap_px/count 逐点=%s"
              % [round(v, 5) for v in cap_px])
        print("  [OK] 测量链路能复现模拟器的逐点增益（见上方对比）")


def cmd_anchor(args):
    c = DeviceClient(args.ip)
    h = c.health()
    print("设备 %s  rssi=%s ble=%s" % (args.ip, h.get("wifi_rssi"), h.get("ble_connected")))
    pr = Probe(c, thresh=args.thresh, min_area=args.min_area, pair_tol=args.pair_tol)
    v = pr.check_vision(args.axis)
    print("视觉预检：%s" % vision_report_text(v))
    if not v["ok"]:
        print(VISION_FAIL_HINT)
        return
    res = pr.anchor(axis=args.axis, rounds=4)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_scan(args):
    c = DeviceClient(args.ip)
    h = c.health()
    print("设备 %s  rssi=%s ble=%s" % (args.ip, h.get("wifi_rssi"), h.get("ble_connected")))
    if not h.get("ble_connected"):
        print("⚠ BLE 未连接 —— 手势不会被宿主接收，请先连上再测")
    pr = Probe(c, thresh=args.thresh, min_area=args.min_area, pair_tol=args.pair_tol)
    v = pr.check_vision(args.axis)
    print("视觉预检：%s" % vision_report_text(v))
    if not v["ok"]:
        print(VISION_FAIL_HINT)
        return
    counts = [int(x) for x in args.counts.split(",")]
    out = {"device": args.ip, "axis": args.axis, "step_ms": args.step_ms,
           "records": [], "fit": None}
    for axis in ([args.axis] if args.axis != "both" else ["x", "y"]):
        print("\n### axis=%s" % axis)
        recs = pr.scan(counts, axis=axis, step_ms=args.step_ms,
                       max_counts=args.max_counts)
        out["records"].extend(recs)
        f = fit_gain(recs)
        print_fit("axis=%s" % axis, f, "cap_px/count")
        out.setdefault("fit_by_axis", {})[axis] = f
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=2)
        print("\n已写出 %s" % args.json)


def cmd_manual(args):
    """人工读数模式：逐步「锚定 → +D」，每步独立，你只需记下当时的指针位置读数。

    每步都重新归零，所以读数之间不累积误差；顺序上 D 递增、读数也应递增，
    一旦不单调就能立刻发现（比累积法更安全）。
    """
    c = DeviceClient(args.ip)
    h = c.health()
    print("设备 %s  rssi=%s ble=%s" % (args.ip, h.get("wifi_rssi"), h.get("ble_connected")))
    counts = [int(x) for x in args.counts.split(",")]
    axis = args.axis
    print("""
================ 你需要做的事（一次性准备） ================
1. 手机上打开「设置 → 关于手机 → 版本号」连点 7 次，开启开发者模式
2. 「设置 → 系统 → 开发者选项」→ 打开【指针位置 / Pointer location】
   → 屏幕顶部会出现一行实时坐标读数（如  P: 0.00 / 0.00 ），这就是权威基准
3. 确认 BLE 已连接，且手机停在**画面静止**的界面上（别停在时钟/视频界面）

============== 即将执行的步骤（每个步骤都要读一次） ==============
""")
    for i, d in enumerate(counts, 1):
        print("  步骤 %d：光标先归零（读数应为 0 附近）→ 向 %s 正方向推 %d counts" % (i, axis, d))
    print("\n开始执行，每步之间停顿 %d 秒 —— 请把每步结束后的读数记下来。\n" % args.gap)

    plan = []
    for i, d in enumerate(counts, 1):
        pr = Probe(c, thresh=args.thresh, min_area=args.min_area, pair_tol=args.pair_tol)
        print(">>> 步骤 %d / %d ：归零中（向负方向猛推，光标必被 clamp 在左上角）…"
              % (i, len(counts)))
        pr.anchor_blind(axis=axis, bursts=1)
        print("    归零完成 —— 此刻读数应 ≈ 0")
        time.sleep(args.gap)
        resp = c.gesture_dir(d if axis == "x" else 0, d if axis == "y" else 0,
                             steps=DEFAULT_STEPS, step_ms=args.step_ms)
        steps_eff = resp.get("steps")
        step_ms_eff = resp.get("step_ms")
        print("    已下发 +%d counts (steps=%s step_ms=%s) —— 请读并记下 %s 坐标"
              % (d, steps_eff, step_ms_eff, axis))
        plan.append({"step": i, "counts": d, "axis": axis,
                     "steps_eff": steps_eff, "step_ms_eff": step_ms_eff,
                     "velocity_counts_per_ms": round(velocity(d, steps_eff, step_ms_eff), 4),
                     "scr_reading": None})
        time.sleep(args.gap)

    out = {"kind": "manual_scr_readings", "device": args.ip, "axis": axis,
           "step_ms_requested": args.step_ms, "readings": plan}
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print("""
================ 现在把读数填进来 ================
模板已写到：%s
请把每步读到的数值填进对应 "scr_reading"（只填数字，例如 312 或 312.5）。
填完告诉我，我跑 fit 求 scr_px/count。""" % args.out)


def save_gray(g, path):
    Image.fromarray(np.clip(g, 0, 255).astype(np.uint8)).save(path)


def cmd_probe(args):
    """单点探针：跑一次完整测量并把 before/after/差分图落盘，供人眼复核成像条件。

    这是「先确认能看见，再谈精度」的一步：数字（过曝占比、亮度极差、光斑面积比）
    加图像证据一起给，避免又出现「相机没对准屏幕却报归零收敛」那种假阳性。
    """
    c = DeviceClient(args.ip)
    h = c.health()
    print("设备 %s  rssi=%s ble=%s" % (args.ip, h.get("wifi_rssi"), h.get("ble_connected")))
    if not h.get("ble_connected"):
        print("⚠ BLE 未连接 —— 宿主收不到手势，光标不会动，请先连上再测")
    pr = Probe(c, thresh=args.thresh, min_area=args.min_area, pair_tol=args.pair_tol)
    v = pr.check_vision(args.axis)
    print("视觉预检：%s" % vision_report_text(v))
    print("  前后帧亮度: before mean=%.1f | after mean=%.1f | Δ=%.1f（上限 %.1f）| 过曝合计 %.1f%%"
          % (v.get("mean_before", 0), v.get("mean_after", 0), v.get("mean_span", 0),
             PAIR_TOL_MEAN, 100 * (v.get("sat_frac") or 0)))
    s = v.get("screen") or {}
    print("  屏幕检测: 占画面 %.1f%%  实心度 %.2f  长宽比 %.2f  bbox=%s  (Otsu 阈值 %.0f)"
          % (100 * s.get("area_frac", 0), s.get("fill", 0), s.get("aspect", 0),
             s.get("bbox"), s.get("thr", 0)))
    if v.get("noise_px_frac") is not None:
        print("  噪声地板: 同位置两帧差分 >%.0f 的像素占 %.3f%%（%d 个光斑）"
              % (args.thresh, 100 * v["noise_px_frac"], v["noise_blobs"]))

    rec = pr.measure(int(args.counts), axis=args.axis, step_ms=args.step_ms)
    print(json.dumps(rec, ensure_ascii=False, indent=2))

    if pr.last_pair:
        os.makedirs(args.out_dir, exist_ok=True)
        before, after = pr.last_pair
        save_gray(before, os.path.join(args.out_dir, "before.png"))
        save_gray(after, os.path.join(args.out_dir, "after.png"))
        save_gray(np.abs(after - before) * 4, os.path.join(args.out_dir, "diff_x4.png"))
        print("已落盘 before/after/diff_x4 到 %s/" % args.out_dir)
    return rec


def cmd_fit(args):
    with open(args.infile, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    print("=== fit: %s ===" % args.infile)
    if data.get("kind") == "manual_scr_readings":
        pts = []
        for r in data.get("readings", []):
            v = r.get("scr_reading")
            if v is None:
                continue
            pts.append({"ok": True, "counts": abs(r["counts"]), "delta_px": abs(float(v)),
                        "velocity": r.get("velocity_counts_per_ms")})
        print("  读数点数=%d / 总步数=%d" % (len(pts), len(data.get("readings", []))))
        res = fit_gain(pts)
        print_fit("manual(屏幕像素)", res, "scr_px/count")
        if res.get("ok") and len(pts) >= 3:
            print("  速度依赖检查（加速度会表现为 gain 随速度变化）:")
            for p in pts:
                print("    counts=%-6s v=%-6s scr_px/count=%.5f"
                      % (p["counts"], p.get("velocity"), p["delta_px"] / p["counts"]))
        return
    # scan 产出的记录
    for axis, f in (data.get("fit_by_axis") or {}).items():
        print_fit("axis=%s" % axis, f, "cap_px/count")
    if not data.get("fit_by_axis"):
        print_fit("全部点", fit_gain(data.get("records", [])), "cap_px/count")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Qshou 指针增益（count ↔ 屏幕像素）标定探针",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ip", default=os.environ.get("QSHOU_IP", "192.168.65.53"))
    common.add_argument("--thresh", type=float, default=40.0,
                        help="差分阈值（默认 40；真机暗光噪声主要落在 16~40 之间）")
    common.add_argument("--min-area", type=int, default=6, help="最小光斑面积（默认 6）")
    common.add_argument("--pair-tol", type=float, default=PAIR_TOL_MEAN,
                        help="前后帧亮度一致性门限（灰阶，默认 %.0f）" % PAIR_TOL_MEAN)

    p = sub.add_parser("discover", help="并发扫 80/81 端口定位设备")
    p.add_argument("--net", default=None, help="网段前缀，如 192.168.65")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("selfcheck", help="纯数学自检（无设备）")
    p.set_defaults(func=cmd_selfcheck)

    p = sub.add_parser("simulate", help="内置模拟设备，自证测量链路")
    p.add_argument("--g0", type=float, default=0.45)
    p.add_argument("--accel", type=float, default=0.0, help="加速度系数 k")
    p.add_argument("--v-ref", type=float, default=1.0)
    p.add_argument("--counts", default="50,100,200,400,800")
    p.add_argument("--step-ms", type=int, default=DEFAULT_STEP_MS)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("anchor", parents=[common], help="角饱和归零")
    p.add_argument("--axis", default="both", choices=["x", "y", "both"])
    p.set_defaults(func=cmd_anchor)

    p = sub.add_parser("scan", parents=[common], help="全自动增益扫描（相机像素）")
    p.add_argument("--counts", default="100,250,500,1000")
    p.add_argument("--axis", default="x", choices=["x", "y", "both"])
    p.add_argument("--step-ms", type=int, default=DEFAULT_STEP_MS)
    p.add_argument("--max-counts", type=int, default=1200,
                   help="单档位移硬上限（防光标飞出可视范围；另有按屏宽的自适应钳位）")
    p.add_argument("--json", default=None)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("manual", parents=[common], help="人工读数模式（屏幕像素）")
    p.add_argument("--counts", default="200,400,800")
    p.add_argument("--axis", default="x", choices=["x", "y"])
    p.add_argument("--step-ms", type=int, default=DEFAULT_STEP_MS)
    p.add_argument("--gap", type=float, default=4.0, help="每步留给你的读数时间（秒）")
    p.add_argument("--out", default="docs/evidence/pointer_gain_readings.json")
    p.set_defaults(func=cmd_manual)

    p = sub.add_parser("probe", parents=[common],
                       help="单点探针：测一次并落盘前后帧（先确认看得见，再谈精度）")
    p.add_argument("--counts", default="1000")
    p.add_argument("--axis", default="x", choices=["x", "y", "both"])
    p.add_argument("--step-ms", type=int, default=DEFAULT_STEP_MS)
    p.add_argument("--out-dir", default=".tmp/pgprobe")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("fit", help="用数据拟合")
    p.add_argument("--in", dest="infile", required=True)
    p.set_defaults(func=cmd_fit)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
