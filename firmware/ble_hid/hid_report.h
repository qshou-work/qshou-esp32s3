/*
 * ble_hid/hid_report.h — 3 字节相对坐标 report 打包（纯逻辑，无硬件依赖）
 *
 * BLE 线帧契约（固件→中心端）：3 字节**相对**报告
 *   [buttons][dX][dY]，X/Y 为相对增量，范围 [-127, 127]（8-bit 有符号）。
 *
 * 设备模型：相对鼠标（Relative Mouse，Generic Desktop / Mouse + 相对 X/Y）。
 * 中心端把每帧 (dX,dY) 当作「系统光标相对上一帧的位移」累加 ——
 * 天然无「绝对坐标依赖 host 回传光标位置」的失步问题。安卓等 host 对相对鼠标
 * 支持可靠（实测 box 手势可画出均匀方框并精准回原点）。
 * ⚠️ 绝不可用绝对坐标形态（Digitizer 被当触摸屏；Absolute Mouse 不创建设备）。
 * （详见 hid_descriptor.c 头注释：为何选相对鼠标。）
 *
 * 字节序：X/Y 为 8-bit 有符号，单字节，无端序问题。
 */
#ifndef QSHOU_HID_REPORT_H
#define QSHOU_HID_REPORT_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HID_REPORT_LEN  3       /* 鼠标单帧字节数（相对鼠标） */
#define HID_REPORT_ID   0x01    /* 鼠标 HID Report ID（向后兼容别名） */
#define HID_BTN_LEFT    0x01    /* 左键 / 按下 */
#define HID_BTN_RIGHT   0x02    /* 右键 */
#define HID_BTN_MIDDLE  0x04    /* 中键 */

/* ── 复合设备：多 Report ID（鼠标 + 键盘 + Consumer Control）──────────
 * 三者共用**一份** report map，esp_hid/nimble_hidd 会为每个 Report ID
 * 注册独立的 Report 特征（带 Report Reference 0x2908），host 据此区分；
 * 线帧同样**不带** Report ID 前缀（HOGP 靠特征关联，见 hid_descriptor.c）。
 * ⚠️ 报告数受 CONFIG_BT_NIMBLE_SVC_HID_MAX_RPTS（=3）限制，正好 3 个。
 *
 *   RID 1 = 相对鼠标    3 字节 [buttons][dX][dY]
 *   RID 2 = 键盘        8 字节 [modifier][reserved][k0..k5]
 *   RID 3 = Consumer     2 字节 [usage_lo][usage_hi]（16-bit usage，小端）
 */
#define HID_REPORT_ID_MOUSE     0x01
#define HID_REPORT_ID_KEYBOARD  0x02
#define HID_REPORT_ID_CONSUMER  0x03
#define HID_KEYBOARD_REPORT_LEN 8
#define HID_CONSUMER_REPORT_LEN 2

/* 键盘修饰键位（HID Usage Page 0x07，0xE0-0xE7；= report[0] 的位掩码） */
#define HID_MOD_LSHIFT  0x02
#define HID_MOD_RSHIFT  0x20
#define HID_MOD_LCTRL   0x01
#define HID_MOD_LALT    0x04
#define HID_MOD_LGUI    0x08

/* 相对增量上下限：BLE 线帧 X/Y 各 8 bit 有符号，范围 [-127, 127] */
#define HID_DELTA_MAX   127
#define HID_DELTA_MIN   (-127)

/* 把相对增量夹紧到 BLE 线帧的 8-bit 有符号范围 [-127, 127] */
int hid_clamp_delta(int v);

/*
 * hid_pack_report — 打包为 3 字节相对 report。
 *   out      : 至少 HID_REPORT_LEN 字节的输出缓冲
 *   buttons  : HID_BTN_LEFT / 0（抬起）
 *   dx, dy   : 相对增量（像素/单位），越界由本函数夹紧到 [-127,127]
 * 返回写入字节数（恒为 HID_REPORT_LEN）；参数为 NULL 返回 0。
 */
int hid_pack_report(uint8_t *out, uint8_t buttons, int dx, int dy);

/*
 * hid_report_valid — 发射前的守门校验：长度正确即视为合法。
 */
int hid_report_valid(const uint8_t *report, size_t len);

/*
 * hid_pack_keyboard — 打包 8 字节键盘 report。
 *   out      : 至少 HID_KEYBOARD_REPORT_LEN 字节
 *   modifier : HID_MOD_* 位掩码
 *   keys     : 6 个 keycode（Usage Page 0x07），0 = 无键
 * 布局：[modifier][reserved=0][k0..k5]
 * 返回写入字节数（恒为 8）；out 为 NULL 返回 0。
 */
int hid_pack_keyboard(uint8_t *out, uint8_t modifier, const uint8_t keys[6]);

/*
 * hid_pack_consumer — 打包 2 字节 Consumer Control report。
 *   usage : Usage Page 0x0C 的 16-bit usage（如 0x0223 = Home），0 = 抬起
 * 布局：[usage_lo][usage_hi]（小端）
 * 返回写入字节数（恒为 2）；out 为 NULL 返回 0。
 */
int hid_pack_consumer(uint8_t *out, uint16_t usage);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_HID_REPORT_H */
