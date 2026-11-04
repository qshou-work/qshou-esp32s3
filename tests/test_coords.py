"""坐标换算单元测试（无硬件依赖）。运行: python -m unittest tests.test_coords"""

import os
import sys
import unittest

# 让测试能 import shared/coords 与 tools/fake_h
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from coords import (  # noqa: E402
    HID_MAX, HID_STATE_DOWN, HID_STATE_UP,
    Calibration, hid_to_report, report_to_hid,
    solve_homography, apply_homography, calibrate, CoordError, CalibrationError,
    CursorState, scr_to_relative_move,
)
import numpy as np  # noqa: E402


class TestReportPacking(unittest.TestCase):
    def test_origin(self):
        self.assertEqual(hid_to_report(0, 0), bytes([HID_STATE_DOWN, 0, 0, 0, 0]))

    def test_full_scale(self):
        # 10000 = 0x2710
        rep = hid_to_report(10000, 10000, HID_STATE_DOWN)
        self.assertEqual(rep, bytes([HID_STATE_DOWN, 0x27, 0x10, 0x27, 0x10]))

    def test_state_bit(self):
        self.assertEqual(hid_to_report(0, 0, HID_STATE_UP)[0], HID_STATE_UP)

    def test_clamp_low(self):
        rep = hid_to_report(-50, -50)
        self.assertEqual(rep, bytes([HID_STATE_DOWN, 0, 0, 0, 0]))

    def test_clamp_high(self):
        rep = hid_to_report(99999, 99999)
        hx = (rep[1] << 8) | rep[2]
        self.assertEqual(hx, HID_MAX)

    def test_roundtrip(self):
        for (x, y) in [(0, 0), (5000, 3000), (10000, 10000)]:
            rep = hid_to_report(x, y)
            state, bx, by = report_to_hid(rep)
            self.assertEqual((state, bx, by), (HID_STATE_DOWN, x, y))


class TestIdentityCalibration(unittest.TestCase):
    """H = 单位阵、offset = 0 时 cap == scr。"""

    def setUp(self):
        self.cal = Calibration(
            H=np.eye(3).tolist(),
            residual_offset=[0.0, 0.0],
            scr_w=1080, scr_h=2400,
        )

    def test_cap_eq_scr(self):
        sx, sy = self.cal.cap_to_scr(100, 200)
        self.assertAlmostEqual(sx, 100.0)
        self.assertAlmostEqual(sy, 200.0)

    def test_scr_to_hid_center(self):
        hid_x, hid_y = self.cal.scr_to_hid(540, 1200)
        self.assertAlmostEqual(hid_x, 540 / 1080 * HID_MAX)
        self.assertAlmostEqual(hid_y, 1200 / 2400 * HID_MAX)

    def test_cap_to_hid_corners(self):
        # 左上角 -> (0,0)
        self.assertEqual(tuple(round(v) for v in self.cal.cap_to_hid(0, 0)), (0, 0))
        # 右下角 -> (10000, 10000)
        self.assertEqual(
            tuple(round(v) for v in self.cal.cap_to_hid(1080, 2400)),
            (HID_MAX, HID_MAX),
        )


class TestHomography(unittest.TestCase):
    def test_identity(self):
        H = solve_homography([[0, 0], [1, 0], [0, 1], [1, 1]],
                              [[0, 0], [1, 0], [0, 1], [1, 1]])
        for (x, y) in [(0, 0), (3, 5), (10, 2)]:
            ox, oy = apply_homography(H, x, y)
            self.assertAlmostEqual(ox, x, places=6)
            self.assertAlmostEqual(oy, y, places=6)

    def test_known_affine(self):
        # scr = 2*cap + (10, 20)
        src = np.array([[0, 0], [1, 0], [0, 1], [2, 3], [5, 4]], dtype=float)
        dst = src * 2 + np.array([10, 20], dtype=float)
        H = solve_homography(src, dst)
        for (x, y) in src:
            ox, oy = apply_homography(H, x, y)
            self.assertAlmostEqual(ox, 2 * x + 10, places=4)
            self.assertAlmostEqual(oy, 2 * y + 20, places=4)

    def test_too_few_points(self):
        with self.assertRaises(CoordError):
            solve_homography([[0, 0], [1, 1]], [[0, 0], [1, 1]])


class TestCursorState(unittest.TestCase):
    """相对鼠标模型：控制器必须自维护光标位置，把绝对目标换算成相对增量。"""

    def test_move_to_returns_delta_and_updates(self):
        c = CursorState(1000.0, 2000.0)
        dx, dy = c.move_to(3000.0, 5000.0)
        self.assertEqual((dx, dy), (2000, 3000))
        self.assertAlmostEqual(c.x, 3000.0)
        self.assertAlmostEqual(c.y, 5000.0)

    def test_move_to_net_zero_when_same(self):
        c = CursorState(5000.0, 5000.0)
        dx, dy = c.move_to(5000.0, 5000.0)
        self.assertEqual((dx, dy), (0, 0))

    def test_clamp_on_move(self):
        c = CursorState(0.0, 0.0)
        dx, dy = c.move_to(99999.0, -500.0)
        self.assertAlmostEqual(c.x, HID_MAX)
        self.assertAlmostEqual(c.y, 0.0)
        self.assertEqual(dx, HID_MAX)

    def test_peek_does_not_update(self):
        c = CursorState(10.0, 20.0)
        dx, dy = c.peek_move_to(100.0, 200.0)
        self.assertEqual((dx, dy), (90, 180))
        self.assertAlmostEqual(c.x, 10.0)
        self.assertAlmostEqual(c.y, 20.0)

    def test_reset_recenters(self):
        c = CursorState(5000.0, 5000.0)
        c.move_to(9000.0, 9000.0)
        c.reset()
        self.assertAlmostEqual(c.x, 0.0)
        self.assertAlmostEqual(c.y, 0.0)

    def test_scr_to_relative_move_with_identity_cal(self):
        cal = Calibration(H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                          residual_offset=[0.0, 0.0], scr_w=1080, scr_h=2400)
        c = CursorState(0.0, 0.0)
        dx, dy = scr_to_relative_move(cal, c, 1080, 2400)
        self.assertEqual(dx, 10000)
        self.assertEqual(dy, 10000)
        self.assertAlmostEqual(c.x, 10000.0)
        self.assertAlmostEqual(c.y, 10000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
