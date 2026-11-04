"""HTTP 延迟分解：把一次请求拆成 DNS/握手/首字节/下载 四段。

用法: python .tmp/jschk/http_breakdown.py [ip]

背景：本项目 httpd 用的是 HTTPD_DEFAULT_CONFIG()，其中
      keep_alive_enable = false、max_open_sockets = 7。
      这意味着每个 HTTP 请求都要重新 TCP 三次握手。
      在高延迟链路上，一个握手 = 一个 RTT，代价被完全暴露。

本脚本回答三个问题：
  1. TCP 握手占一次请求多大比例？
  2. 服务端是否支持 keep-alive（响应头 Connection）？
  3. 大资源（48KB 的 UI 页面）与小 API 请求的时间分布差别？
"""
import socket
import sys
import time

IP = sys.argv[1] if len(sys.argv) > 1 else "10.59.228.53"
PORT = 80
REUSE_HOST = None  # 复用的 socket（若服务端支持 keep-alive）


def http_once(path, sock=None):
    """返回 (握手耗时, TTFB, 下载耗时, 总耗时, 响应头文本, socket)"""
    created = sock is None
    t0 = time.time()
    if created:
        s = socket.create_connection((IP, PORT), timeout=25)
        handshake = (time.time() - t0) * 1000
    else:
        s = sock
        handshake = 0.0

    req = ("GET %s HTTP/1.1\r\nHost: %s\r\n"
           "User-Agent: diag/1.0\r\nAccept: */*\r\n"
           "Connection: keep-alive\r\n\r\n" % (path, IP))
    t1 = time.time()
    s.sendall(req.encode())
    s.settimeout(25)

    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    ttfb = (time.time() - t1) * 1000

    hdr = head.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
    first_line = hdr.splitlines()[0] if hdr else "(无响应头)"
    conn_hdr = ""
    for line in hdr.splitlines():
        if line.lower().startswith("connection:"):
            conn_hdr = line.strip()
    clen = 0
    for line in hdr.splitlines():
        if line.lower().startswith("content-length:"):
            clen = int(line.split(":")[1].strip())

    body = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
    t2 = time.time()
    while len(body) < clen:
        chunk = s.recv(65536)
        if not chunk:
            break
        body += chunk
    dl = (time.time() - t2) * 1000

    total = (time.time() - t0) * 1000
    return handshake, ttfb, dl, total, first_line, conn_hdr, clen, s


def probe(label, path, sock=None):
    h, t, d, tot, line, conn, clen, s = http_once(path, sock)
    tag = "复用连接" if sock else "新建连接"
    print("  %-28s [%s]" % (label, tag))
    print("      握手=%7.1fms  TTFB=%7.1fms  下载=%7.1fms  合计=%7.1fms  %d字节"
          % (h, t, d, tot, clen))
    print("      %s | %s" % (line[:60], conn or "（无 Connection 头）"))
    return s, conn


print("=== HTTP 延迟分解 (%s) ===\n" % IP)
print("--- 小请求：/api/v1/health ---")
s1, conn = probe("第1次", "/api/v1/health")
s2, _ = probe("第2次", "/api/v1/health", s1)

print("\n--- 大资源：/ (UI 页面) ---")
s3, _ = probe("第1次", "/")

print("\n结论支撑：")
try:
    import statistics
except ImportError:
    statistics = None
if conn:
    print("  服务端 Connection 头 = %s" % conn)
print("  注意：两次请求之间的握手耗时差异，即为每次新建 TCP 连接的代价；")
print("        若第2次握手=0 且成功，说明服务端支持复用；若被关则证明 keep-alive 未开启。")
