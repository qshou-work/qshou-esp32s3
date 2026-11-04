/*
 * main/http_api.h — /api/v1/ 请求解析（纯逻辑，无硬件依赖）
 *
 * 说明：固件只接收“已换算”的指令（v1.9.1 决策：SCR→HID 在控制端完成），
 * 因此 /tap、/swipe 的核心载荷是 5 字节 hid_report 数组；SCR 坐标仅作冗余记录。
 * 这里实现的是“合约级”最小解析器——仅提取本固件关心的固定字段，不做通用
 * JSON 解析（避免引入第三方库，契合 A8 零依赖目标）。完整校验在控制台侧完成。
 */
#ifndef QSHOU_HTTP_API_H
#define QSHOU_HTTP_API_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* HTTP 契约：控制器发来的仍是 5 字节**绝对坐标** report
 * [state][X_MSB][X_LSB][Y_MSB][Y_LSB]（与 shared/coords.py 同源）。
 * 这与 BLE 线帧（3 字节相对）是两回事——绝对→相对换算在 ble_hid.c 内完成。 */
#define HID_REPORT_ABS_LEN 5

/* ---- 请求结构 ---- */
typedef struct {
    int hid_report[HID_REPORT_ABS_LEN];  /* 已换算的 5 字节绝对 report；has_report=1 时使用 */
    int has_report;
    int scr_x, scr_y;               /* 冗余 SCR 坐标（仅记录） */
} tap_req_t;

typedef struct {
    int hid_report_start[HID_REPORT_ABS_LEN];
    int hid_report_end[HID_REPORT_ABS_LEN];
    int has_start, has_end;
    int x0, y0, x1, y1;             /* 冗余 SCR 坐标 */
} swipe_req_t;

typedef struct {
    int   index;        /* 标定点序号 0-8 */
    float cap_x, cap_y; /* 相机检测到的像素坐标 */
    int   has_point;
} calib_point_req_t;

typedef struct {
    char  device_id[64];
    int   has_device;
} calib_commit_req_t;

typedef struct {
    char  key[64];
    int   has_key;
} key_req_t;

/* ---- 最小合约级 JSON 取值 ---- */
int json_find_int(const char *body, const char *key, long *out);
int json_find_array5(const char *body, const char *key, int out[5]);
int json_find_str(const char *body, const char *key, char *out, size_t n);
int json_find_bool(const char *body, const char *key, int *out);  /* true/false 或 0/1 */

/* ---- 各端点解析；成功返回 0 ---- */
int http_parse_tap(const char *body, tap_req_t *out);
int http_parse_swipe(const char *body, swipe_req_t *out);
int http_parse_calib_point(const char *body, calib_point_req_t *out);
int http_parse_calib_commit(const char *body, calib_commit_req_t *out);
int http_parse_key(const char *body, key_req_t *out);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_HTTP_API_H */
