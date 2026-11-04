# -*- coding: utf-8 -*-
"""controller/mcp_adapter/device_client.py — 设备 HTTP 客户端。

只用标准库 urllib：适配器会被宿主（MCP 客户端）以子进程方式拉起，
依赖越少越不容易在不同宿主环境下起不来（MCP 生态里最常见的失败原因）。
httpx 虽已在 venv 里，但没必要为一次 GET/POST 引入异步栈。

两处与设备打交道的经验（都是踩出来的）：
1. **POST 绝不因超时重试**。设备是单任务 httpd + BLE HID 实时性要求，
   超时重发会重复执行（点按变两次点击、改配置触发两次重启）。
   GET 可以重试 —— 它按定义无副作用（设备侧所有 GET 都是读；写全走 POST）。
2. **绝不能并发打多个请求**。设备 httpd 只有一个任务，`/stream` 又会
   `while(1)` 独占它（见项目记忆），并发会触发 accept 套接字耗尽
   （`httpd_accept_conn error in accept (23)`) → 只能复位。
   故这里用一把进程内锁把所有请求串行化。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 设备是弱 MCU：MJPEG/WS 都会占住唯一的 httpd 任务，一个请求最长可能等几秒。
# 15~20s 是实测的甜区：低于 10s 会把「设备忙」误判成失败，高于 20s 则
# Agent 侧超时体验太差（宿主一般 30~60s 就放弃）。
DEFAULT_TIMEOUT = 20.0


class DeviceError(RuntimeError):
    """设备侧返回了错误或不可达。带 HTTP 状态码，便于 MCP 侧判 isError。"""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class DeviceClient(object):
    def __init__(self, host, token=None, timeout=DEFAULT_TIMEOUT, retries=1):
        self.host = host
        self.token = token
        self.timeout = timeout
        self.retries = retries      # 仅对 GET 生效
        self._lock = threading.Lock()  # 串行化，见模块 docstring

    # ---------------------------------------------------------------- utils
    def _headers(self):
        h = {"Accept": "application/json"}
        if self.token:
            # 固件只认 Authorization 且必须 `Bearer ` 前缀（routes.c::require_auth）
            h["Authorization"] = "Bearer %s" % self.token
        return h

    def _url(self, base_path, query=None):
        u = "http://%s%s" % (self.host, base_path)
        if query:
            u += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
        return u

    def _once(self, method, base_path, query=None, body=None):
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(base_path, query), data=data,
                                     headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
            status = r.status
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            # 非 JSON（如 httpd 的 404 文本）也照样带回去，便于 Agent 侧判断
            return status, {"_raw": raw.decode("utf-8", "replace")}

    # --------------------------------------------------------------- public
    def request(self, method, path, query=None, body=None):
        """发一次请求。GET 允许重试，POST 绝不重试（见模块 docstring）。"""
        attempts = (self.retries + 1) if method == "GET" else 1
        last = None
        for i in range(attempts):
            try:
                with self._lock:       # 全程串行，含等待时间
                    return self._once(method, path, query, body)
            except urllib.error.HTTPError as e:
                # 4xx/5xx：设备明确答复，重试没有意义（POST 超时才是「不知是否已执行」）
                raw = e.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    payload = {"_raw": raw.decode("utf-8", "replace")}
                return e.code, payload
            except Exception as e:
                last = e
                if i + 1 < attempts:
                    time.sleep(0.5)
        raise DeviceError("设备 %s 不可达（%s %s）：%s" % (self.host, method, path, last))

    def get(self, path, query=None):
        return self.request("GET", path, query)

    def post(self, path, body=None, query=None):
        return self.request("POST", path, query, body)

    def fetch_schema(self):
        """拉自描述。启动必经一步 —— 拿不到就不能注册工具（宁可起不来）。"""
        status, payload = self.get("/api/v1/schema")
        if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
            raise DeviceError("拉取 /api/v1/schema 失败：status=%s payload=%r"
                              % (status, payload), status, payload)
        return payload