"""controller/app.py — Win10 控制器后端（MVP，零第三方依赖）。

职责（对齐 implementation_order.step_11 / shared/api_schema.yaml）：
- 托管 UI（static/index.html），注入 stream_url / scr 尺寸等配置
- 接收 UI 的指令（scr 坐标 / 按键），经 shared/coords 换算为 HID report，
  通过 RelayBackend 下发到设备（MVP 经 broker 中转；mock 模式直连 MockDevice）
- 提供 /health 聚合（透传设备 /api/v1/health）

零第三方依赖：仅标准库 + shared/coords + shared/transport。
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "shared") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "shared"))
# 本模块与 atoms.py 同目录；直接 `python controller/app.py` 或经 run_tests.py 都能导入
_CTRL_DIR = os.path.dirname(os.path.abspath(__file__))
if _CTRL_DIR not in sys.path:
    sys.path.insert(0, _CTRL_DIR)

from coords import (  # noqa: E402
    HID_STATE_DOWN,
    hid_to_report,
    Calibration,
    CursorState,
    scr_to_relative_move,
)
from atoms import ATOM_SPEEDS, AtomService  # noqa: E402
from transport import RelayBackend  # noqa: E402

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")


class ControllerHandler(BaseHTTPRequestHandler):
    server_version = "QshouController/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body_bytes, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_bytes)

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _serve_index(self):
        with open(_STATIC, "r", encoding="utf-8") as f:
            html = f.read()
        # 屏幕「信念示意」小画布：保持与真实屏幕相近的宽高比
        ratio = (self.server.scr_h / float(self.server.scr_w)) if self.server.scr_w else 2.22
        mini_w = 90
        mini_h = max(80, min(240, int(round(mini_w * ratio))))
        html = html.replace("{{STREAM_URL}}", self.server.stream_url)
        html = html.replace("{{SCR_W}}", str(self.server.scr_w))
        html = html.replace("{{SCR_H}}", str(self.server.scr_h))
        html = html.replace("{{SPEEDS}}", json.dumps(ATOM_SPEEDS))
        html = html.replace("{{DEFAULT_SPEED}}", json.dumps(
            self.server.atoms.state()["default_speed"]))
        html = html.replace("{{MINI_W}}", str(mini_w))
        html = html.replace("{{MINI_H}}", str(mini_h))
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            return self._serve_index()
        if p.path == "/config":
            return self._send_json({
                "scr_w": self.server.scr_w,
                "scr_h": self.server.scr_h,
                "stream_url": self.server.stream_url,
                "speeds": {k: dict(v) for k, v in ATOM_SPEEDS.items()},
                "default_speed": self.server.atoms.state()["default_speed"],
            })
        if p.path == "/api/v1/health":
            return self._serve_health()
        if p.path == "/api/v1/sysinfo":
            return self._relay_get("/api/v1/sysinfo")
        if p.path == "/api/v1/atom/state":
            return self._send_json(self.server.atoms.state())
        self._send(404, b"", "text/plain")

    def _serve_health(self):
        url = self.server.device_base_url.rstrip("/") + "/api/v1/health"
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.server.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._send(200, resp.read(), "application/json")
        except urllib.error.URLError as e:
            self._send_json({"error": str(getattr(e, "reason", e))}, 502)

    def do_POST(self):
        p = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        routes = {
            "/api/v1/key": lambda: self._relay("/api/v1/key", payload),
            "/api/v1/camera": lambda: self._relay("/api/v1/camera", payload),
            "/api/v1/calibrate/point": lambda: self._relay("/api/v1/calibrate/point", payload),
            "/api/v1/calibrate/commit": lambda: self._relay("/api/v1/calibrate/commit", payload),
        }
        if p.path in routes:
            return routes[p.path]()
        if p.path == "/api/v1/tap":
            return self._handle_tap(payload)
        if p.path == "/api/v1/swipe":
            return self._handle_swipe(payload)
        if p.path == "/api/v1/atom/move":
            return self._send_json(self.server.atoms.move(
                payload.get("dx", 0), payload.get("dy", 0),
                payload.get("speed"), payload.get("press", 0)))
        if p.path == "/api/v1/atom/drag":
            return self._send_json(self.server.atoms.drag(
                payload.get("dx", 0), payload.get("dy", 0), payload.get("speed")))
        if p.path == "/api/v1/atom/click":
            return self._send_json(self.server.atoms.click())
        if p.path == "/api/v1/atom/anchor":
            return self._send_json(self.server.atoms.anchor())
        self._send(404, b"", "text/plain")

    def _relay_get(self, endpoint):
        """GET 端点转发（只读查询，如 /api/v1/sysinfo）。

        与 _relay 的区别：_relay 走 transport.send_command（带 payload 的命令通道），
        本方法直连设备的 HTTP 只读路径 —— sysinfo 是纯查询，没有对应命令语义。
        设备为旧固件时端点不存在（404），原样透传状态码让前端能区分
        「设备忙」与「固件太旧」，而不是一律报 502。"""
        url = self.server.device_base_url.rstrip("/") + endpoint
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.server.token})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
            self._send(200, body, "application/json")
        except urllib.error.HTTPError as e:
            self._send_json({"error": "device %d" % e.code,
                             "detail": e.read()[:200].decode("utf-8", "replace")}, e.code)
        except urllib.error.URLError as e:
            self._send_json({"error": str(getattr(e, "reason", e))}, 502)

    def _relay(self, endpoint, payload):
        out = self.server.transport.send_command(endpoint, payload)
        self._send_json(out if isinstance(out, dict) else {"ok": True})

    def _handle_tap(self, payload):
        x = int(payload["x"])
        y = int(payload["y"])
        hid_x, hid_y = self.server.calibration.scr_to_hid(x, y)
        report = hid_to_report(hid_x, hid_y, HID_STATE_DOWN)
        out = self.server.transport.send_command("/api/v1/tap", {
            "x": x, "y": y,
            "hid_report": list(report),
            "hid_x": int(round(hid_x)),
            "hid_y": int(round(hid_y)),
        })
        if not isinstance(out, dict):
            out = {}
        out.setdefault("hid_report", list(report))
        out.setdefault("hid_x", int(round(hid_x)))
        out.setdefault("hid_y", int(round(hid_y)))
        self._send_json(out)

    def _handle_swipe(self, payload):
        # 相对鼠标模型：从「当前光标」移动到目标 SCR 点，按住拖拽。
        # start = 控制器侧光标的当前位置（虚拟 HID 坐标）；end = 目标。
        # 固件对 start/end 差分得到相对增量，从而精确落到目标点。
        x1, y1 = int(payload["x1"]), int(payload["y1"])
        cur = self.server.cursor
        start = (cur.x, cur.y)
        dx, dy = scr_to_relative_move(self.server.calibration, cur, x1, y1)
        end = (cur.x, cur.y)
        r0 = hid_to_report(*start, HID_STATE_DOWN)
        r1 = hid_to_report(*end, HID_STATE_DOWN)
        out = self.server.transport.send_command("/api/v1/swipe", {
            "x0": round(start[0]), "y0": round(start[1]),
            "x1": x1, "y1": y1,
            "hid_report_start": list(r0), "hid_report_end": list(r1),
        })
        result = {"ok": True, "dx": dx, "dy": dy,
                  "hid_report_start": list(r0), "hid_report_end": list(r1)}
        if isinstance(out, dict):
            result.update(out)
        self._send_json(result)


class ControllerApp:
    """线程内 HTTP 服务，托管控制台 UI 并转发指令到设备。"""

    def __init__(self, transport, calibration, device_base_url, token,
                 stream_url, scr_w=1080, scr_h=2400, host="127.0.0.1", port=0):
        self.transport = transport
        self.calibration = calibration
        self.device_base_url = device_base_url
        self.token = token
        self.stream_url = stream_url
        self.scr_w = scr_w
        self.scr_h = scr_h
        # 相对鼠标光标状态（控制器侧自维护，swipe 用它求相对增量）
        self.cursor = CursorState()
        # 相对推进（摇杆/方向键）原子操作层：内含 latest-wins 背压合并器
        self.atoms = AtomService(transport, self.cursor)
        self._httpd = ThreadingHTTPServer((host, port), ControllerHandler)
        for attr in ("transport", "calibration", "device_base_url", "token",
                     "stream_url", "scr_w", "scr_h", "cursor", "atoms"):
            setattr(self._httpd, attr, getattr(self, attr))
        self._thread = None

    @property
    def base_url(self):
        host, port = self._httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self.atoms.close()


def _run(device_url=None, token="demo-token", host="127.0.0.1", port=8000,
         scr_w=1080, scr_h=2400, stream_url=None):
    """启动控制台。device_url 为空则拉起内置 MockDevice（离线联调）。

    真机用法：
        python controller/app.py --device http://192.168.65.53 --port 8000
    然后在 PC 浏览器打开 http://127.0.0.1:8000
    """
    dev = None
    if not device_url:
        from controller.mock_device import MockDevice
        dev = MockDevice()
        dev.start()
        device_url = dev.base_url
        stream_url = stream_url or dev.stream_url
    stream_url = stream_url or (device_url.rstrip("/") + "/stream")

    transport = RelayBackend()
    transport.connect(device_url, token)
    cal = Calibration(H=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                      residual_offset=[0, 0], scr_w=scr_w, scr_h=scr_h,
                      device_id="demo" if dev else "qshou")
    app = ControllerApp(transport, cal, device_url, token, stream_url,
                        scr_w=scr_w, scr_h=scr_h, host=host, port=port)
    app.start()
    print("[OK] Controller : %s" % app.base_url)
    print("     Device     : %s" % device_url)
    print("     Video      : %s" % stream_url)
    print("     在浏览器打开上面的 Controller 地址即可操作（摇杆 / 方向键 / 归零）；Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.stop()
        if dev:
            dev.stop()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Qshou 控制台（相对推进交互）")
    ap.add_argument("--device", default=None,
                    help="设备基址，如 http://192.168.65.53；省略则用内置 MockDevice")
    ap.add_argument("--token", default="demo-token", help="X-Auth-Token")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--scr-w", type=int, default=1080)
    ap.add_argument("--scr-h", type=int, default=2400)
    ap.add_argument("--stream-url", default=None,
                    help="覆盖视频流地址（默认 <device>/stream）")
    a = ap.parse_args(argv)
    _run(a.device, a.token, a.host, a.port, a.scr_w, a.scr_h, a.stream_url)


if __name__ == "__main__":
    main()
