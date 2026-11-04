/*
 * main/http_api.c — /api/v1/ 请求解析（纯逻辑）
 * 合约级最小解析：只提取固件关心的固定字段，不引入通用 JSON 库。
 */
#include "main/http_api.h"
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include <stdlib.h>

/* 定位 "key" 字面量；返回其起始位置或 NULL */
static const char *find_key(const char *body, const char *key)
{
    char pat[128];
    int n = snprintf(pat, sizeof(pat), "\"%s\"", key);
    if (n < 0 || n >= (int)sizeof(pat))
        return NULL;
    return strstr(body, pat);
}

int json_find_int(const char *body, const char *key, long *out)
{
    const char *p = find_key(body, key);
    if (!p) return 0;
    p += strlen(key) + 2;                 /* 跳过 "key" */
    while (*p && *p != ':') p++;
    if (!*p) return 0;
    p++;
    while (*p && isspace((unsigned char)*p)) p++;
    char *end = NULL;
    long v = strtol(p, &end, 10);
    if (end == p) return 0;
    *out = v;
    return 1;
}

int json_find_array5(const char *body, const char *key, int out[5])
{
    const char *p = find_key(body, key);
    if (!p) return 0;
    p += strlen(key) + 2;
    while (*p && *p != ':') p++;
    if (!*p) return 0;
    p++;
    while (*p && *p != '[') p++;
    if (*p != '[') return 0;
    p++;
    int count = 0;
    while (*p && count < 5) {
        while (*p && (isspace((unsigned char)*p) || *p == ',')) p++;
        if (*p == ']') break;
        char *end = NULL;
        long v = strtol(p, &end, 10);
        if (end == p) return 0;
        out[count++] = (int)v;
        p = end;
    }
    return (count == 5) ? 1 : 0;
}

int json_find_str(const char *body, const char *key, char *out, size_t n)
{
    const char *p = find_key(body, key);
    if (!p) return 0;
    p += strlen(key) + 2;
    while (*p && *p != ':') p++;
    if (!*p) return 0;
    p++;
    while (*p && *p != '"') p++;
    if (*p != '"') return 0;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < n) {
        out[i++] = *p++;
    }
    out[i] = '\0';
    return 1;
}

int json_find_bool(const char *body, const char *key, int *out)
{
    const char *p = find_key(body, key);
    if (!p) return 0;
    p += strlen(key) + 2;
    while (*p && *p != ':') p++;
    if (!*p) return 0;
    p++;
    while (*p && isspace((unsigned char)*p)) p++;
    if (strncmp(p, "true", 4) == 0)  { *out = 1; return 1; }
    if (strncmp(p, "false", 5) == 0) { *out = 0; return 1; }
    char *end = NULL;
    long v = strtol(p, &end, 10);
    if (end == p) return 0;
    *out = (v != 0) ? 1 : 0;
    return 1;
}

int http_parse_tap(const char *body, tap_req_t *out)
{
    memset(out, 0, sizeof(*out));
    out->has_report = json_find_array5(body, "hid_report", out->hid_report);
    long x = 0, y = 0;
    if (json_find_int(body, "x", &x)) out->scr_x = (int)x;
    if (json_find_int(body, "y", &y)) out->scr_y = (int)y;
    return out->has_report ? 0 : -1;   /* 无 report 则无法哑中继 */
}

int http_parse_swipe(const char *body, swipe_req_t *out)
{
    memset(out, 0, sizeof(*out));
    out->has_start = json_find_array5(body, "hid_report_start", out->hid_report_start);
    out->has_end   = json_find_array5(body, "hid_report_end",   out->hid_report_end);
    long v = 0;
    if (json_find_int(body, "x0", &v)) out->x0 = (int)v;
    if (json_find_int(body, "y0", &v)) out->y0 = (int)v;
    if (json_find_int(body, "x1", &v)) out->x1 = (int)v;
    if (json_find_int(body, "y1", &v)) out->y1 = (int)v;
    return (out->has_start && out->has_end) ? 0 : -1;
}

int http_parse_calib_point(const char *body, calib_point_req_t *out)
{
    memset(out, 0, sizeof(*out));
    long idx = 0, cx = 0, cy = 0;
    out->has_point = json_find_int(body, "index", &idx)
                  && json_find_int(body, "cap_x", &cx)
                  && json_find_int(body, "cap_y", &cy);
    out->index = (int)idx;
    out->cap_x = (float)cx;
    out->cap_y = (float)cy;
    return out->has_point ? 0 : -1;
}

int http_parse_calib_commit(const char *body, calib_commit_req_t *out)
{
    memset(out, 0, sizeof(*out));
    out->has_device = json_find_str(body, "device_id", out->device_id,
                                    sizeof(out->device_id));
    return 0;  /* 始终接受；device_id 缺失时由上层决定动作 */
}

int http_parse_key(const char *body, key_req_t *out)
{
    memset(out, 0, sizeof(*out));
    out->has_key = json_find_str(body, "key", out->key, sizeof(out->key));
    return out->has_key ? 0 : -1;
}
