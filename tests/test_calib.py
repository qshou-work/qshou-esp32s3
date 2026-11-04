"""标定求解器测试（依赖 tools/fake_h 合成数据，无硬件依赖）。

运行: python -m unittest tests.test_calib
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from coords import calibrate, Calibration, CalibrationError  # noqa: E402
from fake_h import generate  # noqa: E402


class TestCalibration(unittest.TestCase):
    def _fit(self, seed=0, noise=0.3, n=9):
        H_true, scr, cap = generate(n=n, w=1080, h=2400, noise=noise, seed=seed)
        cal = calibrate(cap.tolist(), scr.tolist(), scr_w=1080, scr_h=2400,
                        device_id="qshou-test", max_residual_px=3.0)
        return H_true, scr, cap, cal

    def test_recovers_points_low_noise(self):
        H_true, scr, cap, cal = self._fit(seed=1, noise=0.2)
        # 拟合残差应远小于阈值
        self.assertLess(max(cal.errors), 1.0)
        # 投影不变性：用真值 H 投影出的"新"相机点，应被标定还原回原屏幕点
        import numpy as np
        rng = np.random.default_rng(99)
        for _ in range(5):
            sx, sy = rng.uniform(50, 1030), rng.uniform(100, 2300)
            p = np.linalg.inv(H_true) @ np.array([sx, sy, 1.0])
            cx, cy = p[0] / p[2], p[1] / p[2]
            rx, ry = cal.cap_to_scr(cx, cy)
            self.assertLess(abs(rx - sx), 1.5)
            self.assertLess(abs(ry - sy), 1.5)

    def test_multiple_seeds(self):
        for seed in range(5):
            _, _, _, cal = self._fit(seed=seed, noise=0.25)
            self.assertLess(max(cal.errors), 3.0, f"seed={seed} 残差超阈")

    def test_loo_diagnostic(self):
        _, _, _, cal = self._fit(seed=3, noise=0.3)
        # 留一法最大残差通常略高于拟合残差，但仍应在合理范围
        self.assertGreaterEqual(cal.loo_max_error, 0.0)
        self.assertLess(cal.loo_max_error, 5.0)

    def test_serialize_roundtrip(self):
        import tempfile, json
        _, _, _, cal = self._fit(seed=2, noise=0.2)
        path = os.path.join(tempfile.gettempdir(), "_calib_test.json")
        cal.save(path)
        cal2 = Calibration.load(path)
        import numpy as np
        self.assertTrue(np.allclose(np.asarray(cal.H), np.asarray(cal2.H)))
        self.assertEqual(cal.scr_w, cal2.scr_w)
        self.assertEqual(cal.device_id, cal2.device_id)
        os.remove(path)

    def test_too_few_points(self):
        with self.assertRaises(CalibrationError):
            calibrate([[0, 0], [1, 1], [2, 2]], [[0, 0], [1, 1], [2, 2]],
                      scr_w=1080, scr_h=2400)

    def test_degenerate_collinear(self):
        # 全部相同点（零面积）无法定义单应 -> 应抛 CalibrationError
        pts = [[5, 5], [5, 5], [5, 5], [5, 5], [5, 5]]
        with self.assertRaises(CalibrationError):
            calibrate(pts, pts, scr_w=1080, scr_h=2400)

    def test_high_noise_fails(self):
        # 极大噪声应触发残差超阈
        H_true, scr, cap = generate(n=9, w=1080, h=2400, noise=15.0, seed=4)
        with self.assertRaises(CalibrationError):
            calibrate(cap.tolist(), scr.tolist(), scr_w=1080, scr_h=2400,
                      max_residual_px=3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
