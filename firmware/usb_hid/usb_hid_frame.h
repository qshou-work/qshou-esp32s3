/*
 * usb_hid/usb_hid_frame.h — BLE 线帧 → USB HID 线帧转换（纯逻辑，无硬件依赖）
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 为什么需要这一层：BLE 与 USB 的「Report ID 语义」根本不同
 * ═══════════════════════════════════════════════════════════════════════
 *   BLE / HOGP：Report ID 体现在 GATT 特征关联上（Report Reference 0x2908），
 *               线帧**不带**前缀 —— 固件写 3 字节 [buttons][dX][dY] 即可。
 *   USB HID  ：Report ID 必须**写在报文首字节**（当描述符声明了多个 Report ID），
 *               否则 host 按「无 Report ID 的单一 report」解析，字段全错位。
 *
 * 所以同一份业务报文（鼠标/键盘/Consumer）在两个后端上的**线上字节不同**，
 * 这层负责转换。转换规则必须与 usb_hid 的描述符严格一一对应 ——
 * 这是描述符↔线帧契约，两侧不一致会被 host 误解析（见 hid_descriptor.c 同类注释）。
 *
 * ═══════════════════════════════════════════════════════════════════════
 * 接口划分（BIOS 兼容的关键设计，勿随意合并）
 * ═══════════════════════════════════════════════════════════════════════
 *   ┌─ 接口 0：Boot Keyboard ────────────────────────────────────────┐
 *   │ bInterfaceSubClass = 1 (Boot)                                  │
 *   │ bInterfaceProtocol = 1 (Keyboard)                              │
 *   │ 报文 = 固定 8 字节 [modifier][reserved][k0..k5]，**无 Report ID**│
 *   └────────────────────────────────────────────────────────────────┘
 *   ┌─ 接口 1：Pointer + Consumer ───────────────────────────────────┐
 *   │ Report ID 1 = 相对鼠标   4 字节 [0x01][buttons][dX][dY]        │
 *   │ Report ID 2 = Consumer   3 字节 [0x02][usage_lo][usage_hi]     │
 *   └────────────────────────────────────────────────────────────────┘
 *
 * ⚠️⚠️ 为什么键盘必须单独占一个接口：
 *   USB HID Boot Protocol 规定 boot 设备的键盘报文是**固定 8 字节、无 Report ID
 *   前缀**。若把键盘和鼠标塞进同一接口并给键盘加 Report ID 前缀，它就不再满足
 *   boot 协议格式 —— BIOS/UEFI 只认 boot 设备，于是「进 BIOS 按键无效」。
 *   这正是 USB HID 相对 BLE 的核心价值（带外控制），所以接口划分不可为了
 *   「少一个接口」而合并。Report ID 在 USB 里是 **per-interface** 编号，
 *   因此 Consumer 在接口 1 内重新编号为 2（而不是 BLE 侧的 3）。
 *
 * ⚠️ 键盘接口必须**排在描述符第一位**（interface 0）：部分 BIOS 只探测第一个
 *   HID 接口。这一点在 usb_hid 描述符里落实，本层只负责字节。
 */
#ifndef QSHOU_USB_HID_FRAME_H
#define QSHOU_USB_HID_FRAME_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── USB 接口号（必须与描述符里的 bInterfaceNumber 一致）──────────── */
#define USB_HID_ITF_KEYBOARD   0   /* Boot Keyboard（排第一，BIOS 只认它） */
#define USB_HID_ITF_POINTER    1   /* 鼠标 + Consumer 共用 */

/* ── USB 侧 Report ID（per-interface 编号；0 = 无前缀）─────────────── */
#define USB_HID_RID_NONE       0   /* boot 键盘：报文首字节就是 modifier */
#define USB_HID_RID_MOUSE      1
#define USB_HID_RID_CONSUMER   2   /* 注意：接口内重新编号，非 BLE 侧的 3 */

/* ── USB 线上帧长度（含 Report ID 前缀）───────────────────────────── */
#define USB_HID_FRAME_MOUSE     4   /* [0x01][buttons][dX][dY] */
#define USB_HID_FRAME_KEYBOARD  8   /* [modifier][reserved][k0..k5]，无前缀 */
#define USB_HID_FRAME_CONSUMER  3   /* [0x02][usage_lo][usage_hi] */
#define USB_HID_FRAME_MAX       9   /* 三者最大值（键盘 8，留 1 字节余量） */

/* 转换结果的元信息：告诉后端这帧该发到哪个接口、有没有 Report ID 前缀 */
typedef struct {
    uint8_t itf;   /* USB 接口号：0=键盘 / 1=鼠标+Consumer */
    uint8_t rid;   /* USB Report ID：0=无前缀（boot 键盘）/ 1 / 2 */
    uint8_t len;   /* 线上字节数（含前缀，即 out 的有效长度） */
} usb_hid_frame_meta_t;

/*
 * usb_hid_pack — 把一条 BLE 业务报文转换成 USB 线帧。
 *   ble_rid : BLE 侧 Report ID（1=鼠标 / 2=键盘 / 3=Consumer，见 hid_report.h）
 *   data    : BLE 线帧纯数据（不含 Report ID）
 *   len     : data 字节数，必须与该 Report ID 的契约长度一致
 *   out     : 输出缓冲，至少 USB_HID_FRAME_MAX 字节
 *   cap     : out 容量
 *   meta    : 出参，接口/Report ID/线上长度（可为 NULL）
 * 返回：写入 out 的字节数（>0）；参数非法/长度不符/缓冲不足返回 -1。
 *
 * 长度契约（与 hid_report.h 一致，不匹配即拒发 —— 宁可丢帧也不发错位报文）：
 *   ble_rid=1 → len 必须 3（HID_REPORT_LEN）
 *   ble_rid=2 → len 必须 8（HID_KEYBOARD_REPORT_LEN）
 *   ble_rid=3 → len 必须 2（HID_CONSUMER_REPORT_LEN）
 */
int usb_hid_pack(uint8_t ble_rid, const uint8_t *data, size_t len,
                 uint8_t *out, size_t cap, usb_hid_frame_meta_t *meta);

/*
 * usb_hid_frame_len — 该 BLE Report ID 转换后的线上长度（不含校验）。
 * 返回 4/8/3；未知 Report ID 返回 0。供后端预分配缓冲使用。
 */
size_t usb_hid_frame_len(uint8_t ble_rid);

/*
 * usb_hid_ble_rid_supported — 该 BLE Report ID 是否有 USB 映射。
 * 返回 1/0。用于后端在接入前判断「这类报文 USB 能不能发」。
 */
int usb_hid_ble_rid_supported(uint8_t ble_rid);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_USB_HID_FRAME_H */
