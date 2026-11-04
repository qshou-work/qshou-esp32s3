"""MVP broker: TCP 哑中继服务端（零第三方依赖）。

- 监听 TCP 端口，每个连接先做一行 JSON 握手（device_id + token + role）。
- 鉴权通过后注册 Peer；同一 device_id 的 device 与 controller 到齐即建立双向管道。
- 之后两端互发的原始字节由 RelayHub 原样转发；broker 不解析业务数据（满足 R20）。
- 一键 demo：python -m broker.server 启动中转并打印一条端到端回显证明。
"""

import json
import socket
import threading

from .relay import HELLO_TERMINATOR, parse_hello, RelayHub, AuthError, HandshakeError

OK = b"OK\n"
ERR_PREFIX = b"ERR "


class BrokerServer:
    def __init__(self, host="0.0.0.0", port=8765, hub=None, tokens=None):
        self.host = host
        self.port = port
        self.hub = hub or RelayHub(tokens=tokens)
        self._sock = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(32)
        self._thread = threading.Thread(target=self._serve, name="broker-accept", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            t.start()

    def _handle(self, conn):
        peer = None
        try:
            hello = self._read_hello(conn)
            device_id, role = self.hub.authenticate(hello)
            peer, _paired = self.hub.join(device_id, role)
            peer.bind_transport(lambda data: _safe_send(conn, data))
            conn.sendall(OK)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                peer.feed(data)
        except (AuthError, HandshakeError) as e:
            _safe_send(conn, ERR_PREFIX + str(e).encode("utf-8") + b"\n")
        finally:
            if peer is not None:
                self.hub.leave(peer)
            _safe_close(conn)

    def _read_hello(self, conn):
        buf = b""
        while HELLO_TERMINATOR not in buf and len(buf) < 4096:
            chunk = conn.recv(1024)
            if not chunk:
                raise HandshakeError("connection closed before handshake")
            buf += chunk
        return parse_hello(buf)

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)


def _safe_send(conn, data):
    try:
        conn.sendall(data)
    except OSError:
        pass


def _safe_close(conn):
    try:
        conn.close()
    except OSError:
        pass


def _demo():
    """一条命令证明 broker 端到端中转（哑中继，不解析内容）。"""
    hub = RelayHub(tokens={"demo-dev": "demo-token"})
    srv = BrokerServer(host="127.0.0.1", port=0, hub=hub)
    srv.start()
    port = srv._sock.getsockname()[1]

    def _connect(role):
        s = socket.create_connection(("127.0.0.1", port))
        hello = json.dumps(
            {"device_id": "demo-dev", "token": "demo-token", "role": role}
        ).encode() + b"\n"
        s.sendall(hello)
        assert s.recv(1024) == OK
        return s

    dev = _connect("device")
    ctrl = _connect("controller")
    # 控制端下发一段「密文」（broker 不解析）
    ctrl.sendall(b"\x00\x01ENCRYPTED-COMMAND\x02")
    assert dev.recv(1024) == b"\x00\x01ENCRYPTED-COMMAND\x02"
    # 设备回推一段视频帧字节
    dev.sendall(b"MJPEG-FRAME-BYTES")
    assert ctrl.recv(1024) == b"MJPEG-FRAME-BYTES"
    print("[OK] broker 端到端哑中转验证通过："
          "控制端->设备、设备->控制端均字节一致，broker 未解析内容")
    for s in (dev, ctrl):
        s.close()
    srv.stop()


if __name__ == "__main__":
    _demo()
