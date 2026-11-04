"""MVP broker: 哑中继（dumb relay）核心逻辑。

设计目标（对齐 execution_policy.mvp_transport_decision 与 guardrails）:
- 仅做「设备注册 + 每设备 token 鉴权 + 双向字节透传」，**绝不解析业务数据**。
- 满足 R20（E2E 密文）：broker 拿到的永远是密文，无法解读屏幕内容。
- 满足 guardrail「每设备唯一凭据」：device_id -> token 在 broker 侧预置（每设备独立密钥）。
- 零第三方依赖，仅标准库（socket / threading / json）。

配对模型:
- 设备(device)与控制端(controller)各自携带 device_id + token 连接 broker。
- 同一 device_id 的两个角色都到齐后，两者建立双向字节管道。
- 此后任意一方发来的原始字节都原样转发给另一方；broker 不解析、不缓存业务语义。
"""

import json
import threading
from collections import defaultdict

HELLO_TERMINATOR = b"\n"  # 首行 JSON 握手以换行结束


class AuthError(Exception):
    """鉴权失败（device_id 未知 / token 不符 / 角色非法 / 重复连接）。"""


class HandshakeError(Exception):
    """握手帧格式错误。"""


class Peer:
    """一条已鉴权的连接。send() 交字节给底层传输；feed() 由传输层在收到对端数据时调用。"""

    def __init__(self, device_id, role):
        self.device_id = device_id
        self.role = role  # "device" | "controller"
        self._transport_send = None  # callable(bytes)
        self._peer = None
        self.closed = False

    def bind_transport(self, send_fn):
        self._transport_send = send_fn

    def link(self, other):
        self._peer = other
        other._peer = self

    def send(self, data: bytes):
        if self._transport_send is not None and not self.closed:
            self._transport_send(data)

    def feed(self, data: bytes):
        """收到本端原始字节 -> 原样转发给配对方（不解析、不缓冲业务语义）。"""
        if self._peer is not None and not self.closed:
            self._peer.send(data)

    def close(self):
        self.closed = True
        if self._peer is not None:
            self._peer.closed = True
            self._peer = None


class RelayHub:
    """纯逻辑：管理 device_id -> {role: Peer} 的配对与双向透传。"""

    def __init__(self, tokens=None):
        self._tokens = dict(tokens or {})
        self._lock = threading.RLock()
        self._by_device = defaultdict(dict)  # device_id -> {role: Peer}

    # ---- token 管理（guardrail: 每设备唯一凭据）----
    def add_token(self, device_id, token):
        with self._lock:
            self._tokens[device_id] = token

    @classmethod
    def load_tokens(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("tokens file must be a JSON object {device_id: token}")
        return cls(tokens=data)

    # ---- 鉴权 ----
    def authenticate(self, hello):
        if not isinstance(hello, dict):
            raise AuthError("hello must be a JSON object")
        device_id = hello.get("device_id")
        token = hello.get("token")
        role = hello.get("role")
        if role not in ("device", "controller"):
            raise AuthError("role must be 'device' or 'controller'")
        if not device_id or not token:
            raise AuthError("device_id and token required")
        with self._lock:
            expected = self._tokens.get(device_id)
            if expected is None:
                raise AuthError("unknown device_id: %r" % (device_id,))
            if not _const_time_eq(token, expected):
                raise AuthError("token mismatch")
            return device_id, role

    # ---- 注册并配对 ----
    def join(self, device_id, role):
        with self._lock:
            peers = self._by_device[device_id]
            if role in peers:
                raise AuthError("role %r already connected for %r" % (role, device_id))
            peer = Peer(device_id, role)
            peers[role] = peer
            other = peers.get("controller" if role == "device" else "device")
            paired = other is not None and not other.closed
            if paired:
                peer.link(other)
            return peer, paired

    def leave(self, peer):
        with self._lock:
            peers = self._by_device.get(peer.device_id)
            if peers and peers.get(peer.role) is peer:
                del peers[peer.role]
            peer.close()


def parse_hello(raw: bytes):
    """解析首行 JSON 握手，返回 dict。"""
    if HELLO_TERMINATOR not in raw:
        raise HandshakeError("handshake missing newline terminator")
    line, _ = raw.split(HELLO_TERMINATOR, 1)
    try:
        hello = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise HandshakeError("invalid JSON in handshake: %s" % (e,))
    if not isinstance(hello, dict):
        raise HandshakeError("hello must be a JSON object")
    return hello


def _const_time_eq(a, b):
    """恒定时间比较，避免 token 校验的时序侧信道。"""
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0
