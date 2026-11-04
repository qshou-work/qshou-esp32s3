"""tests/test_transport.py — A3 传输层抽象测试（零依赖，仅标准库 + numpy 可选）"""
import io
import os
import sys
import unittest

# 让测试能 import shared/transport
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from transport import (  # noqa: E402
    RelayBackend,
    TransportError,
    parse_mjpeg_frames,
)


class _FakePost:
    """记录调用并原样回显 payload，模拟 broker 的 /api/v1/* 响应。"""

    def __init__(self):
        self.calls = []

    def __call__(self, base_url, endpoint, token, payload):
        self.calls.append((base_url, endpoint, token, payload))
        return {"ok": True, "echo": payload}


class _FragStream:
    """每次只读 chunk 字节，模拟网络分片（用于验证跨 read 的帧解析）。"""

    def __init__(self, data, chunk=7):
        self._d = data
        self._i = 0
        self._c = chunk

    def read(self, n=-1):
        if self._i >= len(self._d):
            return b""
        end = min(self._i + self._c, len(self._d))
        out = self._d[self._i:end]
        self._i = end
        return out


def _synth_mjpeg():
    b = (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\nABC"
        b"\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 3\r\n\r\nDEF"
        b"\r\n--frame--\r\n"
    )
    return b


class TestRelayBackend(unittest.TestCase):
    def test_connect_guard(self):
        t = RelayBackend(http_post=_FakePost())
        with self.assertRaises(TransportError):
            t.send_command("/tap", {"x": 1, "y": 2})

    def test_send_command_routes_correctly(self):
        fake = _FakePost()
        t = RelayBackend(http_post=fake)
        t.connect("http://broker/", "tok123")
        resp = t.send_command("/tap", {"x": 10, "y": 20})
        self.assertEqual(resp["echo"], {"x": 10, "y": 20})
        self.assertEqual(len(fake.calls), 1)
        base, endpoint, token, payload = fake.calls[0]
        self.assertEqual(base, "http://broker")
        self.assertEqual(endpoint, "/tap")
        self.assertEqual(token, "tok123")
        self.assertEqual(payload, {"x": 10, "y": 20})

    def test_endpoint_prefixed(self):
        fake = _FakePost()
        t = RelayBackend(http_post=fake)
        t.connect("http://broker", "")
        t.send_command("swipe", {"x0": 0})
        self.assertEqual(fake.calls[0][1], "/swipe")

    def test_report_endpoint(self):
        t = RelayBackend(http_post=_FakePost())
        t.connect("http://broker/v1", "t")
        self.assertEqual(t.report_endpoint(), "http://broker/v1")

    def test_empty_base_url_rejected(self):
        t = RelayBackend(http_post=_FakePost())
        with self.assertRaises(TransportError):
            t.connect("", "t")


class TestMjpegParser(unittest.TestCase):
    def test_parses_two_frames(self):
        frames = []
        n = parse_mjpeg_frames(io.BytesIO(_synth_mjpeg()), frames.append)
        self.assertEqual(n, 2)
        self.assertEqual(frames, [b"ABC", b"DEF"])

    def test_parses_under_fragmentation(self):
        frames = []
        n = parse_mjpeg_frames(_FragStream(_synth_mjpeg(), chunk=7), frames.append)
        self.assertEqual(n, 2)
        self.assertEqual(frames, [b"ABC", b"DEF"])

    def test_empty_stream(self):
        frames = []
        n = parse_mjpeg_frames(io.BytesIO(b""), frames.append)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
