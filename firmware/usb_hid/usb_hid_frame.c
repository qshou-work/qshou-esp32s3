/*
 * usb_hid/usb_hid_frame.c — BLE 线帧 → USB 线帧转换实现（纯逻辑）
 *
 * 用一张映射表表达「BLE Report ID → (USB 接口, USB Report ID, 长度)」，
 * 而不是 if-else 链 —— 这样描述符契约（usb_hid_frame.h 头注释的表格）
 * 在代码里有唯一一处可读来源，改动时不会漏掉某个分支。
 */
#include "usb_hid/usb_hid_frame.h"
#include "ble_hid/hid_report.h"

#include <string.h>

/* BLE 侧 Report ID → USB 侧落点。顺序与 hid_report.h 的 RID 定义一致。 */
typedef struct {
    uint8_t ble_rid;    /* BLE/HOGP Report ID（1/2/3） */
    uint8_t itf;        /* USB 接口号 */
    uint8_t usb_rid;    /* USB Report ID；USB_HID_RID_NONE = 不加前缀 */
    uint8_t data_len;   /* 纯数据长度（BLE 线帧长度） */
    uint8_t wire_len;   /* USB 线上长度（含前缀） */
} usb_frame_map_t;

static const usb_frame_map_t s_map[] = {
    /* 鼠标：接口 1，RID 1，3 → 4 字节 */
    { HID_REPORT_ID_MOUSE,    USB_HID_ITF_POINTER,  USB_HID_RID_MOUSE,
      HID_REPORT_LEN,          USB_HID_FRAME_MOUSE },

    /* 键盘：**接口 0（boot）**，**无 Report ID**，8 → 8 字节原样
     * —— 这是 BIOS 兼容的命门，见头注释。 */
    { HID_REPORT_ID_KEYBOARD, USB_HID_ITF_KEYBOARD, USB_HID_RID_NONE,
      HID_KEYBOARD_REPORT_LEN, USB_HID_FRAME_KEYBOARD },

    /* Consumer：接口 1，接口内重新编号为 RID 2，2 → 3 字节 */
    { HID_REPORT_ID_CONSUMER, USB_HID_ITF_POINTER,  USB_HID_RID_CONSUMER,
      HID_CONSUMER_REPORT_LEN, USB_HID_FRAME_CONSUMER },
};

#define MAP_COUNT ((int)(sizeof(s_map) / sizeof(s_map[0])))

static const usb_frame_map_t *find_map(uint8_t ble_rid)
{
    for (int i = 0; i < MAP_COUNT; i++) {
        if (s_map[i].ble_rid == ble_rid)
            return &s_map[i];
    }
    return NULL;
}

int usb_hid_ble_rid_supported(uint8_t ble_rid)
{
    return find_map(ble_rid) ? 1 : 0;
}

size_t usb_hid_frame_len(uint8_t ble_rid)
{
    const usb_frame_map_t *m = find_map(ble_rid);
    return m ? (size_t)m->wire_len : 0;
}

int usb_hid_pack(uint8_t ble_rid, const uint8_t *data, size_t len,
                 uint8_t *out, size_t cap, usb_hid_frame_meta_t *meta)
{
    if (!data || !out)
        return -1;

    const usb_frame_map_t *m = find_map(ble_rid);
    if (!m)
        return -1;                          /* 未知 Report ID：无 USB 映射 */

    /* 长度契约：与描述符声明不一致就拒发。宁可丢一帧，也不让 host 收到
     * 错位报文（错位会被解释成「乱按键」或触发 host 侧解析异常）。 */
    if (len != (size_t)m->data_len)
        return -1;

    if (cap < (size_t)m->wire_len)
        return -1;

    size_t w = 0;

    /* 仅当该接口使用 Report ID 时才写前缀；boot 键盘 rid=0 → 不写，
     * 首字节保持 modifier，满足 boot 协议固定 8 字节格式。 */
    if (m->usb_rid != USB_HID_RID_NONE)
        out[w++] = m->usb_rid;

    memcpy(out + w, data, m->data_len);
    w += m->data_len;

    if (meta) {
        meta->itf = m->itf;
        meta->rid = m->usb_rid;
        meta->len = (uint8_t)w;
    }
    return (int)w;
}
