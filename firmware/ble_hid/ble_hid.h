/*
 * ble_hid/ble_hid.h — report 发射与高级动作（依赖 platform 接缝）
 */
#ifndef QSHOU_BLE_HID_H
#define QSHOU_BLE_HID_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * ble_hid_emit_report — 校验后透传发射一个已打包的**相对** report（3 字节）。
 * 返回 0 成功；report 非法或 platform_ble_emit 失败返回 -1。
 * 手势回放（gesture_task）走此路径。
 */
int ble_hid_emit_report(const uint8_t *report, size_t len);

/*
 * 设备端轨迹回放（绕开 host 注入的沙箱限速）。
 * 一次 HTTP 触发 → ESP32 以原生 BLE 速度（step_ms）连发一串 report，
 * host 侧能看到连贯的「按下→拖动→抬起」，而不是被限速掐成离散点。
 */
typedef enum {
    GESTURE_LINE = 0,
    GESTURE_BOX,
    GESTURE_CIRCLE,
    /* 单方向直线拖动/移动：位移由 dx/dy 直接给出（相对像素，含方向）。
     * DIR 完全无状态——不依赖虚拟光标，因此不会出现「第一步朝旧方向补差」、
     * 负坐标回绕等失步问题。这是相对鼠标下四向移动/拖拽的标准做法。
     * 是否按住按键由 spec.press 决定：0=纯移动（光标滑动，不点击），
     * 非零=按住（用于拖拽）。 */
    GESTURE_DIR,
    /* 原地点击（相对鼠标专用）：零位移 down→up 会被安卓相对鼠标 host 当作
     * 无效/抖动丢弃，必须有至少一帧「按下 + 非零位移」来锚定坐标触发 ACTION_DOWN。
     * 用 1px 净零抖动（down(0,0)→down(+1,0)→up(-1,0)）实现，光标不漂移。 */
    GESTURE_TAP
} gesture_type_t;

typedef struct {
    uint8_t buttons;  /* HID_BTN_LEFT / 0（抬起） */
    int16_t dx, dy;   /* 相对上一帧的增量（像素，已夹紧到 [-127,127] 范围） */
} gesture_pt_t;

typedef struct {
    gesture_type_t type;
    int steps;        /* 每条边插值帧数 (>=1) */
    int step_ms;      /* 帧间隔 (ms, 建议 60~120) */
    int dx, dy;       /* 仅 GESTURE_DIR 使用：总位移（相对像素，含方向） */
    uint8_t press;    /* 仅 GESTURE_DIR 使用：非零=按住(拖拽/点击)，0=纯移动不按键 */
} gesture_spec_t;

/* 纯几何：把 spec 展开成逐帧相对增量序列写入 out，返回点数；空间不足返回 -1
 * 输出约定（相对模式）：第 0 帧 = （按需）按下（零位移），中间帧 = 带增量的
 * （拖动/移动），末帧 = 抬起（零位移）。box/line/circle 恒为「按住绘制」；
 * GESTURE_DIR 由 spec.press 决定：非零=按住(拖拽/点击)，0=纯移动不按键。
 * box/circle 增量之和恒为 0 → 光标精确回到起点。 */
int ble_hid_build_gesture_points(const gesture_spec_t *spec,
                                 gesture_pt_t *out, int max);

/* 在设备上异步播放一段轨迹（内部起任务，立即返回）。返回 0 成功，1 忙，<0 参数错。 */
int ble_hid_play_gesture(const gesture_spec_t *spec);

/* ══════════════════════════════════════════════════════════════════════
 * 按键（键盘 / Consumer Control）—— 复合 HID 的第二个 Report 类别
 * ══════════════════════════════════════════════════════════════════════ */

/* 按键帧最大字节数（键盘 report = 8 字节，Consumer = 2 字节） */
#define HID_KEY_FRAME_MAX  8

/*
 * ble_hid_key_frames — 纯映射：把按键名展开成「按下帧 + 抬起帧」。
 *   name    : 命名键（"HOME"/"BACK"/"VOL_UP"/"ENTER"/"ESC"/"TAB"/"SPACE"/
 *             "BACKSPACE"/"DELETE"/"UP"/"DOWN"/"LEFT"/"RIGHT"/"MUTE"/
 *             "PLAY_PAUSE"/"NEXT"/"PREV"/"POWER"）或单个 ASCII 字符（"a"/"1"/"/"…）
 *   rid     : 出参，Report ID（2=键盘 / 3=Consumer）
 *   len     : 出参，帧字节数（8 / 2）
 *   press   : 出参，按下帧（至少 HID_KEY_FRAME_MAX 字节）
 *   release : 出参，抬起帧（同长）
 * 返回 0 成功；名称未知 / 参数为 NULL 返回 -1。
 */
int ble_hid_key_frames(const char *name, uint8_t *rid, uint8_t *len,
                       uint8_t press[HID_KEY_FRAME_MAX],
                       uint8_t release[HID_KEY_FRAME_MAX]);

/*
 * ble_hid_send_key — 把一次按键（按下→抬起）排入**与手势共用的串行队列**。
 * 共用队列是硬约束：按键帧若插进正在回放的手势序列，会破坏 box/circle 的
 * 净位移闭合（真机 bug 反推）。返回 0 成功入队 / 1 忙（队列满）/ <0 名称未知。
 */
int ble_hid_send_key(const char *name);

#ifdef __cplusplus
}
#endif

#endif /* QSHOU_BLE_HID_H */
