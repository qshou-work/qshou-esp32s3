"""controller 单元测试（无硬件依赖）。运行: python -m unittest tests.test_controller"""

import base64
import io
import json
import os
import sys
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "shared"))

from controller.app import ControllerApp  # noqa: E402
from controller.mock_device import MockDevice  # noqa: E402
from transport import RelayBackend, parse_mjpeg_frames  # noqa: E402
from coords import Calibration, hid_to_report, HID_STATE_DOWN  # noqa: E402


def _post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.read()


class TestController(unittest.TestCase):
    def setUp(self):
        self.dev = MockDevice()
        self.dev.start()
        # 合成标定：CAP==SCR 的恒等映射，便于断言
        self.cal = Calibration(H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                              residual_offset=[0, 0], scr_w=1080, scr_h=2400)
        self.transport = RelayBackend()
        self.transport.connect(self.dev.base_url, "demo-token")
        self.app = ControllerApp(self.transport, self.cal, self.dev.base_url,
                                "demo-token", self.dev.stream_url)
        self.app.start()
        self.base = self.app.base_url

    def tearDown(self):
        self.app.stop()
        self.dev.stop()

    def test_tap_resolves_hid_report(self):
        # scr (540,1200) -> hid (5000,5000) -> report [3,19,136,19,136]
        out = _post(self.base, "/api/v1/tap", {"x": 540, "y": 1200})
        self.assertEqual(out["hid_report"], [3, 19, 136, 19, 136])
        self.assertEqual(out["hid_x"], 5000)
        self.assertEqual(out["hid_y"], 5000)
        # 透传到达 mock 设备
        self.assertIsNotNone(self.dev.last_command)
        self.assertEqual(self.dev.last_command["path"], "/api/v1/tap")
        self.assertEqual(self.dev.last_command["payload"]["x"], 540)

    def test_swipe_sends_both_reports(self):
        out = _post(self.base, "/api/v1/swipe",
                    {"x0": 0, "y0": 0, "x1": 1080, "y1": 2400, "duration_ms": 200})
        self.assertIn("hid_report_start", out)
        self.assertIn("hid_report_end", out)
        self.assertEqual(self.dev.last_command["path"], "/api/v1/swipe")

    def test_key_relayed(self):
        out = _post(self.base, "/api/v1/key", {"key": "HOME"})
        self.assertTrue(out.get("ok"))
        self.assertEqual(self.dev.last_command["path"], "/api/v1/key")

    def test_camera_proxy_forwards_to_device(self):
        # 控制器把相机传感器参数代理到设备（真实改 OV2640）；mock 回显存储值。
        out = _post(self.base, "/api/v1/camera",
                    {"exposure_auto": 0, "exposure_value": 600, "contrast": 1, "saturation": -1})
        self.assertTrue(out.get("ok"))
        self.assertEqual(self.dev.last_command["path"], "/api/v1/camera")
        self.assertEqual(self.dev.cam_settings.get("exposure_value"), 600)
        self.assertEqual(self.dev.cam_settings.get("contrast"), 1)

    def test_config_exposes_dims(self):
        body = _get(self.base, "/config").decode()
        cfg = json.loads(body)
        self.assertEqual(cfg["scr_w"], 1080)
        self.assertEqual(cfg["scr_h"], 2400)
        self.assertTrue(cfg["stream_url"].endswith("/stream"))

    def test_index_served_with_placeholders_replaced(self):
        html = _get(self.base, "/").decode()
        self.assertIn('id="screen"', html)
        self.assertNotIn("{{STREAM_URL}}", html)
        self.assertNotIn("{{SCR_W}}", html)

    def test_health_passthrough(self):
        body = _get(self.base, "/api/v1/health").decode()
        h = json.loads(body)
        self.assertIn("uptime_s", h)
        self.assertIn("wifi_rssi", h)

    def test_sysinfo_relay_exposes_resource_fields(self):
        """控制器 /api/v1/sysinfo 必须把设备的资源水位原样转出去。

        结构与 firmware/main/routes.c 的 handle_sysinfo 一致（真源
        shared/api_schema.yaml）。注意 tasks 只有 stack_free —— 固件未开
        configUSE_TRACE_FACILITY，拿不到栈容量，字段一旦「加回来」就是谎报。
        """
        body = _get(self.base, "/api/v1/sysinfo").decode()
        j = json.loads(body)
        self.assertTrue(j.get("ok"))
        self.assertEqual(j.get("storage_mounted"), 0)
        self.assertEqual(
            set(j["nvs"].keys()),
            {"valid", "part_size", "used_entries", "total_entries",
             "free_entries", "namespace_count", "used_bytes_est",
             "cap_bytes_est"})
        self.assertIn("heap_min_free", j["mem"])
        self.assertIn("psram_total", j["mem"])
        for p in j["partitions"]:
            self.assertEqual(set(p.keys()),
                             {"label", "offset", "size", "app", "data", "mounted"})
        for t in j["tasks"]:
            self.assertEqual(set(t.keys()), {"name", "stack_free"})
            self.assertGreater(t["stack_free"], 0)

    def test_calibrate_commit(self):
        out = _post(self.base, "/api/v1/calibrate/commit", {"device_id": "demo"})
        self.assertTrue(out.get("ok"))

    def test_stream_is_valid_multipart(self):
        data = b""
        deadline = time.time() + 1.5
        try:
            with urllib.request.urlopen(self.dev.stream_url, timeout=2) as r:
                while time.time() < deadline and len(data) < 500000:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    data += chunk
        except Exception:
            pass
        self.assertIn(b"--frame", data)
        frames = []
        parse_mjpeg_frames(io.BytesIO(data), lambda f: frames.append(f))
        self.assertGreaterEqual(len(frames), 1)

    def test_static_jpeg_constant_is_valid(self):
        # 回退用的内嵌 JPEG 必须是合法 base64（Pillow 存在时此测试验证了常量仍可用）
        raw = base64.b64decode(
            "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
            "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAA"
            "AAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
        )
        self.assertTrue(len(raw) > 0)


    def test_swipe_uses_relative_delta_from_cursor(self):
        # 光标初值 (0,0)；identity 标定下 swipe 到 (1080,2400) -> dx=dy=10000
        out = _post(self.base, "/api/v1/swipe",
                    {"x0": 0, "y0": 0, "x1": 1080, "y1": 2400})
        self.assertEqual(out["dx"], 10000)
        self.assertEqual(out["dy"], 10000)
        # 光标已更新到目标（控制器侧状态随之前进）
        self.assertAlmostEqual(self.app.cursor.x, 10000.0)
        self.assertAlmostEqual(self.app.cursor.y, 10000.0)
        # 再 swipe 回 (0,0) -> 相对上一光标，dx=dy=-10000
        out2 = _post(self.base, "/api/v1/swipe",
                     {"x0": 1080, "y0": 2400, "x1": 0, "y1": 0})
        self.assertEqual(out2["dx"], -10000)
        self.assertEqual(out2["dy"], -10000)

    def test_swipe_start_is_current_cursor(self):
        # 第一次 swipe 后光标在 (10000,10000)，第二次 swipe 起点应等于此（非请求里的 (x0,y0)）
        _post(self.base, "/api/v1/swipe",
              {"x0": 0, "y0": 0, "x1": 1080, "y1": 2400})
        out = _post(self.base, "/api/v1/swipe",
                    {"x0": 999, "y0": 999, "x1": 540, "y1": 1200})
        # 起点应为上一光标 (10000,10000)，而非请求里的 (999,999)
        self.assertEqual(out["hid_report_start"], [3, 0x27, 0x10, 0x27, 0x10])
        # 终点为 (540,1200) -> hid (5000,5000)
        self.assertEqual(out["hid_report_end"], [3, 0x13, 0x88, 0x13, 0x88])


class TestAtomEndpoints(unittest.TestCase):
    """相对推进（摇杆/方向键）原子端点的端到端 HTTP 行为。"""

    def setUp(self):
        self.dev = MockDevice()
        self.dev.start()
        self.cal = Calibration(H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                              residual_offset=[0, 0], scr_w=1080, scr_h=2400)
        self.transport = RelayBackend()
        self.transport.connect(self.dev.base_url, "demo-token")
        self.app = ControllerApp(self.transport, self.cal, self.dev.base_url,
                                "demo-token", self.dev.stream_url)
        self.app.start()
        self.base = self.app.base_url
        # 原子移动是异步（合并器 worker 发送），每次断言前先排空
        self.drain = lambda: self.assertTrue(self.app.atoms.drain(3.0))

    def tearDown(self):
        self.app.stop()
        self.dev.stop()

    def test_move_reaches_device_as_dir_press0(self):
        out = _post(self.base, "/api/v1/atom/move", {"dx": 200, "dy": -150, "speed": "fine"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["op"], "move")
        self.assertEqual((out["dx"], out["dy"]), (200, -150))
        self.drain()
        cmd = self.dev.commands[-1]
        self.assertEqual(cmd["path"], "/api/v1/gesture")
        self.assertEqual(cmd["payload"]["type"], "dir")
        self.assertEqual(cmd["payload"]["press"], 0)
        self.assertEqual((cmd["payload"]["dx"], cmd["payload"]["dy"]), (200, -150))

    def test_move_advances_cursor_after_send(self):
        _post(self.base, "/api/v1/atom/move", {"dx": 1000, "dy": 0})
        self.drain()
        st = json.loads(_get(self.base, "/api/v1/atom/state").decode())
        self.assertEqual(st["cursor"]["x"], 1000.0)
        self.assertGreaterEqual(st["sent"], 1)

    def test_click_reaches_device_as_tap(self):
        out = _post(self.base, "/api/v1/atom/click", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["op"], "click")
        self.assertEqual(self.dev.commands[-1]["payload"]["type"], "tap")

    def test_drag_reaches_device_as_press1(self):
        out = _post(self.base, "/api/v1/atom/drag", {"dx": 600, "dy": 0, "speed": "fast"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["delivered_dx"], 600)
        cmd = self.dev.commands[-1]
        self.assertEqual(cmd["payload"]["type"], "dir")
        self.assertEqual(cmd["payload"]["press"], 1)
        self.assertEqual(cmd["payload"]["step_ms"], 15)

    def test_anchor_pushes_negative_and_zeroes_belief(self):
        _post(self.base, "/api/v1/atom/move", {"dx": 4000, "dy": 1200})
        self.drain()
        out = _post(self.base, "/api/v1/atom/anchor", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["counts"], -25000)
        self.assertEqual(out["cursor"], {"x": 0.0, "y": 0.0})
        cmd = self.dev.commands[-1]
        self.assertEqual(cmd["payload"]["type"], "dir")
        self.assertEqual(cmd["payload"]["dx"], -25000)
        self.assertEqual(cmd["payload"]["press"], 0)

    def test_state_exposes_speeds_and_budget(self):
        st = json.loads(_get(self.base, "/api/v1/atom/state").decode())
        self.assertIn("fine", st["speeds"])
        self.assertIn("fast", st["speeds"])
        self.assertEqual(st["budget_max"], 25400)
        self.assertIn("cursor", st)

    def test_config_exposes_speeds(self):
        cfg = json.loads(_get(self.base, "/config").decode())
        self.assertIn("speeds", cfg)
        self.assertIn("default_speed", cfg)

    def test_index_replaces_atom_placeholders(self):
        html = _get(self.base, "/").decode()
        for ph in ("{{SPEEDS}}", "{{DEFAULT_SPEED}}", "{{MINI_W}}", "{{MINI_H}}"):
            self.assertNotIn(ph, html)
        self.assertIn('id="stick"', html)
        self.assertIn('id="dpad"', html)
        self.assertIn("atom/move", html)

    def test_move_is_clamped_to_device_budget(self):
        out = _post(self.base, "/api/v1/atom/move", {"dx": 999999, "dy": 0})
        self.assertEqual(out["dx"], 25400)
        self.drain()


if __name__ == "__main__":
    unittest.main()
