"""controller/mock_device.py — 模拟 ESP32 设备（无硬件依赖，供 A6 联调）。

提供两类端点，与 shared/api_schema.yaml 对齐：
- GET  /stream        : MJPEG multipart 测试图样（移动十字 + 时间戳），作为 seam_video_source 的 mock 端
- POST /api/v1/tap|swipe|key|calibrate/* : 回显收到的指令 JSON 并记录最近一次（供集成测试断言）
- GET  /api/v1/health : 返回健康字段（mock 值）

真实部署中，这些端点由 ESP32 固件提供；此处仅用于「跑通第一」阶段的 UI 联调。
MJPEG 帧优先用 Pillow 生成真实 JPEG（可显示）；无 Pillow 时回退为内嵌的合法 1x1 JPEG。
"""

import base64
import io
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageDraw
    _HAS_PIL = True
except Exception:  # pragma: no cover - 仅在没有 Pillow 时走回退
    _HAS_PIL = False

# 合法 1x1 白底 JPEG（Pillow 不可用时的回退，保证流可被解析/浏览器显示）
_STATIC_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAA"
    "AAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)

DEFAULT_SCR_W = 1080
DEFAULT_SCR_H = 2400


def _make_jpeg_frame(w, h, t):
    """生成一帧测试 JPEG（移动十字 + 时间戳）。无 PIL 时返回静态 1x1。"""
    if not _HAS_PIL:
        return _STATIC_JPEG
    import math
    img = Image.new("RGB", (w, h), (18, 22, 27))
    d = ImageDraw.Draw(img)
    cx = int((0.5 + 0.4 * math.sin(t / 2.0)) * w)
    cy = int((0.5 + 0.4 * math.cos(t / 3.0)) * h)
    d.line([(cx - 80, cy), (cx + 80, cy)], fill=(0, 200, 255), width=5)
    d.line([(cx, cy - 80), (cx, cy + 80)], fill=(0, 200, 255), width=5)
    d.rectangle([0, 0, w - 1, h - 1], outline=(70, 70, 70), width=3)
    d.text((24, 24), "MOCK %d" % int(t * 10), fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72)
    return buf.getvalue()


class MockDeviceHandler(BaseHTTPRequestHandler):
    server_version = "MockESP32/0.1"

    def log_message(self, *a):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/stream"):
            return self._serve_stream()
        if self.path.startswith("/api/v1/health"):
            return self._send_json({
                "uptime_s": int(time.time() - self.server._start),
                "free_heap": 182000,
                "reset_reason": "power-on",
                "last_frame_ms": 33,
                "wifi_rssi": -54,
            })
        if self.path.startswith("/api/v1/sysinfo"):
            # 字段必须与 firmware/main/routes.c 的 handle_sysinfo 逐字一致
            # （shared/api_schema.yaml 是二者共同的真源；tests/test_schema.py
            #  的 TestSysinfoContract 做双向守卫）。mock 只提供确定性数值。
            return self._send_json({
                "ok": True,
                "nvs": {"valid": 1, "part_size": 0x6000,
                        "used_entries": 7, "total_entries": 180,
                        "free_entries": 173, "namespace_count": 3,
                        "used_bytes_est": 7 * 32, "cap_bytes_est": 180 * 32},
                "mem": {"heap_free": 182000, "heap_min_free": 150000,
                        "heap_largest": 90000, "internal_total": 400000,
                        "psram_total": 8 * 1024 * 1024, "psram_free": 6 * 1024 * 1024,
                        "psram_largest": 5 * 1024 * 1024},
                "storage_mounted": 0,
                "entry_bytes": 32,
                "partitions": [
                    {"label": "factory", "offset": 0x10000, "size": 0x400000,
                     "app": 1, "data": 0, "mounted": 0},
                    {"label": "nvs", "offset": 0x9000, "size": 0x6000,
                     "app": 0, "data": 1, "mounted": 0},
                ],
                "tasks": [
                    {"name": "main", "stack_free": 2816},
                    {"name": "httpd", "stack_free": 1024},
                ],
            })
        self.send_response(404)
        self.end_headers()

    def _serve_stream(self):
        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.end_headers()
        t = 0.0
        try:
            while True:
                frame = _make_jpeg_frame(self.server.scr_w, self.server.scr_h, t)
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(frame))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                t += 0.2
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        if self.path.startswith(("/api/v1/tap", "/api/v1/swipe", "/api/v1/key",
                                 "/api/v1/gesture")):
            cmd = {"path": self.path, "payload": payload}
            self.server.last_command = cmd
            self.server.commands.append(cmd)
            # 真相契约：/gesture 的回包必须带位移预算字段（原子层据此对账，绝不信 echo）
            if self.path.startswith("/api/v1/gesture") and payload.get("type") == "dir":
                dx, dy = int(payload.get("dx", 0)), int(payload.get("dy", 0))
                steps = int(payload.get("steps", 12))
                need = int(math.ceil(max(abs(dx), abs(dy)) / 127.0)) if (dx or dy) else 0
                steps_eff = max(steps, need)
                budget = steps_eff * 127
                out = {"ok": True, "dx": dx, "dy": dy,
                       "steps_eff": steps_eff,
                       "step_ms_eff": int(payload.get("step_ms", 90)),
                       "budget": budget,
                       "shortfall": 1 if budget < max(abs(dx), abs(dy)) else 0}
                return self._send_json(out)
            return self._send_json({"ok": True, "echo": payload})
        if self.path.startswith("/api/v1/calibrate/commit"):
            return self._send_json(
                {"ok": True, "max_residual_px": 0.7, "loo_max_px": 1.1}
            )
        if self.path.startswith("/api/v1/camera"):
            # 保存并回显当前相机设置（mock 不真正改传感器）
            if payload and isinstance(payload, dict):
                self.server.cam_settings.update(payload)
            cmd = {"path": self.path, "payload": payload}
            self.server.last_command = cmd
            self.server.commands.append(cmd)
            return self._send_json({"ok": True, **self.server.cam_settings})
        if self.path.startswith("/api/v1/calibrate/point"):
            return self._send_json({"ok": True})
        self.send_response(404)
        self.end_headers()


class MockDevice:
    """线程内 HTTP 服务，模拟一台已接入的 ESP32 设备。"""

    def __init__(self, host="127.0.0.1", port=0, scr_w=DEFAULT_SCR_W, scr_h=DEFAULT_SCR_H):
        self.scr_w = scr_w
        self.scr_h = scr_h
        self._httpd = ThreadingHTTPServer((host, port), MockDeviceHandler)
        self._httpd.scr_w = scr_w
        self._httpd.scr_h = scr_h
        self._httpd._start = time.time()
        self._httpd.last_command = None
        self._httpd.commands = []
        self._httpd.cam_settings = {}

    @property
    def base_url(self):
        host, port = self._httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    @property
    def stream_url(self):
        return self.base_url + "/stream"

    @property
    def last_command(self):
        return self._httpd.last_command

    @property
    def cam_settings(self):
        return dict(self._httpd.cam_settings)

    @property
    def commands(self):
        """按到达顺序记录的全部指令（原子层的顺序 / 次数断言用）。"""
        return list(self._httpd.commands)

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
