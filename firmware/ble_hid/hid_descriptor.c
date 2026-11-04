/*
 * ble_hid/hid_descriptor.c — Relative Mouse report descriptor 字节
 *
 * 纯逻辑、零硬件依赖。逐项对应 hid_descriptor.h 头部注释。
 *
 * ── 字节布局推导（务必与 HID_REPORT_LEN = 3 一致）─────────────────────
 * Report ID 0x01（独立 global item，不计入数据长度），数据字段：
 *
 *     字段                      位/字节      说明
 *     ──────────────────────────────────────────────────
 *     Button (0x01)            1 bit        左键 / 按下
 *     padding                    7 bit        Report Size 7，凑满 1 字节
 *     X (0x30)                  8 bit        相对增量 [-127, 127]
 *     Y (0x31)                  8 bit        相对增量 [-127, 127]
 *     ──────────────────────────────────────────────────
 *     合计 1 (按钮+pad) + 1 (X) + 1 (Y) = 3 字节   ✓
 *
 * ⚠️ 关键选型：这里是 **Relative Mouse**（Generic Desktop / Mouse + 相对 X/Y），
 *    不是绝对坐标！实测 host（安卓）对绝对坐标形态均失败：
 *    Digitizer 被当触摸屏（只点按不移动）；Absolute Mouse 不创建鼠标设备。
 *    相对鼠标是已验证可用的路径：host 把每帧 (dX,dY) 当作「光标相对上一帧的位移」。
 *
 * 描述符为**复合设备**：鼠标(RID 1) + 键盘(RID 2) + Consumer(RID 3)，
 * 总长 125 字节（见 _Static_assert 保护与测试 test_firmware_contract.py）。
 */

#include "ble_hid/hid_descriptor.h"
#include "ble_hid/hid_report.h"

/* HID 短条目前缀：item = (bTag << 4) | (bType << 2) | bSize */
#define HID_ITEM(tag, type, size)  ((uint8_t)(((tag) << 4) | ((type) << 2) | (size)))
#define HID_TYPE_MAIN   0
#define HID_TYPE_GLOBAL 1
#define HID_TYPE_LOCAL  2

/* Global items */
#define HID_TAG_USAGE_PAGE  0
#define HID_TAG_LOGICAL_MIN 1
#define HID_TAG_LOGICAL_MAX 2
#define HID_TAG_REPORT_SIZE 7
#define HID_TAG_REPORT_ID   8
#define HID_TAG_REPORT_COUNT 9

/* Local items */
#define HID_TAG_USAGE       0
#define HID_TAG_USAGE_MIN   1
#define HID_TAG_USAGE_MAX   2

/* Main items */
#define HID_TAG_INPUT       8
#define HID_TAG_COLLECTION  10
#define HID_TAG_END_COLLECT 12

/* Collection 类型 */
#define HID_COLLECTION_APPLICATION 0x01
#define HID_COLLECTION_PHYSICAL    0x00

/* Input 标志位（HID 1.11 §6.2.2.5）
 *     bit0 = Constant(1) / Data(0)
 *     bit1 = Variable(1) / Array(0)
 *     bit2 = Relative(1) / Absolute(0)
 * 按钮字段：     Data + Variable + Absolute = 0x02
 * padding 字段： Constant + Variable + Absolute = 0x03
 * X/Y 字段：     Data + Variable + Relative   = 0x06   ← 相对坐标的关键（bit2=1） */
#define HID_INPUT_DATA_VAR_ABS  (0x02)
#define HID_INPUT_CONST_PAD     (0x03)
#define HID_INPUT_DATA_VAR_REL  (0x06)

const uint8_t qshou_hid_report_map[] = {
    /* ── Usage Page: Generic Desktop (0x01) ── */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_DESKTOP,

    /* ── Usage: Mouse (0x02) ── */
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_MOUSE,

    /* ── Collection: Application ── */
    HID_ITEM(HID_TAG_COLLECTION, HID_TYPE_MAIN, 1), HID_COLLECTION_APPLICATION,

    /* ── Report ID 0x01（与 hid_report.h 的 HID_REPORT_ID 一致）──
     * ⚠️ 该 Report ID **只用于标识**，不出现在线帧里：
     *   - host 侧：BLE HOGP 用 Report Reference 描述符(0x2908) 把本
     *     characteristic 关联到 ID 1，载荷里不再重复 ID；
     *   - 固件侧：esp_hid 的 find_report(id) 用它查 notify 句柄，
     *     esp_hidd_dev_input_set(dev, idx, id, data, len) 的 id 参数就是它。
     * NimBLE 的 input_set 原样发 data（nimble_hidd.c:591 mbuf_from_flat），
     * 所以线帧 = 3 字节相对 report，切勿再手动补 0x01 前缀（补了整体错位一字节）。 */
    HID_ITEM(HID_TAG_REPORT_ID, HID_TYPE_GLOBAL, 1), HID_REPORT_ID,

    /* ── Usage: Pointer (0x01) ── */
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_POINTER,

    /* ── Collection: Physical ── */
    HID_ITEM(HID_TAG_COLLECTION, HID_TYPE_MAIN, 1), HID_COLLECTION_PHYSICAL,

    /* ── 字段 1：Button（1 bit）── */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_BUTTON,
    HID_ITEM(HID_TAG_USAGE_MIN,   HID_TYPE_LOCAL,  1), 0x01,
    HID_ITEM(HID_TAG_USAGE_MAX,   HID_TYPE_LOCAL,  1), 0x01,
    HID_ITEM(HID_TAG_LOGICAL_MIN, HID_TYPE_GLOBAL, 1), 0x00,
    HID_ITEM(HID_TAG_LOGICAL_MAX, HID_TYPE_GLOBAL, 1), 0x01,
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x01,          /* 1 bit  */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x01,         /* ×1     */
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), HID_INPUT_DATA_VAR_ABS,

    /* ── padding 7 bit（凑满第 1 字节）── */
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x07,          /* 7 bit  */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x01,         /* ×1     */
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), HID_INPUT_CONST_PAD,

    /* ── 字段 2/3：X / Y（各 8 bit，相对坐标 [-127, 127]）──
     * 两个 Usage 一起声明、再用单个 Input(Report Count=2) 生成 2 个 8 位字段。
     * （Report Count 是 global item 会沿用，分两次声明会重复生成字段导致错位。） */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_DESKTOP,
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_X,    /* 0x30   */
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_Y,    /* 0x31   */
    HID_ITEM(HID_TAG_LOGICAL_MIN, HID_TYPE_GLOBAL, 2), 0x81, 0xFF,    /* -127   */
    HID_ITEM(HID_TAG_LOGICAL_MAX, HID_TYPE_GLOBAL, 2), 0x7F, 0x00,   /* 127    */
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x08,         /* 8 bit  */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x02,        /* ×2     */
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), HID_INPUT_DATA_VAR_REL,

    /* ── 结束 Physical / Application Collection ── */
    HID_ITEM(HID_TAG_END_COLLECT, HID_TYPE_MAIN, 0),
    HID_ITEM(HID_TAG_END_COLLECT, HID_TYPE_MAIN, 0),

    /* ══════════════════════════════════════════════════════════════════
     * Report ID 0x02 —— 键盘（Usage Page 0x07，标准 boot 风格 8 字节）
     * 布局：[modifier(8bit)][reserved(8bit,Const)][6×keycode(8bit Array)]
     * ══════════════════════════════════════════════════════════════════ */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_DESKTOP,
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_KEYBOARD,
    HID_ITEM(HID_TAG_COLLECTION, HID_TYPE_MAIN, 1), HID_COLLECTION_APPLICATION,
    HID_ITEM(HID_TAG_REPORT_ID, HID_TYPE_GLOBAL, 1), HID_REPORT_ID_KEYBOARD,

    /* 修饰键：8 个 1-bit 字段（Usage 0xE0-0xE7）→ 合计 1 字节 */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_KEYBOARD,
    HID_ITEM(HID_TAG_USAGE_MIN,   HID_TYPE_LOCAL,  1), 0xE0,
    HID_ITEM(HID_TAG_USAGE_MAX,   HID_TYPE_LOCAL,  1), 0xE7,
    HID_ITEM(HID_TAG_LOGICAL_MIN, HID_TYPE_GLOBAL, 1), 0x00,
    HID_ITEM(HID_TAG_LOGICAL_MAX, HID_TYPE_GLOBAL, 1), 0x01,
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x01,          /* 1 bit  */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x08,         /* ×8     */
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), HID_INPUT_DATA_VAR_ABS,

    /* 保留字节（Const）→ 1 字节 */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x01,
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x08,
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), HID_INPUT_CONST_PAD,

    /* 键码数组：6 个 8-bit 字段（Array，0 = 无键）→ 6 字节 */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x06,
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x08,
    HID_ITEM(HID_TAG_LOGICAL_MIN, HID_TYPE_GLOBAL, 1), 0x00,
    HID_ITEM(HID_TAG_LOGICAL_MAX, HID_TYPE_GLOBAL, 2), 0xFF, 0x00,   /* 255    */
    HID_ITEM(HID_TAG_USAGE_MIN,   HID_TYPE_LOCAL,  1), 0x00,
    HID_ITEM(HID_TAG_USAGE_MAX,   HID_TYPE_LOCAL,  1), 0xFF,
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), 0x00,   /* Data,Array,Abs */

    HID_ITEM(HID_TAG_END_COLLECT, HID_TYPE_MAIN, 0),

    /* ══════════════════════════════════════════════════════════════════
     * Report ID 0x03 —— Consumer Control（Usage Page 0x0C，2 字节）
     * 16-bit Array usage：HOME(0x0223)/BACK(0x0224)/VOL(0x00E9/E0x00EA)…
     * 因 Usage Min=0、步长 1，报值即 usage 码（0 = 抬起）。
     * ══════════════════════════════════════════════════════════════════ */
    HID_ITEM(HID_TAG_USAGE_PAGE, HID_TYPE_GLOBAL, 1), QSHOU_HID_USAGE_PAGE_CONSUMER,
    HID_ITEM(HID_TAG_USAGE, HID_TYPE_LOCAL, 1), QSHOU_HID_USAGE_CONSUMER_CONTROL,
    HID_ITEM(HID_TAG_COLLECTION, HID_TYPE_MAIN, 1), HID_COLLECTION_APPLICATION,
    HID_ITEM(HID_TAG_REPORT_ID, HID_TYPE_GLOBAL, 1), HID_REPORT_ID_CONSUMER,
    HID_ITEM(HID_TAG_LOGICAL_MIN, HID_TYPE_GLOBAL, 1), 0x00,
    HID_ITEM(HID_TAG_LOGICAL_MAX, HID_TYPE_GLOBAL, 2), 0xFF, 0x03,   /* 1023   */
    HID_ITEM(HID_TAG_USAGE_MIN,   HID_TYPE_LOCAL,  1), 0x00,
    HID_ITEM(HID_TAG_USAGE_MAX,   HID_TYPE_LOCAL,  2), 0xFF, 0x03,   /* 1023   */
    HID_ITEM(HID_TAG_REPORT_SIZE, HID_TYPE_GLOBAL, 1), 0x10,         /* 16 bit */
    HID_ITEM(HID_TAG_REPORT_COUNT, HID_TYPE_GLOBAL, 1), 0x01,        /* ×1     */
    HID_ITEM(HID_TAG_INPUT, HID_TYPE_MAIN, 1), 0x00,   /* Data,Array,Abs */
    HID_ITEM(HID_TAG_END_COLLECT, HID_TYPE_MAIN, 0),
};

const size_t qshou_hid_report_map_len = sizeof(qshou_hid_report_map);

/*
 * 编译期一致性守护：descriptor 声明的字段总和必须等于固件 report 长度。
 */
_Static_assert(QSHOU_HID_DESCRIPTOR_REPORT_BYTES == HID_REPORT_LEN,
               "HID descriptor report bytes must match HID_REPORT_LEN");
