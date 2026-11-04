"""controller/atoms.py — 相对推进交互的「原子操作」层（摇杆 / 方向键）

2026-10-02 决策：主交互 = **相对推进（虚拟摇杆 + 方向键）**，不是「点画面」。

为什么这样最稳
--------------
相对操作与**相对鼠标**的物理模型天然同构 —— 下发的是相对 HID count 增量，
因此**不需要** 相机↔屏幕单应 H、**不需要** counts↔px 增益标定、也**不需要**
CursorState 的绝对信念。零假设，立即可用。

绝对定位怎么办
--------------
只靠**撞角归零**（`anchor()`）：向负方向猛推到底（−25000 counts），host 会把
光标钳在左上角；此时把控制器侧 CursorState 复位到 (0,0)，信念与真实位置重新
对齐。这是**唯一不依赖视觉的绝对定位手段**。
补充：`CursorState.nudge()` 在越界时同样钳位，所以任何一次「推过头」都会自愈。

背压（本模块存在的主要理由）
----------------------------
摇杆按住会持续产生 tick，而固件侧手势是**串行化**的（播放中再触发返回 503）。
若在控制器侧排队，会出现典型的「松手后光标还在动」。故采用 **latest-wins 合并**：
飞行中最多 1 条 + 待发槽最多 1 条，新 tick 覆盖旧 tick，**永不堆积**。

click / drag / anchor 是**阻塞**操作，且先排空移动（`barrier()`），否则会出现
「先点击、随后又漂移」的错序。

不改固件
--------
全部复用现有 `POST /api/v1/gesture`（`type=dir` / `type=tap`），不新增固件路由。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Optional, Tuple

GESTURE_ENDPOINT = "/api/v1/gesture"

# 固件侧位移预算常量（与 firmware/main/routes.c 对齐）
GEST_DIR_PER_FRAME_MAX = 127        # 单帧 X/Y 为 8-bit 有符号
GEST_DIR_MAX_FRAMES = 200           # 固件 pts[256] 上限
GEST_DIR_BUDGET_MAX = GEST_DIR_PER_FRAME_MAX * GEST_DIR_MAX_FRAMES   # 25400

# 速度档位：step_ms 越小 → 单位 count 的屏幕位移越大（指针加速度）
#   ⚠️ measured=False 表示该档位尚未做真机增益标定，数值为占位/插值。
ATOM_SPEEDS: Dict[str, dict] = {
    "fine":   {"step_ms": 90, "tick": 200, "measured": True,
               "note": "实测增益 0.27843 cap_px/count（离散 1.4%），最可控"},
    "normal": {"step_ms": 60, "tick": 450, "measured": False,
               "note": "未实测，插值占位"},
    "fast":   {"step_ms": 15, "tick": 900, "measured": True,
               "note": "实测增益 0.42249 cap_px/count（离散 12.2%），快但粗"},
}
ATOM_DEFAULT_SPEED = "fine"

# 撞角归零：向负方向推到底，host 会把光标钳在左上角
ANCHOR_COUNTS = -25000
ANCHOR_STEP_MS = 15
ANCHOR_STEPS_FLOOR = 16             # 固件会按预算展开到 ceil(25000/127)=197 帧


class MoveCoalescer:
    """latest-wins 背压合并器：把高频 tick 压成「飞行中 1 条 + 待发 1 条」。

    - `send_fn` 契约：`Callable[[payload_dict], dict]` —— **单参**，端点已由调用方绑定。
    - `submit()` 非阻塞：写入待发槽（覆盖旧的），唤醒 worker。
    - worker 串行发送，保证设备侧任意时刻只有一条手势在飞。
    - `barrier()` 原子地立起门闩 `_hold`、丢弃待发、等飞行结束，再独占设备
      （click / drag / anchor 用）。

    🔴 门闩必须是**原子**的：若只清空待发槽再等飞行结束，worker 可能在「飞行
    结束」与「调用方拿到独占」之间的缝隙里抢先取走待发 tick —— 那就会出现
    「点了之后又漂移」。故 `_hold` 与 `_busy` 的检查全在同一把 `_cv` 下完成。

    于是「摇杆松手 → 最多一条待发被丢弃，一条在飞」→ 松手即停。
    """

    def __init__(self, send_fn: Callable[[dict], dict]):
        self._send = send_fn
        self._cv = threading.Condition()
        self._pending: Optional[dict] = None
        self._busy = False
        self._hold = False          # 独占门闩：置位时 worker 不得取待发
        self._closed = False
        self._sent = 0
        self._errors = 0
        self._coalesced = 0
        self._last: Optional[dict] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="atom-coalescer")
        self._thread.start()

    # ---- 提交侧 ----
    def submit(self, payload: dict) -> bool:
        """写入待发槽。返回 True = 已入槽；False = 覆盖掉了尚未发出的旧 tick。"""
        with self._cv:
            if self._closed:
                return False
            replaced = self._pending is not None
            if replaced:
                self._coalesced += 1
            self._pending = payload
            self._cv.notify_all()
            return not replaced

    def drop_pending(self) -> bool:
        """丢弃尚未发出的 tick（阻塞操作前置：避免「先点击、随后又漂移」）。"""
        with self._cv:
            had = self._pending is not None
            self._pending = None
            return had

    @contextmanager
    def barrier(self):
        """独占设备：原子地丢弃待发 + 等飞行结束，之后到释放前 worker 不再发送。"""
        with self._cv:
            self._hold = True
            self._pending = None
            while self._busy and not self._closed:
                self._cv.wait(0.05)
        try:
            yield
        finally:
            with self._cv:
                self._hold = False
                self._cv.notify_all()

    # ---- worker ----
    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._closed and (self._pending is None or self._hold):
                    self._cv.wait()
                if self._closed:
                    return
                payload = self._pending
                self._pending = None
                self._busy = True
            out: dict
            ok = True
            try:
                out = self._send(payload)
            except Exception as e:  # noqa: BLE001 - 记录并继续，绝不因单次失败卡死队列
                out = {"ok": False, "error": str(e)}
                ok = False
            with self._cv:
                self._busy = False
                self._last = out
                self._sent += 1
                if not ok:
                    self._errors += 1
                self._cv.notify_all()

    # ---- 观测 / 生命周期 ----
    def state(self) -> dict:
        with self._cv:
            return {
                "busy": self._busy,
                "pending": self._pending is not None,
                "held": self._hold,
                "sent": self._sent,
                "errors": self._errors,
                "coalesced": self._coalesced,
                "last": self._last,
            }

    def drain(self, timeout: float = 3.0) -> bool:
        """等待系统空闲（待发排空 + 无飞行 + 无独占）。测试 / 收尾用。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cv:
                if self._pending is None and not self._busy and not self._hold:
                    return True
            time.sleep(0.005)
        return False

    def close(self, timeout: float = 3.0) -> None:
        self.drain(timeout)
        with self._cv:
            self._closed = True
            self._pending = None
            self._cv.notify_all()
        self._thread.join(timeout=timeout)


class AtomService:
    """原子操作门面：move / drag / click / anchor / state。

    只依赖 `transport.send_command` 与 `cursor`（`coords.CursorState`）。
    """

    def __init__(self, transport, cursor):
        self.transport = transport
        self.cursor = cursor
        self._co = MoveCoalescer(self._send_move)

    # ---- 内部 ----
    @staticmethod
    def clamp_delta(dx: float, dy: float) -> Tuple[int, int]:
        def one(v) -> int:
            iv = int(round(float(v)))
            return max(-GEST_DIR_BUDGET_MAX, min(GEST_DIR_BUDGET_MAX, iv))
        return one(dx), one(dy)

    def speed_of(self, name: Optional[str]) -> Tuple[str, dict]:
        key = name if name in ATOM_SPEEDS else ATOM_DEFAULT_SPEED
        return key, ATOM_SPEEDS[key]

    def _send_move(self, payload: dict) -> dict:
        """worker 回调：真正下发，并按**实际交付量**推进信念（绝不照满额更新）。"""
        out = self.transport.send_command(GESTURE_ENDPOINT, payload)
        if isinstance(out, dict) and out.get("ok") is False:
            return out
        ddx, ddy = self._delivered(payload, out)
        self.cursor.nudge(ddx, ddy)
        return out

    @staticmethod
    def _delivered(payload: dict, out) -> Tuple[int, int]:
        """从固件回包推算**实际交付**的净位移。

        正常情况下 = 请求值；若 `shortfall=1`（预算不足），按 budget 等比缩放
        —— 这正是「回包 echo 请求值会掩盖少走」那条教训的落地对策。
        """
        dx, dy = int(payload["dx"]), int(payload["dy"])
        if isinstance(out, dict) and int(out.get("shortfall", 0) or 0):
            budget = int(out.get("budget", 0) or 0)
            m = max(abs(dx), abs(dy))
            if m > 0 and 0 < budget < m:
                k = budget / float(m)
                return int(round(dx * k)), int(round(dy * k))
        return dx, dy

    # ---- 原子操作 ----
    def move(self, dx: float, dy: float, speed: Optional[str] = None,
             press: int = 0) -> dict:
        """提交一次相对移动（非阻塞、latest-wins）。"""
        dx, dy = self.clamp_delta(dx, dy)
        key, tier = self.speed_of(speed)
        payload = {"type": "dir", "dx": dx, "dy": dy, "steps": 4,
                   "step_ms": tier["step_ms"], "press": int(press)}
        queued = self._co.submit(payload)
        st = self._co.state()
        return {"ok": True, "op": "move", "queued": queued,
                "coalesced": bool(queued is False),
                "dx": dx, "dy": dy, "speed": key,
                "step_ms": tier["step_ms"], "press": int(press),
                "in_flight": st["busy"], "pending": st["pending"]}

    def drag(self, dx: float, dy: float, speed: Optional[str] = None) -> dict:
        """一次**完整**拖拽（down → move → up，原子手势），阻塞。

        注意：不做「连续拖拽」——那需要固件提供 hold 端点（按下与抬起分离），
        当前固件没有，硬做会让安卓在一次 tick 结束时判定拖拽中断。
        故摇杆的拖拽模式采用「按住累加、松手一次性提交」。
        """
        dx, dy = self.clamp_delta(dx, dy)
        key, tier = self.speed_of(speed)
        payload = {"type": "dir", "dx": dx, "dy": dy, "steps": 4,
                   "step_ms": tier["step_ms"], "press": 1}
        with self._co.barrier():
            out = self.transport.send_command(GESTURE_ENDPOINT, payload)
        ddx, ddy = self._delivered(payload, out)
        self.cursor.nudge(ddx, ddy)
        return {"ok": True, "op": "drag", "dx": dx, "dy": dy,
                "delivered_dx": ddx, "delivered_dy": ddy, "speed": key,
                "step_ms": tier["step_ms"],
                "shortfall": int((out or {}).get("shortfall", 0) or 0)
                if isinstance(out, dict) else 0,
                "device": out}

    def click(self) -> dict:
        """原地点击（净零抖动锚定的 tap），阻塞。"""
        with self._co.barrier():
            out = self.transport.send_command(GESTURE_ENDPOINT,
                                              {"type": "tap", "step_ms": 80})
        return {"ok": True, "op": "click", "device": out}

    def anchor(self) -> dict:
        """撞角归零：向负方向推到底 → 信念复位 (0,0)。

        这是唯一不依赖视觉的绝对定位手段，代价是耗时（197 帧 × 15ms ≈ 3s）。
        """
        payload = {"type": "dir", "dx": ANCHOR_COUNTS, "dy": ANCHOR_COUNTS,
                   "steps": ANCHOR_STEPS_FLOOR, "step_ms": ANCHOR_STEP_MS,
                   "press": 0}
        with self._co.barrier():
            out = self.transport.send_command(GESTURE_ENDPOINT, payload)
        self.cursor.reset(0.0, 0.0)
        return {"ok": True, "op": "anchor", "counts": ANCHOR_COUNTS,
                "cursor": {"x": self.cursor.x, "y": self.cursor.y},
                "device": out}

    # ---- 观测 / 生命周期 ----
    def drain(self, timeout: float = 3.0) -> bool:
        """等待所有已提交的移动发送完毕（收尾 / 测试用）。"""
        return self._co.drain(timeout)

    def state(self) -> dict:
        st = self._co.state()
        st["cursor"] = {"x": self.cursor.x, "y": self.cursor.y}
        st["speeds"] = {k: dict(v) for k, v in ATOM_SPEEDS.items()}
        st["default_speed"] = ATOM_DEFAULT_SPEED
        st["anchor_counts"] = ANCHOR_COUNTS
        st["budget_max"] = GEST_DIR_BUDGET_MAX
        return st

    def close(self, timeout: float = 3.0) -> None:
        self._co.close(timeout)
