"""controller/atoms.py 单元测试（无硬件、无网络）。

重点验证三件事（都是会「静默出错」的地方）：
1. **latest-wins 背压**：摇杆高频 tick 不能堆积；被覆盖的 tick 绝不能推进光标信念。
2. **信念只按实际交付量推进**：shortfall 时按 budget 等比缩放，绝不照满额更新。
3. **阻塞操作先排空移动**：click / drag / anchor 之前必须丢待发 tick 并等飞行结束。

运行: python run_tests.py  或  python -m unittest tests.test_atoms -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "controller"))

from atoms import (  # noqa: E402
    ATOM_DEFAULT_SPEED,
    ATOM_SPEEDS,
    ANCHOR_COUNTS,
    GESTURE_ENDPOINT,
    GEST_DIR_BUDGET_MAX,
    AtomService,
    MoveCoalescer,
)
from coords import CursorState, HID_MAX  # noqa: E402


class RecordingTransport:
    """只记录调用、立刻返回的设备桩。"""

    def __init__(self, reply=None):
        self.calls = []
        self._reply = reply or (lambda ep, p: {"ok": True})

    def send_command(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        return self._reply(endpoint, payload)

    def sender(self):
        """MoveCoalescer 契约是单参 `send_fn(payload)`，这里绑定端点。"""
        return lambda payload: self.send_command(GESTURE_ENDPOINT, payload)

    def payloads(self):
        return [p for _, p in self.calls]


class BlockingTransport(RecordingTransport):
    """首次调用阻塞在事件上，用于观测「飞行中 + 待发槽」的合并行为。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_command(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        if len(self.calls) == 1:
            self.entered.set()
            self.release.wait(3.0)
        return {"ok": True}


# ---------------------------------------------------------------------------
# CursorState.nudge：相对推进的信念更新
# ---------------------------------------------------------------------------

class TestCursorNudge(unittest.TestCase):
    def test_advances_and_clamps(self):
        c = CursorState(5000, 5000)
        ax, ay = c.nudge(1200, -800)
        self.assertEqual((c.x, c.y), (6200, 4200))
        self.assertEqual((ax, ay), (1200, -800))

    def test_overshoot_edge_clamps_and_self_heals(self):
        # 越界：host 会把光标钳在边缘 → 信念同样钳位 → 二者重新对齐
        c = CursorState(300, 300)
        ax, ay = c.nudge(-5000, -5000)
        self.assertEqual((c.x, c.y), (0.0, 0.0))
        self.assertEqual((ax, ay), (-300.0, -300.0))   # 实际只被采纳了 300
        self.assertTrue(c.at_edge())

    def test_clamps_at_high_edge(self):
        c = CursorState(HID_MAX - 10, HID_MAX - 10)
        c.nudge(1000, 1000)
        self.assertEqual((c.x, c.y), (float(HID_MAX), float(HID_MAX)))
        self.assertTrue(c.at_edge())


# ---------------------------------------------------------------------------
# MoveCoalescer：latest-wins 背压
# ---------------------------------------------------------------------------

class TestMoveCoalescer(unittest.TestCase):
    def test_latest_wins_no_queue(self):
        t = BlockingTransport()
        co = MoveCoalescer(t.sender())
        try:
            co.submit({"dx": 100})
            self.assertTrue(t.entered.wait(2.0), "worker 未取走第一条")
            # 飞行中连打 3 个 tick：只有最后一个进待发槽，其余被覆盖
            co.submit({"dx": 200})
            co.submit({"dx": 300})
            co.submit({"dx": 400})
            self.assertEqual(co.state()["coalesced"], 2)
            t.release.set()
            self.assertTrue(co.drain(3.0))
            self.assertEqual(len(t.calls), 2, "绝不能排队超过「飞行中 1 + 待发 1」")
            self.assertEqual([p["dx"] for p in t.payloads()], [100, 400])
        finally:
            t.release.set()
            co.close()

    def test_barrier_drops_pending(self):
        """点击前必须先丢弃待发 tick，否则会出现「点完之后又漂移」。"""
        t = BlockingTransport()
        co = MoveCoalescer(t.sender())
        try:
            co.submit({"dx": 100})
            self.assertTrue(t.entered.wait(2.0))
            co.submit({"dx": 999})            # 待发槽里有货

            done = threading.Event()

            def worker():
                with co.barrier():
                    pass
                done.set()

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            # 仍有移动在飞 → barrier 不得放行（worker 此刻阻塞在 send，拿不到 _cv）
            self.assertFalse(done.wait(0.2))
            t.release.set()
            self.assertTrue(done.wait(3.0))
            self.assertEqual([p["dx"] for p in t.payloads()], [100],
                             "被丢弃的待发 tick 不得发出")
        finally:
            t.release.set()
            co.close()

    def test_error_does_not_kill_worker(self):
        calls = {"n": 0}

        def boom(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("device busy (503)")
            return {"ok": True}

        co = MoveCoalescer(boom)
        try:
            co.submit({"dx": 1})
            co.drain(2.0)
            co.submit({"dx": 2})
            self.assertTrue(co.drain(2.0))
            st = co.state()
            self.assertEqual(st["errors"], 1)
            self.assertEqual(st["sent"], 2)
        finally:
            co.close()


# ---------------------------------------------------------------------------
# AtomService
# ---------------------------------------------------------------------------

class TestAtomService(unittest.TestCase):
    def setUp(self):
        self.cursor = CursorState()

    def _svc(self, reply=None):
        t = RecordingTransport(reply)
        return AtomService(t, self.cursor), t

    def test_move_payload_and_cursor_advance(self):
        self.cursor.reset(5000, 5000)
        svc, t = self._svc()
        try:
            out = svc.move(200, -150, speed="fine")
            self.assertTrue(svc.drain())
            self.assertEqual(out["speed"], "fine")
            self.assertEqual(out["op"], "move")
            ep, payload = t.calls[-1]
            self.assertEqual(ep, GESTURE_ENDPOINT)
            self.assertEqual(payload["type"], "dir")
            self.assertEqual((payload["dx"], payload["dy"]), (200, -150))
            self.assertEqual(payload["press"], 0)
            self.assertEqual(payload["step_ms"], ATOM_SPEEDS["fine"]["step_ms"])
            self.assertEqual((self.cursor.x, self.cursor.y), (5200.0, 4850.0))
        finally:
            svc.close()

    def test_coalesced_tick_does_not_advance_cursor(self):
        """被覆盖掉的 tick 从未下发 → 信念绝不能按它推进（否则会永久超前）。"""
        svc, t = self._svc()
        try:
            svc.move(1000, 0)
            svc.move(1000, 0)
            svc.move(1000, 0)
            self.assertTrue(svc.drain())
            self.assertEqual(len(t.calls), 1, "高频 tick 应被合并")
            self.assertEqual(self.cursor.x, 1000.0)
        finally:
            svc.close()

    def test_clamped_to_device_budget(self):
        svc, t = self._svc()
        try:
            out = svc.move(999999, -999999)
            self.assertTrue(svc.drain())
            self.assertEqual(out["dx"], GEST_DIR_BUDGET_MAX)
            self.assertEqual(out["dy"], -GEST_DIR_BUDGET_MAX)
        finally:
            svc.close()

    def test_shortfall_scales_belief_to_delivered(self):
        """回包 shortfall=1 时必须按 budget 等比缩放，绝不照满额更新信念。"""
        def reply(ep, p):
            return {"ok": True, "dx": p["dx"], "dy": p["dy"],
                    "steps_eff": 1, "budget": 127, "shortfall": 1}

        svc, _ = self._svc(reply)
        try:
            svc.move(2540, 0)
            self.assertTrue(svc.drain())
            self.assertEqual(self.cursor.x, 127.0,
                             "只交付了 127/2540，信念也必须只走 127")
        finally:
            svc.close()

    def test_unknown_speed_falls_back_to_default(self):
        svc, t = self._svc()
        try:
            out = svc.move(10, 10, speed="turbo")
            self.assertTrue(svc.drain())
            self.assertEqual(out["speed"], ATOM_DEFAULT_SPEED)
        finally:
            svc.close()

    def test_click_uses_tap_type(self):
        svc, t = self._svc()
        try:
            out = svc.click()
            self.assertTrue(out["ok"])
            ep, payload = t.calls[-1]
            self.assertEqual(ep, GESTURE_ENDPOINT)
            self.assertEqual(payload["type"], "tap")
        finally:
            svc.close()

    def test_drag_uses_press_1(self):
        svc, t = self._svc()
        try:
            out = svc.drag(600, 0, speed="fast")
            ep, payload = t.calls[-1]
            self.assertEqual(payload["type"], "dir")
            self.assertEqual(payload["press"], 1)
            self.assertEqual(payload["step_ms"], ATOM_SPEEDS["fast"]["step_ms"])
            self.assertEqual(out["delivered_dx"], 600)
            self.assertEqual(self.cursor.x, 600.0)
        finally:
            svc.close()

    def test_anchor_pushes_negative_and_resets_belief(self):
        svc, t = self._svc()
        try:
            svc.move(4321, 2100)
            self.assertTrue(svc.drain())
            self.assertNotEqual(self.cursor.x, 0.0)
            out = svc.anchor()
            ep, payload = t.calls[-1]
            self.assertEqual(payload["type"], "dir")
            self.assertEqual(payload["dx"], ANCHOR_COUNTS)
            self.assertEqual(payload["dy"], ANCHOR_COUNTS)
            self.assertEqual(payload["press"], 0)
            self.assertEqual(out["cursor"], {"x": 0.0, "y": 0.0})
            self.assertEqual((self.cursor.x, self.cursor.y), (0.0, 0.0))
        finally:
            svc.close()

    def test_anchor_precedes_after_inflight_move_completes(self):
        """归零必须先等在飞的移动结束（顺序错了信念就归错）。"""
        t = BlockingTransport()
        svc = AtomService(t, self.cursor)
        try:
            svc.move(300, 0)
            self.assertTrue(t.entered.wait(2.0))
            done = threading.Event()

            def do_anchor():
                svc.anchor()
                done.set()

            th = threading.Thread(target=do_anchor, daemon=True)
            th.start()
            self.assertFalse(done.wait(0.15), "还有移动在飞时 anchor 不应立即返回")
            t.release.set()
            self.assertTrue(done.wait(3.0))
            self.assertEqual([p["type"] for p in t.payloads()], ["dir", "dir"])
            self.assertEqual(t.payloads()[1]["dx"], ANCHOR_COUNTS)
            self.assertEqual((self.cursor.x, self.cursor.y), (0.0, 0.0))
        finally:
            t.release.set()
            svc.close()

    def test_state_exposes_speeds_and_cursor(self):
        svc, _ = self._svc()
        try:
            st = svc.state()
            self.assertIn("fine", st["speeds"])
            self.assertEqual(st["default_speed"], ATOM_DEFAULT_SPEED)
            self.assertEqual(st["anchor_counts"], ANCHOR_COUNTS)
            self.assertEqual(st["budget_max"], GEST_DIR_BUDGET_MAX)
            self.assertIn("cursor", st)
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
