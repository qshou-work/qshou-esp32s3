# 临时真机验证脚本：P3(BLE) + GPIO 白名单修正 + P4(WebSocket)
# 用法: python tools/_verify_p34.py <host>
import asyncio
import json
import sys

import urllib.request

import websockets

HOST = sys.argv[1] if len(sys.argv) > 1 else "qshou-0a0c.local"
BASE = "http://%s" % HOST

results = []


def http(path, body=None, method=None):
    url = BASE + path
    data = body.encode() if body else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "ERR %s" % e


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("%s %-46s %s" % ("PASS" if cond else "FAIL", name, detail[:110]))


# ---------- GPIO 白名单修正 ----------
st, b = http("/api/v1/gpio")
check("GET /gpio reachable", st == 200, "status=%d" % st)
if st == 200:
    try:
        # 实际结构是 {"ok":true,"pins":[{pin,mode,value}, ...]}
        pins = sorted(int(p["pin"]) for p in json.loads(b)["pins"])
    except Exception as e:
        pins = []
        print("   parse fail:", e, "raw=", b[:120])
    check("gpio whitelist == {19,21,22,23,25}", pins == [19, 21, 22, 23, 25], "got=%s" % pins)

st, b = http("/api/v1/gpio/26", '{"mode":"out"}')
check("POST /gpio/26 (PSRAM pin) rejected", st == 400, "status=%d body=%s" % (st, b[:70]))

st, b = http("/api/v1/gpio/27", '{"mode":"out"}')
check("POST /gpio/27 (PSRAM pin) rejected", st == 400, "status=%d" % st)

st, b = http("/api/v1/gpio/19", '{"mode":"out","value":1}')
check("POST /gpio/19 out=1 accepted", st == 200 and '"value":1' in b, "status=%d %s" % (st, b[:70]))
http("/api/v1/gpio/19", '{"mode":"in"}')

# ---------- P3 BLE ----------
st, b = http("/api/v1/ble/status")
check("GET /ble/status", st == 200, "status=%d %s" % (st, b[:100]))

st, b = http("/api/v1/ble/bonds")
check("GET /ble/bonds", st == 200, "status=%d %s" % (st, b[:100]))

st, b = http("/api/v1/ble/advertise", '{"on":0}')
check("POST /ble/advertise off", st == 200, "status=%d %s" % (st, b[:80]))
st, b = http("/api/v1/ble/status")
# 设备用 0/1 而非 true/false 表示布尔
check("advertise off sticks (no auto-restart)",
      st == 200 and '"advertising":0' in b.replace(" ", ""), "%s" % b[:110])
http("/api/v1/ble/advertise", '{"on":1}')

# ---------- P4 events (HTTP status view) ----------
st, b = http("/api/v1/events")
check("GET /events returns subscriber state", st == 200 and '"transport":"websocket"' in b.replace(" ", ""),
      "status=%d %s" % (st, b[:90]))


# ---------- P4 WebSocket ----------
async def ws_check():
    url = "ws://%s/api/v1/events" % HOST
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            # httpd 原生 WS 不会在握手后主动推帧；必须客户端先发一帧，
            # 设备才会回调 handler 并回 status（内含 ip/rssi/ble/subcribers）。
            await ws.send('{"action":"hello"}')
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=12)
            except asyncio.TimeoutError:
                reply = "<timeout: no reply>"
            check("WS frame -> status reply", '"type":"status"' in reply, reply[:130])
            check("WS reply carries device state",
                  '"ip"' in reply and '"subscribers"' in reply, reply[:130])

            # 双向：再发一帧仍应得到独立回帧（证明连接可重复使用）
            await ws.send('{"action":"ping"}')
            try:
                reply2 = await asyncio.wait_for(ws.recv(), timeout=12)
            except asyncio.TimeoutError:
                reply2 = "<timeout>"
            check("WS second frame -> reply", '"type":"status"' in reply2, reply2[:130])

            # 连接存续期间普通 HTTP 仍可用（验证 WS 不霸占 httpd 任务）
            st, b = http("/api/v1/health")
            check("HTTP still works while WS open", st == 200, "status=%d" % st)
    except Exception as e:
        check("WS connect/handshake", False, "exc=%r" % (e,))


asyncio.run(ws_check())


# ---------- P4 回归：僵尸订阅 / fd 复用 ----------
# 🔴 这条必须在「先连 WS → 断开 → 再发普通 GET」的顺序下才有意义。
#    旧实现用「fd 是否已登记在订阅表」区分普通 GET 与 WS 帧，而 fd 会被
#    lwIP 复用：客户端异常断开（这里用 transport 层硬关，不发 CLOSE 帧）
#    会在表里留下僵尸条目 → 随后的普通 GET 命中该 fd 被误判成 WS 帧 →
#    不发响应直接关连接 → GET /events 返回 0 字节。
#    只跑前面的普通 GET 检查是测不出来的（那是「干净」状态）。
async def stale_fd_regression():
    url = "ws://%s/api/v1/events" % HOST
    for i in range(3):
        ws = await websockets.connect(url, open_timeout=15)
        await ws.send('{"action":"probe"}')
        try:
            await asyncio.wait_for(ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            pass
        # 硬关：不发 CLOSE 帧，模拟进程被杀 / 拔网线。
        # ⚠️ transport 是**实例**属性（websockets 17 的 asyncio 实现），
        #    类上没有 → 只能 ws.transport.abort()，不能写 ws.close()。
        #    更稳的兜底：取不到 transport 时直接 abort 底层 socket。
        tr = getattr(ws, "transport", None)
        if tr is not None and hasattr(tr, "abort"):
            tr.abort()
        else:                       # 兜底：退回协议层 socket
            try:
                ws.protocol.transport.abort()
            except Exception:
                pass
        try:
            await ws.wait_closed()
        except Exception:
            pass
    # 紧跟着发普通 GET：此时若 fd 被复用就会命中僵尸条目
    st, b = http("/api/v1/events")
    check("GET /events survives stale WS fds",
          st == 200 and '"transport":"websocket"' in b.replace(" ", ""),
          "status=%d len=%d %s" % (st, len(b), b[:90]))
    # 计数必须已回收：没有活跃订阅者时 subscribers 应为 0
    check("subscriber count reaped after hard close",
          '"subscribers":0' in b.replace(" ", ""),
          "僵尸条目未回收 -> %s" % b[:110])


asyncio.run(stale_fd_regression())

print("\n===== SUMMARY =====")
p = sum(1 for _, ok, _ in results if ok)
print("%d/%d passed" % (p, len(results)))
for n, ok, d in results:
    if not ok:
        print("  FAILED: %s | %s" % (n, d[:150]))