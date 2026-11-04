/*
 * hid_router/hid_router.h — HID 后端路由（纯逻辑，无硬件依赖）
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 为什么需要路由层
 * ═══════════════════════════════════════════════════════════════════════
 * 同一份业务动作（鼠标移动/按键）现在有两个物理出口：
 *   BLE HID —— 带内控制：依赖被控机 OS 的蓝牙栈（桌面可用，BIOS 不可用）
 *   USB HID —— 带外控制：被控机只当它是一根普通键盘（BIOS 可用）
 *
 * 上层（routes.c 的 HTTP 端点、ble_hid.c 的手势回放）不应该关心报文最终
 * 走哪条链路 —— 它们只表达「按下左键」「敲 a」。路由决策收敛到本层，
 * 这样接第三个后端（CH9329 串口转 HID）时上层零改动。
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 两条铁律（都是真机 bug 会立刻暴露的类型）
 * ═══════════════════════════════════════════════════════════════════════
 * 铁律 1 —— **严禁双发**。
 *   若同一帧同时经 BLE 与 USB 送达同一台被控机，会收到两次输入：
 *   鼠标光标跳两倍距离、键盘字符重复两次。因此 emit() 的语义是
 *   「选定一个后端，只调它一次」。**失败也不跨后端重试** ——
 *   因为「USB send 返回错误」并不等于「host 没收到」（EP 忙/超时时
 *   数据可能已入 FIFO），重试即构成双发风险。失败就丢这一帧并计数。
 *
 * 铁律 2 —— **USB 优先、BLE 兜底**。
 *   USB 一旦挂载就优先走它（带外能力更强、延迟更低、不受蓝牙配对状态影响）；
 *   USB 不在（没插线 / 未枚举）时回落 BLE。策略可被 API 显式覆盖
 *   （hid_router_set_policy），用于诊断与「强制走某条链路」的场景。
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 分层位置
 * ═══════════════════════════════════════════════════════════════════════
 *   routes.c / ble_hid.c          ← 业务层（只表达意图）
 *            ↓ hid_router_emit(ble_rid, data, len)   ← 本层：选后端
 *   ┌────────┴────────┐
 *   BLE 后端            USB 后端
 *   platform_ble_       platform_usb_emit()（Phase 1 接 TinyUSB）
 *   emit_report()       内部用 usb_hid_pack() 转线帧
 *
 * 注意：本层传的是 **BLE 语义的 report_id**（1/2/3，见 hid_report.h），
 * USB 后端自己负责转成 USB 线帧（usb_hid_frame.h）。这样业务层与 BLE
 * 的既有契约完全不变，改造面最小。
 */
#ifndef QSHOU_HID_ROUTER_H
#define QSHOU_HID_ROUTER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── 后端标识 ─────────────────────────────────────────────────────── */
typedef enum {
    HID_BACKEND_BLE = 0,
    HID_BACKEND_USB = 1,
    HID_BACKEND_COUNT = 2
} hid_backend_t;

/* ── 路由策略 ─────────────────────────────────────────────────────── */
typedef enum {
    HID_ROUTE_AUTO = 0,     /* 默认：USB 优先，回落 BLE（铁律 2） */
    HID_ROUTE_BLE_ONLY,     /* 强制 BLE（诊断/对比测试用） */
    HID_ROUTE_USB_ONLY      /* 强制 USB；不可用时丢帧，不回落 */
} hid_route_policy_t;

/* ── 后端回调签名 ─────────────────────────────────────────────────── */
/* ready: 返回 1 表示该后端当前可用（BLE=已连接 / USB=已挂载） */
typedef int (*hid_backend_ready_fn)(void);
/* send : 语义同 platform_ble_emit_report —— 参数是 BLE 语义的 report_id
 *        与**不带 Report ID 前缀**的纯数据。返回 0 成功，非 0 失败。 */
typedef int (*hid_backend_send_fn)(uint8_t report_id, const uint8_t *data, size_t len);

/*
 * hid_router_register — 注册/覆盖一个后端（平台层初始化时调用）。
 *   ready / send 传 NULL 表示注销该后端。
 * 未注册的后端永不参与路由。
 */
void hid_router_register(hid_backend_t backend,
                         hid_backend_ready_fn ready,
                         hid_backend_send_fn send);

/* 查询某后端是否已注册 */
int hid_router_is_registered(hid_backend_t backend);

/*
 * hid_router_set_policy / hid_router_get_policy
 *   设置/读取路由策略。默认 HID_ROUTE_AUTO。
 *   注意：策略是全局状态，非线程安全 —— 只在初始化或 HTTP 配置端点里改。
 */
void hid_router_set_policy(hid_route_policy_t policy);
hid_route_policy_t hid_router_get_policy(void);

/*
 * hid_router_emit — 发射一条 HID 报文（核心入口）。
 *   report_id : BLE 语义 Report ID（1=鼠标 / 2=键盘 / 3=Consumer）
 *   data      : 纯数据（不带 Report ID 前缀），长度须符合该 RID 契约
 *   len       : data 字节数
 * 返回：0 = 已被某个后端接受；-1 = 参数非法 / 无可用后端 / 后端发送失败。
 *
 * 行为保证：
 *   - 最多调用**一个**后端的 send（严禁双发，铁律 1）
 *   - 后端 send 失败不重试、不换后端（同上）
 *   - 未注册任何后端时返回 -1（不崩）
 */
int hid_router_emit(uint8_t report_id, const uint8_t *data, size_t len);

/*
 * hid_router_active_backend — 上一次**实际**使用的后端（诊断/UI 展示）。
 * 尚未发送过任何报文时返回 HID_BACKEND_BLE（即历史默认行为）。
 * ⚠️ 这是「过去时」，不等于「下一帧会走哪」：一次都没发过时它恒为 BLE，
 *    哪怕 USB 早已挂载。诊断「报文会走哪条链路」请用 hid_router_peek_backend。
 */
hid_backend_t hid_router_active_backend(void);

/*
 * hid_router_peek_backend — 此刻若发一帧，会被选中的后端（**不发报文**）。
 *
 * 为什么单独开这个口：active_backend 返回的是上一次成功发射的后端，
 * 一条都没发过时恒为 BLE。用它做诊断会给出与事实相反的答案 ——
 * 实测场景：USB 已挂载（mounted=1）、stats 全 0，active 却显示 "ble"，
 * 看着像「路由选错了」，实际只是还没发过帧。
 *
 * is_fallback 出参（可为 NULL）：AUTO 下「USB 已注册但当前不可用，
 * 因而会改用 BLE」时为 1；USB 根本没注册不算回落（那是唯一选择）。
 */
hid_backend_t hid_router_peek_backend(int *is_fallback);

/* 后端名字（日志/JSON 用），未知返回 "unknown" */
const char *hid_router_backend_name(hid_backend_t backend);

/* 策略名字（日志/JSON 用） */
const char *hid_router_policy_name(hid_route_policy_t policy);

/* ── 统计（诊断：哪个后端发了多少、回落了几次、丢了多少）──────────
 * ⚠️ 计数非原子（多任务并发时可能少计一两帧）。仅用于诊断展示，
 *    不参与任何控制逻辑 —— 不要用它做流控判断。 */
typedef struct {
    uint32_t sent[HID_BACKEND_COUNT];  /* 各后端成功发送帧数 */
    uint32_t fallback;                 /* AUTO 下首选不可用而回落的次数 */
    uint32_t dropped;                  /* 无后端可用 / 发送失败的帧数 */
} hid_router_stats_t;

void hid_router_stats(hid_router_stats_t *out);
void hid_router_reset_stats(void);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_HID_ROUTER_H */
