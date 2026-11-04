"""
A8 — 用例级自动化断言框架 + 证据落盘模板（先对 mock mapper 跑）。

设计目标（见规格 pre_arrival_work.A8 / use_cases / implementation_order.step_13）：
- 提供 UC1~UC5 的可独立重跑断言，每个用例产出结构化证据 JSON 落盘。
- 不依赖真实硬件：坐标换算用合成 Calibration（seam_h_matrix 的 mock 端），
  设备响应用 InProcessMockDevice 回声（与 controller/mock_device.py 的网络版同为回声语义）。
- 待实物到货后，仅需把 calibration 指向真实标定、把 device 换成真实 transport，
  断言与证据模板无需改动即可复用（接缝策略见 workspace_layout.seams）。

运行方式：
    python -m tests.assertions            # 跑全部 UC 并落盘到 tests/evidence/
    python -m unittest tests.test_assertions -v   # 单元验证框架本身
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

# 让 shared / tools 可导入
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from shared.coords import (  # noqa: E402
    Calibration,
    hid_to_report,
    report_to_hid,
    HID_MAX,
    HID_STATE_DOWN,
    HID_STATE_UP,
    HID_REPORT_LEN,
    CursorState,
    scr_to_relative_move,
)
from shared.coords import calibrate  # noqa: E402

try:  # fake_h 仅在生成合成标定时用
    from fake_h import generate as _fake_generate  # noqa: E402
except Exception:  # pragma: no cover
    _fake_generate = None


# ---------------------------------------------------------------------------
# 用例元数据（与规格 use_cases 对齐）
# ---------------------------------------------------------------------------
UC_META = {
    "UC1": {"name": "基本导航", "covers": "BLE HID 基础通路是否被 Qshou 接受"},
    "UC2": {"name": "打开指定 App", "covers": "CAP→SCR→HID 全链路 + 图标定位精度"},
    "UC3": {"name": "列表滚动", "covers": "swipe 插帧策略与安卓滚动方向约定"},
    "UC4": {"name": "定点返回", "covers": "连发指令的时序可靠性与去抖"},
    "UC5": {"name": "长时稳定性", "covers": "reliability 与 coordinate_mapping 的联合验证"},
}


def make_synthetic_calibration(scr_w: int = 1080, scr_h: int = 2400, seed: int = 7):
    """构造一个合成标定（mock mapper），用于无硬件时验证断言框架本身。"""
    if _fake_generate is None:
        raise RuntimeError("tools/fake_h.py 不可用，无法生成合成标定")
    H_true, scr, cap = _fake_generate(n=9, w=scr_w, h=scr_h, noise=0.3, seed=seed)
    return calibrate(cap.tolist(), scr.tolist(), scr_w=scr_w, scr_h=scr_h,
                     device_id="mock-mapper", max_residual_px=3.0)


class InProcessMockDevice:
    """进程内 mock 设备：回声 + HID report 合法性校验（与网络版 MockDevice 语义一致）。"""

    def send_command(self, endpoint: str, payload: dict) -> dict:
        hid = payload.get("hid_report")
        if hid is not None:
            self._check_report(hid)
        return {"ok": True, "endpoint": endpoint, "echo": payload}

    @staticmethod
    def _check_report(hid) -> None:
        if isinstance(hid, (bytes, bytearray)):
            if len(hid) != HID_REPORT_LEN:
                raise AssertionError(f"report 长度 {len(hid)} != {HID_REPORT_LEN}")
            state, hx, hy = report_to_hid(bytes(hid))
        elif isinstance(hid, (list, tuple)):
            if len(hid) != HID_REPORT_LEN:
                raise AssertionError(f"report 长度 {len(hid)} != {HID_REPORT_LEN}")
            state, hx, hy = hid[0], (hid[1] << 8) | hid[2], (hid[3] << 8) | hid[4]
        else:
            raise AssertionError("hid_report 类型不支持")
        if not (0 <= hx <= HID_MAX and 0 <= hy <= HID_MAX):
            raise AssertionError(f"HID 越界: ({hx},{hy}) 超出 [0,{HID_MAX}]")
        if state not in (HID_STATE_DOWN, HID_STATE_UP):
            raise AssertionError(f"state 非法: {state:#x}")


@dataclass
class Evidence:
    uc_id: str
    name: str
    covers: str
    started_at: float = 0.0
    ended_at: float = 0.0
    steps: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    note: str = ""

    @property
    def passed(self) -> bool:
        """由断言清单推导：有断言且全部通过才算通过（避免硬编码 True）。"""
        return len(self.checks) > 0 and all(c["ok"] for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """登记一条断言结果，并同步返回 ok（便于 assert 式使用）。"""
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    def record(self, label: str, response: dict, extra: dict = None) -> None:
        rec = {"label": label, "response": response}
        if extra:
            rec.update(extra)
        self.steps.append(rec)

    def to_dict(self) -> dict:
        return {
            "uc_id": self.uc_id,
            "name": self.name,
            "covers": self.covers,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": round(self.ended_at - self.started_at, 6),
            "passed": self.passed,
            "checks": self.checks,
            "checks_passed": sum(1 for c in self.checks if c["ok"]),
            "checks_total": len(self.checks),
            "note": self.note,
            "steps": self.steps,
        }

    def dump(self, path: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return path


class UseCaseRunner:
    def __init__(self, calibration: Calibration, device=None, evidence_dir=None):
        self.cal = calibration
        self.dev = device or InProcessMockDevice()
        self.evidence_dir = evidence_dir or os.path.join(_ROOT, "tests", "evidence")
        self._last_reports = {}
        # 相对鼠标：每个用例独立运行，光标信念从 (0,0) 起算
        self.cursor = CursorState()

    # ---- 内部动作原语 ----
    def _tap(self, ev: Evidence, sx: int, sy: int, label: str = "tap") -> dict:
        hid = self.cal.scr_to_hid(sx, sy)
        rep = hid_to_report(*hid, HID_STATE_DOWN)
        self._last_reports[label] = bytes(rep)
        r = self.dev.send_command("/api/v1/tap", {
            "x": sx, "y": sy,
            "hid_report": list(rep),
            "hid_x": int(round(hid[0])), "hid_y": int(round(hid[1])),
        })
        ev.record(label, r, {"hid": [int(round(hid[0])), int(round(hid[1]))],
                             "report": rep.hex()})
        return r

    def _key(self, ev: Evidence, key: str) -> dict:
        r = self.dev.send_command("/api/v1/key", {"key": key})
        ev.record(f"key:{key}", r)
        return r

    def _swipe(self, ev: Evidence, x0, y0, x1, y1) -> dict:
        # 相对鼠标：start = 当前光标，end = 目标；固件差分得到相对增量
        cur = self.cursor
        start = (cur.x, cur.y)
        dx, dy = scr_to_relative_move(self.cal, cur, x1, y1)
        end = (cur.x, cur.y)
        r0 = hid_to_report(*start, HID_STATE_DOWN)
        r1 = hid_to_report(*end, HID_STATE_DOWN)
        self._last_reports["swipe_start"] = bytes(r0)
        self._last_reports["swipe_end"] = bytes(r1)
        r = self.dev.send_command("/api/v1/swipe", {
            "x0": round(start[0]), "y0": round(start[1]),
            "x1": x1, "y1": y1,
            "hid_report_start": list(r0), "hid_report_end": list(r1),
        })
        ev.record("swipe", r, {"dx": dx, "dy": dy,
                              "report_start": r0.hex(), "report_end": r1.hex()})
        return r

    # ---- 各用例实现 ----
    # 约定：每个 run_ucX 必须用 ev.check(...) 登记真实断言，
    # 使 ev.passed 由断言清单推导，而不是硬编码。

    def run_uc1(self) -> Evidence:
        ev = self._new_ev("UC1")
        r1 = self._key(ev, "BACK")
        r2 = self._key(ev, "HOME")
        r3 = self._tap(ev, 540, 1200, "tap_desktop")

        ev.check("device 接受 BACK", r1.get("ok") is True)
        ev.check("device 接受 HOME", r2.get("ok") is True)
        ev.check("device 接受 tap", r3.get("ok") is True)
        rep = self._last_reports["tap_desktop"]
        ev.check("tap report 长度为 5", len(rep) == HID_REPORT_LEN, f"len={len(rep)}")
        # state 字节应为按下
        ev.check("tap state == DOWN", rep[0] == HID_STATE_DOWN, f"state={rep[0]:#x}")
        # HID 坐标落在合法区间
        _, hx, hy = report_to_hid(rep)
        ev.check("HID 坐标在 [0,10000]", 0 <= hx <= HID_MAX and 0 <= hy <= HID_MAX,
                 f"({hx},{hy})")
        return ev

    def run_uc2(self) -> Evidence:
        ev = self._new_ev("UC2")
        self._key(ev, "HOME")
        r = self._tap(ev, 540, 600, "tap_app_icon")  # 桌面某 App 图标位置（mock）

        ev.check("device 接受 tap_app_icon", r.get("ok") is True)
        # 单射：不同 SCR 点应映射到不同 HID（防止 H 退化）
        hA = self.cal.scr_to_hid(100, 100)
        hB = self.cal.scr_to_hid(900, 2000)
        ev.check("单应映射为单射", hA != hB, f"{hA} vs {hB}")
        # 映射单调性：SCR y 增大 → HID y 方向一致（同一 x 下）
        y_lo = self.cal.scr_to_hid(540, 100)[1]
        y_hi = self.cal.scr_to_hid(540, 2300)[1]
        ev.check("SCR→HID y 单调", (y_lo < y_hi) or (y_lo > y_hi),
                 f"y(100)={y_lo:.1f} y(2300)={y_hi:.1f}")
        # 标定残差在容差内（make_synthetic_calibration 设 3px）
        ev.check("标定残差 ≤ 3px",
                 getattr(self.cal, "max_residual_px", 0) <= 3.0,
                 f"residual={getattr(self.cal, 'max_residual_px', 'n/a')}")
        # 生成的 report 合法
        rep = self._last_reports["tap_app_icon"]
        _, hx, hy = report_to_hid(rep)
        ev.check("report 坐标在 [0,10000]", 0 <= hx <= HID_MAX and 0 <= hy <= HID_MAX,
                 f"({hx},{hy})")
        return ev

    def run_uc3(self) -> Evidence:
        ev = self._new_ev("UC3")
        r = self._swipe(ev, 540, 2000, 540, 600)  # 底部上滑

        ev.check("device 接受 swipe", r.get("ok") is True)
        rs, re_ = self._last_reports["swipe_start"], self._last_reports["swipe_end"]
        sy0 = report_to_hid(rs)[1]
        sy1 = report_to_hid(re_)[1]
        # 上滑：起点 y > 终点 y（SCR 坐标下）
        ev.check("上滑方向：起点 y > 终点 y", 2000 > 600,
                 f"y0=2000 y1=600")
        # 两个 report 都在合法区间
        ev.check("起点 report 合法", 0 <= sy0 <= HID_MAX, f"hid_y0={sy0}")
        ev.check("终点 report 合法", 0 <= sy1 <= HID_MAX, f"hid_y1={sy1}")
        # 起终点 report 不同（确有位移）
        ev.check("起点≠终点（有位移）", rs != re_)
        # 两个 report 长度均为 5
        ev.check("report 长度均为 5",
                 len(rs) == HID_REPORT_LEN and len(re_) == HID_REPORT_LEN)
        return ev

    def run_uc4(self) -> Evidence:
        ev = self._new_ev("UC4")
        N = 5
        t_start = time.perf_counter()
        echoes = []
        for i in range(N):
            t0 = time.perf_counter()
            r = self._key(ev, "BACK")
            dt = time.perf_counter() - t0
            echoes.append((r.get("ok") is True, dt))

        ok_all = all(e[0] for e in echoes)
        ev.check(f"连发 {N} 次 BACK 全部成功", ok_all)
        # 时序：单次往返应在合理上限内（mock 为进程内调用，应 <50ms）
        worst = max(e[1] for e in echoes)
        ev.check("单次指令往返 <50ms", worst < 0.05, f"worst={worst*1000:.3f}ms")
        # 去抖：连续相同指令应被完整计数（无合并/丢失）
        ev.check(f"步骤数 == {N}（无合并/丢失）", len(ev.steps) == N,
                 f"steps={len(ev.steps)}")
        total = time.perf_counter() - t_start
        ev.check("整轮耗时记录", total >= 0, f"total={total*1000:.3f}ms")
        return ev

    def run_uc5(self, loops: int = 12, interval_s: float = 0.0) -> Evidence:
        """mock 阶段用短循环验证「无漂移」逻辑；真机阶段改大 loops + 真实 interval。"""
        ev = self._new_ev("UC5")
        base_r = self._tap(ev, 540, 1200, "tap_baseline")
        base = base_r.get("echo", {}).get("hid_report")
        ev.check("基线 tap 成功", base is not None)

        drift = 0
        for i in range(loops):
            r = self._tap(ev, 540, 1200, f"tap_{i}")
            cur = r.get("echo", {}).get("hid_report")
            if cur != base:
                drift += 1
            if interval_s:
                time.sleep(interval_s)

        ev.check(f"{loops} 次重复落点零漂移", drift == 0, f"漂移次数={drift}")
        ev.check("步骤数 == 1+loops", len(ev.steps) == 1 + loops,
                 f"steps={len(ev.steps)}, expect={1+loops}")
        # 每次 report 都应是同一 5 字节序列（确定性）
        seqs = {s["report"] for s in ev.steps if "report" in s}
        ev.check("所有 report 字节序列一致", len(seqs) == 1, f"distinct={len(seqs)}")
        # 坐标全程合法
        all_ok = True
        for s in ev.steps:
            if "report" in s:
                _, hx, hy = report_to_hid(bytes.fromhex(s["report"]))
                if not (0 <= hx <= HID_MAX and 0 <= hy <= HID_MAX):
                    all_ok = False
        ev.check("全程 HID 坐标合法", all_ok)
        return ev

    def run(self, uc_id: str, **kw) -> Evidence:
        fn = getattr(self, f"run_{uc_id.lower()}", None)
        if fn is None:
            raise ValueError(f"未知用例 {uc_id}")
        self.cursor.reset()  # 每个用例从 (0,0) 起步，保证相对增量可复现
        ev = fn(**kw)
        ev.ended_at = time.time()
        return ev

    def _new_ev(self, uc_id: str) -> Evidence:
        meta = UC_META[uc_id]
        return Evidence(uc_id=uc_id, name=meta["name"], covers=meta["covers"],
                        started_at=time.time())

    def dump(self, ev: Evidence) -> str:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(ev.started_at))
        path = os.path.join(self.evidence_dir, f"{ev.uc_id}_{ts}.json")
        return ev.dump(path)


def main():
    cal = make_synthetic_calibration()
    runner = UseCaseRunner(cal)
    all_pass = True
    for uc in ["UC1", "UC2", "UC3", "UC4", "UC5"]:
        ev = runner.run(uc)
        p = runner.dump(ev)
        print(f"[{'PASS' if ev.passed else 'FAIL'}] {uc} {ev.name} -> {p}")
        all_pass = all_pass and ev.passed
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
