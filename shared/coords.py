"""
Qshou —— 三级坐标系换算与标定求解（纯函数，无硬件依赖）

坐标链路（见规格 coordinate_mapping / input 章节；2026-10-01 起改为相对鼠标模型）：
    CAP  (cap_x, cap_y)   摄像头画面像素，范围 [0, CAM_W) x [0, CAM_H)
    SCR  (scr_x, scr_y)   屏幕物理像素，范围 [0, SCR_W) x [0, SCR_H)  (Qshou: 1080 x 2400)
    HID  (hid_x, hid_y)   虚拟光标坐标空间，范围 [0, 10000]

换算链：  CAP --H(单应)--> SCR --线性缩放--> HID(virtual) --打包--> 5 字节 waypoint 包

相对鼠标模型说明：
    固件 v23 起 HID 设备为「相对鼠标」，只能从当前光标做相对位移，无法「跳变定位」。
    因此这里 HID 坐标是一份「虚拟光标坐标空间」：控制器用它记录光标当前位置，
    把『目标绝对 SCR 坐标』换算成『相对增量 (dx, dy)』再下发。
    5 字节包 [state][X_MSB][X_LSB][Y_MSB][Y_LSB] 现在是 **swipe 的双路点包**
    （start / end）；固件 /api/v1/swipe 自行差分 (end - start) 得到相对增量。

本模块是整个项目的坐标唯一真源，固件侧 ble_hid/ 与控制器侧 controller/ 都引用此处的
常量与算法（固件契约测试逐字节校验 hid_to_report 与固件镜像一致）。

不依赖真实 homography：真实 H 由安装后 9 点标定求得（seam_h_matrix），
本模块用合成 H 即可完整开发与测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional

import numpy as np

# 虚拟光标坐标空间上界（相对鼠标模型：CAP→SCR→HID 把屏幕映射到一个连续坐标空间，
# 仅用于 /api/v1/swipe 的 start/end 路点差分得到相对增量；不再对应 Digitizer 描述符）
HID_MAX = 10000

# 5 字节 waypoint 包布局（用于 /api/v1/swipe 的 start/end 路点；
# 固件自行差分 (end - start) 得到相对鼠标增量，不含 Report ID 前缀字节）
HID_REPORT_LEN = 5
HID_STATE_DOWN = 0x03  # 路点（按下/移动）状态字节
HID_STATE_UP = 0x00    # 抬起状态字节


class CoordError(Exception):
    """坐标换算或标定过程中的可恢复错误。"""


class CalibrationError(CoordError):
    """标定失败（残差超阈、点不足、退化等）。"""


# ---------------------------------------------------------------------------
# 单应矩阵求解（DLT + Hartley 归一化，保证数值稳定）
# ---------------------------------------------------------------------------

def _normalize_points(pts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """将 Points 归一化到均值 0、平均到原点距离 sqrt(2)，返回 (T, norm_pts)。"""
    mean = pts.mean(axis=0)
    d = np.sqrt(((pts - mean) ** 2).sum(axis=1)).mean()
    s = np.sqrt(2.0) / (d if d > 1e-12 else 1.0)
    T = np.array([
        [s, 0.0, -s * mean[0]],
        [0.0, s, -s * mean[1]],
        [0.0, 0.0, 1.0],
    ], dtype=float)
    norm = (pts - mean) * s
    return T, norm


def solve_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """
    求解将 src(Nx2) 映射到 dst(Nx2) 的单应矩阵 H (3x3, H[2,2]=1)。
    src 为相机像素，dst 为屏幕像素。需要 >= 4 对对应点。
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.shape[0] < 4 or src.shape != dst.shape:
        raise CoordError("solve_homography 需要 >=4 对同形对应点")
    if np.any(np.isnan(src)) or np.any(np.isnan(dst)):
        raise CoordError("solve_homography 输入含 NaN")

    Ts, s_norm = _normalize_points(src)
    Td, d_norm = _normalize_points(dst)

    A = []
    for (x, y), (u, v) in zip(s_norm, d_norm):
        A.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y, -u])
        A.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y, -v])
    A = np.asarray(A, dtype=float)

    try:
        _, _, Vt = np.linalg.svd(A)
    except np.linalg.LinAlgError as e:
        raise CalibrationError(f"SVD 失败: {e}") from e

    h = Vt[-1]
    if abs(h[8]) < 1e-18:
        raise CalibrationError("退化的单应矩阵 (h33≈0)")
    Hn = (h / h[8]).reshape(3, 3)

    # 反归一化: H = Td^-1 * Hn * Ts
    H = np.linalg.inv(Td) @ Hn @ Ts
    if abs(H[2, 2]) < 1e-18:
        raise CalibrationError("退化单应矩阵 (H22≈0)")
    return H / H[2, 2]


def apply_homography(H: np.ndarray, x: float, y: float) -> Tuple[float, float]:
    """对单点 (x, y) 施加单应变换，返回去齐次化后的 (x', y')。"""
    p = np.array([x, y, 1.0], dtype=float)
    q = H @ p
    if abs(q[2]) < 1e-12:
        raise CoordError("齐次坐标 w ≈ 0，无法去齐次化")
    return float(q[0] / q[2]), float(q[1] / q[2])


# ---------------------------------------------------------------------------
# 标定结果
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    """
    一次完整标定的结果。

    H                : 相机像素 -> 屏幕像素 的单应矩阵 (3x3)
    residual_offset  : SCR 空间内的平移修正 (dx, dy)，吸收系统性偏差
                       （如面板坐标 vs 应用可用区原点偏移）。
    scr_w, scr_h     : 屏幕分辨率（应回填 target_device / B2 实测值；默认 Qshou 全屏）
    errors           : 各标定点修正后残差 (px)，用于诊断
    device_id        : 标定所属设备
    loo_max_error    : 留一法交叉验证最大残差 (px)，鲁棒性指标
    """
    H: List[List[float]]
    residual_offset: List[float]
    scr_w: int
    scr_h: int
    errors: List[float] = field(default_factory=list)
    loo_max_error: float = 0.0
    device_id: Optional[str] = None

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        d = asdict(self)
        d["H"] = np.asarray(self.H).tolist()
        d["residual_offset"] = list(self.residual_offset)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        return cls(
            H=np.asarray(d["H"], dtype=float),
            residual_offset=np.asarray(d["residual_offset"], dtype=float),
            scr_w=int(d["scr_w"]),
            scr_h=int(d["scr_h"]),
            errors=list(d.get("errors", [])),
            loo_max_error=float(d.get("loo_max_error", 0.0)),
            device_id=d.get("device_id"),
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Calibration":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    # ---- 换算接口 ----
    def cap_to_scr(self, cap_x: float, cap_y: float) -> Tuple[float, float]:
        sx, sy = apply_homography(np.asarray(self.H, dtype=float), cap_x, cap_y)
        dx, dy = self.residual_offset
        return sx - dx, sy - dy

    def scr_to_hid(self, scr_x: float, scr_y: float) -> Tuple[float, float]:
        if self.scr_w <= 0 or self.scr_h <= 0:
            raise CoordError("屏幕分辨率未设置")
        hid_x = scr_x / self.scr_w * HID_MAX
        hid_y = scr_y / self.scr_h * HID_MAX
        return _clamp_hid(hid_x), _clamp_hid(hid_y)

    def cap_to_hid(self, cap_x: float, cap_y: float) -> Tuple[float, float]:
        sx, sy = self.cap_to_scr(cap_x, cap_y)
        return self.scr_to_hid(sx, sy)

    def cap_to_report(self, cap_x: float, cap_y: float,
                      state: int = HID_STATE_DOWN) -> bytes:
        hid_x, hid_y = self.cap_to_hid(cap_x, cap_y)
        return hid_to_report(hid_x, hid_y, state)


# ---------------------------------------------------------------------------
# 标定入口（含 leave-one-out 残差校验 + residual_offset 迭代）
# ---------------------------------------------------------------------------

def calibrate(cap_pts: List[Tuple[float, float]],
              scr_pts: List[Tuple[float, float]],
              scr_w: int,
              scr_h: int,
              device_id: Optional[str] = None,
              max_residual_px: float = 3.0) -> Calibration:
    """
    从对应点对拟合标定。

    cap_pts : 相机中检测到的标定点 [(x,y), ...]
    scr_pts : 屏幕中应出现的标定点 [(x,y), ...]（与 cap_pts 一一对应）
    scr_w/h : 屏幕分辨率
    max_residual_px : 单点修正后残差上限，超过则标定判失败

    返回 Calibration；若残差超阈、点不足或退化则抛 CalibrationError。
    """
    cap = np.asarray(cap_pts, dtype=float)
    scr = np.asarray(scr_pts, dtype=float)
    if len(cap) != len(scr):
        raise CoordError("cap_pts 与 scr_pts 数量必须一致")
    if len(cap) < 4:
        raise CalibrationError("标定至少需要 4 个对应点")

    H = solve_homography(cap, scr)

    # 原始残差 -> 用中位数（对离群点鲁棒）估计系统性平移修正
    raw = np.array([apply_homography(H, *p) for p in cap]) - scr
    offset = np.median(raw, axis=0)

    # 修正后残差
    corr = np.array([apply_homography(H, *p) for p in cap]) - offset
    errors = np.sqrt(((corr - scr) ** 2).sum(axis=1))
    max_err = float(errors.max())

    # 留一法交叉验证：鲁棒性指标，不参与判定但记入结果
    loo_max = 0.0
    n = len(cap)
    for i in range(n):
        H_i = solve_homography(np.delete(cap, i, 0), np.delete(scr, i, 0))
        pred = np.array(apply_homography(H_i, *cap[i])) - offset
        loo_max = max(loo_max, float(np.linalg.norm(pred - scr[i])))

    if max_err > max_residual_px:
        raise CalibrationError(
            f"标定残差超限: max={max_err:.2f}px > {max_residual_px}px "
            f"(loo_max={loo_max:.2f}px, n={n})"
        )

    return Calibration(
        H=H,
        residual_offset=offset,
        scr_w=int(scr_w),
        scr_h=int(scr_h),
        errors=errors.tolist(),
        loo_max_error=loo_max,
        device_id=device_id,
    )


# ---------------------------------------------------------------------------
# HID 量化与 report 打包
# ---------------------------------------------------------------------------

def _clamp_hid(v: float) -> float:
    return max(0.0, min(float(HID_MAX), float(v)))


def hid_to_report(hid_x: float, hid_y: float, state: int = HID_STATE_DOWN) -> bytes:
    """
    把虚拟光标坐标打包为一个 5 字节 waypoint（路点）包：
        [state][X_MSB][X_LSB][Y_MSB][Y_LSB]
    坐标先四舍五入到整数并 clamp 到 [0, 10000]。

    相对鼠标模型下，这个包用于 /api/v1/swipe 的 start 或 end 路点；
    固件读 X/Y 为带符号 16 位并求 (end - start) 得到相对增量。
    """
    hx = int(round(_clamp_hid(hid_x)))
    hy = int(round(_clamp_hid(hid_y)))
    return bytes([
        state & 0xFF,
        (hx >> 8) & 0xFF, hx & 0xFF,
        (hy >> 8) & 0xFF, hy & 0xFF,
    ])


def report_to_hid(report: bytes) -> Tuple[int, int, int]:
    """反向解析 5 字节 report，返回 (state, hid_x, hid_y)。用于测试/调试。"""
    if len(report) != HID_REPORT_LEN:
        raise CoordError(f"report 长度应为 {HID_REPORT_LEN}，实际 {len(report)}")
    state = report[0]
    hid_x = (report[1] << 8) | report[2]
    hid_y = (report[3] << 8) | report[4]
    return state, hid_x, hid_y


# ---------------------------------------------------------------------------
# 相对鼠标光标状态追踪（控制器侧自维护光标位置）
# ---------------------------------------------------------------------------
#
# 相对鼠标无法「跳变定位」，只能从当前位置做相对位移。控制器必须自维护一份
# 「光标当前在虚拟 HID 坐标空间的位置」(CursorState)，把『目标绝对坐标』换算成
# 『相对增量 (dx, dy)』再下发。
#
# 固件 /api/v1/swipe 接收 start/end 两个 5 字节包，自行差分得到相对增量
# (end - start)。故控制器把 start 设为当前光标、end 设为目标，二者之差即相对位移。
#
# 注意：相对鼠标读不回 host 真实光标位置。只要 host 的光标被人为移动（非本控制器
# 驱动），控制器侧的光标信念就会失同步。此时需 re-center（见 CursorState.reset，
# 例如发送 HOME 回到已知锚点后调用）。
#
# 精度提醒：Chrome/Android 指针加速度使「SCR 像素 ↔ HID count」并非严格 1:1，
# 现场应做一次标定确定该比率（本模块只负责坐标换算与增量拆分，比率由 calibration 体现）。

class CursorState:
    """相对鼠标的控制器侧光标状态。坐标落在虚拟 HID 空间 [0, HID_MAX]。"""

    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.x = float(_clamp_hid(x))
        self.y = float(_clamp_hid(y))

    def move_to(self, target_x: float, target_y: float):
        """移动到目标虚拟坐标，返回整型相对增量 (dx, dy) 并更新自身位置。

        固件按整 count 步进，故增量取 int(round())。
        """
        tx = _clamp_hid(target_x)
        ty = _clamp_hid(target_y)
        dx = int(round(tx - self.x))
        dy = int(round(ty - self.y))
        self.x, self.y = tx, ty
        return dx, dy

    def peek_move_to(self, target_x: float, target_y: float):
        """仅计算相对增量，不更新状态（dry-run / 测试用）。"""
        tx = _clamp_hid(target_x)
        ty = _clamp_hid(target_y)
        return int(round(tx - self.x)), int(round(ty - self.y))

    def nudge(self, dx: float, dy: float) -> Tuple[float, float]:
        """按相对增量推进光标信念（相对推进 / 摇杆 / 方向键用）。

        与 move_to 的区别：move_to 的入参是「目标绝对坐标」，nudge 的入参是
        「相对增量」—— 正是相对鼠标的物理语义，无需任何标定即可使用。

        🔴 关键：**钳位到 [0, HID_MAX]**。host 在屏幕边缘会把光标钳住，所以这里的
        信念也同样钳位 —— 二者在越界后反而会**重新对齐**。这正是撞角归零
        （anchor：向负方向猛推到底）能重建绝对定位的物理依据。

        返回**实际采纳**的增量（被钳位时小于入参，可用于诊断越界）。
        """
        nx = _clamp_hid(self.x + float(dx))
        ny = _clamp_hid(self.y + float(dy))
        adopted = (nx - self.x, ny - self.y)
        self.x, self.y = nx, ny
        return adopted

    def at_edge(self, tol: float = 0.5) -> bool:
        """信念是否已贴住屏幕某个边缘（钳位后即视为已归零到边界）。"""
        return (self.x <= tol or self.y <= tol
                or self.x >= HID_MAX - tol or self.y >= HID_MAX - tol)

    def reset(self, x: float = 0.0, y: float = 0.0) -> None:
        """re-center：把控制器侧光标信念对齐到 host 真实光标。

        相对鼠标无法读回 host 光标，只能在已知锚点（如开机居中、发送过 HOME 后）
        调用。默认对齐到 (0,0) 左上角。
        """
        self.x = float(_clamp_hid(x))
        self.y = float(_clamp_hid(y))

    def __repr__(self) -> str:
        return f"CursorState(x={self.x:.1f}, y={self.y:.1f})"


def scr_to_relative_move(cal: "Calibration", cursor: CursorState,
                         scr_x: float, scr_y: float):
    """『移动到指定 SCR 坐标』：SCR→HID 经标定换算，再由 cursor 求相对增量。

    返回 (dx, dy) 并更新 cursor 到目标 HID 坐标。
    """
    hx, hy = cal.scr_to_hid(scr_x, scr_y)
    return cursor.move_to(hx, hy)


def cap_to_relative_move(cal: "Calibration", cursor: CursorState,
                         cap_x: float, cap_y: float):
    """『移动到相机检测到的目标点』：CAP→SCR→HID→相对增量（端到端便捷函数）。"""
    sx, sy = cal.cap_to_scr(cap_x, cap_y)
    return scr_to_relative_move(cal, cursor, sx, sy)


# ---------------------------------------------------------------------------
# 自检（python shared/coords.py）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1) 构造一个已知的仿射+轻微透视 homography，反向生成标定数据
    import sys
    sys.path.insert(0, "tools")
    from fake_h import generate  # noqa: E402

    H_true, scr, cap = generate(n=9, w=1080, h=2400, noise=0.3, seed=7)
    cal = calibrate(cap.tolist(), scr.tolist(), scr_w=1080, scr_h=2400,
                   device_id="qshou-01", max_residual_px=3.0)
    print(f"[OK] 标定通过: n={len(cap)} max_err={max(cal.errors):.3f}px "
          f"loo_max={cal.loo_max_error:.3f}px offset={cal.residual_offset.tolist()}")

    # 2) 中心点与四角的映射一致性
    for label, (cx, cy) in [("中心", (540, 1200)), ("左上", (0, 0)),
                            ("右下", (1080, 2400))]:
        hid = cal.cap_to_hid(cx, cy)
        rep = cal.cap_to_report(cx, cy, HID_STATE_DOWN)
        back = report_to_hid(rep)
        print(f"  {label:>3}: cap=({cx},{cy}) -> hid={tuple(round(v) for v in hid)} "
              f"-> report={rep.hex()} -> back={back}")

    # 3) 序列化往返
    import tempfile, os
    tmp = os.path.join(tempfile.gettempdir(), "_calib_selfcheck.json")
    cal.save(tmp)
    cal2 = Calibration.load(tmp)
    assert np.allclose(np.asarray(cal.H), np.asarray(cal2.H))
    print("[OK] 标定 JSON 序列化往返一致")
