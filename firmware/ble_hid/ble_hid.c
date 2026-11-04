/*
 * ble_hid/ble_hid.c — report 发射与高级动作
 *
 * 真实发射走 platform_ble_emit（seam_command_dispatch）。固件在这里做
 * 校验 + 坐标换算 + 透传，不解析指令语义（满足 R20 密文约束）。
 *
 * 设备类型：相对鼠标（Relative Mouse，Generic Desktop / Mouse + 相对 X/Y）。
 *   - BLE 线帧 = 3 字节 [buttons][dX][dY]，X/Y 为相对增量（8-bit 有符号）。
 *   - host 把每帧 (dX,dY) 当作「系统光标相对上一帧的位移」累加。
 *
 * 为何用相对鼠标（见 hid_descriptor.h 头注释）：实测 host 为安卓手机，对绝对坐标
 * 形态（Digitizer / Absolute Mouse）均失败；相对鼠标是已验证可用的路径
 * （box 手势此前在此形态下「框线均匀、精准回原点」）。相对模式天然无「绝对坐标
 * 依赖 host 回传光标位置」的失步问题。
 *
 * 本文件不再维护虚拟光标：手势几何直接产出逐帧相对增量，发射时原样打包，
 * 概念上无累计漂移、无「第一步朝旧方向补差」。
 */
#include "ble_hid/ble_hid.h"
#include "ble_hid/hid_report.h"
#include "hid_router/hid_router.h"
#include "platform/platform.h"

#include <math.h>   /* GESTURE_CIRCLE 用 cos/sin */
#include <string.h> /* 按键名匹配用 strcmp/strlen */

/* M_PI 不在 C11 标准里（它是 glibc/POSIX 扩展）：gcc 默认的 gnu11 环境会
 * 悄悄暴露它，但严格 -std=c11 或 clang/zig cc / MSVC 下没有定义。
 * 手写兜底，保证任意工具链都能编过；平台已提供时以其为准。 */
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

int ble_hid_emit_report(const uint8_t *report, size_t len)
{
    if (!hid_report_valid(report, len))
        return -1;
    /* 走路由层而非直接 platform_ble_emit：USB 挂载时优先走 USB（带外控制），
     * 未挂载时回落 BLE —— 与改造前行为完全一致（BLE 是唯一注册后端时）。
     * 路由层保证「只发一个后端」，不会出现 BLE+USB 双发导致重复输入。 */
    return hid_router_emit(HID_REPORT_ID_MOUSE, report, len);
}

/*
 * ble_hid_build_gesture_points — 纯几何：把 spec 展开成逐帧相对增量。
 * 输出约定：第 0 帧 = （按需）按下（零位移），中间帧 = 带增量的（拖动/移动），
 * 末帧 = 抬起（零位移）。box/line/circle 恒按住；GESTURE_DIR 由 spec.press 决定。
 *
 * 相对鼠标：每帧 dx/dy 是**相对上一帧的位移**（像素），host 把光标
 * 从当前位置移动 (dx,dy)。box/circle 增量之和恒为 0 → 精确回到起点。
 *
 * 几何尺度用「相对像素」，与屏幕分辨率解耦：
 *   box/line 单边 ~120px，circle 半径 ~40px（都在单帧 ±127 安全范围内；
 *   即便 steps=1，单边 120 < 127，也不会触发夹取导致位移缩水）。
 */
int ble_hid_build_gesture_points(const gesture_spec_t *spec,
                                 gesture_pt_t *out, int max)
{
    if (!spec || !out || max < 8)
        return -1;

    int steps = spec->steps < 1 ? 1 : spec->steps;
    int n = 0;

    /* 是否按住按键：box/line/circle 恒为「拖拽绘制」；GESTURE_DIR 由 spec.press 决定
     * （0=纯移动不按键；非零=按住，用于点击/拖拽）。 */
    int hold = (spec->type == GESTURE_DIR) ? (spec->press ? 1 : 0) : 1;

    /* 第 0 帧：在原地（不）按下 */
    out[n].buttons = hold ? HID_BTN_LEFT : 0; out[n].dx = 0; out[n].dy = 0; n++;

    if (spec->type == GESTURE_LINE) {
        int side = 120;                    /* 单边位移（相对像素） */
        if (steps > side) steps = side;
        int per = (side + steps - 1) / steps;
        for (int k = 1; k <= steps && n < max; k++) {
            out[n].buttons = HID_BTN_LEFT; out[n].dx = (int16_t)per; out[n].dy = 0; n++;
        }
    } else if (spec->type == GESTURE_BOX) {
        /* 顺时针方框：右 → 下 → 左 → 上 */
        int side = 120;                    /* 相对像素 */
        if (steps > side) steps = side;
        int per = (side + steps - 1) / steps;
        const int16_t ex[4] = { 1, 0, -1,  0};
        const int16_t ey[4] = { 0, 1,  0, -1};
        for (int e = 0; e < 4; e++) {
            for (int k = 1; k <= steps && n < max; k++) {
                out[n].buttons = HID_BTN_LEFT;
                out[n].dx = (int16_t)(ex[e] * per);
                out[n].dy = (int16_t)(ey[e] * per);
                n++;
            }
        }
    } else if (spec->type == GESTURE_DIR) {
        /* 单方向直线拖动：把总位移 (dx,dy) 均分成 steps 段。
         * 用「累计取整」而非「每段整除」，保证各帧之和精确等于总位移
         * （整除会把余数丢掉，长距离下误差累积可见）。每帧增量夹紧到 ±127
         * （BLE 线帧 X/Y 是 8-bit 有符号；常规 UI 用法 steps 足够大不会触发）。 */
        int prev_x = 0, prev_y = 0;
        for (int k = 1; k <= steps && n < max; k++) {
            int cx = spec->dx * k / steps;
            int cy = spec->dy * k / steps;
            int sx = hid_clamp_delta(cx - prev_x);
            int sy = hid_clamp_delta(cy - prev_y);
            out[n].buttons = hold ? HID_BTN_LEFT : 0;
            out[n].dx = (int16_t)sx;
            out[n].dy = (int16_t)sy;
            prev_x += sx;
            prev_y += sy;
            n++;
        }
    } else if (spec->type == GESTURE_CIRCLE) {
        int R = 40;                        /* 相对像素 */
        int nseg = steps * 4;
        if (nseg < 8) nseg = 8;
        double prevx = 0.0, prevy = 0.0;
        for (int k = 1; k <= nseg && n < max; k++) {
            double a = 2.0 * M_PI * (double)k / (double)nseg;
            double cx = R * cos(a), cy = R * sin(a);
            out[n].buttons = HID_BTN_LEFT;
            out[n].dx = (int16_t)hid_clamp_delta((int)lround(cx - prevx));
            out[n].dy = (int16_t)hid_clamp_delta((int)lround(cy - prevy));
            prevx = cx; prevy = cy;
            n++;
        }
    } else { /* GESTURE_TAP: 原地点击（相对鼠标下用 1px 净零抖动锚定按下） */
        /* 安卓相对鼠标 HID host 会丢弃「零位移 down→up」（视为无效/抖动），
         * 必须有至少一帧「按下 + 非零位移」来锚定坐标，ACTION_DOWN 才会触发。
         * 帧序：第 0 帧按下(0,0)由函数顶部通用段产出；这里只补「按住并 +1px」
         * （锚定）与「抬起并 -1px」（净位移 0，光标回原位）各一帧。
         * ⚠️ 不要再补一帧 down(0,0) —— 顶部通用段已经发过，重复的零位移
         *    down 帧对 host 无意义（这是首次运行 host 单元测试时抓到的：
         *    pts[1] 本应是 +1px 锚定帧，实得重复 down(0,0)）。 */
        if (n < max) { out[n].buttons = HID_BTN_LEFT; out[n].dx = 1; out[n].dy = 0; n++; }
        if (n < max) { out[n].buttons = 0;            out[n].dx = -1; out[n].dy = 0; n++; }
    }

    /* 末帧：抬起（零位移收尾） */
    if (n < max) {
        out[n].buttons = 0; out[n].dx = 0; out[n].dy = 0; n++;
    }
    return n;
}

/* ══════════════════════════════════════════════════════════════════════
 * 按键：名称 → 键盘 / Consumer report（纯逻辑，可单测）
 * ══════════════════════════════════════════════════════════════════════ */

/* 命名键表。code 的含义随 rid 变：
 *   rid = 3（Consumer, Page 0x0C）：16-bit usage
 *   rid = 2（键盘,     Page 0x07）：8-bit keycode
 * 表中名称全部大写。⚠️ 这是**规格 supported_actions** 的直接落地
 *（HOME/BACK/VOL_UP/VOL_DOWN/POWER + 常用编辑键）。 */
typedef struct {
    const char *name;
    uint8_t     rid;
    uint16_t    code;
} key_name_t;

static const key_name_t s_key_names[] = {
    /* ── Consumer Control（Page 0x0C）── */
    { "HOME",        HID_REPORT_ID_CONSUMER, 0x0223 },
    { "BACK",        HID_REPORT_ID_CONSUMER, 0x0224 },
    { "MENU",        HID_REPORT_ID_CONSUMER, 0x0040 },
    { "SEARCH",      HID_REPORT_ID_CONSUMER, 0x0221 },
    { "VOL_UP",      HID_REPORT_ID_CONSUMER, 0x00E9 },
    { "VOL_DOWN",    HID_REPORT_ID_CONSUMER, 0x00EA },
    { "MUTE",        HID_REPORT_ID_CONSUMER, 0x00E2 },
    { "PLAY_PAUSE",  HID_REPORT_ID_CONSUMER, 0x00CD },
    { "NEXT",        HID_REPORT_ID_CONSUMER, 0x00B5 },
    { "PREV",        HID_REPORT_ID_CONSUMER, 0x00B6 },
    { "POWER",       HID_REPORT_ID_CONSUMER, 0x0030 },
    { "BRIGHT_UP",   HID_REPORT_ID_CONSUMER, 0x006F },
    { "BRIGHT_DOWN", HID_REPORT_ID_CONSUMER, 0x0070 },
    /* ── 键盘命名键（Page 0x07）── */
    { "ENTER",       HID_REPORT_ID_KEYBOARD, 0x28 },
    { "ESC",         HID_REPORT_ID_KEYBOARD, 0x29 },
    { "BACKSPACE",   HID_REPORT_ID_KEYBOARD, 0x2A },
    { "TAB",         HID_REPORT_ID_KEYBOARD, 0x2B },
    { "SPACE",       HID_REPORT_ID_KEYBOARD, 0x2C },
    { "CAPSLOCK",    HID_REPORT_ID_KEYBOARD, 0x39 },
    { "DELETE",      HID_REPORT_ID_KEYBOARD, 0x4C },
    { "RIGHT",       HID_REPORT_ID_KEYBOARD, 0x4F },
    { "LEFT",        HID_REPORT_ID_KEYBOARD, 0x50 },
    { "DOWN",        HID_REPORT_ID_KEYBOARD, 0x51 },
    { "UP",          HID_REPORT_ID_KEYBOARD, 0x52 },
};

/* 单字符 ASCII →（keycode, 修饰键）。返回 0 成功，-1 不支持。 */
static int ascii_to_key(char c, uint8_t *code, uint8_t *mod)
{
    *mod = 0;
    if (c >= 'a' && c <= 'z') { *code = (uint8_t)(0x04 + (c - 'a')); return 0; }
    if (c >= 'A' && c <= 'Z') { *code = (uint8_t)(0x04 + (c - 'A')); *mod = HID_MOD_LSHIFT; return 0; }
    if (c >= '1' && c <= '9') { *code = (uint8_t)(0x1E + (c - '1')); return 0; }
    switch (c) {
    case '0':  *code = 0x27; return 0;
    case ' ':  *code = 0x2C; return 0;
    case '\n': case '\r': *code = 0x28; return 0;
    case '\t': *code = 0x2B; return 0;
    case '-':  *code = 0x2D; return 0;
    case '=':  *code = 0x2E; return 0;
    case '[':  *code = 0x2F; return 0;
    case ']':  *code = 0x30; return 0;
    case '\\': *code = 0x31; return 0;
    case ';':  *code = 0x33; return 0;
    case '\'': *code = 0x34; return 0;
    case '`':  *code = 0x35; return 0;
    case ',':  *code = 0x36; return 0;
    case '.':  *code = 0x37; return 0;
    case '/':  *code = 0x38; return 0;
    /* Shift 组合符号（keycode 同基键，另加 Left Shift） */
    case '!':  *code = 0x1E; *mod = HID_MOD_LSHIFT; return 0;
    case '@':  *code = 0x1F; *mod = HID_MOD_LSHIFT; return 0;
    case '#':  *code = 0x20; *mod = HID_MOD_LSHIFT; return 0;
    case '$':  *code = 0x21; *mod = HID_MOD_LSHIFT; return 0;
    case '%':  *code = 0x22; *mod = HID_MOD_LSHIFT; return 0;
    case '^':  *code = 0x23; *mod = HID_MOD_LSHIFT; return 0;
    case '&':  *code = 0x24; *mod = HID_MOD_LSHIFT; return 0;
    case '*':  *code = 0x25; *mod = HID_MOD_LSHIFT; return 0;
    case '(':  *code = 0x26; *mod = HID_MOD_LSHIFT; return 0;
    case ')':  *code = 0x27; *mod = HID_MOD_LSHIFT; return 0;
    case '_':  *code = 0x2D; *mod = HID_MOD_LSHIFT; return 0;
    case '+':  *code = 0x2E; *mod = HID_MOD_LSHIFT; return 0;
    case '{':  *code = 0x2F; *mod = HID_MOD_LSHIFT; return 0;
    case '}':  *code = 0x30; *mod = HID_MOD_LSHIFT; return 0;
    case '|':  *code = 0x31; *mod = HID_MOD_LSHIFT; return 0;
    case ':':  *code = 0x33; *mod = HID_MOD_LSHIFT; return 0;
    case '"':  *code = 0x34; *mod = HID_MOD_LSHIFT; return 0;
    case '~':  *code = 0x35; *mod = HID_MOD_LSHIFT; return 0;
    case '<':  *code = 0x36; *mod = HID_MOD_LSHIFT; return 0;
    case '>':  *code = 0x37; *mod = HID_MOD_LSHIFT; return 0;
    case '?':  *code = 0x38; *mod = HID_MOD_LSHIFT; return 0;
    default:   return -1;
    }
}

int ble_hid_key_frames(const char *name, uint8_t *rid, uint8_t *len,
                       uint8_t press[HID_KEY_FRAME_MAX],
                       uint8_t release[HID_KEY_FRAME_MAX])
{
    if (!name || !rid || !len || !press || !release)
        return -1;

    /* 1) 命名键查表 */
    for (size_t i = 0; i < sizeof s_key_names / sizeof s_key_names[0]; i++) {
        if (strcmp(name, s_key_names[i].name) == 0) {
            if (s_key_names[i].rid == HID_REPORT_ID_CONSUMER) {
                *rid = HID_REPORT_ID_CONSUMER;
                *len = HID_CONSUMER_REPORT_LEN;
                hid_pack_consumer(press,   s_key_names[i].code);
                hid_pack_consumer(release, 0);
            } else {
                uint8_t keys[6] = { (uint8_t)s_key_names[i].code, 0, 0, 0, 0, 0 };
                *rid = HID_REPORT_ID_KEYBOARD;
                *len = HID_KEYBOARD_REPORT_LEN;
                hid_pack_keyboard(press,   0, keys);
                hid_pack_keyboard(release, 0, NULL);   /* 全 0 = 抬起 */
            }
            return 0;
        }
    }

    /* 2) 单个 ASCII 字符 → 键盘 */
    if (name[0] != '\0' && name[1] == '\0') {
        uint8_t code = 0, mod = 0;
        if (ascii_to_key(name[0], &code, &mod) != 0)
            return -1;
        uint8_t keys[6] = { code, 0, 0, 0, 0, 0 };
        *rid = HID_REPORT_ID_KEYBOARD;
        *len = HID_KEYBOARD_REPORT_LEN;
        hid_pack_keyboard(press,   mod, keys);
        hid_pack_keyboard(release, 0, NULL);
        return 0;
    }
    return -1;
}
