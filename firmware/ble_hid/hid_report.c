/*
 * ble_hid/hid_report.c — 3 字节相对坐标 report 打包实现（纯逻辑）
 *
 * 布局：
 *   byte0 : buttons（左键位）
 *   byte1 : dX（相对增量，8-bit 有符号）
 *   byte2 : dY（相对增量，8-bit 有符号）
 */
#include "ble_hid/hid_report.h"

/* 把增量夹紧到 BLE 线帧的 8-bit 有符号范围 [-127, 127] */
int hid_clamp_delta(int v)
{
    if (v < HID_DELTA_MIN) return HID_DELTA_MIN;
    if (v > HID_DELTA_MAX) return HID_DELTA_MAX;
    return v;
}

int hid_pack_report(uint8_t *out, uint8_t buttons, int dx, int dy)
{
    if (!out)
        return 0;
    out[0] = (uint8_t)(buttons & 0xFF);
    out[1] = (uint8_t)(int8_t)hid_clamp_delta(dx);
    out[2] = (uint8_t)(int8_t)hid_clamp_delta(dy);
    return HID_REPORT_LEN;
}

int hid_report_valid(const uint8_t *report, size_t len)
{
    if (!report || len != HID_REPORT_LEN)
        return 0;
    return 1;
}

int hid_pack_keyboard(uint8_t *out, uint8_t modifier, const uint8_t keys[6])
{
    if (!out)
        return 0;
    out[0] = modifier;
    out[1] = 0;                       /* reserved（boot 键盘协议保留字节） */
    for (int i = 0; i < 6; i++)
        out[2 + i] = keys ? keys[i] : 0;
    return HID_KEYBOARD_REPORT_LEN;
}

int hid_pack_consumer(uint8_t *out, uint16_t usage)
{
    if (!out)
        return 0;
    out[0] = (uint8_t)(usage & 0xFF);         /* 小端 */
    out[1] = (uint8_t)((usage >> 8) & 0xFF);
    return HID_CONSUMER_REPORT_LEN;
}
