/*
 * ble_hid/hid_descriptor.h — Relative Mouse HID Report Descriptor
 *
 * 纯逻辑、零硬件依赖。host 与 esp32 共用同一份描述符字节，
 * 保证「设备对中心端宣告的能力」与「固件实际发送的 3 字节 report」严格一致。
 *
 * 模型：**Relative Mouse**（Generic Desktop → Mouse，X/Y 为**相对**坐标，
 *       Input 标志 0x06 = Data+Var+Rel）。
 *
 * ⚠️ 为何是相对鼠标：实测 host 为**安卓手机**（BLE HID）。安卓对相对鼠标支持可靠
 *   （显示系统指针并真实移动）；而绝对坐标形态都失败——
 *     · Digitizer/Touch Screen → 被当触摸屏（只点按不移动，光标不跟手）；
 *     · Absolute Mouse（Generic Desktop/Mouse + 绝对 X/Y）→ 不创建鼠标设备（完全无反应）。
 *   故回退到相对鼠标，这是已验证可用的路径（box 手势此前在此形态下「框线均匀、精准回原点」）。
 *
 * 约束：
 *   Usage Page 0x01 (Generic Desktop) / Usage 0x02 (Mouse)
 *   Button(0x01) 位 + 7 位 padding = 1 字节
 *   X(0x30) · Y(0x31) 各 8 bit 相对，逻辑范围 [-127, 127]
 *
 * 复合设备（同一份 report map，三个 Report ID）：
 *   Report ID 0x01 : 相对鼠标   3 字节 [buttons][dX][dY]
 *   Report ID 0x02 : 键盘       8 字节 [modifier][reserved][k0..k5]（Page 0x07）
 *   Report ID 0x03 : Consumer   2 字节 [usage_lo][usage_hi]（Page 0x0C，16-bit）
 *   —— 报告数受 CONFIG_BT_NIMBLE_SVC_HID_MAX_RPTS=3 限制，正好用满。
 *   —— 各 Report 在 HOGP 下是独立特征；线帧均不带 Report ID 前缀。
 */

#ifndef QSHOU_HID_DESCRIPTOR_H
#define QSHOU_HID_DESCRIPTOR_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * qshou_hid_report_map — Relative Mouse report descriptor。
 * 长度：qshou_hid_report_map_len。
 */
extern const uint8_t qshou_hid_report_map[];
extern const size_t  qshou_hid_report_map_len;

/* 供一致性校验：descriptor 声明的 Input 报告长度（字节，不含 Report ID） */
#define QSHOU_HID_DESCRIPTOR_REPORT_BYTES  3      /* 鼠标 RID 1 */
#define QSHOU_HID_KB_DESCRIPTOR_BYTES      8      /* 键盘 RID 2 */
#define QSHOU_HID_CC_DESCRIPTOR_BYTES      2      /* Consumer RID 3 */

/* HID 描述符条目集合（用于本文件自检与文档化，非运行时必需） */
#define QSHOU_HID_USAGE_PAGE_DESKTOP    0x01
#define QSHOU_HID_USAGE_PAGE_KEYBOARD   0x07
#define QSHOU_HID_USAGE_PAGE_CONSUMER   0x0C
#define QSHOU_HID_USAGE_PAGE_DIGITIZER  0x0D
#define QSHOU_HID_USAGE_TOUCHSCREEN     0x04
#define QSHOU_HID_USAGE_FINGER          0x22
#define QSHOU_HID_USAGE_TIP_SWITCH      0x42
#define QSHOU_HID_USAGE_MOUSE           0x02
#define QSHOU_HID_USAGE_KEYBOARD        0x06
#define QSHOU_HID_USAGE_CONSUMER_CONTROL 0x01
#define QSHOU_HID_USAGE_POINTER         0x01
#define QSHOU_HID_USAGE_PAGE_BUTTON     0x09
#define QSHOU_HID_USAGE_X               0x30
#define QSHOU_HID_USAGE_Y               0x31

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_HID_DESCRIPTOR_H */
