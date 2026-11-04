/*
 * main/routes.h — HTTP 路由分发（连接解析层与 BLE/health 接缝）
 */
#ifndef QSHOU_ROUTES_H
#define QSHOU_ROUTES_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { M_GET = 1, M_POST = 2 } http_method_t;

/*
 * QSHOU_STREAM_BOUNDARY — MJPEG multipart 边界串（单点定义）。
 * main/main.c 的组帧（mjpeg_part）与 platform/esp32/platform_net.c 的
 * GET /stream 响应头 `Content-Type: multipart/x-mixed-replace;boundary=…`
 * 必须使用同一串，否则浏览器按 multipart 解析会失败（画面不显示）。
 */
#define QSHOU_STREAM_BOUNDARY "QshouFrame"

/*
 * routes_handle — 处理一个请求，把 JSON 响应体写入 resp（不超过 cap）。
 * 返回 HTTP 状态码（200/400/403/404/405/501）。
 * auth : 请求带的 Authorization 头（如 "Bearer xxx"），无则 NULL。
 *        g_control_auth 关闭时忽略；开启时控制端点必须匹配 g_admin_token。
 * 行为：
 *   GET  /api/v1/health        -> 遥测 JSON
 *   GET  /api/v1/schema        -> 自描述全部端点（Agent 发现用）
 *   GET  /api/v1/led?io2=N     -> IO2 灯调试（N=0..255 强制 duty；-1 恢复自动）
 *   POST /api/v1/tap           -> 透传 hid_report 到 BLE HID
 *   POST /api/v1/wifi/connect  -> 热连接指定 SSID
 *   POST /api/v1/hid/mouse     -> 直发鼠标 report（包装 emit_report）
 *   POST /api/v1/gpio/{pin}    -> 设引脚模式/电平
 *   其余 -> 404
 */
int routes_handle(http_method_t method, const char *path, const char *body,
                  const char *auth, char *resp, size_t cap);

/*
 * routes_mark_frame — 由推流主循环在每次成功取帧后调用，供 /health 上报。
 */
void routes_mark_frame(void);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_ROUTES_H */
