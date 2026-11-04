"""shared/transport.py — 传输层抽象 (implementation_order.step_8b, MVP 单后端 outbound_relay)

MVP 仅实现 RelayBackend 一个后端：控制端经 broker（project-operated）中转，
命令走 HTTP/JSON（见 shared/api_schema.yaml），视频流走 MJPEG（broker 透传 ESP32 的 /api/v1/stream）。
坐标换算在控制端完成（见 coords.py），本层只负责「把指令送到 broker / 把帧拉回控制端」。

设计约束（来自规格）：
- 命令必须可靠，走 broker 的 TCP 长连接（HTTP POST）。
- 视频走 MJPEG over HTTP（multipart/x-mixed-replace），用标准库解析，避免引入 websockets 库。
  ESP32 CameraWebServer 的 /stream 端点即为此形态，可直接复用。
- broker 仅透传字节、不解析业务数据（满足 R20 的 E2E 密文约束）。

可测试性：http_post 可注入；MJPEG 解析与网络解耦（parse_mjpeg_frames 接收任意 file-like）。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Optional, Protocol, runtime_checkable

__all__ = ["Transport", "TransportError", "RelayBackend", "parse_mjpeg_frames"]


class TransportError(Exception):
    """传输层统一错误：网络 / HTTP / 解析失败都归因到这里。"""


@runtime_checkable
class Transport(Protocol):
    def connect(self, base_url: str, token: str) -> None: ...

    def send_command(self, endpoint: str, payload: dict) -> dict: ...

    def stream_frames(self, source_url: str, on_frame: Callable[[bytes], None]) -> int: ...

    def report_endpoint(self) -> str: ...


def _default_http_post(base_url: str, endpoint: str, token: str, payload: dict) -> dict:
    """默认 HTTP 实现：POST JSON + X-Auth-Token 头。可注入以便测试。"""
    url = f"{base_url.rstrip('/')}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Auth-Token": token}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # noqa: BLE001 - 统一收敛为 TransportError
        raise TransportError(f"HTTP {e.code}: {e.reason} for {url}") from e
    except urllib.error.URLError as e:  # noqa: BLE001
        raise TransportError(f"network error: {e.reason} for {url}") from e


def parse_mjpeg_frames(
    stream, on_frame: Callable[[bytes], None], boundary: bytes = b"frame"
) -> int:
    """从 MJPEG multipart 流（file-like，支持 .read(n)）解析 JPEG 帧并回调。

    返回解析到的帧数。与网络解耦，便于单元测试（用 BytesIO 喂合成流）。
    假定每段带 Content-Length（ESP32 CameraWebServer 即如此）；无 CL 时退化为按下一边界切。
    """
    search = b"--" + boundary + b"\r\n"
    cl_re = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)
    data = b""
    count = 0
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        data += chunk
        idx = data.find(search)
        while idx != -1:
            header_end = data.find(b"\r\n\r\n", idx)
            if header_end == -1:
                break  # 头还没收全，等更多数据
            segment = data[idx:header_end]
            m = cl_re.search(segment)
            start = header_end + 4
            if m:
                cl = int(m.group(1))
                if len(data) < start + cl:
                    break  # 帧体未收全，等更多数据
                on_frame(data[start : start + cl])
                count += 1
                data = data[start + cl :]
            else:
                nxt = data.find(search, idx + len(search))
                if nxt == -1:
                    break
                if start < nxt:
                    on_frame(data[start:nxt])
                    count += 1
                data = data[nxt:]
            idx = data.find(search)
    return count


class RelayBackend:
    """MVP 单后端：控制端 → broker → ESP32。broker 仅透传，不解包业务数据。

    视频流通过 MJPEG over HTTP 拉取（标准库实现，零第三方依赖）。
    """

    def __init__(self, http_post=_default_http_post):
        self._base_url: Optional[str] = None
        self._token: str = ""
        self._http_post = http_post

    def connect(self, base_url: str, token: str) -> None:
        if not base_url:
            raise TransportError("base_url 不能为空")
        self._base_url = base_url.rstrip("/")
        self._token = token or ""

    def send_command(self, endpoint: str, payload: dict) -> dict:
        if self._base_url is None:
            raise TransportError("未连接：请先调用 connect()")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return self._http_post(self._base_url, endpoint, self._token, payload)

    def stream_frames(self, source_url: str, on_frame: Callable[[bytes], None]) -> int:
        """拉取 MJPEG 流并对每帧回调。返回帧数（单次回放/测试时用于统计）。"""
        headers = {"X-Auth-Token": self._token} if self._token else {}
        req = urllib.request.Request(source_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return parse_mjpeg_frames(resp, on_frame)

    def report_endpoint(self) -> str:
        return self._base_url or ""
