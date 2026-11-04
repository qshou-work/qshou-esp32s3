"""A8 框架自身的单元测试：验证断言可跑、证据可落盘、UC 语义正确。"""
import os
import sys
import json
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from assertions import (UseCaseRunner, make_synthetic_calibration,  # noqa: E402
                        InProcessMockDevice, UC_META)


class TestAssertionsFramework(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal = make_synthetic_calibration()

    def setUp(self):
        self.runner = UseCaseRunner(self.cal)

    def test_uc1_basic_nav(self):
        ev = self.runner.run("UC1")
        self.assertTrue(ev.passed)
        self.assertEqual(len(ev.steps), 3)
        self.assertGreaterEqual(len(ev.checks), 4)

    def test_uc2_open_app_and_injective(self):
        ev = self.runner.run("UC2")
        self.assertTrue(ev.passed)
        self.assertGreaterEqual(len(ev.checks), 4)

    def test_uc3_scroll(self):
        ev = self.runner.run("UC3")
        self.assertTrue(ev.passed)
        self.assertGreaterEqual(len(ev.checks), 5)

    def test_uc4_repeat_back(self):
        ev = self.runner.run("UC4")
        self.assertTrue(ev.passed)
        self.assertEqual(len(ev.steps), 5)   # N=5
        self.assertGreaterEqual(len(ev.checks), 3)

    def test_uc5_no_drift(self):
        ev = self.runner.run("UC5", loops=12)
        self.assertTrue(ev.passed)
        self.assertEqual(len(ev.steps), 13)  # baseline + 12
        self.assertGreaterEqual(len(ev.checks), 4)

    def test_passed_derived_from_checks(self):
        """passed 必须由断言清单推导，而非硬编码。"""
        ev = self.runner.run("UC1")
        self.assertTrue(all(c["ok"] for c in ev.checks))
        # 人为注入一条失败断言 → passed 必须变 False
        ev.check("人为失败", False, "for-test")
        self.assertFalse(ev.passed)

    def test_empty_checks_not_passed(self):
        """没有断言的空证据不应被判为通过。"""
        from assertions import Evidence
        ev = Evidence(uc_id="UCX", name="空", covers="n/a")
        self.assertFalse(ev.passed)

    def test_evidence_dump(self):
        ev = self.runner.run("UC1")
        path = self.runner.dump(ev)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(data["passed"])
        self.assertEqual(data["uc_id"], "UC1")
        self.assertIn("checks", data)
        self.assertEqual(data["checks_passed"], data["checks_total"])

    def test_mock_device_rejects_bad_report(self):
        dev = InProcessMockDevice()
        with self.assertRaises(AssertionError):
            # X = (0x80<<8)|0x00 = 32768，超出 [0, 10000]
            dev.send_command("/api/v1/tap", {"hid_report": [0x03, 0x80, 0x00, 0x00, 0x00]})


if __name__ == "__main__":
    unittest.main()
