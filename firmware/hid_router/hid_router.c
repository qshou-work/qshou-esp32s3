/*
 * hid_router/hid_router.c — HID 后端路由实现（纯逻辑）
 *
 * 设计要点见头注释。这里只强调实现上的两个刻意选择：
 *
 * 1) emit() 里只有一个 send 调用点（在 select 之后），且成功后立即 return。
 *    任何「失败换个后端再试」的写法都会破坏铁律 1（严禁双发），所以本文件
 *    不提供重试路径 —— 这是结构性保证，不是纪律问题。
 *
 * 2) 后端可用性用回调 ready() 实时查询，而不是缓存布尔量。
 *    USB 挂载/卸载、BLE 连接/断开都是异步事件，缓存必然出现「状态过期」
 *    导致的错选（比如拔线后仍走 USB，帧全丢）。
 */
#include "hid_router/hid_router.h"

typedef struct {
    hid_backend_ready_fn ready;
    hid_backend_send_fn  send;
} backend_slot_t;

static backend_slot_t s_be[HID_BACKEND_COUNT];

static hid_route_policy_t s_policy = HID_ROUTE_AUTO;
static hid_backend_t s_last = HID_BACKEND_BLE;   /* 未发送前沿用历史默认行为 */
static hid_router_stats_t s_stats;

static int is_registered(hid_backend_t b)
{
    if (b < 0 || b >= HID_BACKEND_COUNT)
        return 0;
    return s_be[b].send != NULL;
}

static int is_ready(hid_backend_t b)
{
    if (!is_registered(b))
        return 0;
    /* ready 可为 NULL —— 视为「无条件可用」（某些后端没有可探测的就绪态） */
    return s_be[b].ready ? s_be[b].ready() : 1;
}

void hid_router_register(hid_backend_t backend,
                         hid_backend_ready_fn ready,
                         hid_backend_send_fn send)
{
    if (backend < 0 || backend >= HID_BACKEND_COUNT)
        return;

    /* send 为 NULL = 注销（ready 一并清掉，避免留下半注册状态） */
    if (!send) {
        s_be[backend].ready = NULL;
        s_be[backend].send = NULL;
        return;
    }

    s_be[backend].ready = ready;
    s_be[backend].send = send;
}

int hid_router_is_registered(hid_backend_t backend)
{
    return is_registered(backend);
}

void hid_router_set_policy(hid_route_policy_t policy)
{
    if (policy < HID_ROUTE_AUTO || policy > HID_ROUTE_USB_ONLY)
        return;
    s_policy = policy;
}

hid_route_policy_t hid_router_get_policy(void)
{
    return s_policy;
}

/*
 * select_backend — 只做选择，不发报文。
 *   is_fallback 出参：AUTO 下「USB 已注册但当前不可用，因而改用 BLE」时为 1。
 *   USB 根本没注册（如尚未接入 TinyUSB 的构建）不算回落 —— 那是唯一选择。
 */
static hid_backend_t select_backend(int *is_fallback)
{
    /* is_fallback 可为 NULL（头文件契约）。必须判空再写 ——
     * 首次运行 host 单元测试时 peek_backend(NULL) 直接崩在这里：
     * 无条件 `*is_fallback = 0` 是空指针写。routes.c 里一直传合法指针，
     * 所以这个坑在实机上没暴露，但任何按契约传 NULL 的调用方都会崩。 */
    if (is_fallback)
        *is_fallback = 0;

    switch (s_policy) {
    case HID_ROUTE_BLE_ONLY:
        return HID_BACKEND_BLE;

    case HID_ROUTE_USB_ONLY:
        return HID_BACKEND_USB;

    case HID_ROUTE_AUTO:
    default:
        if (is_ready(HID_BACKEND_USB))
            return HID_BACKEND_USB;

        if (is_registered(HID_BACKEND_USB) && is_fallback)
            *is_fallback = 1;       /* 有 USB 但当前不可用 → 回落 BLE */

        return HID_BACKEND_BLE;
    }
}

int hid_router_emit(uint8_t report_id, const uint8_t *data, size_t len)
{
    if (!data || len == 0) {
        s_stats.dropped++;
        return -1;
    }

    int is_fallback = 0;
    hid_backend_t b = select_backend(&is_fallback);

    if (!is_registered(b)) {
        /* 例：HID_ROUTE_USB_ONLY 但 USB 后端尚未接入 */
        s_stats.dropped++;
        return -1;
    }

    /* ── 唯一发送点：只调一次，不重试、不换后端（铁律 1）── */
    int rc = s_be[b].send(report_id, data, len);

    if (rc != 0) {
        /* 失败即丢帧。注意这里**不能**改成「换个后端再发一次」：
         * 后端返回错误 ≠ host 没收到，重试会造成重复输入。 */
        s_stats.dropped++;
        return rc;
    }

    s_stats.sent[b]++;
    if (is_fallback)
        s_stats.fallback++;
    s_last = b;
    return 0;
}

hid_backend_t hid_router_active_backend(void)
{
    return s_last;
}

hid_backend_t hid_router_peek_backend(int *is_fallback)
{
    /* 直接复用 emit 的同一条选择路径 —— 窥探与实走必须是同一个函数，
     * 否则诊断看到的后端会和实际用的不一致（比没有诊断更糟）。 */
    return select_backend(is_fallback);
}

const char *hid_router_backend_name(hid_backend_t backend)
{
    switch (backend) {
    case HID_BACKEND_BLE: return "ble";
    case HID_BACKEND_USB: return "usb";
    default:              return "unknown";
    }
}

const char *hid_router_policy_name(hid_route_policy_t policy)
{
    switch (policy) {
    case HID_ROUTE_AUTO:     return "auto";
    case HID_ROUTE_BLE_ONLY: return "ble";
    case HID_ROUTE_USB_ONLY: return "usb";
    default:                 return "unknown";
    }
}

void hid_router_stats(hid_router_stats_t *out)
{
    if (!out)
        return;
    *out = s_stats;
}

void hid_router_reset_stats(void)
{
    s_stats.sent[HID_BACKEND_BLE] = 0;
    s_stats.sent[HID_BACKEND_USB] = 0;
    s_stats.fallback = 0;
    s_stats.dropped = 0;
}
