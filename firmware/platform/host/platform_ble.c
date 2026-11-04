/*
 * platform/host/platform_ble.c — host 桩
 * 不依赖真实 BLE：把每次发射的 report 写入环形历史缓冲并递增计数，
 * 供单元测试读取整段序列（验证 tap / swipe 的多 report 行为）。
 */
#include "platform/platform.h"
#include "ble_hid/ble_hid.h"
#include "ble_hid/hid_report.h"
#include "hid_router/hid_router.h"
#include <stdio.h>
#include <string.h>

#define HOST_BLE_BUF   16
#define HOST_BLE_HIST  16   /* 保存最近 N 次发射，每次 5 字节 */

static uint8_t s_hist[HOST_BLE_HIST][HOST_BLE_BUF];
static int     s_hist_len[HOST_BLE_HIST];   /* 每次实际字节数 */
static uint8_t s_hist_rid[HOST_BLE_HIST];   /* 每次的 Report ID（复合设备用） */
static int     s_total = 0;                 /* 累计发射次数 */
static int     s_widx  = 0;                 /* 环形写入下标 */

int platform_ble_emit_report(uint8_t report_id, const uint8_t *report, size_t len)
{
    if (!report || len == 0 || len > HOST_BLE_BUF)
        return -1;
    for (size_t i = 0; i < len; i++)
        s_hist[s_widx][i] = report[i];
    s_hist_len[s_widx] = (int)len;
    s_hist_rid[s_widx] = report_id;
    s_widx = (s_widx + 1) % HOST_BLE_HIST;
    s_total++;
    return 0;
}

int platform_ble_emit(const uint8_t *report, size_t len)
{
    /* host 桩的鼠标包装：固定 Report ID 1（与 esp32 行为一致） */
    return platform_ble_emit_report(1, report, len);
}

/* HID 发射出口注入（host 桩）。
 * host 构建没有手势/按键常驻队列，因此没有内部发射点需要经过路由；
 * 这里保留 setter 的唯一目的是让装配层（main.c）的调用能链接，
 * 保证「同一份 main.c 在 host/esp32 都能构建」这条接缝不被破坏。 */
static platform_hid_emit_fn s_hid_emit = NULL;

void platform_hid_set_emit(platform_hid_emit_fn fn)
{
    s_hid_emit = fn;
}

/* 读第 i 次发射的 Report ID（与 host_ble_get_nth 配套） */
int host_ble_get_nth_rid(int i)
{
    int avail = s_total < HOST_BLE_HIST ? s_total : HOST_BLE_HIST;
    if (i < 0 || i >= avail) return 0;
    int idx = (s_widx - avail + i + HOST_BLE_HIST) % HOST_BLE_HIST;
    return s_hist_rid[idx];
}

/* 读第 i 次发射（i 从 0 起，最旧可达 min(total,HIST)）。返回累计发射次数。 */
int host_ble_get_nth(int i, uint8_t *out, int *len)
{
    int avail = s_total < HOST_BLE_HIST ? s_total : HOST_BLE_HIST;
    if (i < 0 || i >= avail) return s_total;
    /* 最旧一条位于 (s_widx - avail + HOST_BLE_HIST) % HIST */
    int idx = (s_widx - avail + i + HOST_BLE_HIST) % HOST_BLE_HIST;
    if (out && len) {
        for (int j = 0; j < s_hist_len[idx]; j++)
            out[j] = s_hist[idx][j];
        *len = s_hist_len[idx];
    }
    return s_total;
}

void platform_ble_last_gesture(int *frames, int *dx, int *dy)
{
    if (frames) *frames = 0;
    if (dx)     *dx     = 0;
    if (dy)     *dy     = 0;
}

void platform_ble_gesture_jitter(int *min_ms, int *avg_ms, int *max_ms)
{
    if (min_ms) *min_ms = 0;
    if (avg_ms) *avg_ms = 0;
    if (max_ms) *max_ms = 0;
}

void platform_ble_gesture_sent(int *sent, int *drop)
{
    if (sent) *sent = 0;
    if (drop) *drop = 0;
}

void host_ble_reset(void)
{
    s_total = 0;
    s_widx  = 0;
    for (int i = 0; i < HOST_BLE_HIST; i++) { s_hist_len[i] = 0; s_hist_rid[i] = 0; }
}

/* ── ble_hid.h 的两个执行器：host 同步实现 ──────────────────────────────
 * esp32 接缝的实现是「入队 + 常驻任务按 step_ms 播放」；host 没有队列也没有
 * 实时要求，**就地同步播放**，但保持三项语义与 esp32 完全一致：
 *   ① 返回码契约：0=成功 / 1=忙（host 同步，永不忙）/ <0=参数或发射错误
 *   ② 每帧都经 ble_hid_emit_report / hid_router_emit —— 与 esp32 回放路径
 *      **同一条路由**。绝不直接调 platform_ble_emit 绕过路由：USB 挂载时
 *      手势必须也能走 USB（见 platform.h 里 platform_hid_set_emit 的注释）
 *   ③ 手势点数上限 256，与 esp32 gesture_task 的局部数组一致
 */
int ble_hid_play_gesture(const gesture_spec_t *spec)
{
    if (!spec)
        return -1;

    gesture_pt_t pts[256];
    int n = ble_hid_build_gesture_points(spec, pts, 256);
    if (n <= 0)
        return -1;                      /* 空间不足/参数非法 */

    for (int i = 0; i < n; i++) {
        uint8_t rep[3];
        hid_pack_report(rep, pts[i].buttons, pts[i].dx, pts[i].dy);
        if (ble_hid_emit_report(rep, sizeof rep) != 0)
            return -1;
    }
    return 0;
}

int ble_hid_send_key(const char *name)
{
    if (!name || !name[0])
        return -1;

    uint8_t rid = 0, len = 0;
    uint8_t press[HID_KEY_FRAME_MAX], release[HID_KEY_FRAME_MAX];
    if (ble_hid_key_frames(name, &rid, &len, press, release) != 0)
        return -1;                      /* 名称未知 → 路由层映射 400 */

    if (hid_router_emit(rid, press, len) != 0)
        return -1;
    if (hid_router_emit(rid, release, len) != 0)
        return -1;
    return 0;
}

/* ---- 控制 API：BLE 外设态管理（host 桩；返回安全默认，真实逻辑 P3）---- */
static bool s_advertising = false;

int platform_ble_advertise(bool on)
{
    s_advertising = on;
    return 0;
}

int platform_ble_disconnect(void)
{
    return 0;
}

int platform_ble_list_bonds(char *buf, size_t n)
{
    if (buf && n) snprintf(buf, n, "[]");
    return 0;
}

int platform_ble_forget_bond(int id)
{
    (void)id;
    return 0;
}

bool platform_ble_is_advertising(void)
{
    return s_advertising;
}

/* ---- 事件通道桩（host 无 WebSocket；仅记录最后一条广播便于断言）---- */
static char s_last_event[256];
static int  s_event_subs = 0;   /* host 桩：虚拟订阅者数 */

int platform_events_subscriber_count(void)
{
    return s_event_subs;
}

int platform_events_broadcast(const char *json)
{
    if (!json) return 0;
    strncpy(s_last_event, json, sizeof(s_last_event) - 1);
    s_last_event[sizeof(s_last_event) - 1] = '\0';
    return s_event_subs;
}
