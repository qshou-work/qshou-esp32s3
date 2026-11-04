"""broker 哑中继单元测试（无硬件依赖）。运行: python -m unittest tests.test_broker"""

import json
import os
import socket
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from broker.relay import (  # noqa: E402
    RelayHub, AuthError, HandshakeError, parse_hello, _const_time_eq,
)
from broker.server import BrokerServer, OK  # noqa: E402


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


class TestHandshake(unittest.TestCase):
    def test_parse_hello_ok(self):
        raw = json.dumps({"device_id": "d1", "token": "t", "role": "device"}).encode() + b"\n"
        h = parse_hello(raw)
        self.assertEqual(h["role"], "device")

    def test_parse_hello_missing_newline(self):
        with self.assertRaises(HandshakeError):
            parse_hello(b'{"device_id":"d1"}')

    def test_parse_hello_bad_json(self):
        with self.assertRaises(HandshakeError):
            parse_hello(b"{not json}\n")

    def test_const_time_eq(self):
        self.assertTrue(_const_time_eq("abc", "abc"))
        self.assertFalse(_const_time_eq("abc", "abd"))
        self.assertFalse(_const_time_eq("abc", "abcd"))


class TestRelayHub(unittest.TestCase):
    def setUp(self):
        self.hub = RelayHub(tokens={"dev1": "tokA"})
        self.d_peer, _ = self.hub.join(*self.hub.authenticate(
            {"device_id": "dev1", "token": "tokA", "role": "device"}))
        self.c_peer, paired = self.hub.join(*self.hub.authenticate(
            {"device_id": "dev1", "token": "tokA", "role": "controller"}))
        self.assertTrue(paired)
        self.d_t = _FakeTransport()
        self.c_t = _FakeTransport()
        self.d_peer.bind_transport(self.d_t.send)
        self.c_peer.bind_transport(self.c_t.send)

    def test_controller_to_device(self):
        self.c_peer.feed(b"cmd-from-ctrl")
        self.assertEqual(self.d_t.sent, [b"cmd-from-ctrl"])

    def test_device_to_controller(self):
        self.d_peer.feed(b"frame-from-device")
        self.assertEqual(self.c_t.sent, [b"frame-from-device"])

    def test_broker_does_not_parse(self):
        # 任意二进制（含 0x00 与不可打印）都原样转发，且不回环到发送方
        blob = bytes(range(256))
        self.c_peer.feed(blob)
        self.assertEqual(self.c_t.sent, [])
        self.assertEqual(self.d_t.sent, [blob])

    def test_unknown_device_rejected(self):
        with self.assertRaises(AuthError):
            self.hub.authenticate({"device_id": "nope", "token": "x", "role": "device"})

    def test_wrong_token_rejected(self):
        with self.assertRaises(AuthError):
            self.hub.authenticate({"device_id": "dev1", "token": "WRONG", "role": "device"})

    def test_bad_role_rejected(self):
        with self.assertRaises(AuthError):
            self.hub.authenticate({"device_id": "dev1", "token": "tokA", "role": "guest"})

    def test_duplicate_role_rejected(self):
        with self.assertRaises(AuthError):
            self.hub.join(*self.hub.authenticate(
                {"device_id": "dev1", "token": "tokA", "role": "device"}))

    def test_leave_cleans_up(self):
        self.hub.leave(self.d_peer)
        # 控制端已离开，新 device 接入不应配对
        p, paired = self.hub.join(*self.hub.authenticate(
            {"device_id": "dev1", "token": "tokA", "role": "device"}))
        self.assertFalse(paired)


class TestBrokerServer(unittest.TestCase):
    def _connect(self, port, device_id, token, role):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        hello = json.dumps(
            {"device_id": device_id, "token": token, "role": role}
        ).encode() + b"\n"
        s.sendall(hello)
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(1024)
        return s, buf.decode()

    def setUp(self):
        self.hub = RelayHub(tokens={"dev1": "tokA"})
        self.srv = BrokerServer(host="127.0.0.1", port=0, hub=self.hub)
        self.srv.start()
        self.port = self.srv._sock.getsockname()[1]

    def tearDown(self):
        self.srv.stop()

    def test_end_to_end_relay(self):
        ds, dresp = self._connect(self.port, "dev1", "tokA", "device")
        self.assertEqual(dresp, "OK\n")
        cs, cresp = self._connect(self.port, "dev1", "tokA", "controller")
        self.assertEqual(cresp, "OK\n")
        cs.sendall(b"ping")
        self.assertEqual(ds.recv(1024), b"ping")
        ds.sendall(b"pong")
        self.assertEqual(cs.recv(1024), b"pong")
        ds.close()
        cs.close()

    def test_wrong_token_rejected(self):
        s, resp = self._connect(self.port, "dev1", "WRONG", "device")
        self.assertTrue(resp.startswith("ERR "))
        s.close()

    def test_unknown_device_rejected(self):
        s, resp = self._connect(self.port, "ghost", "x", "device")
        self.assertTrue(resp.startswith("ERR "))
        s.close()


if __name__ == "__main__":
    unittest.main()
